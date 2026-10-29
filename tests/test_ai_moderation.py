"""Comprehensive test suite for Zero-Knowledge Ephemeral RAM Buffer and Multimodal Gemini AI Moderation."""

import asyncio
from datetime import datetime, timezone
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.database.models import User, ChatSession, Report
from app.services.ai_moderator import (
    ModerationVerdict,
    ProtectedSessionBuffer,
    SessionTurn,
    gemini_moderator,
    session_buffer,
)
from app.handlers import feedback as feedback_handlers
from app.handlers import matchmaking as mm_handlers


class TestProtectedSessionBuffer(unittest.IsolatedAsyncioTestCase):
    """Unit tests for the 4-layer protected in-memory session buffer."""

    def setUp(self) -> None:
        self.buffer = ProtectedSessionBuffer()

    def tearDown(self) -> None:
        self.buffer.clear_for_testing()

    def test_text_truncation_to_max_chars(self) -> None:
        """Text longer than MAX_TEXT_LENGTH (500 chars) is truncated in memory."""
        session_id = 101
        long_text = "A" * 800
        self.buffer.add_text(session_id, user_id=1, text=long_text)

        turns = self.buffer.pop_session(session_id)
        self.assertIsNotNone(turns)
        self.assertEqual(len(turns), 1)
        self.assertEqual(len(turns[0].text), 500)
        self.assertEqual(turns[0].text, "A" * 500)

    def test_circular_queue_maxlen_60(self) -> None:
        """Buffer keeps strictly the last 60 message turns."""
        session_id = 102
        for i in range(75):
            self.buffer.add_text(session_id, user_id=1, text=f"Message {i}")

        turns = self.buffer.pop_session(session_id)
        self.assertIsNotNone(turns)
        self.assertEqual(len(turns), 60)
        self.assertEqual(turns[0].text, "Message 15")
        self.assertEqual(turns[-1].text, "Message 74")

    def test_image_rate_limit_10_seconds(self) -> None:
        """Users can send at most 1 image every 10 seconds."""
        session_id = 103
        user_id = 42

        # First image allowed
        allowed, _ = self.buffer.check_image_rate_limit(session_id, user_id)
        self.assertTrue(allowed)
        self.buffer.add_image(session_id, user_id, b"fake_bytes_1")

        # Immediate second image blocked
        allowed, reason = self.buffer.check_image_rate_limit(session_id, user_id)
        self.assertFalse(allowed)
        self.assertIn("Image cooldown active", reason)

    def test_image_quota_limit_10_per_session(self) -> None:
        """Users can send at most 10 images total per session."""
        session_id = 104
        user_id = 55

        entry = self.buffer._get_or_create(session_id)
        entry.user_image_stats[user_id]["count"] = 10
        entry.user_image_stats[user_id]["last_image_time"] = 0.0

        allowed, reason = self.buffer.check_image_rate_limit(session_id, user_id)
        self.assertFalse(allowed)
        self.assertIn("Image limit reached", reason)

    def test_sliding_window_eviction_max_8_images(self) -> None:
        """When 9th image is added, oldest image bytes are evicted from RAM."""
        session_id = 105
        user_id = 77

        # Add 8 images
        for i in range(8):
            self.buffer.add_image(session_id, user_id, f"img_{i}".encode(), caption=f"Cap {i}")

        turns = self.buffer._sessions[session_id].turns
        self.assertEqual(sum(1 for t in turns if t.image_bytes is not None), 8)
        self.assertFalse(turns[0].had_image_evicted)

        # Add 9th image -> oldest image must be evicted
        self.buffer.add_image(session_id, user_id, b"img_8", caption="Cap 8")

        self.assertEqual(sum(1 for t in turns if t.image_bytes is not None), 8)
        self.assertIsNone(turns[0].image_bytes)
        self.assertTrue(turns[0].had_image_evicted)
        self.assertEqual(turns[0].text, "Cap 0")
        self.assertEqual(turns[-1].image_bytes, b"img_8")

    async def test_auto_destruction_ttl_5_minutes(self) -> None:
        """Buffer is auto-destroyed after TTL expires."""
        session_id = 106
        self.buffer.add_text(session_id, user_id=1, text="Secret chat")
        self.assertTrue(self.buffer.has_session(session_id))

        # Schedule cleanup with small TTL (0.05s) for fast unit testing
        self.buffer.schedule_cleanup(session_id, ttl=0.05)
        await asyncio.sleep(0.08)

        self.assertFalse(self.buffer.has_session(session_id))
        self.assertIsNone(self.buffer.pop_session(session_id))

    def test_pop_session_erases_immediately(self) -> None:
        """pop_session returns turns and wipes the entry atomically from memory."""
        session_id = 107
        self.buffer.add_text(session_id, user_id=1, text="Hello")
        self.assertTrue(self.buffer.has_session(session_id))

        turns = self.buffer.pop_session(session_id)
        self.assertEqual(len(turns), 1)
        self.assertFalse(self.buffer.has_session(session_id))
        self.assertIsNone(self.buffer.pop_session(session_id))


class TestGeminiAIModerationFlow(unittest.IsolatedAsyncioTestCase):
    """Integration tests for AI moderation reporting flow and accountability."""

    def setUp(self) -> None:
        session_buffer.clear_for_testing()

    def tearDown(self) -> None:
        session_buffer.clear_for_testing()

    @patch("app.handlers.feedback.get_db_session")
    @patch("app.handlers.feedback.add_or_update_user_block")
    @patch("app.handlers.feedback.apply_admin_penalty")
    @patch("app.handlers.feedback.create_report")
    @patch("app.handlers.feedback.has_report_for_session", return_value=False)
    @patch("app.handlers.feedback.gemini_moderator")
    async def test_reported_user_penalized_for_harassment(
        self, mock_gemini, mock_has_rep, mock_create_rep, mock_penalty, mock_block, mock_db_session
    ) -> None:
        """Reported user is penalized and blocked when found guilty of harassment."""
        session_id = 201
        reporter_id = 1001
        reported_id = 2002

        session_buffer.add_text(session_id, reporter_id, "Hi there")
        session_buffer.add_text(session_id, reported_id, "You are stupid and ugly")

        from app.services.ai_moderator import ModerationVerdict
        mock_gemini.evaluate_session = AsyncMock(return_value=ModerationVerdict(
            guilty_party="Reported_User",
            severity_category="Severe_Harassment_NSFW",
            reported_user_penalty=6,
            reporter_penalty=0,
            block_reported_user=True,
            block_reporter=False,
            reveal_identity_for_illegal_content=False,
            illegal_perpetrator="None",
            anonymized_summary="Reported user engaged in personal verbal harassment.",
        ))

        bot = AsyncMock()
        state = AsyncMock()
        state.get_data.return_value = {"session_id": session_id, "reported_id": reported_id}

        msg = AsyncMock()
        msg.text = "He called me ugly and insulted me"
        msg.from_user = MagicMock(id=reporter_id)

        await feedback_handlers.handle_report_reason(msg, bot, state)

        # Verify priority penalty applied to reported user
        mock_penalty.assert_awaited_once_with(unittest.mock.ANY, user_id=reported_id, penalty=6)

        # Verify Zero-Knowledge: reporter_id and reported_id are None
        mock_create_rep.assert_awaited_once()
        create_kwargs = mock_create_rep.call_args[1]
        self.assertIsNone(create_kwargs["reporter_id"])
        self.assertIsNone(create_kwargs["reported_id"])
        self.assertFalse(create_kwargs["identity_revealed_for_illegal"])
        self.assertEqual(create_kwargs["guilty_party"], "Reported_User")
        self.assertEqual(create_kwargs["reported_user_penalty"], 6)

        # Verify buffer was purged from RAM
        self.assertFalse(session_buffer.has_session(session_id))

    @patch("app.handlers.feedback.get_db_session")
    @patch("app.handlers.feedback.add_or_update_user_block")
    @patch("app.handlers.feedback.apply_admin_penalty")
    @patch("app.handlers.feedback.create_report")
    @patch("app.handlers.feedback.has_report_for_session", return_value=False)
    @patch("app.handlers.feedback.gemini_moderator")
    async def test_reporter_penalized_for_false_spite_report(
        self, mock_gemini, mock_has_rep, mock_create_rep, mock_penalty, mock_block, mock_db_session
    ) -> None:
        """Under Rule C, reporter is penalized for filing a spiteful/false report."""
        session_id = 202
        reporter_id = 3001
        reported_id = 4002

        session_buffer.add_text(session_id, reporter_id, "Send me your instagram")
        session_buffer.add_text(session_id, reported_id, "Sorry, I prefer to stay anonymous")

        mock_gemini.evaluate_session = AsyncMock(return_value=ModerationVerdict(
            guilty_party="Reporter",
            severity_category="Minor_Spam_Rudeness",
            reported_user_penalty=0,
            reporter_penalty=3,
            block_reported_user=True,
            block_reporter=False,
            reveal_identity_for_illegal_content=False,
            illegal_perpetrator="None",
            anonymized_summary="Partner politely declined to share handles. False report filed out of spite.",
        ))

        bot = AsyncMock()
        state = AsyncMock()
        state.get_data.return_value = {"session_id": session_id, "reported_id": reported_id}

        msg = AsyncMock()
        msg.text = "He refused to give me his social media"
        msg.from_user = MagicMock(id=reporter_id)

        await feedback_handlers.handle_report_reason(msg, bot, state)

        # Verify penalty applied to REPORTER
        mock_penalty.assert_awaited_once_with(unittest.mock.ANY, user_id=reporter_id, penalty=3)

        create_kwargs = mock_create_rep.call_args[1]
        self.assertEqual(create_kwargs["guilty_party"], "Reporter")
        self.assertEqual(create_kwargs["reporter_penalty"], 3)
        self.assertEqual(create_kwargs["reported_user_penalty"], 0)

    @patch("app.handlers.feedback.get_db_session")
    @patch("app.handlers.feedback.has_report_for_session", return_value=True)
    async def test_duplicate_report_blocked(self, mock_has_rep, mock_db_session) -> None:
        """A user cannot submit multiple reports for the same session."""
        bot = AsyncMock()
        state = AsyncMock()
        state.get_data.return_value = {"session_id": 203, "reported_id": 5002}

        msg = AsyncMock()
        msg.text = "Duplicate report"
        msg.from_user = MagicMock(id=5001)

        await feedback_handlers.handle_report_reason(msg, bot, state)
        msg.answer.assert_awaited_once()
        self.assertIn("A report has already been submitted", msg.answer.call_args[0][0])


class TestBannedUserEnforcement(unittest.IsolatedAsyncioTestCase):
    """Test that banned users are blocked from /start and /search."""

    @patch("app.handlers.start.get_db_session")
    @patch("app.handlers.start.get_or_create_user")
    async def test_banned_user_blocked_on_start(self, mock_get_user, mock_db_session) -> None:
        mock_user = MagicMock(is_banned=True, current_partner_id=None)
        mock_get_user.return_value = mock_user

        bot = AsyncMock()
        state = AsyncMock()
        msg = AsyncMock()
        msg.from_user = MagicMock(id=9999, username="banned_guy")

        from app.handlers.start import handle_start
        await handle_start(msg, None, state, bot)

        msg.answer.assert_awaited_once()
        self.assertIn("Access Denied", msg.answer.call_args[0][0])

    @patch("app.handlers.matchmaking.get_db_session")
    @patch("app.handlers.matchmaking.get_or_create_user")
    async def test_banned_user_blocked_on_search(self, mock_get_user, mock_db_session) -> None:
        mock_user = MagicMock(is_banned=True, sex="male", orientation="straight", current_partner_id=None)
        mock_get_user.return_value = mock_user

        bot = AsyncMock()
        msg = AsyncMock()
        msg.from_user = MagicMock(id=9999, username="banned_guy")

        await mm_handlers.perform_search(9999, bot, msg)
        msg.answer.assert_awaited_once()
        self.assertIn("Access Denied", msg.answer.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
