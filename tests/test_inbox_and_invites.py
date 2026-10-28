"""Unit and integration tests for Anonymous Inbox (/link) and Chat Invites (/invite)."""

import asyncio
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.filters import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, User as TgUser
from sqlalchemy import delete, select

from app.database.crud import (
    block_inbox_sender,
    consume_chat_invite,
    create_chat_invite,
    create_or_regenerate_inbox,
    deactivate_chat_invite,
    deactivate_user_inbox,
    get_chat_invite_by_code,
    get_inbox_by_token,
    get_inbox_cooldown_remaining,
    get_inbox_message_by_thread,
    get_or_create_user,
    get_user_active_inbox,
    get_user_active_invites,
    is_inbox_sender_blocked,
    record_inbox_message,
)
from app.database.models import (
    AnonymousInbox,
    ChatInvite,
    ChatSession,
    InboxBlock,
    InboxMessage,
    User,
)
from app.database.session import engine, get_db_session
from app.handlers.inbox import handle_inbox_deeplink
from app.handlers.invites import handle_invite_deeplink
from app.keyboards.inline import (
    get_inbox_block_confirm_keyboard,
    get_inbox_message_keyboard,
    get_inbox_owner_keyboard,
    get_invite_item_keyboard,
    get_invite_menu_keyboard,
)


class TestInboxAndInvites(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.owner_id = 880011
        self.sender_id = 880022
        self.guest_id = 880033

        async with get_db_session() as session:
            test_ids = [self.owner_id, self.sender_id, self.guest_id]
            from sqlalchemy import update
            await session.execute(update(User).where(User.id.in_(test_ids)).values(current_partner_id=None))
            await session.commit()
            await get_or_create_user(session, user_id=self.owner_id, username="inbox_owner")
            await get_or_create_user(session, user_id=self.sender_id, username="inbox_sender")
            await get_or_create_user(session, user_id=self.guest_id, username="invite_guest")

    async def asyncTearDown(self):
        async with get_db_session() as session:
            test_ids = [self.owner_id, self.sender_id, self.guest_id]
            from sqlalchemy import update
            await session.execute(update(User).where(User.id.in_(test_ids)).values(current_partner_id=None))
            await session.execute(delete(ChatSession).where(ChatSession.user1_id.in_(test_ids) | ChatSession.user2_id.in_(test_ids)))
            await session.execute(delete(InboxMessage).where(InboxMessage.owner_id.in_(test_ids) | InboxMessage.sender_id.in_(test_ids)))
            await session.execute(delete(InboxBlock).where(InboxBlock.owner_id.in_(test_ids) | InboxBlock.blocked_sender_id.in_(test_ids)))
            await session.execute(delete(AnonymousInbox).where(AnonymousInbox.user_id.in_(test_ids)))
            await session.execute(delete(ChatInvite).where(ChatInvite.creator_id.in_(test_ids)))
            await session.execute(delete(User).where(User.id.in_(test_ids)))
            await session.commit()
        await engine.dispose()

    async def test_inbox_creation_destruction_and_regeneration(self):
        """Test creating an inbox link, destroying it, and generating a new one."""
        async with get_db_session() as session:
            # 1. Generate inbox link
            inbox1 = await create_or_regenerate_inbox(session, user_id=self.owner_id)
            self.assertTrue(inbox1.is_active)
            self.assertIsNotNone(inbox1.token)

            # Query active inbox
            active = await get_user_active_inbox(session, user_id=self.owner_id)
            self.assertIsNotNone(active)
            self.assertEqual(active.token, inbox1.token)

            # 2. Destroy / deactivate inbox link
            destroyed = await deactivate_user_inbox(session, user_id=self.owner_id)
            self.assertTrue(destroyed)

            # Check that active inbox is now None
            active_after = await get_user_active_inbox(session, user_id=self.owner_id)
            self.assertIsNone(active_after)

            # Check looking up old token: is_active is False
            old_inbox = await get_inbox_by_token(session, token=inbox1.token)
            self.assertIsNotNone(old_inbox)
            self.assertFalse(old_inbox.is_active)

            # 3. Regenerate a new inbox link
            inbox2 = await create_or_regenerate_inbox(session, user_id=self.owner_id)
            self.assertTrue(inbox2.is_active)
            self.assertNotEqual(inbox2.token, inbox1.token)

    async def test_inbox_cooldown_and_blocking(self):
        """Test 20-min cooldown enforcement and sender blocking mechanics."""
        async with get_db_session() as session:
            # Initially no cooldown
            rem0 = await get_inbox_cooldown_remaining(session, self.owner_id, self.sender_id, cooldown_seconds=1200)
            self.assertEqual(rem0, 0)

            # Record message
            msg = await record_inbox_message(session, self.owner_id, self.sender_id, message_type="text")
            self.assertIsNotNone(msg.thread_token)

            # Now cooldown should be active (~1200s)
            rem1 = await get_inbox_cooldown_remaining(session, self.owner_id, self.sender_id, cooldown_seconds=1200)
            self.assertGreater(rem1, 1100)
            self.assertLessEqual(rem1, 1200)

            # Check blocking
            self.assertFalse(await is_inbox_sender_blocked(session, self.owner_id, self.sender_id))
            blocked = await block_inbox_sender(session, self.owner_id, self.sender_id)
            self.assertTrue(blocked)
            self.assertTrue(await is_inbox_sender_blocked(session, self.owner_id, self.sender_id))

    async def test_inbox_deeplink_self_and_deactivated(self):
        """Test deep-link behavior when opening own link or deactivated link."""
        bot_mock = AsyncMock()
        state_mock = AsyncMock(spec=FSMContext)

        # 1. Deactivated link
        async with get_db_session() as session:
            inbox = await create_or_regenerate_inbox(session, user_id=self.owner_id)
            await deactivate_user_inbox(session, user_id=self.owner_id)

        msg_mock = MagicMock(spec=Message)
        msg_mock.from_user = MagicMock(id=self.sender_id, username="sender")
        msg_mock.answer = AsyncMock()
        await handle_inbox_deeplink(msg_mock, token=inbox.token, state=state_mock, bot=bot_mock)
        msg_mock.answer.assert_called()
        self.assertIn("Link Expired or Deactivated", msg_mock.answer.call_args[0][0])

        # 2. Self-message attempt
        async with get_db_session() as session:
            inbox_new = await create_or_regenerate_inbox(session, user_id=self.owner_id)

        msg_self = MagicMock(spec=Message)
        msg_self.from_user = MagicMock(id=self.owner_id, username="owner")
        msg_self.answer = AsyncMock()
        await handle_inbox_deeplink(msg_self, token=inbox_new.token, state=state_mock, bot=bot_mock)
        msg_self.answer.assert_called()
        self.assertIn("This is your own anonymous link", msg_self.answer.call_args[0][0])

    async def test_chat_invites_creation_destruction_and_pairing(self):
        """Test creating one-time/persistent chat invites, revoking, and instant pairing."""
        async with get_db_session() as session:
            # 1. Create One-Time invite
            inv1 = await create_chat_invite(session, creator_id=self.owner_id, invite_type="one_time")
            self.assertEqual(inv1.invite_type, "one_time")
            self.assertTrue(inv1.is_active)
            self.assertFalse(inv1.is_used)

            # 2. Create Persistent invite
            inv2 = await create_chat_invite(session, creator_id=self.owner_id, invite_type="persistent")
            self.assertEqual(inv2.invite_type, "persistent")

            # Check active invites count
            active_list = await get_user_active_invites(session, creator_id=self.owner_id)
            self.assertEqual(len(active_list), 2)

            # 3. Revoke/destroy persistent invite
            destroyed = await deactivate_chat_invite(session, creator_id=self.owner_id, invite_code=inv2.invite_code)
            self.assertTrue(destroyed)

            active_list2 = await get_user_active_invites(session, creator_id=self.owner_id)
            self.assertEqual(len(active_list2), 1)
            self.assertEqual(active_list2[0].invite_code, inv1.invite_code)

        # 4. Guest connects via one-time invite
        bot_mock = AsyncMock()
        state_mock = AsyncMock(spec=FSMContext)
        msg_guest = MagicMock(spec=Message)
        msg_guest.from_user = MagicMock(id=self.guest_id, username="guest")
        msg_guest.answer = AsyncMock()

        await handle_invite_deeplink(msg_guest, invite_code=inv1.invite_code, state=state_mock, bot=bot_mock)
        msg_guest.answer.assert_called()
        self.assertIn("Connected via Invitation Link", msg_guest.answer.call_args[0][0])
        bot_mock.send_message.assert_called()
        self.assertEqual(bot_mock.send_message.call_args[1]["chat_id"], self.owner_id)

        # Verify database state after connection
        async with get_db_session() as session:
            used_inv = await get_chat_invite_by_code(session, inv1.invite_code)
            self.assertTrue(used_inv.is_used)
            self.assertEqual(used_inv.used_by_id, self.guest_id)

            u_owner = (await session.execute(select(User).where(User.id == self.owner_id))).scalar_one()
            u_guest = (await session.execute(select(User).where(User.id == self.guest_id))).scalar_one()
            self.assertEqual(u_owner.current_partner_id, self.guest_id)
            self.assertEqual(u_guest.current_partner_id, self.owner_id)

    def test_keyboards(self):
        """Verify inline keyboard builders return valid markup."""
        kb_active = get_inbox_owner_keyboard(has_active_link=True, token="tok123")
        self.assertEqual(len(kb_active.inline_keyboard), 2)

        kb_inactive = get_inbox_owner_keyboard(has_active_link=False)
        self.assertEqual(len(kb_inactive.inline_keyboard), 1)

        kb_msg = get_inbox_message_keyboard("thread_abc")
        self.assertGreaterEqual(len(kb_msg.inline_keyboard), 2)

        kb_invite_menu = get_invite_menu_keyboard(has_active=True)
        self.assertEqual(len(kb_invite_menu.inline_keyboard), 3)

        kb_invite_item = get_invite_item_keyboard("code123")
        self.assertEqual(len(kb_invite_item.inline_keyboard), 2)


if __name__ == "__main__":
    unittest.main()
