"""Unit and integration tests for Feedback Override, Reset, and Recent Partners Navigation."""

import asyncio
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import AsyncMock, MagicMock

from aiogram.types import CallbackQuery, Message, User as TgUser
from sqlalchemy import select

from app.database.crud import (
    delete_or_reset_feedback,
    get_feedback_by_session_and_user,
    get_or_create_user,
    get_recent_sessions_for_user,
    update_or_override_feedback,
)
from app.database.models import ChatSession, Feedback, User
from app.database.session import get_db_session
from app.handlers import feedback as feedback_handlers
from app.handlers import matchmaking as matchmaking_handlers
from app.keyboards.inline import (
    get_feedback_keyboard,
    get_feedback_manage_keyboard,
    get_past_partner_action_keyboard,
    get_recent_partners_keyboard,
)


class TestFeedbackOverrideAndRecent(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.user1_id = 9988101
        self.user2_id = 9988102
        async with get_db_session() as session:
            u1 = await get_or_create_user(session, user_id=self.user1_id, username="tester1")
            u2 = await get_or_create_user(session, user_id=self.user2_id, username="tester2")
            u1.priority = 50
            u2.priority = 50
            u1.spam_flags_count = 0
            u2.spam_flags_count = 0

            # Create a completed chat session (> 15 seconds long)
            started_at = datetime.now(timezone.utc) - timedelta(seconds=60)
            ended_at = datetime.now(timezone.utc)
            cs = ChatSession(
                user1_id=self.user1_id,
                user2_id=self.user2_id,
                is_active=False,
                started_at=started_at,
                ended_at=ended_at,
                last_activity_at=ended_at,
                messages_count=10,
            )
            session.add(cs)
            await session.commit()
            await session.refresh(cs)
            self.session_id = cs.id

    async def asyncTearDown(self):
        async with get_db_session() as session:
            # Delete feedbacks
            stmt_f = select(Feedback).where(Feedback.session_id == self.session_id)
            res_f = await session.execute(stmt_f)
            for f in res_f.scalars().all():
                await session.delete(f)

            # Delete sessions
            stmt = select(ChatSession).where(
                (ChatSession.user1_id == self.user1_id) | (ChatSession.user2_id == self.user1_id)
            )
            res = await session.execute(stmt)
            for cs in res.scalars().all():
                await session.delete(cs)

            for uid in (self.user1_id, self.user2_id):
                u_stmt = select(User).where(User.id == uid)
                u = (await session.execute(u_stmt)).scalar_one_or_none()
                if u:
                    await session.delete(u)
            await session.commit()

        from app.database.session import engine
        await engine.dispose()

    async def test_feedback_create_and_override_crud(self):
        async with get_db_session() as session:
            # 1. Initial rating: like (+1)
            res1 = await update_or_override_feedback(
                session=session,
                session_id=self.session_id,
                from_user_id=self.user1_id,
                partner_id=self.user2_id,
                new_feedback_type="like",
            )
            self.assertTrue(res1["created"])
            self.assertFalse(res1["updated"])
            self.assertEqual(res1["new_priority"], 51)

            p1 = (await session.execute(select(User).where(User.id == self.user2_id))).scalar_one()
            self.assertEqual(p1.priority, 51)

            # 2. Override rating: switch to dislike (-1 from base 50 -> 49)
            res2 = await update_or_override_feedback(
                session=session,
                session_id=self.session_id,
                from_user_id=self.user1_id,
                partner_id=self.user2_id,
                new_feedback_type="dislike",
            )
            self.assertFalse(res2["created"])
            self.assertTrue(res2["updated"])
            self.assertEqual(res2["new_priority"], 49)

            p2 = (await session.execute(select(User).where(User.id == self.user2_id))).scalar_one()
            self.assertEqual(p2.priority, 49)

            # 3. Repeat same rating: no-op
            res3 = await update_or_override_feedback(
                session=session,
                session_id=self.session_id,
                from_user_id=self.user1_id,
                partner_id=self.user2_id,
                new_feedback_type="dislike",
            )
            self.assertTrue(res3["already_set"])

            # 4. Reset rating: removes feedback and restores priority to 50
            reset_ok = await delete_or_reset_feedback(
                session=session,
                session_id=self.session_id,
                from_user_id=self.user1_id,
            )
            self.assertTrue(reset_ok)

            p3 = (await session.execute(select(User).where(User.id == self.user2_id))).scalar_one()
            self.assertEqual(p3.priority, 50)

            # Feedback record is gone
            fb = await get_feedback_by_session_and_user(session, self.session_id, self.user1_id)
            self.assertIsNone(fb)

    async def test_feedback_spam_override_and_reset(self):
        async with get_db_session() as session:
            # First rate dislike
            await update_or_override_feedback(
                session=session,
                session_id=self.session_id,
                from_user_id=self.user1_id,
                partner_id=self.user2_id,
                new_feedback_type="dislike",
            )
            p = (await session.execute(select(User).where(User.id == self.user2_id))).scalar_one()
            self.assertEqual(p.priority, 49)
            self.assertEqual(p.spam_flags_count, 0)

            # Switch to spam
            res_spam = await update_or_override_feedback(
                session=session,
                session_id=self.session_id,
                from_user_id=self.user1_id,
                partner_id=self.user2_id,
                new_feedback_type="spam",
            )
            self.assertTrue(res_spam["updated"])
            p_spam = (await session.execute(select(User).where(User.id == self.user2_id))).scalar_one()
            self.assertEqual(p_spam.spam_flags_count, 1)
            self.assertEqual(p_spam.priority, 49)

            # Reset spam: restores spam_flags_count to 0 and priority to 50
            await delete_or_reset_feedback(session, self.session_id, self.user1_id)
            p_clean = (await session.execute(select(User).where(User.id == self.user2_id))).scalar_one()
            self.assertEqual(p_clean.spam_flags_count, 0)
            self.assertEqual(p_clean.priority, 50)

    async def test_get_recent_sessions_for_user(self):
        async with get_db_session() as session:
            # Create a 2nd completed session
            s2 = ChatSession(
                user1_id=self.user1_id,
                user2_id=self.user2_id,
                is_active=False,
                started_at=datetime.now(timezone.utc) - timedelta(minutes=5),
                ended_at=datetime.now(timezone.utc) - timedelta(minutes=4),
                last_activity_at=datetime.now(timezone.utc) - timedelta(minutes=4),
                messages_count=5,
            )
            # Create a 3rd session that is still ACTIVE (should be excluded)
            s3 = ChatSession(
                user1_id=self.user1_id,
                user2_id=self.user2_id,
                is_active=True,
                started_at=datetime.now(timezone.utc),
                last_activity_at=datetime.now(timezone.utc),
                messages_count=2,
            )
            session.add_all([s2, s3])
            await session.commit()

            recent = await get_recent_sessions_for_user(session, user_id=self.user1_id, limit=5)
            recent_ids = [s.id for s in recent]
            self.assertIn(self.session_id, recent_ids)
            self.assertIn(s2.id, recent_ids)
            self.assertNotIn(s3.id, recent_ids)  # Active excluded

    async def test_handle_feedback_callback_and_reset(self):
        bot = MagicMock()
        bot.send_message = AsyncMock()

        cb = MagicMock(spec=CallbackQuery)
        cb.data = f"fb:{self.session_id}:{self.user2_id}:like"
        cb.from_user = MagicMock(spec=TgUser)
        cb.from_user.id = self.user1_id
        cb.message = MagicMock()
        cb.message.edit_text = AsyncMock()
        cb.answer = AsyncMock()

        # Submit like
        await feedback_handlers.handle_feedback_callback(cb, bot)
        cb.message.edit_text.assert_called_once()
        self.assertIn("Feedback submitted: 👍 Great Chat!", cb.message.edit_text.call_args[0][0])
        self.assertIsNotNone(cb.message.edit_text.call_args[1]["reply_markup"])

        # Reset via fb_reset callback
        cb_reset = MagicMock(spec=CallbackQuery)
        cb_reset.data = f"fb_reset:{self.session_id}:{self.user2_id}"
        cb_reset.from_user = MagicMock(spec=TgUser)
        cb_reset.from_user.id = self.user1_id
        cb_reset.message = MagicMock()
        cb_reset.message.edit_text = AsyncMock()
        cb_reset.answer = AsyncMock()

        await feedback_handlers.handle_feedback_reset(cb_reset)
        cb_reset.message.edit_text.assert_called_once()
        self.assertIn("Rating Reset", cb_reset.message.edit_text.call_args[0][0])

    async def test_handle_recent_command(self):
        bot = MagicMock()
        state = AsyncMock()

        msg = MagicMock(spec=Message)
        msg.from_user = MagicMock(spec=TgUser)
        msg.from_user.id = self.user1_id
        msg.answer = AsyncMock()

        await matchmaking_handlers.handle_recent_command(msg, bot, state)
        msg.answer.assert_called_once()
        self.assertIn("Recent Chat Partners", msg.answer.call_args[0][0])
        kb = msg.answer.call_args[1]["reply_markup"]
        self.assertIsNotNone(kb)

    async def test_handle_past_partner_selected(self):
        cb = MagicMock(spec=CallbackQuery)
        cb.data = f"past_partner:{self.session_id}:{self.user2_id}"
        cb.from_user = MagicMock(spec=TgUser)
        cb.from_user.id = self.user1_id
        cb.message = MagicMock()
        cb.message.edit_text = AsyncMock()
        cb.answer = AsyncMock()

        await feedback_handlers.handle_past_partner_selected(cb)
        cb.message.edit_text.assert_called_once()
        self.assertIn("Selected Past Partner", cb.message.edit_text.call_args[0][0])
        self.assertIsNotNone(cb.message.edit_text.call_args[1]["reply_markup"])


if __name__ == "__main__":
    unittest.main()
