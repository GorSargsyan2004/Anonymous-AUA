"""Unit and integration tests for student gender verification and conflict safeguards."""

from datetime import datetime, timezone
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.types import CallbackQuery, Message, User as TgUser
from sqlalchemy import delete, select

from app.config import settings
from app.database.crud import (
    admin_override_gender,
    check_and_verify_user_code,
    get_or_create_user,
    resolve_gender_mismatch,
    set_gender_appeal_status,
    set_user_verification_code,
    update_user_gender_verification,
    update_user_profile,
)
from app.database.models import ChatSession, Feedback, User
from app.database.session import engine, get_db_session
from app.handlers import matchmaking as mm
from app.handlers import verification as verification_handlers
from app.services.gender_detector import (
    detect_gender,
    extract_firstname_from_email,
    infer_gender_from_email,
)
from app.states.states import VerificationState


class TestGenderVerification(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.u1 = 771101
        self.u2 = 771102
        self.u3 = 771103
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
            await session.execute(delete(User).where(User.id.in_(self.all_users)))
            await session.commit()

        self.bot = AsyncMock()

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
            await session.execute(delete(User).where(User.id.in_(self.all_users)))
            await session.commit()
        await engine.dispose()

    def test_01_gender_detector_classification(self):
        """Verify robust name parsing and gender classification for Armenian names."""
        # Male names
        self.assertEqual(detect_gender("gor"), "male")
        self.assertEqual(detect_gender("davit"), "male")
        self.assertEqual(detect_gender("david"), "male")
        self.assertEqual(detect_gender("hayk"), "male")
        self.assertEqual(detect_gender("vazgen"), "male")
        self.assertEqual(detect_gender("tigran"), "male")
        self.assertEqual(detect_gender("narek"), "male")

        # Female names
        self.assertEqual(detect_gender("ani"), "female")
        self.assertEqual(detect_gender("mane"), "female")
        self.assertEqual(detect_gender("milena"), "female")
        self.assertEqual(detect_gender("lilit"), "female")
        self.assertEqual(detect_gender("tatevik"), "female")
        self.assertEqual(detect_gender("lusine"), "female")
        self.assertEqual(detect_gender("taguhi"), "female")

        # Email inference
        name, gender = infer_gender_from_email("gor_sargsyan@edu.aua.am")
        self.assertEqual(name, "gor")
        self.assertEqual(gender, "male")

        name, gender = infer_gender_from_email("ani.petrosyan1@edu.aua.am")
        self.assertEqual(name, "ani")
        self.assertEqual(gender, "female")

        # Ambiguous / Unknown
        self.assertEqual(detect_gender("sasha"), "unknown")
        self.assertEqual(detect_gender("nonexistent_unknown_name"), "unknown")

    async def test_02_verification_flow_matching_gender(self):
        """If inferred gender matches profile sex, is_gender_verified becomes True."""
        async with get_db_session() as session:
            await get_or_create_user(session, user_id=self.u1)
            await update_user_profile(session, user_id=self.u1, sex="male", orientation="straight")

        msg = AsyncMock(spec=Message)
        msg.from_user = TgUser(id=self.u1, is_bot=False, first_name="Gor")
        msg.text = "123456"
        msg.answer = AsyncMock()

        state = AsyncMock()

        with patch("app.handlers.verification.check_and_verify_user_code", return_value=(True, "Success")):
            async with get_db_session() as session:
                u = await get_or_create_user(session, user_id=self.u1)
                u.email = "gor_sargsyan@edu.aua.am"
                await session.commit()

            await verification_handlers.handle_code_input(msg, state)

        async with get_db_session() as session:
            u = await get_or_create_user(session, user_id=self.u1)
            self.assertTrue(u.is_gender_verified)
            self.assertFalse(u.gender_mismatch)
            self.assertEqual(u.inferred_gender, "male")

        msg.answer.assert_called_once()
        ans = msg.answer.call_args[0][0]
        self.assertIn("Gender Verified", ans)
        self.assertIn("Male", ans)

    async def test_03_verification_flow_gender_mismatch(self):
        """If a male email is entered by a profile set to female, conflict is flagged."""
        async with get_db_session() as session:
            await get_or_create_user(session, user_id=self.u2)
            await update_user_profile(session, user_id=self.u2, sex="female", orientation="straight")

        msg = AsyncMock(spec=Message)
        msg.from_user = TgUser(id=self.u2, is_bot=False, first_name="FakeGirl")
        msg.text = "654321"
        msg.answer = AsyncMock()

        state = AsyncMock()

        with patch("app.handlers.verification.check_and_verify_user_code", return_value=(True, "Success")):
            async with get_db_session() as session:
                u = await get_or_create_user(session, user_id=self.u2)
                u.email = "davit_minasyan@edu.aua.am"
                await session.commit()

            await verification_handlers.handle_code_input(msg, state)

        async with get_db_session() as session:
            u = await get_or_create_user(session, user_id=self.u2)
            self.assertFalse(u.is_gender_verified)
            self.assertTrue(u.gender_mismatch)
            self.assertEqual(u.inferred_gender, "male")

        msg.answer.assert_called_once()
        ans = msg.answer.call_args[0][0]
        self.assertIn("Gender Conflict Detected", ans)
        self.assertIn("Male", ans)

    async def test_04_matchmaking_blocked_on_gender_mismatch(self):
        """User with gender_mismatch is blocked from entering search queue."""
        async with get_db_session() as session:
            await get_or_create_user(session, user_id=self.u2)
            await update_user_profile(session, user_id=self.u2, sex="female", orientation="straight")
            await update_user_gender_verification(
                session,
                user_id=self.u2,
                inferred_gender="male",
                is_gender_verified=False,
                gender_mismatch=True,
            )

        msg = AsyncMock(spec=Message)
        msg.from_user = TgUser(id=self.u2, is_bot=False, first_name="DavitCatfish")
        msg.answer = AsyncMock()

        await mm.perform_search(user_id=self.u2, bot=self.bot, message=msg)

        async with get_db_session() as session:
            u = await get_or_create_user(session, user_id=self.u2)
            self.assertFalse(u.is_searching)

        msg.answer.assert_called_once()
        ans = msg.answer.call_args[0][0]
        self.assertIn("Matchmaking Paused: Gender Verification Conflict", ans)
        self.assertIn("Male", ans)

    async def test_05_resolve_gender_switch_callback(self):
        """Student tapping 'Switch Profile to Male' resolves mismatch immediately."""
        async with get_db_session() as session:
            await get_or_create_user(session, user_id=self.u2)
            await update_user_profile(session, user_id=self.u2, sex="female", orientation="straight")
            await update_user_gender_verification(
                session,
                user_id=self.u2,
                inferred_gender="male",
                is_gender_verified=False,
                gender_mismatch=True,
            )

        cb = AsyncMock(spec=CallbackQuery)
        cb.from_user = TgUser(id=self.u2, is_bot=False, first_name="Davit")
        cb.data = "resolve_gender_switch:male"
        cb.answer = AsyncMock()
        cb.message = AsyncMock()
        cb.message.edit_text = AsyncMock()

        await verification_handlers.handle_resolve_gender_switch(cb)

        async with get_db_session() as session:
            u = await get_or_create_user(session, user_id=self.u2)
            self.assertEqual(u.sex, "male")
            self.assertTrue(u.is_gender_verified)
            self.assertFalse(u.gender_mismatch)

        cb.answer.assert_called_once()
        cb.message.edit_text.assert_called_once()
        txt = cb.message.edit_text.call_args[0][0]
        self.assertIn("Gender Conflict Resolved", txt)

    async def test_06_gender_appeal_and_admin_approval(self):
        """Student appeals gender conflict and admin approves it."""
        async with get_db_session() as session:
            await get_or_create_user(session, user_id=self.u3)
            await update_user_profile(session, user_id=self.u3, sex="female", orientation="straight")
            await update_user_gender_verification(
                session,
                user_id=self.u3,
                inferred_gender="male",
                is_gender_verified=False,
                gender_mismatch=True,
            )

        cb = AsyncMock(spec=CallbackQuery)
        cb.from_user = TgUser(id=self.u3, is_bot=False, first_name="AppealUser")
        cb.data = "gender_appeal_start"
        cb.answer = AsyncMock()
        cb.message = AsyncMock()
        cb.message.edit_text = AsyncMock()

        admin_id = 999999
        with patch.object(settings, "ADMIN_IDS", [admin_id]):
            await verification_handlers.handle_gender_appeal_start(cb, self.bot)

            self.bot.send_message.assert_called_once()
            admin_msg = self.bot.send_message.call_args[1]["text"]
            self.assertIn("GENDER CONFLICT APPEAL", admin_msg)

            async with get_db_session() as session:
                u = await get_or_create_user(session, user_id=self.u3)
                self.assertTrue(u.gender_appeal_pending)

            # Admin approves female
            admin_cb = AsyncMock(spec=CallbackQuery)
            admin_cb.from_user = TgUser(id=admin_id, is_bot=False, first_name="Admin")
            admin_cb.data = f"admin_gender_appr:{self.u3}:female"
            admin_cb.answer = AsyncMock()
            admin_cb.message = AsyncMock()
            admin_cb.message.edit_text = AsyncMock()

            await verification_handlers.handle_admin_gender_approve(admin_cb, self.bot)

            async with get_db_session() as session:
                u = await get_or_create_user(session, user_id=self.u3)
                self.assertEqual(u.sex, "female")
                self.assertTrue(u.is_gender_verified)
                self.assertFalse(u.gender_mismatch)
                self.assertFalse(u.gender_appeal_pending)

    async def test_07_partner_connection_badge_with_verified_gender(self):
        """When partner is gender-verified, connection banner renders 'Verified AUA Student (Male/Female ✅)'."""
        async with get_db_session() as session:
            p1 = await get_or_create_user(session, user_id=self.u1)
            p1.sex = "male"
            p1.orientation = "straight"
            p1.is_verified = True
            p1.is_gender_verified = True

            p2 = await get_or_create_user(session, user_id=self.u2)
            p2.sex = "female"
            p2.orientation = "straight"
            p2.is_verified = True
            p2.is_gender_verified = True
            await session.commit()

        msg = AsyncMock(spec=Message)
        msg.from_user = TgUser(id=self.u1, is_bot=False, first_name="Gor")
        msg.answer = AsyncMock()

        mock_session = MagicMock()
        mock_session.id = 12345

        with patch("app.handlers.matchmaking.match_user_in_queue", return_value=(p2, mock_session)):
            await mm.perform_search(user_id=self.u1, bot=self.bot, message=msg)

        msg.answer.assert_called_once()
        user_banner = msg.answer.call_args[0][0]
        self.assertIn("Verified AUA Student (Female ✅)", user_banner)

        self.bot.send_message.assert_called_once()
        partner_banner = self.bot.send_message.call_args[1]["text"]
        self.assertIn("Verified AUA Student (Male ✅)", partner_banner)


if __name__ == "__main__":
    unittest.main()
