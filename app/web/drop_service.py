"""Hardened Zero-JS, Zero-Cookie, Zero-Logging Anonymous Post Dropbox Service.

Serves internal endpoints (`GET /_drop/`, `POST /_drop/submit`, `GET /_drop/healthz`)
behind `drop_proxy` (`drop.auahub.am` via Cloudflare Tunnel and Tor v3 `.onion` hidden service).
"""

import asyncio
import base64
from collections import deque
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import html
import io
import json
import logging
import math
import random
import re
import secrets
import time
from typing import Any, Dict, Literal, Optional, Tuple
from urllib.parse import parse_qs

from aiohttp import web
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from pydantic import BaseModel
from sqlalchemy import delete, select

try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None
    types = None

from app.config import settings
from app.database.models import AnonymousDropQueue
from app.database.session import get_db_session
from app.database.web_crud import create_anonymous_public_post

logger = logging.getLogger("aua_hub_drop")

# ------------------------------------------------------------------------------
# Secrets & Startup Fallbacks
# ------------------------------------------------------------------------------

_EPHEMERAL_CAPTCHA_SECRET: str = secrets.token_hex(32)
_WARNED_CAPTCHA_SECRET: bool = False
_WARNED_PROXY_SECRET: bool = False

_PLACEHOLDER_SECRETS = {
    "",
    "change_this_to_a_secure_random_captcha_hmac_secret",
    "change_this_to_a_secure_random_proxy_shared_secret",
    "change_me",
    "changeme",
}

# Unambiguous 6-char CAPTCHA alphabet (excludes 0, O, Q, D, 1, I, l, 8, B)
CAPTCHA_ALPHABET = "2345679ACEFGHJKLMNPRSTUVWXYZ"
CAPTCHA_LENGTH = 6
CAPTCHA_TTL_SECONDS = 300

# Content & Global Circuit Breaker Limits (IP-free for Tor compatibility)
MIN_POST_LENGTH = 20
MAX_POST_LENGTH = 2000
MAX_BODY_BYTES = 16 * 1024  # 16 KB

GLOBAL_MAX_ACCEPTED_PER_HOUR = 20
GLOBAL_MAX_ACCEPTED_PER_DAY = 60
GLOBAL_MAX_GEMINI_CALLS_PER_MINUTE = 15
DUPLICATE_TEXT_TTL_SECONDS = 6 * 3600  # 6 hours

PUBLISH_DELAY_MIN_SECONDS = 120  # 2 minutes
PUBLISH_DELAY_MAX_SECONDS = 480  # 8 minutes
QUEUE_POLL_INTERVAL_SECONDS = 30


def get_captcha_secret() -> bytes:
    """Return the configured CAPTCHA HMAC secret, falling back to an ephemeral CSPRNG key with a warning."""
    global _WARNED_CAPTCHA_SECRET
    raw = (getattr(settings, "DROP_CAPTCHA_SECRET", None) or "").strip()
    if not raw or raw in _PLACEHOLDER_SECRETS:
        if not _WARNED_CAPTCHA_SECRET:
            logger.warning(
                "DROP_CAPTCHA_SECRET is not set; using ephemeral CSPRNG key for stateless CAPTCHA signing."
            )
            _WARNED_CAPTCHA_SECRET = True
        return _EPHEMERAL_CAPTCHA_SECRET.encode("utf-8")
    return raw.encode("utf-8")


def get_internal_proxy_secret() -> Optional[str]:
    """Return the configured internal proxy shared secret, logging a warning if unset."""
    global _WARNED_PROXY_SECRET
    raw = (getattr(settings, "DROP_INTERNAL_PROXY_SECRET", None) or "").strip()
    if not raw or raw in _PLACEHOLDER_SECRETS:
        if not _WARNED_PROXY_SECRET:
            logger.warning(
                "DROP_INTERNAL_PROXY_SECRET is not set or uses placeholder; /_drop/* routes will fail closed (404)."
            )
            _WARNED_PROXY_SECRET = True
        return None
    return raw


def get_onion_hostname() -> Optional[str]:
    """Return validated `.onion` hostname if configured in settings."""
    raw = (getattr(settings, "DROP_ONION_HOSTNAME", None) or "").strip().lower()
    if not raw:
        return None
    # Strip scheme/path if accidentally included
    raw = re.sub(r"^https?://", "", raw).split("/")[0].strip()
    if re.fullmatch(r"[a-z2-7]{16,56}\.onion", raw):
        return raw
    return None


def verify_proxy_secret(request: web.Request) -> bool:
    """Constant-time check of X-Drop-Proxy-Secret header against DROP_INTERNAL_PROXY_SECRET."""
    expected = get_internal_proxy_secret()
    if not expected:
        return False
    provided = request.headers.get("X-Drop-Proxy-Secret", "")
    if not provided:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


# ------------------------------------------------------------------------------
# Security Headers for All Drop Responses (Zero-JS, Zero-Cookie)
# ------------------------------------------------------------------------------

DROP_SECURITY_HEADERS: Dict[str, str] = {
    "Content-Security-Policy": (
        "default-src 'none'; "
        "style-src 'unsafe-inline'; "
        "img-src data:; "
        "form-action 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'none';"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), interest-cohort=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cache-Control": "no-store, no-cache, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}


def build_drop_html_response(html_body: str, status: int = 200) -> web.Response:
    """Create an HTML response with strict zero-JS, zero-cookie security headers."""
    resp = web.Response(
        text=html_body,
        status=status,
        content_type="text/html",
        charset="utf-8",
        headers=DROP_SECURITY_HEADERS,
    )
    return resp


# ------------------------------------------------------------------------------
# In-Memory Anti-Replay, Duplicate Filter & Global Circuit Breaker (IP-Free)
# ------------------------------------------------------------------------------

class DropRateAndReplayState:
    """IP-free in-memory state for CAPTCHA replay prevention, duplicate filtering, and global rate caps."""

    def __init__(self) -> None:
        # nonce -> expiry monotonic timestamp
        self.spent_nonces: Dict[str, float] = {}
        # sha256(normalized_text) -> expiry monotonic timestamp
        self.recent_text_hashes: Dict[str, float] = {}
        # monotonic timestamps of accepted submissions
        self.accepted_timestamps: deque[float] = deque()
        # monotonic timestamps of Gemini moderation calls (protects API quota)
        self.gemini_call_timestamps: deque[float] = deque()

    def reset_for_testing(self) -> None:
        self.spent_nonces.clear()
        self.recent_text_hashes.clear()
        self.accepted_timestamps.clear()
        self.gemini_call_timestamps.clear()

    def _prune(self, now_mono: float) -> None:
        expired_nonces = [k for k, exp in self.spent_nonces.items() if exp <= now_mono]
        for k in expired_nonces:
            self.spent_nonces.pop(k, None)

        expired_hashes = [k for k, exp in self.recent_text_hashes.items() if exp <= now_mono]
        for k in expired_hashes:
            self.recent_text_hashes.pop(k, None)

        day_ago = now_mono - 86400.0
        while self.accepted_timestamps and self.accepted_timestamps[0] <= day_ago:
            self.accepted_timestamps.popleft()

        min_ago = now_mono - 60.0
        while self.gemini_call_timestamps and self.gemini_call_timestamps[0] <= min_ago:
            self.gemini_call_timestamps.popleft()

    def is_nonce_spent(self, nonce: str) -> bool:
        now_mono = time.monotonic()
        self._prune(now_mono)
        return nonce in self.spent_nonces

    def mark_nonce_spent(self, nonce: str, ttl: float = CAPTCHA_TTL_SECONDS + 60.0) -> None:
        now_mono = time.monotonic()
        self._prune(now_mono)
        self.spent_nonces[nonce] = now_mono + ttl

    def check_global_circuit_breaker(self) -> Tuple[bool, Optional[str]]:
        """Check if global hourly (20/h) or daily (60/day) submission limits are reached."""
        now_mono = time.monotonic()
        self._prune(now_mono)

        daily_count = len(self.accepted_timestamps)
        if daily_count >= GLOBAL_MAX_ACCEPTED_PER_DAY:
            return False, "The anonymous dropbox has reached its daily capacity. Please try again later."

        hour_ago = now_mono - 3600.0
        hourly_count = sum(1 for ts in self.accepted_timestamps if ts > hour_ago)
        if hourly_count >= GLOBAL_MAX_ACCEPTED_PER_HOUR:
            return False, "The anonymous dropbox is temporarily busy. Please try again in a little while."

        return True, None

    def check_and_record_gemini_quota(self) -> bool:
        """Protect Gemini API quota from rapid burst floods."""
        now_mono = time.monotonic()
        self._prune(now_mono)
        if len(self.gemini_call_timestamps) >= GLOBAL_MAX_GEMINI_CALLS_PER_MINUTE:
            return False
        self.gemini_call_timestamps.append(now_mono)
        return True

    def is_duplicate_text(self, normalized_text: str) -> bool:
        now_mono = time.monotonic()
        self._prune(now_mono)
        digest = hashlib.sha256(normalized_text.lower().encode("utf-8")).hexdigest()
        return digest in self.recent_text_hashes

    def record_accepted_submission(self, normalized_text: str) -> None:
        now_mono = time.monotonic()
        self._prune(now_mono)
        digest = hashlib.sha256(normalized_text.lower().encode("utf-8")).hexdigest()
        self.recent_text_hashes[digest] = now_mono + DUPLICATE_TEXT_TTL_SECONDS
        self.accepted_timestamps.append(now_mono)


drop_state = DropRateAndReplayState()


# ------------------------------------------------------------------------------
# Stateless HMAC-Signed CAPTCHA Generation & Verification
# ------------------------------------------------------------------------------

def _compute_answer_hash(secret: bytes, nonce: str, normalized_answer: str) -> str:
    msg = f"{nonce}:{normalized_answer.upper()}".encode("utf-8")
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()


def _sign_token_payload(secret: bytes, payload_str: str) -> str:
    return hmac.new(secret, payload_str.encode("utf-8"), hashlib.sha256).hexdigest()


def create_stateless_captcha(
    custom_answer: Optional[str] = None,
    ttl_seconds: int = CAPTCHA_TTL_SECONDS,
) -> Tuple[str, str, str]:
    """Generate a stateless CAPTCHA challenge.

    Returns:
        (answer_text, challenge_token, data_uri_png)
    """
    secret = get_captcha_secret()
    if custom_answer and len(custom_answer) == CAPTCHA_LENGTH:
        answer = custom_answer.upper()
    else:
        answer = "".join(secrets.choice(CAPTCHA_ALPHABET) for _ in range(CAPTCHA_LENGTH))

    nonce = secrets.token_hex(16)  # 128-bit CSPRNG hex
    exp = int(time.time()) + int(ttl_seconds)
    answer_hash = _compute_answer_hash(secret, nonce, answer)

    payload_str = f"{nonce}.{exp}.{answer_hash}"
    sig = _sign_token_payload(secret, payload_str)
    challenge_token = f"{payload_str}.{sig}"

    png_bytes = render_captcha_png(answer)
    b64_png = base64.b64encode(png_bytes).decode("ascii")
    data_uri = f"data:image/png;base64,{b64_png}"
    return answer, challenge_token, data_uri


def verify_stateless_captcha(challenge_token: str, user_answer: str) -> Tuple[bool, str]:
    """Verify a stateless HMAC-signed CAPTCHA token and consume its nonce on first attempt.

    Returns:
        (is_valid, error_message)
    """
    token = (challenge_token or "").strip()
    ans = re.sub(r"\s+", "", (user_answer or "")).upper()
    if not token or not ans:
        return False, "Please enter the 6-character security code shown in the image."

    parts = token.split(".")
    if len(parts) != 4:
        return False, "Invalid security challenge token. Please solve the new code below."

    nonce, exp_str, answer_hash, sig = parts
    if not re.fullmatch(r"[0-9a-f]{32}", nonce):
        return False, "Invalid security challenge nonce. Please solve the new code below."

    try:
        exp = int(exp_str)
    except ValueError:
        return False, "Invalid security challenge timestamp. Please solve the new code below."

    secret = get_captcha_secret()
    payload_str = f"{nonce}.{exp_str}.{answer_hash}"
    expected_sig = _sign_token_payload(secret, payload_str)
    if not hmac.compare_digest(sig, expected_sig):
        return False, "Security challenge signature verification failed. Please solve the new code below."

    now_ts = int(time.time())
    if now_ts > exp:
        return False, "Security code expired (valid for 5 minutes). Please solve the new code below."

    if drop_state.is_nonce_spent(nonce):
        return False, "This security code was already used. Please solve the new code below."

    # Mark nonce spent immediately so neither valid nor invalid tokens can be brute-forced or replayed
    drop_state.mark_nonce_spent(nonce)

    expected_answer_hash = _compute_answer_hash(secret, nonce, ans)
    if not hmac.compare_digest(answer_hash, expected_answer_hash):
        return False, "Incorrect security code. Your message was kept below — please try the new code."

    return True, ""


def _load_captcha_font(size: int = 28) -> Any:
    """Load a TrueType font with fallback to Pillow's built-in font."""
    font_candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSansMono-Bold.ttf",
        "/usr/share/fonts/liberation/LiberationMono-Bold.ttf",
    ]
    for path in font_candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def render_captcha_png(text: str) -> bytes:
    """Render a distorted dark-theme PNG CAPTCHA with wave warp, rotation, and background noise."""
    width, height = 220, 72
    bg_color = (11, 15, 25)
    img = Image.new("RGB", (width, height), color=bg_color)
    draw = ImageDraw.Draw(img)

    rng = random.SystemRandom()

    # Background grid & subtle noise dots
    for _ in range(95):
        x = rng.randint(0, width - 1)
        y = rng.randint(0, height - 1)
        c = rng.choice([(30, 58, 138), (38, 50, 75), (56, 189, 248), (71, 85, 105)])
        draw.point((x, y), fill=c)

    # Random background distraction arcs/lines
    for _ in range(5):
        x1, y1 = rng.randint(0, width // 2), rng.randint(6, height - 6)
        x2, y2 = rng.randint(width // 2, width), rng.randint(6, height - 6)
        line_color = rng.choice([(37, 99, 235), (56, 189, 248), (51, 65, 85)])
        draw.line([(x1, y1), (x2, y2)], fill=line_color, width=rng.randint(1, 2))

    font = _load_captcha_font(30)
    palette = [
        (125, 211, 252),
        (96, 165, 250),
        (165, 180, 252),
        (224, 242, 254),
        (56, 189, 248),
    ]

    # Render each character onto its own rotated layer
    start_x = 18
    step_x = 31
    for idx, ch in enumerate(text):
        glyph_img = Image.new("RGBA", (46, 56), (0, 0, 0, 0))
        g_draw = ImageDraw.Draw(glyph_img)
        color = rng.choice(palette)
        g_draw.text((8, 6), ch, font=font, fill=color)
        angle = rng.randint(-12, 12)
        rotated = glyph_img.rotate(angle, resample=Image.BICUBIC, expand=False)
        x_pos = start_x + idx * step_x + rng.randint(-2, 2)
        y_pos = 8 + rng.randint(-3, 4)
        img.paste(rotated, (x_pos, y_pos), rotated)

    # Apply subtle sine-wave horizontal/vertical distortion
    src_pixels = img.load()
    warped = Image.new("RGB", (width, height), color=bg_color)
    dst_pixels = warped.load()
    phase_x = rng.uniform(0, 2 * math.pi)
    phase_y = rng.uniform(0, 2 * math.pi)
    amp_x = rng.uniform(0.9, 1.5)
    amp_y = rng.uniform(0.6, 1.1)

    for y in range(height):
        shift_x = int(amp_x * math.sin(2.0 * math.pi * y / 32.0 + phase_x))
        for x in range(width):
            shift_y = int(amp_y * math.cos(2.0 * math.pi * x / 40.0 + phase_y))
            sx = (x + shift_x) % width
            sy = (y + shift_y) % height
            dst_pixels[x, y] = src_pixels[sx, sy]

    # Subtle foreground noise arc
    w_draw = ImageDraw.Draw(warped)
    pts = [
        (rng.randint(8, 35), rng.randint(14, height - 14)),
        (rng.randint(85, 135), rng.randint(14, height - 14)),
        (rng.randint(175, width - 8), rng.randint(14, height - 14)),
    ]
    w_draw.line(pts, fill=(56, 189, 248), width=1)

    buf = io.BytesIO()
    warped.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ------------------------------------------------------------------------------
# Content Normalization, Link Blocking & Validation
# ------------------------------------------------------------------------------

# Strip C0/C1 control characters (except \n and \t) and zero-width invisible characters
_INVISIBLE_AND_CONTROL_RE = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]"
)

# Strict link/domain detector to prevent link spam
_URL_OR_DOMAIN_RE = re.compile(
    r"(?:"
    r"https?://|"
    r"www\.|"
    r"t\.me/|"
    r"telegram\.me/|"
    r"bit\.ly/|"
    r"\b[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\."
    r"(?:com|org|net|edu|gov|io|co|am|ru|me|info|biz|xyz|site|online|top|club|vip|app|dev|ai|gg|ly|link|shop|store| Onion|onion)\b"
    r")",
    re.IGNORECASE,
)


def sanitize_and_validate_drop_content(raw_text: str) -> Tuple[bool, str, Optional[str]]:
    """Strip invisible/control characters and validate length and no-URL constraints.

    Returns:
        (is_valid, cleaned_text, error_message)
    """
    cleaned = _INVISIBLE_AND_CONTROL_RE.sub("", raw_text or "")
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    # Collapse 4+ consecutive newlines to 2
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()

    if len(cleaned) < MIN_POST_LENGTH:
        return (
            False,
            cleaned,
            f"Your post is too short ({len(cleaned)} characters). Please write at least {MIN_POST_LENGTH} characters.",
        )

    if len(cleaned) > MAX_POST_LENGTH:
        return (
            False,
            cleaned[:MAX_POST_LENGTH],
            f"Your post exceeds the maximum length of {MAX_POST_LENGTH} characters.",
        )

    if _URL_OR_DOMAIN_RE.search(cleaned):
        return (
            False,
            cleaned,
            "Links, URLs, and domain names (e.g. http://, t.me/, .com, .am) are not allowed in anonymous drop posts.",
        )

    return True, cleaned, None


# ------------------------------------------------------------------------------
# Synchronous Pre-Publish Gemini Safety & Anti-Doxxing Screening (Fail-Closed)
# ------------------------------------------------------------------------------

class DropModerationVerdict(BaseModel):
    """Structured JSON schema returned by Gemini for pre-publish screening."""
    allowed: bool
    category: Literal["ok", "doxxing", "harassment", "spam", "illegal"]
    user_message: str


DROP_MODERATION_SYSTEM_PROMPT = """You are the strict pre-publish safety and anti-doxxing moderator for AUA Hub's Anonymous Post Dropbox (American University of Armenia).
Evaluate the submitted anonymous text before it is queued for public publication.
Students may write in English, Armenian (including Latin-script Armlish transliteration), or Russian.

REJECT (`allowed: false`) if the submission contains ANY of the following:
1. `doxxing`: Full names of private students or private individuals paired with accusations, rumors, or personal commentary; phone numbers; residential addresses; student IDs; private emails; or personal social media / Telegram / Instagram handles (`@username`).
2. `harassment`: Targeted bullying, hate speech, slurs, sexual harassment, defamatory attacks on individuals, or threats.
3. `illegal`: Threats of physical violence, self-harm/suicide encouragement, extortion, blackmail, doxxing threats, or illegal contraband.
4. `spam`: Commercial advertisements, crypto/betting promotions, scams, or meaningless keyboard-mash/bot gibberish.

ALLOW (`allowed: true`, `category: "ok"`) genuine anonymous AUA confessions, campus vents, academic/course opinions, advice requests, lost-and-found notices, and general student discussions that do not dox or attack private individuals.

Return strictly valid JSON matching:
{
  "allowed": boolean,
  "category": "ok" | "doxxing" | "harassment" | "spam" | "illegal",
  "user_message": "Brief, neutral explanation for the user (1 sentence)"
}
"""


class DropAIModerator:
    """Fail-closed Gemini moderator for pre-publish anonymous dropbox screening."""

    def __init__(self) -> None:
        self._client: Any = None
        self._semaphore = asyncio.Semaphore(4)

    def _get_client(self) -> Any:
        if self._client is None:
            key = (getattr(settings, "GEMINI_API_KEY", None) or "").strip()
            if not key or key == "your_gemini_api_key_here" or genai is None:
                return None
            self._client = genai.Client(api_key=key)
        return self._client

    async def screen_submission(self, content: str) -> Tuple[Optional[DropModerationVerdict], Optional[str]]:
        """Screen content with Gemini 2.5 Flash (`thinking_budget=0`).

        Fail-closed policy: if Gemini is unavailable, errors, times out, or returns malformed JSON,
        returns `(None, error_message)` and NEVER auto-approves.
        """
        if not drop_state.check_and_record_gemini_quota():
            return None, "Moderation service is experiencing high traffic. Please try again in a minute."

        async with self._semaphore:
            client = self._get_client()
            if client is None or types is None:
                return None, "Automated safety screening is temporarily unavailable. Please try again shortly."

            try:
                config_kwargs: Dict[str, Any] = {
                    "response_mime_type": "application/json",
                    "response_schema": DropModerationVerdict,
                    "temperature": 0.0,
                    "max_output_tokens": 256,
                    "system_instruction": DROP_MODERATION_SYSTEM_PROMPT,
                }
                if hasattr(types, "ThinkingConfig"):
                    config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)

                config = types.GenerateContentConfig(**config_kwargs)
                response = await asyncio.wait_for(
                    client.aio.models.generate_content(
                        model="gemini-2.5-flash",
                        contents=content,
                        config=config,
                    ),
                    timeout=12.0,
                )

                verdict: Optional[DropModerationVerdict] = None
                if hasattr(response, "parsed") and isinstance(response.parsed, DropModerationVerdict):
                    verdict = response.parsed
                else:
                    raw_text = (getattr(response, "text", None) or "").strip()
                    if raw_text:
                        data = json.loads(raw_text)
                        verdict = DropModerationVerdict.model_validate(data)

                if verdict is None:
                    return None, "Automated safety screening returned an invalid response. Please try again shortly."

                return verdict, None
            except Exception:
                # Never log submitted text or headers!
                return None, "Automated safety screening is temporarily unavailable. Please try again shortly."


drop_ai_moderator = DropAIModerator()


# ------------------------------------------------------------------------------
# Anti-Timing-Correlation Queue Helpers & Background Worker
# ------------------------------------------------------------------------------

def bucket_timestamp_5min(dt: Optional[datetime] = None) -> datetime:
    """Round a UTC timestamp down to the nearest 5-minute bucket to prevent timing metadata leaks."""
    now = (dt or datetime.now(timezone.utc)).astimezone(timezone.utc)
    bucket_minute = (now.minute // 5) * 5
    return now.replace(minute=bucket_minute, second=0, microsecond=0)


async def enqueue_approved_drop_post(content: str) -> AnonymousDropQueue:
    """Insert an approved post into `anonymous_drop_queue` with a random 2-8 minute delay and 5-min bucketed created_at."""
    now_utc = datetime.now(timezone.utc)
    delay_sec = secrets.SystemRandom().randint(PUBLISH_DELAY_MIN_SECONDS, PUBLISH_DELAY_MAX_SECONDS)
    publish_after = now_utc + timedelta(seconds=delay_sec)
    bucketed_created = bucket_timestamp_5min(now_utc)

    async with get_db_session() as session:
        item = AnonymousDropQueue(
            content=content,
            publish_after=publish_after,
            status="queued",
            created_at=bucketed_created,
        )
        session.add(item)
        await session.commit()
        await session.refresh(item)
        return item


async def process_due_drop_queue_items() -> int:
    """Publish all queued items whose `publish_after <= utcnow()` and immediately purge them from `anonymous_drop_queue`."""
    now_utc = datetime.now(timezone.utc)
    published_count = 0

    async with get_db_session() as session:
        stmt = (
            select(AnonymousDropQueue)
            .where(
                AnonymousDropQueue.status == "queued",
                AnonymousDropQueue.publish_after <= now_utc,
            )
            .order_by(AnonymousDropQueue.publish_after.asc())
            .limit(25)
        )
        due_items = list((await session.execute(stmt)).scalars().all())

        for item in due_items:
            await create_anonymous_public_post(
                session=session,
                telegram_user_id=None,
                content_text=item.content,
                source="web_drop",
            )
            await session.execute(
                delete(AnonymousDropQueue).where(AnonymousDropQueue.id == item.id)
            )
            await session.commit()
            published_count += 1

    return published_count


async def drop_queue_publisher_worker() -> None:
    """Background asyncio task polling `anonymous_drop_queue` every 30 seconds."""
    while True:
        try:
            await process_due_drop_queue_items()
        except asyncio.CancelledError:
            break
        except Exception:
            # Never log post contents or details
            pass
        await asyncio.sleep(QUEUE_POLL_INTERVAL_SECONDS)


# ------------------------------------------------------------------------------
# Pure-HTML / Zero-JS Server-Side Template Renderer
# ------------------------------------------------------------------------------

def render_drop_page(
    *,
    transport: str,
    captcha_data_uri: str = "",
    challenge_token: str = "",
    preserved_text: str = "",
    error_message: Optional[str] = None,
    status_kind: Literal["form", "queued", "busy"] = "form",
    busy_message: Optional[str] = None,
) -> str:
    """Render a 100% self-contained, zero-JavaScript, zero-external-asset HTML document.

    CRITICAL PRIVACY RULE:
    When `transport == "onion"`, this function MUST NEVER emit any clearnet `auahub.am` URLs or references.
    """
    is_onion = (transport or "").strip().lower() == "onion"
    onion_host = get_onion_hostname()

    if is_onion:
        transport_banner_html = """
        <div class="transport-badge transport-onion" role="status">
          <span class="dot dot-green" aria-hidden="true"></span>
          <div>
            <strong>Connected directly via Tor (.onion) &mdash; bypassing Cloudflare completely.</strong>
            <div class="transport-sub">End-to-end encrypted inside the Tor network. Zero clearnet links or external assets are loaded on this page.</div>
          </div>
        </div>
        """
    else:
        onion_box_html = ""
        if onion_host:
            escaped_onion_url = html.escape(f"http://{onion_host}/")
            onion_box_html = f"""
            <div class="onion-mirror-box">
              <div class="onion-mirror-label">Direct Tor v3 .onion Mirror (Select &amp; Copy into Tor Browser):</div>
              <div class="onion-address-select">{escaped_onion_url}</div>
            </div>
            """
        transport_banner_html = f"""
        <div class="transport-badge transport-clearnet" role="status">
          <span class="dot dot-amber" aria-hidden="true"></span>
          <div>
            <strong>Clearnet Connection (Cloudflare Tunnel)</strong>
            <div class="transport-sub">
              Our proxy strips all IP headers and cookies before requests reach the server. However, because clearnet traffic passes through Cloudflare Tunnel, we recommend using <strong>Tor Browser</strong> (and our direct <code>.onion</code> mirror) if you want to hide your IP from both your ISP and Cloudflare.
            </div>
            {onion_box_html}
          </div>
        </div>
        """

    error_box_html = ""
    if error_message:
        error_box_html = f"""
        <div class="alert-box alert-error" role="alert">
          <strong>Submission Not Accepted:</strong> {html.escape(error_message)}
        </div>
        """

    if status_kind == "queued":
        main_panel_html = """
        <div class="status-card status-success" role="status">
          <div class="status-title">Post Queued for Anonymous Publication</div>
          <p class="status-desc">
            Your post passed automated safety checks and is queued for anonymous publication in a few minutes (a random 2&ndash;8 minute delay is added to protect your privacy).
          </p>
          <p class="status-desc-muted">
            No receipt ID, cookie, or timestamp was issued, and your queued record is automatically purged from the queue database the moment it is published.
          </p>
          <div class="status-actions">
            <a href="/" class="btn-primary">Submit Another Post</a>
          </div>
        </div>
        """
    elif status_kind == "busy":
        msg = html.escape(busy_message or "Dropbox is temporarily busy, please try again later.")
        main_panel_html = f"""
        <div class="status-card status-busy" role="alert">
          <div class="status-title">Dropbox Temporarily Unavailable</div>
          <p class="status-desc">{msg}</p>
          <div class="status-actions">
            <a href="/" class="btn-primary">Return to Dropbox</a>
          </div>
        </div>
        """
    else:
        escaped_text = html.escape(preserved_text or "")
        escaped_token = html.escape(challenge_token or "")
        escaped_uri = html.escape(captcha_data_uri or "")
        main_panel_html = f"""
        {error_box_html}
        <form action="/submit" method="POST" enctype="application/x-www-form-urlencoded" class="drop-form" autocomplete="off">
          <div class="hp-field" aria-hidden="true">
            <label for="website_url">Leave this field blank</label>
            <input type="text" id="website_url" name="website" value="" tabindex="-1" autocomplete="off" />
          </div>

          <input type="hidden" name="challenge_token" value="{escaped_token}" />

          <div class="field-group">
            <label for="post_content" class="field-label">Your Anonymous Post (20 &ndash; 2,000 characters)</label>
            <textarea
              id="post_content"
              name="content"
              rows="7"
              minlength="20"
              maxlength="2000"
              required
              placeholder="Write your anonymous confession, campus thought, course opinion, or question here. Plain text only — no links/URLs, no full names of private individuals, and no phone numbers or personal handles."
            >{escaped_text}</textarea>
            <div class="field-hint">
              Plain text only &bull; 20 to 2,000 characters &bull; No links or private personal info (PII/doxxing).
            </div>
          </div>

          <div class="captcha-box">
            <div class="captcha-image-col">
              <div class="captcha-header-row">
                <span class="field-label" style="margin-bottom:0;">Security Check (Zero-JS)</span>
                <button type="submit" name="refresh_captcha" value="1" formnovalidate class="btn-refresh-captcha" title="Generate a new CAPTCHA image while keeping your typed post">
                  &#8635; New Code
                </button>
              </div>
              <img src="{escaped_uri}" alt="6-character security challenge image" width="220" height="72" class="captcha-img" />
              <div class="field-hint">Case-insensitive &bull; Excludes 0, O, Q, D, 1, I, 8, B</div>
            </div>
            <div class="captcha-input-col">
              <label for="captcha_answer" class="field-label">Enter 6-Character Code</label>
              <input
                type="text"
                id="captcha_answer"
                name="captcha_answer"
                minlength="6"
                maxlength="6"
                required
                autocomplete="off"
                autocapitalize="off"
                spellcheck="false"
                placeholder="6-char code"
                class="captcha-input"
              />
              <div class="field-hint">Click &ldquo;&#8635; New Code&rdquo; anytime to get a fresh image without losing your text.</div>
            </div>
          </div>

          <button type="submit" class="btn-primary btn-submit">Submit Anonymously</button>
        </form>
        """

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <meta name="referrer" content="no-referrer" />
  <title>AUA Hub — Anonymous Drop</title>
  <style>
    :root {{
      --bg-deep: #05070c;
      --bg-card: #0c1220;
      --bg-input: #080d18;
      --border-subtle: rgba(148, 163, 184, 0.18);
      --border-accent: rgba(56, 189, 248, 0.42);
      --text-primary: #f1f5f9;
      --text-secondary: #cbd5e1;
      --text-muted: #94a3b8;
      --accent-blue: #38bdf8;
      --accent-indigo: #3b82f6;
      --danger-bg: rgba(239, 68, 68, 0.14);
      --danger-border: rgba(248, 113, 113, 0.45);
      --danger-text: #fecaca;
      --success-bg: rgba(16, 185, 129, 0.14);
      --success-border: rgba(52, 211, 153, 0.45);
      --success-text: #a7f3d0;
    }}
    * {{
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }}
    body {{
      background-color: var(--bg-deep);
      color: var(--text-primary);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Ubuntu, Cantarell, "Noto Sans", sans-serif;
      font-size: 16px;
      line-height: 1.55;
      padding: 24px 14px 48px;
    }}
    .container {{
      max-width: 680px;
      margin: 0 auto;
    }}
    .header-card {{
      background: var(--bg-card);
      border: 1px solid var(--border-subtle);
      border-radius: 14px;
      padding: 22px 22px 18px;
      margin-bottom: 16px;
    }}
    .eyebrow {{
      display: inline-block;
      font-size: 12px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--accent-blue);
      background: rgba(56, 189, 248, 0.1);
      border: 1px solid rgba(56, 189, 248, 0.28);
      border-radius: 999px;
      padding: 3px 11px;
      margin-bottom: 10px;
    }}
    h1 {{
      font-size: 24px;
      font-weight: 800;
      letter-spacing: -0.02em;
      color: #ffffff;
      margin-bottom: 6px;
    }}
    .subtitle {{
      font-size: 14.5px;
      color: var(--text-secondary);
    }}
    .transport-badge {{
      display: flex;
      align-items: flex-start;
      gap: 12px;
      background: var(--bg-card);
      border: 1px solid var(--border-subtle);
      border-radius: 12px;
      padding: 14px 16px;
      margin-bottom: 16px;
      font-size: 13.5px;
      color: var(--text-secondary);
    }}
    .transport-onion {{
      border-color: var(--success-border);
      background: rgba(16, 185, 129, 0.08);
    }}
    .transport-clearnet {{
      border-color: rgba(251, 191, 36, 0.35);
      background: rgba(251, 191, 36, 0.06);
    }}
    .transport-sub {{
      margin-top: 4px;
      color: var(--text-muted);
      font-size: 13px;
    }}
    .dot {{
      width: 10px;
      height: 10px;
      border-radius: 50%;
      flex-shrink: 0;
      margin-top: 5px;
    }}
    .dot-green {{
      background: #10b981;
      box-shadow: 0 0 8px rgba(16, 185, 129, 0.8);
    }}
    .dot-amber {{
      background: #f59e0b;
      box-shadow: 0 0 8px rgba(245, 158, 11, 0.7);
    }}
    .onion-mirror-box {{
      margin-top: 10px;
      padding: 10px 12px;
      background: var(--bg-input);
      border: 1px solid var(--border-subtle);
      border-radius: 8px;
    }}
    .onion-mirror-label {{
      font-size: 11.5px;
      font-weight: 700;
      color: var(--accent-blue);
      margin-bottom: 4px;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }}
    .onion-address-select {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
      font-size: 12.5px;
      color: #f8fafc;
      word-break: break-all;
      user-select: all;
      -webkit-user-select: all;
    }}
    .main-card {{
      background: var(--bg-card);
      border: 1px solid var(--border-subtle);
      border-radius: 14px;
      padding: 22px;
      margin-bottom: 16px;
    }}
    .alert-box {{
      border-radius: 10px;
      padding: 13px 15px;
      margin-bottom: 18px;
      font-size: 14px;
    }}
    .alert-error {{
      background: var(--danger-bg);
      border: 1px solid var(--danger-border);
      color: var(--danger-text);
    }}
    .hp-field {{
      position: absolute;
      left: -9999px;
      top: -9999px;
      width: 1px;
      height: 1px;
      overflow: hidden;
    }}
    .field-group {{
      margin-bottom: 18px;
    }}
    .field-label {{
      display: block;
      font-size: 13.5px;
      font-weight: 700;
      color: var(--text-primary);
      margin-bottom: 7px;
    }}
    .field-hint {{
      font-size: 12.5px;
      color: var(--text-muted);
      margin-top: 6px;
    }}
    textarea, .captcha-input {{
      width: 100%;
      background: var(--bg-input);
      color: var(--text-primary);
      border: 1px solid var(--border-subtle);
      border-radius: 10px;
      padding: 12px 14px;
      font-size: 16px;
      font-family: inherit;
      line-height: 1.5;
    }}
    textarea:focus, .captcha-input:focus {{
      outline: 2px solid var(--accent-blue);
      border-color: var(--border-accent);
    }}
    textarea {{
      resize: vertical;
      min-height: 150px;
    }}
    .captcha-box {{
      display: flex;
      flex-wrap: wrap;
      gap: 16px;
      align-items: flex-start;
      background: rgba(8, 13, 24, 0.65);
      border: 1px solid var(--border-subtle);
      border-radius: 12px;
      padding: 15px;
      margin-bottom: 18px;
    }}
    .captcha-image-col {{
      flex: 0 0 auto;
    }}
    .captcha-header-row {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 7px;
    }}
    .btn-refresh-captcha {{
      background: rgba(56, 189, 248, 0.12);
      color: var(--accent-blue);
      border: 1px solid rgba(56, 189, 248, 0.35);
      border-radius: 6px;
      padding: 3px 9px;
      font-size: 12px;
      font-weight: 700;
      cursor: pointer;
      font-family: inherit;
    }}
    .btn-refresh-captcha:hover {{
      background: rgba(56, 189, 248, 0.22);
      color: #ffffff;
    }}
    .captcha-input-col {{
      flex: 1 1 200px;
    }}
    .captcha-img {{
      display: block;
      border-radius: 8px;
      border: 1px solid var(--border-subtle);
      background: #0b0f19;
    }}
    .captcha-input {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-weight: 700;
      letter-spacing: 0.18em;
      text-transform: uppercase;
      text-align: center;
      height: 48px;
    }}
    .btn-primary {{
      display: inline-block;
      width: 100%;
      text-align: center;
      text-decoration: none;
      background: linear-gradient(135deg, #0284c7, #2563eb);
      color: #ffffff;
      font-size: 16px;
      font-weight: 700;
      padding: 13px 20px;
      border: 1px solid rgba(125, 211, 252, 0.35);
      border-radius: 10px;
      cursor: pointer;
    }}
    .btn-primary:hover {{
      background: linear-gradient(135deg, #0369a1, #1d4ed8);
    }}
    .status-card {{
      padding: 10px 4px;
    }}
    .status-title {{
      font-size: 20px;
      font-weight: 800;
      margin-bottom: 10px;
      color: #ffffff;
    }}
    .status-success .status-title {{
      color: var(--success-text);
    }}
    .status-busy .status-title {{
      color: var(--danger-text);
    }}
    .status-desc {{
      font-size: 15px;
      color: var(--text-primary);
      margin-bottom: 12px;
    }}
    .status-desc-muted {{
      font-size: 13.5px;
      color: var(--text-muted);
      margin-bottom: 18px;
    }}
    .status-actions {{
      margin-top: 8px;
    }}
    .privacy-box {{
      background: var(--bg-card);
      border: 1px solid var(--border-subtle);
      border-radius: 14px;
      padding: 20px 22px;
    }}
    .privacy-title {{
      font-size: 15.5px;
      font-weight: 800;
      color: #ffffff;
      margin-bottom: 10px;
    }}
    .privacy-list {{
      list-style: disc;
      padding-left: 20px;
      color: var(--text-secondary);
      font-size: 13.5px;
    }}
    .privacy-list li {{
      margin-bottom: 7px;
    }}
    code {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 12.5px;
      background: rgba(148, 163, 184, 0.14);
      padding: 2px 5px;
      border-radius: 4px;
      color: #e2e8f0;
    }}
  </style>
</head>
<body>
  <div class="container">
    <header class="header-card">
      <span class="eyebrow">Zero-JS &bull; Zero-Cookie &bull; Zero-Log</span>
      <h1>AUA Hub &mdash; Anonymous Drop</h1>
      <p class="subtitle">
        Submit an anonymous post directly to the AUA Hub Anonymous Posts feed without a Telegram account or login.
      </p>
    </header>

    {transport_banner_html}

    <main class="main-card">
      {main_panel_html}
    </main>

    <section class="privacy-box" aria-label="How Your Privacy Works">
      <h2 class="privacy-title">How Your Privacy Works</h2>
      <ul class="privacy-list">
        <li><strong>No Account Required:</strong> No Telegram account, AUA Hub login, or email address is needed to submit a post.</li>
        <li><strong>Zero JavaScript &amp; Zero Cookies:</strong> This portal uses pure HTML/CSS and self-hosted inline base64 CAPTCHAs. It emits zero cookies, loads zero external assets, and works on Tor Browser&apos;s <strong>Safest</strong> security level.</li>
        <li><strong>Zero Access Logs &amp; Header Stripping:</strong> Server access logs are disabled for this dropbox. Identifying headers (<code>CF-Connecting-IP</code>, <code>X-Forwarded-For</code>, <code>Cookie</code>, <code>User-Agent</code>, <code>Referer</code>) are stripped at the edge proxy before reaching the application.</li>
        <li><strong>2&ndash;8 Minute Anti-Timing-Correlation Queue:</strong> Approved posts are held in an encrypted random-delay queue for <strong>2 to 8 minutes</strong> with 5-minute-bucketed timestamps before appearing in the feed, preventing network timing correlation.</li>
        <li><strong>Automated Safety &amp; Anti-Doxxing Screening:</strong> Every post is screened before queuing. Posts containing personal identifiable information (doxxing, full names of private individuals, phone numbers, social handles), severe harassment, links, or spam are automatically rejected.</li>
      </ul>
    </section>
  </div>
</body>
</html>"""


# ------------------------------------------------------------------------------
# aiohttp Route Handlers (`/_drop/`, `/_drop/submit`, `/_drop/healthz`)
# ------------------------------------------------------------------------------

def _get_transport_mode(request: web.Request) -> str:
    raw = (request.headers.get("X-Drop-Transport") or "clearnet").strip().lower()
    return "onion" if raw == "onion" else "clearnet"


async def handle_drop_index(request: web.Request) -> web.Response:
    """Handle `GET /_drop/` — requires valid X-Drop-Proxy-Secret or returns 404."""
    if not verify_proxy_secret(request):
        return web.Response(status=404, text="Not Found")

    transport = _get_transport_mode(request)
    _, challenge_token, data_uri = create_stateless_captcha()
    html_doc = render_drop_page(
        transport=transport,
        captcha_data_uri=data_uri,
        challenge_token=challenge_token,
        status_kind="form",
    )
    return build_drop_html_response(html_doc, status=200)


async def handle_drop_healthz(request: web.Request) -> web.Response:
    """Handle `GET /_drop/healthz` — requires valid X-Drop-Proxy-Secret or returns 404."""
    if not verify_proxy_secret(request):
        return web.Response(status=404, text="Not Found")
    return web.json_response({"ok": True, "service": "anonymous_drop"}, headers=DROP_SECURITY_HEADERS)


async def handle_drop_submit(request: web.Request) -> web.Response:
    """Handle `POST /_drop/submit` — enforces zero-identity, stateless CAPTCHA, anti-spam, Gemini screening, and queue delay."""
    if not verify_proxy_secret(request):
        return web.Response(status=404, text="Not Found")

    transport = _get_transport_mode(request)

    # 1. Enforce Content-Type: application/x-www-form-urlencoded and max body size 16 KB
    content_type = (request.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        _, new_token, new_uri = create_stateless_captcha()
        return build_drop_html_response(
            render_drop_page(
                transport=transport,
                captcha_data_uri=new_uri,
                challenge_token=new_token,
                error_message="Invalid request format. Only standard form submissions are accepted.",
            ),
            status=400,
        )

    if request.content_length is not None and request.content_length > MAX_BODY_BYTES:
        _, new_token, new_uri = create_stateless_captcha()
        return build_drop_html_response(
            render_drop_page(
                transport=transport,
                captcha_data_uri=new_uri,
                challenge_token=new_token,
                error_message="Submission payload exceeds the 16 KB limit.",
            ),
            status=413,
        )

    raw_bytes = await request.content.read(MAX_BODY_BYTES + 1)
    if len(raw_bytes) > MAX_BODY_BYTES:
        _, new_token, new_uri = create_stateless_captcha()
        return build_drop_html_response(
            render_drop_page(
                transport=transport,
                captcha_data_uri=new_uri,
                challenge_token=new_token,
                error_message="Submission payload exceeds the 16 KB limit.",
            ),
            status=413,
        )

    try:
        parsed_form = parse_qs(
            raw_bytes.decode("utf-8", errors="replace"),
            keep_blank_values=True,
            max_num_fields=16,
        )
    except Exception:
        _, new_token, new_uri = create_stateless_captcha()
        return build_drop_html_response(
            render_drop_page(
                transport=transport,
                captcha_data_uri=new_uri,
                challenge_token=new_token,
                error_message="Malformed form data.",
            ),
            status=400,
        )

    honeypot_val = (parsed_form.get("website", [""])[0] or "").strip()
    raw_content = parsed_form.get("content", [""])[0] or ""
    challenge_token = (parsed_form.get("challenge_token", [""])[0] or "").strip()
    captcha_answer = (parsed_form.get("captcha_answer", [""])[0] or "").strip()
    refresh_captcha = (parsed_form.get("refresh_captcha", [""])[0] or "").strip()

    # 2. Honeypot Check (reject without calling Gemini)
    if honeypot_val:
        _, new_token, new_uri = create_stateless_captcha()
        return build_drop_html_response(
            render_drop_page(
                transport=transport,
                captcha_data_uri=new_uri,
                challenge_token=new_token,
                error_message="Invalid submission.",
            ),
            status=400,
        )

    # 2b. Zero-JS CAPTCHA Refresh Button ("↻ New Code" preserves typed text without error)
    preserved_clean = _INVISIBLE_AND_CONTROL_RE.sub("", raw_content)[:MAX_POST_LENGTH]
    if refresh_captcha == "1":
        # Mark old challenge nonce as spent if valid format
        old_parts = challenge_token.split(".")
        if len(old_parts) == 4 and re.fullmatch(r"[0-9a-f]{32}", old_parts[0]):
            drop_state.mark_nonce_spent(old_parts[0])
        _, new_token, new_uri = create_stateless_captcha()
        return build_drop_html_response(
            render_drop_page(
                transport=transport,
                captcha_data_uri=new_uri,
                challenge_token=new_token,
                preserved_text=preserved_clean,
                error_message=None,
            ),
            status=200,
        )

    # 3. Stateless CAPTCHA & Replay Check (preserve user text on failure)
    captcha_ok, captcha_err = verify_stateless_captcha(challenge_token, captcha_answer)
    if not captcha_ok:
        _, new_token, new_uri = create_stateless_captcha()
        return build_drop_html_response(
            render_drop_page(
                transport=transport,
                captcha_data_uri=new_uri,
                challenge_token=new_token,
                preserved_text=preserved_clean,
                error_message=captcha_err,
            ),
            status=400,
        )

    # 4. Content Validation (20-2000 chars, no URLs/links, control chars stripped)
    content_ok, cleaned_content, content_err = sanitize_and_validate_drop_content(raw_content)
    if not content_ok:
        _, new_token, new_uri = create_stateless_captcha()
        return build_drop_html_response(
            render_drop_page(
                transport=transport,
                captcha_data_uri=new_uri,
                challenge_token=new_token,
                preserved_text=cleaned_content,
                error_message=content_err,
            ),
            status=400,
        )

    # 5. Global Circuit Breaker & 6-Hour Duplicate Filter (IP-Free)
    circuit_ok, circuit_msg = drop_state.check_global_circuit_breaker()
    if not circuit_ok:
        return build_drop_html_response(
            render_drop_page(
                transport=transport,
                status_kind="busy",
                busy_message=circuit_msg,
            ),
            status=429,
        )

    if drop_state.is_duplicate_text(cleaned_content):
        _, new_token, new_uri = create_stateless_captcha()
        return build_drop_html_response(
            render_drop_page(
                transport=transport,
                captcha_data_uri=new_uri,
                challenge_token=new_token,
                preserved_text=cleaned_content,
                error_message="This exact post was already submitted recently.",
            ),
            status=400,
        )

    # 6. Synchronous Pre-Publish Gemini Safety & Anti-Doxxing Screening (Fail-Closed)
    verdict, mod_err = await drop_ai_moderator.screen_submission(cleaned_content)
    if mod_err is not None or verdict is None:
        return build_drop_html_response(
            render_drop_page(
                transport=transport,
                status_kind="busy",
                busy_message=mod_err or "Automated safety screening is temporarily unavailable. Please try again shortly.",
            ),
            status=503,
        )

    if not verdict.allowed or verdict.category != "ok":
        _, new_token, new_uri = create_stateless_captcha()
        reason_msg = (verdict.user_message or "Your post did not pass automated community safety checks.").strip()
        return build_drop_html_response(
            render_drop_page(
                transport=transport,
                captcha_data_uri=new_uri,
                challenge_token=new_token,
                preserved_text=cleaned_content,
                error_message=reason_msg,
            ),
            status=400,
        )

    # 7. Enqueue into `AnonymousDropQueue` with 2-8 minute random delay & 5-minute bucketed timestamp
    await enqueue_approved_drop_post(cleaned_content)
    drop_state.record_accepted_submission(cleaned_content)

    return build_drop_html_response(
        render_drop_page(
            transport=transport,
            status_kind="queued",
        ),
        status=200,
    )
