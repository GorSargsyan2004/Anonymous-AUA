"""Group Chat Relay Service: Nickname validation, slow mode, threaded replies, and media broadcast."""

import asyncio
from collections import OrderedDict
from datetime import datetime, timezone
import html
import logging
import random
import re
import time
from typing import Dict, List, Optional, Set, Tuple

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud import (
    close_group_chat_by_id,
    get_active_group_members,
    get_inactive_group_chats_for_closure,
    leave_group_chat,
    record_group_chat_message,
)
from app.database.models import GroupChat, GroupChatMember
from app.services.link_shield import URL_REGEX

logger = logging.getLogger(__name__)

# Slow mode thresholds per user inside a group chat
TEXT_SLOW_MODE_SECONDS = 1.5
MEDIA_SLOW_MODE_SECONDS = 4.0

# Telegram caption character limit
TELEGRAM_MAX_CAPTION_LENGTH = 1024

# Group inactivity auto-close threshold (3 hours)
GROUP_INACTIVITY_TIMEOUT_SECONDS = 3 * 3600

# Reserved / impersonation nickname tokens (case-insensitive)
RESERVED_NICKNAMES: Set[str] = {
    "admin",
    "administrator",
    "moderator",
    "mod",
    "system",
    "bot",
    "staff",
    "auahub",
    "aua_hub",
    "support",
    "official",
    "owner",
    "creator",
    "security",
    "helpdesk",
}

# Basic profanity/slur filter for group names, descriptions, and nicknames
_PROFANITY_PATTERNS = re.compile(
    r"\b(?:fuck|shit|bitch|whore|slut|cunt|nigger|nigga|faggot|retard|dick|pussy|cock|asshole|porn|nsfw|csam)\b",
    re.IGNORECASE,
)

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_NICKNAME_VALID_RE = re.compile(r"^[A-Za-z0-9 _-]+$")

# Curated pool of 96 clean AdjectiveNoun nicknames
CURATED_RANDOM_NICKNAMES: List[str] = [
    "BlueFox", "CrimsonOwl", "SilverWolf", "AmberFalcon", "JadePanda",
    "GoldenHawk", "EmeraldLynx", "CobaltOtter", "VelvetRaven", "CosmicTiger",
    "LunarBear", "SolarEagle", "MysticDeer", "ShadowPuma", "CrystalSwan",
    "ArcticSeal", "ScarletFinch", "BreezeDolphin", "FrostLeopard", "SunnyKoala",
    "NeonBadger", "QuietHeron", "SwiftCheetah", "CloudAlpaca", "StormBison",
    "CoralTurtle", "IvoryCrane", "OnyxPanther", "CopperRobin", "SapphireViper",
    "RubyMantis", "VioletSparrow", "MistyJaguar", "ThunderMoose", "StarlightGazelle",
    "OceanMarlin", "ForestFerret", "DesertFennec", "GlacierPenguin", "MeadowRabbit",
    "CanyonCoyote", "ZenithCondor", "AuroraHedgehog", "EchoChameleon", "BlazePhoenix",
    "DuskWolverine", "DawnPeacock", "PrismLemur", "QuartzOcelot", "TopazMeerkat",
    "ZephyrSwallow", "NimbleSquirrel", "SilentCaracal", "BraveGrizzly", "CleverRaccoon",
    "GentleManatee", "LuckyHummingbird", "NobleStag", "WildMustang", "CalmPelican",
    "RapidKestrel", "BrightFirefly", "DeepNarwhal", "HighAlbatross", "CoolCapybara",
    "WarmWallaby", "BoldJackal", "PureUnicorn", "ZenLoris", "AstroPuffin",
    "CyberGecko", "PixelQuokka", "NovaStarling", "OrbitToucan", "PulseOrca",
    "SparkLark", "DriftOsprey", "CrestIbis", "GroveTapir", "PeakIbex",
    "RidgeCougar", "ValeBadger", "BrookMink", "FlintStoat", "SageMagpie",
    "PineMarten", "AshWren", "FernNewt", "MossDormouse", "ReedKingfisher",
    "SlateOriole", "TealShrike", "Plumjay", "MintChipmunk", "GoldFinch",
    "SilverFox", "AzureLynx", "IndigoWolf", "CoralOwl", "JadeFalcon",
]


def strip_control_chars(text: str) -> str:
    """Remove ASCII/Unicode control characters and collapse excessive whitespace."""
    cleaned = _CONTROL_CHARS_RE.sub("", text or "")
    return re.sub(r"\s+", " ", cleaned).strip()


def validate_group_name(raw_name: str) -> Tuple[bool, str, str]:
    """Validate group chat name (3-40 chars, no control chars, no URLs/profanity).

    Returns:
        (is_valid, cleaned_name, error_message)
    """
    cleaned = strip_control_chars(raw_name)
    if len(cleaned) < 3 or len(cleaned) > 40:
        return False, cleaned, "Group name must be between 3 and 40 characters."
    if URL_REGEX.search(cleaned) or "t.me/" in cleaned.lower() or "@" in cleaned:
        return False, cleaned, "Group name cannot contain links, URLs, or @handles."
    if _PROFANITY_PATTERNS.search(cleaned):
        return False, cleaned, "Group name contains prohibited language. Please keep it clean."
    return True, cleaned, ""


def validate_group_description(raw_desc: Optional[str]) -> Tuple[bool, Optional[str], str]:
    """Validate optional group chat description (up to 200 chars, no URLs/profanity).

    Returns:
        (is_valid, cleaned_description, error_message)
    """
    if not raw_desc or not raw_desc.strip():
        return True, None, ""
    cleaned = strip_control_chars(raw_desc)
    if not cleaned:
        return True, None, ""
    if len(cleaned) > 200:
        return False, cleaned, "Description must be 200 characters or fewer."
    if URL_REGEX.search(cleaned) or "t.me/" in cleaned.lower():
        return False, cleaned, "Description cannot contain external URLs or invite links."
    if _PROFANITY_PATTERNS.search(cleaned):
        return False, cleaned, "Description contains prohibited language. Please keep it clean."
    return True, cleaned, ""


def generate_unique_random_nickname(taken_lower: Optional[Set[str]] = None) -> str:
    """Pick a random AdjectiveNoun nickname guaranteed unique against taken_lower."""
    used = {n.lower() for n in (taken_lower or set())}
    available = [name for name in CURATED_RANDOM_NICKNAMES if name.lower() not in used]
    if available:
        return random.choice(available)

    # Fallback with numeric suffix
    base_pool = list(CURATED_RANDOM_NICKNAMES)
    random.shuffle(base_pool)
    for base in base_pool:
        for num in range(10, 100):
            candidate = f"{base[:18]}{num}"
            if candidate.lower() not in used:
                return candidate
    return f"Student{random.randint(1000, 9999)}"


def validate_group_nickname(
    raw_nickname: str,
    taken_lower: Optional[Set[str]] = None,
) -> Tuple[bool, str, str]:
    """Validate a custom or random group nickname.

    Rules:
    - 2 to 20 characters after trimming and stripping control characters.
    - Alphanumeric, spaces, '_', '-' only.
    - Block reserved/impersonation names (admin, moderator, mod, system, bot, staff, auahub, support, etc.).
    - Block profanity and URLs.
    - Case-insensitive uniqueness across current and past members of that group.

    Returns:
        (is_valid, cleaned_nickname, error_message)
    """
    cleaned = strip_control_chars(raw_nickname)
    if len(cleaned) < 2 or len(cleaned) > 20:
        return False, cleaned, "Nickname must be between 2 and 20 characters."

    if not _NICKNAME_VALID_RE.match(cleaned):
        return (
            False,
            cleaned,
            "Nickname can only contain letters, numbers, spaces, underscores (_), and hyphens (-).",
        )

    lower = cleaned.lower()
    compact = re.sub(r"[\s_-]+", "", lower)
    words = set(re.split(r"[\s_-]+", lower))

    if (
        lower in RESERVED_NICKNAMES
        or compact in RESERVED_NICKNAMES
        or bool(words & RESERVED_NICKNAMES)
        or any(tok in compact for tok in ("admin", "moderator", "auahub", "system", "support", "staff"))
    ):
        return (
            False,
            cleaned,
            "That nickname is reserved or resembles staff/system accounts. Please choose another nickname.",
        )

    if _PROFANITY_PATTERNS.search(cleaned):
        return False, cleaned, "Nickname contains inappropriate language. Please choose another."

    if taken_lower and lower in {n.lower() for n in taken_lower}:
        return (
            False,
            cleaned,
            "That nickname is already taken in this group (including by former members). Please choose another.",
        )

    return True, cleaned, ""


class GroupReplyBuffer:
    """Bounded in-memory ring buffer per group (max 100 entries) for threaded replies.

    Maps:
    - group_message_id -> {user_id: telegram_message_id}
    - (user_id, telegram_message_id) -> group_message_id
    """

    MAX_ENTRIES_PER_GROUP = 100

    def __init__(self) -> None:
        # group_id -> OrderedDict[group_message_id, Dict[user_id, telegram_message_id]]
        self._forward: Dict[int, "OrderedDict[int, Dict[int, int]]"] = {}
        # group_id -> Dict[(user_id, telegram_message_id), group_message_id]
        self._reverse: Dict[int, Dict[Tuple[int, int], int]] = {}

    def record_delivery(
        self,
        group_id: int,
        group_message_id: int,
        user_id: int,
        telegram_message_id: int,
    ) -> None:
        """Associate a user's Telegram message_id with a canonical group_message_id."""
        if group_id not in self._forward:
            self._forward[group_id] = OrderedDict()
            self._reverse[group_id] = {}

        fwd = self._forward[group_id]
        rev = self._reverse[group_id]

        if group_message_id not in fwd:
            if len(fwd) >= self.MAX_ENTRIES_PER_GROUP:
                oldest_gmsg_id, oldest_user_map = fwd.popitem(last=False)
                for u_id, tg_id in list(rev.keys()):
                    if rev.get((u_id, tg_id)) == oldest_gmsg_id:
                        rev.pop((u_id, tg_id), None)
            fwd[group_message_id] = {}

        fwd[group_message_id][user_id] = telegram_message_id
        rev[(user_id, telegram_message_id)] = group_message_id

    def resolve_reply_target(
        self,
        group_id: int,
        sender_user_id: int,
        replied_telegram_message_id: int,
        recipient_user_id: int,
    ) -> Optional[int]:
        """Resolve the corresponding Telegram message_id in recipient_user_id's chat."""
        rev = self._reverse.get(group_id)
        fwd = self._forward.get(group_id)
        if not rev or not fwd:
            return None
        gmsg_id = rev.get((sender_user_id, replied_telegram_message_id))
        if gmsg_id is None:
            return None
        user_map = fwd.get(gmsg_id)
        if not user_map:
            return None
        return user_map.get(recipient_user_id)

    def clear_group(self, group_id: int) -> None:
        """Remove all stored reply mappings for a closed group."""
        self._forward.pop(group_id, None)
        self._reverse.pop(group_id, None)

    def clear_all_for_testing(self) -> None:
        """Reset all in-memory buffers for test isolation."""
        self._forward.clear()
        self._reverse.clear()


class GroupSlowModeTracker:
    """Enforces per-sender slow mode inside groups (1.5s text, 4s media) and tracks album rejections."""

    def __init__(self) -> None:
        # (group_id, user_id) -> last_text_timestamp
        self._last_text: Dict[Tuple[int, int], float] = {}
        # (group_id, user_id) -> last_media_timestamp
        self._last_media: Dict[Tuple[int, int], float] = {}
        # Set of (group_id, user_id, media_group_id) already notified
        self._notified_albums: OrderedDict[Tuple[int, int, str], float] = OrderedDict()

    def check_and_record(
        self,
        group_id: int,
        user_id: int,
        is_media: bool = False,
    ) -> Tuple[bool, float]:
        """Return (allowed, retry_after_seconds)."""
        now = time.monotonic()
        key = (group_id, user_id)
        store = self._last_media if is_media else self._last_text
        threshold = MEDIA_SLOW_MODE_SECONDS if is_media else TEXT_SLOW_MODE_SECONDS

        last_ts = store.get(key, 0.0)
        elapsed = now - last_ts
        if last_ts > 0.0 and elapsed < threshold:
            return False, round(threshold - elapsed, 1)

        store[key] = now
        return True, 0.0

    def should_notify_album_rejection(
        self,
        group_id: int,
        user_id: int,
        media_group_id: str,
    ) -> bool:
        """Return True only for the first message of a media_group_id album burst."""
        key = (group_id, user_id, str(media_group_id))
        if key in self._notified_albums:
            return False
        if len(self._notified_albums) >= 500:
            self._notified_albums.popitem(last=False)
        self._notified_albums[key] = time.monotonic()
        return True

    def clear_for_testing(self) -> None:
        self._last_text.clear()
        self._last_media.clear()
        self._notified_albums.clear()


group_reply_buffer = GroupReplyBuffer()
group_slow_mode = GroupSlowModeTracker()


def format_group_text_message(nickname: str, raw_text: str) -> str:
    """Format a text message with HTML-escaped nickname attribution."""
    safe_nick = html.escape(nickname)
    safe_text = html.escape(raw_text or "")
    return f"<b>{safe_nick}:</b>\n{safe_text}"


def format_group_media_caption(nickname: str, raw_caption: Optional[str] = None) -> str:
    """Format an HTML caption with embedded nickname, safely truncated to 1024 chars."""
    safe_nick = html.escape(nickname)
    header = f"<b>{safe_nick}</b>"
    if not raw_caption or not raw_caption.strip():
        return header

    max_body = TELEGRAM_MAX_CAPTION_LENGTH - len(header) - 5
    trimmed_raw = raw_caption.strip()
    if len(trimmed_raw) > max_body:
        trimmed_raw = trimmed_raw[: max(0, max_body - 3)] + "..."
    safe_caption = html.escape(trimmed_raw)
    result = f"{header}\n{safe_caption}"
    if len(result) > TELEGRAM_MAX_CAPTION_LENGTH:
        # Fallback if HTML entities expanded past 1024
        raw_short = trimmed_raw[: max(0, max_body // 2)] + "..."
        result = f"{header}\n{html.escape(raw_short)}"
    return result


def format_non_captionable_attribution(nickname: str) -> str:
    """Format follow-up reply attribution for stickers and video notes."""
    safe_nick = html.escape(nickname)
    return f"👤 <b>{safe_nick}</b>"


def extract_message_type_and_meta(message: Message) -> Tuple[str, Optional[str], Optional[str], bool]:
    """Determine (msg_type, text_content_for_db, telegram_file_id, is_media) from an incoming Message."""
    if getattr(message, "text", None):
        return "text", message.text, None, False

    caption = getattr(message, "caption", None)
    if getattr(message, "photo", None):
        file_id = message.photo[-1].file_id
        content = f'[Photo, caption: "{caption}"]' if caption else "[Photo]"
        return "photo", content, file_id, True

    if getattr(message, "video", None):
        file_id = message.video.file_id
        content = f'[Video, caption: "{caption}"]' if caption else "[Video]"
        return "video", content, file_id, True

    if getattr(message, "animation", None):
        file_id = message.animation.file_id
        content = f'[GIF, caption: "{caption}"]' if caption else "[GIF]"
        return "animation", content, file_id, True

    if getattr(message, "sticker", None):
        file_id = message.sticker.file_id
        emoji = getattr(message.sticker, "emoji", "") or ""
        content = f"[Sticker {emoji}]".strip()
        return "sticker", content, file_id, True

    if getattr(message, "voice", None):
        file_id = message.voice.file_id
        content = f'[Voice, caption: "{caption}"]' if caption else "[Voice]"
        return "voice", content, file_id, True

    if getattr(message, "video_note", None):
        file_id = message.video_note.file_id
        return "video_note", "[Video Note]", file_id, True

    if getattr(message, "audio", None):
        file_id = message.audio.file_id
        content = f'[Audio, caption: "{caption}"]' if caption else "[Audio]"
        return "audio", content, file_id, True

    if getattr(message, "document", None):
        file_id = message.document.file_id
        content = f'[Document, caption: "{caption}"]' if caption else "[Document]"
        return "document", content, file_id, True

    return "unknown", None, None, False


async def _send_with_retry_after(coro_factory):
    """Execute a Telegram send coroutine factory, respecting RetryAfter (429) once."""
    try:
        return await coro_factory()
    except TelegramRetryAfter as e:
        wait_sec = min(float(getattr(e, "retry_after", 1.0) or 1.0), 5.0)
        await asyncio.sleep(wait_sec)
        return await coro_factory()


async def broadcast_group_system_message(
    bot: Bot,
    session: AsyncSession,
    group_id: int,
    html_text: str,
    exclude_user_id: Optional[int] = None,
) -> None:
    """Broadcast a system message (join, leave, AI moderation notice) to all active members of a group."""
    members = await get_active_group_members(session, group_id)
    for m in members:
        if exclude_user_id is not None and m.user_id == exclude_user_id:
            continue
        try:
            await _send_with_retry_after(
                lambda uid=m.user_id: bot.send_message(
                    chat_id=uid,
                    text=html_text,
                    parse_mode="HTML",
                )
            )
        except (TelegramForbiddenError, TelegramBadRequest) as e:
            err_msg = str(e).lower()
            if isinstance(e, TelegramForbiddenError) or any(
                tok in err_msg for tok in ("chat not found", "user is deactivated", "bot was blocked", "forbidden")
            ):
                logger.info("Deactivating unreachable group member %s in group %s: %s", m.user_id, group_id, e)
                res = await leave_group_chat(session, user_id=m.user_id, group_id=group_id)
                if res and res[3]:
                    group_reply_buffer.clear_group(group_id)
        except Exception as e:
            logger.debug("Error sending system message to member %s in group %s: %s", m.user_id, group_id, e)


async def relay_group_message(
    bot: Bot,
    session: AsyncSession,
    group: GroupChat,
    sender_member: GroupChatMember,
    message: Message,
) -> Tuple[bool, str]:
    """Relay a text or media message from an active group member to all other active members.

    Enforces:
    - Multi-file album rejection (`media_group_id`)
    - Per-sender slow mode (1.5s text, 4s media)
    - Caption-embedded nickname for photo/video/animation/document/audio/voice
    - Two-step `reply_to_message_id` nickname follow-up for sticker/video_note
    - Threaded reply resolution across the 100-message ring buffer
    - Dead-member (`403`) automatic cleanup
    - Rolling 100-message DB persistence
    """
    group_id = group.id
    sender_id = sender_member.user_id
    nickname = sender_member.nickname

    # 1. Reject multi-file albums (media_group_id)
    media_group_id = getattr(message, "media_group_id", None)
    if media_group_id is not None:
        if group_slow_mode.should_notify_album_rejection(group_id, sender_id, str(media_group_id)):
            await message.answer(
                "Please send one photo/video at a time in group chats.",
                parse_mode="HTML",
            )
        return False, "album_rejected"

    msg_type, db_text_content, file_id, is_media = extract_message_type_and_meta(message)
    if msg_type == "unknown":
        await message.answer(
            "⚠️ Unsupported message type in group chat.",
            parse_mode="HTML",
        )
        return False, "unsupported_type"

    # 2. Check per-sender slow mode
    allowed, retry_sec = group_slow_mode.check_and_record(group_id, sender_id, is_media=is_media)
    if not allowed:
        await message.answer(
            f"⏳ <b>Slow mode:</b> Please wait {retry_sec}s before sending another {'media item' if is_media else 'message'}.",
            parse_mode="HTML",
        )
        return False, "slow_mode"

    # 3. Record message in DB (rolling 100 window)
    db_msg = await record_group_chat_message(
        session=session,
        group_id=group_id,
        sender_user_id=sender_id,
        sender_nickname=nickname,
        msg_type=msg_type,
        text_content=db_text_content,
        telegram_file_id=file_id,
    )

    # 4. Record sender's own Telegram message_id in the reply buffer
    sender_tg_msg_id = getattr(message, "message_id", None)
    if sender_tg_msg_id is not None:
        group_reply_buffer.record_delivery(group_id, db_msg.id, sender_id, sender_tg_msg_id)

    replied_to = getattr(message, "reply_to_message", None)
    replied_tg_id = getattr(replied_to, "message_id", None) if replied_to else None

    # 5. Fetch active recipients
    active_members = await get_active_group_members(session, group_id)
    recipients = [m for m in active_members if m.user_id != sender_id]

    for recipient in recipients:
        r_uid = recipient.user_id
        reply_to_msg_id: Optional[int] = None
        if replied_tg_id is not None:
            reply_to_msg_id = group_reply_buffer.resolve_reply_target(
                group_id=group_id,
                sender_user_id=sender_id,
                replied_telegram_message_id=replied_tg_id,
                recipient_user_id=r_uid,
            )

        try:
            sent_msg: Optional[Message] = None
            if msg_type == "text":
                formatted_text = format_group_text_message(nickname, message.text or "")
                sent_msg = await _send_with_retry_after(
                    lambda uid=r_uid, txt=formatted_text, r_id=reply_to_msg_id: bot.send_message(
                        chat_id=uid,
                        text=txt,
                        parse_mode="HTML",
                        reply_to_message_id=r_id,
                    )
                )
            elif msg_type in ("photo", "video", "animation", "document", "audio", "voice"):
                caption_html = format_group_media_caption(nickname, getattr(message, "caption", None))
                if msg_type == "photo":
                    sent_msg = await _send_with_retry_after(
                        lambda uid=r_uid, fid=file_id, cap=caption_html, r_id=reply_to_msg_id: bot.send_photo(
                            chat_id=uid,
                            photo=fid,
                            caption=cap,
                            parse_mode="HTML",
                            reply_to_message_id=r_id,
                        )
                    )
                elif msg_type == "video":
                    sent_msg = await _send_with_retry_after(
                        lambda uid=r_uid, fid=file_id, cap=caption_html, r_id=reply_to_msg_id: bot.send_video(
                            chat_id=uid,
                            video=fid,
                            caption=cap,
                            parse_mode="HTML",
                            reply_to_message_id=r_id,
                        )
                    )
                elif msg_type == "animation":
                    sent_msg = await _send_with_retry_after(
                        lambda uid=r_uid, fid=file_id, cap=caption_html, r_id=reply_to_msg_id: bot.send_animation(
                            chat_id=uid,
                            animation=fid,
                            caption=cap,
                            parse_mode="HTML",
                            reply_to_message_id=r_id,
                        )
                    )
                elif msg_type == "document":
                    sent_msg = await _send_with_retry_after(
                        lambda uid=r_uid, fid=file_id, cap=caption_html, r_id=reply_to_msg_id: bot.send_document(
                            chat_id=uid,
                            document=fid,
                            caption=cap,
                            parse_mode="HTML",
                            reply_to_message_id=r_id,
                        )
                    )
                elif msg_type == "audio":
                    sent_msg = await _send_with_retry_after(
                        lambda uid=r_uid, fid=file_id, cap=caption_html, r_id=reply_to_msg_id: bot.send_audio(
                            chat_id=uid,
                            audio=fid,
                            caption=cap,
                            parse_mode="HTML",
                            reply_to_message_id=r_id,
                        )
                    )
                elif msg_type == "voice":
                    sent_msg = await _send_with_retry_after(
                        lambda uid=r_uid, fid=file_id, cap=caption_html, r_id=reply_to_msg_id: bot.send_voice(
                            chat_id=uid,
                            voice=fid,
                            caption=cap,
                            parse_mode="HTML",
                            reply_to_message_id=r_id,
                        )
                    )
            elif msg_type in ("sticker", "video_note"):
                if msg_type == "sticker":
                    sent_msg = await _send_with_retry_after(
                        lambda uid=r_uid, fid=file_id, r_id=reply_to_msg_id: bot.send_sticker(
                            chat_id=uid,
                            sticker=fid,
                            reply_to_message_id=r_id,
                        )
                    )
                else:
                    sent_msg = await _send_with_retry_after(
                        lambda uid=r_uid, fid=file_id, r_id=reply_to_msg_id: bot.send_video_note(
                            chat_id=uid,
                            video_note=fid,
                            reply_to_message_id=r_id,
                        )
                    )

                if sent_msg is not None and getattr(sent_msg, "message_id", None) is not None:
                    tag_text = format_non_captionable_attribution(nickname)
                    tag_msg = await _send_with_retry_after(
                        lambda uid=r_uid, txt=tag_text, m_id=sent_msg.message_id: bot.send_message(
                            chat_id=uid,
                            text=txt,
                            parse_mode="HTML",
                            reply_to_message_id=m_id,
                        )
                    )
                    if tag_msg is not None and getattr(tag_msg, "message_id", None) is not None:
                        group_reply_buffer.record_delivery(group_id, db_msg.id, r_uid, tag_msg.message_id)

            if sent_msg is not None and getattr(sent_msg, "message_id", None) is not None:
                group_reply_buffer.record_delivery(group_id, db_msg.id, r_uid, sent_msg.message_id)

        except (TelegramForbiddenError, TelegramBadRequest) as e:
            err_msg = str(e).lower()
            if isinstance(e, TelegramForbiddenError) or any(
                tok in err_msg for tok in ("chat not found", "user is deactivated", "bot was blocked", "forbidden")
            ):
                logger.info(
                    "Auto-deactivating dead/blocked group member %s in group %s: %s",
                    r_uid,
                    group_id,
                    e,
                )
                leave_res = await leave_group_chat(session, user_id=r_uid, group_id=group_id)
                if leave_res and leave_res[3]:
                    group_reply_buffer.clear_group(group_id)
            else:
                logger.debug("BadRequest relaying to %s in group %s: %s", r_uid, group_id, e)
        except TelegramAPIError as e:
            logger.warning("TelegramAPIError relaying to %s in group %s: %s", r_uid, group_id, e)

    return True, ""


async def check_and_close_inactive_group_chats(
    bot: Bot,
    session: AsyncSession,
    inactivity_seconds: int = GROUP_INACTIVITY_TIMEOUT_SECONDS,
) -> int:
    """Close any active group chats with no messages for 3 hours and notify remaining members."""
    inactive_groups = await get_inactive_group_chats_for_closure(
        session,
        inactivity_seconds=inactivity_seconds,
    )
    closed_count = 0
    for grp in inactive_groups:
        try:
            safe_name = html.escape(grp.name)
            notified_uids = await close_group_chat_by_id(session, grp.id)
            group_reply_buffer.clear_group(grp.id)
            closed_count += 1

            closure_msg = (
                f"⏳ <b>Group Chat Closed Due to Inactivity</b> 🎭\n\n"
                f"The group <b>{safe_name}</b> had no messages for 3 hours and was automatically closed.\n\n"
                "Send /groups to join or create another group chat, or /search for a 1-on-1 chat!"
            )
            for uid in notified_uids:
                try:
                    await bot.send_message(
                        chat_id=uid,
                        text=closure_msg,
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
        except Exception as e:
            logger.error("Failed to auto-close inactive group chat %s: %s", grp.id, e)

    return closed_count
