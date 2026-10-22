"""Comprehensive test suite for Anti-Cheat & Anti-Exploit protections:
1. Profile sex change 2-day cooldown (48 hours)
2. Minimum engagement threshold (>= 15s AND >= 4 messages)
3. Serial downvoter neutralization & stealth damping
4. Accurate priority delta recording and clean reversion
"""

from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import AsyncMock, MagicMock

from aiogram.types import CallbackQuery, Message, User as TgUser
from sqlalchemy import delete, select

from app.config import settings
from app.database.crud import (
    check_is_serial_downvoter,
    delete_or_reset_feedback,
    get_feedback_by_session_and_user,
    get_or_create_user,
    update_or_override_feedback,
    update_user_profile,
)
from app.database.models import ChatSession, Feedback, User
from app.database.session import engine, get_db_session, init_db
from app.handlers import start as start_handlers
from app.states.states import OnboardingState


class TestAntiCheatRating(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await init_db()
        self.user_a = 8800101
        self.user_b = 8800102
        self.user_c = 8800103
        self.user_d = 8800104
        self.user_e = 8800105
        self.all_users = [self.user_a, self.user_b, self.user_c, self.user_d, self.user_e]

        async with get_db_session() as session:
            # Clean up test rows
            await session.execute(
                delete(Feedback).where(
                    Feedback.from_user_id.in_(self.all_users)
                    | Feedback.to_user_id.in_(self.all_users)
                )
            )
            await session.execute(
                delete(ChatSession).where(
                    ChatSession.user1_id.in_(self.all_users)
                    | ChatSession.user2_id.in_(self.all_users)
                )
            )
            await session.execute(
                delete(User).where(User.id.in_(self.all_users))
            )
            await session.commit()

            # Seed base test users
            for uid in self.all_users:
                u = await get_or_create_user(session, user_id=uid, username=f"user_{uid}")
                u.priority = 50
                u.spam_flags_count = 0
            await session.commit()

    async def asyncTearDown(self):
        async with get_db_session() as session:
            await session.execute(
                delete(Feedback).where(
                    Feedback.from_user_id.in_(self.all_users)
                    | Feedback.to_user_id.in_(self.all_users)
                )
            )
            await session.execute(
                delete(ChatSession).where(
                    ChatSession.user1_id.in_(self.all_users)
                    | ChatSession.user2_id.in_(self.all_users)
                )
            )
            await session.execute(
                delete(User).where(User.id.in_(self.all_users))
            )
            await session.commit()

        await engine.dispose()

    async def test_solution_1_sex_change_cooldown(self):
        """Test that profile sex can only be changed once every 2 days (48 hours)."""
        async with get_db_session() as session:
            # Set initial profile as Male
            user = await update_user_profile(session, self.user_a, sex="male", orientation="straight")
            self.assertEqual(user.sex, "male")
            self.assertIsNotNone(user.sex_updated_at)

        # Attempt to switch to Female immediately via handle_sex_selected
        state = AsyncMock()
        callback = AsyncMock(spec=CallbackQuery)
        callback.from_user = TgUser(id=self.user_a, is_bot=False, first_name="Troll")
        callback.data = "onboarding_sex:female"
        callback.answer = AsyncMock()

        await start_handlers.handle_sex_selected(callback, state)

        # Should be blocked with an alert
        callback.answer.assert_called_once()
        call_args = callback.answer.call_args
        self.assertIn("Profile sex can only be changed once every 2 days", call_args[0][0])
        self.assertTrue(call_args[1].get("show_alert"))
        state.set_state.assert_not_called()

        # Re-selecting the SAME sex (e.g. user just updating orientation) should be permitted
        callback.reset_mock()
        state.reset_mock()
        callback.data = "onboarding_sex:male"
        callback.message = AsyncMock()
        callback.message.edit_text = AsyncMock()

        await start_handlers.handle_sex_selected(callback, state)
        state.set_state.assert_called_once_with(OnboardingState.waiting_for_orientation)

        # Fast forward time past 48 hours (e.g. 49 hours ago)
        async with get_db_session() as session:
            u = await session.get(User, self.user_a)
            u.sex_updated_at = datetime.now(timezone.utc) - timedelta(hours=49)
            await session.commit()

        # Now switching to Female should succeed
        callback.reset_mock()
        state.reset_mock()
        callback.data = "onboarding_sex:female"
        callback.message = AsyncMock()
        callback.message.edit_text = AsyncMock()

        await start_handlers.handle_sex_selected(callback, state)
        state.set_state.assert_called_once_with(OnboardingState.waiting_for_orientation)

    async def test_solution_2_minimum_engagement_threshold(self):
        """Test that dislike delta is 0 unless session >= 15s AND messages >= 4."""
        now = datetime.now(timezone.utc)

        # Case A: Long duration (60s) BUT < 4 messages (e.g. 2 messages)
        async with get_db_session() as session:
            sess_unengaged = ChatSession(
                user1_id=self.user_a,
                user2_id=self.user_b,
                is_active=False,
                started_at=now - timedelta(seconds=60),
                ended_at=now,
                messages_count=2,
            )
            session.add(sess_unengaged)
            await session.commit()
            await session.refresh(sess_unengaged)
            sess_id_a = sess_unengaged.id

            res_a = await update_or_override_feedback(
                session, sess_id_a, self.user_a, self.user_b, "dislike"
            )
            self.assertTrue(res_a["is_unengaged"])
            self.assertEqual(res_a["delta"], 0, "Unengaged chat (messages < 4) must produce delta 0")
            self.assertEqual(res_a["new_priority"], 50)

        # Case B: < 15 seconds duration BUT >= 4 messages (using distinct rater user_c)
        async with get_db_session() as session:
            sess_short = ChatSession(
                user1_id=self.user_c,
                user2_id=self.user_b,
                is_active=False,
                started_at=now - timedelta(seconds=5),
                ended_at=now,
                messages_count=6,
            )
            session.add(sess_short)
            await session.commit()
            await session.refresh(sess_short)
            sess_id_b = sess_short.id

            res_b = await update_or_override_feedback(
                session, sess_id_b, self.user_c, self.user_b, "dislike"
            )
            self.assertTrue(res_b["is_unengaged"])
            self.assertEqual(res_b["delta"], 0, "Short chat (<15s) must produce delta 0")
            self.assertEqual(res_b["new_priority"], 50)

        # Case C: Fully engaged (>= 15s AND >= 4 messages, using distinct rater user_d)
        async with get_db_session() as session:
            sess_engaged = ChatSession(
                user1_id=self.user_d,
                user2_id=self.user_e,
                is_active=False,
                started_at=now - timedelta(seconds=30),
                ended_at=now,
                messages_count=8,
            )
            session.add(sess_engaged)
            await session.commit()
            await session.refresh(sess_engaged)
            sess_id_c = sess_engaged.id

            res_c = await update_or_override_feedback(
                session, sess_id_c, self.user_d, self.user_e, "dislike"
            )
            self.assertFalse(res_c["is_unengaged"])
            self.assertEqual(res_c["delta"], -1)
            self.assertEqual(res_c["new_priority"], 49)

    async def test_solution_3_serial_downvoter_neutralization(self):
        """Test serial downvoters are detected and their negative votes dampened to delta 0."""
        now = datetime.now(timezone.utc)
        troll_id = self.user_a

        # Create 2 previous engaged sessions where troll gave negative feedback
        async with get_db_session() as session:
            s1 = ChatSession(
                user1_id=troll_id, user2_id=self.user_b, is_active=False,
                started_at=now - timedelta(minutes=10), ended_at=now - timedelta(minutes=9),
                messages_count=10,
            )
            s2 = ChatSession(
                user1_id=troll_id, user2_id=self.user_c, is_active=False,
                started_at=now - timedelta(minutes=8), ended_at=now - timedelta(minutes=7),
                messages_count=10,
            )
            session.add_all([s1, s2])
            await session.commit()
            await session.refresh(s1)
            await session.refresh(s2)

            # Troll downvotes partner B and partner C
            await update_or_override_feedback(session, s1.id, troll_id, self.user_b, "dislike")
            await update_or_override_feedback(session, s2.id, troll_id, self.user_c, "dislike")

        # Now check if troll is flagged as serial downvoter for their next chat
        async with get_db_session() as session:
            is_serial = await check_is_serial_downvoter(session, troll_id)
            self.assertTrue(is_serial, "User with 2 consecutive downvotes must be flagged as serial downvoter")

            # 3rd chat: fully engaged session with user D
            s3 = ChatSession(
                user1_id=troll_id, user2_id=self.user_d, is_active=False,
                started_at=now - timedelta(minutes=5), ended_at=now - timedelta(minutes=4),
                messages_count=12,
            )
            session.add(s3)
            await session.commit()
            await session.refresh(s3)

            # Troll tries to downvote user D
            res3 = await update_or_override_feedback(session, s3.id, troll_id, self.user_d, "dislike")
            self.assertTrue(res3["is_serial_downvoter"])
            self.assertEqual(res3["delta"], 0, "Serial downvoter dislike must be dampened to 0")
            self.assertEqual(res3["new_priority"], 50, "User D's priority must be protected")

            # Also verify troll trying to submit spam does NOT penalize or increment spam_flags_count
            res3_spam = await update_or_override_feedback(session, s3.id, troll_id, self.user_d, "spam")
            self.assertEqual(res3_spam["delta"], 0)
            self.assertEqual(res3_spam["spam_flags_count"], 0, "Serial downvoter cannot manufacture spam flags")
            self.assertEqual(res3_spam["new_priority"], 50)

    async def test_feedback_delta_exact_reversion(self):
        """Verify priority_delta_applied enables exact reversion on reset/override."""
        now = datetime.now(timezone.utc)
        async with get_db_session() as session:
            # Neutralized session
            s = ChatSession(
                user1_id=self.user_a, user2_id=self.user_b, is_active=False,
                started_at=now - timedelta(seconds=5), ended_at=now, messages_count=1,
            )
            session.add(s)
            await session.commit()
            await session.refresh(s)

            # User A leaves dislike (neutralized, delta=0)
            res = await update_or_override_feedback(session, s.id, self.user_a, self.user_b, "dislike")
            self.assertEqual(res["delta"], 0)

            # Target priority remains 50
            u_b = await session.get(User, self.user_b)
            self.assertEqual(u_b.priority, 50)

            # Now User A deletes/resets feedback
            ok = await delete_or_reset_feedback(session, s.id, self.user_a)
            self.assertTrue(ok)

            # Target priority must still be exactly 50 (NOT artificially inflated to 51)
            await session.refresh(u_b)
            self.assertEqual(u_b.priority, 50)


if __name__ == "__main__":
    unittest.main()
