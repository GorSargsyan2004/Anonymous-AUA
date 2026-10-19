"""Unit and integration tests for the Typing Indicator emulation & Preferences toggle.

Tests verify:
1. User model defaults show_typing to True.
2. Keyboard rendering reflects show_typing on/off state cleanly without flags.
3. CRUD methods update_user_preferences and set_user_typing_preference.
4. /preferences menu toggle and save handlers persist show_typing preference.
5. Quick /typing command toggles state on/off.
6. Relayed messages send chat action to partner when enabled.
7. Relayed messages omit chat action when disabled (stealth mode).
8. Action type and duration calculation helpers.
"""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from aiogram import Bot
from aiogram.types import CallbackQuery, Message, User as TgUser

from app.database.models import User
from app.keyboards.inline import get_preferences_keyboard
from app.handlers.matchmaking import (
    get_chat_action_for_message,
    get_emulated_typing_duration,
)


class TestTypingIndicator(unittest.IsolatedAsyncioTestCase):
    """Tests for typing indicator emulation, preferences keyboard, and message relaying."""

    def test_01_user_model_show_typing_default(self):
        """User model initializes show_typing to True by default."""
        user = User(id=1001, username="test_user")
        self.assertTrue(user.show_typing)

    def test_02_preferences_keyboard_shows_typing_toggle(self):
        """Preferences keyboard renders typing checkbox with proper checkmark state."""
        # Default True -> ✅ Show 'Typing...' Status
        kb_on = get_preferences_keyboard(
            selected_sexes=["male"],
            selected_orientations=["straight"],
            show_typing=True,
        )
        button_texts_on = [btn.text for row in kb_on.inline_keyboard for btn in row]
        self.assertIn("✅ Show 'Typing...' Status", button_texts_on)

        # False -> ⬜ Show 'Typing...' Status
        kb_off = get_preferences_keyboard(
            selected_sexes=["male"],
            selected_orientations=["straight"],
            show_typing=False,
        )
        button_texts_off = [btn.text for row in kb_off.inline_keyboard for btn in row]
        self.assertIn("⬜ Show 'Typing...' Status", button_texts_off)

    def test_03_action_type_and_duration_helpers(self):
        """Action type and duration correctly correspond to message content."""
        # Text
        msg_text = MagicMock(spec=Message)
        msg_text.text = "Hello there!"
        msg_text.caption = None
        msg_text.photo = None
        msg_text.voice = None
        msg_text.video = None
        msg_text.video_note = None
        msg_text.sticker = None
        msg_text.audio = None
        msg_text.document = None

        self.assertEqual(get_chat_action_for_message(msg_text), "typing")
        duration = get_emulated_typing_duration(msg_text)
        self.assertGreaterEqual(duration, 0.5)
        self.assertLessEqual(duration, 1.8)

        # Photo
        msg_photo = MagicMock(spec=Message)
        msg_photo.text = None
        msg_photo.caption = None
        msg_photo.photo = [MagicMock()]
        msg_photo.voice = None
        msg_photo.video = None
        msg_photo.video_note = None
        msg_photo.sticker = None
        msg_photo.audio = None
        msg_photo.document = None
        self.assertEqual(get_chat_action_for_message(msg_photo), "upload_photo")

        # Voice
        msg_voice = MagicMock(spec=Message)
        msg_voice.text = None
        msg_voice.caption = None
        msg_voice.photo = None
        msg_voice.voice = MagicMock()
        msg_voice.video = None
        msg_voice.video_note = None
        msg_voice.sticker = None
        msg_voice.audio = None
        msg_voice.document = None
        self.assertEqual(get_chat_action_for_message(msg_voice), "record_voice")

        # Video
        msg_video = MagicMock(spec=Message)
        msg_video.text = None
        msg_video.caption = None
        msg_video.photo = None
        msg_video.voice = None
        msg_video.video = MagicMock()
        msg_video.video_note = None
        msg_video.sticker = None
        msg_video.audio = None
        msg_video.document = None
        self.assertEqual(get_chat_action_for_message(msg_video), "upload_video")

    @patch("app.database.crud.select")
    async def test_04_crud_update_preferences_with_typing(self, mock_select):
        """update_user_preferences properly updates user.show_typing."""
        from app.database.crud import update_user_preferences, set_user_typing_preference

        mock_user = MagicMock(spec=User)
        mock_user.id = 1234
        mock_user.show_typing = True

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_user
        mock_session.execute.return_value = mock_result

        # Update via update_user_preferences
        updated = await update_user_preferences(
            session=mock_session,
            user_id=1234,
            target_sexes=["female"],
            target_orientations=["straight"],
            show_typing=False,
        )
        self.assertEqual(mock_user.show_typing, False)

        # Update via set_user_typing_preference
        await set_user_typing_preference(
            session=mock_session,
            user_id=1234,
            show_typing=True,
        )
        self.assertEqual(mock_user.show_typing, True)

    @patch("app.handlers.start.get_db_session")
    async def test_05_handle_preference_toggle_typing_callback(self, mock_get_db):
        """Callback pref_toggle:typing toggles state and updates reply markup."""
        from app.handlers.start import handle_preference_toggle

        mock_state = MagicMock()
        mock_state.get_data = AsyncMock(return_value={
            "pref_sexes": ["male"],
            "pref_orientations": ["straight"],
            "pref_show_typing": True,
        })
        mock_state.update_data = AsyncMock()

        mock_callback = MagicMock(spec=CallbackQuery)
        mock_callback.data = "pref_toggle:typing"
        mock_callback.from_user = MagicMock(spec=TgUser)
        mock_callback.from_user.id = 5555
        mock_callback.message = MagicMock()
        mock_callback.message.edit_reply_markup = AsyncMock()
        mock_callback.answer = AsyncMock()

        await handle_preference_toggle(callback=mock_callback, state=mock_state)

        # Should have updated pref_show_typing to False
        mock_state.update_data.assert_awaited_once_with(pref_show_typing=False)
        mock_callback.message.edit_reply_markup.assert_awaited_once()
        mock_callback.answer.assert_awaited_once()

    @patch("app.handlers.start.update_user_preferences")
    @patch("app.handlers.start.get_user")
    @patch("app.handlers.start.get_db_session")
    async def test_06_handle_preference_save_saves_typing(self, mock_get_db, mock_get_user, mock_update_pref):
        """Saving preferences persists show_typing to database."""
        from app.handlers.start import handle_preference_save

        mock_user = MagicMock(spec=User)
        mock_user.id = 7777
        mock_user.sex = "male"
        mock_user.orientation = "straight"
        mock_get_user.return_value = mock_user

        mock_state = MagicMock()
        mock_state.get_data = AsyncMock(return_value={
            "pref_sexes": ["female"],
            "pref_orientations": ["straight"],
            "pref_show_typing": False,
            "is_onboarding": False,
        })
        mock_state.clear = AsyncMock()

        mock_callback = MagicMock(spec=CallbackQuery)
        mock_callback.data = "pref_save"
        mock_callback.from_user = MagicMock(spec=TgUser)
        mock_callback.from_user.id = 7777
        mock_callback.message = MagicMock()
        mock_callback.message.edit_text = AsyncMock()
        mock_callback.answer = AsyncMock()

        await handle_preference_save(callback=mock_callback, state=mock_state)

        mock_update_pref.assert_awaited_once_with(
            session=mock_get_db.return_value.__aenter__.return_value,
            user_id=7777,
            target_sexes=["female"],
            target_orientations=["straight"],
            show_typing=False,
        )
        # Confirmation message should mention Disabled / Stealth Mode
        call_args = mock_callback.message.edit_text.call_args[0][0]
        self.assertIn("Disabled ❌", call_args)

    @patch("app.handlers.start.set_user_typing_preference")
    @patch("app.handlers.start.get_or_create_user")
    @patch("app.handlers.start.get_db_session")
    async def test_07_handle_typing_toggle_command(self, mock_get_db, mock_get_or_create, mock_set_typing):
        """Command /typing toggles user preference and answers with confirmation."""
        from app.handlers.start import handle_typing_toggle_command

        mock_user = MagicMock(spec=User)
        mock_user.id = 8888
        mock_user.show_typing = True  # currently True -> should flip to False
        mock_get_or_create.return_value = mock_user

        mock_msg = MagicMock(spec=Message)
        mock_msg.from_user = MagicMock(spec=TgUser)
        mock_msg.from_user.id = 8888
        mock_msg.answer = AsyncMock()

        await handle_typing_toggle_command(message=mock_msg)

        mock_set_typing.assert_awaited_once_with(
            mock_get_db.return_value.__aenter__.return_value,
            user_id=8888,
            show_typing=False,
        )
        mock_msg.answer.assert_awaited_once()
        self.assertIn("Disabled ❌", mock_msg.answer.call_args[0][0])

    @patch("app.handlers.matchmaking.reply_manager.register_message")
    @patch("app.handlers.matchmaking.get_active_session_by_user_id")
    @patch("app.handlers.matchmaking.get_or_create_user")
    @patch("app.handlers.matchmaking.get_db_session")
    async def test_08_relay_sends_chat_action_when_enabled(
        self, mock_get_db, mock_get_user, mock_get_active_session, mock_register_msg
    ):
        """When show_typing is True, relay_anonymous_message sends typing chat action to partner."""
        import app.handlers.matchmaking as mm

        user_a = 9101
        user_b = 9102

        mock_db_user = MagicMock()
        mock_db_user.current_partner_id = user_b
        mock_db_user.show_typing = True
        mock_get_user.return_value = mock_db_user

        mock_session_obj = MagicMock()
        mock_session_obj.id = 200
        mock_get_active_session.return_value = mock_session_obj

        mock_bot = MagicMock(spec=Bot)
        mock_bot.send_chat_action = AsyncMock(return_value=True)

        mock_state = MagicMock()
        mock_state.get_state = AsyncMock(return_value=None)

        mock_msg = MagicMock(spec=Message)
        mock_msg.from_user = MagicMock(spec=TgUser)
        mock_msg.from_user.id = user_a
        mock_msg.message_id = 501
        mock_msg.text = "Testing typing indicator!"
        mock_msg.external_reply = None
        mock_msg.reply_to_message = None
        mock_msg.copy_to = AsyncMock(return_value=MagicMock(message_id=601))

        # Disable sleep duration in tests for instantaneous execution
        original_sleep_flag = mm.EMULATED_TYPING_SLEEP
        try:
            mm.EMULATED_TYPING_SLEEP = False
            await mm.relay_anonymous_message(message=mock_msg, bot=mock_bot, state=mock_state)

            mock_bot.send_chat_action.assert_awaited_once_with(chat_id=user_b, action="typing")
            mock_msg.copy_to.assert_awaited_once_with(chat_id=user_b)
        finally:
            mm.EMULATED_TYPING_SLEEP = original_sleep_flag

    @patch("app.handlers.matchmaking.reply_manager.register_message")
    @patch("app.handlers.matchmaking.get_active_session_by_user_id")
    @patch("app.handlers.matchmaking.get_or_create_user")
    @patch("app.handlers.matchmaking.get_db_session")
    async def test_09_relay_skips_chat_action_when_disabled(
        self, mock_get_db, mock_get_user, mock_get_active_session, mock_register_msg
    ):
        """When show_typing is False (stealth mode), relay_anonymous_message omits chat action."""
        import app.handlers.matchmaking as mm

        user_a = 9101
        user_b = 9102

        mock_db_user = MagicMock()
        mock_db_user.current_partner_id = user_b
        mock_db_user.show_typing = False  # Stealth mode
        mock_get_user.return_value = mock_db_user

        mock_session_obj = MagicMock()
        mock_session_obj.id = 200
        mock_get_active_session.return_value = mock_session_obj

        mock_bot = MagicMock(spec=Bot)
        mock_bot.send_chat_action = AsyncMock()

        mock_state = MagicMock()
        mock_state.get_state = AsyncMock(return_value=None)

        mock_msg = MagicMock(spec=Message)
        mock_msg.from_user = MagicMock(spec=TgUser)
        mock_msg.from_user.id = user_a
        mock_msg.message_id = 502
        mock_msg.text = "Stealth message"
        mock_msg.external_reply = None
        mock_msg.reply_to_message = None
        mock_msg.copy_to = AsyncMock(return_value=MagicMock(message_id=602))

        original_sleep_flag = mm.EMULATED_TYPING_SLEEP
        try:
            mm.EMULATED_TYPING_SLEEP = False
            await mm.relay_anonymous_message(message=mock_msg, bot=mock_bot, state=mock_state)

            mock_bot.send_chat_action.assert_not_called()
            mock_msg.copy_to.assert_awaited_once_with(chat_id=user_b)
        finally:
            mm.EMULATED_TYPING_SLEEP = original_sleep_flag


if __name__ == "__main__":
    unittest.main()
