"""Unit and integration tests for email flood protection, anti-bounce shields, and rate limiting."""

from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import AsyncMock, patch

from aiogram.types import Message, User as TgUser
from sqlalchemy import delete

from app.config import settings
from app.database.crud import (
    can_request_verification_email,
    get_or_create_user,
    record_verification_email_sent,
)
from app.database.models import User
from app.database.session import engine, get_db_session, init_db
from app.handlers import verification as verification_handlers
from app.services.email_verification import (
    check_global_email_rate_limit,
    record_global_email_sent,
    reset_global_email_rate_limit,
    validate_aua_email_format,
)
from app.states.states import VerificationState


class TestEmailFloodShield(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        reset_global_email_rate_limit()
        self.u1 = 882201
        self.u2 = 882202
        self.all_users = [self.u1, self.u2]

        async with get_db_session() as session:
            await session.execute(delete(User).where(User.id.in_(self.all_users)))
            await session.commit()

    async def asyncTearDown(self):
        async with get_db_session() as session:
            await session.execute(delete(User).where(User.id.in_(self.all_users)))
            await session.commit()
        await engine.dispose()
        reset_global_email_rate_limit()

    def test_01_email_syntax_and_blacklist_validation(self):
        """Validate student email syntax and ensure dummy/bounce addresses are rejected."""
        # Valid student emails
        valid_samples = [
            "gor_sargsyan@edu.aua.am",
            "ani.petrosyan@edu.aua.am",
            "davit_minasyan1@edu.aua.am",
            "vladimir_harutyunyan@edu.aua.am",
            "hayk-ustabashyan@edu.aua.am",
        ]
        for email in valid_samples:
            ok, err = validate_aua_email_format(email)
            self.assertTrue(ok, f"Expected {email} to be valid, got error: {err}")

        # Invalid domains / syntax
        invalid_domains = [
            "gor@gmail.com",
            "gor_sargsyan@aua.am",
            "not_an_email",
            "@edu.aua.am",
            "",
            None,
        ]
        for email in invalid_domains:
            ok, err = validate_aua_email_format(email)
            self.assertFalse(ok, f"Expected {email} to be invalid")

        # Single word emails (must be firstname_lastname)
        single_words = [
            "gor@edu.aua.am",
            "alex@edu.aua.am",
            "john@edu.aua.am",
        ]
        for email in single_words:
            ok, err = validate_aua_email_format(email)
            self.assertFalse(ok, f"Expected single-word email {email} to be rejected")
            self.assertIn("first name and last name", err)

        # Blocked bounce-traps and dummy test terms
        dummy_samples = [
            "student_test@edu.aua.am",
            "test_student@edu.aua.am",
            "fake_user@edu.aua.am",
            "dummy_account@edu.aua.am",
            "admin_test@edu.aua.am",
            "asdf_qwerty@edu.aua.am",
            "test_gor@edu.aua.am",
            "sample_user@edu.aua.am",
        ]
        for email in dummy_samples:
            ok, err = validate_aua_email_format(email)
            self.assertFalse(ok, f"Expected dummy email {email} to be rejected")
            self.assertIn("Generic or test email", err)

    async def test_02_per_user_cooldown_enforcement(self):
        """Ensure a user cannot request multiple OTP emails within the 3-minute cooldown."""
        async with get_db_session() as session:
            await get_or_create_user(session, user_id=self.u1)

            # First attempt: allowed
            allowed, err = await can_request_verification_email(session, user_id=self.u1)
            self.assertTrue(allowed)
            self.assertIsNone(err)

            # Record email sent
            await record_verification_email_sent(session, user_id=self.u1)

            # Immediate second attempt: blocked by cooldown
            allowed, err = await can_request_verification_email(session, user_id=self.u1)
            self.assertFalse(allowed)
            self.assertIn("Please wait", err)

            # Fast forward 4 minutes
            u = await get_or_create_user(session, user_id=self.u1)
            u.verification_email_sent_at = datetime.now(timezone.utc) - timedelta(minutes=4)
            await session.commit()

            # Now allowed again
            allowed, err = await can_request_verification_email(session, user_id=self.u1)
            self.assertTrue(allowed)

    async def test_03_per_user_daily_limit_cap(self):
        """Ensure a user cannot exceed the maximum daily OTP email limit (5 per 24h)."""
        async with get_db_session() as session:
            u = await get_or_create_user(session, user_id=self.u1)
            u.verification_emails_sent_today = 5
            u.verification_emails_reset_at = datetime.now(timezone.utc)
            # Past cooldown so only daily limit triggers
            u.verification_email_sent_at = datetime.now(timezone.utc) - timedelta(minutes=10)
            await session.commit()

            allowed, err = await can_request_verification_email(session, user_id=self.u1)
            self.assertFalse(allowed)
            self.assertIn("maximum daily limit", err)

            # Fast forward 25 hours -> counter resets
            u.verification_emails_reset_at = datetime.now(timezone.utc) - timedelta(hours=25)
            await session.commit()

            allowed, err = await can_request_verification_email(session, user_id=self.u1)
            self.assertTrue(allowed)

    def test_04_global_rate_limiting(self):
        """Ensure the global sliding window throttles traffic if burst exceeds limit."""
        reset_global_email_rate_limit()

        with patch.object(settings, "VERIFICATION_GLOBAL_RATE_LIMIT", 3):
            # 3 sends allowed
            ok, _ = check_global_email_rate_limit()
            self.assertTrue(ok)
            record_global_email_sent()

            ok, _ = check_global_email_rate_limit()
            self.assertTrue(ok)
            record_global_email_sent()

            ok, _ = check_global_email_rate_limit()
            self.assertTrue(ok)
            record_global_email_sent()

            # 4th send within 5 minutes is blocked
            ok, err = check_global_email_rate_limit()
            self.assertFalse(ok)
            self.assertIn("Verification email traffic is temporarily high", err)

    async def test_05_handler_rejects_dummy_email_and_enforces_cooldown(self):
        """Integration test: handler rejects dummy email, sends valid one, and blocks immediate retry."""
        state = AsyncMock()

        # 1. Try sending dummy email
        dummy_msg = AsyncMock(spec=Message)
        dummy_msg.from_user = TgUser(id=self.u2, is_bot=False, first_name="Tester")
        dummy_msg.text = "student_test@edu.aua.am"
        dummy_msg.answer = AsyncMock()

        await verification_handlers.handle_email_input(dummy_msg, state)

        dummy_msg.answer.assert_called_once()
        resp = dummy_msg.answer.call_args[0][0]
        self.assertIn("Generic or test email addresses are not permitted", resp)

        # 2. Try sending legitimate email
        valid_msg = AsyncMock(spec=Message)
        valid_msg.from_user = TgUser(id=self.u2, is_bot=False, first_name="Tester")
        valid_msg.text = "armine_grigoryan99@edu.aua.am"
        valid_msg.answer = AsyncMock()

        with patch("app.handlers.verification.send_verification_email", return_value=(True, None)):
            await verification_handlers.handle_email_input(valid_msg, state)

        valid_msg.answer.assert_called_once()
        resp2 = valid_msg.answer.call_args[0][0]
        self.assertIn("Verification Code Sent", resp2)

        # 3. Immediate retry should be blocked by cooldown
        retry_msg = AsyncMock(spec=Message)
        retry_msg.from_user = TgUser(id=self.u2, is_bot=False, first_name="Tester")
        retry_msg.text = "ani_petrosyan@edu.aua.am"
        retry_msg.answer = AsyncMock()

        await verification_handlers.handle_email_input(retry_msg, state)

        retry_msg.answer.assert_called_once()
        resp3 = retry_msg.answer.call_args[0][0]
        self.assertIn("Verification Limit Active", resp3)
        self.assertIn("Please wait", resp3)


if __name__ == "__main__":
    unittest.main()
