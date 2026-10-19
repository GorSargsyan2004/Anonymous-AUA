"""Admin-only handlers for statistics dashboard, report reviews, and suggestion replies."""

import html
import logging
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.database.crud import (
    apply_admin_penalty,
    dismiss_report,
    get_admin_stats,
    reply_to_suggestion,
    resolve_report,
)
from app.database.session import get_db_session
from app.filters.admin import IsAdminFilter
from app.states.states import AdminPenalizeState, AdminReplyState

logger = logging.getLogger(__name__)
router = Router(name="admin_router")

# Apply IsAdminFilter to all events in this router
router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminFilter())


@router.message(Command("stata"))
async def handle_stata(message: Message) -> None:
    """Display real-time aggregated metrics for Anonymous AUA Bot."""
    async with get_db_session() as session:
        stats = await get_admin_stats(session)

    dashboard = (
        "📊 <b>AUA Anonymous Chat — Administrator Dashboard</b> 🦅🇦🇲\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👥 <b>User Base:</b>\n"
        f"• Total Registered: <b>{stats['total_users']}</b>\n"
        f"• Actively Searching: <b>{stats['searching_users']}</b>\n"
        f"• Active Chats: <b>{stats['active_chats']}</b>\n\n"
        f"🚻 <b>Sex Breakdown:</b>\n"
        f"• Male: <b>{stats['male_count']}</b>\n"
        f"• Female: <b>{stats['female_count']}</b>\n"
        f"• Unfinished Setup: <b>{stats['unset_sex_count']}</b>\n\n"
        f"🧭 <b>Orientation Breakdown:</b>\n"
        f"• Straight: <b>{stats['straight_count']}</b>\n"
        f"• Bisexual: <b>{stats.get('bisexual_count', 0)}</b>\n"
        f"• Lesbian: <b>{stats['lesbian_count']}</b>\n"
        f"• Gay: <b>{stats['gay_count']}</b>\n\n"
        f"⭐ <b>All-Time Feedbacks:</b>\n"
        f"• 👍 Likes: <b>{stats['likes_count']}</b>\n"
        f"• 👎 Dislikes: <b>{stats['dislikes_count']}</b>\n"
        f"• 🚫 Spams: <b>{stats['spam_count']}</b>\n"
        f"• 🏫 Not from AUA: <b>{stats['not_aua_count']}</b>\n\n"
        f"💡 <b>Bot Suggestions:</b>\n"
        f"• Total Received: <b>{stats.get('total_suggestions', 0)}</b>\n"
        f"• ✅ Replied: <b>{stats.get('replied_suggestions', 0)}</b>\n"
        f"• ⏳ Pending: <b>{stats.get('pending_suggestions', 0)}</b>\n\n"
        f"🚨 <b>Reports Summary:</b>\n"
        f"• Total Filed: <b>{stats['total_reports']}</b>\n"
        f"• ✅ Resolved: <b>{stats['resolved_reports']}</b>\n"
        f"• ⏳ Pending: <b>{stats['pending_reports']}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )

    await message.answer(dashboard, parse_mode="HTML")


@router.callback_query(F.data.startswith("admin_dismiss:"))
async def handle_admin_dismiss(callback: CallbackQuery) -> None:
    """Dismiss a user report without applying any penalty."""
    report_id = int(callback.data.split(":")[1])

    async with get_db_session() as session:
        report = await dismiss_report(session, report_id=report_id)

    if not report:
        await callback.answer("Report not found or already deleted.", show_alert=True)
        return

    admin_tag = f"@{callback.from_user.username}" if callback.from_user.username else f"ID:{callback.from_user.id}"
    await callback.message.edit_text(
        f"{callback.message.text}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ <b>Dismissed by {admin_tag} (0 penalty applied).</b>",
        parse_mode="HTML",
    )
    await callback.answer("Report dismissed.")


@router.callback_query(F.data.startswith("admin_penalize:"))
async def handle_admin_penalize_start(callback: CallbackQuery, state: FSMContext) -> None:
    """Prompt administrator for penalty deduction points."""
    parts = callback.data.split(":")
    report_id = int(parts[1])
    reported_id = int(parts[2])

    await state.set_state(AdminPenalizeState.waiting_for_penalty_amount)
    await state.update_data(
        report_id=report_id,
        reported_id=reported_id,
        message_id=callback.message.message_id,
        chat_id=callback.message.chat.id,
    )

    await callback.message.reply(
        f"⚖️ <b>Penalizing User <code>{reported_id}</code></b> (Report #{report_id})\n\n"
        "Enter the number of priority points to deduct from this user (e.g., 15):",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminPenalizeState.waiting_for_penalty_amount, F.text)
async def handle_admin_penalty_amount(message: Message, state: FSMContext) -> None:
    """Process administrator penalty input and update user priority and report."""
    raw_input = message.text.strip()
    if not raw_input.isdigit() or int(raw_input) <= 0:
        await message.reply(
            "⚠️ Please enter a valid positive integer (e.g. 10, 20):"
        )
        return

    penalty = int(raw_input)
    data = await state.get_data()
    report_id = data.get("report_id")
    reported_id = data.get("reported_id")

    await state.clear()

    async with get_db_session() as session:
        # Subtract penalty points (clamped to min 0)
        new_priority = await apply_admin_penalty(session, user_id=reported_id, penalty=penalty)
        # Mark report as resolved
        await resolve_report(session, report_id=report_id, penalty_applied=penalty)

    admin_tag = f"@{message.from_user.username}" if message.from_user.username else f"ID:{message.from_user.id}"

    await message.reply(
        f"✅ <b>Penalty applied successfully by {admin_tag}!</b>\n\n"
        f"• <b>Report ID:</b> #{report_id}\n"
        f"• <b>User ID:</b> <code>{reported_id}</code>\n"
        f"• <b>Points Deducted:</b> -{penalty}\n"
        f"• <b>New Priority:</b> <b>{new_priority}/100</b>",
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("admin_reply_sug:"))
async def handle_admin_reply_sug_start(callback: CallbackQuery, state: FSMContext) -> None:
    """Prompt administrator for text to reply to a student suggestion."""
    parts = callback.data.split(":")
    suggestion_id = int(parts[1])
    target_user_id = int(parts[2])

    await state.set_state(AdminReplyState.waiting_for_reply_text)
    await state.update_data(
        suggestion_id=suggestion_id,
        target_user_id=target_user_id,
    )

    await callback.message.reply(
        f"✍️ <b>Replying to Student <code>{target_user_id}</code></b> (Suggestion #{suggestion_id})\n\n"
        "Please type your response message below. It will be delivered directly to the student in Telegram:\n\n"
        "<i>(Send /cancel or /stop to abort)</i>",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminReplyState.waiting_for_reply_text, F.text)
async def handle_admin_reply_text(message: Message, bot: Bot, state: FSMContext) -> None:
    """Deliver admin's reply message to the student and record in DB."""
    if message.text.startswith("/"):
        await state.clear()
        await message.reply("Action cancelled.")
        return

    reply_text = message.text.strip()
    data = await state.get_data()
    suggestion_id = data.get("suggestion_id")
    target_user_id = data.get("target_user_id")

    await state.clear()

    async with get_db_session() as session:
        await reply_to_suggestion(session, suggestion_id=suggestion_id, reply_text=reply_text)

    # Deliver to student (escape html formatting)
    safe_reply = html.escape(reply_text)
    student_msg = (
        "📬 <b>Message from AUA Bot Administration:</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>\"{safe_reply}\"</i>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Thank you for helping us improve Anonymous AUA! 🦅🇦🇲"
    )

    try:
        await bot.send_message(chat_id=target_user_id, text=student_msg, parse_mode="HTML")
        await message.reply(
            f"✅ <b>Reply successfully sent to student <code>{target_user_id}</code>!</b>\n\n"
            f"• <b>Suggestion ID:</b> #{suggestion_id}\n"
            f"• <b>Content:</b>\n<i>\"{safe_reply}\"</i>",
            parse_mode="HTML",
        )
    except TelegramAPIError as e:
        logger.error("Failed to deliver admin reply to user %s: %s", target_user_id, e)
        await message.reply(
            f"⚠️ Could not deliver message to user <code>{target_user_id}</code> (they may have blocked the bot):\n{e}",
            parse_mode="HTML",
        )
