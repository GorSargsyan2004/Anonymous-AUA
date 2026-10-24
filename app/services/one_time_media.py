"""Service managing ephemeral (view-once) media, screenshot protection, and auto-destruction."""

import asyncio
from datetime import datetime, timezone
import logging
from typing import Dict, List, Optional, Set
import uuid

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

logger = logging.getLogger(__name__)

OTM_DURATION_SECONDS = 15


class OneTimeMediaManager:
    """Manages view-once tokens, user ephemeral mode toggles, and countdown cleanup."""

    def __init__(self) -> None:
        self._tokens: Dict[str, dict] = {}
        self._user_modes: Set[int] = set()

    def is_user_mode_enabled(self, user_id: int) -> bool:
        """Check if user has /onetime mode toggled ON."""
        return user_id in self._user_modes

    def toggle_user_mode(self, user_id: int) -> bool:
        """Toggle /onetime mode for user. Returns new active boolean state."""
        if user_id in self._user_modes:
            self._user_modes.remove(user_id)
            return False
        else:
            self._user_modes.add(user_id)
            return True

    def disable_user_mode(self, user_id: int) -> None:
        """Disable /onetime mode for user."""
        self._user_modes.discard(user_id)

    def is_one_time_media(self, message: Message, user_id: int) -> bool:
        """Check if an incoming media message is designated as one-time media."""
        # Must contain visual or audio media
        has_media = bool(
            getattr(message, "photo", None)
            or getattr(message, "video", None)
            or getattr(message, "video_note", None)
            or getattr(message, "voice", None)
            or getattr(message, "animation", None)
            or getattr(message, "document", None)
        )
        if not has_media:
            return False

        # Trigger 1: User has /onetime mode toggled ON
        if self.is_user_mode_enabled(user_id):
            return True

        # Trigger 2: Native Telegram media spoiler
        if getattr(message, "has_media_spoiler", False) is True:
            return True

        # Trigger 3: Caption contains view-once tag
        caption = getattr(message, "caption", None) or ""
        caption_lower = caption.lower()
        if any(tag in caption_lower for tag in ("#once", "/once", "#onetime", "/onetime", "#viewonce", "/viewonce")):
            return True

        return False

    def clean_caption(self, caption: Optional[str]) -> Optional[str]:
        """Strip view-once tags from visible caption."""
        if not caption:
            return None
        import re
        text = re.sub(r"(?i)(#onetime|/onetime|#viewonce|/viewonce|#once|/once)", "", caption).strip()
        return text if text else None

    def register(
        self,
        sender_id: int,
        receiver_id: int,
        session_id: Optional[int],
        message: Message,
        caption: Optional[str] = None,
        batch: Optional[List[Message]] = None,
    ) -> str:
        """Register a pending one-time media item and return a unique token."""
        token = uuid.uuid4().hex[:12]
        self._tokens[token] = {
            "token": token,
            "sender_id": sender_id,
            "receiver_id": receiver_id,
            "session_id": session_id,
            "message": message,
            "batch": batch,
            "caption": caption,
            "placeholder_msg_id": None,
            "viewed": False,
            "created_at": datetime.now(timezone.utc),
        }
        return token

    def set_placeholder_msg_id(self, token: str, message_id: int) -> None:
        """Store the placeholder message ID in token metadata."""
        data = self._tokens.get(token)
        if data:
            data["placeholder_msg_id"] = message_id

    def get(self, token: str) -> Optional[dict]:
        """Retrieve token data."""
        return self._tokens.get(token)

    def mark_viewed(self, token: str) -> Optional[dict]:
        """Mark token as viewed."""
        data = self._tokens.get(token)
        if data and not data["viewed"]:
            data["viewed"] = True
            return data
        return None

    def clear_token(self, token: str) -> None:
        """Remove a token."""
        self._tokens.pop(token, None)

    def clear_session(self, session_id: Optional[int]) -> None:
        """Clean up all pending tokens belonging to a session."""
        if not session_id:
            return
        to_del = [tok for tok, d in self._tokens.items() if d.get("session_id") == session_id]
        for tok in to_del:
            self._tokens.pop(tok, None)


one_time_media_manager = OneTimeMediaManager()


def get_otm_prompt_keyboard(token: str) -> InlineKeyboardMarkup:
    """Return inline keyboard prompting partner to reveal one-time media."""
    builder = InlineKeyboardBuilder()
    builder.button(text="👁️ Tap to Reveal (15s)", callback_data=f"otm_rev:{token}")
    return builder.as_markup()


def get_otm_viewing_keyboard() -> InlineKeyboardMarkup:
    """Return disabled status keyboard while media is being viewed."""
    builder = InlineKeyboardBuilder()
    builder.button(text="⏳ Self-destructing in 15s...", callback_data="otm_noop")
    return builder.as_markup()
