"""Service for AUA student email verification via OTP codes with anti-bounce & flood shields."""

import asyncio
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import logging
import re
import secrets
import smtplib
from typing import Any, List, Optional, Set, Tuple

from app.config import settings

logger = logging.getLogger(__name__)

# Validates standard email format and ensures domain ends with @edu.aua.am or @alumni.aua.am
AUA_EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9_.+-]+@(edu|alumni)\.aua\.am$", re.IGNORECASE)
ALLOWED_DOMAINS = ("@edu.aua.am", "@alumni.aua.am")

# Blocked dummy, generic, test, and role prefixes that cause bounce loops
BLOCKED_LOCAL_TERMS: Set[str] = {
    "test", "testing", "student_test", "test_student", "fake", "dummy",
    "sample", "example", "admin", "administrator", "moderator", "mod", "temp",
    "asdf", "qwerty", "aua", "anonymous", "null", "undefined",
}

# Global sliding window for outbound emails across all users
_outbound_timestamps: List[float] = []


def is_valid_aua_email(email: Optional[str]) -> bool:
    """Validate whether an email is a syntactically valid AUA student or alumni email ending with @edu.aua.am or @alumni.aua.am."""
    if not email:
        return False
    clean = email.strip().lower()
    return bool(AUA_EMAIL_REGEX.match(clean))


def get_email_affiliation(email: Optional[str]) -> Optional[str]:
    """Return affiliation ('student' or 'alumni') based on email domain."""
    if not email:
        return None
    clean = email.strip().lower()
    if clean.endswith("@alumni.aua.am"):
        return "alumni"
    elif clean.endswith("@edu.aua.am"):
        return "student"
    return None


def validate_aua_email_format(email: Optional[str]) -> Tuple[bool, Optional[str]]:
    """Validate student or alumni email syntax and block non-existent/dummy test patterns.
    
    AUA emails follow firstname_lastname@edu.aua.am or firstname_lastname@alumni.aua.am.
    Generic, single-word, or dummy email handles are strictly rejected to prevent bounce loops.
    
    Returns:
        (is_valid: bool, error_message: Optional[str])
    """
    if not email:
        return False, "Email address cannot be empty."

    clean = email.strip().lower()
    if not (clean.endswith("@edu.aua.am") or clean.endswith("@alumni.aua.am")):
        return (
            False,
            "The email address must be an official AUA address ending with "
            "<b>@edu.aua.am</b> (current students) or <b>@alumni.aua.am</b> (graduates).",
        )

    if not AUA_EMAIL_REGEX.match(clean):
        return False, "Email contains invalid characters."

    suffix = "@alumni.aua.am" if clean.endswith("@alumni.aua.am") else "@edu.aua.am"
    local_part = clean[:-len(suffix)]

    # Must contain separator between firstname and lastname (underscore, dot, or hyphen)
    parts = [p for p in re.split(r"[._\-]", local_part) if p]
    if len(parts) < 2:
        return (
            False,
            f"AUA emails must include both your first name and last name "
            f"(e.g. <code>firstname_lastname{suffix}</code>).",
        )

    # Check for blocked dummy terms
    for term in BLOCKED_LOCAL_TERMS:
        if term == local_part or term in parts:
            return (
                False,
                f"Generic or test email addresses are not permitted. "
                f"Please enter your real university email (e.g. <code>firstname_lastname{suffix}</code>).",
            )

    # Firstname and lastname parts must each have at least 2 alphabetic characters
    for p in parts[:2]:
        alpha_chars = re.sub(r"[^a-z]", "", p)
        if len(alpha_chars) < 2:
            return (
                False,
                "Invalid name format. Each part of your name must have at least 2 letters.",
            )

    return True, None


def get_current_academic_year(dt: Optional[datetime] = None) -> int:
    """Return the start year of the current academic year.
    
    Academic year starts on September 1st:
    - If date is on or after September 1st, academic year is dt.year (e.g. 2026-09-01 -> 2026).
    - If date is before September 1st, academic year is dt.year - 1 (e.g. 2027-08-31 -> 2026).
    """
    if dt is None:
        dt = datetime.now(timezone.utc)
    return dt.year if dt.month >= 9 else dt.year - 1


def get_user_verification_status(user: Any, now: Optional[datetime] = None) -> str:
    """Compute verification status for a user.
    
    Returns:
        "unverified": User has not verified an AUA email.
        "alumni": User verified via @alumni.aua.am (permanent graduate status).
        "student": Active AUA student verified for the current academic year.
        "student_expired": Student verified in a past academic year who needs annual re-verification on/after September 1st.
    """
    if not getattr(user, "is_verified", False):
        return "unverified"

    if getattr(user, "is_alumni", False):
        return "alumni"

    email = (getattr(user, "email", "") or "").lower()
    if email.endswith("@alumni.aua.am"):
        return "alumni"

    current_acad_year = get_current_academic_year(now)
    acad_year = getattr(user, "academic_year_verified", None)

    if acad_year is not None:
        return "student" if acad_year >= current_acad_year else "student_expired"

    # Fallback to verified_at or created_at if academic_year_verified is None
    v_date = getattr(user, "verified_at", None) or getattr(user, "created_at", None)
    if v_date:
        v_year = get_current_academic_year(v_date)
        return "student" if v_year >= current_acad_year else "student_expired"

    return "student_expired"


def get_partner_verification_badge(partner: Any, now: Optional[datetime] = None) -> str:
    """Generate the formatted HTML badge for a partner in chat sessions."""
    status = get_user_verification_status(partner, now=now)
    sex_cap = (getattr(partner, "sex", None) or "").capitalize()
    is_gender_v = bool(getattr(partner, "is_gender_verified", False) and sex_cap)

    if status == "alumni":
        gender_str = f" ({sex_cap} ✅)" if is_gender_v else " ✅"
        return f"🎓🏛️ <b>Partner Status:</b> Verified AUA Graduate / Alumni{gender_str}\n\n"

    elif status == "student":
        gender_str = f" ({sex_cap} ✅)" if is_gender_v else " ✅"
        return f"🎓 <b>Partner Status:</b> Verified AUA Student{gender_str}\n\n"

    elif status == "student_expired":
        gender_str = f" ({sex_cap})" if sex_cap else ""
        return f"🎓⚠️ <b>Partner Status:</b> Verified AUA Student (Expired: Re-verification Needed){gender_str}\n\n"

    return ""


def check_global_email_rate_limit() -> Tuple[bool, Optional[str]]:
    """Check if outbound email rate limit across all bot users is within safe limits."""
    now = datetime.now(timezone.utc).timestamp()
    window_seconds = 300.0  # 5 minutes
    cutoff = now - window_seconds

    # Prune timestamps older than 5 minutes
    while _outbound_timestamps and _outbound_timestamps[0] < cutoff:
        _outbound_timestamps.pop(0)

    max_limit = getattr(settings, "VERIFICATION_GLOBAL_RATE_LIMIT", 20)
    if len(_outbound_timestamps) >= max_limit:
        return (
            False,
            "Verification email traffic is temporarily high across campus. "
            "Please wait 3-5 minutes and try again.",
        )
    return True, None


def record_global_email_sent() -> None:
    """Record an outbound email dispatch in the global sliding window."""
    now = datetime.now(timezone.utc).timestamp()
    _outbound_timestamps.append(now)


def reset_global_email_rate_limit() -> None:
    """Reset the global outbound sliding window (useful for unit testing)."""
    _outbound_timestamps.clear()


def generate_otp_code() -> str:
    """Generate a cryptographically secure 6-digit numeric OTP code."""
    return f"{secrets.randbelow(900000) + 100000}"


def _send_smtp_sync(
    host: str,
    port: int,
    user: Optional[str],
    password: Optional[str],
    from_addr: str,
    to_addr: str,
    code: str,
    use_tls: bool = True,
) -> None:
    """Synchronous SMTP email delivery."""
    is_alumni = to_addr.lower().endswith("@alumni.aua.am")
    role_label = "AUA Graduate / Alum" if is_alumni else "AUA Student"
    role_subject = "Alumni" if is_alumni else "Student"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Your Anonymous AUA {role_subject} Verification Code: {code}"
    msg["From"] = from_addr
    msg["To"] = to_addr

    plain_text = (
        f"Hello {role_label},\n\n"
        f"Your verification code for the Anonymous AUA Telegram Bot is: {code}\n\n"
        f"This code will expire in 15 minutes.\n\n"
        f"If you did not request this verification, you can safely ignore this email.\n"
    )

    html_content = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f8fafc; padding: 24px; margin: 0;">
  <div style="max-width: 520px; margin: 0 auto; background: #ffffff; border-radius: 12px; padding: 32px; border: 1px solid #e2e8f0; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.05);">
    <div style="text-align: center; margin-bottom: 24px;">
      <h2 style="color: #1e293b; margin: 0; font-size: 22px;">🎭 Anonymous AUA</h2>
      <p style="color: #64748b; font-size: 14px; margin-top: 4px;">American University of Armenia {role_subject} Verification</p>
    </div>
    <p style="color: #334155; font-size: 15px; line-height: 1.5;">Hello {role_label},</p>
    <p style="color: #334155; font-size: 15px; line-height: 1.5;">You requested {role_subject.lower()} verification for your Anonymous AUA Telegram account. Use the 6-digit code below to confirm your status:</p>
    <div style="background-color: #f1f5f9; border-radius: 8px; padding: 18px; text-align: center; margin: 24px 0; letter-spacing: 6px; font-size: 32px; font-weight: bold; color: #0284c7;">
      {code}
    </div>
    <p style="color: #64748b; font-size: 13px; line-height: 1.5;">This code will expire in <b>15 minutes</b>. If you did not request this verification, you can safely ignore this email.</p>
    <hr style="border: none; border-top: 1px solid #e2e8f0; margin: 24px 0;" />
    <p style="color: #94a3b8; font-size: 12px; text-align: center; margin: 0;">American University of Armenia • Anonymous Student Network</p>
  </div>
</body>
</html>"""

    msg.attach(MIMEText(plain_text, "plain", "utf-8"))
    msg.attach(MIMEText(html_content, "html", "utf-8"))

    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=15) as server:
            if user and password:
                server.login(user, password)
            server.sendmail(from_addr, [to_addr], msg.as_string())
    else:
        with smtplib.SMTP(host, port, timeout=15) as server:
            if use_tls:
                server.starttls()
            if user and password:
                server.login(user, password)
            server.sendmail(from_addr, [to_addr], msg.as_string())


async def send_verification_email(to_email: str, code: str) -> Tuple[bool, Optional[str]]:
    """Send verification OTP code to target AUA email asynchronously.

    Returns:
        (success: bool, info_or_error: Optional[str])
    """
    clean_email = to_email.strip().lower()

    if not settings.SMTP_HOST:
        logger.warning(
            "SMTP_HOST is not configured. (Dev/Simulated mode): Verification code for %s is: %s",
            clean_email,
            code,
        )
        record_global_email_sent()
        return True, "simulated"

    from_addr = settings.SMTP_FROM_EMAIL or f"verification@{settings.SMTP_HOST}"
    try:
        await asyncio.to_thread(
            _send_smtp_sync,
            host=settings.SMTP_HOST,
            port=settings.SMTP_PORT,
            user=settings.SMTP_USER,
            password=settings.SMTP_PASSWORD,
            from_addr=from_addr,
            to_addr=clean_email,
            code=code,
            use_tls=settings.SMTP_USE_TLS,
        )
        record_global_email_sent()
        logger.info("Successfully sent AUA verification OTP email to %s", clean_email)
        return True, None
    except Exception as e:
        logger.error("Failed to send verification email to %s: %s", clean_email, e)
        return False, str(e)
