"""Unit and integration tests for:
1. Recent Partner Rematch Cooldown (Anti-Win-Trading)
2. AUA Student Email Verification (/verify, OTP flow, badges, and admin false-report alerts)
3. Suggestion command & input flood cooldown
"""

from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.types import CallbackQuery, Message, User as TgUser
from sqlalchemy import delete, select

from app.config import settings
from app.database.crud import (
    check_and_verify_user_code,
    create_suggestion,
    get_or_create_user,
    is_email_already_verified,
    set_user_verification_code,
    update_or_override_feedback,
    update_user_profile,
)
from app.database.models import ChatSession, Feedback, Suggestion, User
from app.database.session import engine, get_db_session, init_db
from app.handlers import feedback as feedback_handlers
from app.handlers import start as start_handlers
from app.handlers import verification as verification_handlers
from app.services.email_verification import (
    get_current_academic_year,
    get_partner_verification_badge,
    get_user_verification_status,
    is_valid_aua_email,
    send_verification_email,
)
from app.services.matcher import match_user_in_queue
from app.states.states import SuggestionState, VerificationState


class TestVerificationAndCooldowns(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await init_db()
        self.u1 = 889901
        self.u2 = 889902
        self.u3 = 889903
        self.all_users = [self.u1, self.u2, self.u3]

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
                delete(Suggestion).where(Suggestion.user_id.in_(self.all_users))
            )
            await session.execute(
                delete(User).where(User.id.in_(self.all_users))
            )
            await session.commit()

            # Seed base users
            u1_db = await get_or_create_user(session, self.u1, username="test_u1")
            u2_db = await get_or_create_user(session, self.u2, username="test_u2")
            u3_db = await get_or_create_user(session, self.u3, username="test_u3")
            for u in (u1_db, u2_db, u3_db):
                u.priority = 50
                u.sex = "male"
                u.orientation = "straight"
                u.target_sexes = "female,male"
                u.target_orientations = "straight,bisexual,gay"
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
                delete(Suggestion).where(Suggestion.user_id.in_(self.all_users))
            )
            await session.execute(
                delete(User).where(User.id.in_(self.all_users))
            )
            await session.commit()

        await engine.dispose()

    async def test_01_recent_partner_rematch_cooldown(self):
        """Test that recently connected partners cannot immediately match with each other again."""
        now = datetime.now(timezone.utc)

        async with get_db_session() as session:
            # Create a completed chat session between u1 and u2 from 2 minutes ago
            recent_sess = ChatSession(
                user1_id=self.u1,
                user2_id=self.u2,
                is_active=False,
                started_at=now - timedelta(minutes=5),
                ended_at=now - timedelta(minutes=2),
                last_activity_at=now - timedelta(minutes=2),
                messages_count=6,
            )
            session.add(recent_sess)

            # Put u2 in search queue
            u2 = await session.get(User, self.u2)
            u2.is_searching = True

            # Put u3 in search queue
            u3 = await session.get(User, self.u3)
            u3.is_searching = True
            await session.commit()

            # Now u1 searches: u2 is on rematch cooldown, so u1 MUST match with u3!
            candidate, chat_sess = await match_user_in_queue(
                session, user_id=self.u1, allowed_candidate_ids=self.all_users
            )
            self.assertIsNotNone(candidate)
            self.assertEqual(candidate.id, self.u3, "u1 should match with u3, skipping u2 due to rematch cooldown")

            # Reset u1 and u2 to be searching alone (no u3 available)
            candidate.current_partner_id = None
            candidate.is_searching = False
            u1 = await session.get(User, self.u1)
            u1.current_partner_id = None
            u1.is_searching = False
            u2.is_searching = True
            await session.commit()

            # u1 searches when only u2 is in queue (still on cooldown)
            candidate_none, sess_none = await match_user_in_queue(
                session, user_id=self.u1, allowed_candidate_ids=self.all_users
            )
            self.assertIsNone(candidate_none, "u1 must not rematch with u2 while 20m cooldown is active")

            # Fast-forward chat session past 20 minutes (e.g. 25 minutes ago)
            recent_sess.ended_at = now - timedelta(minutes=25)
            recent_sess.started_at = now - timedelta(minutes=30)
            recent_sess.last_activity_at = now - timedelta(minutes=25)
            u2.is_searching = True
            await session.commit()

            # Now u1 searches again: cooldown expired, u1 and u2 can match!
            c_ok, s_ok = await match_user_in_queue(
                session, user_id=self.u1, allowed_candidate_ids=self.all_users
            )
            self.assertIsNotNone(c_ok)
            self.assertEqual(c_ok.id, self.u2, "u1 should match with u2 once cooldown has expired")

    def test_02_email_validator(self):
        """Test is_valid_aua_email rejects non-AUA and accepts @edu.aua.am."""
        self.assertTrue(is_valid_aua_email("student@edu.aua.am"))
        self.assertTrue(is_valid_aua_email("john_smith_26@edu.aua.am"))
        self.assertTrue(is_valid_aua_email("FIRST.LAST@EDU.AUA.AM"))

        self.assertFalse(is_valid_aua_email("student@gmail.com"))
        self.assertFalse(is_valid_aua_email("student@aua.am"))  # Must end with @edu.aua.am per spec
        self.assertFalse(is_valid_aua_email("student@yahoo.com"))
        self.assertFalse(is_valid_aua_email("fake@edu.aua.am.attacker.com"))
        self.assertFalse(is_valid_aua_email(None))
        self.assertFalse(is_valid_aua_email(""))

    async def test_03_verification_flow_and_otp(self):
        """Test the end-to-end OTP generation, delivery, and verification validation."""
        test_email = "tester_student@edu.aua.am"

        async with get_db_session() as session:
            # 1. User starts /verify
            state = AsyncMock()
            msg = AsyncMock(spec=Message)
            msg.from_user = TgUser(id=self.u1, is_bot=False, first_name="Student")
            msg.answer = AsyncMock()

            await verification_handlers.handle_verify_command(msg, state)
            state.set_state.assert_called_once_with(VerificationState.waiting_for_email)

            # 2. User enters invalid email
            msg.text = "hacker@gmail.com"
            await verification_handlers.handle_email_input(msg, state)
            self.assertIn("must be an official AUA address ending with", msg.answer.call_args[0][0])

            # 3. User enters valid email
            msg.text = test_email
            await verification_handlers.handle_email_input(msg, state)
            state.set_state.assert_called_with(VerificationState.waiting_for_code)

            # Verify OTP code was saved in DB
            u1_db = await session.get(User, self.u1)
            self.assertEqual(u1_db.email, test_email)
            self.assertIsNotNone(u1_db.verification_code)
            self.assertFalse(u1_db.is_verified)
            stored_code = u1_db.verification_code

            # 4. User enters wrong code
            msg.text = "000000"
            await verification_handlers.handle_code_input(msg, state)
            self.assertIn("Incorrect verification code", msg.answer.call_args[0][0])

            # 5. User enters correct code
            msg.text = stored_code
            await verification_handlers.handle_code_input(msg, state)
            self.assertIn("Student Verification Complete", msg.answer.call_args[0][0])

            await session.refresh(u1_db)
            self.assertTrue(u1_db.is_verified)
            self.assertIsNone(u1_db.verification_code)

            # 6. Another user cannot verify the same email
            self.assertTrue(await is_email_already_verified(session, test_email, exclude_user_id=self.u2))

    async def test_04_verified_partner_not_aua_report_warns_admin(self):
        """Test that reporting a verified student with 'not_aua' sends an immediate audit alert to admins."""
        now = datetime.now(timezone.utc)
        bot = AsyncMock()
        bot.send_message = AsyncMock()

        async with get_db_session() as session:
            # Mark u2 as verified
            u2_db = await session.get(User, self.u2)
            u2_db.is_verified = True
            u2_db.email = "verified_target@edu.aua.am"

            # Create completed chat session between u1 and u2
            sess = ChatSession(
                user1_id=self.u1,
                user2_id=self.u2,
                is_active=False,
                started_at=now - timedelta(minutes=5),
                ended_at=now,
                messages_count=10,
            )
            session.add(sess)
            await session.commit()
            await session.refresh(sess)
            sess_id = sess.id

        # u1 reports u2 as 'not_aua'
        cb = AsyncMock(spec=CallbackQuery)
        cb.data = f"fb:{sess_id}:{self.u2}:not_aua"
        cb.from_user = TgUser(id=self.u1, is_bot=False, first_name="Reporter")
        cb.message = AsyncMock()
        cb.answer = AsyncMock()

        with patch.object(settings, "ADMIN_IDS", [999999]):
            await feedback_handlers.handle_feedback_callback(cb, bot)

        # Verify bot sent the warning alert to admin!
        bot.send_message.assert_called()
        admin_call = bot.send_message.call_args
        self.assertEqual(admin_call[1]["chat_id"], 999999)
        self.assertIn("AUDIT WARNING: VERIFIED STUDENT REPORTED AS 'NOT FROM AUA'", admin_call[1]["text"])
        self.assertIn("verified_target@edu.aua.am", admin_call[1]["text"])

    async def test_05_suggestion_cooldown(self):
        """Test that manual /suggestion is rate-limited to prevent admin inbox flooding."""
        now = datetime.now(timezone.utc)
        state = AsyncMock()
        bot = AsyncMock()

        msg = AsyncMock(spec=Message)
        msg.from_user = TgUser(id=self.u1, is_bot=False, first_name="Student")
        msg.answer = AsyncMock()

        async with get_db_session() as session:
            # First suggestion: user submits
            msg.text = "Please add dark mode!"
            await feedback_handlers.handle_suggestion_input(msg, bot, state)
            self.assertIn("Thank you for your valuable feedback", msg.answer.call_args[0][0])

            # Immediate second attempt: should be blocked by cooldown
            msg.answer.reset_mock()
            await start_handlers.handle_suggestion_command(msg, state)
            self.assertIn("Suggestion Cooldown Active", msg.answer.call_args[0][0])

            # Also verify input handler directly blocks if cooldown is active
            msg.answer.reset_mock()
            msg.text = "Another spam message!"
            await feedback_handlers.handle_suggestion_input(msg, bot, state)
            self.assertIn("Suggestion Cooldown Active", msg.answer.call_args[0][0])

    async def test_06_alumni_verification_flow(self):
        """Test that @alumni.aua.am emails are accepted, grant alumni badge, and never expire."""
        alumni_email = "arman_sargsyan@alumni.aua.am"

        async with get_db_session() as session:
            state = AsyncMock()
            msg = AsyncMock(spec=Message)
            msg.from_user = TgUser(id=self.u2, is_bot=False, first_name="Alum")
            msg.answer = AsyncMock()

            # 1. Start /verify
            await verification_handlers.handle_verify_command(msg, state)
            state.set_state.assert_called_with(VerificationState.waiting_for_email)

            # 2. Enter @alumni.aua.am email
            msg.text = alumni_email
            await verification_handlers.handle_email_input(msg, state)
            state.set_state.assert_called_with(VerificationState.waiting_for_code)

            # 3. Retrieve code and submit
            u2_db = await session.get(User, self.u2)
            self.assertEqual(u2_db.email, alumni_email)
            code = u2_db.verification_code
            msg.text = code
            await verification_handlers.handle_code_input(msg, state)
            self.assertIn("Alumni Verification Complete", msg.answer.call_args[0][0])

            # 4. Check DB status
            await session.refresh(u2_db)
            self.assertTrue(u2_db.is_verified)
            self.assertTrue(u2_db.is_alumni)
            self.assertEqual(get_user_verification_status(u2_db), "alumni")

            # 5. Badge check
            badge = get_partner_verification_badge(u2_db)
            self.assertIn("Verified AUA Graduate / Alumni", badge)
            self.assertIn("🎓🏛️", badge)

    async def test_07_annual_expiry_and_reverification(self):
        """Test annual September 1st expiration logic and in-chat badge changes."""
        now = datetime.now(timezone.utc)
        curr_year = get_current_academic_year(now)

        async with get_db_session() as session:
            u3_db = await session.get(User, self.u3)
            u3_db.is_verified = True
            u3_db.is_alumni = False
            u3_db.email = "tatev_hakobyan@edu.aua.am"
            u3_db.sex = "female"
            u3_db.is_gender_verified = True

            # 1. Active student verified in current academic year
            u3_db.academic_year_verified = curr_year
            await session.commit()
            self.assertEqual(get_user_verification_status(u3_db), "student")
            badge_active = get_partner_verification_badge(u3_db)
            self.assertIn("Verified AUA Student", badge_active)
            self.assertNotIn("Expired", badge_active)

            # 2. Expired student (verified in past year)
            u3_db.academic_year_verified = curr_year - 1
            await session.commit()
            self.assertEqual(get_user_verification_status(u3_db), "student_expired")
            badge_expired = get_partner_verification_badge(u3_db)
            self.assertIn("Expired: Re-verification Needed", badge_expired)

            # 3. Running /verify shows expired notice with re-verify button
            state = AsyncMock()
            msg = AsyncMock(spec=Message)
            msg.from_user = TgUser(id=self.u3, is_bot=False, first_name="Tatev")
            msg.answer = AsyncMock()

            await verification_handlers.handle_verify_command(msg, state)
            call_text = msg.answer.call_args[0][0]
            self.assertIn("AUA Student Status: Expired", call_text)
            self.assertIn("September 1st", call_text)
            reply_markup = msg.answer.call_args[1]["reply_markup"]
            self.assertEqual(reply_markup.inline_keyboard[0][0].callback_data, "reverify_start")

            # 4. Trigger reverify_start callback
            cb = AsyncMock(spec=CallbackQuery)
            cb.from_user = TgUser(id=self.u3, is_bot=False, first_name="Tatev")
            cb.answer = AsyncMock()
            cb.message = AsyncMock()
            cb.message.answer = AsyncMock()

            await verification_handlers.handle_reverify_start(cb, state)
            state.set_state.assert_called_with(VerificationState.waiting_for_email)

            # 5. Enter email and complete re-verification
            msg.text = "tatev_hakobyan@edu.aua.am"
            await verification_handlers.handle_email_input(msg, state)
            await session.refresh(u3_db)
            code = u3_db.verification_code

            msg.text = code
            await verification_handlers.handle_code_input(msg, state)

            await session.refresh(u3_db)
            self.assertEqual(u3_db.academic_year_verified, curr_year)
            self.assertEqual(get_user_verification_status(u3_db), "student")
            badge_renewed = get_partner_verification_badge(u3_db)
            self.assertIn("Verified AUA Student", badge_renewed)
            self.assertNotIn("Expired", badge_renewed)


if __name__ == "__main__":
    unittest.main()
