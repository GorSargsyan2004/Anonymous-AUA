"""Unit and integration tests for Privacy Policy and /delete_me functionality."""

import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from app.database.crud import (
    create_chat_invite,
    create_or_regenerate_inbox,
    delete_user_account,
    get_or_create_user,
    get_user,
)
from app.database.models import AnonymousInbox, ChatInvite, ChatSession, User
from app.database.session import engine, get_db_session
from app.handlers import start as start_handlers
from app.keyboards.inline import (
    get_delete_me_confirmation_keyboard,
    get_privacy_back_keyboard,
    get_privacy_menu_keyboard,
)


class TestPrivacyPolicy(unittest.IsolatedAsyncioTestCase):
    """Test Privacy Policy document, bot commands, and interactive callbacks."""

    def test_privacy_markdown_file_exists_and_complete(self):
        """Verify PRIVACY.md exists in repository root and has all required sections."""
        privacy_path = os.path.join(os.path.dirname(__file__), "..", "PRIVACY.md")
        self.assertTrue(os.path.exists(privacy_path), "PRIVACY.md must exist in root")

        with open(privacy_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Check required sections
        self.assertIn("Privacy Policy & Community Code of Conduct", content)
        self.assertIn("1. How Your Anonymity & Data Are Protected", content)
        self.assertIn("1.1 Data Stored in Our Database (PostgreSQL)", content)
        self.assertIn("1.2 Zero-Knowledge Ephemeral Chat Buffer", content)
        self.assertIn("2. Community Code of Conduct & Mutual AI Judging Rules", content)
        self.assertIn("Rule A: Privacy Violations & Anti-Doxxing", content)
        self.assertIn("Rule B: Harassment, NSFW, and Abuse", content)
        self.assertIn("Rule C: Reporter Accountability", content)
        self.assertIn("Rule D: Illegal Content & Emergency Identity Unmasking", content)
        self.assertIn("3. Data Deletion & User Rights", content)
        self.assertIn("/delete_me", content)

    async def test_privacy_command_and_interactive_navigation(self):
        """Verify /privacy command and navigation callback views."""
        msg = AsyncMock()
        await start_handlers.handle_privacy_command(msg)

        msg.answer.assert_awaited_once()
        text = msg.answer.call_args[0][0]
        reply_markup = msg.answer.call_args[1]["reply_markup"]

        self.assertIn("Privacy Policy & Community Code of Conduct", text)
        self.assertIn("Zero Permanent Logs", text)
        self.assertEqual(len(reply_markup.inline_keyboard), 4)

        # 1. Navigation callback: privacy_view:data
        cb_data = MagicMock(spec=CallbackQuery)
        cb_data.data = "privacy_view:data"
        cb_data.message = AsyncMock()
        cb_data.answer = AsyncMock()

        await start_handlers.handle_privacy_view_callback(cb_data)
        cb_data.message.edit_text.assert_awaited_once()
        data_text = cb_data.message.edit_text.call_args[0][0]
        self.assertIn("How Your Anonymity & Data Are Protected", data_text)
        self.assertIn("PostgreSQL", data_text)

        # 2. Navigation callback: privacy_view:rules
        cb_rules = MagicMock(spec=CallbackQuery)
        cb_rules.data = "privacy_view:rules"
        cb_rules.message = AsyncMock()
        cb_rules.answer = AsyncMock()

        await start_handlers.handle_privacy_view_callback(cb_rules)
        cb_rules.message.edit_text.assert_awaited_once()
        rules_text = cb_rules.message.edit_text.call_args[0][0]
        self.assertIn("Code of Conduct & Mutual AI Judging Rules", rules_text)
        self.assertIn("Rule A: Privacy Violations & Anti-Doxxing", rules_text)

        # 3. Navigation callback: privacy_view:full
        cb_full = MagicMock(spec=CallbackQuery)
        cb_full.data = "privacy_view:full"
        cb_full.message = AsyncMock()
        cb_full.answer = AsyncMock()

        await start_handlers.handle_privacy_view_callback(cb_full)
        cb_full.message.edit_text.assert_awaited_once()
        cb_full.message.answer.assert_awaited_once()

        # 4. Navigation callback: privacy_view:menu
        cb_menu = MagicMock(spec=CallbackQuery)
        cb_menu.data = "privacy_view:menu"
        cb_menu.message = AsyncMock()
        cb_menu.answer = AsyncMock()

        await start_handlers.handle_privacy_view_callback(cb_menu)
        cb_menu.message.edit_text.assert_awaited_once()
        menu_text = cb_menu.message.edit_text.call_args[0][0]
        self.assertIn("Privacy Policy & Community Code of Conduct", menu_text)

    async def test_help_command_includes_privacy_and_delete_me(self):
        """Verify /help includes references to /privacy and /delete_me."""
        msg = AsyncMock()
        await start_handlers.handle_help_command(msg)

        text = msg.answer.call_args[0][0]
        self.assertIn("/privacy", text)
        self.assertIn("/delete_me", text)


class TestDeleteMe(unittest.IsolatedAsyncioTestCase):
    """Test /delete_me command workflow and database account deletion."""

    async def asyncSetUp(self):
        self.test_user_id = 99112233
        self.partner_user_id = 99112244

        async with get_db_session() as session:
            await get_or_create_user(session, user_id=self.test_user_id, username="delete_test_user")
            await get_or_create_user(session, user_id=self.partner_user_id, username="delete_test_partner")

    async def asyncTearDown(self):
        async with get_db_session() as session:
            from sqlalchemy import delete
            test_ids = [self.test_user_id, self.partner_user_id]
            await session.execute(delete(ChatSession).where(ChatSession.user1_id.in_(test_ids) | ChatSession.user2_id.in_(test_ids)))
            await session.execute(delete(AnonymousInbox).where(AnonymousInbox.user_id.in_(test_ids)))
            await session.execute(delete(ChatInvite).where(ChatInvite.creator_id.in_(test_ids)))
            await session.execute(delete(User).where(User.id.in_(test_ids)))
            await session.commit()
        await engine.dispose()

    async def test_delete_me_command_prompts_confirmation(self):
        """Verify /delete_me displays warning and confirmation markup."""
        msg = AsyncMock()
        await start_handlers.handle_delete_me_command(msg)

        msg.answer.assert_awaited_once()
        text = msg.answer.call_args[0][0]
        self.assertIn("Delete Account & All Data?", text)
        self.assertIn("irreversible", text)

    async def test_delete_me_cancel_callback(self):
        """Verify canceling deletion keeps account intact."""
        cb = MagicMock(spec=CallbackQuery)
        cb.from_user = MagicMock(id=self.test_user_id)
        cb.message = AsyncMock()
        cb.answer = AsyncMock()

        await start_handlers.handle_delete_me_cancel_callback(cb)
        cb.message.edit_text.assert_awaited_once()
        text = cb.message.edit_text.call_args[0][0]
        self.assertIn("cancelled", text)

        # Verify user still exists
        async with get_db_session() as session:
            u = await get_user(session, self.test_user_id)
            self.assertIsNotNone(u)

    async def test_delete_me_confirm_deletes_account_and_cleans_session(self):
        """Verify confirming deletion completely removes user and frees partner."""
        bot_mock = AsyncMock()
        state_mock = AsyncMock(spec=FSMContext)

        async with get_db_session() as session:
            # Setup active chat session between test_user and partner
            chat_sess = ChatSession(
                user1_id=self.test_user_id,
                user2_id=self.partner_user_id,
                is_active=True,
            )
            session.add(chat_sess)
            u1 = await session.get(User, self.test_user_id)
            u2 = await session.get(User, self.partner_user_id)
            u1.current_partner_id = self.partner_user_id
            u2.current_partner_id = self.test_user_id
            await session.commit()

            # Create inbox and invite for test_user
            await create_or_regenerate_inbox(session, user_id=self.test_user_id)
            await create_chat_invite(session, creator_id=self.test_user_id)

        cb = MagicMock(spec=CallbackQuery)
        cb.from_user = MagicMock(id=self.test_user_id)
        cb.message = AsyncMock()
        cb.answer = AsyncMock()

        await start_handlers.handle_delete_me_confirm_callback(cb, bot=bot_mock, state=state_mock)

        cb.message.edit_text.assert_awaited_once()
        self.assertIn("permanently deleted", cb.message.edit_text.call_args[0][0])

        # Verify partner was notified
        bot_mock.send_message.assert_awaited_once()
        self.assertEqual(bot_mock.send_message.call_args[1]["chat_id"], self.partner_user_id)

        # Verify database state: test_user gone, partner freed
        async with get_db_session() as session:
            u1_after = await get_user(session, self.test_user_id)
            self.assertIsNone(u1_after)

            u2_after = await get_user(session, self.partner_user_id)
            self.assertIsNotNone(u2_after)
            self.assertIsNone(u2_after.current_partner_id)


if __name__ == "__main__":
    unittest.main()

