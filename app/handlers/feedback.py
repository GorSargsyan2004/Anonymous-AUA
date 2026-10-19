import html
import logging
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.config import settings
from app.database.crud import (
    create_report,
    create_suggestion,
    get_user,
    record_feedback,
    update_user_priority,
)
from app.database.session import get_db_session
from app.keyboards.inline import (
    get_admin_report_keyboard,
    get_admin_suggestion_keyboard,
)
from app.states.states import ReportState, SuggestionState

logger = logging.getLogger(__name__)
router = Router(name="feedback_router")

FEEDBACK_DELTAS = {
    "like": 1,
    "dislike": -1,
    "spam": -5,
    "not_aua": -5,
}

FEEDBACK_LABELS = {
    "like": "👍 Liked",
    "dislike": "👎 Disliked",
    "spam": "🚫 Reported as Spam",
    "not_aua": "🏫 Reported as Not from AUA",
}


@router.callback_query(F.data.startswith("fb:"))
async def handle_feedback_callback(callback: CallbackQuery) -> None:
    """Process thumbs up/down, spam, or not-AUA feedback."""
    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer("Invalid feedback data.", show_alert=True)
        return

    _, session_id_str, partner_id_str, feedback_type = parts
    session_id = int(session_id_str)
    partner_id = int(partner_id_str)
    from_user_id = callback.from_user.id

    if feedback_type not in FEEDBACK_DELTAS:
        await callback.answer("Unknown feedback type.", show_alert=True)
        return

    delta = FEEDBACK_DELTAS[feedback_type]
    label = FEEDBACK_LABELS[feedback_type]

    async with get_db_session() as session:
        # Check uniqueness and record
        _, created = await record_feedback(
            session=session,
            session_id=session_id,
            from_user_id=from_user_id,
            to_user_id=partner_id,
            feedback_type=feedback_type,
        )

        if not created:
            await callback.answer(
                "You have already submitted feedback for this session.",
                show_alert=True,
            )
            return

        # Apply priority change invisibly behind the scenes
        await update_user_priority(session, user_id=partner_id, delta=delta)

    # Edit message to confirm rating without exposing priority values
    await callback.message.edit_text(
        f"✅ <b>Feedback submitted: {label}!</b>\n\n"
        "Thank you for helping keep the AUA community safe, positive, and authentic. 🦅🇦🇲\n\n"
        "<i>Send /search whenever you want to start a new chat!</i>",
        parse_mode="HTML",
    )
    await callback.answer("Feedback recorded!")


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
    safe_reason = html.escape(reason_text)

    admin_text = (
        f"🚨 <b>NEW USER REPORT #{report.id}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Session ID:</b> #{session_id}\n"
        f"• <b>Reporter:</b> <code>{reporter_id}</code> (@{safe_reporter_username})\n"
        f"• <b>Reported User:</b> <code>{reported_id}</code>\n"
        f"• <b>Current Priority:</b> {current_priority}/100\n\n"
        f"📝 <b>Reason:</b>\n{safe_reason}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
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
