"""Inline keyboards for user onboarding, post-session feedback, and admin actions."""

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def get_sex_keyboard() -> InlineKeyboardMarkup:
    """Return inline keyboard for selecting user sex during onboarding."""
    builder = InlineKeyboardBuilder()
    builder.button(text="👨 Male", callback_data="onboarding_sex:male")
    builder.button(text="👩 Female", callback_data="onboarding_sex:female")
    builder.adjust(2)
    return builder.as_markup()


def get_orientation_keyboard() -> InlineKeyboardMarkup:
    """Return inline keyboard for selecting user sexual orientation during onboarding."""
    builder = InlineKeyboardBuilder()
    builder.button(text="👫 Straight", callback_data="onboarding_orientation:straight")
    builder.button(text="🌈 Bisexual", callback_data="onboarding_orientation:bisexual")
    builder.button(text="👭 Lesbian", callback_data="onboarding_orientation:lesbian")
    builder.button(text="👬 Gay", callback_data="onboarding_orientation:gay")
    builder.adjust(2, 2)
    return builder.as_markup()


def get_feedback_keyboard(session_id: int, partner_id: int) -> InlineKeyboardMarkup:
    """Return inline keyboard for rating the session partner or reporting them (priorities hidden)."""
    builder = InlineKeyboardBuilder()
    builder.button(text="👍 Like", callback_data=f"fb:{session_id}:{partner_id}:like")
    builder.button(text="👎 Dislike", callback_data=f"fb:{session_id}:{partner_id}:dislike")
    builder.button(text="🚫 Spam", callback_data=f"fb:{session_id}:{partner_id}:spam")
    builder.button(text="🏫 Not from AUA", callback_data=f"fb:{session_id}:{partner_id}:not_aua")
    builder.button(text="⚠️ Report User", callback_data=f"report_start:{session_id}:{partner_id}")
    builder.adjust(2, 2, 1)
    return builder.as_markup()


def get_bot_feedback_keyboard() -> InlineKeyboardMarkup:
    """Return inline keyboard prompting user for bot improvement suggestions."""
    builder = InlineKeyboardBuilder()
    builder.button(text="💡 Give suggestions", callback_data="suggest_start")
    builder.button(text="✖️ Dismiss", callback_data="suggest_dismiss")
    builder.adjust(2)
    return builder.as_markup()


def get_admin_suggestion_keyboard(suggestion_id: int, user_id: int) -> InlineKeyboardMarkup:
    """Return inline keyboard allowing admin to reply to a student suggestion."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text="✍️ Reply to Student",
        callback_data=f"admin_reply_sug:{suggestion_id}:{user_id}",
    )
    builder.adjust(1)
    return builder.as_markup()


def get_admin_report_keyboard(report_id: int, reported_id: int) -> InlineKeyboardMarkup:
    """Return inline keyboard for administrator actions on a reported user."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text="⚖️ Penalize User",
        callback_data=f"admin_penalize:{report_id}:{reported_id}",
    )
    builder.button(
        text="✅ Dismiss",
        callback_data=f"admin_dismiss:{report_id}",
    )
    builder.adjust(2)
    return builder.as_markup()

