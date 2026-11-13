from datetime import datetime, timedelta, timezone
import html
import logging
import math
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import delete

from app.config import settings
from app.database.crud import (
    add_or_update_user_block,
    apply_admin_penalty,
    ban_user_by_admin,
    count_recent_sessions_for_user,
    create_reconnect_request,
    create_report,
    create_suggestion,
    delete_or_reset_feedback,
    end_active_session,
    get_active_session_by_user_id,
    get_chat_session,
    get_feedback_by_session_and_user,
    get_latest_session_for_user,
    get_or_create_user,
    get_recent_reconnect_request_between,
    get_recent_sessions_for_user,
    get_reconnect_request_by_id,
    get_session_duration_seconds,
    get_user,
    has_report_for_session,
    is_user_blocked_by_partner,
    process_feedback_with_consensus,
    record_feedback,
    update_or_override_feedback,
    update_reconnect_request_status,
    update_user_priority,
)
from app.database.models import ChatSession, UserBlock
from app.services.ai_moderator import (
    gemini_moderator,
    session_buffer,
)
from app.services.ban_manager import add_banned_user
from app.services.reply_manager import reply_manager
from app.database.session import get_db_session
from app.keyboards.inline import (
    get_admin_report_keyboard,
    get_admin_suggestion_keyboard,
    get_block_duration_keyboard,
    get_emergency_report_keyboard,
    get_feedback_keyboard,
    get_feedback_manage_keyboard,
    get_past_partner_action_keyboard,
    get_recent_partners_keyboard,
    get_reconnect_incoming_keyboard,
    get_reconnect_ready_keyboard,
    get_reconnect_reason_keyboard,
    get_report_options_keyboard,
    get_sunset_duration_keyboard,
)
from app.states.states import ReportState, SuggestionState

logger = logging.getLogger(__name__)
router = Router(name="feedback_router")


@router.callback_query(F.data.startswith("report_menu:"))
async def handle_report_menu(callback: CallbackQuery, state: FSMContext) -> None:
    """Show specific issue options when user clicks 'Report Issue...'."""
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer()
        return
    session_id = int(parts[1])
    partner_id = int(parts[2])

    # Immediately arm FSM state so any typed message/screenshot is captured as a report!
    await state.set_state(ReportState.waiting_for_reason)
    await state.update_data(session_id=session_id, reported_id=partner_id)

    await callback.message.edit_text(
        "⚠️ <b>Report an Issue / Incident</b> 🛡️\n\n"
        "What problem did you encounter with this partner?\n\n"
        "<i>Select a category below, or simply type your report / attach a screenshot:</i>",
        reply_markup=get_report_options_keyboard(session_id, partner_id),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("fb_back:"))
async def handle_feedback_back(callback: CallbackQuery, state: FSMContext) -> None:
    """Return to Tier 1 post-chat rating keyboard."""
    await state.clear()
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer()
        return
    session_id = int(parts[1])
    partner_id = int(parts[2])

    await callback.message.edit_text(
        "🛑 <b>Chat ended.</b>\n\n"
        "How was your conversation with this partner?",
        reply_markup=get_feedback_keyboard(session_id, partner_id),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("block_prompt:"))
async def handle_block_prompt(callback: CallbackQuery) -> None:
    """Prompt user to choose how long to avoid matching with partner."""
    parts = callback.data.split(":")
    if len(parts) < 3:
        await callback.answer()
        return
    session_id = int(parts[1])
    partner_id = int(parts[2])
    from_dislike = len(parts) >= 4 and parts[3] == "dislike"

    back_callback = (
        f"fb_back_dislike:{session_id}:{partner_id}"
        if from_dislike
        else f"fb_back:{session_id}:{partner_id}"
    )

    await callback.message.edit_text(
        "🚫 <b>Do Not Connect Again</b>\n\n"
        "How long would you like to avoid connecting with this partner?\n\n"
        "• <b>Temporary (1d / 3d / 1w):</b> Take a break from matching with them, but allow crossing paths later.\n"
        "• <b>Permanent:</b> Never match with this partner again.\n\n"
        "<i>Select a cooldown duration:</i>",
        reply_markup=get_block_duration_keyboard(
            session_id, partner_id, back_callback=back_callback
        ),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("block_dur:"))
async def handle_block_duration(callback: CallbackQuery) -> None:
    """Save user-to-partner block/cooldown and confirm to user."""
    parts = callback.data.split(":")
    if len(parts) < 3:
        await callback.answer()
        return
    partner_id = int(parts[1])
    dur_key = parts[2]
    user_id = callback.from_user.id
    now = datetime.now(timezone.utc)

    if dur_key == "1d":
        expires_at = now + timedelta(days=1)
        dur_text = "1 day"
        is_temp = True
    elif dur_key == "3d":
        expires_at = now + timedelta(days=3)
        dur_text = "3 days"
        is_temp = True
    elif dur_key == "7d":
        expires_at = now + timedelta(days=7)
        dur_text = "1 week"
        is_temp = True
    else:
        expires_at = None
        dur_text = "permanently"
        is_temp = False

    async with get_db_session() as session:
        await add_or_update_user_block(
            session=session,
            user_id=user_id,
            blocked_user_id=partner_id,
            expires_at=expires_at,
            reason="dislike",
        )

    if is_temp:
        resp_text = (
            f"🛑 <b>Partner Blocked!</b>\n\n"
            f"You will not connect with this anonymous partner for <b>{dur_text}</b>.\n"
            f"• After {dur_text}, they will return to your search pool naturally.\n\n"
            "💡 <i>You can view, manage, or unblock partners anytime using /blocklist.</i>\n\n"
            "Send /search to find a new chat partner! 🎭"
        )
    else:
        resp_text = (
            "🛑 <b>Partner Blocked!</b>\n\n"
            "You will not connect with this anonymous partner <b>permanently</b>.\n"
            "Your paths will not cross again in random chat.\n\n"
            "💡 <i>You can view, manage, or unblock partners anytime using /blocklist.</i>\n\n"
            "Send /search to find a new chat partner! 🎭"
        )

    await callback.message.edit_text(resp_text, parse_mode="HTML")
    await callback.answer("Partner blocked!")


@router.callback_query(F.data.startswith("fb_sunset_prompt:"))
async def handle_feedback_sunset_prompt(callback: CallbackQuery) -> None:
    """Prompt user to choose temporary cooldown or permanent closure for a great chat."""
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer()
        return
    session_id = int(parts[1])
    partner_id = int(parts[2])

    await callback.message.edit_text(
        "🕊️ <b>Close Chapter / Take a Break</b>\n\n"
        "How long would you like to avoid connecting with this partner?\n\n"
        "• <b>Temporary (1d / 3d / 1w):</b> Pause matching to let the good conversation settle, but allow crossing paths later.\n"
        "• <b>Permanent:</b> Keep the chat as a perfect one-time memory and never match again.\n\n"
        "<i>Select a cooldown duration:</i>",
        reply_markup=get_sunset_duration_keyboard(session_id, partner_id),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("fb_sunset_dur:"))
async def handle_feedback_sunset_duration(callback: CallbackQuery) -> None:
    """Save user-to-partner block/cooldown with closed_chapter reason and friendly confirmation."""
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer()
        return
    partner_id = int(parts[1])
    dur_key = parts[2]
    user_id = callback.from_user.id
    now = datetime.now(timezone.utc)

    if dur_key == "1d":
        expires_at = now + timedelta(days=1)
        dur_text = "1 day"
        is_temp = True
    elif dur_key == "3d":
        expires_at = now + timedelta(days=3)
        dur_text = "3 days"
        is_temp = True
    elif dur_key == "7d":
        expires_at = now + timedelta(days=7)
        dur_text = "1 week"
        is_temp = True
    else:
        expires_at = None
        dur_text = "permanently"
        is_temp = False

    async with get_db_session() as session:
        await add_or_update_user_block(
            session=session,
            user_id=user_id,
            blocked_user_id=partner_id,
            expires_at=expires_at,
            reason="closed_chapter",
        )

    if is_temp:
        resp_text = (
            "✨ <b>Chapter paused on a high note!</b> 🕊️\n\n"
            "• Your partner received your <b>👍 Great Chat</b> rating! ⭐\n"
            f"• <b>Cooldown active:</b> You will not match for <b>{dur_text}</b>.\n"
            f"• After {dur_text}, they will return to your search pool naturally.\n\n"
            "💡 <i>You can view, manage, or reopen this chapter anytime using /blocklist.</i>\n\n"
            "Send /search to find a new chat partner! 🎭"
        )
    else:
        resp_text = (
            "✨ <b>Chapter closed on a high note!</b> 🕊️\n\n"
            "• Your partner received your <b>👍 Great Chat</b> rating! ⭐\n"
            "• Your paths will not cross again in random chat.\n\n"
            "<i>\"Some conversations are best kept as perfect one-time memories.\"</i> 🎭\n\n"
            "💡 <i>Changed your mind? You can reopen this chapter anytime using /blocklist.</i>\n\n"
            "Send /search to find a new chat partner! 🎭"
        )

    await callback.message.edit_text(resp_text, parse_mode="HTML")
    await callback.answer("Chapter closed! 🕊️")


@router.callback_query(F.data.startswith("fb_rematch_ok:"))
async def handle_feedback_rematch_ok(callback: CallbackQuery) -> None:
    """User prefers to keep partner in matching pool."""
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer()
        return
    session_id = int(parts[1])
    partner_id = int(parts[2])
    user_id = callback.from_user.id

    async with get_db_session() as session:
        from app.database.models import UserBlock
        stmt = delete(UserBlock).where(UserBlock.user_id == user_id, UserBlock.blocked_user_id == partner_id)
        await session.execute(stmt)
        await session.commit()
        existing = await get_feedback_by_session_and_user(session, session_id, user_id)
        fb_type = existing.feedback_type if existing else "like"

    if fb_type == "dislike":
        rating_note = "• Your rating (<b>👎 Not for me</b>) has been recorded."
    else:
        rating_note = "• Your partner received your <b>👍 Great Chat</b> rating! ⭐"

    await callback.message.edit_text(
        "✨ <b>Preferences Saved!</b> 🎭\n\n"
        f"{rating_note}\n"
        "• You may cross paths with them again naturally in future searches.\n\n"
        "Send /search whenever you are ready for a new chat! 🎭",
        parse_mode="HTML",
    )
    await callback.answer("Saved! Open to rematch.")


@router.callback_query(F.data.startswith("fb_back_like:"))
async def handle_feedback_back_like(callback: CallbackQuery) -> None:
    """Return to Great Chat preference options."""
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer()
        return
    session_id = int(parts[1])
    partner_id = int(parts[2])

    msg_text = (
        "✅ <b>Feedback submitted: 👍 Great Chat!</b>\n\n"
        "Glad you had a great conversation! 🎭\n\n"
        "<b>Would you like to connect with this person again in future chats?</b>\n"
        "• <b>Open to Rematch:</b> Keep them in your matching pool.\n"
        "• <b>Close Chapter:</b> Take a break (1d, 3d, 1w) or close permanently so your paths don't cross awkwardly."
    )
    manage_kb = get_feedback_manage_keyboard(session_id, partner_id, "like")
    await callback.message.edit_text(msg_text, reply_markup=manage_kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("fb_back_dislike:"))
async def handle_feedback_back_dislike(callback: CallbackQuery) -> None:
    """Return to Not for me preference options."""
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer()
        return
    session_id = int(parts[1])
    partner_id = int(parts[2])

    msg_text = (
        "✅ <b>Feedback submitted: 👎 Not for me.</b>\n\n"
        "Thank you for your feedback! We'll use this to match you with better partners! 🎭\n\n"
        "<b>Would you like to connect with this person again in future chats?</b>\n"
        "• <b>Avoid Matching:</b> Take a break (1d, 3d, 1w) or block permanently so you don't connect again.\n"
        "• <b>Open to Rematch:</b> Keep them in your search pool despite this chat."
    )
    manage_kb = get_feedback_manage_keyboard(session_id, partner_id, "dislike")
    await callback.message.edit_text(msg_text, reply_markup=manage_kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("fb:"))
async def handle_feedback_callback(callback: CallbackQuery, bot: Bot) -> None:
    """Process feedback with anti-spite chat duration checks and consensus penalty logic."""
    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer("Invalid feedback data.", show_alert=True)
        return

    _, session_id_str, partner_id_str, feedback_type = parts
    session_id = int(session_id_str)
    partner_id = int(partner_id_str)
    from_user_id = callback.from_user.id

    valid_types = {"like", "dislike", "spam", "not_aua"}
    if feedback_type not in valid_types:
        await callback.answer("Unknown feedback type.", show_alert=True)
        return

    async with get_db_session() as session:
        result = await update_or_override_feedback(
            session=session,
            session_id=session_id,
            from_user_id=from_user_id,
            partner_id=partner_id,
            new_feedback_type=feedback_type,
        )

    if result.get("already_set"):
        await callback.answer("You have already selected this rating.", show_alert=False)
        return

    is_update = result.get("updated", False)
    prefix = "Rating updated" if is_update else "Feedback submitted"

    # User-facing confirmation message
    if feedback_type == "like":
        msg_text = (
            f"✅ <b>{prefix}: 👍 Great Chat!</b>\n\n"
            "Glad you had a great conversation! 🎭\n\n"
            "<b>Would you like to connect with this person again in future chats?</b>\n"
            "• <b>Open to Rematch:</b> Keep them in your matching pool.\n"
            "• <b>Close Chapter:</b> Take a break (1d, 3d, 1w) or close permanently so your paths don't cross awkwardly."
        )
    elif feedback_type == "dislike":
        note = ""
        if result.get("is_unengaged", False) or result.get("is_short_chat", False):
            note = "<i>(Since the conversation was very brief or had minimal interaction, this was recorded as a neutral skip).</i>\n\n"

        msg_text = (
            f"✅ <b>{prefix}: 👎 Not for me.</b>\n\n"
            f"{note}"
            "Thank you for your feedback! We'll use this to match you with better partners! 🎭\n\n"
            "<b>Would you like to connect with this person again in future chats?</b>\n"
            "• <b>Avoid Matching:</b> Take a break (1d, 3d, 1w) or block permanently so you don't connect again.\n"
            "• <b>Open to Rematch:</b> Keep them in your search pool despite this chat."
        )
    elif feedback_type == "spam":
        msg_text = (
            f"✅ <b>Report recorded: 🚫 Spam / Promotion.</b>\n\n"
            "Thank you for keeping AUA safe and clean. Our automated system tracks repeated reports "
            "across independent students and penalizes offenders. 🛡️\n\n"
            "<i>Changed your mind or clicked by mistake? You can update or reset your rating below:</i>"
        )
    elif feedback_type == "not_aua":
        msg_text = (
            f"✅ <b>Report recorded: 🏫 Not from AUA.</b>\n\n"
            "Thank you! Our automated system tracks reports from multiple students to identify "
            "and filter out non-AUA intruders. 🛡️\n\n"
            "<i>Changed your mind or clicked by mistake? You can update or reset your rating below:</i>"
        )
        # If reported partner is a verified student, send critical alert to administrators
        partner_obj = result.get("partner")
        if partner_obj and getattr(partner_obj, "is_verified", False):
            reporter_name = html.escape(callback.from_user.full_name or "AUA Student")
            reporter_username = f"@{callback.from_user.username}" if callback.from_user.username else f"ID: {from_user_id}"
            partner_username = f"@{partner_obj.username}" if partner_obj.username else f"ID: {partner_id}"
            partner_email = html.escape(partner_obj.email or "N/A")

            alert_text = (
                "🚨 <b>AUDIT WARNING: VERIFIED STUDENT REPORTED AS 'NOT FROM AUA'!</b> ⚠️\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "A student reported their partner for not being from AUA, but this partner holds <b>Verified Student Status</b>!\n\n"
                f"👤 <b>Reported Student:</b> {partner_username}\n"
                f"🎓 <b>Verified AUA Email:</b> <code>{partner_email}</code>\n"
                f"⚡ <b>Priority:</b> {partner_obj.priority}\n\n"
                f"👤 <b>Reporting User:</b> {reporter_username} ({reporter_name})\n"
                f"🆔 <b>Session ID:</b> #{session_id}\n\n"
                "⚠️ <i>Please investigate: Either the reporter made a false/malicious report, or the student's email/account is compromised.</i>"
            )
            for admin_id in settings.admin_ids:
                try:
                    await bot.send_message(chat_id=admin_id, text=alert_text, parse_mode="HTML")
                except TelegramAPIError as e:
                    logger.warning("Failed to dispatch verified student report alert to admin %s: %s", admin_id, e)
    else:
        msg_text = f"✅ <b>{prefix}!</b> Thank you! 🎭"

    manage_kb = get_feedback_manage_keyboard(session_id, partner_id, feedback_type)
    await callback.message.edit_text(msg_text, reply_markup=manage_kb, parse_mode="HTML")
    await callback.answer(f"{prefix}!")


@router.callback_query(F.data.startswith("fb_edit:"))
async def handle_feedback_edit(callback: CallbackQuery) -> None:
    """Prompt user to change their rating for this partner."""
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer()
        return
    session_id = int(parts[1])
    partner_id = int(parts[2])
    user_id = callback.from_user.id

    async with get_db_session() as session:
        existing = await get_feedback_by_session_and_user(session, session_id, user_id)
        current_type = existing.feedback_type if existing else None

    await callback.message.edit_text(
        "✏️ <b>Change your rating for this partner:</b>\n\n"
        "<i>Select a new rating below:</i>",
        reply_markup=get_feedback_keyboard(session_id, partner_id, current_type=current_type),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("fb_done:"))
async def handle_feedback_done(callback: CallbackQuery) -> None:
    """Return to rating management view."""
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer()
        return
    session_id = int(parts[1])
    partner_id = int(parts[2])
    user_id = callback.from_user.id

    async with get_db_session() as session:
        existing = await get_feedback_by_session_and_user(session, session_id, user_id)
        current_type = existing.feedback_type if existing else "like"

    type_labels = {
        "like": "👍 Great Chat",
        "dislike": "👎 Not for me",
        "spam": "🚫 Spam / Promo",
        "not_aua": "🏫 Not from AUA",
    }
    label = type_labels.get(current_type, current_type)

    if current_type == "like":
        msg_text = (
            f"✅ <b>Current Rating: {label}</b>\n\n"
            "<b>Would you like to connect with this person again in future chats?</b>\n"
            "• <b>Open to Rematch:</b> Keep them in your matching pool.\n"
            "• <b>Close Chapter:</b> Take a break (1d, 3d, 1w) or close permanently so your paths don't cross awkwardly."
        )
    elif current_type == "dislike":
        msg_text = (
            f"✅ <b>Current Rating: {label}</b>\n\n"
            "<b>Would you like to connect with this person again in future chats?</b>\n"
            "• <b>Avoid Matching:</b> Take a break (1d, 3d, 1w) or block permanently so you don't connect again.\n"
            "• <b>Open to Rematch:</b> Keep them in your search pool despite this chat."
        )
    else:
        msg_text = (
            f"✅ <b>Current Rating: {label}</b>\n\n"
            "<i>Changed your mind or clicked by mistake? You can update or reset your rating below:</i>"
        )

    await callback.message.edit_text(
        msg_text,
        reply_markup=get_feedback_manage_keyboard(session_id, partner_id, current_type),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("fb_reset:"))
async def handle_feedback_reset(callback: CallbackQuery) -> None:
    """Reset / cancel previously submitted rating."""
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer()
        return
    session_id = int(parts[1])
    partner_id = int(parts[2])
    user_id = callback.from_user.id

    async with get_db_session() as session:
        success = await delete_or_reset_feedback(session, session_id, user_id)

    if success:
        await callback.message.edit_text(
            "🛑 <b>Rating Reset.</b>\n\n"
            "How was your conversation with this partner? Leave a rating below:",
            reply_markup=get_feedback_keyboard(session_id, partner_id),
            parse_mode="HTML",
        )
        await callback.answer("Rating reset! You can vote afresh.", show_alert=True)
    else:
        await callback.answer("No rating found to reset.", show_alert=True)


@router.callback_query(F.data.startswith("past_partner:"))
async def handle_past_partner_selected(callback: CallbackQuery) -> None:
    """Display actions for a selected past partner."""
    parts = callback.data.split(":")
    if len(parts) not in (3, 4):
        await callback.answer()
        return
    session_id = int(parts[1])
    partner_id = int(parts[2])
    page = int(parts[3]) if len(parts) == 4 else 1
    user_id = callback.from_user.id

    async with get_db_session() as session:
        existing = await get_feedback_by_session_and_user(session, session_id, user_id)
        current_type = existing.feedback_type if existing else None

    type_labels = {
        "like": "👍 Great Chat",
        "dislike": "👎 Not for me",
        "spam": "🚫 Spam / Promo",
        "not_aua": "🏫 Not from AUA",
    }
    status_text = f"\n• <b>Your current rating:</b> {type_labels.get(current_type, current_type)}" if current_type else ""

    await callback.message.edit_text(
        f"👤 <b>Selected Past Partner</b> (Chat #{session_id}){status_text}\n\n"
        "What would you like to do regarding this partner?\n\n"
        "• Request a reconnection if they also accept\n"
        "• Leave or update your rating\n"
        "• Report harassment, blackmail, or spam\n"
        "• Prevent connecting again with this partner",
        reply_markup=get_past_partner_action_keyboard(session_id, partner_id, page=page),
        parse_mode="HTML",
    )
    await callback.answer()


RECONNECT_REASON_LABELS = {
    "accident": '⚡ "Disconnected by accident!"',
    "continue": '💬 "Want to continue our convo!"',
    "forgot": '✨ "Forgot to tell you something!"',
    "none": None,
}


def _format_session_context(chat_session: ChatSession | None) -> tuple[str, str]:
    """Return human-readable (ended_ago_str, stats_str) for a past chat session."""
    if not chat_session:
        return "recently", "Past conversation"

    now = datetime.now(timezone.utc)
    t = chat_session.ended_at or chat_session.last_activity_at or chat_session.started_at
    if t and t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    if t:
        diff_sec = max(0, (now - t).total_seconds())
        if diff_sec < 60:
            ended_str = "just now"
        elif diff_sec < 3600:
            ended_str = f"{int(diff_sec // 60)}m ago"
        elif diff_sec < 86400:
            ended_str = f"{int(diff_sec // 3600)}h ago"
        else:
            ended_str = f"{int(diff_sec // 86400)}d ago"
    else:
        ended_str = "recently"

    msgs = getattr(chat_session, "messages_count", 0) or 0
    dur_sec = get_session_duration_seconds(chat_session)
    mins = max(1, int(round(dur_sec / 60))) if dur_sec >= 30 else 0
    dur_label = f"{mins} min{'s' if mins != 1 else ''}" if mins > 0 else "< 1 min"
    stats_str = f"{msgs} message{'s' if msgs != 1 else ''} · {dur_label}"
    return ended_str, stats_str


@router.callback_query(F.data.startswith("reconn_prompt:"))
async def handle_reconnect_prompt(callback: CallbackQuery) -> None:
    """Show 1-tap preset note options before sending a reconnection request."""
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer()
        return
    session_id = int(parts[1])
    partner_id = int(parts[2])
    user_id = callback.from_user.id

    if user_id == partner_id:
        await callback.answer("Cannot reconnect with yourself.", show_alert=True)
        return

    async with get_db_session() as session:
        recent_req = await get_recent_reconnect_request_between(
            session, sender_id=user_id, recipient_id=partner_id, cooldown_hours=24
        )
        if recent_req:
            await callback.answer(
                "⏳ You have already sent a reconnection request to this partner within the last 24 hours.",
                show_alert=True,
            )
            return

    await callback.message.edit_text(
        "🔄 <b>Request Reconnection</b> 🤝\n\n"
        "Would you like to attach a quick note so your past partner knows why you're reaching out?\n\n"
        "🔒 <i>Your identity remains 100% anonymous. You can send 1 request per partner every 24 hours.</i>",
        reply_markup=get_reconnect_reason_keyboard(session_id, partner_id),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("reconn_send:"))
async def handle_reconnect_send(callback: CallbackQuery, bot: Bot) -> None:
    """Send an anonymous reconnection request with spam & anti-retaliation safeguards."""
    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer()
        return
    session_id = int(parts[1])
    partner_id = int(parts[2])
    reason_tag = parts[3]
    user_id = callback.from_user.id

    if user_id == partner_id:
        await callback.answer("Invalid partner.", show_alert=True)
        return

    async with get_db_session() as session:
        recent_req = await get_recent_reconnect_request_between(
            session, sender_id=user_id, recipient_id=partner_id, cooldown_hours=24
        )
        if recent_req:
            await callback.answer(
                "⏳ You have already sent a reconnection request to this partner within the last 24 hours.",
                show_alert=True,
            )
            return

        chat_sess = await get_chat_session(session, session_id)
        if not chat_sess or user_id not in (chat_sess.user1_id, chat_sess.user2_id):
            await callback.answer("Could not locate that past chat session.", show_alert=True)
            return

        partner = await get_user(session, user_id=partner_id)
        partner_feedback = await get_feedback_by_session_and_user(session, session_id, partner_id)
        is_blocked_by_partner = await is_user_blocked_by_partner(
            session, partner_id=partner_id, user_id=user_id
        )
        session_reported = await has_report_for_session(session, session_id)

        # Silent drop if recipient blocked sender, reported the chat, rated as spam/not_aua, or is unavailable
        silent_drop = (
            partner is None
            or bool(partner.is_banned)
            or bool(getattr(partner, "is_bot_blocked", False))
            or is_blocked_by_partner
            or session_reported
            or (partner_feedback is not None and partner_feedback.feedback_type in ("spam", "not_aua"))
        )

        req = await create_reconnect_request(
            session=session,
            session_id=session_id,
            sender_id=user_id,
            recipient_id=partner_id,
            reason_tag=reason_tag,
            status="declined" if silent_drop else "pending",
        )
        ended_str, stats_str = _format_session_context(chat_sess)
        rating_map = {
            "like": "👍 Great Chat",
            "dislike": "👎 Not for me",
        }
        your_rating = (
            rating_map.get(partner_feedback.feedback_type, "No rating left")
            if partner_feedback
            else "No rating left"
        )

    note_preview = RECONNECT_REASON_LABELS.get(reason_tag)
    note_line = f"\n• <b>Note attached:</b> {note_preview}" if note_preview else ""

    await callback.message.edit_text(
        "📨 <b>Reconnection Request Sent!</b> 🔄\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Your anonymous request has been sent to your past partner.{note_line}\n\n"
        "🔔 <b>What happens next?</b>\n"
        "• If they accept while you are both free, you will be connected immediately.\n"
        "• If they accept while you are in another chat, we'll notify you with a button to connect when you finish.\n"
        "• Requests expire after 24 hours if unanswered.",
        parse_mode="HTML",
    )
    await callback.answer("Reconnection request sent!")

    if not silent_drop:
        note_section = f"• 💬 <b>Their note:</b> <i>{note_preview}</i>\n" if note_preview else ""
        incoming_text = (
            "🔄 <b>Anonymous Reconnection Request</b> 🎭\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "A recent chat partner would like to reconnect with you!\n\n"
            f"• 🕒 <b>Chat ended:</b> {ended_str}\n"
            f"• 💬 <b>Session:</b> {stats_str}\n"
            f"• ⭐ <b>Your rating:</b> {your_rating}\n"
            f"{note_section}\n"
            "<i>Would you like to start a new anonymous chat with this partner?</i>"
        )
        try:
            await bot.send_message(
                chat_id=partner_id,
                text=incoming_text,
                reply_markup=get_reconnect_incoming_keyboard(req.id),
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error("Failed to send reconnection request #%s to %s: %s", req.id, partner_id, e)


@router.callback_query(F.data.startswith("reconn_accept:"))
async def handle_reconnect_accept(callback: CallbackQuery, bot: Bot) -> None:
    """Handle recipient accepting a reconnection request."""
    parts = callback.data.split(":")
    if len(parts) != 2:
        await callback.answer()
        return
    req_id = int(parts[1])
    user_id = callback.from_user.id
    now = datetime.now(timezone.utc)

    async with get_db_session() as session:
        req = await get_reconnect_request_by_id(session, req_id)
        if not req or req.recipient_id != user_id:
            await callback.answer("Reconnection request not found.", show_alert=True)
            return

        if req.status == "accepted":
            await callback.answer("This reconnection request was already accepted.", show_alert=True)
            return
        if req.status not in ("pending", "accepted_waiting_sender"):
            await callback.answer("This reconnection request is no longer active.", show_alert=True)
            return

        exp = req.expires_at
        if exp and exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp and now > exp:
            req.status = "expired"
            await session.commit()
            await callback.message.edit_text(
                "⏳ <b>This reconnection request has expired (24h limit).</b>",
                parse_mode="HTML",
            )
            await callback.answer("Request expired.")
            return

        recipient = await get_user(session, user_id=req.recipient_id)
        sender = await get_user(session, user_id=req.sender_id)
        if not recipient or not sender or sender.is_banned:
            req.status = "expired"
            await session.commit()
            await callback.message.edit_text(
                "⚠️ <b>Partner is no longer available.</b>",
                parse_mode="HTML",
            )
            await callback.answer("Partner unavailable.")
            return

        # 1. If Recipient is currently in an active chat
        if recipient.current_partner_id is not None:
            if recipient.current_partner_id == sender.id:
                req.status = "accepted"
                await session.commit()
                await callback.message.edit_text(
                    "✅ <b>You are already connected with this partner!</b>",
                    parse_mode="HTML",
                )
                await callback.answer("Already connected!")
                return

            await callback.answer(
                "⚠️ You are currently in an active chat! Send /stop first, then tap Accept & Connect again.",
                show_alert=True,
            )
            return

        # Lift any block/chapter recipient had on sender since recipient explicitly accepted
        await session.execute(
            delete(UserBlock).where(
                UserBlock.user_id == recipient.id,
                UserBlock.blocked_user_id == sender.id,
            )
        )

        # 2. If Sender is currently in another active chat
        if sender.current_partner_id is not None:
            req.status = "accepted_waiting_sender"
            await session.commit()

            await callback.message.edit_text(
                "⏳ <b>Reconnection Accepted — Partner is Currently in a Chat</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "We've notified your past partner that you accepted their request!\n"
                "As soon as they finish their current conversation and tap <b>🔗 Connect Now</b>, your chat will begin automatically.",
                parse_mode="HTML",
            )
            await callback.answer("Accepted! Notified partner.")

            try:
                await bot.send_message(
                    chat_id=sender.id,
                    text=(
                        "🎉 <b>Your Reconnection Request Was Accepted!</b> 🔄\n"
                        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        "Your past partner accepted your request to reconnect, but you are currently in another active chat.\n\n"
                        "👉 Send /stop to finish your current chat, then tap <b>🔗 Connect Now</b> below!"
                    ),
                    reply_markup=get_reconnect_ready_keyboard(req.id),
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.error("Failed to notify sender %s of accepted reconnect #%s: %s", sender.id, req.id, e)
            return

        # 3. Both are free! Pair them immediately (pulling out of /search if searching)
        req.status = "accepted"
        new_session = ChatSession(
            user1_id=sender.id,
            user2_id=recipient.id,
            is_active=True,
        )
        session.add(new_session)
        sender.is_searching = False
        sender.current_partner_id = recipient.id
        recipient.is_searching = False
        recipient.current_partner_id = sender.id
        await session.commit()

    await reply_manager.clear_chat(sender.id, recipient.id)

    await callback.message.edit_text(
        "🎉 <b>Reconnected with Past Partner!</b> 🎭\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "You are now in a live, anonymous chat session with your past partner!\n"
        "Everything you send here will be relayed to them.\n\n"
        "⚡ <b>Commands:</b>\n"
        "• /next — Skip to a new partner\n"
        "• /stop — End this chat\n"
        "• /report — Emergency report & block\n\n"
        "🔒 <i>Privacy Shield Active: Identities strictly hidden.</i>",
        parse_mode="HTML",
    )
    await callback.answer("Connected!")

    try:
        await bot.send_message(
            chat_id=sender.id,
            text=(
                "🎉 <b>Reconnection Accepted — You Are Connected!</b> 🎭\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "Your past partner accepted your reconnection request! You are now in a live, anonymous chat session.\n"
                "Everything you send here will be relayed to your partner.\n\n"
                "⚡ <b>Commands:</b>\n"
                "• /next — Skip to a new partner\n"
                "• /stop — End this chat\n"
                "• /report — Emergency report & block\n\n"
                "🔒 <i>Privacy Shield Active: Identities strictly hidden.</i>"
            ),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error("Failed to notify sender %s on instant reconnect #%s: %s", sender.id, req.id, e)


@router.callback_query(F.data.startswith("reconn_connect_now:"))
async def handle_reconnect_connect_now(callback: CallbackQuery, bot: Bot) -> None:
    """Handle sender clicking 'Connect Now' after recipient accepted while sender was busy."""
    parts = callback.data.split(":")
    if len(parts) != 2:
        await callback.answer()
        return
    req_id = int(parts[1])
    user_id = callback.from_user.id
    now = datetime.now(timezone.utc)

    async with get_db_session() as session:
        req = await get_reconnect_request_by_id(session, req_id)
        if not req or req.sender_id != user_id:
            await callback.answer("Reconnection request not found.", show_alert=True)
            return

        if req.status == "accepted":
            await callback.answer("Already connected!", show_alert=True)
            return
        if req.status != "accepted_waiting_sender":
            await callback.answer("This reconnection request is no longer active.", show_alert=True)
            return

        exp = req.expires_at
        if exp and exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp and now > exp:
            req.status = "expired"
            await session.commit()
            await callback.message.edit_text(
                "⏳ <b>This reconnection request has expired.</b>",
                parse_mode="HTML",
            )
            await callback.answer("Request expired.")
            return

        sender = await get_user(session, user_id=req.sender_id)
        recipient = await get_user(session, user_id=req.recipient_id)
        if not sender or not recipient or recipient.is_banned:
            req.status = "expired"
            await session.commit()
            await callback.message.edit_text(
                "⚠️ <b>Partner is no longer available.</b>",
                parse_mode="HTML",
            )
            await callback.answer("Partner unavailable.")
            return

        if sender.current_partner_id is not None:
            if sender.current_partner_id == recipient.id:
                req.status = "accepted"
                await session.commit()
                await callback.message.edit_text(
                    "✅ <b>You are already connected with this partner!</b>",
                    parse_mode="HTML",
                )
                await callback.answer("Already connected!")
                return

            await callback.answer(
                "⚠️ You are still in an active chat! Send /stop first, then tap Connect Now.",
                show_alert=True,
            )
            return

        if recipient.current_partner_id is not None:
            await callback.answer(
                "⏳ Your partner is currently in another chat! Please try tapping Connect Now again in a few moments.",
                show_alert=True,
            )
            return

        # Both are free! Pair them now
        req.status = "accepted"
        new_session = ChatSession(
            user1_id=sender.id,
            user2_id=recipient.id,
            is_active=True,
        )
        session.add(new_session)
        sender.is_searching = False
        sender.current_partner_id = recipient.id
        recipient.is_searching = False
        recipient.current_partner_id = sender.id
        await session.commit()

    await reply_manager.clear_chat(sender.id, recipient.id)

    await callback.message.edit_text(
        "🎉 <b>Reconnected with Past Partner!</b> 🎭\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "You are now in a live, anonymous chat session with your past partner!\n"
        "Everything you send here will be relayed to them.\n\n"
        "⚡ <b>Commands:</b>\n"
        "• /next — Skip to a new partner\n"
        "• /stop — End this chat\n"
        "• /report — Emergency report & block\n\n"
        "🔒 <i>Privacy Shield Active: Identities strictly hidden.</i>",
        parse_mode="HTML",
    )
    await callback.answer("Connected!")

    try:
        await bot.send_message(
            chat_id=recipient.id,
            text=(
                "🎉 <b>Reconnected with Past Partner!</b> 🎭\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "Your past partner finished their previous chat and connected with you! You are now in a live, anonymous session.\n"
                "Everything you send here will be relayed to your partner.\n\n"
                "⚡ <b>Commands:</b>\n"
                "• /next — Skip to a new partner\n"
                "• /stop — End this chat\n"
                "• /report — Emergency report & block\n\n"
                "🔒 <i>Privacy Shield Active: Identities strictly hidden.</i>"
            ),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error("Failed to notify recipient %s on connect_now #%s: %s", recipient.id, req.id, e)


@router.callback_query(F.data.startswith("reconn_decline:"))
async def handle_reconnect_decline(callback: CallbackQuery) -> None:
    """Silently decline an incoming reconnection request without notifying sender."""
    parts = callback.data.split(":")
    if len(parts) != 2:
        await callback.answer()
        return
    req_id = int(parts[1])
    user_id = callback.from_user.id

    async with get_db_session() as session:
        req = await get_reconnect_request_by_id(session, req_id)
        if not req or req.recipient_id != user_id:
            await callback.answer("Request not found.", show_alert=True)
            return
        await update_reconnect_request_status(session, req.id, "declined")

    await callback.message.edit_text(
        "✅ <b>Reconnection request declined.</b>\n\n"
        "<i>Your past partner was not notified that you declined.</i>",
        parse_mode="HTML",
    )
    await callback.answer("Request declined.")


@router.callback_query(F.data.startswith("reconn_block:"))
async def handle_reconnect_block(callback: CallbackQuery) -> None:
    """Silently block a past partner from an incoming reconnection request."""
    parts = callback.data.split(":")
    if len(parts) != 2:
        await callback.answer()
        return
    req_id = int(parts[1])
    user_id = callback.from_user.id

    async with get_db_session() as session:
        req = await get_reconnect_request_by_id(session, req_id)
        if not req or req.recipient_id != user_id:
            await callback.answer("Request not found.", show_alert=True)
            return

        await add_or_update_user_block(
            session=session,
            user_id=user_id,
            blocked_user_id=req.sender_id,
            expires_at=None,
            reason="reconnect_block",
        )
        await update_reconnect_request_status(session, req.id, "blocked")

    await callback.message.edit_text(
        "🚫 <b>Partner Permanently Blocked.</b> 🛡️\n\n"
        "You will no longer match with this user or receive reconnection requests from them.\n"
        "<i>(They were not notified, and you can manage this anytime via /blocklist)</i>",
        parse_mode="HTML",
    )
    await callback.answer("Partner blocked.")


@router.callback_query(F.data.startswith("reconn_dismiss:"))
async def handle_reconnect_dismiss(callback: CallbackQuery) -> None:
    """Allow sender to dismiss a waiting reconnection notification."""
    parts = callback.data.split(":")
    if len(parts) != 2:
        await callback.answer()
        return
    req_id = int(parts[1])
    user_id = callback.from_user.id

    async with get_db_session() as session:
        req = await get_reconnect_request_by_id(session, req_id)
        if req and req.sender_id == user_id:
            await update_reconnect_request_status(session, req.id, "declined")

    await callback.message.edit_text(
        "✅ <b>Reconnection dismissed.</b>",
        parse_mode="HTML",
    )
    await callback.answer("Dismissed.")


@router.callback_query((F.data == "recent_list") | F.data.startswith("recent_page:"))
async def handle_recent_list_callback(callback: CallbackQuery) -> None:
    """Re-display recent partners list with pagination."""
    page = 1
    if callback.data and callback.data.startswith("recent_page:"):
        try:
            page = max(1, int(callback.data.split(":")[1]))
        except (ValueError, IndexError):
            page = 1

    user_id = callback.from_user.id
    page_size = 5
    async with get_db_session() as session:
        user = await get_user(session, user_id=user_id)
        partner_id = user.current_partner_id if user else None
        active_chat = await get_active_session_by_user_id(session, user_id=user_id) if partner_id else None
        total_recent = await count_recent_sessions_for_user(session, user_id=user_id)
        total_pages = max(1, math.ceil(total_recent / page_size))
        page = min(page, total_pages)
        offset = (page - 1) * page_size
        recent_sessions = await get_recent_sessions_for_user(
            session, user_id=user_id, limit=page_size, offset=offset
        )

    if not recent_sessions and not active_chat:
        await callback.message.edit_text(
            "💬 <b>No recent chat partners found.</b>\n\n"
            "Send /search to find an anonymous partner! 🥷",
            parse_mode="HTML",
        )
        await callback.answer()
        return

    kb = get_recent_partners_keyboard(
        sessions=recent_sessions,
        user_id=user_id,
        in_active_chat=bool(active_chat and partner_id),
        active_session_id=active_chat.id if active_chat else None,
        active_partner_id=partner_id,
        page=page,
        total_pages=total_pages,
        page_size=page_size,
    )
    await callback.message.edit_text(
        "📋 <b>Your Recent Chat Partners</b> 🥷\n\n"
        "<i>Select a partner below to reconnect, rate, report, or block:</i>",
        reply_markup=kb,
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "recent_noop")
async def handle_recent_noop_callback(callback: CallbackQuery) -> None:
    """Handle click on page indicator or disabled arrow in recent partners list."""
    await callback.answer()


@router.callback_query(F.data == "past_partner_close")
async def handle_past_partner_close(callback: CallbackQuery) -> None:
    """Close the recent partners menu."""
    await callback.message.delete()
    await callback.answer()


@router.callback_query(F.data.startswith("emerg_report_prompt:"))
async def handle_emerg_report_prompt_callback(callback: CallbackQuery) -> None:
    """Prompt emergency report for current active chat partner."""
    parts = callback.data.split(":")
    session_id = int(parts[1])
    partner_id = int(parts[2])

    await callback.message.edit_text(
        "🚨 <b>Emergency Safety & Report Shield</b> 🛡️\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Reporting your current active chat partner.\n\n"
        "🔒 <b>You are completely safe:</b>\n"
        "• Your partner <b>CANNOT</b> see your phone number, real name, @username, or profile picture.\n"
        "• Telegram does NOT reveal IP addresses.\n\n"
        "👉 <i>Would you like to immediately disconnect and block this partner while filing the report?</i>",
        reply_markup=get_emergency_report_keyboard(session_id, partner_id),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "suggest_dismiss")
async def handle_suggest_dismiss(callback: CallbackQuery) -> None:
    """Acknowledge dismissal of bot feedback prompt."""
    await callback.message.edit_text(
        "👍 <b>Got it!</b> You can always send us your thoughts or ideas anytime by typing /suggestion.",
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "suggest_start")
async def handle_suggest_start(callback: CallbackQuery, state: FSMContext) -> None:
    """Prompt user to write bot improvement suggestions."""
    await state.set_state(SuggestionState.waiting_for_suggestion)
    await callback.message.edit_text(
        "💡 <b>We'd love your ideas to improve Anonymous AUA!</b> 🎭\n\n"
        "What features would you love to see, what should be improved, or did you encounter any issues?\n\n"
        "✍️ <i>Please type your suggestion below and send it. Our administrative team will read it and reply back to you!</i>\n\n"
        "<i>(Send /cancel at any time to abort)</i>",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(SuggestionState.waiting_for_suggestion, F.text)
async def handle_suggestion_input(message: Message, bot: Bot, state: FSMContext) -> None:
    """Handle incoming suggestion from user, save to DB, and notify administrators."""
    if message.text and message.text.strip().lower() in ["/cancel", "/stop"]:
        await state.clear()
        await message.answer("✅ <b>Action cancelled.</b>", parse_mode="HTML")
        return

    # Prevent other bot commands from being captured as suggestion text
    if message.text.startswith("/"):
        return

    suggestion_text = message.text.strip()
    user_id = message.from_user.id
    username = message.from_user.username
    full_name = message.from_user.full_name

    await state.clear()

    async with get_db_session() as session:
        user = await get_or_create_user(session, user_id=user_id)
        if user.last_suggestion_date:
            now = datetime.now(timezone.utc)
            last_date = (
                user.last_suggestion_date
                if user.last_suggestion_date.tzinfo
                else user.last_suggestion_date.replace(tzinfo=timezone.utc)
            )
            elapsed = (now - last_date).total_seconds()
            cooldown_sec = settings.SUGGESTION_COOLDOWN_MINUTES * 60
            if elapsed < cooldown_sec:
                rem = int(cooldown_sec - elapsed)
                mins, secs = rem // 60, rem % 60
                time_str = f"{mins}m {secs}s" if mins > 0 else f"{secs}s"
                await message.answer(
                    f"⏳ <b>Suggestion Cooldown Active:</b>\n\n"
                    f"Thank you for sharing your thoughts! To prevent inbox spam, suggestions can be submitted once every {settings.SUGGESTION_COOLDOWN_MINUTES} minutes.\n\n"
                    f"Please wait <b>{time_str}</b> before submitting another suggestion.",
                    parse_mode="HTML",
                )
                return

        suggestion = await create_suggestion(session, user_id=user_id, text=suggestion_text)

    await message.answer(
        "🌟 <b>Thank you for your valuable feedback!</b> 🎭\n\n"
        "Your suggestion has been submitted directly to the AUA bot administration. "
        "When an admin reviews and replies to your message, you will receive their response right here!\n\n"
        "⚡ <b>Commands:</b>\n"
        "• /search — Find an anonymous partner\n"
        "• /suggestion — Submit another suggestion anytime",
        parse_mode="HTML",
    )

    # Forward suggestion to all administrators (escape user-controlled content)
    safe_full_name = html.escape(full_name or "AUA Student")
    safe_username = html.escape(username or "N/A")
    safe_suggestion = html.escape(suggestion_text)

    admin_text = (
        f"💡 <b>NEW STUDENT SUGGESTION #{suggestion.id}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Student ID:</b> <code>{user_id}</code>\n"
        f"• <b>Telegram:</b> @{safe_username} ({safe_full_name})\n\n"
        f"📝 <b>Suggestion / Feedback:</b>\n"
        f"<i>\"{safe_suggestion}\"</i>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )

    admin_keyboard = get_admin_suggestion_keyboard(suggestion.id, user_id)

    for admin_id in settings.ADMIN_IDS:
        try:
            await bot.send_message(
                chat_id=admin_id,
                text=admin_text,
                reply_markup=admin_keyboard,
                parse_mode="HTML",
            )
        except TelegramAPIError as e:
            logger.error("Failed to forward suggestion #%s to admin %s: %s", suggestion.id, admin_id, e)


@router.callback_query(F.data.startswith("report_start:"))
async def handle_report_start_callback(callback: CallbackQuery, state: FSMContext) -> None:
    """Initiate report submission flow by asking for the reason."""
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Invalid report data.", show_alert=True)
        return

    _, session_id_str, partner_id_str = parts
    session_id = int(session_id_str)
    reported_id = int(partner_id_str)

    async with get_db_session() as session:
        if await has_report_for_session(session, session_id):
            await callback.answer("A report has already been submitted for this chat session.", show_alert=True)
            return

    await state.set_state(ReportState.waiting_for_reason)
    await state.update_data(session_id=session_id, reported_id=reported_id)

    await callback.message.edit_text(
        "🚨 <b>Describe the Incident / Report Issue</b> 🛡️\n\n"
        "Please provide a brief text explanation of what occurred (e.g. harassment, doxxing, non-AUA intruder, or blackmail).\n\n"
        "🤖 <b>Zero-Knowledge Automated AI Review:</b>\n"
        "Our Gemini AI Safety Moderator evaluates the recent conversation context directly from ephemeral memory. "
        "<b>No screenshots or uploads needed!</b>\n\n"
        "<i>(Send /cancel to abort)</i>",
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("emerg_report:") | F.data.startswith("emerg_block_report:"))
async def handle_emergency_report_callback(callback: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    """Handle emergency in-chat report: disconnect active chat and prompt for explanation."""
    parts = callback.data.split(":")
    is_block = parts[0] == "emerg_block_report"
    session_id = int(parts[1])
    partner_id = int(parts[2])
    user_id = callback.from_user.id

    # 1. Terminate active chat immediately
    async with get_db_session() as session:
        await end_active_session(session, user_id=user_id)
        if is_block:
            await add_or_update_user_block(session, user_id=user_id, blocked_user_id=partner_id, expires_at=None)

    # Clear reply cache and one-time media session
    from app.handlers.matchmaking import reply_manager
    await reply_manager.clear_chat(user_id, partner_id)
    from app.services.one_time_media import one_time_media_manager
    one_time_media_manager.clear_session(session_id)

    # Notify partner they were disconnected
    try:
        await bot.send_message(
            chat_id=partner_id,
            text="🛑 <b>Your partner has disconnected or left the chat.</b>\n\nSend /search to find a new partner! ×͜×",
            parse_mode="HTML",
        )
    except Exception:
        pass

    # Arm FSM state for report description
    await state.set_state(ReportState.waiting_for_reason)
    await state.update_data(session_id=session_id, reported_id=partner_id)

    block_note = " and permanently blocked" if is_block else ""
    await callback.message.edit_text(
        f"🛑 <b>Chat disconnected{block_note}.</b> 🛡️\n\n"
        "You are now safe. Please provide a brief description of what happened:\n\n"
        "🤖 <i>Our AI Moderator evaluates the conversation directly from ephemeral memory. No screenshots needed!</i>\n\n"
        "<i>(Send /cancel to abort)</i>",
        parse_mode="HTML",
    )
    await callback.answer("Chat ended.")


@router.callback_query(F.data == "emerg_cancel")
async def handle_emergency_cancel(callback: CallbackQuery) -> None:
    """Cancel in-chat emergency report."""
    await callback.message.edit_text(
        "✅ <b>Report cancelled.</b> You are still connected in your chat.",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(
    ReportState.waiting_for_reason,
    F.text | F.photo | F.document | F.voice | F.video,
)
async def handle_report_reason(message: Message, bot: Bot, state: FSMContext) -> None:
    """Receive report reason, pop ephemeral buffer, evaluate with Gemini AI, and apply verdicts."""
    if message.text and message.text.strip().lower() in ["/cancel", "/stop"]:
        await state.clear()
        await message.answer("✅ Report cancelled.", parse_mode="HTML")
        return

    data = await state.get_data()
    session_id = data.get("session_id")
    reported_id = data.get("reported_id")
    reporter_id = message.from_user.id

    # Fallback to DB latest session if state data was lost across restart
    if not session_id or not reported_id:
        async with get_db_session() as session:
            latest = await get_latest_session_for_user(session, user_id=reporter_id)
            if latest:
                session_id = latest.id
                reported_id = latest.user2_id if latest.user1_id == reporter_id else latest.user1_id

    if not session_id or not reported_id:
        await state.clear()
        await message.answer(
            "⚠️ <b>Could not locate an associated chat session for this report.</b>\n\n"
            "Please send /suggestion to submit feedback directly to admins.",
            parse_mode="HTML",
        )
        return

    reason_text = message.text.strip() if message.text else (
        message.caption.strip() if message.caption else "Unspecified report reason"
    )

    await state.clear()

    async with get_db_session() as session:
        # Enforce one report per session
        if await has_report_for_session(session, session_id):
            await message.answer(
                "⚠️ <b>A report has already been submitted for this chat session.</b>\n\n"
                "Our AI moderation system evaluates each conversation once.",
                parse_mode="HTML",
            )
            return

        # Atomically pop turns from ProtectedSessionBuffer (clearing RAM)
        turns = session_buffer.pop_session(session_id)

        # Always ensure the partner is blocked for the reporter
        await add_or_update_user_block(
            session=session,
            user_id=reporter_id,
            blocked_user_id=reported_id,
            expires_at=None,
        )

        if not turns:
            # Buffer has expired (> 5 minutes TTL) or was already cleared
            await create_report(
                session=session,
                session_id=session_id,
                reporter_id=None,
                reported_id=None,
                reason_text=reason_text,
                guilty_party="Neither",
                severity_category="None",
                reported_user_penalty=0,
                reporter_penalty=0,
                reported_user_blocked=True,
                reporter_blocked=False,
                identity_revealed_for_illegal=False,
                anonymized_summary="Report submitted after 5-minute ephemeral memory TTL expired. User blocked.",
                is_resolved=True,
            )
            await message.answer(
                "⏳ <b>Report Window Expired (Zero-Knowledge Privacy Protection)</b>\n\n"
                "To guarantee student privacy, Anonymous AUA enforces a strict <b>5-Minute Ephemeral Buffer Policy</b>. "
                "Chat turns are never written to disk or database, and are permanently erased 5 minutes after a chat ends.\n\n"
                "Because more than 5 minutes have passed since this conversation concluded, the memory buffer has been destroyed.\n\n"
                "🛡️ <b>Action Taken:</b> This user has been permanently blocked so you will never match with them again.",
                parse_mode="HTML",
            )
            return

        # Status notification while AI evaluates
        status_msg = await message.answer(
            "🤖 <b>AI Safety Moderator Evaluating Conversation...</b>\n\n"
            "Analyzing conversation context against AUA Community & Privacy Rules. Please wait a moment...",
            parse_mode="HTML",
        )

        # Run Multimodal Gemini AI Evaluation
        verdict = await gemini_moderator.evaluate_session(
            turns=turns,
            reporter_id=reporter_id,
            reported_id=reported_id,
            reason_text=reason_text,
        )

        # Apply Penalties
        if verdict.reported_user_penalty > 0:
            await apply_admin_penalty(session, user_id=reported_id, penalty=verdict.reported_user_penalty)

        if verdict.reporter_penalty > 0:
            await apply_admin_penalty(session, user_id=reporter_id, penalty=verdict.reporter_penalty)

        # Apply Blocks
        if verdict.block_reported_user:
            await add_or_update_user_block(session, user_id=reporter_id, blocked_user_id=reported_id, expires_at=None)

        if verdict.block_reporter:
            await add_or_update_user_block(session, user_id=reported_id, blocked_user_id=reporter_id, expires_at=None)

        # Handle Tier 3 Extreme Illegal Violations
        if verdict.reveal_identity_for_illegal_content:
            if verdict.illegal_perpetrator in ("Reported_User", "Both"):
                await ban_user_by_admin(session, user_id=reported_id)
                add_banned_user(reported_id)
            if verdict.illegal_perpetrator in ("Reporter", "Both"):
                await ban_user_by_admin(session, user_id=reporter_id)
                add_banned_user(reporter_id)

            # Store unmasked IDs for illegal violation audit
            report = await create_report(
                session=session,
                session_id=session_id,
                reporter_id=reporter_id,
                reported_id=reported_id,
                reason_text=reason_text,
                guilty_party=verdict.guilty_party,
                severity_category=verdict.severity_category,
                reported_user_penalty=verdict.reported_user_penalty,
                reporter_penalty=verdict.reporter_penalty,
                reported_user_blocked=verdict.block_reported_user,
                reporter_blocked=verdict.block_reporter,
                identity_revealed_for_illegal=True,
                anonymized_summary=verdict.anonymized_summary,
                is_resolved=True,
            )

            # Alert Admins with Emergency Unmasking Alert
            reported_user = await get_user(session, user_id=reported_id)
            reporter_user = await get_user(session, user_id=reporter_id)
            reported_uname = html.escape(reported_user.username or "N/A") if reported_user else "N/A"
            reporter_uname = html.escape(reporter_user.username or "N/A") if reporter_user else "N/A"

            admin_alert = (
                "🚨🚨🚨 <b>CRITICAL EMERGENCY: TIER 3 ILLEGAL CONTENT DETECTED</b> 🚨🚨🚨\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"• <b>Report ID:</b> #{report.id}\n"
                f"• <b>Perpetrator:</b> {verdict.illegal_perpetrator}\n"
                f"• <b>Reported User ID:</b> <code>{reported_id}</code> (@{reported_uname})\n"
                f"• <b>Reporter ID:</b> <code>{reporter_id}</code> (@{reporter_uname})\n"
                f"• <b>Severity:</b> {verdict.severity_category}\n\n"
                f"📝 <b>AI Summary:</b>\n{html.escape(verdict.anonymized_summary)}\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"⚡ <i>Quick Command to Ban:</i> <code>/block {reported_id}</code>"
            )
            admin_ids = getattr(settings, "admin_ids", []) or getattr(settings, "ADMIN_IDS", [])
            for admin_id in admin_ids:
                try:
                    await bot.send_message(chat_id=admin_id, text=admin_alert, parse_mode="HTML")
                except Exception as e:
                    logger.error("Failed to deliver emergency illegal alert to admin %s: %s", admin_id, e)
        else:
            # Privacy-Preserved Report (Zero-Knowledge: IDs nullified!)
            await create_report(
                session=session,
                session_id=session_id,
                reporter_id=None,
                reported_id=None,
                reason_text=reason_text,
                guilty_party=verdict.guilty_party,
                severity_category=verdict.severity_category,
                reported_user_penalty=verdict.reported_user_penalty,
                reporter_penalty=verdict.reporter_penalty,
                reported_user_blocked=verdict.block_reported_user,
                reporter_blocked=verdict.block_reporter,
                identity_revealed_for_illegal=False,
                anonymized_summary=verdict.anonymized_summary,
                is_resolved=True,
            )

    # Deliver verdict response to Reporter
    safe_summary = html.escape(verdict.anonymized_summary)
    category_label = verdict.severity_category.replace("_", " ")

    if verdict.guilty_party == "Reported_User":
        response_text = (
            "✅ <b>AI Safety Evaluation Complete</b> 🛡️\n\n"
            f"<b>Verdict:</b> Violation Confirmed (<code>{category_label}</code>)\n"
            f"<b>Action Taken:</b> -{verdict.reported_user_penalty} priority penalty applied to partner.\n"
            f"• Partner has been permanently blocked.\n\n"
            f"📝 <b>AI Findings:</b>\n<i>{safe_summary}</i>\n\n"
            "Thank you for keeping Anonymous AUA safe and respectful for all students! 🎓"
        )
    elif verdict.guilty_party == "Reporter":
        response_text = (
            "⚠️ <b>AI Safety Evaluation Complete</b> 🛡️\n\n"
            f"<b>Notice:</b> Under Rule C (Reporter Accountability), the AI Moderator determined that "
            f"your report was improper, baited, or in violation of community standards.\n\n"
            f"<b>Action Taken:</b> -{verdict.reporter_penalty} priority penalty applied to your account.\n\n"
            f"📝 <b>AI Findings:</b>\n<i>{safe_summary}</i>\n\n"
            "Please review /safety and /privacy to maintain good standing."
        )
    elif verdict.guilty_party == "Both":
        response_text = (
            "⚠️ <b>AI Safety Evaluation Complete</b> 🛡️\n\n"
            "<b>Verdict:</b> Mutual Misconduct Detected.\n"
            f"<b>Action Taken:</b> -{verdict.reporter_penalty} penalty applied to your account, "
            f"and -{verdict.reported_user_penalty} applied to partner.\n\n"
            f"📝 <b>AI Findings:</b>\n<i>{safe_summary}</i>"
        )
    else:
        response_text = (
            "ℹ️ <b>AI Safety Evaluation Complete</b> 🛡️\n\n"
            "<b>Verdict:</b> No policy violations detected.\n"
            "The conversation appeared normal and respectful under Community Rules.\n\n"
            "🛡️ <b>Action Taken:</b> The user has been added to your personal blocklist.\n\n"
            f"📝 <b>AI Findings:</b>\n<i>{safe_summary}</i>"
        )

    try:
        await status_msg.edit_text(response_text, parse_mode="HTML")
    except Exception:
        await message.answer(response_text, parse_mode="HTML")

    # If reported user was penalized (and not banned), notify them anonymously
    if verdict.reported_user_penalty > 0 and not verdict.reveal_identity_for_illegal_content:
        try:
            await bot.send_message(
                chat_id=reported_id,
                text=(
                    "⚠️ <b>Community Safety Notice</b>\n\n"
                    f"A recent chat partner submitted a report. After automated AI moderation review, "
                    f"a penalty of <b>-{verdict.reported_user_penalty} priority points</b> was applied to your profile "
                    f"for <b>{category_label}</b>.\n\n"
                    "Please review /safety and /privacy guidelines to maintain high matching priority."
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass

