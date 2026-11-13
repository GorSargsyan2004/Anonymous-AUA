"""Unit and integration tests for Feedback Override, Reset, and Recent Partners Navigation."""

import asyncio
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import AsyncMock, MagicMock

from aiogram import Bot
from aiogram.types import CallbackQuery, Message, User as TgUser
from sqlalchemy import select

from app.database.crud import (
    delete_or_reset_feedback,
    get_feedback_by_session_and_user,
    get_or_create_user,
    get_recent_sessions_for_user,
    update_or_override_feedback,
)
from app.database.models import ChatSession, Feedback, ReconnectRequest, User, UserBlock
from app.database.session import get_db_session
from app.handlers import feedback as feedback_handlers
from app.handlers import matchmaking as matchmaking_handlers
from app.handlers import start as start_handlers
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
            # Delete reconnect requests
            stmt_r = select(ReconnectRequest).where(
                (ReconnectRequest.sender_id == self.user1_id)
                | (ReconnectRequest.recipient_id == self.user1_id)
                | (ReconnectRequest.sender_id == self.user2_id)
                | (ReconnectRequest.recipient_id == self.user2_id)
            )
            res_r = await session.execute(stmt_r)
            for r in res_r.scalars().all():
                await session.delete(r)

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

    async def test_close_chapter_sunset_temporary_and_reopen(self):
        bot = MagicMock()
        bot.send_message = AsyncMock()

        # 1. User rates "Great Chat"
        cb_like = MagicMock(spec=CallbackQuery)
        cb_like.data = f"fb:{self.session_id}:{self.user2_id}:like"
        cb_like.from_user = MagicMock(spec=TgUser)
        cb_like.from_user.id = self.user1_id
        cb_like.message = MagicMock()
        cb_like.message.edit_text = AsyncMock()
        cb_like.answer = AsyncMock()

        await feedback_handlers.handle_feedback_callback(cb_like, bot)
        cb_like.message.edit_text.assert_called_once()
        self.assertIn("Great Chat!", cb_like.message.edit_text.call_args[0][0])
        self.assertIn("Close Chapter", cb_like.message.edit_text.call_args[0][0])

        # 2. User taps "Close chapter..."
        cb_prompt = MagicMock(spec=CallbackQuery)
        cb_prompt.data = f"fb_sunset_prompt:{self.session_id}:{self.user2_id}"
        cb_prompt.from_user = MagicMock(spec=TgUser)
        cb_prompt.from_user.id = self.user1_id
        cb_prompt.message = MagicMock()
        cb_prompt.message.edit_text = AsyncMock()
        cb_prompt.answer = AsyncMock()

        await feedback_handlers.handle_feedback_sunset_prompt(cb_prompt)
        cb_prompt.message.edit_text.assert_called_once()
        self.assertIn("Close Chapter", cb_prompt.message.edit_text.call_args[0][0])

        # 3. User selects 3 days cooldown
        cb_dur = MagicMock(spec=CallbackQuery)
        cb_dur.data = f"fb_sunset_dur:{self.user2_id}:3d"
        cb_dur.from_user = MagicMock(spec=TgUser)
        cb_dur.from_user.id = self.user1_id
        cb_dur.message = MagicMock()
        cb_dur.message.edit_text = AsyncMock()
        cb_dur.answer = AsyncMock()

        await feedback_handlers.handle_feedback_sunset_duration(cb_dur)
        cb_dur.message.edit_text.assert_called_once()
        self.assertIn("Chapter paused on a high note!", cb_dur.message.edit_text.call_args[0][0])
        self.assertIn("3 days", cb_dur.message.edit_text.call_args[0][0])

        # Verify UserBlock in database
        async with get_db_session() as session:
            stmt = select(UserBlock).where(
                UserBlock.user_id == self.user1_id,
                UserBlock.blocked_user_id == self.user2_id,
            )
            block = (await session.execute(stmt)).scalar_one_or_none()
            self.assertIsNotNone(block)
            self.assertEqual(block.reason, "closed_chapter")
            self.assertIsNotNone(block.expires_at)
            block_id = block.id

        # 4. Verify /blocklist command displays chapter
        msg = MagicMock(spec=Message)
        msg.from_user = MagicMock(spec=TgUser)
        msg.from_user.id = self.user1_id
        msg.answer = AsyncMock()

        await start_handlers.handle_blocklist_command(msg)
        msg.answer.assert_called_once()
        self.assertIn("Match Restrictions & Chapters", msg.answer.call_args[0][0])
        kb = msg.answer.call_args[1]["reply_markup"]
        btn_texts = [btn.text for row in kb.inline_keyboard for btn in row]
        self.assertTrue(any("Chapter" in t and "🕊️" in t for t in btn_texts))

        # 5. User taps the chapter item to view details
        cb_select = MagicMock(spec=CallbackQuery)
        cb_select.data = f"bl_select:{block_id}:1"
        cb_select.from_user = MagicMock(spec=TgUser)
        cb_select.from_user.id = self.user1_id
        cb_select.message = MagicMock()
        cb_select.message.edit_text = AsyncMock()
        cb_select.answer = AsyncMock()

        await start_handlers.handle_blocklist_select(cb_select)
        cb_select.message.edit_text.assert_called_once()
        self.assertIn("Reopen Chapter with Partner?", cb_select.message.edit_text.call_args[0][0])

        # 6. User confirms reopening chapter
        cb_unblock = MagicMock(spec=CallbackQuery)
        cb_unblock.data = f"bl_unblock_confirm:{block_id}:1"
        cb_unblock.from_user = MagicMock(spec=TgUser)
        cb_unblock.from_user.id = self.user1_id
        cb_unblock.message = MagicMock()
        cb_unblock.message.edit_text = AsyncMock()
        cb_unblock.answer = AsyncMock()

        await start_handlers.handle_unblock_confirm(cb_unblock)
        cb_unblock.message.edit_text.assert_called_once()
        self.assertIn("Partner unblocked / restored!", cb_unblock.message.edit_text.call_args[0][0])

        # Verify UserBlock is gone
        async with get_db_session() as session:
            stmt = select(UserBlock).where(
                UserBlock.user_id == self.user1_id,
                UserBlock.blocked_user_id == self.user2_id,
            )
            block_after = (await session.execute(stmt)).scalar_one_or_none()
            self.assertIsNone(block_after)

    async def test_close_chapter_sunset_permanent(self):
        cb_dur = MagicMock(spec=CallbackQuery)
        cb_dur.data = f"fb_sunset_dur:{self.user2_id}:perm"
        cb_dur.from_user = MagicMock(spec=TgUser)
        cb_dur.from_user.id = self.user1_id
        cb_dur.message = MagicMock()
        cb_dur.message.edit_text = AsyncMock()
        cb_dur.answer = AsyncMock()

        await feedback_handlers.handle_feedback_sunset_duration(cb_dur)
        cb_dur.message.edit_text.assert_called_once()
        self.assertIn("Chapter closed on a high note!", cb_dur.message.edit_text.call_args[0][0])
        self.assertIn("Some conversations are best kept as perfect one-time memories", cb_dur.message.edit_text.call_args[0][0])

        async with get_db_session() as session:
            stmt = select(UserBlock).where(
                UserBlock.user_id == self.user1_id,
                UserBlock.blocked_user_id == self.user2_id,
            )
            block = (await session.execute(stmt)).scalar_one_or_none()
            self.assertIsNotNone(block)
            self.assertEqual(block.reason, "closed_chapter")
            self.assertIsNone(block.expires_at)

    async def test_open_to_rematch(self):
        # First put a block
        cb_dur = MagicMock(spec=CallbackQuery)
        cb_dur.data = f"fb_sunset_dur:{self.user2_id}:1d"
        cb_dur.from_user = MagicMock(spec=TgUser)
        cb_dur.from_user.id = self.user1_id
        cb_dur.message = MagicMock()
        cb_dur.message.edit_text = AsyncMock()
        cb_dur.answer = AsyncMock()
        await feedback_handlers.handle_feedback_sunset_duration(cb_dur)

        # Now click open to rematch
        cb_rematch = MagicMock(spec=CallbackQuery)
        cb_rematch.data = f"fb_rematch_ok:{self.session_id}:{self.user2_id}"
        cb_rematch.from_user = MagicMock(spec=TgUser)
        cb_rematch.from_user.id = self.user1_id
        cb_rematch.message = MagicMock()
        cb_rematch.message.edit_text = AsyncMock()
        cb_rematch.answer = AsyncMock()

        await feedback_handlers.handle_feedback_rematch_ok(cb_rematch)
        cb_rematch.message.edit_text.assert_called_once()
        self.assertIn("Preferences Saved!", cb_rematch.message.edit_text.call_args[0][0])

        # Verify block removed
        async with get_db_session() as session:
            stmt = select(UserBlock).where(
                UserBlock.user_id == self.user1_id,
                UserBlock.blocked_user_id == self.user2_id,
            )
            block = (await session.execute(stmt)).scalar_one_or_none()
            self.assertIsNone(block)

    async def test_dislike_avoid_matching_prompt_and_temporary_block(self):
        # 1. User rates dislike
        bot = AsyncMock(spec=Bot)
        cb_fb = MagicMock(spec=CallbackQuery)
        cb_fb.data = f"fb:{self.session_id}:{self.user2_id}:dislike"
        cb_fb.from_user = MagicMock(spec=TgUser)
        cb_fb.from_user.id = self.user1_id
        cb_fb.from_user.full_name = "User One"
        cb_fb.from_user.username = "userone"
        cb_fb.message = MagicMock()
        cb_fb.message.edit_text = AsyncMock()
        cb_fb.answer = AsyncMock()

        await feedback_handlers.handle_feedback_callback(cb_fb, bot)
        cb_fb.message.edit_text.assert_called_once()
        text_fb = cb_fb.message.edit_text.call_args[0][0]
        self.assertIn("👎 Not for me", text_fb)
        self.assertIn("Avoid Matching", text_fb)
        self.assertIn("Open to Rematch", text_fb)

        # 2. User clicks Avoid Matching
        cb_prompt = MagicMock(spec=CallbackQuery)
        cb_prompt.data = f"block_prompt:{self.session_id}:{self.user2_id}:dislike"
        cb_prompt.from_user = MagicMock(spec=TgUser)
        cb_prompt.from_user.id = self.user1_id
        cb_prompt.message = MagicMock()
        cb_prompt.message.edit_text = AsyncMock()
        cb_prompt.answer = AsyncMock()

        await feedback_handlers.handle_block_prompt(cb_prompt)
        cb_prompt.message.edit_text.assert_called_once()
        self.assertIn("Do Not Connect Again", cb_prompt.message.edit_text.call_args[0][0])
        # Back button should route to fb_back_dislike
        kb = cb_prompt.message.edit_text.call_args[1]["reply_markup"]
        back_btn = [btn for row in kb.inline_keyboard for btn in row if btn.text == "🔙 Back"][0]
        self.assertEqual(back_btn.callback_data, f"fb_back_dislike:{self.session_id}:{self.user2_id}")

        # 3. User chooses 3 days cooldown
        cb_dur = MagicMock(spec=CallbackQuery)
        cb_dur.data = f"block_dur:{self.user2_id}:3d"
        cb_dur.from_user = MagicMock(spec=TgUser)
        cb_dur.from_user.id = self.user1_id
        cb_dur.message = MagicMock()
        cb_dur.message.edit_text = AsyncMock()
        cb_dur.answer = AsyncMock()

        await feedback_handlers.handle_block_duration(cb_dur)
        cb_dur.message.edit_text.assert_called_once()
        self.assertIn("Partner Blocked!", cb_dur.message.edit_text.call_args[0][0])
        self.assertIn("3 days", cb_dur.message.edit_text.call_args[0][0])

        async with get_db_session() as session:
            stmt = select(UserBlock).where(
                UserBlock.user_id == self.user1_id,
                UserBlock.blocked_user_id == self.user2_id,
            )
            block = (await session.execute(stmt)).scalar_one_or_none()
            self.assertIsNotNone(block)
            self.assertEqual(block.reason, "dislike")
            self.assertIsNotNone(block.expires_at)

    async def test_dislike_avoid_matching_permanent(self):
        cb_dur = MagicMock(spec=CallbackQuery)
        cb_dur.data = f"block_dur:{self.user2_id}:perm"
        cb_dur.from_user = MagicMock(spec=TgUser)
        cb_dur.from_user.id = self.user1_id
        cb_dur.message = MagicMock()
        cb_dur.message.edit_text = AsyncMock()
        cb_dur.answer = AsyncMock()

        await feedback_handlers.handle_block_duration(cb_dur)
        cb_dur.message.edit_text.assert_called_once()
        self.assertIn("Partner Blocked!", cb_dur.message.edit_text.call_args[0][0])
        self.assertIn("permanently", cb_dur.message.edit_text.call_args[0][0])

        async with get_db_session() as session:
            stmt = select(UserBlock).where(
                UserBlock.user_id == self.user1_id,
                UserBlock.blocked_user_id == self.user2_id,
            )
            block = (await session.execute(stmt)).scalar_one_or_none()
            self.assertIsNotNone(block)
            self.assertEqual(block.reason, "dislike")
            self.assertIsNone(block.expires_at)

    async def test_dislike_open_to_rematch(self):
        # 1. Rate dislike
        bot = AsyncMock(spec=Bot)
        cb_fb = MagicMock(spec=CallbackQuery)
        cb_fb.data = f"fb:{self.session_id}:{self.user2_id}:dislike"
        cb_fb.from_user = MagicMock(spec=TgUser)
        cb_fb.from_user.id = self.user1_id
        cb_fb.from_user.full_name = "User One"
        cb_fb.from_user.username = "userone"
        cb_fb.message = MagicMock()
        cb_fb.message.edit_text = AsyncMock()
        cb_fb.answer = AsyncMock()
        await feedback_handlers.handle_feedback_callback(cb_fb, bot)

        # 2. Set temporary block
        cb_dur = MagicMock(spec=CallbackQuery)
        cb_dur.data = f"block_dur:{self.user2_id}:1d"
        cb_dur.from_user = MagicMock(spec=TgUser)
        cb_dur.from_user.id = self.user1_id
        cb_dur.message = MagicMock()
        cb_dur.message.edit_text = AsyncMock()
        cb_dur.answer = AsyncMock()
        await feedback_handlers.handle_block_duration(cb_dur)

        # 3. User chooses Open to Rematch
        cb_rematch = MagicMock(spec=CallbackQuery)
        cb_rematch.data = f"fb_rematch_ok:{self.session_id}:{self.user2_id}"
        cb_rematch.from_user = MagicMock(spec=TgUser)
        cb_rematch.from_user.id = self.user1_id
        cb_rematch.message = MagicMock()
        cb_rematch.message.edit_text = AsyncMock()
        cb_rematch.answer = AsyncMock()

        await feedback_handlers.handle_feedback_rematch_ok(cb_rematch)
        cb_rematch.message.edit_text.assert_called_once()
        msg_text = cb_rematch.message.edit_text.call_args[0][0]
        self.assertIn("Preferences Saved!", msg_text)
        self.assertIn("👎 Not for me", msg_text)

        # 4. Block must be removed
        async with get_db_session() as session:
            stmt = select(UserBlock).where(
                UserBlock.user_id == self.user1_id,
                UserBlock.blocked_user_id == self.user2_id,
            )
            block = (await session.execute(stmt)).scalar_one_or_none()
            self.assertIsNone(block)

    async def test_dislike_back_navigation(self):
        cb_back = MagicMock(spec=CallbackQuery)
        cb_back.data = f"fb_back_dislike:{self.session_id}:{self.user2_id}"
        cb_back.from_user = MagicMock(spec=TgUser)
        cb_back.from_user.id = self.user1_id
        cb_back.message = MagicMock()
        cb_back.message.edit_text = AsyncMock()
        cb_back.answer = AsyncMock()

        await feedback_handlers.handle_feedback_back_dislike(cb_back)
        cb_back.message.edit_text.assert_called_once()
        self.assertIn("👎 Not for me", cb_back.message.edit_text.call_args[0][0])
        self.assertIn("Avoid Matching", cb_back.message.edit_text.call_args[0][0])

    async def test_reconnect_prompt_and_send_and_cooldown(self):
        bot = MagicMock()
        bot.send_message = AsyncMock()

        # 1. Open Reconnect Prompt
        cb_prompt = MagicMock(spec=CallbackQuery)
        cb_prompt.data = f"reconn_prompt:{self.session_id}:{self.user2_id}"
        cb_prompt.from_user = MagicMock(spec=TgUser)
        cb_prompt.from_user.id = self.user1_id
        cb_prompt.message = MagicMock()
        cb_prompt.message.edit_text = AsyncMock()
        cb_prompt.answer = AsyncMock()

        await feedback_handlers.handle_reconnect_prompt(cb_prompt)
        cb_prompt.message.edit_text.assert_called_once()
        self.assertIn("Request Reconnection", cb_prompt.message.edit_text.call_args[0][0])

        # 2. Send Reconnect Request with "continue" note
        cb_send = MagicMock(spec=CallbackQuery)
        cb_send.data = f"reconn_send:{self.session_id}:{self.user2_id}:continue"
        cb_send.from_user = MagicMock(spec=TgUser)
        cb_send.from_user.id = self.user1_id
        cb_send.message = MagicMock()
        cb_send.message.edit_text = AsyncMock()
        cb_send.answer = AsyncMock()

        await feedback_handlers.handle_reconnect_send(cb_send, bot)
        cb_send.message.edit_text.assert_called_once()
        self.assertIn("Reconnection Request Sent!", cb_send.message.edit_text.call_args[0][0])
        bot.send_message.assert_called_once()
        self.assertEqual(bot.send_message.call_args[1]["chat_id"], self.user2_id)
        self.assertIn("Anonymous Reconnection Request", bot.send_message.call_args[1]["text"])
        self.assertIn("Want to continue our convo!", bot.send_message.call_args[1]["text"])

        # 3. Attempting to prompt or send again within 24h hits cooldown
        cb_prompt2 = MagicMock(spec=CallbackQuery)
        cb_prompt2.data = f"reconn_prompt:{self.session_id}:{self.user2_id}"
        cb_prompt2.from_user = MagicMock(spec=TgUser)
        cb_prompt2.from_user.id = self.user1_id
        cb_prompt2.message = MagicMock()
        cb_prompt2.message.edit_text = AsyncMock()
        cb_prompt2.answer = AsyncMock()

        await feedback_handlers.handle_reconnect_prompt(cb_prompt2)
        cb_prompt2.message.edit_text.assert_not_called()
        cb_prompt2.answer.assert_called_once()
        self.assertIn("within the last 24 hours", cb_prompt2.answer.call_args[0][0])

    async def test_reconnect_silent_drop_when_blocked_by_recipient(self):
        bot = MagicMock()
        bot.send_message = AsyncMock()

        # User 2 blocks User 1
        async with get_db_session() as session:
            block = UserBlock(
                user_id=self.user2_id,
                blocked_user_id=self.user1_id,
                expires_at=None,
                reason="manual",
            )
            session.add(block)
            await session.commit()

        # User 1 sends reconnect request to User 2
        cb_send = MagicMock(spec=CallbackQuery)
        cb_send.data = f"reconn_send:{self.session_id}:{self.user2_id}:accident"
        cb_send.from_user = MagicMock(spec=TgUser)
        cb_send.from_user.id = self.user1_id
        cb_send.message = MagicMock()
        cb_send.message.edit_text = AsyncMock()
        cb_send.answer = AsyncMock()

        await feedback_handlers.handle_reconnect_send(cb_send, bot)

        # Sender still sees normal confirmation
        cb_send.message.edit_text.assert_called_once()
        self.assertIn("Reconnection Request Sent!", cb_send.message.edit_text.call_args[0][0])
        # Recipient is NOT notified
        bot.send_message.assert_not_called()

        # DB status is recorded as declined
        async with get_db_session() as session:
            stmt = select(ReconnectRequest).where(
                ReconnectRequest.sender_id == self.user1_id,
                ReconnectRequest.recipient_id == self.user2_id,
            )
            req = (await session.execute(stmt)).scalar_one()
            self.assertEqual(req.status, "declined")

    async def test_reconnect_accept_both_free_pairs_immediately(self):
        bot = MagicMock()
        bot.send_message = AsyncMock()

        # Create pending request
        cb_send = MagicMock(spec=CallbackQuery)
        cb_send.data = f"reconn_send:{self.session_id}:{self.user2_id}:none"
        cb_send.from_user = MagicMock(spec=TgUser)
        cb_send.from_user.id = self.user1_id
        cb_send.message = MagicMock()
        cb_send.message.edit_text = AsyncMock()
        cb_send.answer = AsyncMock()
        await feedback_handlers.handle_reconnect_send(cb_send, bot)

        async with get_db_session() as session:
            stmt = select(ReconnectRequest).where(
                ReconnectRequest.sender_id == self.user1_id,
                ReconnectRequest.recipient_id == self.user2_id,
            )
            req = (await session.execute(stmt)).scalar_one()
            req_id = req.id

        bot.send_message.reset_mock()

        # Recipient clicks Accept & Connect
        cb_accept = MagicMock(spec=CallbackQuery)
        cb_accept.data = f"reconn_accept:{req_id}"
        cb_accept.from_user = MagicMock(spec=TgUser)
        cb_accept.from_user.id = self.user2_id
        cb_accept.message = MagicMock()
        cb_accept.message.edit_text = AsyncMock()
        cb_accept.answer = AsyncMock()

        await feedback_handlers.handle_reconnect_accept(cb_accept, bot)
        cb_accept.message.edit_text.assert_called_once()
        self.assertIn("Reconnected with Past Partner!", cb_accept.message.edit_text.call_args[0][0])
        bot.send_message.assert_called_once()
        self.assertEqual(bot.send_message.call_args[1]["chat_id"], self.user1_id)

        async with get_db_session() as session:
            u1 = (await session.execute(select(User).where(User.id == self.user1_id))).scalar_one()
            u2 = (await session.execute(select(User).where(User.id == self.user2_id))).scalar_one()
            self.assertEqual(u1.current_partner_id, self.user2_id)
            self.assertEqual(u2.current_partner_id, self.user1_id)

    async def test_reconnect_accept_while_sender_busy_then_connect_now(self):
        bot = MagicMock()
        bot.send_message = AsyncMock()

        # Create pending request
        cb_send = MagicMock(spec=CallbackQuery)
        cb_send.data = f"reconn_send:{self.session_id}:{self.user2_id}:forgot"
        cb_send.from_user = MagicMock(spec=TgUser)
        cb_send.from_user.id = self.user1_id
        cb_send.message = MagicMock()
        cb_send.message.edit_text = AsyncMock()
        cb_send.answer = AsyncMock()
        await feedback_handlers.handle_reconnect_send(cb_send, bot)

        async with get_db_session() as session:
            stmt = select(ReconnectRequest).where(
                ReconnectRequest.sender_id == self.user1_id,
                ReconnectRequest.recipient_id == self.user2_id,
            )
            req = (await session.execute(stmt)).scalar_one()
            req_id = req.id
            # Simulate sender being in another active chat
            u1 = (await session.execute(select(User).where(User.id == self.user1_id))).scalar_one()
            u1.current_partner_id = 7777777
            await session.commit()

        bot.send_message.reset_mock()

        # Recipient clicks Accept while sender is busy
        cb_accept = MagicMock(spec=CallbackQuery)
        cb_accept.data = f"reconn_accept:{req_id}"
        cb_accept.from_user = MagicMock(spec=TgUser)
        cb_accept.from_user.id = self.user2_id
        cb_accept.message = MagicMock()
        cb_accept.message.edit_text = AsyncMock()
        cb_accept.answer = AsyncMock()

        await feedback_handlers.handle_reconnect_accept(cb_accept, bot)
        cb_accept.message.edit_text.assert_called_once()
        self.assertIn("Partner is Currently in a Chat", cb_accept.message.edit_text.call_args[0][0])
        bot.send_message.assert_called_once()
        self.assertEqual(bot.send_message.call_args[1]["chat_id"], self.user1_id)

        # Sender finishes chat and taps Connect Now
        async with get_db_session() as session:
            u1 = (await session.execute(select(User).where(User.id == self.user1_id))).scalar_one()
            u1.current_partner_id = None
            await session.commit()

        bot.send_message.reset_mock()
        cb_conn = MagicMock(spec=CallbackQuery)
        cb_conn.data = f"reconn_connect_now:{req_id}"
        cb_conn.from_user = MagicMock(spec=TgUser)
        cb_conn.from_user.id = self.user1_id
        cb_conn.message = MagicMock()
        cb_conn.message.edit_text = AsyncMock()
        cb_conn.answer = AsyncMock()

        await feedback_handlers.handle_reconnect_connect_now(cb_conn, bot)
        cb_conn.message.edit_text.assert_called_once()
        self.assertIn("Reconnected with Past Partner!", cb_conn.message.edit_text.call_args[0][0])
        bot.send_message.assert_called_once()
        self.assertEqual(bot.send_message.call_args[1]["chat_id"], self.user2_id)

        async with get_db_session() as session:
            u1 = (await session.execute(select(User).where(User.id == self.user1_id))).scalar_one()
            u2 = (await session.execute(select(User).where(User.id == self.user2_id))).scalar_one()
            self.assertEqual(u1.current_partner_id, self.user2_id)
            self.assertEqual(u2.current_partner_id, self.user1_id)

    async def test_reconnect_decline_and_block(self):
        bot = MagicMock()
        bot.send_message = AsyncMock()

        # Send request
        cb_send = MagicMock(spec=CallbackQuery)
        cb_send.data = f"reconn_send:{self.session_id}:{self.user2_id}:none"
        cb_send.from_user = MagicMock(spec=TgUser)
        cb_send.from_user.id = self.user1_id
        cb_send.message = MagicMock()
        cb_send.message.edit_text = AsyncMock()
        cb_send.answer = AsyncMock()
        await feedback_handlers.handle_reconnect_send(cb_send, bot)

        async with get_db_session() as session:
            stmt = select(ReconnectRequest).where(
                ReconnectRequest.sender_id == self.user1_id,
                ReconnectRequest.recipient_id == self.user2_id,
            )
            req = (await session.execute(stmt)).scalar_one()
            req_id = req.id

        bot.send_message.reset_mock()

        # Recipient clicks Block Partner
        cb_block = MagicMock(spec=CallbackQuery)
        cb_block.data = f"reconn_block:{req_id}"
        cb_block.from_user = MagicMock(spec=TgUser)
        cb_block.from_user.id = self.user2_id
        cb_block.message = MagicMock()
        cb_block.message.edit_text = AsyncMock()
        cb_block.answer = AsyncMock()

        await feedback_handlers.handle_reconnect_block(cb_block)
        cb_block.message.edit_text.assert_called_once()
        self.assertIn("Partner Permanently Blocked", cb_block.message.edit_text.call_args[0][0])
        # Sender is not notified
        bot.send_message.assert_not_called()

        async with get_db_session() as session:
            stmt_b = select(UserBlock).where(
                UserBlock.user_id == self.user2_id,
                UserBlock.blocked_user_id == self.user1_id,
            )
            block = (await session.execute(stmt_b)).scalar_one_or_none()
            self.assertIsNotNone(block)
            self.assertEqual(block.reason, "reconnect_block")
            self.assertIsNone(block.expires_at)

    async def test_recent_partners_pagination(self):
        # Create 6 more completed sessions (total 7)
        async with get_db_session() as session:
            now = datetime.now(timezone.utc)
            for i in range(6):
                ended = now - timedelta(minutes=(i + 2) * 5)
                cs = ChatSession(
                    user1_id=self.user1_id,
                    user2_id=self.user2_id,
                    is_active=False,
                    started_at=ended - timedelta(minutes=2),
                    ended_at=ended,
                    last_activity_at=ended,
                    messages_count=i + 2,
                )
                session.add(cs)
            await session.commit()

        # 1. /recent command shows Page 1 (Partners #1..#5 and [1/2])
        bot = MagicMock()
        state = AsyncMock()
        msg = MagicMock(spec=Message)
        msg.from_user = MagicMock(spec=TgUser)
        msg.from_user.id = self.user1_id
        msg.answer = AsyncMock()

        await matchmaking_handlers.handle_recent_command(msg, bot, state)
        msg.answer.assert_called_once()
        kb1 = msg.answer.call_args[1]["reply_markup"]
        texts1 = [btn.text for row in kb1.inline_keyboard for btn in row]
        self.assertTrue(any("Partner #1" in t for t in texts1))
        self.assertTrue(any("Partner #5" in t for t in texts1))
        self.assertFalse(any("Partner #6" in t for t in texts1))
        self.assertIn("[1/2]", texts1)

        # 2. Clicking next page (recent_page:2) shows Partners #6..#7 and [2/2]
        cb_page2 = MagicMock(spec=CallbackQuery)
        cb_page2.data = "recent_page:2"
        cb_page2.from_user = MagicMock(spec=TgUser)
        cb_page2.from_user.id = self.user1_id
        cb_page2.message = MagicMock()
        cb_page2.message.edit_text = AsyncMock()
        cb_page2.answer = AsyncMock()

        await feedback_handlers.handle_recent_list_callback(cb_page2)
        cb_page2.message.edit_text.assert_called_once()
        kb2 = cb_page2.message.edit_text.call_args[1]["reply_markup"]
        texts2 = [btn.text for row in kb2.inline_keyboard for btn in row]
        self.assertTrue(any("Partner #6" in t for t in texts2))
        self.assertTrue(any("Partner #7" in t for t in texts2))
        self.assertIn("[2/2]", texts2)


if __name__ == "__main__":
    unittest.main()
