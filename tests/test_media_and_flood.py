"""Unit and integration tests for media grouping, file size guardrails, and anti-flood protection."""

import asyncio
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import Message, PhotoSize, Video, Document

from app.config import settings
from app.handlers import matchmaking as mm
from app.services.media_batcher import MediaGroupBatcher
from app.services.rate_limiter import TokenBucketRateLimiter
from app.services.reply_manager import ReplyManager


class TestRateLimiter(unittest.TestCase):
    """Test token bucket rate limiter logic, burst capacity, and cooldown penalties."""

    def setUp(self) -> None:
        self.limiter = TokenBucketRateLimiter(
            capacity=4.0,
            refill_rate=1.0,
            penalty_threshold=3,
            penalty_window=10.0,
            cooldown_duration=30.0,
            warning_debounce=1.0,
        )

    def test_burst_allowance(self) -> None:
        """User can send messages up to capacity burst without hindrance."""
        user_id = 1001
        for i in range(4):
            allowed, reason, _, _ = self.limiter.check(user_id)
            self.assertTrue(allowed, f"Burst message {i} should be allowed")
            self.assertIsNone(reason)

    def test_rate_exceeded(self) -> None:
        """Exceeding burst capacity returns rate_exceeded."""
        user_id = 1002
        for _ in range(4):
            self.limiter.check(user_id)
        allowed, reason, retry_after, should_notify = self.limiter.check(user_id)
        self.assertFalse(allowed)
        self.assertEqual(reason, "rate_exceeded")
        self.assertGreater(retry_after, 0)
        self.assertTrue(should_notify)

    def test_warning_debounce(self) -> None:
        """Flooding repeatedly only produces notification once per debounce period."""
        user_id = 1003
        for _ in range(4):
            self.limiter.check(user_id)

        # 1st violation: should notify
        _, _, _, notify1 = self.limiter.check(user_id)
        self.assertTrue(notify1)

        # 2nd rapid violation: debounced
        _, _, _, notify2 = self.limiter.check(user_id)
        self.assertFalse(notify2)

    def test_cooldown_penalty_trigger(self) -> None:
        """3 violations within penalty window trigger temporary cooldown penalty."""
        user_id = 1004
        # Drain bucket
        for _ in range(4):
            self.limiter.check(user_id)

        # Trigger violations
        self.limiter.check(user_id)  # violation 1
        self.limiter.check(user_id)  # violation 2
        allowed, reason, retry_after, should_notify = self.limiter.check(user_id)  # violation 3 -> triggers cooldown

        self.assertFalse(allowed)
        self.assertEqual(reason, "cooldown_triggered")
        self.assertGreaterEqual(retry_after, 29.0)

        # Subsequent attempts are in cooldown
        allowed2, reason2, retry_after2, _ = self.limiter.check(user_id)
        self.assertFalse(allowed2)
        self.assertEqual(reason2, "cooldown")

    def test_is_in_cooldown_and_reset(self) -> None:
        """Verify cooldown query helper and reset logic."""
        user_id = 1005
        # Force cooldown
        self.limiter._get_or_create_bucket(user_id).cooldown_until = time.time() + 60.0
        in_cooldown, remaining = self.limiter.is_in_cooldown(user_id)
        self.assertTrue(in_cooldown)
        self.assertGreater(remaining, 50.0)

        self.limiter.reset(user_id)
        in_cooldown_after, _ = self.limiter.is_in_cooldown(user_id)
        self.assertFalse(in_cooldown_after)

    def test_prune_stale(self) -> None:
        """Pruning stale buckets frees idle user records."""
        self.limiter._get_or_create_bucket(2001).last_update = time.time() - 4000.0
        self.limiter._get_or_create_bucket(2002).last_update = time.time()
        pruned = self.limiter.prune_stale(max_idle_seconds=3600.0)
        self.assertEqual(pruned, 1)
        self.assertNotIn(2001, self.limiter._buckets)
        self.assertIn(2002, self.limiter._buckets)


class TestMediaGroupBatcher(unittest.IsolatedAsyncioTestCase):
    """Test album aggregation and debouncing."""

    async def test_batcher_groups_messages(self) -> None:
        """Verify messages sharing media_group_id are returned as a single sorted batch."""
        batcher = MediaGroupBatcher(debounce_delay=0.05)
        group_id = "test_album_123"

        msg1 = MagicMock(spec=Message, message_id=101)
        msg2 = MagicMock(spec=Message, message_id=103)
        msg3 = MagicMock(spec=Message, message_id=102)

        # Launch concurrent additions
        t1 = asyncio.create_task(batcher.add_message(group_id, msg1))
        await asyncio.sleep(0.01)
        t2 = asyncio.create_task(batcher.add_message(group_id, msg2))
        t3 = asyncio.create_task(batcher.add_message(group_id, msg3))

        res1, res2, res3 = await asyncio.gather(t1, t2, t3)

        # Leader returns full sorted batch
        self.assertIsNotNone(res1)
        self.assertEqual(len(res1), 3)
        self.assertEqual([m.message_id for m in res1], [101, 102, 103])

        # Followers return None
        self.assertIsNone(res2)
        self.assertIsNone(res3)

        # Storage is cleaned up
        self.assertFalse(batcher.is_collecting(group_id))


class TestMediaGuardrailsAndRelay(unittest.IsolatedAsyncioTestCase):
    """Test media guardrails, rate limit enforcement in handler, and album relay."""

    def setUp(self) -> None:
        self.bot = AsyncMock()
        self.state = AsyncMock()
        self.state.get_state.return_value = None
        # Clean rate limiter and reply manager for tests
        mm.rate_limiter.reset(5555)
        mm.rate_limiter.reset(6666)
        mm.reply_manager.clear_chat_sync(5555, 6666)
        mm.session_buffer.clear_for_testing()

    def tearDown(self) -> None:
        mm.reply_manager.clear_chat_sync(5555, 6666)
        mm.session_buffer.clear_for_testing()


    def test_get_message_file_size_helper(self) -> None:
        """Verify get_message_file_size extracts largest photo size and video size."""
        # Photo message
        p1 = MagicMock(spec=PhotoSize, file_size=1000)
        p2 = MagicMock(spec=PhotoSize, file_size=500000)
        photo_msg = MagicMock(spec=Message, photo=[p1, p2], video=None, document=None, audio=None, voice=None, video_note=None, animation=None)
        self.assertEqual(mm.get_message_file_size(photo_msg), 500000)

        # Video message
        vid = MagicMock(spec=Video, file_size=20000000)
        vid_msg = MagicMock(spec=Message, photo=None, video=vid, document=None, audio=None, voice=None, video_note=None, animation=None)
        self.assertEqual(mm.get_message_file_size(vid_msg), 20000000)

        # Text message (no media)
        txt_msg = MagicMock(spec=Message, photo=None, video=None, document=None, audio=None, voice=None, video_note=None, animation=None)
        self.assertIsNone(mm.get_message_file_size(txt_msg))

    @patch("app.handlers.matchmaking.get_db_session")
    @patch("app.handlers.matchmaking.get_or_create_user")
    @patch("app.handlers.matchmaking.get_active_session_by_user_id")
    async def test_oversized_file_rejected(self, mock_get_chat, mock_get_user, mock_db_session) -> None:
        """File larger than MAX_MEDIA_FILE_SIZE_MB is rejected with warning and not relayed."""
        mock_user = MagicMock(id=5555, current_partner_id=6666, is_searching=False, show_typing=True)
        mock_get_user.return_value = mock_user
        mock_get_chat.return_value = MagicMock(id=10)

        # 150 MB video message (limit is 100 MB)
        oversized_video = MagicMock(spec=Video, file_size=150 * 1024 * 1024)
        msg = MagicMock(spec=Message)
        msg.answer = AsyncMock()
        msg.copy_to = AsyncMock()
        msg.from_user = MagicMock(id=5555)
        msg.text = None
        msg.media_group_id = None
        msg.reply_to_message = None
        msg.external_reply = None
        msg.photo = None
        msg.video = oversized_video
        msg.document = None
        msg.audio = None
        msg.voice = None
        msg.video_note = None
        msg.animation = None

        await mm.relay_anonymous_message(msg, self.bot, self.state)

        # User was notified of oversized rejection
        msg.answer.assert_awaited_once()
        warning_text = msg.answer.call_args[0][0]
        self.assertIn("File too large", warning_text)
        self.assertIn("150.0 MB", warning_text)

        # Was not sent to partner
        msg.copy_to.assert_not_called()

    @patch("app.handlers.matchmaking.get_db_session")
    @patch("app.handlers.matchmaking.get_or_create_user")
    @patch("app.handlers.matchmaking.get_active_session_by_user_id")
    async def test_album_relayed_via_copy_messages(self, mock_get_chat, mock_get_user, mock_db_session) -> None:
        """Album messages are batched and relayed using bot.copy_messages preserving layout."""
        mock_user = MagicMock(id=5555, current_partner_id=6666, is_searching=False, show_typing=False)
        mock_get_user.return_value = mock_user
        mock_get_chat.return_value = MagicMock(id=10)

        p = MagicMock(spec=PhotoSize, file_size=2000)
        msg1 = MagicMock(spec=Message, message_id=301, media_group_id="album_999", photo=[p], text=None, video=None, document=None, audio=None, voice=None, video_note=None, animation=None)
        msg1.from_user = MagicMock(id=5555)
        msg1.answer = AsyncMock()
        msg1.copy_to = AsyncMock()

        msg2 = MagicMock(spec=Message, message_id=302, media_group_id="album_999", photo=[p], text=None, video=None, document=None, audio=None, voice=None, video_note=None, animation=None)
        msg2.from_user = MagicMock(id=5555)
        msg2.answer = AsyncMock()
        msg2.copy_to = AsyncMock()

        # Mock bot.copy_messages return
        copied_id_1 = MagicMock(message_id=401)
        copied_id_2 = MagicMock(message_id=402)
        self.bot.copy_messages = AsyncMock(return_value=[copied_id_1, copied_id_2])

        # Use shorter debounce for test speed
        original_debounce = mm.media_batcher.debounce_delay
        mm.media_batcher.debounce_delay = 0.05
        try:
            t1 = asyncio.create_task(mm.relay_anonymous_message(msg1, self.bot, self.state))
            await asyncio.sleep(0.01)
            t2 = asyncio.create_task(mm.relay_anonymous_message(msg2, self.bot, self.state))
            await asyncio.gather(t1, t2)
        finally:
            mm.media_batcher.debounce_delay = original_debounce

        # copy_messages was called once with both IDs
        self.bot.copy_messages.assert_awaited_once_with(
            chat_id=6666,
            from_chat_id=5555,
            message_ids=[301, 302],
        )

        # Verify both messages registered in reply_manager
        target_1 = await mm.reply_manager.get_target_message_id(5555, 6666, 301)
        target_2 = await mm.reply_manager.get_target_message_id(5555, 6666, 302)
        self.assertEqual(target_1, 401)
        self.assertEqual(target_2, 402)

    @patch("app.handlers.matchmaking.get_db_session")
    @patch("app.handlers.matchmaking.get_or_create_user")
    @patch("app.handlers.matchmaking.get_active_session_by_user_id")
    async def test_album_fallback_to_individual_copy(self, mock_get_chat, mock_get_user, mock_db_session) -> None:
        """If bot.copy_messages encounters an error, fallback copies each item individually."""
        mock_user = MagicMock(id=5555, current_partner_id=6666, is_searching=False, show_typing=False)
        mock_get_user.return_value = mock_user
        mock_get_chat.return_value = MagicMock(id=10)

        # bot.copy_messages fails with TelegramBadRequest
        self.bot.copy_messages = AsyncMock(side_effect=TelegramBadRequest(method=MagicMock(), message="Method not allowed"))

        p = MagicMock(spec=PhotoSize, file_size=2000)
        msg1 = MagicMock(spec=Message, message_id=501, media_group_id="album_fallback", photo=[p], text=None, video=None, document=None, audio=None, voice=None, video_note=None, animation=None)
        msg1.from_user = MagicMock(id=5555)
        msg1.answer = AsyncMock()
        sent_mock = MagicMock(message_id=601)
        msg1.copy_to = AsyncMock(return_value=sent_mock)

        original_debounce = mm.media_batcher.debounce_delay
        mm.media_batcher.debounce_delay = 0.02
        try:
            await mm.relay_anonymous_message(msg1, self.bot, self.state)
        finally:
            mm.media_batcher.debounce_delay = original_debounce

        # Fallback copy_to was called
        msg1.copy_to.assert_called_once_with(chat_id=6666)


if __name__ == "__main__":
    unittest.main()
