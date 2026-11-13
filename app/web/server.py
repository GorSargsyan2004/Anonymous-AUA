"""AUA Hub Web Application Server (aiohttp.web).

Provides REST APIs, WebSocket real-time Spam Chat & unread badge counter,
file uploads, and static SPA hosting for AUA Hub.
"""

import asyncio
import base64
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import hashlib
import hmac
import html
import json
import logging
import os
from pathlib import Path
import re
import secrets
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from html.parser import HTMLParser
import urllib.parse
from urllib.parse import urlparse

from aiohttp import WSMsgType, web
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud import mark_inbox_message_replied, record_bot_activity_snapshot
from app.database.models import (
    AcademicFeedback,
    AnonymousInbox,
    Course,
    HaveNeedPost,
    InboxMessage,
    SpamChatMessage,
    TelegramGroup,
    User,
    UserNotification,
    WebUser,
)
from app.database.session import get_db_session, init_db
from app.database.web_crud import (
    authenticate_web_user,
    create_academic_feedback,
    create_have_need_post,
    create_spam_chat_message,
    create_telegram_group,
    create_user_notification,
    create_web_registration_otp,
    delete_academic_feedback,
    delete_have_need_post,
    delete_or_anonymize_web_user,
    delete_spam_chat_message,
    delete_telegram_group,
    edit_spam_chat_message,
    find_reciprocal_have_need_matches,
    generate_web_sync_code,
    get_active_semester_label,
    get_feedback_catalog,
    get_message_reactions,
    get_reported_anon_post_ids,
    get_spam_chat_unread_count,
    get_synced_web_inbox,
    get_unread_notification_count,
    get_web_user_by_email,
    get_web_user_by_id,
    list_academic_feedbacks,
    list_anonymous_public_posts,
    list_have_need_posts,
    list_spam_chat_messages,
    list_telegram_groups,
    list_user_notifications,
    mark_all_notifications_read,
    mark_notification_read,
    mark_spam_chat_read,
    record_web_analytics_event,
    report_anonymous_public_post,
    search_courses,
    serialize_user_notification,
    toggle_spam_chat_reaction,
    update_have_need_post,
    update_telegram_group,
    update_web_user_profile,
    verify_web_otp_and_register,
    ANON_POST_REPORT_HIDE_THRESHOLD,
    get_full_admin_stata_payload,
)
from app.services.ai_agent import (
    MAX_PROMPTS_PER_SESSION,
    ai_orchestrator,
    ai_session_store,
    load_or_sync_ai_session,
    reset_ai_session_persistent,
)
from app.services.booking_scraper import (
    free_rooms_prewarm_worker,
    get_free_rooms_schedule,
)
from app.services.email_verification import (
    BLOCKED_LOCAL_TERMS,
    is_valid_aua_email,
    send_have_need_match_email,
    send_verification_email,
)
from app.services.jenzareviews_sync import (
    get_jenzareviews_sync_status,
    jenzareviews_sync_worker,
)
from app.services.link_shield import URL_REGEX, LinkShield
from app.services.sonis_scraper import sync_courses_from_sonis

logger = logging.getLogger("aua_hub_web")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"
PROJECT_ROOT = BASE_DIR.parent.parent
UPLOADS_DIR = PROJECT_ROOT / "uploads"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
ADMIN_STATA_EMAIL = "gor_sargsyan@edu.aua.am"

ALLOWED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov"}
ALLOWED_MEDIA_EXTENSIONS = ALLOWED_IMAGE_EXTENSIONS | ALLOWED_VIDEO_EXTENSIONS

MAX_IMAGE_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB limit for images and GIFs
MAX_VIDEO_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB limit for videos
MAX_UPLOAD_BYTES = MAX_VIDEO_UPLOAD_BYTES

SAFE_UPLOAD_URL_RE = re.compile(r"^/uploads/[0-9]+_[0-9a-f]{16}\.(png|jpg|jpeg|webp|gif|mp4|webm|mov)$")

TELEGRAM_REACTION_EMOJIS = [
    "👍", "👎", "❤", "🔥", "🥰", "👏", "😁", "🤔", "🤯", "😱", "🤬", "😢",
    "🎉", "🤩", "🤮", "💩", "🙏", "👌", "🕊", "🤡", "🥱", "🥴", "😍", "🐳",
    "❤️🔥", "🌚", "🌭", "💯", "🤣", "⚡", "🍌", "🏆", "💔", "🤨", "😐", "🍓",
    "🍾", "💋", "🖕", "😈", "😴", "😭", "🤓", "👻", "👨💻", "👀", "🎃", "🙈",
    "😇", "😨", "🤝", "✍", "🤗", "🫡", "🎅", "🎄", "☃", "💅", "🤪", "🗿",
    "🆒", "💘", "🙉", "🦄", "😘", "💊", "🙊", "😎", "👾", "🤷♂️", "🤷", "🤷♀️", "😡",
]
ALLOWED_REACTIONS = set(TELEGRAM_REACTION_EMOJIS) | {
    "❤️", "❤️‍🔥", "👨‍💻", "🤷‍♂️", "🤷‍♀️", "✍️", "☃️", "💻", "🤦", "🙋"
}

_ACTIVE_WS_CLIENTS: Set[web.WebSocketResponse] = set()
_ACTIVE_WS_USERS: Dict[web.WebSocketResponse, Optional[int]] = {}


async def broadcast_ws_event(event_dict: Dict[str, Any]) -> None:
    """Broadcast an event JSON to connected WebSocket clients (scoping private notifications & AI sessions to recipient only)."""
    target_recipient_id = (
        int(event_dict["recipient_id"])
        if event_dict.get("type") in ("new_notification", "ai_session_updated")
        and event_dict.get("recipient_id") is not None
        else None
    )
    payload_str = json.dumps(event_dict)
    dead = []
    for ws in list(_ACTIVE_WS_CLIENTS):
        if target_recipient_id is not None and _ACTIVE_WS_USERS.get(ws) != target_recipient_id:
            continue
        try:
            await ws.send_str(payload_str)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _ACTIVE_WS_CLIENTS.discard(ws)
        _ACTIVE_WS_USERS.pop(ws, None)


# ==============================================================================
# Security Utilities: Rate Limiting, Magic Bytes, Upload URLs, & Link Shield
# ==============================================================================

_RATE_BUCKETS: Dict[str, List[float]] = {}
_LAST_USER_MESSAGES: Dict[int, Tuple[str, float]] = {}


def reset_rate_limits() -> None:
    """Clear all in-memory rate limit buckets (used in unit tests)."""
    _RATE_BUCKETS.clear()
    _LAST_USER_MESSAGES.clear()


def check_rate_limit(bucket: str, key: str, max_calls: int, window_sec: float) -> Tuple[bool, int]:
    """Sliding-window rate limiter returning (allowed, retry_after_seconds)."""
    now = time.monotonic()
    composite_key = f"{bucket}:{key}"
    timestamps = _RATE_BUCKETS.get(composite_key, [])
    cutoff = now - window_sec
    timestamps = [ts for ts in timestamps if ts > cutoff]

    if len(timestamps) >= max_calls:
        _RATE_BUCKETS[composite_key] = timestamps
        retry_after = max(1, int(window_sec - (now - timestamps[0])) + 1)
        return False, retry_after

    timestamps.append(now)
    # Periodic cleanup if map grows large
    if len(_RATE_BUCKETS) > 5000:
        expired_keys = [k for k, v in _RATE_BUCKETS.items() if not v or v[-1] <= cutoff]
        for k in expired_keys:
            _RATE_BUCKETS.pop(k, None)
    _RATE_BUCKETS[composite_key] = timestamps
    return True, 0


def get_client_ip(request: web.Request) -> str:
    """Extract client IP address safely."""
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()[:64]
    return (request.remote or "unknown")[:64]


def _is_https_request(request: web.Request) -> bool:
    """Return True if the request arrived over HTTPS directly or via reverse proxy."""
    if request.secure:
        return True
    proto = request.headers.get("X-Forwarded-Proto", "")
    return proto.split(",")[0].strip().lower() == "https"


def validate_media_magic_bytes(data: bytes, ext: str) -> Tuple[bool, str]:
    """Verify binary file header magic bytes match the claimed extension.
    Returns (is_valid, media_type) where media_type is 'image', 'gif', or 'video'.
    """
    if not data or len(data) < 4:
        return False, ""
    ext_lower = ext.lower()

    if ext_lower == ".png":
        if len(data) >= 8 and data.startswith(b"\x89PNG\r\n\x1a\n"):
            return True, "image"
        return False, ""

    if ext_lower in (".jpg", ".jpeg"):
        if data.startswith(b"\xff\xd8\xff"):
            return True, "image"
        return False, ""

    if ext_lower == ".gif":
        if data.startswith((b"GIF87a", b"GIF89a")):
            return True, "gif"
        return False, ""

    if ext_lower == ".webp":
        if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return True, "image"
        return False, ""

    if ext_lower == ".mp4":
        if len(data) >= 8 and data[4:8] == b"ftyp":
            return True, "video"
        return False, ""

    if ext_lower == ".webm":
        if data.startswith(b"\x1a\x45\xdf\xa3"):
            return True, "video"
        return False, ""

    if ext_lower == ".mov":
        if len(data) >= 8 and data[4:8] in (b"ftyp", b"moov", b"mdat", b"wide", b"free", b"skip", b"pnot"):
            return True, "video"
        return False, ""

    return False, ""


def validate_image_magic_bytes(data: bytes, ext: str) -> bool:
    """Verify binary file header magic bytes match the claimed image extension."""
    valid, media_t = validate_media_magic_bytes(data, ext)
    return valid and media_t in ("image", "gif")


def is_safe_upload_url(url: Optional[str]) -> bool:
    """Ensure image/avatar URLs only reference locally uploaded sanitized files."""
    if not url:
        return True
    return bool(SAFE_UPLOAD_URL_RE.match(url.strip()))


def scan_web_text_for_malicious_links(text: Optional[str]) -> Optional[str]:
    """Scan user-submitted text for known IP-logger domains and dangerous executables (.apk, .exe, etc.).

    Returns an error reason string if blocked, or None if safe.
    """
    if not text:
        return None
    for match in URL_REGEX.finditer(text):
        raw_url = match.group(0)
        domain, path = LinkShield.parse_domain(raw_url)
        if domain and LinkShield.is_ip_logger(domain):
            return f"Security Alert: Links to IP-tracking/logger domain '{domain}' are strictly prohibited."
        if path and LinkShield.has_dangerous_file(path):
            return "Security Alert: Links to executable files (.apk, .exe, .bat, .scr, etc.) are blocked."
    return None


@web.middleware
async def security_middleware(request: web.Request, handler: Callable) -> web.StreamResponse:
    """Global security middleware enforcing dotfile/traversal blocking, CSRF Origin checks, and strict HTTP security headers."""
    raw_path = request.raw_path or request.path or ""
    if ".." in raw_path or "/." in raw_path or "%2e" in raw_path.lower():
        return web.json_response({"error": "Forbidden path."}, status=403)

    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        origin = request.headers.get("Origin")
        if origin:
            try:
                parsed_origin = urlparse(origin)
                if parsed_origin.netloc and request.host and parsed_origin.netloc.lower() != request.host.lower():
                    return web.json_response(
                        {"error": "Cross-origin state-changing request blocked by CSRF policy."},
                        status=403,
                    )
            except Exception:
                return web.json_response({"error": "Invalid Origin header."}, status=403)

    response = await handler(request)

    response.headers["Server"] = "AUA-Hub"
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com data:; "
            "img-src 'self' data: blob:; "
            "media-src 'self' blob:; "
            "connect-src 'self' ws: wss:; "
            "manifest-src 'self'; "
            "worker-src 'self'; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "frame-ancestors 'none'; "
            "form-action 'self'"
        ),
    )
    if request.path.startswith("/uploads/"):
        response.headers.setdefault("Content-Disposition", "inline")
    if request.path in (
        "/",
        "/index.html",
        "/sw.js",
        "/stata",
        "/stata/",
        "/stata/assets/stata.css",
        "/stata/assets/stata.js",
        "/api/admin/stata",
    ):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"

    return response


# ==============================================================================
# JWT Token Signing & Verification (HMAC-SHA256)
# ==============================================================================

_PLACEHOLDER_JWT_SECRETS = {
    "",
    "aua_hub_default_jwt_secret_change_me",
    "change_this_to_a_secure_random_jwt_secret",
}


def _get_jwt_secret() -> bytes:
    secret = (getattr(settings, "WEB_JWT_SECRET", "") or "").strip()
    if secret in _PLACEHOLDER_JWT_SECRETS:
        return hashlib.sha256(f"aua-hub-jwt-{settings.BOT_TOKEN}".encode("utf-8")).digest()
    return secret.encode("utf-8")


def create_jwt_token(web_user_id: int, expires_days: int = 30) -> str:
    """Create a signed JWT token for a WebUser."""
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": int(web_user_id),
        "iat": int(time.time()),
        "exp": int(time.time()) + expires_days * 86400,
    }
    h_b64 = base64.urlsafe_b64encode(json.dumps(header).encode()).rstrip(b"=").decode()
    p_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    signing_input = f"{h_b64}.{p_b64}".encode()
    sig = hmac.new(_get_jwt_secret(), signing_input, hashlib.sha256).digest()
    s_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
    return f"{h_b64}.{p_b64}.{s_b64}"


def decode_jwt_token(token: Optional[str]) -> Optional[int]:
    """Verify a JWT token and return web_user_id if valid."""
    if not token:
        return None
    try:
        parts = token.strip().split(".")
        if len(parts) != 3:
            return None
        h_b64, p_b64, s_b64 = parts
        signing_input = f"{h_b64}.{p_b64}".encode()
        expected_sig = hmac.new(_get_jwt_secret(), signing_input, hashlib.sha256).digest()
        expected_b64 = base64.urlsafe_b64encode(expected_sig).rstrip(b"=").decode()
        if not hmac.compare_digest(s_b64, expected_b64):
            return None
        pad = "=" * (-len(p_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(p_b64 + pad).decode("utf-8"))
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        return int(payload["sub"])
    except Exception:
        return None


async def get_current_web_user(request: web.Request) -> Optional[WebUser]:
    """Extract authenticated WebUser from Authorization Bearer header or cookie."""
    token: Optional[str] = None
    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
    if not token:
        token = request.cookies.get("aua_hub_token")

    user_id = decode_jwt_token(token)
    if not user_id:
        return None

    async with get_db_session() as session:
        user = await get_web_user_by_id(session, user_id)
        if not user or getattr(user, "is_deleted", False):
            return None
        return user


# ==============================================================================
# Safe Markdown & HTML Formatter for Spam Chat
# ==============================================================================

_SAFE_TAG_MAP = {
    "<b>": "\x00TAG_B_OPEN\x00",
    "</b>": "\x00TAG_B_CLOSE\x00",
    "<strong>": "\x00TAG_B_OPEN\x00",
    "</strong>": "\x00TAG_B_CLOSE\x00",
    "<i>": "\x00TAG_I_OPEN\x00",
    "</i>": "\x00TAG_I_CLOSE\x00",
    "<em>": "\x00TAG_I_OPEN\x00",
    "</em>": "\x00TAG_I_CLOSE\x00",
    "<u>": "\x00TAG_U_OPEN\x00",
    "</u>": "\x00TAG_U_CLOSE\x00",
    "<s>": "\x00TAG_S_OPEN\x00",
    "</s>": "\x00TAG_S_CLOSE\x00",
    "<strike>": "\x00TAG_S_OPEN\x00",
    "</strike>": "\x00TAG_S_CLOSE\x00",
    "<del>": "\x00TAG_S_OPEN\x00",
    "</del>": "\x00TAG_S_CLOSE\x00",
    "<code>": "\x00TAG_CODE_OPEN\x00",
    "</code>": "\x00TAG_CODE_CLOSE\x00",
    "<pre>": "\x00TAG_PRE_OPEN\x00",
    "</pre>": "\x00TAG_PRE_CLOSE\x00",
    "<br>": "\x00TAG_BR\x00",
    "<br/>": "\x00TAG_BR\x00",
    "<br />": "\x00TAG_BR\x00",
}

_RESTORE_TAG_MAP = {
    "\x00TAG_B_OPEN\x00": "<b>",
    "\x00TAG_B_CLOSE\x00": "</b>",
    "\x00TAG_I_OPEN\x00": "<i>",
    "\x00TAG_I_CLOSE\x00": "</i>",
    "\x00TAG_U_OPEN\x00": "<u>",
    "\x00TAG_U_CLOSE\x00": "</u>",
    "\x00TAG_S_OPEN\x00": "<s>",
    "\x00TAG_S_CLOSE\x00": "</s>",
    "\x00TAG_CODE_OPEN\x00": "<code>",
    "\x00TAG_CODE_CLOSE\x00": "</code>",
    "\x00TAG_PRE_OPEN\x00": "<pre>",
    "\x00TAG_PRE_CLOSE\x00": "</pre>",
    "\x00TAG_BR\x00": "<br>",
}


def sanitize_and_render_chat_html(raw_input: str) -> Tuple[str, List[str]]:
    """Convert user chat text/HTML supporting safe tags (<b>, <i>, <s>, <u>, <code>) and Markdown.
    Discards dangerous tags/scripts by escaping them, auto-balances tags, and extracts URLs for LinkShield.
    """
    if not raw_input:
        return "", []
    text = raw_input.replace("\x00", "").strip()[:4000]

    # Convert div/p block containers into clean linebreaks
    text = re.sub(r"</div>\s*<div>", "<br>", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>\s*<p>", "<br><br>", text, flags=re.IGNORECASE)
    text = re.sub(r"<(div|p)[^>]*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"</(div|p)>", "<br>", text, flags=re.IGNORECASE)

    # 1. Preserve explicit safe HTML tags (case-insensitive)
    for tag_lower, placeholder in _SAFE_TAG_MAP.items():
        text = re.sub(re.escape(tag_lower), placeholder, text, flags=re.IGNORECASE)

    # 2. Escape all other HTML characters to prevent XSS (<script>, <img onerror=...>, etc.)
    text = html.escape(text)

    # 3. Apply Markdown conversions
    # Code blocks ```...```
    text = re.sub(r"```([\s\S]+?)```", r"<pre><code>\1</code></pre>", text)
    # Inline code `...`
    text = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", text)
    # Bold **...**
    text = re.sub(r"\*\*([^\*\n]+)\*\*", r"<b>\1</b>", text)
    # Strikethrough ~~...~~
    text = re.sub(r"~~([^~\n]+)~~", r"<s>\1</s>", text)
    # Italic *...* or _..._
    text = re.sub(r"(?<!\*)\*([^\*\n]+)\*(?!\*)", r"<i>\1</i>", text)
    # Markdown links [label](https://...)
    text = re.sub(
        r"\[([^\]]+)\]\((https?://(?:[a-zA-Z0-9._~:/?#\[\]@!$()*+,=%-]|&amp;)+)\)",
        r'<a href="\2" target="_blank" rel="noopener noreferrer">\1</a>',
        text,
    )
    # Bare URLs not already inside href="..."
    text = re.sub(
        r'(?<!href=")(https?://(?:[a-zA-Z0-9._~:/?#\[\]@!$()*+,=%-]|&amp;)+)',
        r'<a href="\1" target="_blank" rel="noopener noreferrer">\1</a>',
        text,
    )
    # Convert @telegram_handles to clickable t.me links (excluding email addresses)
    text = re.sub(
        r"(?<![a-zA-Z0-9._%+-])@([a-zA-Z0-9_]{4,32})",
        r'<a href="https://t.me/\1" target="_blank" rel="noopener noreferrer">@\1</a>',
        text,
    )

    # 4. Restore safe HTML placeholders
    for placeholder, safe_tag in _RESTORE_TAG_MAP.items():
        text = text.replace(placeholder, safe_tag)

    # 5. Balance any unclosed formatting tags
    for tag_name in ("b", "i", "u", "s", "code", "pre"):
        opens = len(re.findall(f"<{tag_name}>", text))
        closes = len(re.findall(f"</{tag_name}>", text))
        if opens > closes:
            text += f"</{tag_name}>" * (opens - closes)

    text = text.replace("\n", "<br>")
    text = re.sub(r"(<br\s*/?>\s*){3,}", "<br><br>", text)
    text = re.sub(r"^(<br\s*/?>|\s)+|(<br\s*/?>|\s)+$", "", text)

    extracted_urls = [html.unescape(u) for u in re.findall(r'href="([^"]+)"', text)]
    return text, extracted_urls


def render_safe_chat_html(raw_text: str) -> str:
    """Convenience wrapper returning cleaned HTML string."""
    clean_html, _ = sanitize_and_render_chat_html(raw_text)
    return clean_html


# ==============================================================================
# Serializers
# ==============================================================================

def is_stata_admin(user: Optional[WebUser]) -> bool:
    """Restrict /stata analytics exclusively to gor_sargsyan@edu.aua.am."""
    if not user or getattr(user, "is_deleted", False):
        return False
    return (user.email or "").strip().lower() == ADMIN_STATA_EMAIL


def serialize_web_user(u: WebUser) -> Dict[str, Any]:
    return {
        "id": u.id,
        "name": u.name,
        "email": u.email,
        "bio": u.bio or "",
        "avatar_url": u.avatar_url or "",
        "inbox_link": u.inbox_link or "",
        "telegram_user_id": u.telegram_user_id,
        "is_bot_synced": u.telegram_user_id is not None,
        "is_admin": bool(u.telegram_user_id and u.telegram_user_id in settings.admin_ids),
        "is_stata_admin": is_stata_admin(u),
        "is_alumni": u.email.lower().endswith("@alumni.aua.am"),
        "is_deleted": getattr(u, "is_deleted", False),
        "created_at": u.created_at.isoformat() if u.created_at else None,
    }


def serialize_public_web_user(u: WebUser) -> Dict[str, Any]:
    if getattr(u, "is_deleted", False) or u.name == "Deleted Account":
        return {
            "id": u.id,
            "name": "Deleted Account",
            "bio": "",
            "avatar_url": "/static/bot-logo.svg",
            "inbox_link": "",
            "is_bot_synced": False,
            "is_alumni": False,
            "is_deleted": True,
            "role_badge": "Deleted Account",
            "created_at": None,
        }
    is_alumni = u.email.lower().endswith("@alumni.aua.am")
    return {
        "id": u.id,
        "name": u.name,
        "bio": u.bio or "",
        "avatar_url": u.avatar_url or "",
        "inbox_link": u.inbox_link or "",
        "is_bot_synced": u.telegram_user_id is not None,
        "is_alumni": is_alumni,
        "is_deleted": False,
        "role_badge": "AUA Alumni" if is_alumni else "AUA Student",
        "created_at": u.created_at.isoformat() if u.created_at else None,
    }


DAY_MAP_SHORT = {
    "MON": "M", "MONDAY": "M",
    "TUE": "T", "TUES": "T", "TUESDAY": "T",
    "WED": "W", "WEDNESDAY": "W",
    "THU": "TH", "THUR": "TH", "THURS": "TH", "THURSDAY": "TH",
    "FRI": "F", "FRIDAY": "F",
    "SAT": "SAT", "SATURDAY": "SAT",
    "SUN": "SUN", "SUNDAY": "SUN",
}
DAY_ORDER_LIST = ["M", "T", "W", "TH", "F", "SAT", "SUN"]
SINGLE_DAY_LABEL = {"M": "MON", "T": "TUE", "W": "WED", "TH": "THU", "F": "FRI", "SAT": "SAT", "SUN": "SUN"}


def format_schedule_times(raw: Optional[str]) -> str:
    if not raw or not raw.strip() or raw.strip().upper() == "TBA":
        return "TBA"
    text = raw.strip()
    text = re.sub(r"TUE/THU\b", "TTH", text, flags=re.IGNORECASE)
    text = re.sub(r"MON/WED/FRI\b", "MWF", text, flags=re.IGNORECASE)
    text = re.sub(r"MON/WED\b", "MW", text, flags=re.IGNORECASE)
    if "MWF" in text or "TTH" in text:
        return text

    parts = [p.strip() for p in text.split(",") if p.strip()]
    parsed = []
    for p in parts:
        m = re.match(r"^([A-Za-z/]+)\s+(.+)$", p)
        if m:
            day_str, time_str = m.group(1).upper(), m.group(2).strip()
            days = []
            for d in re.split(r"[/,\s]+", day_str):
                d = d.strip()
                if d in DAY_MAP_SHORT:
                    days.append(DAY_MAP_SHORT[d])
                elif d:
                    days.append(d)
            parsed.append((days, time_str))
        else:
            return text

    from collections import OrderedDict
    time_groups = OrderedDict()
    for days, t in parsed:
        t_norm = re.sub(r"\s*-\s*", "-", t)
        if t_norm not in time_groups:
            time_groups[t_norm] = []
        for d in days:
            if d not in time_groups[t_norm]:
                time_groups[t_norm].append(d)

    formatted_groups = []
    for t_norm, d_list in time_groups.items():
        d_list.sort(key=lambda x: DAY_ORDER_LIST.index(x) if x in DAY_ORDER_LIST else 99)
        if d_list == ["M", "W", "F"]:
            day_code = "MWF"
        elif d_list == ["T", "TH"]:
            day_code = "TTH"
        elif d_list == ["M", "W"]:
            day_code = "MW"
        elif d_list == ["M", "F"]:
            day_code = "MF"
        elif d_list == ["W", "F"]:
            day_code = "WF"
        elif d_list == ["M", "T", "W", "TH", "F"]:
            day_code = "MTWTHF"
        elif len(d_list) == 1:
            day_code = SINGLE_DAY_LABEL.get(d_list[0], d_list[0])
        else:
            day_code = ", ".join(SINGLE_DAY_LABEL.get(d, d) for d in d_list)
        formatted_groups.append(f"{day_code} {t_norm}")

    return ", ".join(formatted_groups)


def serialize_course(c: Course) -> Dict[str, Any]:
    times_formatted = format_schedule_times(c.schedule_times)
    return {
        "id": c.id,
        "semester_label": c.semester_label,
        "course_code": c.course_code,
        "course_title": c.course_title,
        "section": c.section,
        "credits": c.credits or "",
        "instructor": c.instructor or "TBA",
        "schedule_times": times_formatted,
        "location": c.location or "",
        "display_label": f"{c.course_code} ({c.section}) — {c.course_title} • {c.instructor or 'TBA'} [{times_formatted}]",
    }


def serialize_have_need(p: HaveNeedPost, current_user_id: Optional[int] = None) -> Dict[str, Any]:
    u = p.web_user
    is_deleted = getattr(u, "is_deleted", False) or (u and u.name == "Deleted Account")
    return {
        "id": p.id,
        "web_user_id": p.web_user_id,
        "is_owner": bool(current_user_id and p.web_user_id == current_user_id and not is_deleted),
        "author": {
            "id": u.id if u else p.web_user_id,
            "name": "Deleted Account" if is_deleted else (u.name if u else "AUA Student"),
            "bio": "" if is_deleted else ((u.bio or "") if u else ""),
            "avatar_url": "/static/bot-logo.svg" if is_deleted else ((u.avatar_url or "") if u else ""),
            "inbox_link": "" if is_deleted else ((u.inbox_link or "") if u else ""),
            "is_bot_synced": False if is_deleted else bool(u and u.telegram_user_id is not None),
            "is_alumni": False if is_deleted else bool(u and u.email.lower().endswith("@alumni.aua.am")),
            "is_deleted": is_deleted,
        },
        "have_course": serialize_course(p.have_course) if p.have_course else None,
        "need_course": serialize_course(p.need_course) if p.need_course else None,
        "contact_dm": "" if is_deleted else p.contact_dm,
        "notes": p.notes or "",
        "notify_email": bool(getattr(p, "notify_email", True)),
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


def serialize_group(g: TelegramGroup, current_user_id: Optional[int] = None) -> Dict[str, Any]:
    u = g.web_user
    is_deleted = getattr(u, "is_deleted", False) or (u and u.name == "Deleted Account")
    return {
        "id": g.id,
        "web_user_id": g.web_user_id,
        "is_owner": bool(current_user_id and g.web_user_id == current_user_id and not is_deleted),
        "name": g.name,
        "description": g.description or "",
        "invite_link": g.invite_link,
        "image_url": g.image_url or "",
        "author_id": u.id if u else g.web_user_id,
        "author_name": "Deleted Account" if is_deleted else (u.name if u else "AUA Student"),
        "author_avatar": "/static/bot-logo.svg" if is_deleted else ((u.avatar_url or "") if u else ""),
        "author_bio": "" if is_deleted else ((u.bio or "") if u else ""),
        "author_inbox_link": "" if is_deleted else ((u.inbox_link or "") if u else ""),
        "is_deleted": is_deleted,
        "created_at": g.created_at.isoformat() if g.created_at else None,
    }


def clean_chat_snippet(html_str: str, max_len: int = 70) -> str:
    if not html_str:
        return ""
    clean = re.sub(r"<[^>]+>", " ", html_str)
    clean = html.unescape(clean)
    clean = " ".join(clean.split())
    if len(clean) > max_len:
        return clean[:max_len] + "…"
    return clean


def serialize_spam_message(m: SpamChatMessage) -> Dict[str, Any]:
    u = m.web_user
    is_deleted = getattr(u, "is_deleted", False) or (u and u.name == "Deleted Account")
    media_t = getattr(m, "media_type", None)
    if not media_t:
        img = (m.image_url or "").lower()
        if img.endswith((".mp4", ".webm", ".mov")):
            media_t = "video"
        elif img.endswith(".gif"):
            media_t = "gif"
        elif img:
            media_t = "image"
        else:
            media_t = ""

    reply_to_data = None
    if getattr(m, "reply_to", None) and m.reply_to:
        parent = m.reply_to
        pu = parent.web_user
        pu_deleted = getattr(pu, "is_deleted", False) or (pu and pu.name == "Deleted Account")
        snippet = clean_chat_snippet(parent.content_html)
        if not snippet and parent.image_url:
            snippet = "Photo" if (parent.media_type or "image") == "image" else "Video"
        reply_to_data = {
            "id": parent.id,
            "author_id": pu.id if pu else parent.web_user_id,
            "author_name": "Deleted Account" if pu_deleted else (pu.name if pu else "AUA Student"),
            "snippet": snippet or "Message",
            "has_media": bool(parent.image_url),
        }

    reactions_dict: Dict[str, Dict[str, Any]] = {}
    if hasattr(m, "reactions") and m.reactions:
        for r in m.reactions:
            if r.emoji not in reactions_dict:
                reactions_dict[r.emoji] = {
                    "emoji": r.emoji,
                    "count": 0,
                    "user_ids": [],
                    "users": [],
                }
            reactions_dict[r.emoji]["count"] += 1
            reactions_dict[r.emoji]["user_ids"].append(r.web_user_id)
            r_user = r.web_user
            r_deleted = getattr(r_user, "is_deleted", False) or (r_user and r_user.name == "Deleted Account")
            u_name = "Deleted Account" if r_deleted else (r_user.name if r_user else "AUA Student")
            u_avatar = "/static/bot-logo.svg" if r_deleted else ((r_user.avatar_url or "") if r_user else "")
            reactions_dict[r.emoji]["users"].append({
                "id": r.web_user_id,
                "name": u_name,
                "avatar": u_avatar,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            })
    reactions_list = list(reactions_dict.values())

    return {
        "id": m.id,
        "web_user_id": m.web_user_id,
        "author_id": u.id if u else m.web_user_id,
        "author_name": "Deleted Account" if is_deleted else (u.name if u else "AUA Student"),
        "author_avatar": "/static/bot-logo.svg" if is_deleted else ((u.avatar_url or "") if u else ""),
        "author_bio": "" if is_deleted else ((u.bio or "") if u else ""),
        "author_inbox_link": "" if is_deleted else ((u.inbox_link or "") if u else ""),
        "is_deleted": is_deleted,
        "content_html": m.content_html,
        "image_url": m.image_url or "",
        "media_type": media_t,
        "reply_to": reply_to_data,
        "reactions": reactions_list,
        "is_edited": getattr(m, "is_edited", False) or False,
        "edited_at": m.edited_at.isoformat() if getattr(m, "edited_at", None) else None,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


def serialize_academic_feedback(
    fb: AcademicFeedback,
    current_user_id: Optional[int] = None,
) -> Dict[str, Any]:
    u = fb.web_user
    is_deleted = getattr(u, "is_deleted", False) or (u and u.name == "Deleted Account")
    is_owner = bool(current_user_id and fb.web_user_id is not None and fb.web_user_id == current_user_id and not is_deleted)
    if fb.is_anonymous or fb.web_user_id is None:
        author_id = None
        author_name = "Anonymous AUA Student"
        author_avatar = "/static/bot-logo.svg"
        author_bio = ""
        author_inbox_link = ""
    else:
        author_id = u.id if u else fb.web_user_id
        author_name = "Deleted Account" if is_deleted else (u.name if u else "AUA Student")
        author_avatar = "/static/bot-logo.svg" if is_deleted else ((u.avatar_url or "") if u else "")
        author_bio = "" if is_deleted else ((u.bio or "") if u else "")
        author_inbox_link = "" if is_deleted else ((u.inbox_link or "") if u else "")

    return {
        "id": fb.id,
        "target_type": fb.target_type,
        "target_key": fb.target_key,
        "target_title": fb.target_title,
        "target_subtitle": fb.target_subtitle or "",
        "rating": fb.rating,
        "comment": fb.comment,
        "course_name": getattr(fb, "course_name", None) or "",
        "would_take_again": getattr(fb, "would_take_again", None),
        "is_anonymous": fb.is_anonymous or fb.web_user_id is None,
        "is_owner": is_owner,
        "author_id": author_id,
        "author_name": author_name,
        "author_avatar": author_avatar,
        "author_bio": author_bio,
        "author_inbox_link": author_inbox_link,
        "is_deleted": False if (fb.is_anonymous or fb.web_user_id is None) else is_deleted,
        "created_at": fb.created_at.isoformat() if fb.created_at else None,
        "updated_at": fb.updated_at.isoformat() if fb.updated_at else None,
    }


# ==============================================================================
# Route Handlers
# ==============================================================================

routes = web.RouteTableDef()


@routes.get("/api/health")
async def api_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "service": "AUA Hub"})


# ------------------------------------------------------------------------------
# 1. Authentication & Registration
# ------------------------------------------------------------------------------

@routes.post("/api/auth/register")
async def api_auth_register(request: web.Request) -> web.Response:
    client_ip = get_client_ip(request)
    allowed_ip, retry_ip = check_rate_limit("auth_register_ip", client_ip, max_calls=5, window_sec=600)
    if not allowed_ip:
        return web.json_response(
            {"error": f"Too many registration attempts from your IP. Please wait {retry_ip}s."},
            status=429,
        )

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body."}, status=400)

    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    confirm_password = data.get("confirm_password") or ""

    if len(name) < 2 or len(name) > 80:
        return web.json_response({"error": "Please enter your full name (2–80 characters)."}, status=400)

    if not is_valid_aua_email(email):
        return web.json_response(
            {
                "error": "Only official @edu.aua.am (students) and @alumni.aua.am (graduates) email addresses are allowed."
            },
            status=400,
        )

    local_part = email.split("@", 1)[0]
    if local_part in BLOCKED_LOCAL_TERMS:
        return web.json_response(
            {"error": "Generic or dummy email addresses are not permitted. Please use your personal AUA email."},
            status=400,
        )

    allowed_email, retry_email = check_rate_limit("auth_register_email", email, max_calls=4, window_sec=600)
    if not allowed_email:
        return web.json_response(
            {"error": f"Too many OTP requests for this email. Please wait {retry_email}s."},
            status=429,
        )

    if len(password) < 6 or len(password) > 128:
        return web.json_response({"error": "Password must be between 6 and 128 characters long."}, status=400)

    if password != confirm_password:
        return web.json_response({"error": "Passwords do not match."}, status=400)

    async with get_db_session() as session:
        existing = await get_web_user_by_email(session, email)
        if existing:
            return web.json_response(
                {"error": "An AUA Hub account with this email already exists. Please Sign In instead."},
                status=409,
            )

        otp_code, _ = await create_web_registration_otp(
            session=session,
            name=name,
            email=email,
            password=password,
        )

    sent, _ = await send_verification_email(to_email=email, code=otp_code)
    if not sent:
        return web.json_response(
            {"error": "Failed to send verification OTP email. Please check your email address and try again."},
            status=500,
        )

    return web.json_response(
        {
            "ok": True,
            "email": email,
            "message": f"A 6-digit verification code has been sent to {email}.",
        }
    )


@routes.post("/api/auth/verify-otp")
async def api_auth_verify_otp(request: web.Request) -> web.Response:
    client_ip = get_client_ip(request)
    allowed_ip, retry_ip = check_rate_limit("auth_otp_ip", client_ip, max_calls=8, window_sec=300)
    if not allowed_ip:
        return web.json_response(
            {"error": f"Too many OTP verification attempts. Please wait {retry_ip}s."},
            status=429,
        )

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body."}, status=400)

    email = (data.get("email") or "").strip().lower()
    code = (data.get("code") or "").strip()

    if not email or not code:
        return web.json_response({"error": "Email and 6-digit OTP code are required."}, status=400)

    allowed_email, retry_email = check_rate_limit("auth_otp_email", email, max_calls=8, window_sec=300)
    if not allowed_email:
        return web.json_response(
            {"error": f"Too many OTP verification attempts for this email. Please wait {retry_email}s."},
            status=429,
        )

    async with get_db_session() as session:
        ok, web_user, msg = await verify_web_otp_and_register(
            session=session,
            email=email,
            otp_code=code,
        )
        if not ok or not web_user:
            return web.json_response({"error": msg}, status=400)

        unread_count = await get_spam_chat_unread_count(session, web_user.id)
        user_dict = serialize_web_user(web_user)

    token = create_jwt_token(web_user.id)
    response = web.json_response(
        {
            "ok": True,
            "token": token,
            "user": user_dict,
            "unread_spam_count": unread_count,
            "message": msg,
        }
    )
    response.set_cookie(
        "aua_hub_token",
        token,
        max_age=30 * 86400,
        httponly=True,
        secure=_is_https_request(request),
        samesite="Lax",
        path="/",
    )
    return response


@routes.post("/api/auth/login")
async def api_auth_login(request: web.Request) -> web.Response:
    client_ip = get_client_ip(request)
    allowed_ip, retry_ip = check_rate_limit("auth_login_ip", client_ip, max_calls=10, window_sec=300)
    if not allowed_ip:
        return web.json_response(
            {"error": f"Too many sign-in attempts from your IP. Please wait {retry_ip}s."},
            status=429,
        )

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body."}, status=400)

    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not email or not password:
        return web.json_response({"error": "Please enter both your AUA email and password."}, status=400)

    allowed_email, retry_email = check_rate_limit("auth_login_email", email, max_calls=8, window_sec=300)
    if not allowed_email:
        return web.json_response(
            {"error": f"Too many sign-in attempts for this account. Please wait {retry_email}s."},
            status=429,
        )

    async with get_db_session() as session:
        web_user = await authenticate_web_user(session, email=email, password=password)
        if not web_user:
            return web.json_response({"error": "Invalid email or password."}, status=401)

        unread_count = await get_spam_chat_unread_count(session, web_user.id)
        user_dict = serialize_web_user(web_user)

    token = create_jwt_token(web_user.id)
    response = web.json_response(
        {
            "ok": True,
            "token": token,
            "user": user_dict,
            "unread_spam_count": unread_count,
        }
    )
    response.set_cookie(
        "aua_hub_token",
        token,
        max_age=30 * 86400,
        httponly=True,
        secure=_is_https_request(request),
        samesite="Lax",
        path="/",
    )
    return response


@routes.post("/api/auth/logout")
async def api_auth_logout(request: web.Request) -> web.Response:
    response = web.json_response({"ok": True})
    response.del_cookie("aua_hub_token", path="/")
    return response


@routes.get("/api/auth/me")
async def api_auth_me(request: web.Request) -> web.Response:
    user = await get_current_web_user(request)
    if not user:
        return web.json_response({"authenticated": False, "user": None, "unread_spam_count": 0})

    async with get_db_session() as session:
        unread_count = await get_spam_chat_unread_count(session, user.id)

    response = web.json_response(
        {
            "authenticated": True,
            "user": serialize_web_user(user),
            "unread_spam_count": unread_count,
        }
    )
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer ") and not request.cookies.get("aua_hub_token"):
        bearer_token = auth_header[7:].strip()
        if bearer_token:
            response.set_cookie(
                "aua_hub_token",
                bearer_token,
                max_age=30 * 86400,
                httponly=True,
                secure=_is_https_request(request),
                samesite="Lax",
                path="/",
            )
    return response


# ------------------------------------------------------------------------------
# 2. Profile & Image Upload (<Profile>)
# ------------------------------------------------------------------------------

@routes.put("/api/profile")
async def api_update_profile(request: web.Request) -> web.Response:
    user = await get_current_web_user(request)
    if not user:
        return web.json_response({"error": "Authentication required."}, status=401)

    allowed, retry = check_rate_limit("profile_update", str(user.id), max_calls=15, window_sec=60)
    if not allowed:
        return web.json_response({"error": f"Too many profile updates. Please wait {retry}s."}, status=429)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body."}, status=400)

    name = data.get("name")
    bio = data.get("bio")
    avatar_url = data.get("avatar_url")
    inbox_link = data.get("inbox_link")

    if avatar_url is not None:
        avatar_url = avatar_url.strip()
        if not is_safe_upload_url(avatar_url):
            return web.json_response(
                {"error": "Invalid avatar URL. Please upload an image directly on AUA Hub."},
                status=400,
            )

    if bio:
        link_err = scan_web_text_for_malicious_links(bio)
        if link_err:
            return web.json_response({"error": link_err}, status=400)

    if inbox_link is not None:
        inbox_link = inbox_link.strip()
        if inbox_link and not (
            inbox_link.startswith("https://t.me/")
            or inbox_link.startswith("http://t.me/")
            or inbox_link.startswith("t.me/")
        ):
            return web.json_response(
                {"error": "Inbox link must be a valid Telegram bot link (e.g. https://t.me/aua_anonymous_bot?start=msg_...)."},
                status=400,
            )
        if inbox_link.startswith("t.me/"):
            inbox_link = f"https://{inbox_link}"

    async with get_db_session() as session:
        updated = await update_web_user_profile(
            session=session,
            web_user_id=user.id,
            name=name,
            bio=bio,
            avatar_url=avatar_url,
            inbox_link=inbox_link,
        )
        if not updated:
            return web.json_response({"error": "User not found."}, status=404)

        # If the user pasted a ?start=msg_<token> link and isn't linked to a Telegram account yet,
        # check if that inbox token exists and link their account automatically!
        if inbox_link and not updated.telegram_user_id:
            m = re.search(r"start=msg[_-]([A-Za-z0-9_-]+)", inbox_link)
            if m:
                token_str = m.group(1)
                inbox_obj = (
                    await session.execute(
                        select(AnonymousInbox).where(
                            AnonymousInbox.token == token_str,
                            AnonymousInbox.is_active.is_(True),
                        )
                    )
                ).scalar_one_or_none()
                if inbox_obj:
                    other_web = (
                        await session.execute(
                            select(WebUser).where(WebUser.telegram_user_id == inbox_obj.owner_id)
                        )
                    ).scalar_one_or_none()
                    if not other_web:
                        updated.telegram_user_id = inbox_obj.owner_id
                        await session.commit()
                        await session.refresh(updated)

        return web.json_response({"ok": True, "user": serialize_web_user(updated)})


@routes.delete("/api/profile/delete-account")
@routes.post("/api/profile/delete-account")
async def api_delete_account(request: web.Request) -> web.Response:
    user = await get_current_web_user(request)
    if not user:
        return web.json_response({"error": "Authentication required."}, status=401)

    async with get_db_session() as session:
        ok = await delete_or_anonymize_web_user(session, user.id)
        if not ok:
            return web.json_response({"error": "Account could not be deleted or is already deleted."}, status=400)

    logger.info("WebUser %s (%s) successfully deleted and scrubbed.", user.id, user.email)
    response = web.json_response({"ok": True, "message": "Your account has been deleted successfully."})
    response.del_cookie("aua_hub_token", path="/")
    return response


@routes.get("/api/users/{user_id}/profile")
async def api_get_public_user_profile(request: web.Request) -> web.Response:
    try:
        target_uid = int(request.match_info["user_id"])
    except (KeyError, ValueError):
        return web.json_response({"error": "Invalid user ID."}, status=400)

    viewer = await get_current_web_user(request)
    viewer_id = viewer.id if viewer else None

    async with get_db_session() as session:
        target_user = await get_web_user_by_id(session, target_uid)
        if not target_user or getattr(target_user, "is_deleted", False) or target_user.name == "Deleted Account":
            return web.json_response({"error": "Student profile not found or account has been deleted."}, status=404)

        hn_posts = await list_have_need_posts(
            session=session,
            only_user_id=target_user.id,
            limit=20,
        )
        from sqlalchemy.orm import selectinload

        groups_rows = (
            await session.execute(
                select(TelegramGroup)
                .options(selectinload(TelegramGroup.web_user))
                .where(TelegramGroup.web_user_id == target_user.id)
                .order_by(TelegramGroup.created_at.desc())
                .limit(12)
            )
        ).scalars().all()

        return web.json_response(
            {
                "ok": True,
                "user": serialize_public_web_user(target_user),
                "is_self": bool(viewer_id and viewer_id == target_user.id),
                "have_need_posts": [serialize_have_need(p, current_user_id=viewer_id) for p in hn_posts],
                "telegram_groups": [serialize_group(g, current_user_id=viewer_id) for g in groups_rows],
            }
        )


@routes.post("/api/upload")
async def api_upload_file(request: web.Request) -> web.Response:
    user = await get_current_web_user(request)
    if not user:
        return web.json_response({"error": "Please sign in to upload files."}, status=401)

    allowed, retry = check_rate_limit("file_upload", str(user.id), max_calls=15, window_sec=60)
    if not allowed:
        return web.json_response({"error": f"Upload rate limit reached. Please wait {retry}s."}, status=429)

    try:
        reader = await request.multipart()
        field = await reader.next()
    except Exception:
        return web.json_response({"error": "Invalid multipart upload request."}, status=400)

    if field is None or field.name != "file":
        return web.json_response({"error": "No file field provided."}, status=400)

    filename = field.filename or "upload.png"
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_MEDIA_EXTENSIONS:
        return web.json_response(
            {"error": "Unsupported file format. Allowed: Images (PNG, JPG, JPEG, WEBP, GIF up to 10 MB) and Videos (MP4, WEBM, MOV up to 25 MB)."},
            status=400,
        )

    is_video = ext in ALLOWED_VIDEO_EXTENSIONS
    max_bytes = MAX_VIDEO_UPLOAD_BYTES if is_video else MAX_IMAGE_UPLOAD_BYTES
    max_label = "25 MB" if is_video else "10 MB"

    chunks = bytearray()
    while True:
        chunk = await field.read_chunk(65536)
        if not chunk:
            break
        chunks.extend(chunk)
        if len(chunks) > max_bytes:
            return web.json_response({"error": f"File size exceeds the {max_label} limit."}, status=413)

    if not chunks:
        return web.json_response({"error": "Uploaded file is empty."}, status=400)

    raw_bytes = bytes(chunks)
    is_valid, media_type = validate_media_magic_bytes(raw_bytes, ext)
    if not is_valid:
        return web.json_response(
            {"error": f"Uploaded file content does not match a valid {ext} signature (file header magic bytes check failed)."},
            status=400,
        )

    safe_name = f"{int(time.time())}_{secrets.token_hex(8)}{ext}"
    dest_path = UPLOADS_DIR / safe_name
    dest_path.write_bytes(raw_bytes)

    return web.json_response({
        "ok": True,
        "url": f"/uploads/{safe_name}",
        "media_type": media_type,
        "filename": filename,
    })


# ------------------------------------------------------------------------------
# 3. Courses & Have-Need (<Have-Need>)
# ------------------------------------------------------------------------------

@routes.get("/api/courses")
async def api_get_courses(request: web.Request) -> web.Response:
    q = (request.query.get("q") or "").strip()
    refresh = request.query.get("refresh") == "1"
    try:
        limit = min(max(1, int(request.query.get("limit", "250"))), 700)
    except ValueError:
        limit = 250

    user = await get_current_web_user(request) if refresh else None

    async with get_db_session() as session:
        sem_label = await get_active_semester_label(session)
        allow_forced = False
        if refresh and user:
            allow_forced, _ = check_rate_limit("global_scrape", "sonis_courses", max_calls=1, window_sec=60)
        if not sem_label or allow_forced:
            try:
                sem_label, _ = await sync_courses_from_sonis(session)
            except Exception as e:
                logger.warning("Course sync on /api/courses failed: %s", e)

        courses = await search_courses(
            session=session,
            query=q,
            semester_label=sem_label,
            limit=limit,
        )

    return web.json_response(
        {
            "semester": sem_label or "Upcoming Semester",
            "count": len(courses),
            "courses": [serialize_course(c) for c in courses],
        }
    )


@routes.get("/api/have-need")
async def api_list_have_need(request: web.Request) -> web.Response:
    user = await get_current_web_user(request)
    q = (request.query.get("q") or "").strip()
    mine = request.query.get("mine") == "1"

    only_user_id = user.id if (mine and user) else None
    if mine and not user:
        return web.json_response({"error": "Sign in to view your own posts."}, status=401)

    async with get_db_session() as session:
        sem_label = await get_active_semester_label(session)
        posts = await list_have_need_posts(
            session=session,
            search_query=q,
            only_user_id=only_user_id,
            limit=150,
        )
        current_uid = user.id if user else None
        serialized = [serialize_have_need(p, current_user_id=current_uid) for p in posts]

    return web.json_response(
        {
            "semester": sem_label or "Current Semester",
            "posts": serialized,
        }
    )


async def _process_have_need_matches(
    session: AsyncSession,
    post: HaveNeedPost,
    user: WebUser,
) -> int:
    """Detect reciprocal Have-Need swap matches, create in-app notifications, broadcast WebSocket
    alerts, and send email notifications to users who opted into `notify_email`.
    """
    hc = post.have_course or await session.get(Course, post.have_course_id)
    nc = post.need_course or await session.get(Course, post.need_course_id)
    if not hc or not nc:
        return 0

    matches = await find_reciprocal_have_need_matches(
        session=session,
        have_course_id=post.have_course_id,
        need_course_id=post.need_course_id,
        exclude_user_id=user.id,
    )

    my_have_short = f"{hc.course_code} [Sec {hc.section}]"
    my_need_short = f"{nc.course_code} [Sec {nc.section}]"
    my_have_full = f"{hc.course_code} (Sec {hc.section}) — {hc.course_title}"
    my_need_full = f"{nc.course_code} (Sec {nc.section}) — {nc.course_title}"

    for m_post in matches:
        partner = m_post.web_user
        if not partner or getattr(partner, "is_deleted", False):
            continue

        p_hc = m_post.have_course or nc
        p_nc = m_post.need_course or hc
        p_have_short = f"{p_hc.course_code} [Sec {p_hc.section}]"
        p_need_short = f"{p_nc.course_code} [Sec {p_nc.section}]"
        p_have_full = f"{p_hc.course_code} (Sec {p_hc.section}) — {p_hc.course_title}"
        p_need_full = f"{p_nc.course_code} (Sec {p_nc.section}) — {p_nc.course_title}"

        # 1. In-app notification + live WebSocket alert for the partner (existing listing owner)
        p_notif = await create_user_notification(
            session=session,
            recipient_id=partner.id,
            sender_id=user.id,
            category="have_need_match",
            title=f"Swap Match: {p_have_short} ⇄ {p_need_short}",
            content=(
                f"Reciprocal match found with {user.name}! "
                f"They have {p_need_short} and need {p_have_short}. Contact: {post.contact_dm}"
            ),
            target_type="have_need",
            target_id=str(post.id),
            extra_data={
                "post_id": m_post.id,
                "matched_post_id": post.id,
                "partner_name": user.name,
                "partner_dm": post.contact_dm,
                "have_code": p_hc.course_code,
                "need_code": p_nc.course_code,
                "have_course_code": p_hc.course_code,
                "need_course_code": p_nc.course_code,
            },
        )
        p_unread = await get_unread_notification_count(session, partner.id)
        await broadcast_ws_event(
            {
                "type": "new_notification",
                "recipient_id": partner.id,
                "notification": serialize_user_notification(p_notif),
                "unread_count": p_unread,
            }
        )

        # 2. In-app notification + live WebSocket alert for the current user
        my_notif = await create_user_notification(
            session=session,
            recipient_id=user.id,
            sender_id=partner.id,
            category="have_need_match",
            title=f"Swap Match: {my_have_short} ⇄ {my_need_short}",
            content=(
                f"Reciprocal match found with {partner.name}! "
                f"They have {my_need_short} and need {my_have_short}. Contact: {m_post.contact_dm}"
            ),
            target_type="have_need",
            target_id=str(m_post.id),
            extra_data={
                "post_id": post.id,
                "matched_post_id": m_post.id,
                "partner_name": partner.name,
                "partner_dm": m_post.contact_dm,
                "have_code": hc.course_code,
                "need_code": nc.course_code,
                "have_course_code": hc.course_code,
                "need_course_code": nc.course_code,
            },
        )
        my_unread = await get_unread_notification_count(session, user.id)
        await broadcast_ws_event(
            {
                "type": "new_notification",
                "recipient_id": user.id,
                "notification": serialize_user_notification(my_notif),
                "unread_count": my_unread,
            }
        )

        # 3. Email alerts for each party that opted into "Receive an email if there is a match"
        if getattr(m_post, "notify_email", True) and partner.email:
            asyncio.create_task(
                send_have_need_match_email(
                    to_email=partner.email,
                    recipient_name=partner.name,
                    partner_name=user.name,
                    partner_contact_dm=post.contact_dm,
                    partner_notes=post.notes,
                    my_have_label=p_have_full,
                    my_need_label=p_need_full,
                )
            )

        if getattr(post, "notify_email", True) and user.email:
            asyncio.create_task(
                send_have_need_match_email(
                    to_email=user.email,
                    recipient_name=user.name,
                    partner_name=partner.name,
                    partner_contact_dm=m_post.contact_dm,
                    partner_notes=m_post.notes,
                    my_have_label=my_have_full,
                    my_need_label=my_need_full,
                )
            )

    return len(matches)


@routes.post("/api/have-need")
async def api_create_have_need(request: web.Request) -> web.Response:
    user = await get_current_web_user(request)
    if not user:
        return web.json_response({"error": "Please sign in to post a Have-Need exchange."}, status=401)

    allowed, retry = check_rate_limit("have_need_write", str(user.id), max_calls=10, window_sec=60)
    if not allowed:
        return web.json_response({"error": f"Rate limit reached. Please wait {retry}s."}, status=429)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body."}, status=400)

    try:
        have_course_id = int(data.get("have_course_id"))
        need_course_id = int(data.get("need_course_id"))
    except (TypeError, ValueError):
        return web.json_response({"error": "Please select both the course you Have and the course you Need."}, status=400)

    if have_course_id == need_course_id:
        return web.json_response({"error": "'Have' and 'Need' cannot be the exact same course section."}, status=400)

    contact_dm = (data.get("contact_dm") or "").strip()
    notes = (data.get("notes") or "").strip()
    notify_email = bool(data.get("notify_email", True))

    if not contact_dm:
        return web.json_response(
            {"error": "Please provide your Telegram DM (@username) or contact info so students can reach you."},
            status=400,
        )

    link_err = scan_web_text_for_malicious_links(f"{contact_dm} {notes}")
    if link_err:
        return web.json_response({"error": link_err}, status=400)

    async with get_db_session() as session:
        hc = await session.get(Course, have_course_id)
        nc = await session.get(Course, need_course_id)
        if not hc or not nc:
            return web.json_response({"error": "Selected course section was not found."}, status=404)

        post = await create_have_need_post(
            session=session,
            web_user_id=user.id,
            have_course_id=have_course_id,
            need_course_id=need_course_id,
            contact_dm=contact_dm,
            notes=notes,
            notify_email=notify_email,
        )

        matches_count = await _process_have_need_matches(session=session, post=post, user=user)

        return web.json_response({
            "ok": True,
            "post": serialize_have_need(post, current_user_id=user.id),
            "matches_count": matches_count,
        })


@routes.put("/api/have-need/{post_id}")
async def api_update_have_need(request: web.Request) -> web.Response:
    user = await get_current_web_user(request)
    if not user:
        return web.json_response({"error": "Authentication required."}, status=401)

    allowed, retry = check_rate_limit("have_need_write", str(user.id), max_calls=10, window_sec=60)
    if not allowed:
        return web.json_response({"error": f"Rate limit reached. Please wait {retry}s."}, status=429)

    try:
        post_id = int(request.match_info["post_id"])
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid request."}, status=400)

    have_cid = int(data["have_course_id"]) if data.get("have_course_id") else None
    need_cid = int(data["need_course_id"]) if data.get("need_course_id") else None
    if have_cid and need_cid and have_cid == need_cid:
        return web.json_response({"error": "'Have' and 'Need' cannot be the exact same course section."}, status=400)

    contact_dm = data.get("contact_dm")
    notes = data.get("notes")
    notify_email = bool(data["notify_email"]) if "notify_email" in data else None

    link_err = scan_web_text_for_malicious_links(f"{contact_dm or ''} {notes or ''}")
    if link_err:
        return web.json_response({"error": link_err}, status=400)

    async with get_db_session() as session:
        existing = await session.get(HaveNeedPost, post_id)
        old_pair = (existing.have_course_id, existing.need_course_id) if existing else (None, None)

        updated = await update_have_need_post(
            session=session,
            post_id=post_id,
            web_user_id=user.id,
            have_course_id=have_cid,
            need_course_id=need_cid,
            contact_dm=contact_dm,
            notes=notes,
            notify_email=notify_email,
        )
        if not updated:
            return web.json_response({"error": "Post not found or not owned by you."}, status=404)

        matches_count = 0
        if (updated.have_course_id, updated.need_course_id) != old_pair:
            matches_count = await _process_have_need_matches(session=session, post=updated, user=user)

        return web.json_response({
            "ok": True,
            "post": serialize_have_need(updated, current_user_id=user.id),
            "matches_count": matches_count,
        })


@routes.delete("/api/have-need/{post_id}")
async def api_delete_have_need(request: web.Request) -> web.Response:
    user = await get_current_web_user(request)
    if not user:
        return web.json_response({"error": "Authentication required."}, status=401)

    try:
        post_id = int(request.match_info["post_id"])
    except ValueError:
        return web.json_response({"error": "Invalid post ID."}, status=400)

    async with get_db_session() as session:
        deleted = await delete_have_need_post(session, post_id=post_id, web_user_id=user.id)
        if not deleted:
            return web.json_response({"error": "Post not found or not owned by you."}, status=404)

    return web.json_response({"ok": True})


# ------------------------------------------------------------------------------
# 4. Anonymous Public Posts & Synced Bot Inbox (<Anonymous>)
# ------------------------------------------------------------------------------

@routes.get("/api/anonymous/posts")
async def api_list_anonymous_posts(request: web.Request) -> web.Response:
    try:
        page = max(1, int(request.query.get("page", "1")))
        per_page = min(50, max(5, int(request.query.get("per_page", "20"))))
    except ValueError:
        page, per_page = 1, 20

    q = (request.query.get("q") or "").strip()[:100]
    user = await get_current_web_user(request)
    reporter_key = f"user:{user.id}" if user else f"ip:{get_client_ip(request)}"

    async with get_db_session() as session:
        posts, total = await list_anonymous_public_posts(session, search_query=q, page=page, per_page=per_page)
        reported_ids = await get_reported_anon_post_ids(session, reporter_key, [p.id for p in posts])

    total_pages = max(1, (total + per_page - 1) // per_page)
    return web.json_response(
        {
            "posts": [
                {
                    "index": p.id,
                    "content_text": p.content_text,
                    "report_count": int(p.report_count or 0),
                    "reported_by_me": p.id in reported_ids,
                    "created_at": p.created_at.isoformat() if p.created_at else None,
                }
                for p in posts
            ],
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "q": q,
            "report_hide_threshold": ANON_POST_REPORT_HIDE_THRESHOLD,
        }
    )


@routes.post("/api/anonymous/posts/{post_id}/report")
async def api_report_anonymous_post(request: web.Request) -> web.Response:
    try:
        post_id = int(request.match_info["post_id"])
    except (KeyError, ValueError):
        return web.json_response({"error": "Invalid post ID."}, status=400)

    user = await get_current_web_user(request)
    reporter_key = f"user:{user.id}" if user else f"ip:{get_client_ip(request)}"

    allowed, retry = check_rate_limit("report_anon_post", reporter_key, max_calls=15, window_sec=300)
    if not allowed:
        return web.json_response(
            {"error": f"Too many report submissions. Please wait {retry}s."}, status=429
        )

    reason = None
    try:
        body = await request.json()
        if isinstance(body, dict):
            reason = (body.get("reason") or "").strip()[:250] or None
    except Exception:
        pass

    async with get_db_session() as session:
        res = await report_anonymous_public_post(
            session=session,
            post_id=post_id,
            reporter_key=reporter_key,
            web_user_id=user.id if user else None,
            reason=reason,
            hide_threshold=ANON_POST_REPORT_HIDE_THRESHOLD,
        )

    if not res.get("ok"):
        return web.json_response({"error": res.get("error", "Post not found.")}, status=404)

    # Notify administrators if this report crossed the auto-hide threshold
    if res.get("newly_hidden") and _BOT_INSTANCE is not None:
        post_obj = res.get("post")
        preview_txt = html.escape((post_obj.content_text if post_obj else "")[:350])
        alert_msg = (
            f"🚩 <b>Anonymous Post #{post_id} Auto-Hidden (Tier 3 Moderation)</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"• <b>Unique Community Reports:</b> {res.get('report_count')}/{ANON_POST_REPORT_HIDE_THRESHOLD}\n"
            f"• <b>Status:</b> Hidden from live feed\n\n"
            f"💬 <b>Post Content:</b>\n<i>{preview_txt}</i>"
        )
        for admin_id in settings.admin_ids:
            try:
                await _BOT_INSTANCE.send_message(chat_id=admin_id, text=alert_msg, parse_mode="HTML")
            except Exception:
                pass

    return web.json_response(
        {
            "ok": True,
            "post_id": post_id,
            "already_reported": bool(res.get("already_reported")),
            "report_count": int(res.get("report_count", 0)),
            "auto_hidden": bool(res.get("auto_hidden")),
            "threshold": ANON_POST_REPORT_HIDE_THRESHOLD,
        }
    )


@routes.post("/api/anonymous/sync-code")
async def api_generate_sync_code(request: web.Request) -> web.Response:
    user = await get_current_web_user(request)
    if not user:
        return web.json_response({"error": "Please sign in to AUA Hub first to sync with the bot."}, status=401)

    allowed, retry = check_rate_limit("sync_code", str(user.id), max_calls=6, window_sec=300)
    if not allowed:
        return web.json_response({"error": f"Too many sync code requests. Please wait {retry}s."}, status=429)

    async with get_db_session() as session:
        code = await generate_web_sync_code(session, web_user_id=user.id)

    return web.json_response(
        {
            "ok": True,
            "code": code,
            "expires_in_minutes": 15,
            "bot_username": "aua_anonymous_bot",
            "command": f"/sync {code}",
            "deep_link": f"https://t.me/aua_anonymous_bot?start=sync_{code}",
        }
    )


@routes.get("/api/anonymous/inbox")
async def api_get_anonymous_inbox(request: web.Request) -> web.Response:
    user = await get_current_web_user(request)
    if not user:
        return web.json_response({"error": "Please sign in to view your personal inbox."}, status=401)

    q = (request.query.get("q") or "").strip()[:100]

    async with get_db_session() as session:
        inbox_data = await get_synced_web_inbox(session, web_user=user, search_query=q)

    return web.json_response(inbox_data)


@routes.post("/api/analytics/event")
async def api_track_analytics_event(request: web.Request) -> web.Response:
    """Track user interaction events and button clicks for web analytics dashboard."""
    user = await get_current_web_user(request)
    try:
        data = await request.json()
        event_type = (data.get("event_type") or "").strip()[:64]
        meta_info = (data.get("meta_info") or "").strip()[:256] if data.get("meta_info") else None
    except Exception:
        return web.json_response({"ok": False}, status=400)

    if not event_type:
        return web.json_response({"ok": False}, status=400)

    # Light rate-limiting to prevent spamming
    ip = request.headers.get("CF-Connecting-IP") or request.headers.get("X-Forwarded-For") or request.remote or "anon"
    ident = f"{user.id}" if user else f"ip_{ip}"
    allowed, _ = check_rate_limit("analytics_evt", ident, max_calls=120, window_sec=60)
    if not allowed:
        return web.json_response({"ok": False, "rate_limited": True})

    async with get_db_session() as session:
        await record_web_analytics_event(
            session,
            event_type=event_type,
            web_user_id=user.id if user else None,
            meta_info=meta_info,
        )

    return web.json_response({"ok": True})


@routes.post("/api/anonymous/inbox/reply")
async def api_reply_anonymous_inbox(request: web.Request) -> web.Response:
    """Allow replying to a synced personal inbox message directly from the AUA Hub web interface."""
    user = await get_current_web_user(request)
    if not user or not user.telegram_user_id:
        return web.json_response({"error": "Your account must be synced with @aua_anonymous_bot first."}, status=400)

    allowed, retry = check_rate_limit("inbox_reply", str(user.id), max_calls=15, window_sec=60)
    if not allowed:
        return web.json_response({"error": f"Too many replies sent quickly. Please wait {retry}s."}, status=429)

    try:
        data = await request.json()
        message_id = int(data.get("message_id"))
        reply_text = (data.get("reply_text") or "").strip()[:1500]
    except Exception:
        return web.json_response({"error": "Invalid request payload."}, status=400)

    if not reply_text:
        return web.json_response({"error": "Reply text cannot be empty."}, status=400)

    link_err = scan_web_text_for_malicious_links(reply_text)
    if link_err:
        return web.json_response({"error": link_err}, status=400)

    async with get_db_session() as session:
        msg_obj = await session.get(InboxMessage, message_id)
        if not msg_obj or msg_obj.owner_id != user.telegram_user_id:
            return web.json_response({"error": "Inbox message not found."}, status=404)

        bot = request.app.get("bot")
        if bot:
            from app.keyboards.inline import get_inbox_reply_keyboard

            kb = get_inbox_reply_keyboard(thread_token=msg_obj.thread_token, to_owner=True)
            try:
                await bot.send_message(
                    chat_id=msg_obj.sender_id,
                    text=(
                        "💬 <b>Anonymous Inbox Reply Received!</b> 🎭\n"
                        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                        f"{html.escape(reply_text)}"
                    ),
                    reply_markup=kb,
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.warning("Failed to deliver inbox reply via Telegram: %s", e)
                return web.json_response(
                    {"error": "Could not deliver reply via Telegram. The recipient may have blocked the bot."},
                    status=502,
                )

        await mark_inbox_message_replied(
            session=session,
            thread_token=msg_obj.thread_token,
            reply_text=reply_text,
        )

    return web.json_response({"ok": True, "reply_text": reply_text})


# ------------------------------------------------------------------------------
# 5. Free Rooms Finder (<Free Rooms>)
# ------------------------------------------------------------------------------

@routes.get("/api/free-rooms")
async def api_get_free_rooms(request: web.Request) -> web.Response:
    ip = get_client_ip(request)

    # 1. Per-IP general rate limit (max 30 requests per minute)
    allowed, retry_after = check_rate_limit("ip_free_rooms", ip, max_calls=30, window_sec=60)
    if not allowed:
        return web.json_response(
            {"error": f"Too many requests for room schedules. Please wait {retry_after} seconds."},
            status=429,
            headers={"Retry-After": str(retry_after)},
        )

    date_param = (request.query.get("date") or "").strip() or None
    building_filter = (request.query.get("building") or "all").strip().lower()
    refresh = request.query.get("refresh") == "1"

    # 2. Strict Date Range Validation (allow from yesterday to 7 days ahead in Yerevan time)
    if date_param:
        try:
            target_dt = datetime.strptime(date_param, "%Y-%m-%d").date()
            today_yerevan = datetime.now(ZoneInfo("Asia/Yerevan")).date()
            min_date = today_yerevan - timedelta(days=1)
            max_date = today_yerevan + timedelta(days=7)
            if not (min_date <= target_dt <= max_date):
                return web.json_response(
                    {"error": f"Date must be between {min_date} and {max_date}."},
                    status=400,
                )
        except ValueError:
            return web.json_response({"error": "Invalid date format. Use YYYY-MM-DD."}, status=400)

    # 3. Dual-tier anti-exploit guard for forced refreshes:
    # - Per-IP cooldown: max 1 forced refresh per 30 seconds
    # - Global server cooldown: max 1 forced scrape per 30 seconds
    allow_forced = False
    if refresh:
        ip_refresh_ok, _ = check_rate_limit("ip_free_rooms_refresh", ip, max_calls=1, window_sec=30)
        global_refresh_ok, _ = check_rate_limit("global_scrape", "booking_rooms", max_calls=1, window_sec=30)
        allow_forced = bool(ip_refresh_ok and global_refresh_ok)

    try:
        schedule = await get_free_rooms_schedule(date_str=date_param, force_refresh=allow_forced)
    except Exception as e:
        logger.error("Error fetching free rooms schedule: %s", e)
        return web.json_response(
            {"error": "Unable to reach booking.aua.am right now. Please try again shortly."},
            status=502,
        )

    return web.json_response(
        {
            **schedule,
            "selected_building": building_filter,
            "refreshed_live": allow_forced,
        },
        headers={
            "Cache-Control": "private, max-age=60, stale-while-revalidate=600",
        },
    )


# ------------------------------------------------------------------------------
# 6. Telegram Groups Directory (<Telegram Groups>)
# ------------------------------------------------------------------------------

@routes.get("/api/groups")
async def api_list_groups(request: web.Request) -> web.Response:
    user = await get_current_web_user(request)
    q = (request.query.get("q") or "").strip()
    try:
        page = max(1, int(request.query.get("page", "1")))
        per_page = min(36, max(4, int(request.query.get("per_page", "9"))))
    except ValueError:
        page, per_page = 1, 9

    async with get_db_session() as session:
        groups, total, total_pages = await list_telegram_groups(
            session=session,
            search_query=q,
            page=page,
            per_page=per_page,
        )
        current_uid = user.id if user else None
        serialized = [serialize_group(g, current_user_id=current_uid) for g in groups]

    return web.json_response(
        {
            "groups": serialized,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
        }
    )


@routes.post("/api/groups")
async def api_create_group(request: web.Request) -> web.Response:
    user = await get_current_web_user(request)
    if not user:
        return web.json_response({"error": "Please sign in to post a Telegram group."}, status=401)

    allowed, retry = check_rate_limit("groups_write", str(user.id), max_calls=6, window_sec=60)
    if not allowed:
        return web.json_response({"error": f"Rate limit reached. Please wait {retry}s."}, status=429)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body."}, status=400)

    name = (data.get("name") or "").strip()
    invite_link = (data.get("invite_link") or "").strip()
    description = (data.get("description") or "").strip()
    image_url = (data.get("image_url") or "").strip()

    if len(name) < 2 or len(name) > 120:
        return web.json_response({"error": "Group name must be between 2 and 120 characters."}, status=400)

    if image_url and not is_safe_upload_url(image_url):
        return web.json_response(
            {"error": "Invalid group image URL. Please upload an image directly on AUA Hub."},
            status=400,
        )

    if invite_link.startswith("t.me/") or invite_link.startswith("telegram.me/"):
        invite_link = f"https://{invite_link}"

    if not re.match(r"^https?://(t\.me|telegram\.me)/[A-Za-z0-9_+/-]+$", invite_link, re.IGNORECASE):
        return web.json_response(
            {"error": "Please provide a valid Telegram invite link starting with https://t.me/..."},
            status=400,
        )

    link_err = scan_web_text_for_malicious_links(f"{name} {description}")
    if link_err:
        return web.json_response({"error": link_err}, status=400)

    async with get_db_session() as session:
        group = await create_telegram_group(
            session=session,
            web_user_id=user.id,
            name=name,
            invite_link=invite_link,
            description=description,
            image_url=image_url or None,
        )
        return web.json_response({"ok": True, "group": serialize_group(group, current_user_id=user.id)})


@routes.put("/api/groups/{group_id}")
async def api_update_group(request: web.Request) -> web.Response:
    user = await get_current_web_user(request)
    if not user:
        return web.json_response({"error": "Authentication required."}, status=401)

    allowed, retry = check_rate_limit("groups_write", str(user.id), max_calls=10, window_sec=60)
    if not allowed:
        return web.json_response({"error": f"Rate limit reached. Please wait {retry}s."}, status=429)

    try:
        group_id = int(request.match_info["group_id"])
    except ValueError:
        return web.json_response({"error": "Invalid group ID."}, status=400)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body."}, status=400)

    name = (data.get("name") or "").strip()
    invite_link = (data.get("invite_link") or "").strip()
    description = (data.get("description") or "").strip()
    image_url = (data.get("image_url") or "").strip()

    if len(name) < 2 or len(name) > 120:
        return web.json_response({"error": "Group name must be between 2 and 120 characters."}, status=400)

    if image_url and not is_safe_upload_url(image_url):
        return web.json_response(
            {"error": "Invalid group image URL. Please upload an image directly on AUA Hub."},
            status=400,
        )

    if invite_link.startswith("t.me/") or invite_link.startswith("telegram.me/"):
        invite_link = f"https://{invite_link}"

    if not re.match(r"^https?://(t\.me|telegram\.me)/[A-Za-z0-9_+/-]+$", invite_link, re.IGNORECASE):
        return web.json_response(
            {"error": "Please provide a valid Telegram invite link starting with https://t.me/..."},
            status=400,
        )

    link_err = scan_web_text_for_malicious_links(f"{name} {description}")
    if link_err:
        return web.json_response({"error": link_err}, status=400)

    async with get_db_session() as session:
        group = await update_telegram_group(
            session=session,
            group_id=group_id,
            web_user_id=user.id,
            name=name,
            invite_link=invite_link,
            description=description,
            image_url=image_url or None,
        )
        if not group:
            return web.json_response({"error": "Group not found or not owned by you."}, status=404)

        return web.json_response({"ok": True, "group": serialize_group(group, current_user_id=user.id)})


@routes.delete("/api/groups/{group_id}")
async def api_delete_group(request: web.Request) -> web.Response:
    user = await get_current_web_user(request)
    if not user:
        return web.json_response({"error": "Authentication required."}, status=401)

    try:
        group_id = int(request.match_info["group_id"])
    except ValueError:
        return web.json_response({"error": "Invalid group ID."}, status=400)

    async with get_db_session() as session:
        deleted = await delete_telegram_group(session, group_id=group_id, web_user_id=user.id)
        if not deleted:
            return web.json_response({"error": "Group not found or not owned by you."}, status=404)

    return web.json_response({"ok": True})


# ------------------------------------------------------------------------------
# 7. Campus Chat & Unread Counter (<Chat>)
# ------------------------------------------------------------------------------

@routes.get("/api/spam-chat/messages")
@routes.get("/api/chat/messages")
async def api_list_spam_chat(request: web.Request) -> web.Response:
    user = await get_current_web_user(request)
    after_id_str = request.query.get("after_id")
    after_id = int(after_id_str) if (after_id_str and after_id_str.isdigit()) else None

    async with get_db_session() as session:
        messages = await list_spam_chat_messages(session, limit=80, after_id=after_id)
        serialized = [serialize_spam_message(m) for m in messages]
        unread_count = await get_spam_chat_unread_count(session, user.id) if user else 0

    return web.json_response(
        {
            "messages": serialized,
            "unread_count": unread_count,
        }
    )


@routes.post("/api/spam-chat/messages")
@routes.post("/api/chat/messages")
async def api_post_spam_chat(request: web.Request) -> web.Response:
    user = await get_current_web_user(request)
    if not user:
        return web.json_response({"error": "Please sign in to send messages in Campus Chat."}, status=401)

    allowed, retry = check_rate_limit("spam_chat_send", str(user.id), max_calls=15, window_sec=60)
    if not allowed:
        return web.json_response({"error": f"You are sending messages too fast. Please wait {retry}s."}, status=429)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body."}, status=400)

    raw_text = (data.get("content") or "").strip()
    image_url = (data.get("image_url") or "").strip() or None
    media_type = (data.get("media_type") or "").strip().lower() or None
    reply_to_id = data.get("reply_to_id")
    if reply_to_id is not None:
        try:
            reply_to_id = int(reply_to_id)
            if reply_to_id <= 0:
                reply_to_id = None
        except (ValueError, TypeError):
            reply_to_id = None

    if not raw_text and not image_url:
        return web.json_response({"error": "Please enter a message or attach a media file."}, status=400)

    # Anti-flood duplicate message protection
    now = time.monotonic()
    if user.id in _LAST_USER_MESSAGES:
        last_txt, last_ts = _LAST_USER_MESSAGES[user.id]
        if last_txt and last_txt == raw_text and (now - last_ts) < 4.0:
            return web.json_response(
                {"error": "Duplicate message detected. Please wait a moment before sending again."},
                status=429,
            )

    if image_url:
        if not is_safe_upload_url(image_url):
            return web.json_response(
                {"error": "Invalid attachment URL. Please upload your media directly on AUA Hub."},
                status=400,
            )
        if not media_type:
            ext = Path(image_url).suffix.lower()
            if ext in (".mp4", ".webm", ".mov"):
                media_type = "video"
            elif ext == ".gif":
                media_type = "gif"
            else:
                media_type = "image"

    content_html = ""
    if raw_text:
        content_html, extracted_urls = sanitize_and_render_chat_html(raw_text)
        for link in extracted_urls:
            link_err = scan_web_text_for_malicious_links(link)
            if link_err:
                return web.json_response({"error": link_err}, status=400)
        link_err = scan_web_text_for_malicious_links(raw_text)
        if link_err:
            return web.json_response({"error": link_err}, status=400)

    _LAST_USER_MESSAGES[user.id] = (raw_text, now)

    async with get_db_session() as session:
        msg = await create_spam_chat_message(
            session=session,
            web_user_id=user.id,
            content_html=content_html,
            image_url=image_url,
            media_type=media_type,
            reply_to_id=reply_to_id,
        )
        serialized = serialize_spam_message(msg)

        if reply_to_id:
            parent_stmt = select(SpamChatMessage).where(SpamChatMessage.id == reply_to_id)
            parent_msg = (await session.execute(parent_stmt)).scalar_one_or_none()
            if parent_msg and parent_msg.web_user_id != user.id:
                reply_snippet = clean_chat_snippet(content_html) or "New reply"
                notif = await create_user_notification(
                    session=session,
                    recipient_id=parent_msg.web_user_id,
                    sender_id=user.id,
                    category="chat_reply",
                    title=f"{user.name} replied to you",
                    content=reply_snippet,
                    target_type="chat_message",
                    target_id=str(msg.id),
                    extra_data={
                        "message_id": msg.id,
                        "sender_name": user.name,
                        "sender_avatar": user.avatar_url or "",
                    },
                )
                unread_cnt = await get_unread_notification_count(session, parent_msg.web_user_id)
                await broadcast_ws_event({
                    "type": "new_notification",
                    "recipient_id": parent_msg.web_user_id,
                    "notification": serialize_user_notification(notif),
                    "unread_count": unread_cnt,
                })

    # Broadcast to all active WebSocket connections
    await broadcast_ws_event({"type": "new_message", "message": serialized})

    return web.json_response({"ok": True, "message": serialized})


@routes.post("/api/spam-chat/messages/{message_id:\\d+}/react")
@routes.post("/api/chat/messages/{message_id:\\d+}/react")
async def api_react_spam_chat(request: web.Request) -> web.Response:
    user = await get_current_web_user(request)
    if not user:
        return web.json_response({"error": "Please sign in to react to messages."}, status=401)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body."}, status=400)

    try:
        message_id = int(request.match_info["message_id"])
    except (KeyError, ValueError):
        return web.json_response({"error": "Invalid message ID."}, status=400)

    emoji = (data.get("emoji") or "").strip()
    if emoji not in ALLOWED_REACTIONS:
        return web.json_response({"error": "Unsupported reaction emoji."}, status=400)

    async with get_db_session() as session:
        active_emoji, reactions_list = await toggle_spam_chat_reaction(
            session=session,
            message_id=message_id,
            web_user_id=user.id,
            emoji=emoji,
        )

        if active_emoji:
            stmt = select(SpamChatMessage).where(SpamChatMessage.id == message_id)
            target_msg = (await session.execute(stmt)).scalar_one_or_none()
            if target_msg and target_msg.web_user_id != user.id:
                msg_snippet = clean_chat_snippet(target_msg.content_html) or "Your message"
                notif = await create_user_notification(
                    session=session,
                    recipient_id=target_msg.web_user_id,
                    sender_id=user.id,
                    category="chat_reaction",
                    title=f"{user.name} reacted {emoji}",
                    content=f'"{msg_snippet}"',
                    target_type="chat_message",
                    target_id=str(target_msg.id),
                    extra_data={
                        "message_id": target_msg.id,
                        "emoji": emoji,
                        "sender_name": user.name,
                        "sender_avatar": user.avatar_url or "",
                    },
                )
                unread_cnt = await get_unread_notification_count(session, target_msg.web_user_id)
                await broadcast_ws_event({
                    "type": "new_notification",
                    "recipient_id": target_msg.web_user_id,
                    "notification": serialize_user_notification(notif),
                    "unread_count": unread_cnt,
                })

    # Broadcast reaction event via WebSockets
    await broadcast_ws_event({
        "type": "message_reaction",
        "message_id": message_id,
        "reactions": reactions_list,
    })

    return web.json_response({
        "ok": True,
        "message_id": message_id,
        "active_emoji": active_emoji,
        "reactions": reactions_list,
    })


@routes.get("/api/spam-chat/messages/{message_id:\\d+}/reactions")
@routes.get("/api/chat/messages/{message_id:\\d+}/reactions")
async def api_get_spam_chat_reactions(request: web.Request) -> web.Response:
    try:
        message_id = int(request.match_info["message_id"])
    except (KeyError, ValueError):
        return web.json_response({"error": "Invalid message ID."}, status=400)

    async with get_db_session() as session:
        reactions_list = await get_message_reactions(session, message_id)

    return web.json_response({
        "ok": True,
        "message_id": message_id,
        "reactions": reactions_list,
    })


@routes.put("/api/spam-chat/messages/{message_id:\\d+}")
@routes.put("/api/chat/messages/{message_id:\\d+}")
async def api_edit_spam_chat(request: web.Request) -> web.Response:
    user = await get_current_web_user(request)
    if not user:
        return web.json_response({"error": "Please sign in to edit your messages."}, status=401)

    try:
        message_id = int(request.match_info["message_id"])
    except (KeyError, ValueError):
        return web.json_response({"error": "Invalid message ID."}, status=400)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body."}, status=400)

    raw_text = (data.get("content") or "").strip()
    if not raw_text:
        return web.json_response({"error": "Message content cannot be empty."}, status=400)

    content_html, extracted_urls = sanitize_and_render_chat_html(raw_text)
    for link in extracted_urls:
        link_err = scan_web_text_for_malicious_links(link)
        if link_err:
            return web.json_response({"error": link_err}, status=400)
    link_err = scan_web_text_for_malicious_links(raw_text)
    if link_err:
        return web.json_response({"error": link_err}, status=400)

    async with get_db_session() as session:
        msg = await edit_spam_chat_message(
            session=session,
            message_id=message_id,
            web_user_id=user.id,
            new_content_html=content_html,
        )
        if not msg:
            return web.json_response({"error": "Message not found or you do not have permission to edit it."}, status=403)
        serialized = serialize_spam_message(msg)

    # Broadcast edit to all WebSocket clients
    ws_clients: Set[web.WebSocketResponse] = request.app.get("ws_clients", set())
    if ws_clients:
        payload_str = json.dumps({"type": "message_edit", "message": serialized})
        dead = []
        for ws in list(ws_clients):
            try:
                await ws.send_str(payload_str)
            except Exception:
                dead.append(ws)
        for ws in dead:
            ws_clients.discard(ws)

    return web.json_response({"ok": True, "message": serialized})


@routes.delete("/api/spam-chat/messages/{message_id:\\d+}")
@routes.delete("/api/chat/messages/{message_id:\\d+}")
async def api_delete_spam_chat(request: web.Request) -> web.Response:
    user = await get_current_web_user(request)
    if not user:
        return web.json_response({"error": "Please sign in to delete messages."}, status=401)

    try:
        message_id = int(request.match_info["message_id"])
    except (KeyError, ValueError):
        return web.json_response({"error": "Invalid message ID."}, status=400)

    is_admin = False
    if user.telegram_user_id and user.telegram_user_id in settings.admin_ids:
        is_admin = True

    async with get_db_session() as session:
        ok = await delete_spam_chat_message(
            session=session,
            message_id=message_id,
            web_user_id=user.id,
            is_admin=is_admin,
        )
        if not ok:
            return web.json_response({"error": "Message not found or you do not have permission to delete it."}, status=403)

    # Broadcast delete to all WebSocket clients
    ws_clients: Set[web.WebSocketResponse] = request.app.get("ws_clients", set())
    if ws_clients:
        payload_str = json.dumps({"type": "message_delete", "message_id": message_id})
        dead = []
        for ws in list(ws_clients):
            try:
                await ws.send_str(payload_str)
            except Exception:
                dead.append(ws)
        for ws in dead:
            ws_clients.discard(ws)

    return web.json_response({"ok": True, "message_id": message_id})


@routes.get("/api/spam-chat/unread")
@routes.get("/api/chat/unread")
async def api_get_spam_chat_unread(request: web.Request) -> web.Response:
    user = await get_current_web_user(request)
    async with get_db_session() as session:
        max_id = int((await session.execute(select(func.max(SpamChatMessage.id)))).scalar_one_or_none() or 0)
        if not user:
            return web.json_response({"unread_count": 0, "latest_id": max_id})
        unread = await get_spam_chat_unread_count(session, user.id)
        return web.json_response({"unread_count": unread, "latest_id": max_id})


@routes.post("/api/spam-chat/read")
@routes.post("/api/chat/read")
async def api_mark_spam_chat_read(request: web.Request) -> web.Response:
    user = await get_current_web_user(request)
    if not user:
        return web.json_response({"ok": True, "unread_count": 0})

    latest_id: Optional[int] = None
    try:
        data = await request.json()
        if data and data.get("latest_message_id") is not None:
            latest_id = int(data["latest_message_id"])
    except Exception:
        pass

    async with get_db_session() as session:
        marked_id = await mark_spam_chat_read(session, web_user_id=user.id, latest_message_id=latest_id)

    return web.json_response({"ok": True, "last_read_message_id": marked_id, "unread_count": 0})


@routes.get("/ws/spam-chat")
@routes.get("/ws/chat")
async def ws_spam_chat(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=30.0)
    await ws.prepare(request)

    token = request.cookies.get("aua_hub_token") or request.query.get("token")
    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()

    _ACTIVE_WS_CLIENTS.add(ws)
    _ACTIVE_WS_USERS[ws] = decode_jwt_token(token)

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                if msg.data == "ping":
                    await ws.send_str(json.dumps({"type": "pong"}))
                elif msg.data and msg.data.startswith("{"):
                    try:
                        payload = json.loads(msg.data)
                        if payload.get("type") == "auth":
                            _ACTIVE_WS_USERS[ws] = decode_jwt_token(payload.get("token"))
                    except Exception:
                        pass
            elif msg.type == WSMsgType.ERROR:
                break
    finally:
        _ACTIVE_WS_CLIENTS.discard(ws)
        _ACTIVE_WS_USERS.pop(ws, None)

    return ws


# ------------------------------------------------------------------------------
# 7b. Real-Time In-App Notifications
# ------------------------------------------------------------------------------

@routes.get("/api/notifications")
async def api_get_notifications(request: web.Request) -> web.Response:
    user = await get_current_web_user(request)
    if not user:
        return web.json_response({"error": "Authentication required."}, status=401)

    async with get_db_session() as session:
        notifs = await list_user_notifications(session, recipient_id=user.id, limit=50)
        unread_count = await get_unread_notification_count(session, recipient_id=user.id)
        serialized = [serialize_user_notification(n) for n in notifs]

    return web.json_response({
        "ok": True,
        "notifications": serialized,
        "unread_count": unread_count,
    })


@routes.post("/api/notifications/{notification_id:\\d+}/read")
async def api_mark_notification_read(request: web.Request) -> web.Response:
    user = await get_current_web_user(request)
    if not user:
        return web.json_response({"error": "Authentication required."}, status=401)

    try:
        notif_id = int(request.match_info["notification_id"])
    except (KeyError, ValueError):
        return web.json_response({"error": "Invalid notification ID."}, status=400)

    async with get_db_session() as session:
        ok = await mark_notification_read(session, notification_id=notif_id, recipient_id=user.id)
        unread_count = await get_unread_notification_count(session, recipient_id=user.id)

    return web.json_response({"ok": ok, "unread_count": unread_count})


@routes.post("/api/notifications/read-all")
async def api_mark_all_notifications_read(request: web.Request) -> web.Response:
    user = await get_current_web_user(request)
    if not user:
        return web.json_response({"error": "Authentication required."}, status=401)

    async with get_db_session() as session:
        marked_count = await mark_all_notifications_read(session, recipient_id=user.id)

    return web.json_response({"ok": True, "marked_count": marked_count, "unread_count": 0})


# ------------------------------------------------------------------------------
# 8. Academic Feedbacks (<Feedbacks>: Instructors & Courses)
# ------------------------------------------------------------------------------

@routes.get("/api/feedbacks/sync-status")
async def api_get_feedbacks_sync_status(request: web.Request) -> web.Response:
    return web.json_response(get_jenzareviews_sync_status())


@routes.get("/api/feedbacks/catalog")
async def api_get_feedbacks_catalog(request: web.Request) -> web.Response:
    async with get_db_session() as session:
        sem_label = await get_active_semester_label(session)
        if not sem_label:
            try:
                await sync_courses_from_sonis(session)
            except Exception as e:
                logger.warning("Course sync on /api/feedbacks/catalog failed: %s", e)

        catalog = await get_feedback_catalog(session)
    return web.json_response(catalog)


@routes.get("/api/feedbacks")
async def api_list_feedbacks(request: web.Request) -> web.Response:
    user = await get_current_web_user(request)
    target_type = (request.query.get("type") or "instructor").strip().lower()
    if target_type not in ("instructor", "course"):
        target_type = "instructor"

    q = (request.query.get("q") or "").strip()
    target_key = (request.query.get("target_key") or "").strip() or None
    mine = request.query.get("mine") == "1"
    only_uid = user.id if (mine and user) else None

    async with get_db_session() as session:
        items, summary = await list_academic_feedbacks(
            session=session,
            target_type=target_type,
            search_query=q,
            target_key=target_key,
            only_user_id=only_uid,
            limit=120,
        )
        current_uid = user.id if user else None
        serialized = [serialize_academic_feedback(fb, current_user_id=current_uid) for fb in items]

    return web.json_response(
        {
            "target_type": target_type,
            "target_key": target_key,
            "summary": summary,
            "feedbacks": serialized,
        }
    )


@routes.post("/api/feedbacks")
async def api_create_feedback(request: web.Request) -> web.Response:
    user = await get_current_web_user(request)
    if not user:
        return web.json_response({"error": "Please sign in with your AUA account to leave feedback."}, status=401)

    allowed, retry = check_rate_limit("feedback_write", str(user.id), max_calls=10, window_sec=60)
    if not allowed:
        return web.json_response({"error": f"Rate limit reached. Please wait {retry}s."}, status=429)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body."}, status=400)

    target_type = (data.get("target_type") or "instructor").strip().lower()
    if target_type not in ("instructor", "course"):
        return web.json_response({"error": "Invalid feedback target type."}, status=400)

    target_key = (data.get("target_key") or "").strip()
    target_title = (data.get("target_title") or target_key).strip()
    target_subtitle = (data.get("target_subtitle") or "").strip()
    comment = (data.get("comment") or "").strip()
    is_anonymous = bool(data.get("is_anonymous", True))
    course_name = (data.get("course_name") or data.get("course") or "").strip() or None
    wta_raw = data.get("would_take_again")
    would_take_again = bool(wta_raw) if wta_raw is not None else None

    try:
        rating = int(data.get("rating", 5))
    except (TypeError, ValueError):
        rating = 5

    if not target_key:
        label = "an instructor" if target_type == "instructor" else "a course"
        return web.json_response({"error": f"Please select {label} from the list first."}, status=400)

    if len(comment) < 5:
        return web.json_response({"error": "Please write at least 5 characters in your feedback."}, status=400)

    link_err = scan_web_text_for_malicious_links(comment)
    if link_err:
        return web.json_response({"error": link_err}, status=400)

    async with get_db_session() as session:
        fb = await create_academic_feedback(
            session=session,
            web_user_id=user.id,
            target_type=target_type,
            target_key=target_key,
            target_title=target_title,
            target_subtitle=target_subtitle,
            rating=rating,
            comment=comment,
            is_anonymous=is_anonymous,
            course_name=course_name,
            would_take_again=would_take_again,
        )
        return web.json_response(
            {
                "ok": True,
                "feedback": serialize_academic_feedback(fb, current_user_id=user.id),
            }
        )


@routes.delete("/api/feedbacks/{feedback_id}")
async def api_delete_feedback(request: web.Request) -> web.Response:
    user = await get_current_web_user(request)
    if not user:
        return web.json_response({"error": "Authentication required."}, status=401)

    try:
        feedback_id = int(request.match_info["feedback_id"])
    except ValueError:
        return web.json_response({"error": "Invalid feedback ID."}, status=400)

    async with get_db_session() as session:
        deleted = await delete_academic_feedback(session, feedback_id=feedback_id, web_user_id=user.id)
        if not deleted:
            return web.json_response({"error": "Feedback not found or not owned by you."}, status=404)

    return web.json_response({"ok": True})


# ------------------------------------------------------------------------------
# 8.5. AI Assistant (Multi-Source RAG with 5-Prompt Session Limit)
# ------------------------------------------------------------------------------

@routes.get("/api/ai/session")
@routes.post("/api/ai/session")
async def api_ai_session(request: web.Request) -> web.Response:
    user = await get_current_web_user(request)
    session_id = (request.query.get("session_id") or "").strip() or None
    client_history = None

    if request.method == "POST":
        try:
            body = await request.json()
            if isinstance(body, dict):
                session_id = (body.get("session_id") or session_id or "").strip() or None
                if isinstance(body.get("client_history"), list):
                    client_history = body.get("client_history")
        except Exception:
            pass

    if not user and not session_id and not client_history:
        return web.json_response(
            {
                "ok": True,
                "session_id": "",
                "prompts_used": 0,
                "max_prompts": MAX_PROMPTS_PER_SESSION,
                "limit_reached": False,
                "history": [],
            }
        )

    async with get_db_session() as session:
        chat_state, was_updated = await load_or_sync_ai_session(
            db_session=session,
            session_id=session_id,
            web_user_id=user.id if user else None,
            client_history=client_history,
        )
        history = chat_state.get_history_payload()

    payload = {
        "ok": True,
        "session_id": chat_state.session_id,
        "prompts_used": chat_state.prompts_used,
        "max_prompts": MAX_PROMPTS_PER_SESSION,
        "limit_reached": chat_state.prompts_used >= MAX_PROMPTS_PER_SESSION,
        "history": history,
    }

    if user and was_updated:
        await broadcast_ws_event(
            {
                "type": "ai_session_updated",
                "recipient_id": user.id,
                **payload,
            }
        )

    return web.json_response(payload)


@routes.post("/api/ai/chat")
async def api_ai_chat(request: web.Request) -> web.Response:
    user = await get_current_web_user(request)
    rate_key = f"user_{user.id}" if user else f"ip_{get_client_ip(request)}"
    allowed, retry = check_rate_limit("ai_chat", rate_key, max_calls=15, window_sec=60)
    if not allowed:
        return web.json_response(
            {"error": f"Too many AI requests. Please wait {retry}s."},
            status=429,
        )

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body."}, status=400)

    prompt = (data.get("prompt") or "").strip()
    session_id = (data.get("session_id") or "").strip() or None
    reset = bool(data.get("reset", False))
    client_history = data.get("client_history")

    async with get_db_session() as session:
        if reset:
            chat_state = await reset_ai_session_persistent(
                db_session=session,
                old_session_id=session_id,
                web_user_id=user.id if user else None,
            )
        else:
            chat_state, _ = await load_or_sync_ai_session(
                db_session=session,
                session_id=session_id,
                web_user_id=user.id if user else None,
                client_history=client_history if isinstance(client_history, list) else None,
            )

        if chat_state.prompts_used >= MAX_PROMPTS_PER_SESSION:
            return web.json_response(
                {
                    "ok": False,
                    "limit_reached": True,
                    "session_id": chat_state.session_id,
                    "prompts_used": chat_state.prompts_used,
                    "max_prompts": MAX_PROMPTS_PER_SESSION,
                    "history": chat_state.get_history_payload(),
                    "error": (
                        "You've reached the 5-prompt limit for this session. "
                        "Please start a new chat to continue."
                    ),
                },
                status=429,
            )

        if not prompt:
            return web.json_response({"error": "Please enter a prompt question."}, status=400)

        if len(prompt) > 1200:
            return web.json_response(
                {"error": "Prompt is too long (maximum 1,200 characters)."},
                status=400,
            )

        result = await ai_orchestrator.answer_prompt(
            db_session=session,
            user_prompt=prompt,
            chat_state=chat_state,
            web_user_id=user.id if user else None,
        )

    if user:
        await broadcast_ws_event(
            {
                "type": "ai_session_updated",
                "recipient_id": user.id,
                "session_id": result["session_id"],
                "prompts_used": result["prompts_used"],
                "max_prompts": result["max_prompts"],
                "limit_reached": result["limit_reached"],
                "history": result["history"],
            }
        )

    return web.json_response(result)


@routes.post("/api/ai/reset")
async def api_ai_reset(request: web.Request) -> web.Response:
    user = await get_current_web_user(request)
    try:
        data = await request.json()
    except Exception:
        data = {}

    old_sid = (data.get("session_id") or "").strip() or None
    async with get_db_session() as session:
        fresh_state = await reset_ai_session_persistent(
            db_session=session,
            old_session_id=old_sid,
            web_user_id=user.id if user else None,
        )

    payload = {
        "ok": True,
        "session_id": fresh_state.session_id,
        "prompts_used": 0,
        "max_prompts": MAX_PROMPTS_PER_SESSION,
        "limit_reached": False,
        "history": [],
    }

    if user:
        await broadcast_ws_event(
            {
                "type": "ai_session_updated",
                "recipient_id": user.id,
                **payload,
            }
        )

    return web.json_response(payload)


# ------------------------------------------------------------------------------
# 8B. Admin Analytics Dashboard (/stata) & Telemetry Worker
# ------------------------------------------------------------------------------

_STATA_ALLOWED_ASSETS: Dict[str, str] = {
    "stata.css": "text/css; charset=utf-8",
    "stata.js": "application/javascript; charset=utf-8",
}


@routes.get("/api/admin/stata")
async def api_admin_stata(request: web.Request) -> web.Response:
    user = await get_current_web_user(request)
    if not user:
        return web.json_response({"error": "Authentication required."}, status=401)
    if not is_stata_admin(user):
        return web.json_response(
            {"error": "Forbidden: administrator telemetry is restricted."},
            status=403,
        )

    async with get_db_session() as session:
        payload = await get_full_admin_stata_payload(
            session,
            live_ws_count=len(_ACTIVE_WS_CLIENTS),
            jenzareviews_sync_status=get_jenzareviews_sync_status(),
        )

    return web.json_response(payload)


@routes.get("/stata")
@routes.get("/stata/")
async def serve_admin_stata(request: web.Request) -> web.Response:
    user = await get_current_web_user(request)
    if not user:
        return web.FileResponse(TEMPLATES_DIR / "stata_gate.html")
    if not is_stata_admin(user):
        denied_html = (TEMPLATES_DIR / "stata_denied.html").read_text(encoding="utf-8")
        return web.Response(text=denied_html, content_type="text/html", charset="utf-8", status=403)
    return web.FileResponse(TEMPLATES_DIR / "stata.html")


@routes.get("/stata/assets/{asset_name}")
async def serve_admin_stata_asset(request: web.Request) -> web.Response:
    user = await get_current_web_user(request)
    if not user:
        return web.json_response({"error": "Authentication required."}, status=401)
    if not is_stata_admin(user):
        return web.json_response({"error": "Forbidden."}, status=403)

    asset_name = request.match_info.get("asset_name", "")
    content_type = _STATA_ALLOWED_ASSETS.get(asset_name)
    if not content_type:
        return web.json_response({"error": "Asset not found."}, status=404)

    asset_path = TEMPLATES_DIR / asset_name
    if not asset_path.is_file():
        return web.json_response({"error": "Asset not found."}, status=404)

    return web.FileResponse(asset_path, headers={"Content-Type": content_type})


async def bot_activity_snapshot_worker() -> None:
    """Capture live anonymous-chat concurrency and WebSocket snapshots every 60 seconds."""
    while True:
        try:
            async with get_db_session() as session:
                await record_bot_activity_snapshot(
                    session,
                    web_ws_clients=len(_ACTIVE_WS_CLIENTS),
                )
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning("bot_activity_snapshot_worker error: %s", exc)
        await asyncio.sleep(60)


# ------------------------------------------------------------------------------
# 9. Static Assets, Uploads & SPA Fallback
# ------------------------------------------------------------------------------

@routes.get("/")
async def serve_spa_index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC_DIR / "index.html")


@routes.get("/manifest.json")
async def serve_pwa_manifest(request: web.Request) -> web.FileResponse:
    return web.FileResponse(
        STATIC_DIR / "manifest.json",
        headers={"Content-Type": "application/manifest+json; charset=utf-8"},
    )


@routes.get("/sw.js")
async def serve_service_worker(request: web.Request) -> web.FileResponse:
    return web.FileResponse(
        STATIC_DIR / "sw.js",
        headers={
            "Content-Type": "application/javascript; charset=utf-8",
            "Service-Worker-Allowed": "/",
            "Cache-Control": "no-cache",
        },
    )


def create_web_app(bot: Any = None) -> web.Application:
    """Create and configure the AUA Hub aiohttp.web Application."""
    app = web.Application(
        client_max_size=10 * 1024 * 1024,
        middlewares=[security_middleware],
    )
    app["bot"] = bot
    app["ws_clients"] = _ACTIVE_WS_CLIENTS

    app.add_routes(routes)
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    app.router.add_static("/static/", path=STATIC_DIR, name="static")
    app.router.add_static("/uploads/", path=UPLOADS_DIR, name="uploads")
    return app


async def start_background_web_server(bot: Any = None, host: str = "0.0.0.0", port: Optional[int] = None) -> web.AppRunner:
    """Start the AUA Hub web server inside the running asyncio event loop."""
    listen_port = port or getattr(settings, "WEB_PORT", 8000)
    app = create_web_app(bot=bot)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=listen_port)
    await site.start()
    logger.info("AUA Hub Web Server listening on http://%s:%d", host, listen_port)

    # Trigger non-blocking background workers: course sync, free rooms pre-warmer, jenzareviews sync, and telemetry snapshotter
    asyncio.create_task(_initial_course_warmup())
    asyncio.create_task(free_rooms_prewarm_worker())
    asyncio.create_task(jenzareviews_sync_worker())
    asyncio.create_task(bot_activity_snapshot_worker())
    return runner


async def _initial_course_warmup() -> None:
    """Ensure courses table is populated from Sonis on startup."""
    try:
        async with get_db_session() as session:
            sem = await get_active_semester_label(session)
            if not sem:
                label, count = await sync_courses_from_sonis(session)
                logger.info("Initial Sonis warmup populated %d courses for %s.", count, label)
    except Exception as e:
        logger.warning("Initial Sonis course warmup skipped: %s", e)


if __name__ == "__main__":
    async def _standalone() -> None:
        await init_db()
        await start_background_web_server()
        await asyncio.Event().wait()

    asyncio.run(_standalone())
