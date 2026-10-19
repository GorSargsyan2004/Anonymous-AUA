"""In-memory bidirectional reply tracking service for 1-to-1 anonymous chat sessions."""

from collections import deque
import logging
from typing import Dict, Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Maximum message mappings retained per active conversation pair (FIFO eviction)
MAX_MESSAGES_PER_CHAT = 400


class ChatReplyMap:
    """Bounded, bidirectional mapping of message IDs between two chatting users."""

    def __init__(self, max_messages: int = MAX_MESSAGES_PER_CHAT) -> None:
        self.max_messages = max_messages
        # Queue to track insertion order for FIFO eviction: (sender_id, sender_msg_id, receiver_id, receiver_msg_id)
        self.history: deque[Tuple[int, int, int, int]] = deque()
        # Maps (receiver_id, receiver_msg_id) -> sender_msg_id (when replying to partner's message)
        self.receiver_to_sender: Dict[Tuple[int, int], int] = {}
        # Maps (sender_id, sender_msg_id) -> receiver_msg_id (when quoting own previous message)
        self.sender_to_receiver: Dict[Tuple[int, int], int] = {}

    def register(
        self,
        sender_id: int,
        sender_msg_id: int,
        receiver_id: int,
        receiver_msg_id: int,
    ) -> None:
        """Register a relayed message pair between sender and receiver."""
        # Evict oldest entry if at capacity
        if len(self.history) >= self.max_messages:
            oldest = self.history.popleft()
            o_s_id, o_s_msg_id, o_r_id, o_r_msg_id = oldest
            self.receiver_to_sender.pop((o_r_id, o_r_msg_id), None)
            self.sender_to_receiver.pop((o_s_id, o_s_msg_id), None)

        entry = (sender_id, sender_msg_id, receiver_id, receiver_msg_id)
        self.history.append(entry)
        self.receiver_to_sender[(receiver_id, receiver_msg_id)] = sender_msg_id
        self.sender_to_receiver[(sender_id, sender_msg_id)] = receiver_msg_id

    def get_target_message_id(self, user_id: int, replied_msg_id: int) -> Optional[int]:
        """Given user_id and the replied_msg_id in user_id's chat, find the corresponding message ID in partner's chat.

        1. Check if user_id received replied_msg_id from partner (reply to partner).
        2. Check if user_id was the sender of replied_msg_id (quote own previous message).
        """
        # Case 1: user_id received this message from partner
        if (user_id, replied_msg_id) in self.receiver_to_sender:
            return self.receiver_to_sender[(user_id, replied_msg_id)]

        # Case 2: user_id sent this message earlier (self-quote)
        if (user_id, replied_msg_id) in self.sender_to_receiver:
            return self.sender_to_receiver[(user_id, replied_msg_id)]

        return None

    def clear(self) -> None:
        """Clear all stored message mappings."""
        self.history.clear()
        self.receiver_to_sender.clear()
        self.sender_to_receiver.clear()


class ReplyManager:
    """Manages active conversation reply maps keyed by user pair with PostgreSQL persistence."""

    def __init__(self) -> None:
        # Key: (min_user_id, max_user_id) -> ChatReplyMap
        self._chats: Dict[Tuple[int, int], ChatReplyMap] = {}

    @staticmethod
    def _get_chat_key(user1_id: int, user2_id: int) -> Tuple[int, int]:
        return (min(user1_id, user2_id), max(user1_id, user2_id))

    def register_message_in_memory(
        self,
        sender_id: int,
        sender_msg_id: int,
        receiver_id: int,
        receiver_msg_id: int,
    ) -> None:
        key = self._get_chat_key(sender_id, receiver_id)
        if key not in self._chats:
            self._chats[key] = ChatReplyMap()
        self._chats[key].register(
            sender_id=sender_id,
            sender_msg_id=sender_msg_id,
            receiver_id=receiver_id,
            receiver_msg_id=receiver_msg_id,
        )

    # Alias for synchronous testing
    register_message_sync = register_message_in_memory

    def get_target_message_id_sync(
        self,
        sender_id: int,
        partner_id: int,
        replied_msg_id: int,
    ) -> Optional[int]:
        """Synchronously lookup target message ID from in-memory cache."""
        key = self._get_chat_key(sender_id, partner_id)
        chat_map = self._chats.get(key)
        if not chat_map:
            return None
        return chat_map.get_target_message_id(user_id=sender_id, replied_msg_id=replied_msg_id)

    async def register_message(
        self,
        sender_id: int,
        sender_msg_id: int,
        receiver_id: int,
        receiver_msg_id: int,
        session: Optional[AsyncSession] = None,
        session_id: Optional[int] = None,
    ) -> None:
        """Record a relayed message mapping between sender and receiver in memory and PostgreSQL."""
        self.register_message_in_memory(sender_id, sender_msg_id, receiver_id, receiver_msg_id)
        if session and session_id:
            try:
                from app.database.crud import save_chat_message_mapping
                await save_chat_message_mapping(
                    session=session,
                    session_id=session_id,
                    sender_id=sender_id,
                    sender_msg_id=sender_msg_id,
                    receiver_id=receiver_id,
                    receiver_msg_id=receiver_msg_id,
                )
            except Exception as e:
                logger.warning("Could not persist message mapping to DB: %s", e)

    async def get_target_message_id(
        self,
        sender_id: int,
        partner_id: int,
        replied_msg_id: int,
        session: Optional[AsyncSession] = None,
    ) -> Optional[int]:
        """Find corresponding message ID in partner's chat for a given reply.

        Checks fast in-memory map first; falls back to PostgreSQL on cache miss (e.g. after restart).
        """
        # 1. Fast in-memory lookup
        target = self.get_target_message_id_sync(sender_id, partner_id, replied_msg_id)
        if target is not None:
            return target

        # 2. Database lookup on cache miss (survives container restarts)
        if session:
            try:
                from app.database.crud import get_mapped_target_message_id_from_db
                db_target = await get_mapped_target_message_id_from_db(
                    session=session,
                    sender_id=sender_id,
                    partner_id=partner_id,
                    replied_msg_id=replied_msg_id,
                )
                if db_target is not None:
                    # Warm the in-memory cache symmetrically for both users
                    key = self._get_chat_key(sender_id, partner_id)
                    if key not in self._chats:
                        self._chats[key] = ChatReplyMap()
                    cm = self._chats[key]
                    cm.receiver_to_sender[(sender_id, replied_msg_id)] = db_target
                    cm.sender_to_receiver[(sender_id, replied_msg_id)] = db_target
                    cm.receiver_to_sender[(partner_id, db_target)] = replied_msg_id
                    cm.sender_to_receiver[(partner_id, db_target)] = replied_msg_id
                    return db_target
            except Exception as e:
                logger.warning("Error querying message mapping from DB: %s", e)

        return None

    async def clear_chat(
        self,
        user1_id: int,
        user2_id: int,
        session: Optional[AsyncSession] = None,
        session_id: Optional[int] = None,
    ) -> None:
        """Remove and clean up the reply mapping when a chat session terminates."""
        key = self._get_chat_key(user1_id, user2_id)
        chat_map = self._chats.pop(key, None)
        if chat_map:
            chat_map.clear()
            logger.debug("Cleared in-memory reply mapping for chat pair %s", key)

        if session and session_id:
            try:
                from app.database.crud import delete_chat_message_mappings
                await delete_chat_message_mappings(session=session, session_id=session_id)
            except Exception as e:
                logger.warning("Error clearing DB message mappings for session %s: %s", session_id, e)

    def clear_chat_sync(self, user1_id: int, user2_id: int) -> None:
        """Synchronously clear in-memory mapping."""
        key = self._get_chat_key(user1_id, user2_id)
        chat_map = self._chats.pop(key, None)
        if chat_map:
            chat_map.clear()

    def active_chat_count(self) -> int:
        """Return the number of active tracked chat pairs."""
        return len(self._chats)


# Global singleton instance
reply_manager = ReplyManager()

