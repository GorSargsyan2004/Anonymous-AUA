"""Inline keyboards for user onboarding, post-session feedback, and admin actions."""

from typing import List, Sequence
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


def get_preferences_keyboard(
    selected_sexes: Sequence[str],
    selected_orientations: Sequence[str],
) -> InlineKeyboardMarkup:
    """Return inline keyboard with checkboxes for matching preferences.

    CRITICAL REQUIREMENT: Do NOT use any LGBT flags near the orientations.
    Clean checkboxes only:
      - Male, Female
      - Straight, Bisexual, Lesbian, Gay
      - Save Preferences
    """
    builder = InlineKeyboardBuilder()

    # Sex checkboxes
    sexes_lower = [s.lower().strip() for s in selected_sexes]
    male_check = "✅" if "male" in sexes_lower else "⬜"
    female_check = "✅" if "female" in sexes_lower else "⬜"
    builder.button(text=f"{male_check} Male", callback_data="pref_toggle:sex:male")
    builder.button(text=f"{female_check} Female", callback_data="pref_toggle:sex:female")

    # Orientation checkboxes (NO LGBT FLAGS)
    orients_lower = [o.lower().strip() for o in selected_orientations]
    str_check = "✅" if "straight" in orients_lower else "⬜"
    bi_check = "✅" if "bisexual" in orients_lower else "⬜"
    les_check = "✅" if "lesbian" in orients_lower else "⬜"
    gay_check = "✅" if "gay" in orients_lower else "⬜"

    builder.button(text=f"{str_check} Straight", callback_data="pref_toggle:orient:straight")
    builder.button(text=f"{bi_check} Bisexual", callback_data="pref_toggle:orient:bisexual")
    builder.button(text=f"{les_check} Lesbian", callback_data="pref_toggle:orient:lesbian")
    builder.button(text=f"{gay_check} Gay", callback_data="pref_toggle:orient:gay")

    # Action / Save button
    builder.button(text="💾 Save Preferences", callback_data="pref_save")
    builder.adjust(2, 2, 2, 1)
    return builder.as_markup()


def get_feedback_keyboard(session_id: int, partner_id: int) -> InlineKeyboardMarkup:
    """Return streamlined inline keyboard for post-chat feedback (2-tier anti-spite design)."""
    builder = InlineKeyboardBuilder()
    builder.button(text="👍 Great Chat", callback_data=f"fb:{session_id}:{partner_id}:like")
    builder.button(text="👎 Not for me", callback_data=f"fb:{session_id}:{partner_id}:dislike")
    builder.button(text="⚠️ Report Issue...", callback_data=f"report_menu:{session_id}:{partner_id}")
    builder.adjust(2, 1)
    return builder.as_markup()


def get_report_options_keyboard(session_id: int, partner_id: int) -> InlineKeyboardMarkup:
    """Return inline keyboard with specific issue categories and back button."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🚫 Spam / Promo", callback_data=f"fb:{session_id}:{partner_id}:spam")
    builder.button(text="🏫 Not from AUA", callback_data=f"fb:{session_id}:{partner_id}:not_aua")
    builder.button(text="🚨 Harassment / Other", callback_data=f"report_start:{session_id}:{partner_id}")
    builder.button(text="🔙 Back", callback_data=f"fb_back:{session_id}:{partner_id}")
    builder.adjust(2, 1, 1)
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

