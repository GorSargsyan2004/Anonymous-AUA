"""Service for AUA student email verification via OTP codes."""

import asyncio
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import logging
import re
import secrets
import smtplib
from typing import Optional, Tuple

from app.config import settings

logger = logging.getLogger(__name__)

# Validates standard email format and ensures domain ends with @edu.aua.am
AUA_EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9_.+-]+@edu\.aua\.am$", re.IGNORECASE)


def is_valid_aua_email(email: Optional[str]) -> bool:
    """Validate whether an email is a syntactically valid AUA student email ending with @edu.aua.am."""
    if not email:
        return False
    clean = email.strip().lower()
    return bool(AUA_EMAIL_REGEX.match(clean))


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
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Your Anonymous AUA Verification Code: {code}"
    msg["From"] = from_addr
    msg["To"] = to_addr

    plain_text = (
        f"Hello AUA Student,\n\n"
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
      <p style="color: #64748b; font-size: 14px; margin-top: 4px;">American University of Armenia Student Verification</p>
    </div>
    <p style="color: #334155; font-size: 15px; line-height: 1.5;">Hello AUA Student,</p>
    <p style="color: #334155; font-size: 15px; line-height: 1.5;">You requested student verification for your Anonymous AUA Telegram account. Use the 6-digit code below to confirm your student status:</p>
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
        logger.info("Successfully sent AUA verification OTP email to %s", clean_email)
        return True, None
    except Exception as e:
        logger.error("Failed to send verification email to %s: %s", clean_email, e)
        return False, str(e)
