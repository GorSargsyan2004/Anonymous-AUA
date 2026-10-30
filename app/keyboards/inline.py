"""Inline keyboards for user onboarding, post-session feedback, and admin actions."""

from datetime import datetime, timezone
from typing import List, Optional, Sequence
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
    show_typing: bool = True,
) -> InlineKeyboardMarkup:
    """Return inline keyboard with checkboxes for matching preferences and typing status.

    CRITICAL REQUIREMENT: Do NOT use any LGBT flags near the orientations.
    Clean checkboxes only:
      - Male, Female
      - Straight, Bisexual, Lesbian, Gay
      - Typing status toggle ("✅ Show 'Typing...' Status" or "⬜ Show 'Typing...' Status")
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

    # Typing status toggle
    typing_check = "✅" if show_typing else "⬜"
    builder.button(
        text=f"{typing_check} Show 'Typing...' Status",
        callback_data="pref_toggle:typing",
    )

    # Action / Save button
    builder.button(text="💾 Save Preferences", callback_data="pref_save")
    builder.adjust(2, 2, 2, 1, 1)
    return builder.as_markup()


def get_feedback_keyboard(
    session_id: int,
    partner_id: int,
    current_type: Optional[str] = None,
) -> InlineKeyboardMarkup:
    """Return streamlined inline keyboard for post-chat feedback and blocking.
    If current_type is specified, shows checkmark on active choice and adds a Done button.
    """
    builder = InlineKeyboardBuilder()
    like_text = "✅ Great Chat" if current_type == "like" else "👍 Great Chat"
    dislike_text = "✅ Not for me" if current_type == "dislike" else "👎 Not for me"
    builder.button(text=like_text, callback_data=f"fb:{session_id}:{partner_id}:like")
    builder.button(text=dislike_text, callback_data=f"fb:{session_id}:{partner_id}:dislike")
    builder.button(text="🚫 Don't connect again", callback_data=f"block_prompt:{session_id}:{partner_id}")
    builder.button(text="⚠️ Report Issue...", callback_data=f"report_menu:{session_id}:{partner_id}")
    if current_type:
        builder.button(text="🔙 Done", callback_data=f"fb_done:{session_id}:{partner_id}")
        builder.adjust(2, 1, 1, 1)
    else:
        builder.adjust(2, 1, 1)
    return builder.as_markup()


def get_feedback_manage_keyboard(
    session_id: int,
    partner_id: int,
    current_type: str,
) -> InlineKeyboardMarkup:
    """Return management keyboard allowing user to change or reset their submitted rating."""
    builder = InlineKeyboardBuilder()
    builder.button(text="✏️ Change Rating", callback_data=f"fb_edit:{session_id}:{partner_id}")
    builder.button(text="🗑️ Reset / Undo", callback_data=f"fb_reset:{session_id}:{partner_id}")
    builder.button(text="⚠️ Report Issue...", callback_data=f"report_menu:{session_id}:{partner_id}")
    builder.button(text="🚫 Don't connect again", callback_data=f"block_prompt:{session_id}:{partner_id}")
    builder.adjust(2, 2)
    return builder.as_markup()


def get_recent_partners_keyboard(
    sessions: list,
    user_id: int,
    in_active_chat: bool = False,
    active_session_id: Optional[int] = None,
    active_partner_id: Optional[int] = None,
) -> InlineKeyboardMarkup:
    """Return inline keyboard listing recent partners for feedback/reporting."""
    builder = InlineKeyboardBuilder()
    adjust_pattern = []

    if in_active_chat and active_session_id and active_partner_id:
        builder.button(
            text="🚨 Current Partner (Active Chat)",
            callback_data=f"emerg_report_prompt:{active_session_id}:{active_partner_id}",
        )
        adjust_pattern.append(1)

    now = datetime.now(timezone.utc)
    for i, s in enumerate(sessions):
        p_id = s.user2_id if s.user1_id == user_id else s.user1_id
        t = s.ended_at or s.last_activity_at or s.started_at
        if t and t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        if t:
            diff_sec = max(0, (now - t).total_seconds())
            if diff_sec < 60:
                time_str = "just now"
            elif diff_sec < 3600:
                time_str = f"{int(diff_sec // 60)}m ago"
            elif diff_sec < 86400:
                time_str = f"{int(diff_sec // 3600)}h ago"
            else:
                time_str = f"{int(diff_sec // 86400)}d ago"
        else:
            time_str = "recently"

        msgs = s.messages_count or 0
        btn_text = f"👤 Partner #{i + 1} · {time_str} ({msgs} msg{'s' if msgs != 1 else ''})"
        builder.button(text=btn_text, callback_data=f"past_partner:{s.id}:{p_id}")
        adjust_pattern.append(1)

    builder.button(text="🔙 Close", callback_data="past_partner_close")
    adjust_pattern.append(1)

    builder.adjust(*adjust_pattern)
    return builder.as_markup()


def get_past_partner_action_keyboard(session_id: int, partner_id: int) -> InlineKeyboardMarkup:
    """Return inline keyboard with actions for a selected past partner."""
    builder = InlineKeyboardBuilder()
    builder.button(text="👍 Great Chat", callback_data=f"fb:{session_id}:{partner_id}:like")
    builder.button(text="👎 Not for me", callback_data=f"fb:{session_id}:{partner_id}:dislike")
    builder.button(text="⚠️ Report Issue...", callback_data=f"report_menu:{session_id}:{partner_id}")
    builder.button(text="🚫 Don't connect again", callback_data=f"block_prompt:{session_id}:{partner_id}")
    builder.button(text="🔙 Back to Recent List", callback_data="recent_list")
    builder.adjust(2, 2, 1)
    return builder.as_markup()


def get_block_duration_keyboard(session_id: int, partner_id: int) -> InlineKeyboardMarkup:
    """Return inline keyboard with cooldown options for not matching again."""
    builder = InlineKeyboardBuilder()
    builder.button(text="⏰ 1 Day", callback_data=f"block_dur:{partner_id}:1d")
    builder.button(text="⏰ 3 Days", callback_data=f"block_dur:{partner_id}:3d")
    builder.button(text="⏰ 1 Week", callback_data=f"block_dur:{partner_id}:7d")
    builder.button(text="⛔ Permanently", callback_data=f"block_dur:{partner_id}:perm")
    builder.button(text="🔙 Back", callback_data=f"fb_back:{session_id}:{partner_id}")
    builder.adjust(2, 2, 1)
    return builder.as_markup()


def get_blocklist_keyboard(blocks: list, page: int, total_pages: int) -> InlineKeyboardMarkup:
    """Return inline keyboard rendering blocked partners as date buttons with [<] [2/3] [>] pagination."""
    builder = InlineKeyboardBuilder()

    # Add blocked partner buttons (1 per row, showing the block date)
    for block in blocks:
        date_str = block.created_at.strftime("%Y-%m-%d %H:%M")
        dur_label = " (perm)" if block.expires_at is None else " (temp)"
        builder.button(
            text=f"📅 {date_str}{dur_label}",
            callback_data=f"bl_select:{block.id}:{page}",
        )

    adjust_pattern = [1] * len(blocks)

    # Pagination row
    if total_pages > 1:
        prev_data = f"bl_page:{page - 1}" if page > 1 else "bl_noop"
        next_data = f"bl_page:{page + 1}" if page < total_pages else "bl_noop"

        builder.button(text="[<]", callback_data=prev_data)
        builder.button(text=f"[{page}/{total_pages}]", callback_data="bl_noop")
        builder.button(text="[>]", callback_data=next_data)
        adjust_pattern.append(3)

    builder.adjust(*adjust_pattern)
    return builder.as_markup()


def get_unblock_confirmation_keyboard(block_id: int, page: int) -> InlineKeyboardMarkup:
    """Return inline keyboard prompting confirmation to unblock a partner."""
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Yes, Unblock", callback_data=f"bl_unblock_confirm:{block_id}:{page}")
    builder.button(text="🔙 Cancel", callback_data=f"bl_page:{page}")
    builder.adjust(2)
    return builder.as_markup()


def get_report_options_keyboard(session_id: int, partner_id: int) -> InlineKeyboardMarkup:
    """Return inline keyboard with specific issue categories and back button."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🚨 Blackmail / Threats", callback_data=f"report_start:{session_id}:{partner_id}")
    builder.button(text="⚠️ Harassment / Abuse", callback_data=f"report_start:{session_id}:{partner_id}")
    builder.button(text="🚫 Spam / Promo", callback_data=f"fb:{session_id}:{partner_id}:spam")
    builder.button(text="🏫 Not from AUA", callback_data=f"fb:{session_id}:{partner_id}:not_aua")
    builder.button(text="🔙 Back", callback_data=f"fb_back:{session_id}:{partner_id}")
    builder.adjust(2, 2, 1)
    return builder.as_markup()


def get_emergency_report_keyboard(session_id: int, partner_id: int) -> InlineKeyboardMarkup:
    """Return inline keyboard for in-chat emergency /report command."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🛑 End Chat & Report", callback_data=f"emerg_report:{session_id}:{partner_id}")
    builder.button(text="🛡️ Block & Report Partner", callback_data=f"emerg_block_report:{session_id}:{partner_id}")
    builder.button(text="✖️ Cancel", callback_data="emerg_cancel")
    builder.adjust(1, 1, 1)
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
        text="🚫 Ban User",
        callback_data=f"admin_ban_user:{reported_id}",
    )
    builder.button(
        text="⚖️ Penalize User",
        callback_data=f"admin_penalize:{report_id}:{reported_id}",
    )
    builder.button(
        text="✅ Dismiss",
        callback_data=f"admin_dismiss:{report_id}",
    )
    builder.adjust(1, 2)
    return builder.as_markup()


def get_inactivity_warning_keyboard(session_id: int) -> InlineKeyboardMarkup:
    """Return inline keyboard prompting users about inactive/ghost chat."""
    builder = InlineKeyboardBuilder()
    builder.button(text="💬 I'm Here", callback_data=f"inact_keep:{session_id}")
    builder.button(text="🌙 Sleep Mode / Pause", callback_data=f"inact_pause:{session_id}")
    builder.button(text="⏭️ Next Partner", callback_data=f"inact_next:{session_id}")
    builder.button(text="🛑 End Chat", callback_data=f"inact_stop:{session_id}")
    builder.adjust(2, 2)
    return builder.as_markup()


def get_gender_mismatch_keyboard(inferred_gender: str) -> InlineKeyboardMarkup:
    """Return inline keyboard for student to resolve gender conflict or appeal."""
    builder = InlineKeyboardBuilder()
    label = "👨 Switch Profile to Male" if inferred_gender.lower() == "male" else "👩 Switch Profile to Female"
    builder.button(
        text=label,
        callback_data=f"resolve_gender_switch:{inferred_gender.lower()}",
    )
    builder.button(
        text="📩 Appeal to Admin",
        callback_data="gender_appeal_start",
    )
    builder.adjust(1, 1)
    return builder.as_markup()


def get_admin_gender_appeal_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Return inline keyboard for administrator to review gender appeal."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text="👨 Approve Male",
        callback_data=f"admin_gender_appr:{user_id}:male",
    )
    builder.button(
        text="👩 Approve Female",
        callback_data=f"admin_gender_appr:{user_id}:female",
    )
    builder.button(
        text="❌ Reject Appeal",
        callback_data=f"admin_gender_rej:{user_id}",
    )
    builder.adjust(2, 1)
    return builder.as_markup()


# ==============================================================================
# Anonymous Message Inbox (/link) Keyboards
# ==============================================================================

def get_inbox_owner_keyboard(
    has_active_link: bool,
    token: Optional[str] = None,
    bot_username: str = "aua_anonymous_bot",
) -> InlineKeyboardMarkup:
    """Return management keyboard for personal anonymous inbox link."""
    builder = InlineKeyboardBuilder()
    if has_active_link and token:
        share_url = (
            f"https://t.me/share/url?url=https://t.me/{bot_username}?start=msg_{token}"
            "&text=🎭%20Send%20me%20an%20anonymous%20message%20or%20question!"
        )
        builder.button(text="📤 Share Link", url=share_url)
        builder.button(text="🗑️ Destroy & Deactivate Link", callback_data="inbox_destroy")
        builder.adjust(1, 1)
    else:
        builder.button(text="➕ Generate New Link", callback_data="inbox_generate")
        builder.adjust(1)
    return builder.as_markup()


def get_inbox_message_keyboard(thread_token: str) -> InlineKeyboardMarkup:
    """Return inline action buttons for an inbox message received by owner."""
    builder = InlineKeyboardBuilder()
    builder.button(text="💬 Reply", callback_data=f"inbox_reply:{thread_token}")
    builder.button(text="🚫 Block", callback_data=f"inbox_block_confirm:{thread_token}")
    builder.button(text="🚨 Report", callback_data=f"inbox_report_prompt:{thread_token}")

    # Fun reactions
    emojis = ["❤️", "😂", "🔥", "😮", "🙈"]
    for emo in emojis:
        builder.button(text=emo, callback_data=f"inbox_react:{thread_token}:{emo}")

    builder.adjust(3, 5)
    return builder.as_markup()


def get_inbox_block_confirm_keyboard(thread_token: str) -> InlineKeyboardMarkup:
    """Return confirmation keyboard before permanently blocking an anonymous sender."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🚫 Confirm Block", callback_data=f"inbox_block_do:{thread_token}")
    builder.button(text="❌ Cancel", callback_data=f"inbox_block_cancel:{thread_token}")
    builder.adjust(2)
    return builder.as_markup()


def get_inbox_report_confirm_keyboard(thread_token: str) -> InlineKeyboardMarkup:
    """Return confirmation keyboard before reporting an anonymous message to AI Moderator."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🚨 Confirm Report to AI", callback_data=f"inbox_report_do:{thread_token}")
    builder.button(text="❌ Cancel", callback_data=f"inbox_report_cancel:{thread_token}")
    builder.adjust(1, 1)
    return builder.as_markup()


def get_inbox_reply_keyboard(thread_token: str) -> InlineKeyboardMarkup:
    """Return reply-back and report button when partner responds."""
    builder = InlineKeyboardBuilder()
    builder.button(text="💬 Reply Back", callback_data=f"inbox_reply:{thread_token}")
    builder.button(text="🚨 Report", callback_data=f"inbox_report_prompt:{thread_token}")
    builder.adjust(2)
    return builder.as_markup()


def get_inbox_prompt_keyboard() -> InlineKeyboardMarkup:
    """Return helper keyboard when drafting an anonymous message."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🎲 Random Question Idea", callback_data="inbox_random_prompt")
    builder.button(text="❌ Cancel", callback_data="inbox_cancel")
    builder.adjust(1, 1)
    return builder.as_markup()


# ==============================================================================
# Direct Chat Invitation Links (/invite) Keyboards
# ==============================================================================

def get_invite_menu_keyboard(has_active: bool = False) -> InlineKeyboardMarkup:
    """Return menu to create one-time or persistent direct chat invites."""
    builder = InlineKeyboardBuilder()
    builder.button(text="⚡ Create One-Time Invite", callback_data="invite_create:one_time")
    builder.button(text="♾️ Create Persistent Invite", callback_data="invite_create:persistent")
    if has_active:
        builder.button(text="📋 My Active Invites", callback_data="invite_list")
        builder.adjust(1, 1, 1)
    else:
        builder.adjust(1, 1)
    return builder.as_markup()


def get_invite_item_keyboard(invite_code: str) -> InlineKeyboardMarkup:
    """Return action buttons for an active chat invite link."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🗑️ Destroy / Revoke Link", callback_data=f"invite_destroy:{invite_code}")
    builder.button(text="🔙 Back to Invites", callback_data="invite_menu")
    builder.adjust(1, 1)
    return builder.as_markup()


# ==============================================================================
# Privacy Policy & Data Deletion Keyboards
# ==============================================================================

def get_privacy_menu_keyboard() -> InlineKeyboardMarkup:
    """Return navigation keyboard for the Privacy Policy sections."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🛡️ 1. Anonymity & Data", callback_data="privacy_view:data")
    builder.button(text="⚖️ 2. Code of Conduct & Rules", callback_data="privacy_view:rules")
    builder.button(text="📖 Read Full Policy", callback_data="privacy_view:full")
    builder.button(text="🗑️ Delete My Data (/delete_me)", callback_data="delete_me_prompt")
    builder.adjust(1, 1, 1, 1)
    return builder.as_markup()


def get_privacy_back_keyboard() -> InlineKeyboardMarkup:
    """Return back button to return to main Privacy Policy menu."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 Back to Privacy Menu", callback_data="privacy_view:menu")
    return builder.as_markup()


def get_delete_me_confirmation_keyboard() -> InlineKeyboardMarkup:
    """Return confirmation keyboard before deleting user account permanently."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🗑️ Yes, Delete Everything", callback_data="delete_me_confirm")
    builder.button(text="❌ Cancel", callback_data="delete_me_cancel")
    builder.adjust(1, 1)
    return builder.as_markup()


def get_verification_reminder_keyboard() -> InlineKeyboardMarkup:
    """Return action buttons for the 24-hour verification reminder:
    - [ 🎓 Verify Status ]
    - [ ⏳ Remind in a week ]
    - [ 🚫 Never show again ]
    """
    builder = InlineKeyboardBuilder()
    builder.button(text="🎓 Verify Now (/verify)", callback_data="verif_remind_start")
    builder.button(text="⏳ Remind in a week", callback_data="verif_remind_week")
    builder.button(text="🚫 Never show this again", callback_data="verif_remind_never")
    builder.adjust(1, 2)
    return builder.as_markup()





