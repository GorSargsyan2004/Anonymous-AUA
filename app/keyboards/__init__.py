"""Keyboards package."""

from app.keyboards.inline import (
    get_admin_report_keyboard,
    get_admin_suggestion_keyboard,
    get_bot_feedback_keyboard,
    get_feedback_keyboard,
    get_orientation_keyboard,
    get_report_options_keyboard,
    get_sex_keyboard,
)

__all__ = [
    "get_sex_keyboard",
    "get_orientation_keyboard",
    "get_feedback_keyboard",
    "get_report_options_keyboard",
    "get_bot_feedback_keyboard",
    "get_admin_suggestion_keyboard",
    "get_admin_report_keyboard",
]

