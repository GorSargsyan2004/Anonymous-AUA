from datetime import datetime, timedelta, timezone
import html
import logging
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.config import settings
from app.database.crud import (
    add_or_update_user_block,
    create_report,
    create_suggestion,
    get_user,
    process_feedback_with_consensus,
    record_feedback,
    update_user_priority,
)
from app.database.session import get_db_session
from app.keyboards.inline import (
    get_admin_report_keyboard,
    get_admin_suggestion_keyboard,
    get_block_duration_keyboard,
    get_feedback_keyboard,
    get_report_options_keyboard,
)
from app.states.states import ReportState, SuggestionState

logger = logging.getLogger(__name__)
router = Router(name="feedback_router")


@router.callback_query(F.data.startswith("report_menu:"))
async def handle_report_menu(callback: CallbackQuery) -> None:
    """Show specific issue options when user clicks 'Report Issue...'."""
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer()
        return
    session_id = int(parts[1])
    partner_id = int(parts[2])

    await callback.message.edit_text(
        "⚠️ <b>Report an Issue</b>\n\n"
        "What problem did you encounter with this partner?\n\n"
        "<i>Select a category below:</i>",
        reply_markup=get_report_options_keyboard(session_id, partner_id),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("fb_back:"))
async def handle_feedback_back(callback: CallbackQuery) -> None:
    """Return to Tier 1 post-chat rating keyboard."""
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
    if len(parts) != 3:
        await callback.answer()
        return
    session_id = int(parts[1])
    partner_id = int(parts[2])

    await callback.message.edit_text(
        "🚫 <b>Do Not Connect Again</b>\n\n"
        "How long would you like to avoid connecting with this partner?\n\n"
        "<i>Select a cooldown duration:</i>",
        reply_markup=get_block_duration_keyboard(session_id, partner_id),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("block_dur:"))
async def handle_block_duration(callback: CallbackQuery) -> None:
    """Save user-to-partner block/cooldown and confirm to user."""
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
    elif dur_key == "3d":
        expires_at = now + timedelta(days=3)
        dur_text = "3 days"
    elif dur_key == "7d":
        expires_at = now + timedelta(days=7)
        dur_text = "1 week"
    else:
        expires_at = None
        dur_text = "permanently"

    async with get_db_session() as session:
        await add_or_update_user_block(
            session=session,
            user_id=user_id,
            blocked_user_id=partner_id,
            expires_at=expires_at,
        )

    await callback.message.edit_text(
        f"🛑 <b>Partner Blocked!</b>\n\n"
        f"You will not connect with this anonymous partner for <b>{dur_text}</b>.\n\n"
        "💡 <i>You can view, manage, or unblock partners anytime using /blocklist.</i>\n\n"
        "Send /search to find a new chat partner! 🦅🇦🇲",
        parse_mode="HTML",
    )
    await callback.answer("Partner blocked!")


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
        result = await process_feedback_with_consensus(
            session=session,
            session_id=session_id,
            from_user_id=from_user_id,
            partner_id=partner_id,
            feedback_type=feedback_type,
        )

    if not result["created"]:
        await callback.answer(
            "You have already submitted feedback for this session.",
            show_alert=True,
        )
        return

    # User-facing confirmation message
    if feedback_type == "like":
        msg_text = (
            "✅ <b>Feedback submitted: 👍 Great Chat!</b>\n\n"
            "Thank you for helping keep the AUA community positive, friendly, and authentic! 🦅🇦🇲\n\n"
            "<i>Send /search whenever you want to start a new chat!</i>"
        )
    elif feedback_type == "dislike":
        if result["is_short_chat"]:
            msg_text = (
                "✅ <b>Feedback submitted: 👎 Not for me.</b>\n\n"
                "<i>(Since the conversation was very brief, this was recorded as a neutral skip).</i>\n\n"
                "Thank you for your feedback! Send /search whenever you want to start a new chat! 🦅"
            )
        else:
            msg_text = (
                "✅ <b>Feedback submitted: 👎 Not for me.</b>\n\n"
                "Thank you for your feedback. We'll use this to match you with better partners!\n\n"
                "<i>Send /search whenever you want to start a new chat!</i> 🦅🇦🇲"
            )
    elif feedback_type == "spam":
        msg_text = (
            "✅ <b>Report recorded: 🚫 Spam / Promotion.</b>\n\n"
            "Thank you for keeping AUA safe and clean. Our automated system tracks repeated reports "
            "across independent students and penalizes offenders. 🦅🇦🇲\n\n"
            "<i>Send /search to find a new partner!</i>"
        )
    elif feedback_type == "not_aua":
        msg_text = (
            "✅ <b>Report recorded: 🏫 Not from AUA.</b>\n\n"
            "Thank you! Our automated system tracks reports from multiple students to identify "
            "and filter out non-AUA intruders. 🦅🇦🇲\n\n"
            "<i>Send /search to find a new partner!</i>"
        )
    else:
        msg_text = "✅ <b>Feedback recorded!</b> Thank you! 🦅🇦🇲"

    await callback.message.edit_text(msg_text, parse_mode="HTML")
    await callback.answer("Feedback recorded!")

    # If consensus was reached (e.g. 2+ independent flags), notify admins
    if result.get("consensus_reached"):
        partner = result.get("partner")
        safe_p_username = html.escape(partner.username or "N/A") if partner else "N/A"
        admin_alert_text = (
            "🚨 <b>AUTOMATED CONSENSUS WARNING: Repeated Flags!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"• <b>Flagged User ID:</b> <code>{partner_id}</code> (@{safe_p_username})\n"
            f"• <b>Violation Type:</b> {feedback_type.upper()}\n"
            f"• <b>Independent Flags:</b> {result['spam_flags_count']} students\n"
            f"• <b>Penalty Applied:</b> -5 priority points (Current: {result['new_priority']}/100)\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "<i>Multiple independent students have reported this account. Review via /stata.</i>"
        )
        for admin_id in settings.ADMIN_IDS:
            try:
                await bot.send_message(chat_id=admin_id, text=admin_alert_text, parse_mode="HTML")
            except TelegramAPIError as e:
                logger.debug("Failed to send consensus alert to admin %s: %s", admin_id, e)


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
        "💡 <b>We'd love your ideas to improve Anonymous AUA!</b> 🦅🇦🇲\n\n"
        "What features would you love to see, what should be improved, or did you encounter any issues?\n\n"
        "✍️ <i>Please type your suggestion below and send it. Our administrative team will read it and reply back to you!</i>\n\n"
        "<i>(Send /stop at any time to cancel)</i>",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(SuggestionState.waiting_for_suggestion, F.text)
async def handle_suggestion_input(message: Message, bot: Bot, state: FSMContext) -> None:
    """Handle incoming suggestion from user, save to DB, and notify administrators."""
    # Prevent bot commands from being captured as suggestion text
    if message.text.startswith("/"):
        return

    suggestion_text = message.text.strip()
    user_id = message.from_user.id
    username = message.from_user.username
    full_name = message.from_user.full_name

    await state.clear()

    async with get_db_session() as session:
        suggestion = await create_suggestion(session, user_id=user_id, text=suggestion_text)

    await message.answer(
        "🌟 <b>Thank you for your valuable feedback!</b> 🦅🇦🇲\n\n"
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

    await state.set_state(ReportState.waiting_for_reason)
    await state.update_data(session_id=session_id, reported_id=reported_id)

    await callback.message.edit_text(
        "⚠️ <b>Reporting User</b>\n\n"
        "Please describe why you are reporting this user "
        "(e.g., harassment, inappropriate content, scam, non-AUA):\n\n"
        "<i>Send your explanation as a message below, or /stop to cancel.</i>",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(ReportState.waiting_for_reason, F.text)
async def handle_report_reason(message: Message, bot: Bot, state: FSMContext) -> None:
    """Receive the report reason, record in DB, and notify all administrators."""
    if message.text.startswith("/"):
        return

    data = await state.get_data()
    session_id = data.get("session_id")
    reported_id = data.get("reported_id")
    reporter_id = message.from_user.id
    reason_text = message.text.strip()

    await state.clear()

    async with get_db_session() as session:
        # Create Report
        report = await create_report(
            session=session,
            session_id=session_id,
            reporter_id=reporter_id,
            reported_id=reported_id,
            reason_text=reason_text,
        )
        reported_user = await get_user(session, user_id=reported_id)
        current_priority = reported_user.priority if reported_user else 50

    await message.answer(
        "✅ <b>Your report has been submitted to AUA moderators.</b>\n\n"
        "Thank you for helping keep our university community safe and respectful. 🦅🇦🇲\n\n"
        "Send /search whenever you want to find a new chat partner.",
        parse_mode="HTML",
    )

    # Notify all administrators (priority is visible only to admins, escape user text)
    safe_reporter_username = html.escape(message.from_user.username or "N/A")
    safe_reported_username = html.escape(reported_user.username or "N/A") if reported_user else "N/A"
    safe_reason = html.escape(reason_text)

    admin_text = (
        f"🚨 <b>NEW USER REPORT #{report.id}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Session ID:</b> #{session_id}\n"
        f"• <b>Reported User ID:</b> <code>{reported_id}</code> (@{safe_reported_username})\n"
        f"• <b>Reporter ID:</b> <code>{reporter_id}</code> (@{safe_reporter_username})\n"
        f"• <b>Current Priority:</b> {current_priority}/100\n\n"
        f"📝 <b>Report Reason:</b>\n{safe_reason}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚡ <i>Quick Command to Ban:</i> <code>/block {reported_id}</code>"
    )

    admin_keyboard = get_admin_report_keyboard(report.id, reported_id)

    for admin_id in settings.ADMIN_IDS:
        try:
            await bot.send_message(
                chat_id=admin_id,
                text=admin_text,
                reply_markup=admin_keyboard,
                parse_mode="HTML",
            )
        except TelegramAPIError as e:
            logger.error("Failed to notify admin %s of report #%s: %s", admin_id, report.id, e)
