"""In-memory cache for banned user IDs to enable zero-latency ban enforcement."""

import logging
from typing import Set
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud import get_all_banned_user_ids

logger = logging.getLogger(__name__)

_banned_user_ids: Set[int] = set()


async def sync_banned_users(session: AsyncSession) -> int:
    """Load all banned user IDs from database into memory."""
    global _banned_user_ids
    ids = await get_all_banned_user_ids(session)
    _banned_user_ids = set(ids)
    logger.info("Loaded %d banned user ID(s) into memory.", len(_banned_user_ids))
    return len(_banned_user_ids)


def is_user_banned(user_id: int) -> bool:
    """Check if a user is currently banned."""
    return user_id in _banned_user_ids


def add_banned_user(user_id: int) -> None:
    """Add a user ID to the in-memory ban set."""
    _banned_user_ids.add(user_id)


def remove_banned_user(user_id: int) -> None:
    """Remove a user ID from the in-memory ban set."""
    _banned_user_ids.discard(user_id)


def get_banned_count() -> int:
    """Return total count of banned users."""
    return len(_banned_user_ids)


def clear_bans_for_testing() -> None:
    """Clear in-memory banned set (for testing purposes)."""
    _banned_user_ids.clear()
