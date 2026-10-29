"""Unit and integration tests for new features:
1. Public statistics (/stats, /stata)
2. Help guide (/help)
3. Admin blocking (/block <user_id>, /unblock <user_id>, report inline ban)
4. Banned user middleware enforcement
5. Report notifications containing reported user ID
6. Post-chat cooldown / partner blocking (1d, 3d, 7d, perm)
7. Matchmaker mutual block exclusion
8. /blocklist menu with [<] [2/3] [>] pagination and unblock flow
"""

import asyncio
from datetime import datetime, timedelta, timezone
import math
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.types import CallbackQuery, Message, User as TgUser

from app.database.crud import (
    add_or_update_user_block,
    ban_user_by_admin,
    delete_user_block,
    get_public_community_stats,
    get_user_blocks,
    unban_user_by_admin,
)
from app.handlers import admin as admin_handlers
from app.handlers import feedback as feedback_handlers
from app.handlers import start as start_handlers
from app.keyboards.inline import (
    get_block_duration_keyboard,
    get_blocklist_keyboard,
    get_feedback_keyboard,
    get_unblock_confirmation_keyboard,
)
from app.middlewares.banned import BannedCheckMiddleware
from app.services import ban_manager
from app.services.matcher import match_user_in_queue


class TestPublicStatsAndHelp(unittest.IsolatedAsyncioTestCase):
    """Test /stats, /stata and /help handlers."""

    @patch("app.handlers.start.get_db_session")
    @patch("app.handlers.start.get_public_community_stats")
    async def test_handle_user_stats_content(self, mock_get_stats, mock_db_session) -> None:
        """Verify /stats output includes community stats and omits admin-sensitive data."""
        mock_get_stats.return_value = {
            "total_users": 150,
            "active_chats": 12,
            "male_count": 80,
            "female_count": 70,
            "straight_count": 100,
            "bisexual_count": 30,
            "lesbian_count": 10,
            "gay_count": 10,
        }
        msg = AsyncMock()
        await start_handlers.handle_user_stats(msg)

        msg.answer.assert_awaited_once()
        text = msg.answer.call_args[0][0]

        # Verify required community metrics are present
        self.assertIn("150", text)
        self.assertIn("12", text)
        self.assertIn("Male: <b>80</b>", text)
        self.assertIn("Female: <b>70</b>", text)
        self.assertIn("Straight: <b>100</b>", text)
        self.assertIn("Bisexual: <b>30</b>", text)
        self.assertIn("Lesbian: <b>10</b>", text)
        self.assertIn("Gay: <b>10</b>", text)

        # Verify admin-only data is NOT present
        self.assertNotIn("Reports Summary", text)
        self.assertNotIn("Database Backups", text)
        self.assertNotIn("All-Time Feedbacks", text)

    async def test_handle_help_command(self) -> None:
        """Verify /help displays reference guide containing core user commands."""
        msg = AsyncMock()
        await start_handlers.handle_help_command(msg)

        msg.answer.assert_awaited_once()
        text = msg.answer.call_args[0][0]
        self.assertIn("/search", text)
        self.assertIn("/next", text)
        self.assertIn("/stop", text)
        self.assertIn("/link", text)
        self.assertIn("/invite", text)
        self.assertIn("/privacy", text)
        self.assertIn("/delete_me", text)
        self.assertIn("/preferences", text)
        self.assertIn("/typing", text)
        self.assertIn("/blocklist", text)
        self.assertIn("/stats", text)


class TestAdminBlockAndBannedMiddleware(unittest.IsolatedAsyncioTestCase):
    """Test admin /block, /unblock, and BannedCheckMiddleware."""

    def setUp(self) -> None:
        ban_manager.clear_bans_for_testing()

    def tearDown(self) -> None:
        ban_manager.clear_bans_for_testing()

    @patch("app.handlers.admin.get_db_session")
    @patch("app.handlers.admin.ban_user_by_admin")
    @patch("app.handlers.admin.reply_manager.clear_chat")
    async def test_admin_block_user_command(self, mock_clear_chat, mock_ban_db, mock_db_session) -> None:
        """Admin /block <user_id> bans user, disconnects partner, and updates ban cache."""
        mock_ban_db.return_value = (True, 9999)  # Partner 9999 was active
        bot = AsyncMock()
        msg = AsyncMock()
        msg.text = "/block 12345"
        msg.from_user = MagicMock(id=5321254678, username="admin_gor")

        await admin_handlers.handle_admin_block_user(msg, bot)

        # Verify ban cache updated
        self.assertTrue(ban_manager.is_user_banned(12345))

        # Verify partner was notified of disconnection
        bot.send_message.assert_any_await(
            chat_id=9999,
            text=unittest.mock.ANY,
            parse_mode="HTML",
        )
        mock_clear_chat.assert_awaited_once_with(12345, 9999)

        # Verify admin received confirmation
        msg.reply.assert_awaited_once()
        self.assertIn("12345", msg.reply.call_args[0][0])
        self.assertIn("banned", msg.reply.call_args[0][0])

    @patch("app.handlers.admin.get_db_session")
    @patch("app.handlers.admin.unban_user_by_admin")
    async def test_admin_unblock_user_command(self, mock_unban_db, mock_db_session) -> None:
        """Admin /unblock <user_id> unbans user and removes from cache."""
        ban_manager.add_banned_user(12345)
        self.assertTrue(ban_manager.is_user_banned(12345))

        msg = AsyncMock()
        msg.text = "/unblock 12345"
        msg.from_user = MagicMock(id=5321254678, username="admin_gor")

        await admin_handlers.handle_admin_unblock_user(msg)

        self.assertFalse(ban_manager.is_user_banned(12345))
        msg.reply.assert_awaited_once()
        self.assertIn("unbanned", msg.reply.call_args[0][0])

    async def test_banned_middleware_blocks_message(self) -> None:
        """BannedCheckMiddleware blocks message events from banned users."""
        ban_manager.add_banned_user(8888)
        middleware = BannedCheckMiddleware()

        msg = AsyncMock(spec=Message)
        msg.answer = AsyncMock()
        user = MagicMock(spec=TgUser, id=8888)
        data = {"event_from_user": user}
        next_handler = AsyncMock()

        result = await middleware(next_handler, msg, data)

        # Handler should NOT be called
        next_handler.assert_not_called()
        self.assertIsNone(result)

        # User got ban alert
        msg.answer.assert_awaited_once()
        self.assertIn("Access Denied", msg.answer.call_args[0][0])

    async def test_banned_middleware_blocks_callback_query(self) -> None:
        """BannedCheckMiddleware blocks callback query events from banned users."""
        ban_manager.add_banned_user(8888)
        middleware = BannedCheckMiddleware()

        cb = AsyncMock(spec=CallbackQuery)
        cb.answer = AsyncMock()
        user = MagicMock(spec=TgUser, id=8888)
        data = {"event_from_user": user}
        next_handler = AsyncMock()

        result = await middleware(next_handler, cb, data)

        next_handler.assert_not_called()
        self.assertIsNone(result)
        cb.answer.assert_awaited_once_with(
            "🚫 You have been banned by an administrator.",
            show_alert=True,
        )

    async def test_banned_middleware_allows_normal_user(self) -> None:
        """BannedCheckMiddleware passes events through for non-banned users."""
        middleware = BannedCheckMiddleware()

        msg = AsyncMock()
        user = MagicMock(spec=TgUser, id=7777)
        data = {"event_from_user": user}
        next_handler = AsyncMock(return_value="OK")

        result = await middleware(next_handler, msg, data)

        next_handler.assert_awaited_once_with(msg, data)
        self.assertEqual(result, "OK")


class TestReportNotificationReportedId(unittest.IsolatedAsyncioTestCase):
    """Test report reason handler with automated AI moderation."""

    @patch("app.handlers.feedback.get_db_session")
    @patch("app.handlers.feedback.add_or_update_user_block")
    @patch("app.handlers.feedback.apply_admin_penalty")
    @patch("app.handlers.feedback.ban_user_by_admin")
    @patch("app.handlers.feedback.create_report")
    @patch("app.handlers.feedback.get_user")
    @patch("app.handlers.feedback.has_report_for_session", return_value=False)
    @patch("app.handlers.feedback.session_buffer")
    @patch("app.handlers.feedback.gemini_moderator")
    async def test_report_includes_reported_user_id_and_ban_command_on_illegal(
        self, mock_gemini, mock_buffer, mock_has_rep, mock_get_user, mock_create_rep, mock_ban, mock_penalty, mock_add_block, mock_db_session
    ) -> None:
        """Admin alert is triggered with unmasked IDs only for Tier 3 illegal violations."""
        mock_create_rep.return_value = MagicMock(id=42)
        mock_reported = MagicMock(username="troublemaker", priority=50)
        mock_get_user.return_value = mock_reported
        mock_buffer.pop_session.return_value = [
            MagicMock(sender_id=98765, text="I will extort money or leak your info", image_bytes=None, had_image_evicted=False)
        ]
        from app.services.ai_moderator import ModerationVerdict
        mock_gemini.evaluate_session = AsyncMock(return_value=ModerationVerdict(
            guilty_party="Reported_User",
            severity_category="Illegal_Extreme",
            reported_user_penalty=10,
            reporter_penalty=0,
            block_reported_user=True,
            block_reporter=False,
            reveal_identity_for_illegal_content=True,
            illegal_perpetrator="Reported_User",
            anonymized_summary="Extortion and blackmail detected.",
        ))

        bot = AsyncMock()
        state = AsyncMock()
        state.get_data.return_value = {"session_id": 10, "reported_id": 98765}

        msg = AsyncMock()
        msg.text = "Extortion blackmail threat"
        msg.photo = None
        msg.document = None
        msg.voice = None
        msg.video = None
        msg.from_user = MagicMock(id=11111, username="good_student")

        with patch("app.handlers.feedback.settings.ADMIN_IDS", [5321254678]):
            await feedback_handlers.handle_report_reason(msg, bot, state)

        # Bot notifies admin about illegal emergency unmasking
        admin_alert = [call[1]["text"] for call in bot.send_message.call_args_list if call[1].get("chat_id") == 5321254678][0]

        self.assertIn("Reported User ID:</b> <code>98765</code>", admin_alert)
        self.assertIn("Reporter ID:</b> <code>11111</code>", admin_alert)
        self.assertIn("/block 98765", admin_alert)

    @patch("app.handlers.feedback.get_db_session")
    @patch("app.handlers.feedback.add_or_update_user_block")
    @patch("app.handlers.feedback.create_report")
    @patch("app.handlers.feedback.has_report_for_session", return_value=False)
    @patch("app.handlers.feedback.session_buffer")
    async def test_report_expired_buffer(
        self, mock_buffer, mock_has_rep, mock_create_rep, mock_add_block, mock_db_session
    ) -> None:
        """When 5-min TTL has passed and buffer is empty, Zero-Knowledge auto-blocks partner."""
        mock_create_rep.return_value = MagicMock(id=43)
        mock_buffer.pop_session.return_value = None

        bot = AsyncMock()
        state = AsyncMock()
        state.get_data.return_value = {"session_id": 12, "reported_id": 55555}

        msg = AsyncMock()
        msg.text = "Report on expired session"
        msg.photo = None
        msg.document = None
        msg.voice = None
        msg.video = None
        msg.from_user = MagicMock(id=22222, username="victim_student")

        with patch("app.handlers.feedback.settings.ADMIN_IDS", [5321254678]):
            await feedback_handlers.handle_report_reason(msg, bot, state)

        # Partner is auto-blocked
        mock_add_block.assert_awaited_once_with(
            session=unittest.mock.ANY,
            user_id=22222,
            blocked_user_id=55555,
            expires_at=None,
        )
        # Message to reporter explains 5-minute zero knowledge policy
        reporter_msg = msg.answer.call_args[0][0]
        self.assertIn("Report Window Expired", reporter_msg)
        self.assertIn("5-Minute Ephemeral Buffer Policy", reporter_msg)



class TestPartnerBlockAndCooldown(unittest.IsolatedAsyncioTestCase):
    """Test post-chat cooldown options and block duration selection."""

    def test_feedback_keyboard_contains_block_button(self) -> None:
        """get_feedback_keyboard includes 'Don't connect again'."""
        kb = get_feedback_keyboard(session_id=1, partner_id=2222)
        button_texts = [btn.text for row in kb.inline_keyboard for btn in row]
        self.assertIn("🚫 Don't connect again", button_texts)

    def test_block_duration_keyboard_options(self) -> None:
        """get_block_duration_keyboard contains 1 Day, 3 Days, 1 Week, Permanently."""
        kb = get_block_duration_keyboard(session_id=1, partner_id=2222)
        button_texts = [btn.text for row in kb.inline_keyboard for btn in row]
        self.assertIn("⏰ 1 Day", button_texts)
        self.assertIn("⏰ 3 Days", button_texts)
        self.assertIn("⏰ 1 Week", button_texts)
        self.assertIn("⛔ Permanently", button_texts)

    @patch("app.handlers.feedback.get_db_session")
    @patch("app.handlers.feedback.add_or_update_user_block")
    async def test_block_duration_handler_temporary(self, mock_add_block, mock_db_session) -> None:
        """Choosing 3 Days sets expires_at to ~3 days from now."""
        cb = AsyncMock()
        cb.data = "block_dur:3333:3d"
        cb.from_user = MagicMock(id=4444)
        cb.message = AsyncMock()

        await feedback_handlers.handle_block_duration(cb)

        mock_add_block.assert_awaited_once()
        call_kwargs = mock_add_block.call_args[1]
        self.assertEqual(call_kwargs["user_id"], 4444)
        self.assertEqual(call_kwargs["blocked_user_id"], 3333)
        self.assertIsNotNone(call_kwargs["expires_at"])
        # Check roughly 3 days
        diff = call_kwargs["expires_at"] - datetime.now(timezone.utc)
        self.assertAlmostEqual(diff.total_seconds(), 3 * 86400, delta=10)

        cb.message.edit_text.assert_awaited_once()
        self.assertIn("3 days", cb.message.edit_text.call_args[0][0])

    @patch("app.handlers.feedback.get_db_session")
    @patch("app.handlers.feedback.add_or_update_user_block")
    async def test_block_duration_handler_permanent(self, mock_add_block, mock_db_session) -> None:
        """Choosing Permanently sets expires_at to None."""
        cb = AsyncMock()
        cb.data = "block_dur:3333:perm"
        cb.from_user = MagicMock(id=4444)
        cb.message = AsyncMock()

        await feedback_handlers.handle_block_duration(cb)

        mock_add_block.assert_awaited_once()
        call_kwargs = mock_add_block.call_args[1]
        self.assertIsNone(call_kwargs["expires_at"])

        cb.message.edit_text.assert_awaited_once()
        self.assertIn("permanently", cb.message.edit_text.call_args[0][0])


class TestBlocklistPaginationAndUnblock(unittest.IsolatedAsyncioTestCase):
    """Test /blocklist menu, [<] [2/3] [>] pagination, and unblock flow."""

    def test_blocklist_keyboard_single_page(self) -> None:
        """<= 5 items renders date buttons with no pagination row."""
        blocks = [
            MagicMock(id=1, created_at=datetime(2026, 10, 21, 0, 30), expires_at=None),
            MagicMock(id=2, created_at=datetime(2026, 10, 20, 18, 15), expires_at=datetime(2026, 10, 23)),
        ]
        kb = get_blocklist_keyboard(blocks, page=1, total_pages=1)
        # 2 date button rows
        self.assertEqual(len(kb.inline_keyboard), 2)
        self.assertIn("2026-10-21 00:30", kb.inline_keyboard[0][0].text)
        self.assertIn("(perm)", kb.inline_keyboard[0][0].text)
        self.assertIn("2026-10-20 18:15", kb.inline_keyboard[1][0].text)
        self.assertIn("(temp)", kb.inline_keyboard[1][0].text)

    def test_blocklist_keyboard_multi_page_pagination(self) -> None:
        """Multi-page blocklist renders navigation row [<] [2/3] [>]."""
        blocks = [
            MagicMock(id=i, created_at=datetime(2026, 10, 21, 0, i), expires_at=None)
            for i in range(1, 6)
        ]
        kb = get_blocklist_keyboard(blocks, page=2, total_pages=3)
        # 5 item rows + 1 pagination row = 6 rows
        self.assertEqual(len(kb.inline_keyboard), 6)
        nav_row = kb.inline_keyboard[5]
        self.assertEqual(len(nav_row), 3)
        self.assertEqual(nav_row[0].text, "[<]")
        self.assertEqual(nav_row[0].callback_data, "bl_page:1")
        self.assertEqual(nav_row[1].text, "[2/3]")
        self.assertEqual(nav_row[2].text, "[>]")
        self.assertEqual(nav_row[2].callback_data, "bl_page:3")

    @patch("app.handlers.start.get_db_session")
    @patch("app.handlers.start.delete_user_block")
    @patch("app.handlers.start.get_user_blocks")
    async def test_unblock_confirm_deletes_block_and_refreshes(
        self, mock_get_blocks, mock_delete_block, mock_db_session
    ) -> None:
        """Confirming unblock removes record and refreshes remaining items."""
        mock_delete_block.return_value = True
        mock_get_blocks.return_value = ([], 0)  # No remaining blocks

        cb = AsyncMock()
        cb.data = "bl_unblock_confirm:10:1"
        cb.from_user = MagicMock(id=5000)
        cb.message = AsyncMock()

        await start_handlers.handle_unblock_confirm(cb)

        mock_delete_block.assert_awaited_once_with(mock_db_session.return_value.__aenter__.return_value, user_id=5000, block_id=10)
        cb.message.edit_text.assert_awaited_once()
        self.assertIn("Partner unblocked", cb.message.edit_text.call_args[0][0])
        self.assertIn("empty", cb.message.edit_text.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
