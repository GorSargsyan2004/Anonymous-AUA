"""Unit and integration tests for daily 24-hour setup and email verification reminders."""

import asyncio
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError

from app.database.crud import (
    get_users_for_setup_reminder,
    get_users_for_verification_reminder,
    record_setup_reminder_sent,
    record_verification_reminder_sent,
)
from app.database.models import User
from app.database.session import engine, get_db_session
from app.services.daily_reminders import (
    SETUP_REMINDER_TEXT,
    VERIFICATION_REMINDER_TEXT,
    daily_reminders_worker,
    process_daily_reminders,
)


class TestDailyReminders(unittest.IsolatedAsyncioTestCase):
    """Test daily reminder queries, message formatting, broadcast engine, and worker."""

    async def asyncTearDown(self) -> None:
        """Dispose of database engine connections between isolated test cases."""
        await engine.dispose()

    def test_reminder_message_content(self) -> None:
        """Verify reminder messages contain clear CTAs, commands, and anonymity notice."""
        # Setup reminder
        self.assertIn("/start", SETUP_REMINDER_TEXT)
        self.assertIn("profile", SETUP_REMINDER_TEXT.lower())

        # Verification reminder
        self.assertIn("/verify", VERIFICATION_REMINDER_TEXT)
        self.assertIn("trust", VERIFICATION_REMINDER_TEXT.lower())
        self.assertIn("sex", VERIFICATION_REMINDER_TEXT.lower())
        self.assertIn("anonymous", VERIFICATION_REMINDER_TEXT.lower())
        self.assertIn("@edu.aua.am", VERIFICATION_REMINDER_TEXT)

    async def test_crud_setup_reminder_eligibility(self) -> None:
        """Verify get_users_for_setup_reminder selects only eligible uncompleted profiles."""
        now = datetime.now(timezone.utc)
        old_time = now - timedelta(hours=30)
        recent_time = now - timedelta(hours=2)

        async with get_db_session() as session:
            # User 1: Incomplete setup, old account, no reminder yet -> ELIGIBLE
            u1 = User(id=99001, sex=None, orientation=None, is_banned=False, created_at=old_time)
            # User 2: Incomplete setup, new account (<24h) -> NOT eligible
            u2 = User(id=99002, sex=None, orientation=None, is_banned=False, created_at=recent_time)
            # User 3: Incomplete setup, reminded 2h ago -> NOT eligible
            u3 = User(id=99003, sex=None, orientation=None, is_banned=False, created_at=old_time, last_setup_reminder_at=recent_time)
            # User 4: Incomplete setup, reminded 26h ago -> ELIGIBLE
            u4 = User(id=99004, sex=None, orientation=None, is_banned=False, created_at=old_time, last_setup_reminder_at=now - timedelta(hours=26))
            # User 5: Completed profile -> NOT eligible
            u5 = User(id=99005, sex="male", orientation="straight", is_banned=False, created_at=old_time)
            # User 6: Banned -> NOT eligible
            u6 = User(id=99006, sex=None, orientation=None, is_banned=True, created_at=old_time)

            session.add_all([u1, u2, u3, u4, u5, u6])
            await session.commit()

            try:
                eligible = await get_users_for_setup_reminder(session, hours_threshold=24, limit=1000)
                eligible_ids = {u.id for u in eligible}
                self.assertIn(99001, eligible_ids)
                self.assertIn(99004, eligible_ids)
                self.assertNotIn(99002, eligible_ids)
                self.assertNotIn(99003, eligible_ids)
                self.assertNotIn(99005, eligible_ids)
                self.assertNotIn(99006, eligible_ids)
            finally:
                for uid in [99001, 99002, 99003, 99004, 99005, 99006]:
                    obj = await session.get(User, uid)
                    if obj:
                        await session.delete(obj)
                await session.commit()

    async def test_crud_verification_reminder_eligibility(self) -> None:
        """Verify get_users_for_verification_reminder selects only eligible unverified accounts."""
        now = datetime.now(timezone.utc)
        old_time = now - timedelta(hours=30)
        recent_time = now - timedelta(hours=2)

        async with get_db_session() as session:
            # User 1: Finished setup, unverified, old account, no reminder yet -> ELIGIBLE
            u1 = User(id=99101, sex="female", orientation="straight", is_verified=False, is_banned=False, created_at=old_time)
            # User 2: Finished setup, already verified -> NOT eligible
            u2 = User(id=99102, sex="female", orientation="straight", is_verified=True, is_banned=False, created_at=old_time)
            # User 3: Finished setup, unverified, reminded 2h ago -> NOT eligible
            u3 = User(id=99103, sex="male", orientation="straight", is_verified=False, is_banned=False, created_at=old_time, last_verification_reminder_at=recent_time)
            # User 4: Finished setup, unverified, reminded 26h ago -> ELIGIBLE
            u4 = User(id=99104, sex="male", orientation="straight", is_verified=False, is_banned=False, created_at=old_time, last_verification_reminder_at=now - timedelta(hours=26))
            # User 5: Incomplete profile -> NOT eligible
            u5 = User(id=99105, sex=None, orientation=None, is_verified=False, is_banned=False, created_at=old_time)
            # User 6: Banned -> NOT eligible
            u6 = User(id=99106, sex="female", orientation="straight", is_verified=False, is_banned=True, created_at=old_time)

            session.add_all([u1, u2, u3, u4, u5, u6])
            await session.commit()

            try:
                eligible = await get_users_for_verification_reminder(session, hours_threshold=24, limit=1000)
                eligible_ids = {u.id for u in eligible}
                self.assertIn(99101, eligible_ids)
                self.assertIn(99104, eligible_ids)
                self.assertNotIn(99102, eligible_ids)
                self.assertNotIn(99103, eligible_ids)
                self.assertNotIn(99105, eligible_ids)
                self.assertNotIn(99106, eligible_ids)
            finally:
                for uid in [99101, 99102, 99103, 99104, 99105, 99106]:
                    obj = await session.get(User, uid)
                    if obj:
                        await session.delete(obj)
                await session.commit()

    async def test_record_reminders_sent(self) -> None:
        """Verify record_setup_reminder_sent and record_verification_reminder_sent update timestamps."""
        async with get_db_session() as session:
            u = User(id=99201, sex="male", orientation="straight", is_banned=False)
            session.add(u)
            await session.commit()

            try:
                await record_setup_reminder_sent(session, 99201)
                await record_verification_reminder_sent(session, 99201)

                await session.refresh(u)
                self.assertIsNotNone(u.last_setup_reminder_at)
                self.assertIsNotNone(u.last_verification_reminder_at)
            finally:
                await session.delete(u)
                await session.commit()

    @patch("app.services.daily_reminders.INTER_MESSAGE_DELAY_SECONDS", 0.0)
    async def test_process_daily_reminders_broadcast(self) -> None:
        """Verify process_daily_reminders sends messages and marks timestamps."""
        now = datetime.now(timezone.utc)
        old_time = now - timedelta(hours=30)

        async with get_db_session() as session:
            u_setup = User(id=99301, sex=None, orientation=None, is_banned=False, created_at=old_time)
            u_verif = User(id=99302, sex="female", orientation="straight", is_verified=False, is_banned=False, created_at=old_time)
            session.add_all([u_setup, u_verif])
            await session.commit()

            bot = AsyncMock(spec=Bot)
            bot.send_message = AsyncMock()

            # Target only our test users to ensure completely isolated unit testing
            with patch("app.services.daily_reminders.get_users_for_setup_reminder", return_value=[u_setup]):
                with patch("app.services.daily_reminders.get_users_for_verification_reminder", return_value=[u_verif]):
                    try:
                        stats = await process_daily_reminders(bot, hours_threshold=24)
                        self.assertEqual(stats["setup_sent"], 1)
                        self.assertEqual(stats["verification_sent"], 1)

                        # Verify send_message was called for both users
                        called_chat_ids = {call[1]["chat_id"] for call in bot.send_message.call_args_list}
                        self.assertIn(99301, called_chat_ids)
                        self.assertIn(99302, called_chat_ids)

                        # Check timestamp updated
                        await session.refresh(u_setup)
                        await session.refresh(u_verif)
                        self.assertIsNotNone(u_setup.last_setup_reminder_at)
                        self.assertIsNotNone(u_verif.last_verification_reminder_at)
                    finally:
                        for uid in [99301, 99302]:
                            obj = await session.get(User, uid)
                            if obj:
                                await session.delete(obj)
                        await session.commit()

    @patch("app.services.daily_reminders.INTER_MESSAGE_DELAY_SECONDS", 0.0)
    async def test_process_daily_reminders_handles_blocked_user(self) -> None:
        """Verify TelegramForbiddenError is caught gracefully and does not break the reminder loop."""
        now = datetime.now(timezone.utc)
        old_time = now - timedelta(hours=30)

        async with get_db_session() as session:
            u = User(id=99401, sex=None, orientation=None, is_banned=False, created_at=old_time)
            session.add(u)
            await session.commit()

            bot = AsyncMock(spec=Bot)
            bot.send_message = AsyncMock(side_effect=TelegramForbiddenError(method=MagicMock(), message="Forbidden: bot was blocked by the user"))

            with patch("app.services.daily_reminders.get_users_for_setup_reminder", return_value=[u]):
                with patch("app.services.daily_reminders.get_users_for_verification_reminder", return_value=[]):
                    try:
                        stats = await process_daily_reminders(bot, hours_threshold=24)
                        self.assertEqual(stats["setup_sent"], 0)
                        self.assertEqual(stats["blocked_or_failed"], 1)

                        # Timestamp must still be updated to prevent infinite retry loops on blocked users
                        await session.refresh(u)
                        self.assertIsNotNone(u.last_setup_reminder_at)
                    finally:
                        obj = await session.get(User, 99401)
                        if obj:
                            await session.delete(obj)
                        await session.commit()

    async def test_worker_cancellation(self) -> None:
        """Verify daily_reminders_worker starts and responds cleanly to cancellation."""
        bot = AsyncMock(spec=Bot)
        task = asyncio.create_task(daily_reminders_worker(bot, interval_seconds=1, initial_delay_seconds=10))
        await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self.assertTrue(task.done())


if __name__ == "__main__":
    unittest.main()
