"""Test suite for anonymous chat replied message support."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import unittest

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message, MessageReactionUpdated, ReactionTypeEmoji, ReplyParameters, User as TgUser

from app.services.reply_manager import ChatReplyMap, ReplyManager, reply_manager


class TestReplySupport(unittest.TestCase):
    """Unit tests for ChatReplyMap and ReplyManager."""

    def setUp(self):
        self.rm = ReplyManager()
        self.user_a = 10001
        self.user_b = 20002

    def tearDown(self):
        self.rm.clear_chat(self.user_a, self.user_b)

    def test_01_basic_reply_to_partner_message(self):
        """User A sends a message; User B replies to it; verify User A's message ID is resolved."""
        # User A sends msg 101 -> received by User B as msg 501
        self.rm.register_message(
            sender_id=self.user_a,
            sender_msg_id=101,
            receiver_id=self.user_b,
            receiver_msg_id=501,
        )

        # User B replies to msg 501 in B's chat
        target_in_a = self.rm.get_target_message_id(
            sender_id=self.user_b,
            partner_id=self.user_a,
            replied_msg_id=501,
        )
        self.assertEqual(target_in_a, 101, "User B's reply to 501 should point to 101 in User A's chat")

    def test_02_bidirectional_multi_turn_replies(self):
        """Test multi-turn back-and-forth conversational replies between User A and User B."""
        # Turn 1: A sends 101 -> B receives 501
        self.rm.register_message(self.user_a, 101, self.user_b, 501)

        # Turn 2: B replies to 501 with message 502 -> A receives 102
        target_a = self.rm.get_target_message_id(self.user_b, self.user_a, 501)
        self.assertEqual(target_a, 101)
        self.rm.register_message(self.user_b, 502, self.user_a, 102)

        # Turn 3: A replies to 102 (B's reply) with message 103 -> B receives 503
        target_b = self.rm.get_target_message_id(self.user_a, self.user_b, 102)
        self.assertEqual(target_b, 502, "User A's reply to 102 should point to 502 in User B's chat")
        self.rm.register_message(self.user_a, 103, self.user_b, 503)

        # Turn 4: B replies to 503 -> target in A should be 103
        target_a_next = self.rm.get_target_message_id(self.user_b, self.user_a, 503)
        self.assertEqual(target_a_next, 103)

    def test_03_self_quote_reply(self):
        """A user replies to their OWN previous message (self-quote). Verify partner receives corresponding msg."""
        # A sends 101 -> B receives 501
        self.rm.register_message(self.user_a, 101, self.user_b, 501)
        # A sends 102 -> B receives 502
        self.rm.register_message(self.user_a, 102, self.user_b, 502)

        # A replies to their own earlier message 101 (in A's chat)
        target_in_b = self.rm.get_target_message_id(
            sender_id=self.user_a,
            partner_id=self.user_b,
            replied_msg_id=101,
        )
        self.assertEqual(target_in_b, 501, "Self-quote of 101 by A should resolve to 501 in B's chat")

        # B replies to their own message 501 (received) or B sends 503 -> A receives 103
        self.rm.register_message(self.user_b, 503, self.user_a, 103)
        # B quotes their own message 503
        target_in_a = self.rm.get_target_message_id(
            sender_id=self.user_b,
            partner_id=self.user_a,
            replied_msg_id=503,
        )
        self.assertEqual(target_in_a, 103, "Self-quote of 503 by B should resolve to 103 in A's chat")

    def test_04_unknown_or_system_reply(self):
        """Replying to a non-relayed message (e.g. bot greeting) returns None."""
        target = self.rm.get_target_message_id(
            sender_id=self.user_a,
            partner_id=self.user_b,
            replied_msg_id=999999,
        )
        self.assertIsNone(target, "Unknown or system message reply should return None")

    def test_05_fifo_capacity_eviction(self):
        """Verify bounded memory with FIFO eviction under high message volume."""
        chat_map = ChatReplyMap(max_messages=3)

        # Register 4 messages (capacity 3)
        chat_map.register(self.user_a, 1, self.user_b, 11)
        chat_map.register(self.user_a, 2, self.user_b, 12)
        chat_map.register(self.user_a, 3, self.user_b, 13)
        chat_map.register(self.user_a, 4, self.user_b, 14)

        # Oldest message (1 -> 11) should be evicted
        self.assertIsNone(chat_map.get_target_message_id(self.user_b, 11))
        self.assertIsNone(chat_map.get_target_message_id(self.user_a, 1))

        # Newer messages (2, 3, 4) must still be present
        self.assertEqual(chat_map.get_target_message_id(self.user_b, 12), 2)
        self.assertEqual(chat_map.get_target_message_id(self.user_b, 13), 3)
        self.assertEqual(chat_map.get_target_message_id(self.user_b, 14), 4)

    def test_06_clear_chat(self):
        """Verify chat termination cleanly drops all mappings and frees memory."""
        self.rm.register_message(self.user_a, 100, self.user_b, 200)
        self.assertEqual(self.rm.active_chat_count(), 1)

        self.rm.clear_chat(self.user_a, self.user_b)
        self.assertEqual(self.rm.active_chat_count(), 0)

        # Looking up now returns None
        target = self.rm.get_target_message_id(self.user_b, self.user_a, 200)
        self.assertIsNone(target)

    def test_07_isolation_between_different_pairs(self):
        """Conversations between (A, B) and (C, D) do not leak or overwrite each other."""
        user_c = 30003
        user_d = 40004

        # A -> B: msg 1 -> msg 10
        self.rm.register_message(self.user_a, 1, self.user_b, 10)
        # C -> D: msg 1 -> msg 99 (same sender_msg_id 1, but different pair)
        self.rm.register_message(user_c, 1, user_d, 99)

        # Lookup in A-B chat
        self.assertEqual(self.rm.get_target_message_id(self.user_b, self.user_a, 10), 1)
        # Lookup in C-D chat
        self.assertEqual(self.rm.get_target_message_id(user_d, user_c, 99), 1)

        self.rm.clear_chat(user_c, user_d)
        self.assertEqual(self.rm.active_chat_count(), 1)
        self.assertEqual(self.rm.get_target_message_id(self.user_b, self.user_a, 10), 1)


class TestAsyncRelayReplyHandler(unittest.IsolatedAsyncioTestCase):
    """Integration test for aiogram message copy with ReplyParameters and emoji reactions."""

    async def test_08_copy_to_with_reply_parameters(self):
        """Verify copy_to call passes ReplyParameters with allow_sending_without_reply=True."""
        reply_manager.clear_chat(111, 222)
        reply_manager.register_message(
            sender_id=111,
            sender_msg_id=77,
            receiver_id=222,
            receiver_msg_id=88,
        )

        # Mock incoming message from user 222 replying to msg 88
        mock_msg = MagicMock(spec=Message)
        mock_msg.message_id = 89
        mock_msg.reply_to_message = MagicMock(spec=Message)
        mock_msg.reply_to_message.message_id = 88
        mock_msg.copy_to = AsyncMock(return_value=MagicMock(message_id=78))

        target_id = reply_manager.get_target_message_id(
            sender_id=222,
            partner_id=111,
            replied_msg_id=mock_msg.reply_to_message.message_id,
        )
        self.assertEqual(target_id, 77)

        reply_params = ReplyParameters(
            message_id=target_id,
            allow_sending_without_reply=True,
        )
        sent = await mock_msg.copy_to(chat_id=111, reply_parameters=reply_params)

        mock_msg.copy_to.assert_awaited_once_with(chat_id=111, reply_parameters=reply_params)
        self.assertEqual(sent.message_id, 78)

        # Register outgoing copy
        reply_manager.register_message(
            sender_id=222,
            sender_msg_id=mock_msg.message_id,
            receiver_id=111,
            receiver_msg_id=sent.message_id,
        )

        # Verify next turn: 111 replies to 78 -> points to 89 in 222's chat
        next_target = reply_manager.get_target_message_id(
            sender_id=111,
            partner_id=222,
            replied_msg_id=78,
        )
        self.assertEqual(next_target, 89)

        reply_manager.clear_chat(111, 222)

    @patch("app.handlers.matchmaking.get_user")
    @patch("app.handlers.matchmaking.get_db_session")
    async def test_09_relay_message_reaction_add(self, mock_get_db, mock_get_user):
        """User A adds emoji reaction to B's message; bot relays reaction onto B's message."""
        user_a = 5001
        user_b = 6002

        reply_manager.clear_chat(user_a, user_b)
        # B sent msg 200, which A received as msg 100
        reply_manager.register_message(
            sender_id=user_b,
            sender_msg_id=200,
            receiver_id=user_a,
            receiver_msg_id=100,
        )

        mock_db_user = MagicMock()
        mock_db_user.current_partner_id = user_b
        mock_get_user.return_value = mock_db_user

        mock_bot = MagicMock(spec=Bot)
        mock_bot.set_message_reaction = AsyncMock(return_value=True)

        reaction = ReactionTypeEmoji(emoji="🔥")
        event = MagicMock(spec=MessageReactionUpdated)
        event.user = MagicMock(spec=TgUser)
        event.user.id = user_a
        event.message_id = 100
        event.new_reaction = [reaction]

        from app.handlers.matchmaking import relay_message_reaction
        await relay_message_reaction(event=event, bot=mock_bot)

        # Target in B's chat for msg 100 in A's chat is 200
        mock_bot.set_message_reaction.assert_awaited_once_with(
            chat_id=user_b,
            message_id=200,
            reaction=[reaction],
        )

        reply_manager.clear_chat(user_a, user_b)

    @patch("app.handlers.matchmaking.get_user")
    @patch("app.handlers.matchmaking.get_db_session")
    async def test_10_relay_message_reaction_remove(self, mock_get_db, mock_get_user):
        """User A removes reaction (new_reaction=[]); bot clears reaction on B's message."""
        user_a = 5001
        user_b = 6002

        reply_manager.clear_chat(user_a, user_b)
        reply_manager.register_message(
            sender_id=user_b,
            sender_msg_id=300,
            receiver_id=user_a,
            receiver_msg_id=150,
        )

        mock_db_user = MagicMock()
        mock_db_user.current_partner_id = user_b
        mock_get_user.return_value = mock_db_user

        mock_bot = MagicMock(spec=Bot)
        mock_bot.set_message_reaction = AsyncMock(return_value=True)

        event = MagicMock(spec=MessageReactionUpdated)
        event.user = MagicMock(spec=TgUser)
        event.user.id = user_a
        event.message_id = 150
        event.new_reaction = []

        from app.handlers.matchmaking import relay_message_reaction
        await relay_message_reaction(event=event, bot=mock_bot)

        mock_bot.set_message_reaction.assert_awaited_once_with(
            chat_id=user_b,
            message_id=300,
            reaction=[],
        )

        reply_manager.clear_chat(user_a, user_b)

    @patch("app.handlers.matchmaking.get_user")
    @patch("app.handlers.matchmaking.get_db_session")
    async def test_11_relay_message_reaction_unmapped_message(self, mock_get_db, mock_get_user):
        """Reaction on unmapped/system message is ignored without calling Telegram API."""
        user_a = 5001
        user_b = 6002

        reply_manager.clear_chat(user_a, user_b)
        mock_db_user = MagicMock()
        mock_db_user.current_partner_id = user_b
        mock_get_user.return_value = mock_db_user

        mock_bot = MagicMock(spec=Bot)
        mock_bot.set_message_reaction = AsyncMock()

        event = MagicMock(spec=MessageReactionUpdated)
        event.user = MagicMock(spec=TgUser)
        event.user.id = user_a
        event.message_id = 999999  # Unmapped message
        event.new_reaction = [ReactionTypeEmoji(emoji="👍")]

        from app.handlers.matchmaking import relay_message_reaction
        await relay_message_reaction(event=event, bot=mock_bot)

        mock_bot.set_message_reaction.assert_not_called()

    @patch("app.handlers.matchmaking.get_user")
    @patch("app.handlers.matchmaking.get_db_session")
    async def test_12_relay_message_reaction_no_active_chat(self, mock_get_db, mock_get_user):
        """Reaction when user is not connected to a partner is cleanly ignored."""
        user_a = 5001
        mock_db_user = MagicMock()
        mock_db_user.current_partner_id = None
        mock_get_user.return_value = mock_db_user

        mock_bot = MagicMock(spec=Bot)
        mock_bot.set_message_reaction = AsyncMock()

        event = MagicMock(spec=MessageReactionUpdated)
        event.user = MagicMock(spec=TgUser)
        event.user.id = user_a
        event.message_id = 100
        event.new_reaction = [ReactionTypeEmoji(emoji="❤️")]

        from app.handlers.matchmaking import relay_message_reaction
        await relay_message_reaction(event=event, bot=mock_bot)

        mock_bot.set_message_reaction.assert_not_called()

    @patch("app.handlers.matchmaking.get_user")
    @patch("app.handlers.matchmaking.get_db_session")
    async def test_13_relay_message_reaction_api_error_handled(self, mock_get_db, mock_get_user):
        """Telegram API errors (e.g. invalid reaction or old msg) are caught and logged without raising."""
        user_a = 5001
        user_b = 6002

        reply_manager.clear_chat(user_a, user_b)
        reply_manager.register_message(
            sender_id=user_b,
            sender_msg_id=400,
            receiver_id=user_a,
            receiver_msg_id=200,
        )

        mock_db_user = MagicMock()
        mock_db_user.current_partner_id = user_b
        mock_get_user.return_value = mock_db_user

        mock_bot = MagicMock(spec=Bot)
        mock_bot.set_message_reaction = AsyncMock(
            side_effect=TelegramBadRequest(method="SetMessageReaction", message="REACTION_INVALID")
        )

        event = MagicMock(spec=MessageReactionUpdated)
        event.user = MagicMock(spec=TgUser)
        event.user.id = user_a
        event.message_id = 200
        event.new_reaction = [ReactionTypeEmoji(emoji="custom")]

        from app.handlers.matchmaking import relay_message_reaction
        # Should not raise exception
        await relay_message_reaction(event=event, bot=mock_bot)

        mock_bot.set_message_reaction.assert_awaited_once()
        reply_manager.clear_chat(user_a, user_b)

    @patch("app.handlers.matchmaking.get_user")
    @patch("app.handlers.matchmaking.get_db_session")
    async def test_14_relay_edited_message_text(self, mock_get_db, mock_get_user):
        """User A edits text message; partner B's corresponding message is edited in real-time."""
        user_a = 7001
        user_b = 8002

        reply_manager.clear_chat(user_a, user_b)
        # User A sent msg 10, which User B received as msg 20
        reply_manager.register_message(
            sender_id=user_a,
            sender_msg_id=10,
            receiver_id=user_b,
            receiver_msg_id=20,
        )

        mock_db_user = MagicMock()
        mock_db_user.current_partner_id = user_b
        mock_get_user.return_value = mock_db_user

        mock_bot = MagicMock(spec=Bot)
        mock_bot.edit_message_text = AsyncMock(return_value=True)

        mock_state = MagicMock()
        mock_state.get_state = AsyncMock(return_value=None)

        msg = MagicMock(spec=Message)
        msg.from_user = MagicMock(spec=TgUser)
        msg.from_user.id = user_a
        msg.message_id = 10
        msg.text = "Hey, let's meet at PAB!"
        msg.entities = []
        msg.caption = None
        msg.photo = None
        msg.video = None
        msg.document = None
        msg.audio = None
        msg.animation = None
        msg.voice = None

        from app.handlers.matchmaking import relay_edited_message
        await relay_edited_message(message=msg, bot=mock_bot, state=mock_state)

        mock_bot.edit_message_text.assert_awaited_once_with(
            text="Hey, let's meet at PAB!",
            chat_id=user_b,
            message_id=20,
            entities=[],
            parse_mode=None,
        )

        reply_manager.clear_chat(user_a, user_b)

    @patch("app.handlers.matchmaking.get_user")
    @patch("app.handlers.matchmaking.get_db_session")
    async def test_15_relay_edited_message_caption(self, mock_get_db, mock_get_user):
        """User A edits media caption; partner B's corresponding media caption is edited."""
        user_a = 7001
        user_b = 8002

        reply_manager.clear_chat(user_a, user_b)
        reply_manager.register_message(
            sender_id=user_a,
            sender_msg_id=11,
            receiver_id=user_b,
            receiver_msg_id=21,
        )

        mock_db_user = MagicMock()
        mock_db_user.current_partner_id = user_b
        mock_get_user.return_value = mock_db_user

        mock_bot = MagicMock(spec=Bot)
        mock_bot.edit_message_caption = AsyncMock(return_value=True)

        mock_state = MagicMock()
        mock_state.get_state = AsyncMock(return_value=None)

        msg = MagicMock(spec=Message)
        msg.from_user = MagicMock(spec=TgUser)
        msg.from_user.id = user_a
        msg.message_id = 11
        msg.text = None
        msg.caption = "Updated photo caption!"
        msg.caption_entities = []
        msg.photo = [MagicMock()]
        msg.video = None
        msg.document = None
        msg.audio = None
        msg.animation = None
        msg.voice = None

        from app.handlers.matchmaking import relay_edited_message
        await relay_edited_message(message=msg, bot=mock_bot, state=mock_state)

        mock_bot.edit_message_caption.assert_awaited_once_with(
            caption="Updated photo caption!",
            chat_id=user_b,
            message_id=21,
            caption_entities=[],
            parse_mode=None,
        )

        reply_manager.clear_chat(user_a, user_b)

    @patch("app.handlers.matchmaking.get_user")
    @patch("app.handlers.matchmaking.get_db_session")
    async def test_16_relay_edited_message_unmapped_ignored(self, mock_get_db, mock_get_user):
        """Editing an unmapped message (e.g. system bot message) is safely ignored."""
        user_a = 7001
        user_b = 8002

        reply_manager.clear_chat(user_a, user_b)
        mock_db_user = MagicMock()
        mock_db_user.current_partner_id = user_b
        mock_get_user.return_value = mock_db_user

        mock_bot = MagicMock(spec=Bot)
        mock_bot.edit_message_text = AsyncMock()
        mock_bot.edit_message_caption = AsyncMock()

        mock_state = MagicMock()
        mock_state.get_state = AsyncMock(return_value=None)

        msg = MagicMock(spec=Message)
        msg.from_user = MagicMock(spec=TgUser)
        msg.from_user.id = user_a
        msg.message_id = 999999
        msg.text = "Some edit"
        msg.entities = []

        from app.handlers.matchmaking import relay_edited_message
        await relay_edited_message(message=msg, bot=mock_bot, state=mock_state)

        mock_bot.edit_message_text.assert_not_called()
        mock_bot.edit_message_caption.assert_not_called()

    @patch("app.handlers.matchmaking.get_user")
    @patch("app.handlers.matchmaking.get_db_session")
    async def test_17_relay_edited_message_no_partner_ignored(self, mock_get_db, mock_get_user):
        """Editing message when user is not in an active chat is ignored."""
        user_a = 7001
        mock_db_user = MagicMock()
        mock_db_user.current_partner_id = None
        mock_get_user.return_value = mock_db_user

        mock_bot = MagicMock(spec=Bot)
        mock_bot.edit_message_text = AsyncMock()

        mock_state = MagicMock()
        mock_state.get_state = AsyncMock(return_value=None)

        msg = MagicMock(spec=Message)
        msg.from_user = MagicMock(spec=TgUser)
        msg.from_user.id = user_a
        msg.message_id = 10
        msg.text = "Hello"

        from app.handlers.matchmaking import relay_edited_message
        await relay_edited_message(message=msg, bot=mock_bot, state=mock_state)

        mock_bot.edit_message_text.assert_not_called()

    @patch("app.handlers.matchmaking.get_user")
    @patch("app.handlers.matchmaking.get_db_session")
    async def test_18_relay_edited_message_not_modified_error_handled(self, mock_get_db, mock_get_user):
        """TelegramBadRequest with 'message is not modified' is caught and handled cleanly."""
        user_a = 7001
        user_b = 8002

        reply_manager.clear_chat(user_a, user_b)
        reply_manager.register_message(
            sender_id=user_a,
            sender_msg_id=12,
            receiver_id=user_b,
            receiver_msg_id=22,
        )

        mock_db_user = MagicMock()
        mock_db_user.current_partner_id = user_b
        mock_get_user.return_value = mock_db_user

        mock_bot = MagicMock(spec=Bot)
        mock_bot.edit_message_text = AsyncMock(
            side_effect=TelegramBadRequest(method="EditMessageText", message="Bad Request: message is not modified")
        )

        mock_state = MagicMock()
        mock_state.get_state = AsyncMock(return_value=None)

        msg = MagicMock(spec=Message)
        msg.from_user = MagicMock(spec=TgUser)
        msg.from_user.id = user_a
        msg.message_id = 12
        msg.text = "Same text"
        msg.entities = []

        from app.handlers.matchmaking import relay_edited_message
        # Should not raise exception
        await relay_edited_message(message=msg, bot=mock_bot, state=mock_state)

        mock_bot.edit_message_text.assert_awaited_once()
        reply_manager.clear_chat(user_a, user_b)


if __name__ == "__main__":
    unittest.main()
