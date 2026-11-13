"""Database CRUD operations for AUA Hub companion website and bot integration."""

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import re
import secrets
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import and_, delete, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.crud import (
    YEREVAN_TZ,
    _ensure_utc,
    _safe_mean,
    _safe_median,
    create_or_regenerate_inbox,
    get_admin_stats,
    get_or_create_user,
    get_user_active_inbox,
)
from app.database.models import (
    AcademicFeedback,
    AIUsageLog,
    AnonymousInbox,
    AnonymousPublicPost,
    AnonymousPublicPostReport,
    BotActivitySnapshot,
    Course,
    HaveNeedPost,
    InboxMessage,
    JenzaProfessor,
    JenzaReview,
    SpamChatMessage,
    SpamChatReaction,
    SpamChatRead,
    TelegramGroup,
    User,
    UserNotification,
    WebAnalyticsEvent,
    WebOtpCode,
    WebSyncCode,
    WebUser,
    WebUserAIChatSession,
)
from app.services.email_verification import get_current_academic_year
from app.services.gender_detector import infer_gender_from_email


# ==============================================================================
# Password Hashing Helpers (PBKDF2-HMAC-SHA256)
# ==============================================================================

def hash_password(password: str) -> str:
    """Hash a plaintext password using PBKDF2-HMAC-SHA256 with a random 16-byte salt."""
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 200_000)
    return f"pbkdf2_sha256$200000${salt}${dk.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Verify a plaintext password against a stored PBKDF2-HMAC-SHA256 hash."""
    try:
        parts = stored_hash.split("$")
        if len(parts) != 4 or parts[0] != "pbkdf2_sha256":
            return False
        iterations = int(parts[1])
        salt = parts[2]
        expected_hex = parts[3]
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations)
        return hmac.compare_digest(dk.hex(), expected_hex)
    except Exception:
        return False


# ==============================================================================
# Web Authentication & OTP Registration
# ==============================================================================

async def get_web_user_by_email(session: AsyncSession, email: str) -> Optional[WebUser]:
    """Fetch a WebUser by email (case-insensitive)."""
    clean_email = email.strip().lower()
    stmt = select(WebUser).where(func.lower(WebUser.email) == clean_email)
    res = await session.execute(stmt)
    return res.scalar_one_or_none()


async def get_web_user_by_id(session: AsyncSession, web_user_id: int) -> Optional[WebUser]:
    """Fetch a WebUser by primary key ID."""
    stmt = select(WebUser).where(WebUser.id == web_user_id)
    res = await session.execute(stmt)
    return res.scalar_one_or_none()


async def get_web_user_by_telegram_id(session: AsyncSession, telegram_user_id: int) -> Optional[WebUser]:
    """Fetch a WebUser by linked Telegram user ID."""
    stmt = select(WebUser).where(WebUser.telegram_user_id == telegram_user_id)
    res = await session.execute(stmt)
    return res.scalar_one_or_none()


async def create_web_registration_otp(
    session: AsyncSession,
    name: str,
    email: str,
    password: str,
    ttl_minutes: int = 15,
) -> Tuple[str, WebOtpCode]:
    """Create or replace a pending registration OTP for an AUA email."""
    clean_email = email.strip().lower()
    await session.execute(delete(WebOtpCode).where(func.lower(WebOtpCode.email) == clean_email))

    code = f"{secrets.randbelow(900000) + 100000:06d}"
    pwd_hash = hash_password(password)
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)

    otp_obj = WebOtpCode(
        email=clean_email,
        name=name.strip(),
        password_hash=pwd_hash,
        otp_code=code,
        expires_at=expires_at,
        attempts=0,
    )
    session.add(otp_obj)
    await session.commit()
    await session.refresh(otp_obj)
    return code, otp_obj


async def verify_web_otp_and_register(
    session: AsyncSession,
    email: str,
    otp_code: str,
    bot_username: str = "aua_anonymous_bot",
) -> Tuple[bool, Optional[WebUser], str]:
    """Verify the 6-digit OTP code and create the WebUser account."""
    clean_email = email.strip().lower()
    clean_code = otp_code.strip()

    stmt = (
        select(WebOtpCode)
        .where(func.lower(WebOtpCode.email) == clean_email)
        .order_by(WebOtpCode.created_at.desc())
        .limit(1)
    )
    res = await session.execute(stmt)
    otp_obj = res.scalar_one_or_none()

    if not otp_obj:
        return False, None, "No pending registration found for this email. Please register again."

    now = datetime.now(timezone.utc)
    expires_utc = otp_obj.expires_at if otp_obj.expires_at.tzinfo else otp_obj.expires_at.replace(tzinfo=timezone.utc)
    if now > expires_utc:
        await session.delete(otp_obj)
        await session.commit()
        return False, None, "Verification code has expired. Please request a new code."

    if otp_obj.attempts >= 5:
        await session.delete(otp_obj)
        await session.commit()
        return False, None, "Too many failed attempts. Please register again."

    if not hmac.compare_digest(otp_obj.otp_code, clean_code):
        otp_obj.attempts += 1
        await session.commit()
        remaining = max(0, 5 - otp_obj.attempts)
        return False, None, f"Invalid verification code ({remaining} attempts remaining)."

    # Check if already registered
    existing = await get_web_user_by_email(session, clean_email)
    if existing:
        await session.delete(otp_obj)
        await session.commit()
        return True, existing, "Account already verified."

    # Check if a Telegram bot user already verified with this exact email
    tg_stmt = select(User).where(func.lower(User.email) == clean_email, User.is_verified.is_(True))
    tg_user = (await session.execute(tg_stmt)).scalar_one_or_none()
    linked_tg_id: Optional[int] = None
    auto_inbox_link: Optional[str] = None

    if tg_user:
        linked_tg_id = tg_user.id
        active_inbox = await get_user_active_inbox(session, user_id=tg_user.id)
        if active_inbox:
            auto_inbox_link = f"https://t.me/{bot_username}?start=msg_{active_inbox.token}"

    web_user = WebUser(
        name=otp_obj.name,
        email=clean_email,
        password_hash=otp_obj.password_hash,
        telegram_user_id=linked_tg_id,
        inbox_link=auto_inbox_link,
    )
    session.add(web_user)
    await session.delete(otp_obj)
    await session.commit()
    await session.refresh(web_user)
    return True, web_user, "Registration complete!"


async def authenticate_web_user(
    session: AsyncSession,
    email: str,
    password: str,
) -> Optional[WebUser]:
    """Verify email and password, returning the WebUser if valid."""
    user = await get_web_user_by_email(session, email)
    if not user:
        return None
    if getattr(user, "is_deleted", False):
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


async def update_web_user_profile(
    session: AsyncSession,
    web_user_id: int,
    name: Optional[str] = None,
    bio: Optional[str] = None,
    avatar_url: Optional[str] = None,
    inbox_link: Optional[str] = None,
) -> Optional[WebUser]:
    """Update profile fields for a WebUser."""
    user = await get_web_user_by_id(session, web_user_id)
    if not user or getattr(user, "is_deleted", False):
        return None

    if name is not None and name.strip():
        user.name = name.strip()[:128]
    if bio is not None:
        user.bio = bio.strip()[:1000]
    if avatar_url is not None:
        user.avatar_url = avatar_url.strip()[:512]
    if inbox_link is not None:
        user.inbox_link = inbox_link.strip()[:256]

    user.updated_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(user)
    return user


async def delete_or_anonymize_web_user(
    session: AsyncSession,
    web_user_id: int,
) -> bool:
    """Permanently anonymize/delete a WebUser, their anonymous inbox, and scrub PII."""
    user = await get_web_user_by_id(session, web_user_id)
    if not user or getattr(user, "is_deleted", False):
        return False

    now = datetime.now(timezone.utc)
    original_telegram_id = user.telegram_user_id

    # 1. Scrub User PII & Mark as Deleted
    user.name = "Deleted Account"
    user.email = f"deleted_{user.id}_{int(now.timestamp())}@deleted.local"
    user.password_hash = "DELETED"
    user.bio = ""
    user.avatar_url = "/static/bot-logo.svg"
    user.inbox_link = ""
    user.telegram_user_id = None
    user.is_deleted = True
    user.deleted_at = now
    user.updated_at = now

    # 2. Deactivate & Purge Telegram Anonymous Inbox & Messages (if linked)
    if original_telegram_id:
        await session.execute(
            delete(AnonymousInbox).where(AnonymousInbox.user_id == original_telegram_id)
        )
        await session.execute(
            delete(InboxMessage).where(InboxMessage.owner_id == original_telegram_id)
        )

    # 3. Clean up Web Sync Codes
    await session.execute(
        delete(WebSyncCode).where(WebSyncCode.web_user_id == web_user_id)
    )

    # 4. Deactivate Active Have-Need listings & clear contact_dm
    await session.execute(
        update(HaveNeedPost)
        .where(HaveNeedPost.web_user_id == web_user_id)
        .values(is_active=False, contact_dm="[deleted]", updated_at=now)
    )

    # 5. Clean up User Notifications (where recipient is deleted user)
    await session.execute(
        delete(UserNotification).where(UserNotification.recipient_id == web_user_id)
    )

    # 6. Clean up Chat Reactions & Unread Pointers
    await session.execute(
        delete(SpamChatReaction).where(SpamChatReaction.web_user_id == web_user_id)
    )
    await session.execute(
        delete(SpamChatRead).where(SpamChatRead.web_user_id == web_user_id)
    )

    await session.commit()
    return True


# ==============================================================================
# Bot Synchronization Bridge (/sync)
# ==============================================================================

async def generate_web_sync_code(
    session: AsyncSession,
    web_user_id: int,
    ttl_minutes: int = 15,
) -> str:
    """Generate a unique 6-digit sync code for linking WebUser with Telegram Bot."""
    await session.execute(
        delete(WebSyncCode).where(WebSyncCode.web_user_id == web_user_id, WebSyncCode.is_used.is_(False))
    )
    for _ in range(10):
        code = f"{secrets.randbelow(900000) + 100000:06d}"
        exists = (
            await session.execute(select(WebSyncCode).where(WebSyncCode.code == code))
        ).scalar_one_or_none()
        if not exists:
            break

    sync_obj = WebSyncCode(
        web_user_id=web_user_id,
        code=code,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes),
        is_used=False,
    )
    session.add(sync_obj)
    await session.commit()
    return code


async def consume_sync_code_and_link_bot(
    session: AsyncSession,
    code: str,
    telegram_user_id: int,
    telegram_username: Optional[str] = None,
    bot_username: str = "aua_anonymous_bot",
) -> Tuple[bool, Optional[WebUser], Optional[User], str]:
    """Consume a 6-digit sync code from the bot (/sync), link accounts, and auto-verify the bot user."""
    clean_code = code.strip()
    stmt = select(WebSyncCode).where(WebSyncCode.code == clean_code, WebSyncCode.is_used.is_(False))
    sync_obj = (await session.execute(stmt)).scalar_one_or_none()

    if not sync_obj:
        return False, None, None, "Invalid or already used sync code. Please generate a new code on AUA Hub."

    now = datetime.now(timezone.utc)
    expires_utc = sync_obj.expires_at if sync_obj.expires_at.tzinfo else sync_obj.expires_at.replace(tzinfo=timezone.utc)
    if now > expires_utc:
        await session.delete(sync_obj)
        await session.commit()
        return False, None, None, "This sync code has expired. Please click 'Sync with the Bot' on AUA Hub again."

    web_user = await get_web_user_by_id(session, sync_obj.web_user_id)
    if not web_user:
        return False, None, None, "Web account not found."

    # Ensure Telegram User record exists
    tg_user = await get_or_create_user(session, user_id=telegram_user_id, username=telegram_username)

    # Clear any old web_user link pointing to this telegram_user_id
    await session.execute(
        update(WebUser)
        .where(WebUser.telegram_user_id == telegram_user_id, WebUser.id != web_user.id)
        .values(telegram_user_id=None)
    )

    web_user.telegram_user_id = telegram_user_id

    # Ensure user has an active anonymous inbox in the bot so it's immediately synced
    active_inbox = await get_user_active_inbox(session, user_id=telegram_user_id)
    if not active_inbox:
        active_inbox = await create_or_regenerate_inbox(session, user_id=telegram_user_id)
    web_user.inbox_link = f"https://t.me/{bot_username}?start=msg_{active_inbox.token}"
    web_user.updated_at = now

    # Auto-verify the Telegram user if they were not verified yet!
    if not tg_user.is_verified:
        email_lower = web_user.email.strip().lower()
        # Only set email on tg_user if no other tg_user owns it
        other_owner = (
            await session.execute(
                select(User).where(func.lower(User.email) == email_lower, User.id != telegram_user_id)
            )
        ).scalar_one_or_none()
        if not other_owner:
            tg_user.email = email_lower
            tg_user.is_verified = True
            tg_user.is_alumni = email_lower.endswith("@alumni.aua.am")
            tg_user.verified_at = now
            tg_user.academic_year_verified = get_current_academic_year(now)
            tg_user.verification_code = None
            tg_user.verification_code_expires_at = None

            # Run gender verification from email first name
            _, inferred_gender = infer_gender_from_email(email_lower)
            tg_user.inferred_gender = inferred_gender
            if inferred_gender in ("male", "female"):
                if tg_user.sex and tg_user.sex.lower() == inferred_gender:
                    tg_user.is_gender_verified = True
                    tg_user.gender_mismatch = False
                elif tg_user.sex and tg_user.sex.lower() != inferred_gender:
                    tg_user.is_gender_verified = False
                    tg_user.gender_mismatch = True
                else:
                    # User hasn't set sex in bot yet; pre-set it to inferred gender
                    tg_user.sex = inferred_gender
                    tg_user.is_gender_verified = True
                    tg_user.gender_mismatch = False

    sync_obj.is_used = True
    await session.commit()
    await session.refresh(web_user)
    await session.refresh(tg_user)
    return True, web_user, tg_user, "Synced successfully!"


# ==============================================================================
# Anonymous Public Posts (/post) & Synced Inbox
# ==============================================================================

async def create_anonymous_public_post(
    session: AsyncSession,
    telegram_user_id: int,
    content_text: str,
) -> AnonymousPublicPost:
    """Create a new strictly sequentially indexed anonymous post for the AUA Hub Anonymous -> Posts tab."""
    max_id_res = await session.execute(select(func.max(AnonymousPublicPost.id)))
    max_id = max_id_res.scalar_one_or_none() or 0
    next_id = int(max_id) + 1

    post = AnonymousPublicPost(
        id=next_id,
        telegram_user_id=telegram_user_id,
        content_text=content_text.strip(),
    )
    session.add(post)
    await session.commit()
    try:
        await session.execute(
            text("SELECT setval(pg_get_serial_sequence('anonymous_public_posts', 'id'), :val, true)"),
            {"val": next_id},
        )
        await session.commit()
    except Exception:
        pass
    await session.refresh(post)
    return post


async def get_last_public_post_time(
    session: AsyncSession,
    telegram_user_id: int,
) -> Optional[datetime]:
    """Return the timestamp of the user's most recent /post submission."""
    stmt = (
        select(AnonymousPublicPost.created_at)
        .where(AnonymousPublicPost.telegram_user_id == telegram_user_id)
        .order_by(AnonymousPublicPost.created_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


ANON_POST_REPORT_HIDE_THRESHOLD: int = 3


async def list_anonymous_public_posts(
    session: AsyncSession,
    search_query: str = "",
    page: int = 1,
    per_page: int = 25,
) -> Tuple[List[AnonymousPublicPost], int]:
    """Return paginated visible anonymous public posts ordered newest first, with fast ID or word search."""
    page = max(1, page)
    q = (search_query or "").strip()

    stmt = select(AnonymousPublicPost).where(AnonymousPublicPost.is_hidden == False)
    count_stmt = select(func.count(AnonymousPublicPost.id)).where(AnonymousPublicPost.is_hidden == False)

    if q:
        # Check if query targets an exact post ID (e.g. "#16", "# 16", "16", "post 16")
        num_match = re.search(r"^(?:#|post\s*)?(\d+)$", q, re.IGNORECASE)
        if num_match:
            target_id = int(num_match.group(1))
            cond = or_(
                AnonymousPublicPost.id == target_id,
                AnonymousPublicPost.content_text.ilike(f"%{q}%"),
            )
        else:
            # Word sequence search: all words must be present in content_text
            terms = [term for term in q.split() if term]
            if len(terms) > 1:
                cond = or_(
                    AnonymousPublicPost.content_text.ilike(f"%{q}%"),
                    and_(*[AnonymousPublicPost.content_text.ilike(f"%{term}%") for term in terms]),
                )
            elif terms:
                cond = AnonymousPublicPost.content_text.ilike(f"%{terms[0]}%")
            else:
                cond = None

        if cond is not None:
            stmt = stmt.where(cond)
            count_stmt = count_stmt.where(cond)

    total = (await session.execute(count_stmt)).scalar_one() or 0
    stmt = stmt.order_by(AnonymousPublicPost.id.desc()).offset((page - 1) * per_page).limit(per_page)
    posts = list((await session.execute(stmt)).scalars().all())
    return posts, int(total)


async def get_reported_anon_post_ids(
    session: AsyncSession,
    reporter_key: str,
    post_ids: List[int],
) -> set:
    """Return the set of post IDs from `post_ids` already reported by `reporter_key`."""
    if not reporter_key or not post_ids:
        return set()
    stmt = select(AnonymousPublicPostReport.post_id).where(
        and_(
            AnonymousPublicPostReport.reporter_key == reporter_key,
            AnonymousPublicPostReport.post_id.in_(post_ids),
        )
    )
    rows = (await session.execute(stmt)).scalars().all()
    return set(rows)


async def report_anonymous_public_post(
    session: AsyncSession,
    post_id: int,
    reporter_key: str,
    web_user_id: Optional[int] = None,
    reason: Optional[str] = None,
    hide_threshold: int = ANON_POST_REPORT_HIDE_THRESHOLD,
) -> Dict[str, Any]:
    """Record a unique community report on an anonymous public post (Tier 3 Moderation).

    Automatically hides the post (`is_hidden = True`, `moderation_status = 'flagged_by_reports'`)
    once it reaches `hide_threshold` (default: 3) unique user reports.
    """
    post = (
        await session.execute(select(AnonymousPublicPost).where(AnonymousPublicPost.id == post_id))
    ).scalar_one_or_none()
    if not post:
        return {"ok": False, "error": "Anonymous post not found."}

    # Check if this reporter already reported this post
    existing = (
        await session.execute(
            select(AnonymousPublicPostReport).where(
                and_(
                    AnonymousPublicPostReport.post_id == post_id,
                    AnonymousPublicPostReport.reporter_key == reporter_key,
                )
            )
        )
    ).scalar_one_or_none()

    if existing:
        return {
            "ok": True,
            "already_reported": True,
            "report_count": int(post.report_count or 0),
            "auto_hidden": bool(post.is_hidden),
            "post": post,
        }

    report_row = AnonymousPublicPostReport(
        post_id=post_id,
        web_user_id=web_user_id,
        reporter_key=reporter_key,
        reason=(reason or "").strip()[:250] or None,
    )
    session.add(report_row)
    await session.flush()

    total_reports = (
        await session.execute(
            select(func.count(AnonymousPublicPostReport.id)).where(
                AnonymousPublicPostReport.post_id == post_id
            )
        )
    ).scalar_one() or 0

    post.report_count = int(total_reports)
    newly_hidden = False
    if post.report_count >= hide_threshold and not post.is_hidden:
        post.is_hidden = True
        post.moderation_status = "flagged_by_reports"
        newly_hidden = True

    await session.commit()
    await session.refresh(post)
    return {
        "ok": True,
        "already_reported": False,
        "report_count": int(post.report_count),
        "auto_hidden": bool(post.is_hidden),
        "newly_hidden": newly_hidden,
        "post": post,
    }


async def get_synced_web_inbox(
    session: AsyncSession,
    web_user: WebUser,
    bot_username: str = "aua_anonymous_bot",
    search_query: str = "",
) -> Dict[str, Any]:
    """Retrieve the user's synced personal inbox link and received anonymous messages, optionally filtered."""
    if not web_user.telegram_user_id:
        return {
            "is_synced": False,
            "telegram_user_id": None,
            "inbox_link": web_user.inbox_link,
            "messages": [],
        }

    active_inbox = await get_user_active_inbox(session, user_id=web_user.telegram_user_id)
    inbox_link = (
        f"https://t.me/{bot_username}?start=msg_{active_inbox.token}"
        if active_inbox
        else web_user.inbox_link
    )

    stmt = (
        select(InboxMessage)
        .where(InboxMessage.owner_id == web_user.telegram_user_id)
    )

    q = (search_query or "").strip()
    if q:
        num_match = re.search(r"^(?:#|msg\s*)?(\d+)$", q, re.IGNORECASE)
        if num_match:
            target_id = int(num_match.group(1))
            cond = or_(
                InboxMessage.id == target_id,
                InboxMessage.content_text.ilike(f"%{q}%"),
                InboxMessage.reply_text.ilike(f"%{q}%"),
                InboxMessage.thread_token.ilike(f"%{q}%"),
            )
        else:
            terms = [term for term in q.split() if term]
            if len(terms) > 1:
                cond = or_(
                    InboxMessage.content_text.ilike(f"%{q}%"),
                    InboxMessage.reply_text.ilike(f"%{q}%"),
                    and_(*[
                        or_(
                            InboxMessage.content_text.ilike(f"%{t}%"),
                            InboxMessage.reply_text.ilike(f"%{t}%"),
                        )
                        for t in terms
                    ]),
                )
            elif terms:
                term = terms[0]
                cond = or_(
                    InboxMessage.content_text.ilike(f"%{term}%"),
                    InboxMessage.reply_text.ilike(f"%{term}%"),
                    InboxMessage.thread_token.ilike(f"%{term}%"),
                )
            else:
                cond = None

        if cond is not None:
            stmt = stmt.where(cond)

    stmt = stmt.order_by(InboxMessage.created_at.desc()).limit(100)
    messages = list((await session.execute(stmt)).scalars().all())

    return {
        "is_synced": True,
        "telegram_user_id": web_user.telegram_user_id,
        "inbox_link": inbox_link,
        "messages": [
            {
                "id": m.id,
                "thread_token": m.thread_token,
                "message_type": m.message_type,
                "content_text": m.content_text or f"[{m.message_type.capitalize()} message received in Telegram]",
                "reply_text": m.reply_text,
                "is_replied": m.is_replied,
                "reply_count": m.reply_count,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in messages
        ],
    }


# ==============================================================================
# Courses & Have-Need Exchange
# ==============================================================================

async def get_active_semester_label(session: AsyncSession) -> Optional[str]:
    """Return the active semester label currently stored in the courses table."""
    stmt = select(Course.semester_label).order_by(Course.updated_at.desc(), Course.id.desc()).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none()


async def search_courses(
    session: AsyncSession,
    query: str = "",
    semester_label: Optional[str] = None,
    limit: int = 100,
) -> List[Course]:
    """Search courses by code, title, instructor, or section."""
    if not semester_label:
        semester_label = await get_active_semester_label(session)

    stmt = select(Course)
    if semester_label:
        stmt = stmt.where(Course.semester_label == semester_label)

    if query and query.strip():
        q = f"%{query.strip()}%"
        stmt = stmt.where(
            or_(
                Course.course_code.ilike(q),
                Course.course_title.ilike(q),
                Course.instructor.ilike(q),
                Course.section.ilike(q),
            )
        )

    stmt = stmt.order_by(Course.course_code.asc(), Course.section.asc()).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def create_have_need_post(
    session: AsyncSession,
    web_user_id: int,
    have_course_id: int,
    need_course_id: int,
    contact_dm: str,
    notes: Optional[str] = None,
    notify_email: bool = True,
) -> HaveNeedPost:
    """Create a new Have-Need course section exchange listing."""
    post = HaveNeedPost(
        web_user_id=web_user_id,
        have_course_id=have_course_id,
        need_course_id=need_course_id,
        contact_dm=contact_dm.strip()[:128],
        notes=notes.strip()[:500] if notes else None,
        notify_email=bool(notify_email),
        is_active=True,
    )
    session.add(post)
    await session.commit()
    stmt = (
        select(HaveNeedPost)
        .options(
            selectinload(HaveNeedPost.web_user),
            selectinload(HaveNeedPost.have_course),
            selectinload(HaveNeedPost.need_course),
        )
        .where(HaveNeedPost.id == post.id)
    )
    return (await session.execute(stmt)).scalar_one()


async def list_have_need_posts(
    session: AsyncSession,
    search_query: str = "",
    have_course_id: Optional[int] = None,
    need_course_id: Optional[int] = None,
    only_user_id: Optional[int] = None,
    limit: int = 100,
) -> List[HaveNeedPost]:
    """List active Have-Need posts with eager-loaded course and user details."""
    stmt = (
        select(HaveNeedPost)
        .options(
            selectinload(HaveNeedPost.web_user),
            selectinload(HaveNeedPost.have_course),
            selectinload(HaveNeedPost.need_course),
        )
        .where(HaveNeedPost.is_active.is_(True))
    )
    if only_user_id is not None:
        stmt = stmt.where(HaveNeedPost.web_user_id == only_user_id)
    if have_course_id is not None:
        stmt = stmt.where(HaveNeedPost.have_course_id == have_course_id)
    if need_course_id is not None:
        stmt = stmt.where(HaveNeedPost.need_course_id == need_course_id)

    stmt = stmt.order_by(HaveNeedPost.created_at.desc()).limit(limit)
    posts = list((await session.execute(stmt)).scalars().all())

    if search_query and search_query.strip():
        sq = search_query.strip().lower()
        filtered = []
        for p in posts:
            hc = p.have_course
            nc = p.need_course
            haystack = " ".join(
                filter(
                    None,
                    [
                        hc.course_code if hc else "",
                        hc.course_title if hc else "",
                        hc.section if hc else "",
                        hc.instructor if hc else "",
                        nc.course_code if nc else "",
                        nc.course_title if nc else "",
                        nc.section if nc else "",
                        nc.instructor if nc else "",
                        p.contact_dm,
                        p.notes or "",
                    ],
                )
            ).lower()
            if sq in haystack:
                filtered.append(p)
        return filtered

    return posts


async def update_have_need_post(
    session: AsyncSession,
    post_id: int,
    web_user_id: int,
    have_course_id: Optional[int] = None,
    need_course_id: Optional[int] = None,
    contact_dm: Optional[str] = None,
    notes: Optional[str] = None,
    notify_email: Optional[bool] = None,
) -> Optional[HaveNeedPost]:
    """Update a user's own Have-Need listing."""
    stmt = (
        select(HaveNeedPost)
        .options(
            selectinload(HaveNeedPost.web_user),
            selectinload(HaveNeedPost.have_course),
            selectinload(HaveNeedPost.need_course),
        )
        .where(HaveNeedPost.id == post_id, HaveNeedPost.web_user_id == web_user_id)
    )
    post = (await session.execute(stmt)).scalar_one_or_none()
    if not post:
        return None

    if have_course_id is not None:
        post.have_course_id = have_course_id
    if need_course_id is not None:
        post.need_course_id = need_course_id
    if contact_dm is not None and contact_dm.strip():
        post.contact_dm = contact_dm.strip()[:128]
    if notes is not None:
        post.notes = notes.strip()[:500] if notes.strip() else None
    if notify_email is not None:
        post.notify_email = bool(notify_email)

    post.updated_at = datetime.now(timezone.utc)
    await session.commit()
    # Reload relationships
    stmt_reload = (
        select(HaveNeedPost)
        .options(
            selectinload(HaveNeedPost.web_user),
            selectinload(HaveNeedPost.have_course),
            selectinload(HaveNeedPost.need_course),
        )
        .where(HaveNeedPost.id == post.id)
    )
    return (await session.execute(stmt_reload)).scalar_one()


async def delete_have_need_post(
    session: AsyncSession,
    post_id: int,
    web_user_id: int,
) -> bool:
    """Delete a user's own Have-Need listing."""
    res = await session.execute(
        delete(HaveNeedPost).where(
            HaveNeedPost.id == post_id,
            HaveNeedPost.web_user_id == web_user_id,
        )
    )
    await session.commit()
    return (res.rowcount or 0) > 0


# ==============================================================================
# Telegram Groups Directory
# ==============================================================================

async def create_telegram_group(
    session: AsyncSession,
    web_user_id: int,
    name: str,
    invite_link: str,
    description: Optional[str] = None,
    image_url: Optional[str] = None,
) -> TelegramGroup:
    """Create a new Telegram group listing."""
    group = TelegramGroup(
        web_user_id=web_user_id,
        name=name.strip()[:128],
        invite_link=invite_link.strip()[:256],
        description=description.strip()[:600] if description else None,
        image_url=image_url.strip()[:512] if image_url else None,
    )
    session.add(group)
    await session.commit()
    stmt = (
        select(TelegramGroup)
        .options(selectinload(TelegramGroup.web_user))
        .where(TelegramGroup.id == group.id)
    )
    return (await session.execute(stmt)).scalar_one()


async def list_telegram_groups(
    session: AsyncSession,
    search_query: str = "",
    page: int = 1,
    per_page: int = 12,
) -> Tuple[List[TelegramGroup], int, int]:
    """List Telegram groups with optional name search and pagination."""
    page = max(1, page)
    conditions = []
    if search_query and search_query.strip():
        q = f"%{search_query.strip()}%"
        conditions.append(
            or_(
                TelegramGroup.name.ilike(q),
                TelegramGroup.description.ilike(q),
            )
        )

    count_stmt = select(func.count(TelegramGroup.id))
    if conditions:
        count_stmt = count_stmt.where(*conditions)
    total = int((await session.execute(count_stmt)).scalar_one() or 0)
    total_pages = max(1, (total + per_page - 1) // per_page)

    stmt = select(TelegramGroup).options(selectinload(TelegramGroup.web_user))
    if conditions:
        stmt = stmt.where(*conditions)
    stmt = (
        stmt.order_by(TelegramGroup.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    groups = list((await session.execute(stmt)).scalars().all())
    return groups, total, total_pages


async def update_telegram_group(
    session: AsyncSession,
    group_id: int,
    web_user_id: int,
    name: str,
    invite_link: str,
    description: Optional[str] = None,
    image_url: Optional[str] = None,
) -> Optional[TelegramGroup]:
    """Update an existing Telegram group owned by the specified user."""
    stmt = (
        select(TelegramGroup)
        .options(selectinload(TelegramGroup.web_user))
        .where(
            TelegramGroup.id == group_id,
            TelegramGroup.web_user_id == web_user_id,
        )
    )
    group = (await session.execute(stmt)).scalar_one_or_none()
    if not group:
        return None

    group.name = name.strip()[:128]
    group.invite_link = invite_link.strip()[:256]
    group.description = description.strip()[:600] if (description and description.strip()) else None
    group.image_url = image_url.strip()[:512] if (image_url and image_url.strip()) else None

    await session.commit()
    await session.refresh(group)
    return group


async def delete_telegram_group(
    session: AsyncSession,
    group_id: int,
    web_user_id: int,
) -> bool:
    """Delete a Telegram group created by the specified user."""
    res = await session.execute(
        delete(TelegramGroup).where(
            TelegramGroup.id == group_id,
            TelegramGroup.web_user_id == web_user_id,
        )
    )
    await session.commit()
    return (res.rowcount or 0) > 0


# ==============================================================================
# Spam Chat & Unread Message Counter
# ==============================================================================

async def create_spam_chat_message(
    session: AsyncSession,
    web_user_id: int,
    content_html: str,
    image_url: Optional[str] = None,
    media_type: Optional[str] = "image",
    reply_to_id: Optional[int] = None,
) -> SpamChatMessage:
    """Create a new message in Campus Chat and mark the sender as read up to this message."""
    valid_reply_to_id = None
    if reply_to_id and int(reply_to_id) > 0:
        check = await session.execute(
            select(SpamChatMessage.id).where(SpamChatMessage.id == int(reply_to_id))
        )
        if check.scalar_one_or_none():
            valid_reply_to_id = int(reply_to_id)

    msg = SpamChatMessage(
        web_user_id=web_user_id,
        content_html=content_html,
        image_url=image_url,
        media_type=media_type or "image",
        reply_to_id=valid_reply_to_id,
    )
    session.add(msg)
    await session.commit()

    # Update sender's read pointer
    await mark_spam_chat_read(session, web_user_id=web_user_id, latest_message_id=msg.id)

    stmt = (
        select(SpamChatMessage)
        .options(
            selectinload(SpamChatMessage.web_user),
            selectinload(SpamChatMessage.reply_to).selectinload(SpamChatMessage.web_user),
            selectinload(SpamChatMessage.reactions).selectinload(SpamChatReaction.web_user),
        )
        .where(SpamChatMessage.id == msg.id)
    )
    return (await session.execute(stmt)).scalar_one()


async def list_spam_chat_messages(
    session: AsyncSession,
    limit: int = 80,
    after_id: Optional[int] = None,
) -> List[SpamChatMessage]:
    """Retrieve recent Spam Chat messages ordered chronologically (oldest to newest)."""
    options = [
        selectinload(SpamChatMessage.web_user),
        selectinload(SpamChatMessage.reply_to).selectinload(SpamChatMessage.web_user),
        selectinload(SpamChatMessage.reactions).selectinload(SpamChatReaction.web_user),
    ]
    if after_id is not None and after_id > 0:
        stmt = (
            select(SpamChatMessage)
            .options(*options)
            .where(SpamChatMessage.id > after_id)
            .order_by(SpamChatMessage.id.asc())
            .limit(limit)
        )
        return list((await session.execute(stmt)).scalars().all())

    stmt = (
        select(SpamChatMessage)
        .options(*options)
        .order_by(SpamChatMessage.id.desc())
        .limit(limit)
    )
    recent_desc = list((await session.execute(stmt)).scalars().all())
    recent_desc.reverse()
    return recent_desc


async def edit_spam_chat_message(
    session: AsyncSession,
    message_id: int,
    web_user_id: int,
    new_content_html: str,
) -> Optional[SpamChatMessage]:
    """Edit an existing Campus Chat message if caller is the author."""
    stmt = (
        select(SpamChatMessage)
        .options(
            selectinload(SpamChatMessage.web_user),
            selectinload(SpamChatMessage.reply_to).selectinload(SpamChatMessage.web_user),
            selectinload(SpamChatMessage.reactions).selectinload(SpamChatReaction.web_user),
        )
        .where(SpamChatMessage.id == message_id)
    )
    msg = (await session.execute(stmt)).scalar_one_or_none()
    if not msg:
        return None
    if msg.web_user_id != web_user_id:
        return None

    msg.content_html = new_content_html
    msg.is_edited = True
    msg.edited_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(msg)
    return msg


async def delete_spam_chat_message(
    session: AsyncSession,
    message_id: int,
    web_user_id: int,
    is_admin: bool = False,
) -> bool:
    """Delete an existing Campus Chat message if caller is author or admin."""
    stmt = select(SpamChatMessage).where(SpamChatMessage.id == message_id)
    msg = (await session.execute(stmt)).scalar_one_or_none()
    if not msg:
        return False
    if not is_admin and msg.web_user_id != web_user_id:
        return False

    await session.delete(msg)
    await session.commit()
    return True


async def toggle_spam_chat_reaction(
    session: AsyncSession,
    message_id: int,
    web_user_id: int,
    emoji: str,
) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """Toggle or update an emoji reaction on a Campus Chat message.

    - If user already reacted with this exact emoji, remove it (toggle off).
    - If user had a different reaction, update it to the new emoji.
    - If user had no reaction, insert it.
    Returns (active_emoji_or_none, aggregated_reactions_list).
    """
    stmt = select(SpamChatReaction).where(
        SpamChatReaction.message_id == message_id,
        SpamChatReaction.web_user_id == web_user_id,
    )
    existing = (await session.execute(stmt)).scalar_one_or_none()

    active_emoji: Optional[str] = None
    if existing:
        if existing.emoji == emoji:
            await session.delete(existing)
            active_emoji = None
        else:
            existing.emoji = emoji
            active_emoji = emoji
    else:
        new_r = SpamChatReaction(
            message_id=message_id,
            web_user_id=web_user_id,
            emoji=emoji,
        )
        session.add(new_r)
        active_emoji = emoji

    await session.commit()
    reactions = await get_message_reactions(session, message_id)
    return active_emoji, reactions


async def get_message_reactions(
    session: AsyncSession,
    message_id: int,
) -> List[Dict[str, Any]]:
    """Retrieve aggregated reactions for a message with user info."""
    stmt = (
        select(SpamChatReaction)
        .options(selectinload(SpamChatReaction.web_user))
        .where(SpamChatReaction.message_id == message_id)
        .order_by(SpamChatReaction.id.asc())
    )
    records = list((await session.execute(stmt)).scalars().all())

    grouped: Dict[str, Dict[str, Any]] = {}
    for r in records:
        if r.emoji not in grouped:
            grouped[r.emoji] = {
                "emoji": r.emoji,
                "count": 0,
                "user_ids": [],
                "users": [],
            }
        grouped[r.emoji]["count"] += 1
        grouped[r.emoji]["user_ids"].append(r.web_user_id)
        user_name = r.web_user.name if r.web_user else "AUA Student"
        user_avatar = (r.web_user.avatar_url or "") if r.web_user else ""
        grouped[r.emoji]["users"].append({
            "id": r.web_user_id,
            "name": user_name,
            "avatar": user_avatar,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        })

    return list(grouped.values())


async def get_spam_chat_unread_count(
    session: AsyncSession,
    web_user_id: int,
) -> int:
    """Count how many Spam Chat messages have been posted since the user last viewed Spam Chat."""
    read_stmt = select(SpamChatRead.last_read_message_id).where(SpamChatRead.web_user_id == web_user_id)
    last_read_id = (await session.execute(read_stmt)).scalar_one_or_none() or 0

    count_stmt = select(func.count(SpamChatMessage.id)).where(
        SpamChatMessage.id > last_read_id,
        SpamChatMessage.web_user_id != web_user_id,
    )
    unread = (await session.execute(count_stmt)).scalar_one() or 0
    return int(unread)


async def mark_spam_chat_read(
    session: AsyncSession,
    web_user_id: int,
    latest_message_id: Optional[int] = None,
) -> int:
    """Mark Spam Chat messages as read up to the latest message ID."""
    if latest_message_id is None:
        max_stmt = select(func.max(SpamChatMessage.id))
        latest_message_id = int((await session.execute(max_stmt)).scalar_one_or_none() or 0)

    existing = (
        await session.execute(select(SpamChatRead).where(SpamChatRead.web_user_id == web_user_id))
    ).scalar_one_or_none()

    if existing:
        if latest_message_id > existing.last_read_message_id:
            existing.last_read_message_id = latest_message_id
            existing.updated_at = datetime.now(timezone.utc)
    else:
        read_obj = SpamChatRead(
            web_user_id=web_user_id,
            last_read_message_id=latest_message_id,
        )
        session.add(read_obj)

    await session.commit()
    return latest_message_id


# ==============================================================================
# Academic Feedbacks (Instructors & Courses)
# ==============================================================================

async def get_feedback_catalog(session: AsyncSession) -> Dict[str, Any]:
    """Build deduplicated Instructor and Course catalogs from the courses table with aggregated ratings."""
    sem_label = await get_active_semester_label(session)
    stmt = select(Course)
    if sem_label:
        stmt = stmt.where(Course.semester_label == sem_label)
    all_courses = list((await session.execute(stmt)).scalars().all())

    # Aggregate ratings per (target_type, target_key)
    stats_stmt = (
        select(
            AcademicFeedback.target_type,
            AcademicFeedback.target_key,
            func.count(AcademicFeedback.id),
            func.avg(AcademicFeedback.rating),
        )
        .group_by(AcademicFeedback.target_type, AcademicFeedback.target_key)
    )
    stats_rows = (await session.execute(stats_stmt)).all()
    stats_map: Dict[Tuple[str, str], Tuple[int, float]] = {}
    for t_type, t_key, cnt, avg_val in stats_rows:
        stats_map[(t_type, (t_key or "").upper())] = (int(cnt or 0), round(float(avg_val or 0.0), 2))

    instructors_map: Dict[str, Dict[str, Any]] = {}
    courses_map: Dict[str, Dict[str, Any]] = {}

    for c in all_courses:
        code = (c.course_code or "").strip()
        title = (c.course_title or "").strip()
        inst = (c.instructor or "").strip()

        if code:
            c_key_upper = code.upper()
            if c_key_upper not in courses_map:
                courses_map[c_key_upper] = {
                    "key": code,
                    "title": f"{code} — {title}" if title else code,
                    "course_code": code,
                    "course_title": title,
                    "instructors": set(),
                }
            if inst and inst.upper() not in ("TBA", "STAFF"):
                courses_map[c_key_upper]["instructors"].add(inst)

        if inst and inst.upper() not in ("TBA", "STAFF"):
            i_key_upper = inst.upper()
            if i_key_upper not in instructors_map:
                instructors_map[i_key_upper] = {
                    "key": inst,
                    "title": inst,
                    "department": None,
                    "courses": set(),
                }
            if code:
                instructors_map[i_key_upper]["courses"].add(code)

    # Merge professors mirrored from remote jenzareviews database
    jenza_profs = list((await session.execute(select(JenzaProfessor))).scalars().all())
    for jp in jenza_profs:
        p_name = (jp.name or "").strip()
        if not p_name or p_name.upper() in ("TBA", "STAFF"):
            continue
        i_key_upper = p_name.upper()
        if i_key_upper not in instructors_map:
            instructors_map[i_key_upper] = {
                "key": p_name,
                "title": p_name,
                "department": (jp.department or "").strip() or None,
                "courses": set(),
            }
        elif jp.department and not instructors_map[i_key_upper].get("department"):
            instructors_map[i_key_upper]["department"] = jp.department.strip()

    instructors_list: List[Dict[str, Any]] = []
    for i_key_upper, info in sorted(instructors_map.items(), key=lambda x: x[1]["title"].lower()):
        cnt, avg_r = stats_map.get(("instructor", i_key_upper), (0, 0.0))
        sorted_codes = sorted(info["courses"])
        if sorted_codes:
            sub = ", ".join(sorted_codes[:8]) + ("..." if len(sorted_codes) > 8 else "")
        else:
            sub = info.get("department") or "AUA Faculty"
        instructors_list.append(
            {
                "key": info["key"],
                "title": info["title"],
                "subtitle": sub,
                "reviews_count": cnt,
                "avg_rating": avg_r,
            }
        )

    courses_list: List[Dict[str, Any]] = []
    for c_key_upper, info in sorted(courses_map.items(), key=lambda x: x[1]["key"].lower()):
        cnt, avg_r = stats_map.get(("course", c_key_upper), (0, 0.0))
        sorted_insts = sorted(info["instructors"])
        courses_list.append(
            {
                "key": info["key"],
                "title": info["title"],
                "course_code": info["course_code"],
                "course_title": info["course_title"],
                "subtitle": ", ".join(sorted_insts[:4]) + ("..." if len(sorted_insts) > 4 else ""),
                "reviews_count": cnt,
                "avg_rating": avg_r,
            }
        )

    return {
        "semester": sem_label or "Current Semester",
        "instructors": instructors_list,
        "courses": courses_list,
    }


async def create_academic_feedback(
    session: AsyncSession,
    web_user_id: int,
    target_type: str,
    target_key: str,
    target_title: str,
    comment: str,
    rating: int = 5,
    target_subtitle: Optional[str] = None,
    is_anonymous: bool = True,
    course_name: Optional[str] = None,
    would_take_again: Optional[bool] = None,
    push_to_remote: bool = True,
) -> AcademicFeedback:
    """Create a new feedback entry locally and push an anonymized copy to remote `jenzareviews`."""
    clamped_rating = max(1, min(5, int(rating)))
    clean_course = course_name.strip()[:256] if course_name and course_name.strip() else None
    fb = AcademicFeedback(
        web_user_id=web_user_id,
        target_type=target_type.strip().lower(),
        target_key=target_key.strip()[:160],
        target_title=target_title.strip()[:256],
        target_subtitle=target_subtitle.strip()[:256] if target_subtitle else None,
        rating=clamped_rating,
        comment=comment.strip()[:2000],
        is_anonymous=bool(is_anonymous),
        course_name=clean_course,
        would_take_again=would_take_again,
    )
    session.add(fb)
    await session.commit()

    if push_to_remote:
        try:
            from app.services.jenzareviews_sync import push_anonymized_review_to_remote
            await push_anonymized_review_to_remote(session, fb)
        except Exception:
            # Best-effort immediate write-through; 5-minute sync worker will retry if remote was unreachable
            pass

    stmt = (
        select(AcademicFeedback)
        .options(selectinload(AcademicFeedback.web_user))
        .where(AcademicFeedback.id == fb.id)
    )
    return (await session.execute(stmt)).scalar_one()


async def list_academic_feedbacks(
    session: AsyncSession,
    target_type: str = "instructor",
    search_query: str = "",
    target_key: Optional[str] = None,
    only_user_id: Optional[int] = None,
    limit: int = 100,
) -> Tuple[List[AcademicFeedback], Dict[str, Any]]:
    """List feedbacks for instructors or courses with optional target_key / search filter and summary stats."""
    t_type = target_type.strip().lower()
    conditions = [AcademicFeedback.target_type == t_type]

    if target_key and target_key.strip():
        conditions.append(func.upper(AcademicFeedback.target_key) == target_key.strip().upper())

    if only_user_id is not None:
        conditions.append(AcademicFeedback.web_user_id == only_user_id)

    if search_query and search_query.strip():
        q = f"%{search_query.strip()}%"
        conditions.append(
            or_(
                AcademicFeedback.target_key.ilike(q),
                AcademicFeedback.target_title.ilike(q),
                AcademicFeedback.target_subtitle.ilike(q),
                AcademicFeedback.comment.ilike(q),
            )
        )

    stats_stmt = select(
        func.count(AcademicFeedback.id),
        func.avg(AcademicFeedback.rating),
    ).where(*conditions)
    cnt, avg_val = (await session.execute(stats_stmt)).one()

    stmt = (
        select(AcademicFeedback)
        .options(selectinload(AcademicFeedback.web_user))
        .where(*conditions)
        .order_by(AcademicFeedback.created_at.desc())
        .limit(limit)
    )
    items = list((await session.execute(stmt)).scalars().all())
    summary = {
        "total_reviews": int(cnt or 0),
        "avg_rating": round(float(avg_val or 0.0), 2) if cnt else 0.0,
    }
    return items, summary


async def delete_academic_feedback(
    session: AsyncSession,
    feedback_id: int,
    web_user_id: int,
) -> bool:
    """Delete a user's own academic feedback entry (and its remote anonymized mirror if linked)."""
    existing_stmt = select(AcademicFeedback).where(
        AcademicFeedback.id == feedback_id,
        AcademicFeedback.web_user_id == web_user_id,
    )
    existing_fb = (await session.execute(existing_stmt)).scalar_one_or_none()
    if not existing_fb:
        return False

    remote_rev_id = existing_fb.remote_review_id
    res = await session.execute(
        delete(AcademicFeedback).where(
            AcademicFeedback.id == feedback_id,
            AcademicFeedback.web_user_id == web_user_id,
        )
    )
    if remote_rev_id is not None:
        await session.execute(delete(JenzaReview).where(JenzaReview.id == int(remote_rev_id)))
    await session.commit()

    if remote_rev_id is not None:
        try:
            from app.services.jenzareviews_sync import delete_review_on_remote
            await delete_review_on_remote(int(remote_rev_id))
        except Exception:
            pass

    return (res.rowcount or 0) > 0


# ==============================================================================
# Web Analytics & Admin Reporting
# ==============================================================================

async def record_web_analytics_event(
    session: AsyncSession,
    event_type: str,
    web_user_id: Optional[int] = None,
    meta_info: Optional[str] = None,
) -> WebAnalyticsEvent:
    """Record an interaction or navigation event on AUA Hub for analytics."""
    event = WebAnalyticsEvent(
        event_type=event_type[:64],
        web_user_id=web_user_id,
        meta_info=meta_info[:256] if meta_info else None,
    )
    session.add(event)
    await session.commit()
    return event


async def get_web_admin_stats(session: AsyncSession) -> Dict[str, Any]:
    """Compile comprehensive web platform statistics and user engagement metrics for admin reporting."""
    now = datetime.now(timezone.utc)
    day_ago = now - timedelta(days=1)
    week_ago = now - timedelta(days=7)

    # 1. Web Users breakdown
    total_users = (await session.execute(select(func.count(WebUser.id)))).scalar_one() or 0
    users_24h = (
        await session.execute(select(func.count(WebUser.id)).where(WebUser.created_at >= day_ago))
    ).scalar_one() or 0
    users_7d = (
        await session.execute(select(func.count(WebUser.id)).where(WebUser.created_at >= week_ago))
    ).scalar_one() or 0
    synced_users = (
        await session.execute(
            select(func.count(WebUser.id)).where(WebUser.telegram_user_id.isnot(None))
        )
    ).scalar_one() or 0
    students_count = (
        await session.execute(
            select(func.count(WebUser.id)).where(WebUser.email.ilike("%@edu.aua.am"))
        )
    ).scalar_one() or 0
    alumni_count = (
        await session.execute(
            select(func.count(WebUser.id)).where(WebUser.email.ilike("%@alumni.aua.am"))
        )
    ).scalar_one() or 0
    other_users_count = max(0, total_users - (students_count + alumni_count))

    # 2. Module Activity
    total_anon_posts = (
        await session.execute(select(func.count(AnonymousPublicPost.id)))
    ).scalar_one() or 0
    total_hn_posts = (
        await session.execute(select(func.count(HaveNeedPost.id)))
    ).scalar_one() or 0
    active_hn_posts = (
        await session.execute(
            select(func.count(HaveNeedPost.id)).where(HaveNeedPost.is_active.is_(True))
        )
    ).scalar_one() or 0
    total_feedbacks = (
        await session.execute(select(func.count(AcademicFeedback.id)))
    ).scalar_one() or 0
    total_groups = (
        await session.execute(select(func.count(TelegramGroup.id)))
    ).scalar_one() or 0
    total_chat_messages = (
        await session.execute(select(func.count(SpamChatMessage.id)))
    ).scalar_one() or 0
    total_inbox_messages = (
        await session.execute(select(func.count(InboxMessage.id)))
    ).scalar_one() or 0

    # 3. Analytics events (button clicks, searches, lookups)
    total_events = (
        await session.execute(select(func.count(WebAnalyticsEvent.id)))
    ).scalar_one() or 0
    events_24h = (
        await session.execute(
            select(func.count(WebAnalyticsEvent.id)).where(WebAnalyticsEvent.created_at >= day_ago)
        )
    ).scalar_one() or 0

    # Top event types
    events_stmt = (
        select(WebAnalyticsEvent.event_type, func.count(WebAnalyticsEvent.id))
        .group_by(WebAnalyticsEvent.event_type)
        .order_by(func.count(WebAnalyticsEvent.id).desc())
        .limit(15)
    )
    event_counts = dict((await session.execute(events_stmt)).all())
    ai_stats = await get_ai_assistant_stats(session)

    return {
        "users": {
            "total": int(total_users),
            "new_24h": int(users_24h),
            "new_7d": int(users_7d),
            "synced": int(synced_users),
            "students": int(students_count),
            "alumni": int(alumni_count),
            "other": int(other_users_count),
        },
        "content": {
            "anon_posts": int(total_anon_posts),
            "have_need_total": int(total_hn_posts),
            "have_need_active": int(active_hn_posts),
            "academic_feedbacks": int(total_feedbacks),
            "telegram_groups": int(total_groups),
            "chat_messages": int(total_chat_messages),
            "inbox_messages": int(total_inbox_messages),
        },
        "analytics": {
            "total_clicks": int(total_events),
            "clicks_24h": int(events_24h),
            "event_breakdown": event_counts,
        },
        "ai_assistant": ai_stats,
    }


async def backfill_ai_usage_logs(session: AsyncSession) -> int:
    """One-time backfill of AI usage telemetry from existing WebUserAIChatSession histories if log table is empty."""
    existing_cnt = (await session.execute(select(func.count(AIUsageLog.id)))).scalar_one() or 0
    if existing_cnt > 0:
        return 0

    rows = (await session.execute(select(WebUserAIChatSession))).scalars().all()
    inserted = 0
    for row in rows:
        try:
            exchanges = json.loads(row.history_json or "[]")
        except Exception:
            exchanges = []
        if not isinstance(exchanges, list):
            continue
        for idx, ex in enumerate(exchanges, start=1):
            if not isinstance(ex, dict):
                continue
            steps = ex.get("steps") or []
            tool_ids = [s.get("id") for s in steps if isinstance(s, dict) and s.get("id") and s.get("id") != "synthesize"]
            sources = ex.get("sources") or []
            ts_dt = row.updated_at or datetime.now(timezone.utc)
            if ex.get("timestamp"):
                try:
                    ts_dt = datetime.fromisoformat(str(ex["timestamp"]))
                    if ts_dt.tzinfo is None:
                        ts_dt = ts_dt.replace(tzinfo=timezone.utc)
                except Exception:
                    pass
            log = AIUsageLog(
                web_user_id=row.web_user_id,
                session_id=row.session_id,
                prompt_index=idx,
                tools_used_json=json.dumps(tool_ids),
                sources_count=len(sources),
                latency_ms=1450,
                used_gemini=True,
                prompt_length=len(str(ex.get("prompt") or "")),
                answer_length=len(str(ex.get("answer") or "")),
                created_at=ts_dt,
            )
            session.add(log)
            inserted += 1
    if inserted > 0:
        await session.commit()
    return inserted


async def get_ai_assistant_stats(session: AsyncSession) -> Dict[str, Any]:
    """Compute comprehensive AI Assistant usage, tool selection, latency, and session depth metrics."""
    try:
        await backfill_ai_usage_logs(session)
    except Exception:
        pass

    now = datetime.now(timezone.utc)
    now_amt = now.astimezone(YEREVAN_TZ)
    day_ago = now - timedelta(hours=24)
    month_ago = now - timedelta(days=30)

    logs = list(
        (await session.execute(select(AIUsageLog).order_by(AIUsageLog.created_at.asc()))).scalars().all()
    )
    active_web_sessions_cnt = (
        await session.execute(select(func.count(WebUserAIChatSession.id)))
    ).scalar_one() or 0

    total_prompts = len(logs)
    prompts_24h = 0
    prompts_30d = 0
    sessions_all: Dict[str, int] = defaultdict(int)
    sessions_24h_set = set()
    sessions_30d_set = set()
    signed_users_set = set()
    signed_in_prompts = 0
    guest_prompts = 0
    gemini_success_count = 0
    latencies: List[float] = []
    sources_counts: List[float] = []
    prompt_lengths: List[float] = []
    answer_lengths: List[float] = []
    tool_counts: Counter = Counter()
    depth_counts: Counter = Counter()
    hourly_prompts_30d = [0] * 24
    hourly_prompts_24h = [0] * 24

    day_keys = []
    day_labels = {}
    for offset in range(29, -1, -1):
        d = (now_amt - timedelta(days=offset)).date()
        dk = d.isoformat()
        day_keys.append(dk)
        day_labels[dk] = d.strftime("%b %d")
    daily_prompts: Dict[str, int] = defaultdict(int)
    daily_sessions: Dict[str, set] = defaultdict(set)

    for lg in logs:
        ts = _ensure_utc(lg.created_at, now)
        ts_amt = ts.astimezone(YEREVAN_TZ)
        sid = lg.session_id
        sessions_all[sid] += 1

        if lg.web_user_id is not None:
            signed_in_prompts += 1
            signed_users_set.add(lg.web_user_id)
        else:
            guest_prompts += 1

        if lg.used_gemini:
            gemini_success_count += 1

        if lg.latency_ms and lg.latency_ms > 0:
            latencies.append(float(lg.latency_ms))
        sources_counts.append(float(lg.sources_count or 0))
        prompt_lengths.append(float(lg.prompt_length or 0))
        answer_lengths.append(float(lg.answer_length or 0))
        depth_counts[min(5, max(1, int(lg.prompt_index or 1)))] += 1

        try:
            tools_list = json.loads(lg.tools_used_json or "[]")
            if isinstance(tools_list, list):
                for t_id in tools_list:
                    if t_id:
                        tool_counts[str(t_id)] += 1
        except Exception:
            pass

        if ts >= month_ago:
            prompts_30d += 1
            sessions_30d_set.add(sid)
            hourly_prompts_30d[ts_amt.hour] += 1
            dk = ts_amt.date().isoformat()
            if dk in day_labels:
                daily_prompts[dk] += 1
                daily_sessions[dk].add(sid)

        if ts >= day_ago:
            prompts_24h += 1
            sessions_24h_set.add(sid)
            hourly_prompts_24h[ts_amt.hour] += 1

    prompts_per_session_vals = [float(c) for c in sessions_all.values()]
    limit_reached_sessions = sum(1 for c in sessions_all.values() if c >= 5)

    tool_labels_map = {
        "feedbacks": "Course & Prof Feedbacks",
        "directory": "AUA Directory & People",
        "newsroom": "AUA Newsroom Events",
        "highlander": "The Highlander Magazine",
        "chat": "Campus Global Chat",
        "anonymous": "Anonymous Posts & Groups",
    }
    tool_breakdown = [
        {
            "id": tid,
            "label": tool_labels_map.get(tid, tid.title()),
            "count": int(tool_counts.get(tid, 0)),
        }
        for tid in ("feedbacks", "directory", "newsroom", "highlander", "chat", "anonymous")
    ]
    tool_breakdown.sort(key=lambda x: x["count"], reverse=True)

    daily_series_30d = [
        {
            "date": dk,
            "label": day_labels[dk],
            "prompts": daily_prompts.get(dk, 0),
            "sessions": len(daily_sessions.get(dk, set())),
        }
        for dk in day_keys
    ]

    return {
        "total_prompts": total_prompts,
        "prompts_24h": prompts_24h,
        "prompts_30d": prompts_30d,
        "unique_sessions_total": len(sessions_all),
        "unique_sessions_24h": len(sessions_24h_set),
        "unique_sessions_30d": len(sessions_30d_set),
        "synced_web_session_records": int(active_web_sessions_cnt),
        "unique_signed_in_users": len(signed_users_set),
        "signed_in_prompts": signed_in_prompts,
        "guest_prompts": guest_prompts,
        "mean_prompts_per_session": _safe_mean(prompts_per_session_vals),
        "median_prompts_per_session": _safe_median(prompts_per_session_vals),
        "limit_reached_sessions": limit_reached_sessions,
        "mean_latency_ms": _safe_mean(latencies),
        "median_latency_ms": _safe_median(latencies),
        "mean_sources_per_prompt": _safe_mean(sources_counts),
        "mean_prompt_chars": _safe_mean(prompt_lengths),
        "mean_answer_chars": _safe_mean(answer_lengths),
        "gemini_success_count": gemini_success_count,
        "fallback_count": max(0, total_prompts - gemini_success_count),
        "gemini_success_rate": round((gemini_success_count / total_prompts) * 100, 1) if total_prompts > 0 else 0.0,
        "tool_breakdown": tool_breakdown,
        "prompt_depth_distribution": [
            {"prompt_num": i, "label": f"Prompt #{i}", "count": int(depth_counts.get(i, 0))}
            for i in range(1, 6)
        ],
        "hourly_30d": [
            {"hour": h, "label": f"{h:02d}:00", "prompts_30d": hourly_prompts_30d[h], "prompts_24h": hourly_prompts_24h[h]}
            for h in range(24)
        ],
        "daily_series_30d": daily_series_30d,
    }


async def get_full_admin_stata_payload(
    session: AsyncSession,
    live_ws_count: int = 0,
    jenzareviews_sync_status: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Assemble the complete unified analytics payload for auahub.am/stata."""
    now = datetime.now(timezone.utc)
    now_amt = now.astimezone(YEREVAN_TZ)
    day_ago = now - timedelta(hours=24)
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)

    # 1. Telegram Bot + Advanced Anonymous Chat telemetry
    bot_stats = await get_admin_stats(session)
    adv_chat = bot_stats.pop("advanced_chat", {})

    # 2. AI Assistant telemetry
    ai_stats = await get_ai_assistant_stats(session)

    # 3. Website Users + 30d Daily Signup Series
    web_users = list(
        (await session.execute(select(WebUser).where(WebUser.is_deleted.is_(False)))).scalars().all()
    )
    wu_total = len(web_users)
    wu_24h = sum(1 for u in web_users if _ensure_utc(u.created_at, now) >= day_ago)
    wu_7d = sum(1 for u in web_users if _ensure_utc(u.created_at, now) >= week_ago)
    wu_30d = sum(1 for u in web_users if _ensure_utc(u.created_at, now) >= month_ago)
    wu_synced = sum(1 for u in web_users if u.telegram_user_id is not None)
    wu_students = sum(1 for u in web_users if "@edu.aua.am" in (u.email or "").lower())
    wu_alumni = sum(1 for u in web_users if "@alumni.aua.am" in (u.email or "").lower())
    wu_other = max(0, wu_total - wu_students - wu_alumni)

    # 4. Web Modules Detailed Breakdown
    # Feedbacks
    all_feedbacks = list(
        (
            await session.execute(
                select(
                    AcademicFeedback.target_type,
                    AcademicFeedback.target_title,
                    AcademicFeedback.rating,
                    AcademicFeedback.remote_review_id,
                    AcademicFeedback.is_anonymous,
                    AcademicFeedback.created_at,
                )
            )
        ).all()
    )
    fb_total = len(all_feedbacks)
    fb_local = sum(1 for r in all_feedbacks if r.remote_review_id is None)
    fb_synced = fb_total - fb_local
    fb_instructor = sum(1 for r in all_feedbacks if r.target_type == "instructor")
    fb_course = fb_total - fb_instructor
    fb_24h = sum(1 for r in all_feedbacks if _ensure_utc(r.created_at, now) >= day_ago)
    fb_30d = sum(1 for r in all_feedbacks if _ensure_utc(r.created_at, now) >= month_ago)
    fb_ratings = [float(r.rating or 5) for r in all_feedbacks]
    fb_dist = Counter(int(r.rating or 5) for r in all_feedbacks)
    fb_target_counts = Counter(r.target_title for r in all_feedbacks if r.target_title)
    fb_top_targets = [{"name": name, "count": cnt} for name, cnt in fb_target_counts.most_common(8)]
    jenza_prof_count = (await session.execute(select(func.count(JenzaProfessor.id)))).scalar_one() or 0

    # Have-Need
    hn_posts = list((await session.execute(select(HaveNeedPost))).scalars().all())
    hn_total = len(hn_posts)
    hn_active = sum(1 for h in hn_posts if h.is_active)
    hn_email_enabled = sum(1 for h in hn_posts if h.notify_email)
    hn_24h = sum(1 for h in hn_posts if _ensure_utc(h.created_at, now) >= day_ago)
    # Count active reciprocal swap pairs
    active_hn = [h for h in hn_posts if h.is_active]
    pair_map: Dict[Tuple[int, int], int] = Counter((h.have_course_id, h.need_course_id) for h in active_hn)
    reciprocal_matches = 0
    for h in active_hn:
        if pair_map.get((h.need_course_id, h.have_course_id), 0) > 0:
            reciprocal_matches += 1
    reciprocal_swap_pairs = reciprocal_matches // 2

    # Anonymous Public Posts & Tier 3 Moderation
    anon_posts = list((await session.execute(select(AnonymousPublicPost))).scalars().all())
    anon_total = len(anon_posts)
    anon_hidden = sum(1 for p in anon_posts if p.is_hidden)
    anon_visible = anon_total - anon_hidden
    anon_24h = sum(1 for p in anon_posts if _ensure_utc(p.created_at, now) >= day_ago)
    anon_reports_total = (await session.execute(select(func.count(AnonymousPublicPostReport.id)))).scalar_one() or 0

    # Telegram Groups
    tg_groups_cnt = (await session.execute(select(func.count(TelegramGroup.id)))).scalar_one() or 0
    tg_groups_30d = (
        await session.execute(select(func.count(TelegramGroup.id)).where(TelegramGroup.created_at >= month_ago))
    ).scalar_one() or 0

    # Campus Global Chat & Reactions
    chat_msgs = list(
        (await session.execute(select(SpamChatMessage.id, SpamChatMessage.image_url, SpamChatMessage.reply_to_id, SpamChatMessage.created_at))).all()
    )
    chat_total = len(chat_msgs)
    chat_24h = sum(1 for m in chat_msgs if _ensure_utc(m.created_at, now) >= day_ago)
    chat_30d = sum(1 for m in chat_msgs if _ensure_utc(m.created_at, now) >= month_ago)
    chat_with_media = sum(1 for m in chat_msgs if m.image_url)
    chat_replies = sum(1 for m in chat_msgs if m.reply_to_id is not None)
    reactions_total = (await session.execute(select(func.count(SpamChatReaction.id)))).scalar_one() or 0

    # Notifications & Courses
    total_notifications = (await session.execute(select(func.count(UserNotification.id)))).scalar_one() or 0
    courses_cnt = (await session.execute(select(func.count(Course.id)))).scalar_one() or 0

    # 5. WebAnalyticsEvent Deep Breakdown + Daily Activity Trend (30d) + Top Search Queries
    all_events = list(
        (
            await session.execute(
                select(WebAnalyticsEvent.event_type, WebAnalyticsEvent.meta_info, WebAnalyticsEvent.created_at)
            )
        ).all()
    )
    ev_total = len(all_events)
    ev_24h = 0
    ev_30d = 0
    ev_breakdown: Counter = Counter()
    ev_breakdown_24h: Counter = Counter()
    search_queries: Counter = Counter()

    day_keys = []
    day_labels = {}
    for offset in range(29, -1, -1):
        d = (now_amt - timedelta(days=offset)).date()
        dk = d.isoformat()
        day_keys.append(dk)
        day_labels[dk] = d.strftime("%b %d")

    daily_web_events: Dict[str, int] = defaultdict(int)
    daily_web_signups: Dict[str, int] = defaultdict(int)
    daily_chat_msgs: Dict[str, int] = defaultdict(int)

    for u in web_users:
        dk = _ensure_utc(u.created_at, now).astimezone(YEREVAN_TZ).date().isoformat()
        if dk in day_labels:
            daily_web_signups[dk] += 1

    for m in chat_msgs:
        dk = _ensure_utc(m.created_at, now).astimezone(YEREVAN_TZ).date().isoformat()
        if dk in day_labels:
            daily_chat_msgs[dk] += 1

    for ev_type, meta, created_at in all_events:
        ts = _ensure_utc(created_at, now)
        ev_breakdown[ev_type] += 1
        if ts >= day_ago:
            ev_24h += 1
            ev_breakdown_24h[ev_type] += 1
        if ts >= month_ago:
            ev_30d += 1
            dk = ts.astimezone(YEREVAN_TZ).date().isoformat()
            if dk in day_labels:
                daily_web_events[dk] += 1
        if ev_type and "search" in ev_type and meta:
            q_clean = str(meta).strip().lower()
            if q_clean:
                search_queries[(ev_type, q_clean)] += 1

    top_events = [
        {"event": k, "count": int(v), "count_24h": int(ev_breakdown_24h.get(k, 0))}
        for k, v in ev_breakdown.most_common(16)
    ]
    top_searches = [
        {"category": cat, "query": q, "count": int(cnt)}
        for (cat, q), cnt in search_queries.most_common(12)
    ]

    web_daily_series_30d = [
        {
            "date": dk,
            "label": day_labels[dk],
            "web_events": daily_web_events.get(dk, 0),
            "new_web_users": daily_web_signups.get(dk, 0),
            "campus_chat_messages": daily_chat_msgs.get(dk, 0),
        }
        for dk in day_keys
    ]

    # Live snapshot series (last 24h)
    snap_rows = list(
        (
            await session.execute(
                select(BotActivitySnapshot)
                .where(BotActivitySnapshot.created_at >= day_ago)
                .order_by(BotActivitySnapshot.created_at.asc())
            )
        ).scalars().all()
    )
    snapshots_24h = [
        {
            "time": _ensure_utc(sn.created_at, now).astimezone(YEREVAN_TZ).strftime("%H:%M"),
            "active_chats": sn.active_chats,
            "non_paused_chats": sn.non_paused_chats,
            "searching_users": sn.searching_users,
            "web_ws_clients": sn.web_ws_clients,
        }
        for sn in snap_rows[-96:]
    ]

    return {
        "generated_at": now.isoformat(),
        "generated_at_amt": now_amt.strftime("%Y-%m-%d %H:%M:%S AMT"),
        "timezone": "Asia/Yerevan (UTC+4)",
        "live": {
            "web_ws_clients": int(live_ws_count),
            "snapshots_24h": snapshots_24h,
            **adv_chat.get("live", {}),
        },
        "anonymous_chat_advanced": adv_chat,
        "ai_assistant": ai_stats,
        "bot_overview": bot_stats,
        "website": {
            "users": {
                "total": wu_total,
                "new_24h": wu_24h,
                "new_7d": wu_7d,
                "new_30d": wu_30d,
                "synced_with_bot": wu_synced,
                "students": wu_students,
                "alumni": wu_alumni,
                "other": wu_other,
            },
            "modules": {
                "courses_catalog": int(courses_cnt),
                "have_need": {
                    "total": hn_total,
                    "active": hn_active,
                    "email_alerts_enabled": hn_email_enabled,
                    "created_24h": hn_24h,
                    "reciprocal_swap_pairs": reciprocal_swap_pairs,
                },
                "feedbacks": {
                    "total": fb_total,
                    "local_submitted": fb_local,
                    "jenzareviews_synced": fb_synced,
                    "professors_indexed": int(jenza_prof_count),
                    "instructor_reviews": fb_instructor,
                    "course_reviews": fb_course,
                    "new_24h": fb_24h,
                    "new_30d": fb_30d,
                    "avg_rating": round(sum(fb_ratings) / len(fb_ratings), 2) if fb_ratings else 0.0,
                    "rating_distribution": [
                        {"stars": s, "count": int(fb_dist.get(s, 0))} for s in range(1, 6)
                    ],
                    "top_targets": fb_top_targets,
                    "sync_status": jenzareviews_sync_status or {},
                },
                "anonymous_posts": {
                    "total": anon_total,
                    "visible": anon_visible,
                    "hidden": anon_hidden,
                    "new_24h": anon_24h,
                    "community_reports": int(anon_reports_total),
                },
                "telegram_groups": {
                    "total": int(tg_groups_cnt),
                    "new_30d": int(tg_groups_30d),
                },
                "campus_chat": {
                    "total_messages": chat_total,
                    "messages_24h": chat_24h,
                    "messages_30d": chat_30d,
                    "media_messages": chat_with_media,
                    "reply_messages": chat_replies,
                    "total_reactions": int(reactions_total),
                },
                "notifications_sent": int(total_notifications),
            },
            "analytics": {
                "total_events": ev_total,
                "events_24h": ev_24h,
                "events_30d": ev_30d,
                "top_events": top_events,
                "top_searches": top_searches,
                "daily_series_30d": web_daily_series_30d,
            },
        },
    }


# ==============================================================================
# 11. User Notifications CRUD
# ==============================================================================

def serialize_user_notification(n: UserNotification) -> Dict[str, Any]:
    """Serialize a UserNotification instance to dictionary format."""
    extra = {}
    if n.extra_data:
        try:
            extra = json.loads(n.extra_data)
        except Exception:
            extra = {}
    return {
        "id": n.id,
        "recipient_id": n.recipient_id,
        "sender_id": n.sender_id,
        "sender_name": n.sender.name if n.sender else extra.get("sender_name", ""),
        "sender_avatar": n.sender.avatar_url if n.sender else extra.get("sender_avatar", ""),
        "category": n.category,
        "title": n.title,
        "content": n.content,
        "target_type": n.target_type,
        "target_id": n.target_id,
        "extra_data": extra,
        "is_read": n.is_read,
        "created_at": n.created_at.isoformat() if n.created_at else None,
    }


async def create_user_notification(
    session: AsyncSession,
    recipient_id: int,
    category: str,
    title: str,
    content: str,
    target_type: str,
    target_id: Optional[str] = None,
    sender_id: Optional[int] = None,
    extra_data: Optional[Dict[str, Any]] = None,
) -> UserNotification:
    """Create and persist a new in-app notification for a web user."""
    notif = UserNotification(
        recipient_id=recipient_id,
        sender_id=sender_id,
        category=category,
        title=title,
        content=content,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        extra_data=json.dumps(extra_data) if extra_data else None,
        is_read=False,
    )
    session.add(notif)
    await session.commit()
    await session.refresh(notif)
    return notif


async def list_user_notifications(
    session: AsyncSession,
    recipient_id: int,
    limit: int = 40,
) -> List[UserNotification]:
    """Retrieve notifications for a user, newest first, eager-loading sender info."""
    stmt = (
        select(UserNotification)
        .options(selectinload(UserNotification.sender))
        .where(UserNotification.recipient_id == recipient_id)
        .order_by(UserNotification.created_at.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_unread_notification_count(
    session: AsyncSession,
    recipient_id: int,
) -> int:
    """Return count of unread notifications for a given user."""
    stmt = (
        select(func.count(UserNotification.id))
        .where(
            and_(
                UserNotification.recipient_id == recipient_id,
                UserNotification.is_read.is_(False),
            )
        )
    )
    count = (await session.execute(stmt)).scalar_one_or_none()
    return int(count or 0)


async def mark_notification_read(
    session: AsyncSession,
    notification_id: int,
    recipient_id: int,
) -> bool:
    """Mark a single notification as read if it belongs to the user."""
    stmt = (
        update(UserNotification)
        .where(
            and_(
                UserNotification.id == notification_id,
                UserNotification.recipient_id == recipient_id,
            )
        )
        .values(is_read=True)
    )
    result = await session.execute(stmt)
    await session.commit()
    return bool(result.rowcount and result.rowcount > 0)


async def mark_all_notifications_read(
    session: AsyncSession,
    recipient_id: int,
) -> int:
    """Mark all unread notifications as read for a given user."""
    stmt = (
        update(UserNotification)
        .where(
            and_(
                UserNotification.recipient_id == recipient_id,
                UserNotification.is_read.is_(False),
            )
        )
        .values(is_read=True)
    )
    result = await session.execute(stmt)
    await session.commit()
    return int(result.rowcount or 0)


async def find_reciprocal_have_need_matches(
    session: AsyncSession,
    have_course_id: int,
    need_course_id: int,
    exclude_user_id: int,
) -> List[HaveNeedPost]:
    """Find active posts where another user has the course we need, and needs the course we have."""
    stmt = (
        select(HaveNeedPost)
        .options(
            selectinload(HaveNeedPost.web_user),
            selectinload(HaveNeedPost.have_course),
            selectinload(HaveNeedPost.need_course),
        )
        .where(
            and_(
                HaveNeedPost.is_active.is_(True),
                HaveNeedPost.have_course_id == need_course_id,
                HaveNeedPost.need_course_id == have_course_id,
                HaveNeedPost.web_user_id != exclude_user_id,
            )
        )
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())

