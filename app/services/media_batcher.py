"""Media group (album) batching service.

Collects concurrently arriving messages that share a Telegram media_group_id
and debounces them so they can be processed and forwarded as a single album batch.
"""

import asyncio
from typing import Dict, List, Optional
from aiogram.types import Message


class MediaGroupBatcher:
    """In-memory debouncing collector for Telegram media group messages."""

    def __init__(self, debounce_delay: float = 0.35) -> None:
        """Initialize the batcher.

        Args:
            debounce_delay: Time in seconds to wait for subsequent album items.
        """
        self.debounce_delay: float = debounce_delay
        self._groups: Dict[str, List[Message]] = {}
        self._master_lock = asyncio.Lock()

    def is_collecting(self, group_id: Optional[str]) -> bool:
        """Check if group_id is currently collecting album items."""
        if not group_id:
            return False
        return group_id in self._groups

    async def add_message(self, group_id: str, message: Message) -> Optional[List[Message]]:
        """Add a message to the media group collector.

        Returns:
            List[Message]: The complete sorted list of messages if this invocation
                           is the designated leader for the album.
            None: If this message was collected into an in-flight album handled by a leader.
        """
        async with self._master_lock:
            if group_id in self._groups:
                self._groups[group_id].append(message)
                return None
            self._groups[group_id] = [message]

        # Leader coroutine: wait for remaining album items to arrive
        try:
            await asyncio.sleep(self.debounce_delay)
        finally:
            async with self._master_lock:
                batch = self._groups.pop(group_id, None)

        if batch:
            batch.sort(key=lambda m: m.message_id)
        return batch

    def clear(self) -> None:
        """Clear all active groups (used in testing)."""
        self._groups.clear()


# Global batcher instance
media_batcher = MediaGroupBatcher()
