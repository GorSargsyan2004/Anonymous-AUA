"""Unit tests for One-Time (View-Once) self-destructing media feature."""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.types import CallbackQuery, Message, PhotoSize, User

from app.handlers import matchmaking as mm
from app.services.one_time_media import (
    OTM_DURATION_SECONDS,
    OneTimeMediaManager,
    get_otm_prompt_keyboard,
    get_otm_viewing_keyboard,
    one_time_media_manager,
)


class TestOneTimeMediaService(unittest.TestCase):
    """Test OneTimeMediaManager methods and helper keyboards."""

    def setUp(self) -> None:
        self.mgr = OneTimeMediaManager()

    def test_user_mode_toggle(self) -> None:
        user_id = 9991
        self.assertFalse(self.mgr.is_user_mode_enabled(user_id))
        self.assertTrue(self.mgr.toggle_user_mode(user_id))
        self.assertTrue(self.mgr.is_user_mode_enabled(user_id))
        self.assertFalse(self.mgr.toggle_user_mode(user_id))
        self.assertFalse(self.mgr.is_user_mode_enabled(user_id))

    def test_detection_triggers(self) -> None:
        # Non-media returns False
        msg_text = MagicMock(spec=Message, photo=None, video=None, video_note=None, voice=None, animation=None, document=None)
        self.assertFalse(self.mgr.is_one_time_media(msg_text, 101))

        # Media without triggers returns False
        msg_photo = MagicMock(spec=Message, photo=[MagicMock()], video=None, video_note=None, voice=None, animation=None, document=None, has_media_spoiler=False, caption="Hello world")
        self.assertFalse(self.mgr.is_one_time_media(msg_photo, 101))

        # Trigger 1: #once in caption
        msg_tag1 = MagicMock(spec=Message, photo=[MagicMock()], video=None, video_note=None, voice=None, animation=None, document=None, has_media_spoiler=False, caption="Check this out #once")
        self.assertTrue(self.mgr.is_one_time_media(msg_tag1, 101))

        # Trigger 1b: /once in caption
        msg_tag2 = MagicMock(spec=Message, video=MagicMock(), photo=None, video_note=None, voice=None, animation=None, document=None, has_media_spoiler=False, caption="/once secret video")
        self.assertTrue(self.mgr.is_one_time_media(msg_tag2, 101))

        # Trigger 1c: /onetime or #onetime in caption
        msg_tag3 = MagicMock(spec=Message, photo=[MagicMock()], video=None, video_note=None, voice=None, animation=None, document=None, has_media_spoiler=False, caption="/onetime photo")
        self.assertTrue(self.mgr.is_one_time_media(msg_tag3, 101))
        msg_tag4 = MagicMock(spec=Message, photo=[MagicMock()], video=None, video_note=None, voice=None, animation=None, document=None, has_media_spoiler=False, caption="Check #onetime")
        self.assertTrue(self.mgr.is_one_time_media(msg_tag4, 101))

        # Trigger 2: native Telegram media spoiler
        msg_spoiler = MagicMock(spec=Message, photo=[MagicMock()], video=None, video_note=None, voice=None, animation=None, document=None, has_media_spoiler=True, caption=None)
        self.assertTrue(self.mgr.is_one_time_media(msg_spoiler, 101))

        # Trigger 3: user mode enabled
        self.mgr.toggle_user_mode(102)
        msg_plain_media = MagicMock(spec=Message, voice=MagicMock(), photo=None, video=None, video_note=None, animation=None, document=None, has_media_spoiler=False, caption=None)
        self.assertTrue(self.mgr.is_one_time_media(msg_plain_media, 102))

    def test_clean_caption(self) -> None:
        self.assertIsNone(self.mgr.clean_caption(None))
        self.assertIsNone(self.mgr.clean_caption("#once"))
        self.assertIsNone(self.mgr.clean_caption("/ONCE"))
        self.assertIsNone(self.mgr.clean_caption("/onetime"))
        self.assertIsNone(self.mgr.clean_caption("#onetime"))
        self.assertEqual(self.mgr.clean_caption("Check this #once please"), "Check this  please")
        self.assertEqual(self.mgr.clean_caption("Look here /once!"), "Look here !")
        self.assertEqual(self.mgr.clean_caption("Secret /onetime here"), "Secret  here")

    def test_token_lifecycle(self) -> None:
        msg = MagicMock(spec=Message)
        token = self.mgr.register(
            sender_id=1,
            receiver_id=2,
            session_id=10,
            message=msg,
            caption="secret",
        )
        self.assertIsNotNone(token)
        data = self.mgr.get(token)
        self.assertIsNotNone(data)
        self.assertEqual(data["sender_id"], 1)
        self.assertEqual(data["receiver_id"], 2)
        self.assertFalse(data["viewed"])

        self.mgr.set_placeholder_msg_id(token, 555)
        self.assertEqual(self.mgr.get(token)["placeholder_msg_id"], 555)

        # Mark viewed
        viewed = self.mgr.mark_viewed(token)
        self.assertTrue(viewed["viewed"])
        # Second call returns None
        self.assertIsNone(self.mgr.mark_viewed(token))

        # Clear session
        self.mgr.clear_session(10)
        self.assertIsNone(self.mgr.get(token))

    def test_keyboards(self) -> None:
        kb_prompt = get_otm_prompt_keyboard("test_token")
        self.assertEqual(kb_prompt.inline_keyboard[0][0].callback_data, "otm_rev:test_token")
        self.assertIn("15s", kb_prompt.inline_keyboard[0][0].text)

        kb_viewing = get_otm_viewing_keyboard()
        self.assertEqual(kb_viewing.inline_keyboard[0][0].callback_data, "otm_noop")
        self.assertIn("Self-destructing", kb_viewing.inline_keyboard[0][0].text)


class TestOneTimeMediaHandlers(unittest.IsolatedAsyncioTestCase):
    """Test matchmaking handlers with one-time media flows."""

    async def test_toggle_onetime_command(self) -> None:
        """Command /onetime toggles mode and answers confirmation."""
        msg = MagicMock(spec=Message)
        msg.from_user = MagicMock(spec=User, id=8881)
        msg.answer = AsyncMock()

        # Toggle ON
        await mm.handle_toggle_onetime_mode(msg)
        msg.answer.assert_called_once()
        self.assertIn("ON", msg.answer.call_args[0][0])
        self.assertTrue(one_time_media_manager.is_user_mode_enabled(8881))

        # Toggle OFF
        msg.answer.reset_mock()
        await mm.handle_toggle_onetime_mode(msg)
        msg.answer.assert_called_once()
        self.assertIn("OFF", msg.answer.call_args[0][0])
        self.assertFalse(one_time_media_manager.is_user_mode_enabled(8881))

    @patch("app.handlers.matchmaking.get_user")
    @patch("app.handlers.matchmaking.get_db_session")
    @patch("app.handlers.matchmaking._auto_destruct_one_time_media")
    async def test_reveal_callback_success(self, mock_auto_destruct, mock_get_db, mock_get_user) -> None:
        """Recipient clicking reveal triggers media delivery with protect_content=True."""
        bot = AsyncMock()
        bot.send_message = AsyncMock()

        orig_msg = MagicMock(spec=Message)
        orig_msg.copy_to = AsyncMock(return_value=MagicMock(message_id=901))

        token = one_time_media_manager.register(
            sender_id=7001,
            receiver_id=7002,
            session_id=123,
            message=orig_msg,
            caption="Test secret",
        )

        callback = MagicMock(spec=CallbackQuery)
        callback.data = f"otm_rev:{token}"
        callback.from_user = MagicMock(spec=User, id=7002)
        callback.message = MagicMock(spec=Message, message_id=801)
        callback.message.edit_text = AsyncMock()
        callback.answer = AsyncMock()

        # Mock DB user to confirm active chat
        db_user = MagicMock(id=7002, current_partner_id=7001)
        mock_get_user.return_value = db_user

        mock_sess_ctx = AsyncMock()
        mock_get_db.return_value.__aenter__.return_value = mock_sess_ctx

        await mm.handle_otm_reveal(callback, bot)

        # Verified placeholder was updated to viewing state
        callback.message.edit_text.assert_called_once()
        self.assertIn("Viewing", callback.message.edit_text.call_args[0][0])

        # Verified media copied with protect_content=True
        orig_msg.copy_to.assert_called_once_with(
            chat_id=7002,
            caption="Test secret",
            protect_content=True,
        )

        # Verified sender received notification
        bot.send_message.assert_called_once_with(
            chat_id=7001,
            text=(
                "👁️ <b>Your one-time media was opened!</b>\n\n"
                f"It is currently being viewed and will self-destruct in {OTM_DURATION_SECONDS} seconds."
            ),
            parse_mode="HTML",
        )

        # Verified auto-destruct task launched
        mock_auto_destruct.assert_called_once()

    @patch("app.handlers.matchmaking.get_user")
    @patch("app.handlers.matchmaking.get_db_session")
    async def test_reveal_callback_unauthorized_user(self, mock_get_db, mock_get_user) -> None:
        """Non-recipient clicking reveal is rejected."""
        bot = AsyncMock()
        token = one_time_media_manager.register(
            sender_id=6001,
            receiver_id=6002,
            session_id=124,
            message=MagicMock(),
        )

        callback = MagicMock(spec=CallbackQuery)
        callback.data = f"otm_rev:{token}"
        callback.from_user = MagicMock(spec=User, id=9999)  # Impostor
        callback.answer = AsyncMock()

        await mm.handle_otm_reveal(callback, bot)
        callback.answer.assert_called_once_with("⚠️ Only the designated recipient can view this media.", show_alert=True)
        one_time_media_manager.clear_token(token)

    async def test_auto_destruct_deletes_media_and_updates_placeholder(self) -> None:
        """_auto_destruct_one_time_media deletes media, edits placeholder, and alerts sender."""
        bot = AsyncMock()
        bot.delete_message = AsyncMock()
        bot.edit_message_text = AsyncMock()
        bot.send_message = AsyncMock()

        token = "test_destruct_tok"
        one_time_media_manager._tokens[token] = {"session_id": 99}

        with patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
            await mm._auto_destruct_one_time_media(
                bot=bot,
                token=token,
                receiver_id=3001,
                sender_id=3002,
                revealed_msg_ids=[4001, 4002],
                placeholder_msg_id=5001,
            )
            mock_sleep.assert_called_once_with(OTM_DURATION_SECONDS)

        # Verify messages deleted
        self.assertEqual(bot.delete_message.call_count, 2)
        bot.delete_message.assert_any_call(chat_id=3001, message_id=4001)
        bot.delete_message.assert_any_call(chat_id=3001, message_id=4002)

        # Verify placeholder edited to expired
        bot.edit_message_text.assert_called_once()
        self.assertIn("Expired", bot.edit_message_text.call_args[1]["text"])

        # Verify sender notified
        bot.send_message.assert_called_once()
        self.assertIn("destroyed", bot.send_message.call_args[1]["text"])

        # Token cleaned up
        self.assertIsNone(one_time_media_manager.get(token))


if __name__ == "__main__":
    unittest.main()
