"""Custom filter to check if an incoming event comes from a bot administrator."""

from aiogram.filters import BaseFilter
from aiogram.types import TelegramObject
from app.config import settings


class IsAdminFilter(BaseFilter):
    """Filter that restricts handler execution to users whose Telegram ID is in settings.ADMIN_IDS."""

    async def __call__(self, event: TelegramObject) -> bool:
        user = getattr(event, "from_user", None)
        if not user:
            return False
        return user.id in settings.ADMIN_IDS
