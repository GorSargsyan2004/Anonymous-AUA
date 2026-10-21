"""Unit and integration tests for Inactivity & Ghost Chat Shield."""

import asyncio
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.types import CallbackQuery, Message, User as TgUser
from sqlalchemy import select

from app.database.crud import (
    end_session_by_id,
    get_expired_paused_sessions,
    get_inactive_sessions_for_closure,
    get_inactive_sessions_for_warning,
    get_or_create_user,
    mark_chat_session_inactivity_warned,
    pause_chat_session,
    reset_chat_session_inactivity_warning,
    touch_chat_session_activity,
    unpause_chat_session,
)
from app.database.models import ChatSession, User
from app.database.session import get_db_session
from app.handlers import matchmaking as matchmaking_handlers
from app.keyboards.inline import get_inactivity_warning_keyboard
from app.services.inactivity_monitor import check_and_process_inactivity


class TestInactivityShield(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.user1_id = 9988001
        self.user2_id = 9988002
        async with get_db_session() as session:
            u1 = await get_or_create_user(session, user_id=self.user1_id, username="ghost1")
            u2 = await get_or_create_user(session, user_id=self.user2_id, username="ghost2")
            u1.current_partner_id = self.user2_id
            u2.current_partner_id = self.user1_id
            u1.is_searching = False
            u2.is_searching = False
            u1.completed_sessions_count = 0
            u2.completed_sessions_count = 0

            # Create an active chat session
            cs = ChatSession(
                user1_id=self.user1_id,
                user2_id=self.user2_id,
                is_active=True,
                started_at=datetime.now(timezone.utc),
                last_activity_at=datetime.now(timezone.utc),
                messages_count=0,
                inactivity_warned=False,
            )
            session.add(cs)
            await session.commit()
            await session.refresh(cs)
            self.session_id = cs.id

    async def asyncTearDown(self):
        async with get_db_session() as session:
            stmt = select(ChatSession).where(ChatSession.id == self.session_id)
            res = await session.execute(stmt)
            cs = res.scalar_one_or_none()
            if cs:
                await session.delete(cs)

            for uid in (self.user1_id, self.user2_id):
                u_stmt = select(User).where(User.id == uid)
                u_res = await session.execute(u_stmt)
                u = u_res.scalar_one_or_none()
                if u:
                    await session.delete(u)
            await session.commit()
        from app.database.session import engine
        await engine.dispose()

    async def test_touch_activity(self):
        async with get_db_session() as session:
            await touch_chat_session_activity(session, self.session_id)
            stmt = select(ChatSession).where(ChatSession.id == self.session_id)
            res = await session.execute(stmt)
            cs = res.scalar_one()
            self.assertEqual(cs.messages_count, 1)
            self.assertFalse(cs.inactivity_warned)

    async def test_warning_and_reset(self):
        async with get_db_session() as session:
            await mark_chat_session_inactivity_warned(session, self.session_id)
            stmt = select(ChatSession).where(ChatSession.id == self.session_id)
            cs = (await session.execute(stmt)).scalar_one()
            self.assertTrue(cs.inactivity_warned)
            self.assertIsNotNone(cs.inactivity_warned_at)

            # Reset warning via I'm Here action
            await reset_chat_session_inactivity_warning(session, self.session_id)
            stmt2 = select(ChatSession).where(ChatSession.id == self.session_id)
            cs2 = (await session.execute(stmt2)).scalar_one()
            self.assertFalse(cs2.inactivity_warned)
            self.assertIsNone(cs2.inactivity_warned_at)

    async def test_ghost_warning_eligibility(self):
        async with get_db_session() as session:
            # Shift started_at back by 150 seconds (> 120s timeout)
            old_time = datetime.now(timezone.utc) - timedelta(seconds=150)
            stmt = select(ChatSession).where(ChatSession.id == self.session_id)
            cs = (await session.execute(stmt)).scalar_one()
            cs.started_at = old_time
            cs.last_activity_at = old_time
            cs.messages_count = 0
            cs.inactivity_warned = False
            await session.commit()

            warn_list = await get_inactive_sessions_for_warning(session, ghost_timeout_seconds=120, mid_chat_timeout_seconds=420)
            matching_ids = [s.id for s in warn_list]
            self.assertIn(self.session_id, matching_ids)

    async def test_closure_eligibility(self):
        async with get_db_session() as session:
            # Warned 150 seconds ago (> 120s grace period)
            warned_time = datetime.now(timezone.utc) - timedelta(seconds=150)
            stmt = select(ChatSession).where(ChatSession.id == self.session_id)
            cs = (await session.execute(stmt)).scalar_one()
            cs.inactivity_warned = True
            cs.inactivity_warned_at = warned_time
            await session.commit()

            close_list = await get_inactive_sessions_for_closure(session, grace_period_seconds=120)
            matching_ids = [s.id for s in close_list]
            self.assertIn(self.session_id, matching_ids)

    async def test_end_session_by_id_ghost_neutral(self):
        async with get_db_session() as session:
            # End ghost session with 0 messages
            res = await end_session_by_id(session, self.session_id)
            self.assertIsNotNone(res)
            ended_cs, u1_id, u2_id = res
            self.assertFalse(ended_cs.is_active)

            # Ensure neither user had completed_sessions_count bumped
            u1 = (await session.execute(select(User).where(User.id == self.user1_id))).scalar_one()
            u2 = (await session.execute(select(User).where(User.id == self.user2_id))).scalar_one()
            self.assertEqual(u1.completed_sessions_count, 0)
            self.assertEqual(u2.completed_sessions_count, 0)
            self.assertIsNone(u1.current_partner_id)
            self.assertIsNone(u2.current_partner_id)

    async def test_inactivity_keep_callback(self):
        bot = MagicMock()
        bot.send_message = AsyncMock()

        cb = MagicMock(spec=CallbackQuery)
        cb.data = f"inact_keep:{self.session_id}"
        cb.from_user = MagicMock(spec=TgUser)
        cb.from_user.id = self.user1_id
        cb.message = MagicMock()
        cb.message.edit_text = AsyncMock()
        cb.answer = AsyncMock()

        await matchmaking_handlers.handle_inactivity_keep(cb, bot)

        cb.message.edit_text.assert_called_once()
        self.assertIn("Noted! Timer reset", cb.message.edit_text.call_args[0][0])
        bot.send_message.assert_called_once()
        self.assertIn("confirmed they are here", bot.send_message.call_args[1]["text"])

    async def test_inactivity_stop_callback(self):
        bot = MagicMock()
        bot.send_message = AsyncMock()

        cb = MagicMock(spec=CallbackQuery)
        cb.data = f"inact_stop:{self.session_id}"
        cb.from_user = MagicMock(spec=TgUser)
        cb.from_user.id = self.user1_id
        cb.message = MagicMock()
        cb.message.edit_text = AsyncMock()
        cb.answer = AsyncMock()

        await matchmaking_handlers.handle_inactivity_stop(cb, bot)

        cb.message.edit_text.assert_called_once()
        self.assertIn("Conversation ended", cb.message.edit_text.call_args[0][0])
        bot.send_message.assert_called_once()
        self.assertIn("ended the conversation", bot.send_message.call_args[1]["text"])

    async def test_pause_and_unpause_crud(self):
        async with get_db_session() as session:
            # Mark warned first to test that pause clears warning
            await mark_chat_session_inactivity_warned(session, self.session_id)
            paused = await pause_chat_session(session, self.session_id)
            self.assertTrue(paused)

            cs = (await session.execute(select(ChatSession).where(ChatSession.id == self.session_id))).scalar_one()
            self.assertTrue(cs.is_paused)
            self.assertIsNotNone(cs.paused_at)
            self.assertFalse(cs.inactivity_warned)
            self.assertIsNone(cs.inactivity_warned_at)

            # Now unpause
            unpaused = await unpause_chat_session(session, self.session_id)
            self.assertTrue(unpaused)

            cs2 = (await session.execute(select(ChatSession).where(ChatSession.id == self.session_id))).scalar_one()
            self.assertFalse(cs2.is_paused)
            self.assertIsNone(cs2.paused_at)

    async def test_paused_sessions_excluded_from_warnings_and_closures(self):
        async with get_db_session() as session:
            # Backdate activity
            old_time = datetime.now(timezone.utc) - timedelta(hours=2)
            cs = (await session.execute(select(ChatSession).where(ChatSession.id == self.session_id))).scalar_one()
            cs.last_activity_at = old_time
            cs.is_paused = True
            cs.paused_at = old_time
            await session.commit()

            # Ensure NOT included in warning query
            warn_list = await get_inactive_sessions_for_warning(session, ghost_timeout_seconds=120, mid_chat_timeout_seconds=1800)
            self.assertNotIn(self.session_id, [s.id for s in warn_list])

            # Even if warned flag was set, ensure NOT included in closure query
            cs.inactivity_warned = True
            cs.inactivity_warned_at = old_time
            await session.commit()

            close_list = await get_inactive_sessions_for_closure(session, grace_period_seconds=120)
            self.assertNotIn(self.session_id, [s.id for s in close_list])

    async def test_expired_paused_sessions_detection(self):
        async with get_db_session() as session:
            # Session paused 25 hours ago
            past_25h = datetime.now(timezone.utc) - timedelta(hours=25)
            cs = (await session.execute(select(ChatSession).where(ChatSession.id == self.session_id))).scalar_one()
            cs.is_paused = True
            cs.paused_at = past_25h
            await session.commit()

            expired = await get_expired_paused_sessions(session, max_pause_hours=24)
            self.assertIn(self.session_id, [s.id for s in expired])

    async def test_inactivity_pause_callback(self):
        bot = MagicMock()
        bot.send_message = AsyncMock()

        cb = MagicMock(spec=CallbackQuery)
        cb.data = f"inact_pause:{self.session_id}"
        cb.from_user = MagicMock(spec=TgUser)
        cb.from_user.id = self.user1_id
        cb.message = MagicMock()
        cb.message.edit_text = AsyncMock()
        cb.answer = AsyncMock()

        await matchmaking_handlers.handle_inactivity_pause(cb, bot)

        cb.message.edit_text.assert_called_once()
        self.assertIn("Sleep Mode", cb.message.edit_text.call_args[0][0])
        bot.send_message.assert_called_once()
        self.assertIn("put this chat into Sleep Mode", bot.send_message.call_args[1]["text"])

        async with get_db_session() as session:
            cs = (await session.execute(select(ChatSession).where(ChatSession.id == self.session_id))).scalar_one()
            self.assertTrue(cs.is_paused)

    async def test_pause_command_and_awakening(self):
        bot = MagicMock()
        bot.send_message = AsyncMock()
        state = AsyncMock()

        msg = MagicMock(spec=Message)
        msg.from_user = MagicMock(spec=TgUser)
        msg.from_user.id = self.user1_id
        msg.answer = AsyncMock()

        # Trigger /pause
        await matchmaking_handlers.handle_pause_command(msg, bot, state)
        msg.answer.assert_called_once()
        self.assertIn("Sleep Mode Activated", msg.answer.call_args[0][0])
        bot.send_message.assert_called_once()
        self.assertIn("put this chat into Sleep Mode", bot.send_message.call_args[1]["text"])

        async with get_db_session() as session:
            cs = (await session.execute(select(ChatSession).where(ChatSession.id == self.session_id))).scalar_one()
            self.assertTrue(cs.is_paused)

            # Test awakening helper
            await matchmaking_handlers._handle_awakening_if_paused(
                bot=bot,
                session=session,
                session_id=self.session_id,
                was_paused=True,
                user_id=self.user1_id,
                partner_id=self.user2_id,
            )

            cs_after = (await session.execute(select(ChatSession).where(ChatSession.id == self.session_id))).scalar_one()
            self.assertFalse(cs_after.is_paused)


if __name__ == "__main__":
    unittest.main()

