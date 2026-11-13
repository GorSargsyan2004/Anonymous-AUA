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
from app.database.web_crud import get_web_admin_stats
from app.filters.admin import IsAdminFilter
from app.keyboards.inline import get_admin_report_keyboard
from app.services.backup import create_database_backup, get_backup_metadata
from app.services.ban_manager import add_banned_user, remove_banned_user
from app.services.matcher import get_default_preferences
from app.services.reply_manager import reply_manager
from app.states.states import AdminPenalizeState, AdminReplyState


logger = logging.getLogger(__name__)
router = Router(name="admin_router")

# Apply IsAdminFilter to all events in this router
router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminFilter())


@router.message(Command("stata", "stats"))
async def handle_stata(message: Message) -> None:
    """Display real-time aggregated metrics, advanced chat analytics, and backup status for Anonymous AUA Bot."""
    async with get_db_session() as session:
        stats = await get_admin_stats(session)
        web_stats = await get_web_admin_stats(session)

    adv = stats.get("advanced_chat", {})
    w24 = adv.get("window_24h", {})
    w30 = adv.get("window_30d", {})
    wall = adv.get("window_all_time", {})
    peak = adv.get("peak_hours", {})
    top_peaks = peak.get("top_by_initiated_30d", [])[:3]
    peak_str = ", ".join(
        f"{p['window_label'].replace(' AMT', '')} ({p['initiated_30d']})"
        for p in top_peaks
    ) or "N/A"
    ai = web_stats.get("ai_assistant", {})

    backup_info = get_backup_metadata()
    if backup_info["exists"]:
        latest_kb = backup_info["latest_size_bytes"] / 1024
        backup_str = (
            f"✅ Active ({backup_info['latest_modified']})\n"
            f"• Primary: <code>backups/latest.sql.gz</code> ({latest_kb:.1f} KB) | Snapshots: <b>{backup_info['total_snapshots']}</b>"
        )
    else:
        backup_str = "⚠️ No backup snapshot found yet (run /backup)"

    appeal_note = (
        f" <i>({stats['gender_appeal_count']} appeal pending)</i>"
        if stats.get("gender_appeal_count", 0) > 0
        else ""
    )

    def _fmt_dur(sec: float) -> str:
        s = int(round(sec or 0))
        if s < 60:
            return f"{s}s"
        if s < 3600:
            return f"{s // 60}m {s % 60}s"
        return f"{s // 3600}h {(s % 3600) // 60}m"

    dashboard = (
        "📊 <b>Anonymous AUA — Administrator Dashboard</b> 🎭\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👥 <b>User Base & Reach:</b>\n"
        f"• Total Registered: <b>{stats['total_users']}</b>\n"
        f"• 💬 Active Bot Chats: <b>{stats['active_chat_users']}</b> | 🚫 Blocked Bot: <b>{stats['blocked_bot_users']}</b> | 🔨 Banned: <b>{stats['banned_count']}</b>\n"
        f"• 🔍 Searching Queue: <b>{stats['searching_users']}</b> | ⚡ Live Active Chats: <b>{stats['active_chats']}</b>\n\n"
        f"📈 <b>Anonymous Chat Analytics (AMT UTC+4):</b>\n"
        f"• <b>Chats Initiated:</b> <b>{w24.get('initiated', 0)}</b> (24h) | <b>{w30.get('initiated', 0)}</b> (30d) | <b>{wall.get('initiated', 0)}</b> (Total)\n"
        f"• <b>Active Sessions Mean Counter:</b> <b>{w24.get('concurrent_mean', 0)}</b> (24h) | <b>{w30.get('concurrent_mean', 0)}</b> (30d)\n"
        f"• <b>Active Sessions Median:</b> <b>{w24.get('concurrent_median', 0)}</b> (24h) | <b>{w30.get('concurrent_median', 0)}</b> (30d)\n"
        f"• <b>Peak Concurrency:</b> <b>{w24.get('concurrent_peak', 0)}</b> (24h) | <b>{w30.get('concurrent_peak', 0)}</b> (30d)\n"
        f"• <b>Msgs / Chat (Mean | Median):</b> {w30.get('messages_mean', 0)} | {w30.get('messages_median', 0)} (30d)\n"
        f"• <b>Duration (Mean | Median):</b> {_fmt_dur(w30.get('duration_mean_sec', 0))} | {_fmt_dur(w30.get('duration_median_sec', 0))} (30d)\n"
        f"• 🔥 <b>Most Active Hours (30d):</b> {peak_str} AMT\n\n"
        f"✦ <b>AI Assistant Usage:</b>\n"
        f"• Prompts: <b>{ai.get('prompts_24h', 0)}</b> (24h) | <b>{ai.get('prompts_30d', 0)}</b> (30d) | <b>{ai.get('total_prompts', 0)}</b> (Total)\n"
        f"• Sessions: <b>{ai.get('unique_sessions_total', 0)}</b> | Mean/Sess: <b>{ai.get('mean_prompts_per_session', 0)}</b> | Avg Latency: <b>{ai.get('mean_latency_ms', 0)}ms</b>\n\n"
        f"🎓 <b>Verification &amp; Demographics:</b>\n"
        f"• Verified: <b>{stats['total_verified']}</b> (🎓 Students: {stats['verified_students']} | 🏛️ Alumni: {stats['verified_alumni']} | ⚠️ Expired: {stats['verified_expired']} | 🔒 Gender OK: {stats['gender_verified']})\n"
        f"• Unverified: <b>{stats['unverified_count']}</b> | Conflicts: <b>{stats['gender_mismatch_count']}</b>{appeal_note}\n"
        f"• Sex: M <b>{stats['male_count']}</b> / F <b>{stats['female_count']}</b> / Unset <b>{stats['unset_sex_count']}</b>\n"
        f"• Orientation: Straight <b>{stats['straight_count']}</b> | Bi <b>{stats.get('bisexual_count', 0)}</b> | Lesbian <b>{stats['lesbian_count']}</b> | Gay <b>{stats['gay_count']}</b>\n\n"
        f"⭐ <b>Feedback, Reports &amp; Suggestions:</b>\n"
        f"• Ratings: 👍 {stats['likes_count']} | 👎 {stats['dislikes_count']} | 🚫 {stats['spam_count']} | 🏫 {stats['not_aua_count']}\n"
        f"• Reports: {stats['total_reports']} total ({stats['pending_reports']} pending) | Suggestions: {stats.get('total_suggestions', 0)} ({stats.get('pending_suggestions', 0)} pending)\n\n"
        f"📬 <b>/link Inbox &amp; /invite Links:</b>\n"
        f"• Inbox Owners: <b>{stats['inbox_users_count']}</b> ({stats['active_inboxes_count']} active) | Q&amp;As: <b>{stats['total_inbox_messages']}</b> ({stats['inbox_reply_rate']}% replied)\n"
        f"• Invites: <b>{stats['total_chat_invites']}</b> created | <b>{stats['used_chat_invites']}</b> connected ({stats['active_chat_invites']} active)\n\n"
        f"💾 <b>Database Backups:</b>\n"
        f"{backup_str}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📈 <b>Full Visual Charts Dashboard:</b> <a href=\"https://auahub.am/stata\">auahub.am/stata</a>\n"
        "💡 <i>Tip: Use /stats_web for website metrics, /reports for pending reports, /backup for snapshots.</i>"
    )

    await message.answer(dashboard, parse_mode="HTML", disable_web_page_preview=True)


@router.message(Command("stats_web", "web_stats"))
async def handle_stats_web(message: Message) -> None:
    """Display real-time aggregated metrics for the AUA Hub web platform (auahub.am)."""
    async with get_db_session() as session:
        stats = await get_web_admin_stats(session)

    u = stats["users"]
    c = stats["content"]
    a = stats["analytics"]
    ai = stats.get("ai_assistant", {})

    event_labels = {
        "click_free_rooms": "🏫 Free Rooms Lookups",
        "click_have_need": "🔄 Have-Need Views",
        "click_groups": "👥 Telegram Groups Views",
        "click_chat": "💬 Global Chat Opens",
        "click_pwa_install": "📲 PWA Install Clicks",
        "search_anon_posts": "🔍 Anon Posts Searches",
        "search_inbox": "🔎 Inbox Searches",
        "join_group": "🔗 Group Invites Clicked",
        "search_courses": "📚 Course Swap Searches",
        "view_feedbacks": "⭐ Instructor Ratings Views",
        "click_ai_assistant": "✦ AI Assistant Views",
        "click_partner_findaua": "🐻 FindAUA Partner Clicks",
        "click_partner_jenzareviews": "🎓 JenzaReviews Partner Clicks",
    }
    events_lines = []
    for ev, count in a["event_breakdown"].items():
        label = event_labels.get(ev, f"🖱️ {ev}")
        events_lines.append(f"  • {label}: <b>{count}</b>")

    events_str = "\n".join(events_lines) if events_lines else "  • <i>No recorded click events yet</i>"

    dashboard = (
        "🌐 <b>AUA Hub (auahub.am) — Web Admin Dashboard</b> 📊\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "👥 <b>Registered Web Accounts:</b>\n"
        f"• Total Web Accounts: <b>{u['total']}</b>\n"
        f"  ├ 🎓 Students (@edu.aua.am): <b>{u['students']}</b>\n"
        f"  ├ 🏛️ Alumni (@alumni.aua.am): <b>{u['alumni']}</b>\n"
        f"  └ 💼 Staff/Other (@aua.am): <b>{u['other']}</b>\n"
        f"• 🔗 Synced with Telegram Bot: <b>{u['synced']}</b>\n"
        f"• 📈 Growth: <b>+{u['new_24h']}</b> last 24h | <b>+{u['new_7d']}</b> last 7d\n\n"
        "✦ <b>AI Assistant Telemetry:</b>\n"
        f"• Total Prompts: <b>{ai.get('total_prompts', 0)}</b> (24h: <b>{ai.get('prompts_24h', 0)}</b> | 30d: <b>{ai.get('prompts_30d', 0)}</b>)\n"
        f"• Sessions: <b>{ai.get('unique_sessions_total', 0)}</b> | Mean Prompts/Session: <b>{ai.get('mean_prompts_per_session', 0)}</b> | Median: <b>{ai.get('median_prompts_per_session', 0)}</b>\n"
        f"• Gemini Rate: <b>{ai.get('gemini_success_rate', 0)}%</b> | Mean Latency: <b>{ai.get('mean_latency_ms', 0)}ms</b>\n\n"
        "📦 <b>Web Platform Content & Activity:</b>\n"
        f"• 🎭 Anonymous Public Posts: <b>{c['anon_posts']}</b>\n"
        f"• 📬 Total Personal Inbox Q&As: <b>{c['inbox_messages']}</b>\n"
        f"• 🔄 Course Swaps (Have-Need): <b>{c['have_need_active']}</b> active <i>({c['have_need_total']} total)</i>\n"
        f"• 👥 Telegram Communities Listed: <b>{c['telegram_groups']}</b>\n"
        f"• ⭐ Instructor & Course Ratings: <b>{c['academic_feedbacks']}</b>\n"
        f"• 💬 Campus Global Chat Messages: <b>{c['chat_messages']}</b>\n\n"
        "🖱️ <b>Web Interactions & Button Clicks:</b>\n"
        f"• Total Tracked Clicks: <b>{a['total_clicks']}</b> <i>(+{a['clicks_24h']} in last 24h)</i>\n"
        f"{events_str}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📊 <b>Interactive Charts Dashboard:</b> <a href=\"https://auahub.am/stata\">auahub.am/stata</a>"
    )

    await message.answer(dashboard, parse_mode="HTML", disable_web_page_preview=True)


@router.message(Command("reports", "pending_reports"))
async def handle_pending_reports(message: Message, bot: Bot) -> None:
    """Display and manage all pending/unresolved user reports."""
    async with get_db_session() as session:
        reports = await get_pending_reports(session)
        if not reports:
            await message.reply("✅ <b>No pending reports found!</b> All reports are resolved.", parse_mode="HTML")
            return

        user_ids = {r.reporter_id for r in reports if r.reporter_id} | {r.reported_id for r in reports if r.reported_id}

        user_map = {}
        if user_ids:
            u_stmt = select(User).where(User.id.in_(user_ids))
            u_res = await session.execute(u_stmt)
            for u in u_res.scalars().all():
                user_map[u.id] = u

    await message.reply(
        f"🚨 <b>Found {len(reports)} pending report(s):</b>\n"
        "Reviewing each report below with available AI verdict audit details:",
        parse_mode="HTML",
    )

    for report in reports:
        date_str = report.created_at.strftime("%Y-%m-%d %H:%M UTC") if report.created_at else "N/A"

        if report.identity_revealed_for_illegal and report.reported_id:
            reporter = user_map.get(report.reporter_id)
            reported = user_map.get(report.reported_id)
            reporter_tag = f"@{html.escape(reporter.username)}" if reporter and reporter.username else "N/A"
            reported_tag = f"@{html.escape(reported.username)}" if reported and reported.username else "N/A"

            report_text = (
                f"🚨 <b>Emergency Unmasked Report #{report.id} (Tier 3 Illegal)</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"• <b>Session ID:</b> #{report.session_id}\n"
                f"• <b>Reported User ID:</b> <code>{report.reported_id}</code> ({reported_tag})\n"
                f"• <b>Reporter ID:</b> <code>{report.reporter_id}</code> ({reporter_tag})\n"
                f"• <b>Filed At:</b> {date_str}\n"
                f"• <b>Severity:</b> {report.severity_category}\n\n"
                f"📝 <b>AI Summary:</b>\n{html.escape(report.anonymized_summary or report.reason_text or 'N/A')}\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"⚡ <i>Command:</i> <code>/block {report.reported_id}</code>"
            )
            keyboard = get_admin_report_keyboard(report.id, report.reported_id)
        else:
            guilty_str = report.guilty_party or "Evaluated by AI"
            sev_str = (report.severity_category or "None").replace("_", " ")
            summary_str = html.escape(report.anonymized_summary or report.reason_text or "No summary available")

            report_text = (
                f"🛡️ <b>Anonymized AI Report #{report.id}</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"• <b>Session ID:</b> #{report.session_id}\n"
                f"• <b>Privacy Status:</b> 🔒 Zero-Knowledge Ephemeral RAM (User IDs Protected)\n"
                f"• <b>Guilty Party:</b> <code>{guilty_str}</code>\n"
                f"• <b>Severity Category:</b> <code>{sev_str}</code>\n"
                f"• <b>Penalties:</b> Reported User: -{report.reported_user_penalty} | Reporter: -{report.reporter_penalty}\n"
                f"• <b>Filed At:</b> {date_str}\n\n"
                f"📝 <b>Anonymized AI Summary:</b>\n<i>{summary_str}</i>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "<i>AI Moderation resolved this report automatically.</i>"
            )
            keyboard = None

        await bot.send_message(
            chat_id=message.chat.id,
            text=report_text,
            reply_markup=keyboard,
            parse_mode="HTML",
        )



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
        "Enter the number of priority points to deduct from this user (e.g., 15):\n\n"
        "<i>(Send /cancel to abort)</i>",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminPenalizeState.waiting_for_penalty_amount, F.text)
async def handle_admin_penalty_amount(message: Message, state: FSMContext) -> None:
    """Process administrator penalty input and update user priority and report."""
    raw_input = message.text.strip()
    if raw_input.lower() in ["/cancel", "/stop"]:
        await state.clear()
        await message.reply("Action cancelled.")
        return

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


@router.message(Command("setsex", "changesex"))
async def handle_admin_set_sex(message: Message, bot: Bot) -> None:
    """Manually correct or override a user's sex: /setsex <user_id_or_@username> <male|female>"""
    args = message.text.strip().split()
    if len(args) < 3:
        await message.reply(
            "⚠️ <b>Usage:</b> <code>/setsex &lt;user_id or @username&gt; &lt;male|female&gt;</code>\n\n"
            "Example: <code>/setsex @mm1191188 male</code> or <code>/setsex 8911682354 male</code>",
            parse_mode="HTML",
        )
        return

    target_raw = args[1].lstrip("@").strip()
    new_sex = args[2].lower().strip()
    if new_sex not in ("male", "female"):
        await message.reply("⚠️ Sex must be either <code>male</code> or <code>female</code>.", parse_mode="HTML")
        return

    async with get_db_session() as session:
        if target_raw.isdigit():
            stmt = select(User).where(User.id == int(target_raw))
        else:
            stmt = select(User).where(User.username.ilike(target_raw))
        res = await session.execute(stmt)
        user = res.scalar_one_or_none()

        if not user:
            await message.reply(f"❌ User <code>{html.escape(target_raw)}</code> not found.", parse_mode="HTML")
            return

        old_sex = user.sex or "unset"
        user.sex = new_sex
        user.inferred_gender = new_sex
        user.is_gender_verified = True
        user.gender_mismatch = False
        user.gender_appeal_pending = False
        user.sex_updated_at = None  # Clear change cooldown penalty
        if user.priority < 50:
            user.priority = 50

        # Update default target preferences
        def_sexes, def_orients = get_default_preferences(user.sex, user.orientation or "straight")
        user.target_sexes = ",".join(def_sexes)
        user.target_orientations = ",".join(def_orients)

        user_id = user.id
        username = user.username
        target_sexes = user.target_sexes
        target_orients = user.target_orientations
        await session.commit()

    admin_tag = f"@{message.from_user.username}" if message.from_user.username else f"ID:{message.from_user.id}"
    await message.reply(
        f"✅ <b>User Sex Updated Successfully by {admin_tag}!</b>\n\n"
        f"• <b>User ID:</b> <code>{user_id}</code> (@{html.escape(username or 'N/A')})\n"
        f"• <b>Sex:</b> {old_sex} ➔ <b>{new_sex.capitalize()}</b>\n"
        f"• <b>Cooldown Penalty:</b> Cleared ✅\n"
        f"• <b>Target Sexes:</b> <code>{target_sexes}</code>\n"
        f"• <b>Target Orientations:</b> <code>{target_orients}</code>\n"
        f"• <b>Gender Mismatch:</b> Cleared",
        parse_mode="HTML",
    )

    try:
        await bot.send_message(
            chat_id=user_id,
            text=(
                f"⚙️ <b>Profile Updated by Administrator</b>\n\n"
                f"Your profile sex has been updated to <b>{new_sex.capitalize()}</b> ✅.\n"
                "The change cooldown penalty has been cleared, and your matching preferences have been restored.\n\n"
                "Send /search to start chatting!"
            ),
            parse_mode="HTML",
        )
    except TelegramAPIError:
        pass


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
        "<i>(Send /cancel at any time to abort)</i>",
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
