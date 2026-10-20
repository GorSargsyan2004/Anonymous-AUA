"""Middleware that blocks banned users from interacting with the bot."""

import logging
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.services.ban_manager import is_user_banned

logger = logging.getLogger(__name__)


class BannedCheckMiddleware(BaseMiddleware):
    """Outer middleware checking if user is banned by administration."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        from_user = data.get("event_from_user")
        if from_user and is_user_banned(from_user.id):
            logger.debug("Blocked event from banned user %s", from_user.id)
            if isinstance(event, Message):
                await event.answer(
                    "🚫 <b>Access Denied</b>\n\n"
                    "You have been banned by an administrator from using Anonymous AUA.",
                    parse_mode="HTML",
                )
            elif isinstance(event, CallbackQuery):
                await event.answer(
                    "🚫 You have been banned by an administrator.",
                    show_alert=True,
                )
            return None

        return await handler(event, data)
