"""Admin-only handlers for statistics dashboard, report reviews, and suggestion replies."""

import html
import logging
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from sqlalchemy import select

from app.database.crud import (
    apply_admin_penalty,
    ban_user_by_admin,
    dismiss_report,
    get_admin_stats,
    get_pending_reports,
    reply_to_suggestion,
    resolve_report,
    unban_user_by_admin,
)
from app.database.models import User
from app.database.session import get_db_session
from app.filters.admin import IsAdminFilter
from app.keyboards.inline import get_admin_report_keyboard
from app.services.backup import create_database_backup, get_backup_metadata
from app.services.ban_manager import add_banned_user, remove_banned_user
from app.services.reply_manager import reply_manager
from app.states.states import AdminPenalizeState, AdminReplyState


logger = logging.getLogger(__name__)
router = Router(name="admin_router")

# Apply IsAdminFilter to all events in this router
router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminFilter())


@router.message(Command("stata", "stats"))
async def handle_stata(message: Message) -> None:
    """Display real-time aggregated metrics and backup status for Anonymous AUA Bot."""
    async with get_db_session() as session:
        stats = await get_admin_stats(session)

    backup_info = get_backup_metadata()
    if backup_info["exists"]:
        latest_kb = backup_info["latest_size_bytes"] / 1024
        backup_str = (
            f"✅ Active ({backup_info['latest_modified']})\n"
            f"• Primary File: <code>backups/latest.sql.gz</code> ({latest_kb:.1f} KB)\n"
            f"• Rolling Snapshots: <b>{backup_info['total_snapshots']}</b>"
        )
    else:
        backup_str = "⚠️ No backup snapshot found yet (run /backup)"

    appeal_note = (
        f" <i>({stats['gender_appeal_count']} appeal pending)</i>"
        if stats.get("gender_appeal_count", 0) > 0
        else ""
    )

    dashboard = (
        "📊 <b>Anonymous AUA — Administrator Dashboard</b> 🎭\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👥 <b>User Base & Reach:</b>\n"
        f"• Total Registered: <b>{stats['total_users']}</b>\n"
        f"• 💬 Active Bot Chats: <b>{stats['active_chat_users']}</b> <i>(have not blocked/deleted bot)</i>\n"
        f"• 🚫 Blocked / Left Bot: <b>{stats['blocked_bot_users']}</b>\n"
        f"• 🔨 Banned Accounts: <b>{stats['banned_count']}</b>\n"
        f"• 🔍 Actively Searching: <b>{stats['searching_users']}</b>\n"
        f"• ⚡ Active Match Sessions: <b>{stats['active_chats']}</b>\n\n"
        f"🎓 <b>AUA Verification:</b>\n"
        f"• Total Verified: <b>{stats['total_verified']}</b>\n"
        f"  ├ 🎓 Active Students: <b>{stats['verified_students']}</b>\n"
        f"  ├ 🎓🏛️ Alumni / Graduates: <b>{stats['verified_alumni']}</b>\n"
        f"  ├ ⚠️ Expired (Sep 1): <b>{stats['verified_expired']}</b>\n"
        f"  └ 🔒 Gender Confirmed: <b>{stats['gender_verified']}</b>\n"
        f"• ❌ Unverified Users: <b>{stats['unverified_count']}</b>\n"
        f"• ⚠️ Gender Conflicts: <b>{stats['gender_mismatch_count']}</b>{appeal_note}\n\n"
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
        f"• ⏳ Pending: <b>{stats['pending_reports']}</b>"
        + (" <i>(Review with /reports)</i>" if stats.get("pending_reports", 0) > 0 else "")
        + "\n\n"
        f"💾 <b>Database Backups:</b>\n"
        f"{backup_str}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "💡 <i>Tip: Use /reports to review pending reports, /backup for snapshots.</i>"
    )

    await message.answer(dashboard, parse_mode="HTML")


@router.message(Command("reports", "pending_reports"))
async def handle_pending_reports(message: Message, bot: Bot) -> None:
    """Display and manage all pending/unresolved user reports."""
    async with get_db_session() as session:
        reports = await get_pending_reports(session)
        if not reports:
            await message.reply("✅ <b>No pending reports found!</b> All reports are resolved.", parse_mode="HTML")
            return

        user_ids = set()
        for r in reports:
            user_ids.add(r.reporter_id)
            user_ids.add(r.reported_id)

        user_map = {}
        if user_ids:
            u_stmt = select(User).where(User.id.in_(user_ids))
            u_res = await session.execute(u_stmt)
            for u in u_res.scalars().all():
                user_map[u.id] = u

    await message.reply(
        f"🚨 <b>Found {len(reports)} pending report(s):</b>\n"
        "Reviewing each report below with available evidence and moderation actions:",
        parse_mode="HTML",
    )

    for report in reports:
        reporter = user_map.get(report.reporter_id)
        reported = user_map.get(report.reported_id)

        reporter_tag = f"@{html.escape(reporter.username)}" if reporter and reporter.username else "N/A"
        reported_tag = f"@{html.escape(reported.username)}" if reported and reported.username else "N/A"
        date_str = report.created_at.strftime("%Y-%m-%d %H:%M UTC") if report.created_at else "N/A"

        report_text = (
            f"🚨 <b>Pending Report #{report.id}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"• <b>Session ID:</b> #{report.session_id}\n"
            f"• <b>Reported User ID:</b> <code>{report.reported_id}</code> ({reported_tag})\n"
            f"• <b>Reporter ID:</b> <code>{report.reporter_id}</code> ({reporter_tag})\n"
            f"• <b>Filed At:</b> {date_str}\n\n"
            f"📝 <b>Report Reason:</b>\n{html.escape(report.reason_text or 'No reason provided')}\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚡ <i>Quick Command to Ban:</i> <code>/block {report.reported_id}</code>"
        )
        keyboard = get_admin_report_keyboard(report.id, report.reported_id)

        await bot.send_message(
            chat_id=message.chat.id,
            text=report_text,
            reply_markup=keyboard,
            parse_mode="HTML",
        )

        if report.evidence_file_id:
            try:
                await bot.send_photo(
                    chat_id=message.chat.id,
                    photo=report.evidence_file_id,
                    caption=f"📸 <b>Evidence attached to Report #{report.id}</b>",
                    parse_mode="HTML",
                )
            except TelegramAPIError:
                try:
                    await bot.send_document(
                        chat_id=message.chat.id,
                        document=report.evidence_file_id,
                        caption=f"📄 <b>Document evidence attached to Report #{report.id}</b>",
                        parse_mode="HTML",
                    )
                except TelegramAPIError as e:
                    logger.warning("Could not send evidence file for report %s: %s", report.id, e)


@router.message(Command("backup"))
async def handle_admin_backup(message: Message) -> None:
    """Trigger an immediate manual PostgreSQL database backup and report status."""
    status_msg = await message.reply("⏳ <i>Creating PostgreSQL database backup snapshot...</i>", parse_mode="HTML")
    success, filename_or_err, size_bytes = await create_database_backup()

    if success:
        size_kb = size_bytes / 1024
        size_display = f"{size_kb / 1024:.2f} MB" if size_kb >= 1024 else f"{size_kb:.1f} KB"
        meta = get_backup_metadata()
        report = (
            "💾 <b>PostgreSQL Database Backup Created Successfully!</b> 🛡️\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📁 <b>Primary:</b> <code>backups/latest.sql.gz</code>\n"
            f"📦 <b>Archive Snapshot:</b> <code>{filename_or_err}</code>\n"
            f"📊 <b>Snapshot Size:</b> <b>{size_display}</b>\n"
            f"🗂 <b>Total Rolling Snapshots:</b> <b>{meta['total_snapshots']}</b>\n"
            f"🕒 <b>Timestamp:</b> <b>{meta['latest_modified']}</b>\n"
            "🔒 <b>Security:</b> Strict 0644 / Git-ignored / Isolated\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )
        await status_msg.edit_text(report, parse_mode="HTML")
    else:
        safe_err = html.escape(str(filename_or_err))
        await status_msg.edit_text(
            "❌ <b>Database Backup Failed</b> ⚠️\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Error Details:</b>\n<code>{safe_err}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            parse_mode="HTML",
        )



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


@router.message(Command("block"))
async def handle_admin_block_user(message: Message, bot: Bot) -> None:
    """Block a user from using the bot: /block [user_id]."""
    args = message.text.strip().split()
    if len(args) < 2 or not args[1].isdigit():
        await message.reply(
            "⚠️ <b>Usage:</b> <code>/block &lt;user_id&gt;</code>\n\n"
            "Example: <code>/block 123456789</code>",
            parse_mode="HTML",
        )
        return

    target_id = int(args[1])
    async with get_db_session() as session:
        success, partner_id = await ban_user_by_admin(session, user_id=target_id)

    # Update in-memory fast ban cache
    add_banned_user(target_id)

    # If partner was connected, notify and clear reply cache
    if partner_id:
        await reply_manager.clear_chat(target_id, partner_id)
        try:
            await bot.send_message(
                chat_id=partner_id,
                text=(
                    "🛑 <b>Your partner has disconnected or left the chat.</b>\n\n"
                    "Send /search to find a new partner! ×͜×"
                ),
                parse_mode="HTML",
            )
        except TelegramAPIError:
            pass

    # Notify banned user if possible
    try:
        await bot.send_message(
            chat_id=target_id,
            text="🚫 <b>You have been banned by an administrator from using Anonymous AUA.</b>",
            parse_mode="HTML",
        )
    except TelegramAPIError:
        pass

    admin_tag = f"@{message.from_user.username}" if message.from_user.username else f"ID:{message.from_user.id}"
    await message.reply(
        f"✅ <b>User <code>{target_id}</code> has been banned by {admin_tag}.</b>\n\n"
        "They can no longer search or send messages in the bot.",
        parse_mode="HTML",
    )


@router.message(Command("unblock"))
async def handle_admin_unblock_user(message: Message) -> None:
    """Unblock a previously banned user: /unblock [user_id]."""
    args = message.text.strip().split()
    if len(args) < 2 or not args[1].isdigit():
        await message.reply(
            "⚠️ <b>Usage:</b> <code>/unblock &lt;user_id&gt;</code>",
            parse_mode="HTML",
        )
        return

    target_id = int(args[1])
    async with get_db_session() as session:
        await unban_user_by_admin(session, user_id=target_id)

    remove_banned_user(target_id)

    admin_tag = f"@{message.from_user.username}" if message.from_user.username else f"ID:{message.from_user.id}"
    await message.reply(
        f"✅ <b>User <code>{target_id}</code> has been unbanned by {admin_tag}.</b>",
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("admin_ban_user:"))
async def handle_admin_ban_callback(callback: CallbackQuery, bot: Bot) -> None:
    """Ban a user directly via inline button on report alert."""
    target_id = int(callback.data.split(":")[1])
    async with get_db_session() as session:
        success, partner_id = await ban_user_by_admin(session, user_id=target_id)

    add_banned_user(target_id)

    if partner_id:
        await reply_manager.clear_chat(target_id, partner_id)
        try:
            await bot.send_message(
                chat_id=partner_id,
                text="🛑 <b>Your partner has disconnected.</b> Send /search to find a new partner.",
                parse_mode="HTML",
            )
        except TelegramAPIError:
            pass

    try:
        await bot.send_message(
            chat_id=target_id,
            text="🚫 <b>You have been banned by an administrator from using Anonymous AUA.</b>",
            parse_mode="HTML",
        )
    except TelegramAPIError:
        pass

    admin_tag = f"@{callback.from_user.username}" if callback.from_user.username else f"ID:{callback.from_user.id}"
    await callback.message.edit_text(
        f"{callback.message.text}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🚫 <b>User <code>{target_id}</code> BANNED by {admin_tag}.</b>",
        parse_mode="HTML",
    )
    await callback.answer(f"User {target_id} banned.")


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
        "Thank you for helping us improve Anonymous AUA! 🎭"
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
