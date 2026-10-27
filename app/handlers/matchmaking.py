"""Matchmaking handlers (/search, /stop, /next) and anonymous message relay."""

import asyncio
import html
import logging
from typing import Dict, Optional
from aiogram import Bot, F, Router
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, MessageReactionUpdated, ReplyParameters
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud import (
    end_active_session,
    get_active_session_by_user_id,
    get_latest_session_for_user,
    get_or_create_user,
    get_recent_sessions_for_user,
    get_user,
    pause_chat_session,
    reset_chat_session_inactivity_warning,
    set_user_bot_blocked,
    set_user_searching,
    should_prompt_bot_feedback,
    touch_chat_session_activity,
    unpause_chat_session,
)
from app.database.session import get_db_session
from app.keyboards.inline import (
    get_bot_feedback_keyboard,
    get_emergency_report_keyboard,
    get_feedback_keyboard,
    get_gender_mismatch_keyboard,
    get_recent_partners_keyboard,
)
from app.services.email_verification import get_partner_verification_badge
from app.services.link_shield import link_shield
from app.services.matcher import match_user_in_queue
from app.services.media_batcher import media_batcher
from app.services.one_time_media import (
    DEFAULT_OTM_DURATION_SECONDS,
    OTM_DURATION_SECONDS,
    get_otm_prompt_keyboard,
    get_otm_viewing_keyboard,
    one_time_media_manager,
)
from app.services.rate_limiter import rate_limiter
from app.services.reply_manager import reply_manager
from app.states.states import ReportState

logger = logging.getLogger(__name__)
router = Router(name="matchmaking_router")

EMULATED_TYPING_SLEEP = True
_user_relay_locks: Dict[int, asyncio.Lock] = {}


def get_message_file_size(message: Message) -> Optional[int]:
    """Extract file size in bytes from any media type in message."""
    photos = getattr(message, "photo", None)
    if photos:
        return getattr(photos[-1], "file_size", None)
    for media_attr in ("video", "document", "audio", "voice", "video_note", "animation"):
        media_obj = getattr(message, media_attr, None)
        if media_obj and getattr(media_obj, "file_size", None) is not None:
            return media_obj.file_size
    return None


def get_user_relay_lock(user_id: int) -> asyncio.Lock:
    """Retrieve or initialize an asyncio.Lock per user ensuring strict FIFO message relay ordering."""
    if user_id not in _user_relay_locks:
        _user_relay_locks[user_id] = asyncio.Lock()
    return _user_relay_locks[user_id]


def get_chat_action_for_message(message: Message) -> str:
    """Determine the appropriate Telegram chat action based on incoming message type."""
    if message.text:
        return "typing"
    elif message.photo:
        return "upload_photo"
    elif message.voice:
        return "record_voice"
    elif message.video:
        return "upload_video"
    elif message.video_note:
        return "record_video_note"
    elif message.sticker:
        return "choose_sticker"
    elif message.audio:
        return "upload_document"
    elif message.document:
        return "upload_document"
    return "typing"


def get_emulated_typing_duration(message: Message) -> float:
    """Calculate natural typing delay in seconds based on message length and content."""
    if message.text:
        length = len(message.text)
        return min(max(0.5, 0.5 + length * 0.01), 1.8)
    elif message.caption:
        length = len(message.caption)
        return min(max(0.6, 0.6 + length * 0.01), 1.8)
    elif message.voice or message.video_note:
        return 1.0
    elif message.photo or message.video or message.document or message.audio:
        return 0.8
    return 0.5


async def schedule_search_notice(bot: Bot, user_id: int) -> None:
    """Send a friendly notification after 30 seconds of searching asking to share the bot."""
    await asyncio.sleep(30)
    async with get_db_session() as session:
        user = await get_user(session, user_id=user_id)
        if user and user.is_searching and user.current_partner_id is None:
            try:
                await bot.send_message(
                    chat_id=user_id,
                    text=(
                        "⏳ <b>Still looking for a partner...</b> 🥷\n\n"
                        "<b>Anonymous AUA</b> matches students by mutual preferences, so there might be fewer active peers in your queue right now.\n\n"
                        "📢 <b>Help grow our campus community!</b>\n"
                        "Share the bot with your AUA classmates and group chats so more people join:\n"
                        "👉 <b>https://t.me/aua_anonymous_bot</b>\n\n"
                        "The more students join, the faster matches happen! You remain in the search queue and will be connected as soon as someone joins.\n\n"
                        "<i>(Send /stop at any time to cancel) ×͜×</i>"
                    ),
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except TelegramAPIError as e:
                logger.debug("Could not send search timeout notice to %s: %s", user_id, e)


async def maybe_send_bot_feedback_prompt(bot: Bot, session: AsyncSession, user_id: int) -> None:
    """Send bot improvement prompt after every 2 sessions if user hasn't submitted one today."""
    if await should_prompt_bot_feedback(session, user_id=user_id):
        try:
            await bot.send_message(
                chat_id=user_id,
                text=(
                    "💡 <b>Help Us Improve Anonymous AUA!</b> 🎭\n\n"
                    "You've completed several anonymous conversations! How is your experience so far?\n\n"
                    "We'd love to hear your thoughts, ideas, or suggestions on how to make this bot better for our campus community."
                ),
                reply_markup=get_bot_feedback_keyboard(),
                parse_mode="HTML",
            )
        except TelegramAPIError as e:
            logger.debug("Could not send bot improvement prompt to %s: %s", user_id, e)


async def perform_search(user_id: int, bot: Bot, message: Message) -> None:
    """Execute queue search or enqueue user."""
    async with get_db_session() as session:
        user = await get_or_create_user(session, user_id=user_id, username=message.from_user.username)

        # Check onboarding
        if not user.sex or not user.orientation:
            await message.answer(
                "⚠️ <b>Profile not set up yet!</b>\n\n"
                "Please configure your sex and sexual orientation before searching.\n"
                "Send /start to complete your profile setup.",
                parse_mode="HTML",
            )
            return

        # Check gender conflict for verified users
        if getattr(user, "gender_mismatch", False):
            inferred = (user.inferred_gender or "other").capitalize()
            current = (user.sex or "other").capitalize()
            kb = get_gender_mismatch_keyboard(inferred_gender=user.inferred_gender or "other")
            await message.answer(
                f"⚠️ <b>Matchmaking Paused: Gender Verification Conflict</b>\n\n"
                f"Your verified university email name indicates <b>{inferred}</b>, but your profile is currently set to <b>{current}</b>.\n\n"
                f"To protect our student community from catfishing and false identity claims, "
                f"you cannot enter the matchmaking queue until this conflict is resolved.\n\n"
                f"<b>Options:</b>\n"
                f"• Tap <b>Switch Profile to {inferred}</b> to instantly correct your profile.\n"
                f"• If your name is unisex, rare, or the system misclassified it, tap <b>Appeal to Admin</b>.",
                reply_markup=kb,
                parse_mode="HTML",
            )
            return

        # Check if already in active chat
        if user.current_partner_id is not None:
            await message.answer(
                "💬 <b>You are already in an active chat!</b>\n\n"
                "• Send /next to switch to a new partner\n"
                "• Send /stop to end this chat",
                parse_mode="HTML",
            )
            return

        # Check if already searching
        if user.is_searching:
            await message.answer(
                "🔍 <b>You are already in the search queue...</b>\n\n"
                "Please wait while we find a compatible student, or send /stop to cancel.",
                parse_mode="HTML",
            )
            return

        # Try to find a match
        partner, chat_session = await match_user_in_queue(session, user_id=user_id)

    if partner and chat_session:
        partner_verified_badge = get_partner_verification_badge(partner)
        current_verified_badge = get_partner_verification_badge(user)

        user_match_msg = (
            "✨ <b>Partner found!</b> 🎭\n\n"
            f"{partner_verified_badge}"
            "You are now connected anonymously. Say hello! 👾\n\n"
            "⚡ <b>Commands:</b>\n"
            "• /report — Emergency report & block partner\n"
            "• /next — Skip to the next partner\n"
            "• /stop — End this chat\n\n"
            "🔒 <i>Privacy Shield Active: Your profile, phone number, and IP are strictly hidden.</i>"
        )
        partner_match_msg = (
            "✨ <b>Partner found!</b> 🎭\n\n"
            f"{current_verified_badge}"
            "You are now connected anonymously. Say hello! 👾\n\n"
            "⚡ <b>Commands:</b>\n"
            "• /report — Emergency report & block partner\n"
            "• /next — Skip to the next partner\n"
            "• /stop — End this chat\n\n"
            "🔒 <i>Privacy Shield Active: Your profile, phone number, and IP are strictly hidden.</i>"
        )
        # Notify initiating user
        await message.answer(user_match_msg, parse_mode="HTML")
        # Notify matched partner
        try:
            await bot.send_message(chat_id=partner.id, text=partner_match_msg, parse_mode="HTML")
        except TelegramAPIError as e:
            logger.warning("Failed to notify matched partner %s: %s", partner.id, e)
    else:
        await message.answer(
            "🔍 <b>Searching for a compatible AUA partner...</b> 🥷\n\n"
            "Please wait while we connect you, or send /stop to cancel.",
            parse_mode="HTML",
        )
        # Schedule the 30-second notification if still searching
        asyncio.create_task(schedule_search_notice(bot, user_id))


@router.message(Command("report"))
async def handle_report_command(message: Message, bot: Bot, state: FSMContext) -> None:
    """Handle /report command during an active chat or to select a recent partner to report."""
    user_id = message.from_user.id
    async with get_db_session() as session:
        user = await get_or_create_user(session, user_id=user_id)
        partner_id = user.current_partner_id
        active_chat = await get_active_session_by_user_id(session, user_id=user_id) if partner_id else None
        recent_sessions = await get_recent_sessions_for_user(session, user_id=user_id, limit=5)

        # 1. If user is in an active chat
        if partner_id and active_chat:
            if recent_sessions:
                kb = get_recent_partners_keyboard(
                    sessions=recent_sessions,
                    user_id=user_id,
                    in_active_chat=True,
                    active_session_id=active_chat.id,
                    active_partner_id=partner_id,
                )
                await message.answer(
                    "🚨 <b>Emergency Safety & Report Shield</b> 🛡️\n"
                    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    "Select which partner you would like to report:\n\n"
                    "• <b>Current Active Partner</b>: Disconnect and file report immediately\n"
                    "• <b>Previous Partners</b>: File a report against a past chat\n\n"
                    "🔒 <i>Your real identity, phone number, and IP are strictly protected.</i>",
                    reply_markup=kb,
                    parse_mode="HTML",
                )
                return

            await message.answer(
                "🚨 <b>Emergency Safety & Report Shield</b> 🛡️\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "You requested to report your current chat partner.\n\n"
                "🔒 <b>You are completely safe:</b>\n"
                "• Your partner <b>CANNOT</b> see your phone number, real name, @username, or profile picture.\n"
                "• Telegram does NOT reveal IP addresses. Claims of 'hacking' or 'tracking your location' are <b>100% fake bluffs</b>.\n\n"
                "👉 <i>Would you like to immediately disconnect and block this partner while filing the report?</i>",
                reply_markup=get_emergency_report_keyboard(active_chat.id, partner_id),
                parse_mode="HTML",
            )
            return

        # 2. User is not in active chat: show recent partners list
        if recent_sessions:
            kb = get_recent_partners_keyboard(
                sessions=recent_sessions,
                user_id=user_id,
                in_active_chat=False,
            )
            await message.answer(
                "🚨 <b>Report a Recent Partner</b> 🛡️\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "Select which recent chat partner you want to report:\n\n"
                "🔒 <i>Your identity is 100% private and protected.</i>",
                reply_markup=kb,
                parse_mode="HTML",
            )
            return

    await message.answer(
        "💬 <b>No recent chat found to report.</b>\n\n"
        "You can use /report whenever you are in an active chat or after completing a conversation.",
        parse_mode="HTML",
    )


@router.message(Command("recent", "history"))
async def handle_recent_command(message: Message, bot: Bot, state: FSMContext) -> None:
    """View recent chat partners, review ratings, or report incidents."""
    await state.clear()
    user_id = message.from_user.id

    async with get_db_session() as session:
        user = await get_or_create_user(session, user_id=user_id)
        partner_id = user.current_partner_id
        active_chat = await get_active_session_by_user_id(session, user_id=user_id) if partner_id else None
        recent_sessions = await get_recent_sessions_for_user(session, user_id=user_id, limit=5)

    if not recent_sessions and not active_chat:
        await message.answer(
            "💬 <b>No recent chat history found.</b>\n\n"
            "Send /search to find an anonymous partner and start chatting! 🥷",
            parse_mode="HTML",
        )
        return

    kb = get_recent_partners_keyboard(
        sessions=recent_sessions,
        user_id=user_id,
        in_active_chat=bool(active_chat and partner_id),
        active_session_id=active_chat.id if active_chat else None,
        active_partner_id=partner_id,
    )
    await message.answer(
        "📋 <b>Your Recent Chat Partners</b> 🥷\n\n"
        "Select a partner below to view details, update your rating, or submit a report:",
        reply_markup=kb,
        parse_mode="HTML",
    )


@router.message(Command("search"))
async def handle_search(message: Message, bot: Bot, state: FSMContext) -> None:
    """Handle /search command to find a chat partner."""
    await state.clear()
    await perform_search(message.from_user.id, bot, message)


@router.message(Command("stop"))
async def handle_stop(message: Message, bot: Bot, state: FSMContext) -> None:
    """Handle /stop command to cancel search or end active chat session."""
    await state.clear()
    user_id = message.from_user.id

    async with get_db_session() as session:
        user = await get_or_create_user(session, user_id=user_id)

        # 1. If user is in an active chat
        if user.current_partner_id is not None:
            result = await end_active_session(session, user_id=user_id)
            if result:
                chat_session, partner_id = result
                await reply_manager.clear_chat(user_id, partner_id, session=session, session_id=chat_session.id)
                one_time_media_manager.clear_session(chat_session.id)

                # Notify initiating user
                await message.answer(
                    "🛑 <b>Chat ended.</b>\n\n"
                    "How was your conversation with this partner? Leave a rating below:",
                    reply_markup=get_feedback_keyboard(chat_session.id, partner_id),
                    parse_mode="HTML",
                )

                # Check if user qualifies for bot improvement feedback prompt
                await maybe_send_bot_feedback_prompt(bot, session, user_id)

                # Notify partner
                try:
                    await bot.send_message(
                        chat_id=partner_id,
                        text=(
                            "🛑 <b>Your partner has ended the chat.</b>\n\n"
                            "How was your conversation with this partner? Leave a rating below:"
                        ),
                        reply_markup=get_feedback_keyboard(chat_session.id, user_id),
                        parse_mode="HTML",
                    )
                    # Check if partner qualifies for bot improvement feedback prompt
                    await maybe_send_bot_feedback_prompt(bot, session, partner_id)
                except TelegramAPIError as e:
                    logger.warning("Could not send session end message to partner %s: %s", partner_id, e)
                return

        # 2. If user is only searching
        if user.is_searching:
            await set_user_searching(session, user_id=user_id, is_searching=False)
            await message.answer(
                "🛑 <b>Search cancelled.</b>\n\n"
                "Send /search whenever you are ready to find a partner! ×͜×",
                parse_mode="HTML",
            )
            return

    # 3. Neither in chat nor searching
    await message.answer(
        "💬 You are not currently in an active chat or search.\n\n"
        "Send /search to find someone to chat with! 🎭",
        parse_mode="HTML",
    )


@router.message(Command("next"))
async def handle_next(message: Message, bot: Bot, state: FSMContext) -> None:
    """Handle /next command to end current chat and immediately search for a new partner."""
    await state.clear()
    user_id = message.from_user.id

    async with get_db_session() as session:
        user = await get_or_create_user(session, user_id=user_id)

        # If in active chat, tear it down and send feedback prompt to both
        if user.current_partner_id is not None:
            result = await end_active_session(session, user_id=user_id)
            if result:
                chat_session, partner_id = result
                await reply_manager.clear_chat(user_id, partner_id, session=session, session_id=chat_session.id)
                one_time_media_manager.clear_session(chat_session.id)

                await message.answer(
                    "⏭️ <b>Skipping to next partner...</b>\n\n"
                    "Rate your previous chat:",
                    reply_markup=get_feedback_keyboard(chat_session.id, partner_id),
                    parse_mode="HTML",
                )
                await maybe_send_bot_feedback_prompt(bot, session, user_id)

                try:
                    await bot.send_message(
                        chat_id=partner_id,
                        text=(
                            "🛑 <b>Your partner has skipped to the next chat.</b>\n\n"
                            "Rate your previous chat:"
                        ),
                        reply_markup=get_feedback_keyboard(chat_session.id, user_id),
                        parse_mode="HTML",
                    )
                    await maybe_send_bot_feedback_prompt(bot, session, partner_id)
                except TelegramAPIError as e:
                    logger.warning("Failed to notify partner %s on /next: %s", partner_id, e)

    # Immediately initiate search for this user
    await perform_search(user_id, bot, message)


@router.message(Command("pause", "sleep"))
async def handle_pause_command(message: Message, bot: Bot, state: FSMContext) -> None:
    """Put active anonymous chat session into Sleep Mode / Pause."""
    await state.clear()
    user_id = message.from_user.id

    async with get_db_session() as session:
        user = await get_or_create_user(session, user_id=user_id)
        if not user.current_partner_id:
            await message.answer(
                "💬 <b>You are not currently in an active chat.</b>\n\n"
                "Send /search to find an anonymous partner first! 🥷",
                parse_mode="HTML",
            )
            return

        active_chat = await get_active_session_by_user_id(session, user_id=user_id)
        if not active_chat:
            await message.answer("⚠️ <b>Could not find an active chat session.</b>", parse_mode="HTML")
            return

        partner_id = user.current_partner_id

        if active_chat.is_paused:
            await message.answer(
                "🌙 <b>Your chat is already in Sleep Mode!</b> 💤\n\n"
                "Inactivity timer is paused. Send any message whenever you want to wake it up! ☀️",
                parse_mode="HTML",
            )
            return

        await pause_chat_session(session, active_chat.id)

    await message.answer(
        "🌙 <b>Sleep Mode Activated!</b> 💤\n\n"
        "• Inactivity auto-closure is frozen for up to 24 hours.\n"
        "• Hold your chat overnight without fear of disconnection.\n"
        "• <b>To resume:</b> Simply send any message, and chat will instantly awaken! ☀️",
        parse_mode="HTML",
    )

    try:
        await bot.send_message(
            chat_id=partner_id,
            text=(
                "🌙 <b>Your partner put this chat into Sleep Mode!</b> 💤\n\n"
                "• Inactivity auto-disconnect is frozen for up to 24 hours.\n"
                "• Send any message whenever you're ready to resume chatting! ☀️\n"
                "• Or send /stop if you wish to end this chat."
            ),
            parse_mode="HTML",
        )
    except Exception:
        pass


async def _handle_awakening_if_paused(
    bot: Bot,
    session: AsyncSession,
    session_id: Optional[int],
    was_paused: bool,
    user_id: int,
    partner_id: int,
) -> None:
    """If chat was paused in sleep mode, unpause and notify both partners."""
    if not was_paused or not session_id:
        return
    await unpause_chat_session(session, session_id)
    try:
        await bot.send_message(
            chat_id=partner_id,
            text="☀️ <b>Chat Awakened from Sleep Mode!</b> 🎭\n\n<i>Your partner resumed the conversation:</i>",
            parse_mode="HTML",
        )
    except Exception:
        pass
    try:
        await bot.send_message(
            chat_id=user_id,
            text="☀️ <b>Chat Awakened!</b> 🎭\n<i>Sleep mode ended. Normal chatting resumed!</i>",
            parse_mode="HTML",
        )
    except Exception:
        pass


@router.message(Command("onetime", "once", "viewonce"))
async def handle_toggle_onetime_mode(message: Message) -> None:
    """Toggle ephemeral View-Once mode for outgoing media."""
    user_id = message.from_user.id
    is_enabled = one_time_media_manager.toggle_user_mode(user_id)
    if is_enabled:
        await message.answer(
            "🔒 <b>View-Once Mode: ON</b>\n\n"
            "Every photo, video, voice, and video note you send will automatically be delivered as a <b>self-destructing</b> item!\n\n"
            "• Photos / Gifs / Documents: 15s viewing window\n"
            "• Videos / Voice notes: full clip duration + 5s buffer\n"
            "• Protected against screenshots, saving, and forwarding\n"
            "• Auto-destructs after viewing window expires\n\n"
            "<i>Type /onetime again to disable.</i>",
            parse_mode="HTML",
        )
    else:
        await message.answer(
            "🔓 <b>View-Once Mode: OFF</b>\n\n"
            "Media will now be sent normally.\n\n"
            "<i>Tip: You can still send individual view-once items anytime by adding <code>#once</code> to the caption or using Telegram's 'Hide with Spoiler' option.</i>",
            parse_mode="HTML",
        )


@router.message(
    (F.text & ~F.text.startswith("/"))
    | F.photo
    | F.sticker
    | F.voice
    | F.video_note
    | F.video
    | F.audio
    | F.document
    | F.animation
)
async def relay_anonymous_message(message: Message, bot: Bot, state: FSMContext) -> None:
    """Relay non-command messages anonymously between connected partners."""
    # If user is in an active FSM state (like reporting reason or admin penalty), do not relay
    current_state = await state.get_state()
    if current_state is not None:
        return

    # Ignore bot commands
    if message.text and message.text.startswith("/"):
        return

    user_id = message.from_user.id
    media_group_id = getattr(message, "media_group_id", None)

    # Anti-flood check (bypass check if message is part of an already-registering media group)
    if not media_batcher.is_collecting(media_group_id):
        is_allowed, reason, retry_after, should_notify = rate_limiter.check(user_id)
        if not is_allowed:
            if should_notify:
                if reason == "cooldown_triggered":
                    await message.answer(
                        "🛑 <b>Anti-flood protection triggered:</b> Too many rapid messages detected.\n\n"
                        "Messaging has been paused for 2 minutes to protect bot stability.",
                        parse_mode="HTML",
                    )
                elif reason == "cooldown":
                    await message.answer(
                        f"⏳ <b>Anti-flood cooldown active:</b> Please wait {int(retry_after)}s before sending messages.",
                        parse_mode="HTML",
                    )
                else:
                    await message.answer(
                        "⚠️ <b>Slow down!</b> You are sending messages too quickly. Please wait a moment.",
                        parse_mode="HTML",
                    )
            return

    async with get_db_session() as session:
        user = await get_or_create_user(session, user_id=user_id)
        partner_id = user.current_partner_id

        if partner_id is None:
            if user.is_searching:
                await message.answer(
                    "🔍 <b>You are in the search queue.</b>\n"
                    "Please wait for a partner to connect, or send /stop to cancel.",
                    parse_mode="HTML",
                )
            else:
                await message.answer(
                    "💬 <b>You are not currently in a chat.</b>\n\n"
                    "Send /search to find an anonymous partner! 🥷",
                    parse_mode="HTML",
                )
            return

        active_chat = await get_active_session_by_user_id(session, user_id=user_id)
        session_id = active_chat.id if active_chat else None
        was_paused = bool(active_chat and getattr(active_chat, "is_paused", False) is True)

        # Check file size limit for single media messages
        max_bytes = settings.MAX_MEDIA_FILE_SIZE_MB * 1024 * 1024
        if not media_group_id:
            file_size = get_message_file_size(message)
            if file_size and file_size > max_bytes:
                file_size_mb = file_size / (1024 * 1024)
                await message.answer(
                    f"⚠️ <b>File too large ({file_size_mb:.1f} MB).</b>\n\n"
                    f"To ensure fast delivery and protect mobile data, files are limited to {settings.MAX_MEDIA_FILE_SIZE_MB} MB. "
                    "Please compress or send a smaller file.",
                    parse_mode="HTML",
                )
                return

        # Handle album / media group batching
        if media_group_id:
            batch = await media_batcher.add_message(media_group_id, message)
            if batch is None:
                # Secondary album item debounced into batch; leader will relay all
                return

            # Check if any files in album exceed MAX_MEDIA_FILE_SIZE_MB
            oversized = [m for m in batch if (get_message_file_size(m) or 0) > max_bytes]
            if oversized:
                await message.answer(
                    f"⚠️ <b>One or more files in your album exceed {settings.MAX_MEDIA_FILE_SIZE_MB} MB.</b>\n\n"
                    "Please compress or remove oversized items before sending.",
                    parse_mode="HTML",
                )
                return

            # Scan album captions for unsafe links or IP-trackers
            for item in batch:
                album_scan = await link_shield.scan_message(item)
                if album_scan.is_blocked:
                    reason = html.escape(album_scan.block_reason)
                    await message.reply(
                        "🚫 <b>Album Blocked by Link Shield</b>\n\n"
                        "An item in your album was not delivered because it contains a link detected as an IP-tracker or unsafe download:\n"
                        f"<code>{reason}</code>\n\n"
                        "<i>Attempting to track, scam, or compromise students will result in an immediate permanent ban.</i>",
                        parse_mode="HTML",
                    )
                    return

            if was_paused and session_id:
                await _handle_awakening_if_paused(bot, session, session_id, was_paused, user_id, partner_id)
                was_paused = False

            # Check if album is designated as One-Time Media
            if any(one_time_media_manager.is_one_time_media(m, user_id) for m in batch):
                clean_cap = one_time_media_manager.clean_caption(batch[0].caption)
                duration = one_time_media_manager.calculate_duration(batch[0], batch)
                token = one_time_media_manager.register(
                    sender_id=user_id,
                    receiver_id=partner_id,
                    session_id=session_id,
                    message=batch[0],
                    batch=batch,
                    caption=clean_cap,
                    duration_seconds=duration,
                )
                try:
                    await batch[0].reply(
                        "🔒 <b>One-Time Album Sent!</b>\n\n"
                        f"Your partner received a protected placeholder for {len(batch)} items.\n"
                        f"Once revealed, it will self-destruct in {duration} seconds, and screenshots/saving are blocked.",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

                placeholder_text = (
                    f"🔒 <b>One-Time Media Album ({len(batch)} items)</b>\n\n"
                    "Your partner sent a self-destructing view-once album.\n"
                    f"Tap the button below to reveal it. Once revealed, you have <b>{duration} seconds</b> before it permanently self-destructs.\n\n"
                    "<i>🛡️ Screenshots, forwarding, and saving are strictly disabled.</i>"
                )
                try:
                    placeholder_msg = await bot.send_message(
                        chat_id=partner_id,
                        text=placeholder_text,
                        reply_markup=get_otm_prompt_keyboard(token, duration=duration),
                        parse_mode="HTML",
                    )
                    one_time_media_manager.set_placeholder_msg_id(token, placeholder_msg.message_id)
                    for item in batch:
                        await reply_manager.register_message(
                            sender_id=user_id,
                            sender_msg_id=item.message_id,
                            receiver_id=partner_id,
                            receiver_msg_id=placeholder_msg.message_id,
                            session=session,
                            session_id=session_id,
                        )
                except TelegramForbiddenError as e:
                    logger.warning("Partner %s blocked bot during OTM album relay: %s", partner_id, e)
                    await set_user_bot_blocked(session, partner_id, is_blocked=True)
                    result = await end_active_session(session, user_id=user_id)
                    if result:
                        chat_session, _ = result
                        await reply_manager.clear_chat(user_id, partner_id, session=session, session_id=chat_session.id)
                        one_time_media_manager.clear_session(chat_session.id)
                        await message.answer(
                            "⚠️ <b>Your partner has disconnected or left the chat.</b>\n\n"
                            "How was your conversation?",
                            reply_markup=get_feedback_keyboard(chat_session.id, partner_id),
                            parse_mode="HTML",
                        )
                        await maybe_send_bot_feedback_prompt(bot, session, user_id)
                except Exception as e:
                    logger.error("Failed to send OTM album placeholder to %s: %s", partner_id, e)

                if session_id:
                    await touch_chat_session_activity(session, session_id)
                return

            async with get_user_relay_lock(user_id):
                show_typing = getattr(user, "show_typing", True)
                if show_typing is None:
                    show_typing = True

                if show_typing:
                    action = get_chat_action_for_message(batch[0])
                    duration = get_emulated_typing_duration(batch[0]) if EMULATED_TYPING_SLEEP else 0.0
                    try:
                        await bot.send_chat_action(chat_id=partner_id, action=action)
                    except Exception as e:
                        logger.debug("Failed to send album chat action '%s' to partner %s: %s", action, partner_id, e)
                    if duration > 0:
                        await asyncio.sleep(duration)

                # Relay album batch preserving Telegram album grid layout
                try:
                    copied_msg_ids = await bot.copy_messages(
                        chat_id=partner_id,
                        from_chat_id=user_id,
                        message_ids=[m.message_id for m in batch],
                    )
                    for orig_msg, copied_id_obj in zip(batch, copied_msg_ids):
                        sent_msg_id = getattr(copied_id_obj, "message_id", None)
                        if sent_msg_id:
                            await reply_manager.register_message(
                                sender_id=user_id,
                                sender_msg_id=orig_msg.message_id,
                                receiver_id=partner_id,
                                receiver_msg_id=sent_msg_id,
                                session=session,
                                session_id=session_id,
                            )
                    if session_id:
                        await touch_chat_session_activity(session, session_id)
                    logger.info(
                        "Relayed album of %d messages from %s to %s",
                        len(batch),
                        user_id,
                        partner_id,
                    )
                except TelegramForbiddenError as e:
                    logger.warning("Partner %s blocked the bot or is deactivated during album relay: %s", partner_id, e)
                    await set_user_bot_blocked(session, partner_id, is_blocked=True)
                    result = await end_active_session(session, user_id=user_id)
                    if result:
                        chat_session, _ = result
                        await reply_manager.clear_chat(user_id, partner_id, session=session, session_id=chat_session.id)
                        one_time_media_manager.clear_session(chat_session.id)
                        await message.answer(
                            "⚠️ <b>Your partner has disconnected or left the chat.</b>\n\n"
                            "How was your conversation?",
                            reply_markup=get_feedback_keyboard(chat_session.id, partner_id),
                            parse_mode="HTML",
                        )
                        await maybe_send_bot_feedback_prompt(bot, session, user_id)
                except TelegramBadRequest as e:
                    err_msg = str(e).lower()
                    if any(term in err_msg for term in ["chat not found", "user is deactivated", "bot was blocked"]):
                        logger.warning("Partner %s unavailable during album relay (%s): %s", partner_id, err_msg, e)
                        result = await end_active_session(session, user_id=user_id)
                        if result:
                            chat_session, _ = result
                            await reply_manager.clear_chat(user_id, partner_id, session=session, session_id=chat_session.id)
                            one_time_media_manager.clear_session(chat_session.id)
                            await message.answer(
                                "⚠️ <b>Your partner has disconnected or left the chat.</b>\n\n"
                                "How was your conversation?",
                                reply_markup=get_feedback_keyboard(chat_session.id, partner_id),
                                parse_mode="HTML",
                            )
                            await maybe_send_bot_feedback_prompt(bot, session, user_id)
                    else:
                        logger.warning("bot.copy_messages failed for album (%s), falling back to individual copy_to", e)
                        for item in batch:
                            try:
                                sent_item = await item.copy_to(chat_id=partner_id)
                                sent_item_id = getattr(sent_item, "message_id", None)
                                if sent_item_id:
                                    await reply_manager.register_message(
                                        sender_id=user_id,
                                        sender_msg_id=item.message_id,
                                        receiver_id=partner_id,
                                        receiver_msg_id=sent_item_id,
                                        session=session,
                                        session_id=session_id,
                                    )
                            except Exception as item_err:
                                logger.error("Fallback copy_to failed for album item %s: %s", item.message_id, item_err)
                except Exception as e:
                    logger.error("Unexpected error relaying album from %s to %s: %s", user_id, partner_id, e)
            return

        # Check if incoming message is a reply to a previous message in the conversation
        reply_params: Optional[ReplyParameters] = None
        replied_msg = message.reply_to_message or message.external_reply
        replied_msg_id = getattr(replied_msg, "message_id", None) if replied_msg else None

        if replied_msg_id:
            target_msg_id = await reply_manager.get_target_message_id(
                sender_id=user_id,
                partner_id=partner_id,
                replied_msg_id=replied_msg_id,
                session=session,
            )
            is_self = bool(
                message.reply_to_message
                and message.reply_to_message.from_user
                and message.reply_to_message.from_user.id == user_id
            )
            logger.info(
                "Reply lookup: user %s replied to %s (self_reply=%s) -> target in %s is %s",
                user_id,
                replied_msg_id,
                is_self,
                partner_id,
                target_msg_id,
            )
            if target_msg_id:
                reply_params = ReplyParameters(
                    message_id=target_msg_id,
                    allow_sending_without_reply=True,
                )

        # Scan links with multi-tier Link Shield (IP-logger blocker + unshortener + malware check)
        scan_res = await link_shield.scan_message(message)
        if scan_res.is_blocked:
            reason = html.escape(scan_res.block_reason)
            await message.reply(
                "🚫 <b>Message Blocked by Link Shield</b>\n\n"
                "Your message was not delivered because it contains a link detected as a potential IP-tracker, phishing page, or unsafe file:\n"
                f"<code>{reason}</code>\n\n"
                "<i>Attempting to track, scam, or compromise students will result in an immediate permanent ban.</i>",
                parse_mode="HTML",
            )
            logger.warning(
                "Link Shield blocked message from user %s to %s: %s",
                user_id,
                partner_id,
                scan_res.block_reason,
            )
            return

        if was_paused and session_id:
            await _handle_awakening_if_paused(bot, session, session_id, was_paused, user_id, partner_id)
            was_paused = False

        # Check if single media message is designated as One-Time Media
        if one_time_media_manager.is_one_time_media(message, user_id):
            clean_cap = one_time_media_manager.clean_caption(message.caption)
            duration = one_time_media_manager.calculate_duration(message)
            token = one_time_media_manager.register(
                sender_id=user_id,
                receiver_id=partner_id,
                session_id=session_id,
                message=message,
                caption=clean_cap,
                duration_seconds=duration,
            )

            # Confirm to sender
            try:
                await message.reply(
                    "🔒 <b>One-Time Media Sent!</b>\n\n"
                    f"Your partner received a protected placeholder with a {duration}s reveal button.\n"
                    f"It will self-destruct {duration} seconds after they tap to reveal it, and screenshots/saving are blocked.",
                    parse_mode="HTML",
                )
            except Exception:
                pass

            media_type_label = "Media"
            if message.photo:
                media_type_label = "Photo"
            elif message.video:
                media_type_label = "Video"
            elif message.video_note:
                media_type_label = "Video Note"
            elif message.voice:
                media_type_label = "Voice Message"
            elif message.animation:
                media_type_label = "GIF / Animation"
            elif message.document:
                media_type_label = "File / Document"

            placeholder_text = (
                f"🔒 <b>One-Time {media_type_label}</b>\n\n"
                "Your partner sent a self-destructing view-once item.\n"
                f"Tap the button below to reveal it. Once revealed, you have <b>{duration} seconds</b> before it permanently self-destructs.\n\n"
                "<i>🛡️ Screenshots, forwarding, and saving are strictly disabled.</i>"
            )

            try:
                placeholder_msg = await bot.send_message(
                    chat_id=partner_id,
                    text=placeholder_text,
                    reply_markup=get_otm_prompt_keyboard(token, duration=duration),
                    reply_parameters=reply_params,
                    parse_mode="HTML",
                )
                one_time_media_manager.set_placeholder_msg_id(token, placeholder_msg.message_id)

                await reply_manager.register_message(
                    sender_id=user_id,
                    sender_msg_id=message.message_id,
                    receiver_id=partner_id,
                    receiver_msg_id=placeholder_msg.message_id,
                    session=session,
                    session_id=session_id,
                )
            except TelegramForbiddenError as e:
                logger.warning("Partner %s blocked bot during OTM relay: %s", partner_id, e)
                await set_user_bot_blocked(session, partner_id, is_blocked=True)
                result = await end_active_session(session, user_id=user_id)
                if result:
                    chat_session, _ = result
                    await reply_manager.clear_chat(user_id, partner_id, session=session, session_id=chat_session.id)
                    one_time_media_manager.clear_session(chat_session.id)
                    await message.answer(
                        "⚠️ <b>Your partner has disconnected or left the chat.</b>\n\n"
                        "How was your conversation?",
                        reply_markup=get_feedback_keyboard(chat_session.id, partner_id),
                        parse_mode="HTML",
                    )
                    await maybe_send_bot_feedback_prompt(bot, session, user_id)
            except Exception as e:
                logger.error("Failed to send OTM placeholder to %s: %s", partner_id, e)

            if session_id:
                await touch_chat_session_activity(session, session_id)
            return

        # Acquire per-user lock to maintain strict FIFO message ordering during typing emulation
        async with get_user_relay_lock(user_id):
            show_typing = getattr(user, "show_typing", True)
            if show_typing is None:
                show_typing = True

            if show_typing:
                action = get_chat_action_for_message(message)
                duration = get_emulated_typing_duration(message) if EMULATED_TYPING_SLEEP else 0.0
                try:
                    await bot.send_chat_action(chat_id=partner_id, action=action)
                except Exception as e:
                    logger.debug("Failed to send chat action '%s' to partner %s: %s", action, partner_id, e)
                if duration > 0:
                    await asyncio.sleep(duration)

            # Attempt to relay message to partner
            try:
                if reply_params:
                    try:
                        sent_msg = await message.copy_to(chat_id=partner_id, reply_parameters=reply_params)
                        logger.info(
                            "Relayed message with reply from %s to %s (target msg %s)",
                            user_id,
                            partner_id,
                            target_msg_id,
                        )
                    except TelegramBadRequest as e:
                        logger.warning(
                            "Telegram rejected reply_parameters for partner %s (msg %s): %s",
                            partner_id,
                            target_msg_id,
                            e,
                        )
                        if "reply" in str(e).lower() or "message to be replied not found" in str(e).lower():
                            sent_msg = await message.copy_to(chat_id=partner_id)
                        else:
                            raise
                else:
                    sent_msg = await message.copy_to(chat_id=partner_id)

                # Record message ID mapping for bidirectional reply support (both in-memory and PostgreSQL)
                sent_msg_id = getattr(sent_msg, "message_id", None)
                if sent_msg_id:
                    await reply_manager.register_message(
                        sender_id=user_id,
                        sender_msg_id=message.message_id,
                        receiver_id=partner_id,
                        receiver_msg_id=sent_msg_id,
                        session=session,
                        session_id=session_id,
                    )
                if session_id:
                    await touch_chat_session_activity(session, session_id)

                # Phishing & link safety advisory for recipient
                if scan_res.has_links and not scan_res.is_all_trusted:
                    domains_str = ", ".join(f"<code>{html.escape(d)}</code>" for d in scan_res.final_domains[:3]) or "external website"
                    unshortened_notes = ""
                    if scan_res.unshortened_mappings:
                        unshortened_notes = "\n".join(
                            f"ℹ️ <i>Short link expanded: {html.escape(k)} ➔ <code>{html.escape(v)}</code></i>"
                            for k, v in list(scan_res.unshortened_mappings.items())[:2]
                        ) + "\n\n"

                    try:
                        await bot.send_message(
                            chat_id=partner_id,
                            text=(
                                "⚠️ <b>Security Notice: External Link</b>\n\n"
                                f"Your partner sent an external link to: {domains_str}\n\n"
                                f"{unshortened_notes}"
                                "• Never enter your Telegram code, passwords, or personal credentials.\n"
                                "• Telegram never shares your IP or identity unless you click external links.\n"
                                "• If this person behaves suspiciously, type /report to immediately block them."
                            ),
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass
            except TelegramForbiddenError as e:
                logger.warning("Partner %s blocked the bot or is deactivated: %s", partner_id, e)
                await set_user_bot_blocked(session, partner_id, is_blocked=True)
                result = await end_active_session(session, user_id=user_id)
                if result:
                    chat_session, _ = result
                    await reply_manager.clear_chat(user_id, partner_id, session=session, session_id=chat_session.id)
                    one_time_media_manager.clear_session(chat_session.id)
                    await message.answer(
                        "⚠️ <b>Your partner has disconnected or left the chat.</b>\n\n"
                        "How was your conversation?",
                        reply_markup=get_feedback_keyboard(chat_session.id, partner_id),
                        parse_mode="HTML",
                    )
                    await maybe_send_bot_feedback_prompt(bot, session, user_id)
            except TelegramBadRequest as e:
                err_msg = str(e).lower()
                if any(term in err_msg for term in ["chat not found", "user is deactivated", "bot was blocked"]):
                    logger.warning("Partner %s unavailable (%s): %s", partner_id, err_msg, e)
                    result = await end_active_session(session, user_id=user_id)
                    if result:
                        chat_session, _ = result
                        await reply_manager.clear_chat(user_id, partner_id, session=session, session_id=chat_session.id)
                        one_time_media_manager.clear_session(chat_session.id)
                        await message.answer(
                            "⚠️ <b>Your partner has disconnected or left the chat.</b>\n\n"
                            "How was your conversation?",
                            reply_markup=get_feedback_keyboard(chat_session.id, partner_id),
                            parse_mode="HTML",
                        )
                        await maybe_send_bot_feedback_prompt(bot, session, user_id)
                else:
                    logger.error("Failed to relay message from %s to %s (bad request): %s", user_id, partner_id, e)
                    await message.answer(
                        "⚠️ <i>Message could not be delivered to your partner. Please try again.</i>",
                        parse_mode="HTML",
                    )
            except TelegramAPIError as e:
                # Temporary network or Telegram server error: do NOT terminate the active session!
                logger.error("Temporary Telegram API error while relaying from %s to %s: %s", user_id, partner_id, e)
                await message.answer(
                    "⚠️ <i>Temporary connection issue delivering message. Your chat is still active. Please resend.</i>",
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.error("Unexpected error while relaying from %s to %s: %s", user_id, partner_id, e)


@router.message_reaction()
async def relay_message_reaction(event: MessageReactionUpdated, bot: Bot) -> None:
    """Relay emoji reactions on messages between connected anonymous partners."""
    user = event.user
    if not user:
        return

    user_id = user.id

    async with get_db_session() as session:
        db_user = await get_user(session, user_id=user_id)
        if not db_user or not db_user.current_partner_id:
            return
        partner_id = db_user.current_partner_id

    # Find corresponding message ID in partner's chat (check memory then DB)
    target_msg_id = await reply_manager.get_target_message_id(
        sender_id=user_id,
        partner_id=partner_id,
        replied_msg_id=event.message_id,
        session=session,
    )
    if not target_msg_id:
        logger.debug(
            "Reaction ignored: no mapped partner message for user %s, msg %s",
            user_id,
            event.message_id,
        )
        return

    try:
        # Pass new_reaction (empty list removes/clears reaction on Telegram)
        reaction_to_set = event.new_reaction if event.new_reaction is not None else []
        await bot.set_message_reaction(
            chat_id=partner_id,
            message_id=target_msg_id,
            reaction=reaction_to_set,
        )
        logger.info(
            "Relayed reaction from user %s to partner %s on msg %s (target %s)",
            user_id,
            partner_id,
            event.message_id,
            target_msg_id,
        )
    except TelegramBadRequest as e:
        logger.warning(
            "Could not set reaction for partner %s on msg %s: %s",
            partner_id,
            target_msg_id,
            e,
        )
    except TelegramAPIError as e:
        logger.error(
            "Telegram API error setting reaction for partner %s: %s",
            partner_id,
            e,
        )
    except Exception as e:
        logger.exception("Unexpected error relaying reaction: %s", e)


@router.edited_message()
async def relay_edited_message(message: Message, bot: Bot, state: FSMContext) -> None:
    """Relay message edits (text or media captions) anonymously to partner."""
    # If user is in an active FSM state (like reporting reason or admin penalty), do not relay
    current_state = await state.get_state()
    if current_state is not None:
        return

    # Ignore command edits
    if message.text and message.text.startswith("/"):
        return

    if not message.from_user:
        return

    user_id = message.from_user.id

    async with get_db_session() as session:
        db_user = await get_user(session, user_id=user_id)
        if not db_user or not db_user.current_partner_id:
            return
        partner_id = db_user.current_partner_id

    # Resolve corresponding message ID in partner's chat (check memory then DB)
    target_msg_id = await reply_manager.get_target_message_id(
        sender_id=user_id,
        partner_id=partner_id,
        replied_msg_id=message.message_id,
        session=session,
    )
    if not target_msg_id:
        logger.debug(
            "Edited message ignored: no mapped partner message for user %s, msg %s",
            user_id,
            message.message_id,
        )
        return

    # Verify link safety on edited message
    edit_scan = await link_shield.scan_message(message)
    if edit_scan.is_blocked:
        reason = html.escape(edit_scan.block_reason)
        await message.reply(
            "🚫 <b>Edit Blocked by Link Shield</b>\n\n"
            "Your edited message contains a link detected as an unsafe URL or IP-tracker:\n"
            f"<code>{reason}</code>\n\n"
            "<i>The edit was not relayed to your partner.</i>",
            parse_mode="HTML",
        )
        return

    try:
        if message.text is not None:
            await bot.edit_message_text(
                text=message.text,
                chat_id=partner_id,
                message_id=target_msg_id,
                entities=message.entities,
                parse_mode=None,
            )
            logger.info(
                "Relayed edited text from user %s to partner %s (msg %s -> target %s)",
                user_id,
                partner_id,
                message.message_id,
                target_msg_id,
            )
        elif message.caption is not None or any([
            message.photo,
            message.video,
            message.document,
            message.audio,
            message.animation,
            message.voice,
        ]):
            await bot.edit_message_caption(
                caption=message.caption or "",
                chat_id=partner_id,
                message_id=target_msg_id,
                caption_entities=message.caption_entities,
                parse_mode=None,
            )
            logger.info(
                "Relayed edited caption from user %s to partner %s (msg %s -> target %s)",
                user_id,
                partner_id,
                message.message_id,
                target_msg_id,
            )
    except TelegramBadRequest as e:
        err_msg = str(e).lower()
        if "message is not modified" in err_msg:
            logger.debug("Edit ignored: message content unchanged for partner %s msg %s", partner_id, target_msg_id)
        elif any(term in err_msg for term in ["chat not found", "user is deactivated", "bot was blocked"]):
            logger.warning("Partner %s unavailable during edit (%s): %s", partner_id, err_msg, e)
        else:
            logger.warning(
                "Could not edit message for partner %s on msg %s: %s",
                partner_id,
                target_msg_id,
                e,
            )
    except TelegramAPIError as e:
        logger.error(
            "Telegram API error editing message for partner %s: %s",
            partner_id,
            e,
        )
    except Exception as e:
        logger.exception("Unexpected error relaying edited message: %s", e)


@router.callback_query(F.data.startswith("inact_keep:"))
async def handle_inactivity_keep(callback: CallbackQuery, bot: Bot) -> None:
    """User acknowledges presence and resets the inactivity timer."""
    parts = callback.data.split(":")
    session_id = int(parts[1])
    user_id = callback.from_user.id

    async with get_db_session() as session:
        db_user = await get_user(session, user_id=user_id)
        if not db_user or not db_user.current_partner_id:
            await callback.answer("This chat is no longer active.", show_alert=True)
            try:
                await callback.message.edit_text("⚠️ <b>This chat is no longer active.</b>", reply_markup=None, parse_mode="HTML")
            except Exception:
                pass
            return

        partner_id = db_user.current_partner_id
        await reset_chat_session_inactivity_warning(session, session_id)

    try:
        await callback.message.edit_text(
            "✅ <b>Noted! Timer reset. Happy chatting!</b> 🎭\n\n"
            "<i>Your partner was notified that you're here.</i>",
            reply_markup=None,
            parse_mode="HTML",
        )
    except Exception:
        pass

    try:
        await bot.send_message(
            chat_id=partner_id,
            text="💬 <b>Your partner confirmed they are here! Say hi ×͜×</b>",
            parse_mode="HTML",
        )
    except Exception:
        pass

    await callback.answer("Presence confirmed! Timer reset. 👍")


@router.callback_query(F.data.startswith("inact_pause:"))
async def handle_inactivity_pause(callback: CallbackQuery, bot: Bot) -> None:
    """User puts the chat into Sleep Mode / Pause from inactivity prompt."""
    parts = callback.data.split(":")
    session_id = int(parts[1])
    user_id = callback.from_user.id

    async with get_db_session() as session:
        db_user = await get_user(session, user_id=user_id)
        if not db_user or not db_user.current_partner_id:
            await callback.answer("This chat is no longer active.", show_alert=True)
            try:
                await callback.message.edit_text("⚠️ <b>This chat is no longer active.</b>", reply_markup=None, parse_mode="HTML")
            except Exception:
                pass
            return

        partner_id = db_user.current_partner_id
        await pause_chat_session(session, session_id)

    try:
        await callback.message.edit_text(
            "🌙 <b>Chat placed in Sleep Mode!</b> 💤\n\n"
            "• Inactivity auto-closure is frozen for up to 24 hours.\n"
            "• You won't lose this connection overnight.\n"
            "• <b>To resume:</b> Simply send any message, and chat will instantly awaken! ☀️",
            reply_markup=None,
            parse_mode="HTML",
        )
    except Exception:
        pass

    try:
        await bot.send_message(
            chat_id=partner_id,
            text=(
                "🌙 <b>Your partner put this chat into Sleep Mode!</b> 💤\n\n"
                "• Inactivity timer is frozen for up to 24 hours so you won't lose each other.\n"
                "• Send any message whenever you want to resume chatting! ☀️\n"
                "• Or send /stop if you want to end this chat."
            ),
            parse_mode="HTML",
        )
    except Exception:
        pass

    await callback.answer("Sleep Mode active! 🌙")


@router.callback_query(F.data.startswith("inact_next:"))
async def handle_inactivity_next(callback: CallbackQuery, bot: Bot) -> None:
    """User skips inactive chat and immediately queues for next partner."""
    user_id = callback.from_user.id

    async with get_db_session() as session:
        result = await end_active_session(session, user_id=user_id)
        if not result:
            await callback.answer("This chat has already ended.", show_alert=True)
            try:
                await callback.message.edit_text("⚠️ <b>This chat is no longer active.</b>", reply_markup=None, parse_mode="HTML")
            except Exception:
                pass
            return

        chat_session, partner_id = result
        await reply_manager.clear_chat(user_id, partner_id, session=session, session_id=chat_session.id)
        one_time_media_manager.clear_session(chat_session.id)

        # Notify partner
        try:
            await bot.send_message(
                chat_id=partner_id,
                text="⚠️ <b>Your partner skipped to the next chat.</b>\n\nPress /search to find someone new! ×͜×",
                parse_mode="HTML",
            )
        except Exception:
            pass

        try:
            await callback.message.edit_text(
                "⏭️ <b>Skipping to next partner...</b> 🥷",
                reply_markup=None,
                parse_mode="HTML",
            )
        except Exception:
            pass

        # Enter queue
        db_user = await get_user(session, user_id=user_id)
        candidate, new_session = await match_user_in_queue(session, current_user=db_user)

    await callback.answer()

    if candidate and new_session:
        # Match found! Notify both
        match_msg = (
            "🎉 <b>Partner found! Say hi!</b> 🎭\n\n"
            "🔒 <i>Privacy Shield Active: Your profile, phone number, and IP are strictly hidden.</i>\n\n"
            "• Use /next to skip\n"
            "• Use /stop to end\n"
            "• Use /report for emergency disconnect"
        )
        for uid in (user_id, candidate.id):
            try:
                await bot.send_message(chat_id=uid, text=match_msg, parse_mode="HTML")
            except Exception:
                pass
    else:
        try:
            await bot.send_message(
                chat_id=user_id,
                text=(
                    "🔍 <b>Searching for an active campus partner...</b> 🥷\n\n"
                    "Please wait a moment. Send /stop to cancel."
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass


@router.callback_query(F.data.startswith("inact_stop:"))
async def handle_inactivity_stop(callback: CallbackQuery, bot: Bot) -> None:
    """User ends inactive chat."""
    user_id = callback.from_user.id

    async with get_db_session() as session:
        result = await end_active_session(session, user_id=user_id)
        if not result:
            await callback.answer("This chat has already ended.", show_alert=True)
            try:
                await callback.message.edit_text("⚠️ <b>This chat is no longer active.</b>", reply_markup=None, parse_mode="HTML")
            except Exception:
                pass
            return

        chat_session, partner_id = result
        await reply_manager.clear_chat(user_id, partner_id, session=session, session_id=chat_session.id)
        one_time_media_manager.clear_session(chat_session.id)

    try:
        await callback.message.edit_text(
            "🛑 <b>Conversation ended.</b>\n\n"
            "Press /search when you are ready to chat with someone new! ×͜×",
            reply_markup=None,
            parse_mode="HTML",
        )
    except Exception:
        pass

    try:
        await bot.send_message(
            chat_id=partner_id,
            text="⚠️ <b>Your partner ended the conversation.</b>\n\nPress /search to find someone new! ×͜×",
            parse_mode="HTML",
        )
    except Exception:
        pass

    await callback.answer("Chat ended.")


# ---------------------------------------------------------
# Ephemeral One-Time (View-Once) Media Handlers
# ---------------------------------------------------------


@router.callback_query(F.data.startswith("otm_rev:"))
async def handle_otm_reveal(callback: CallbackQuery, bot: Bot) -> None:
    """Handle recipient tapping 'Tap to Reveal (15s)' on one-time media placeholder."""
    token = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id

    data = one_time_media_manager.get(token)
    if not data:
        await callback.answer("⚠️ This one-time media has expired or is no longer available.", show_alert=True)
        if callback.message:
            try:
                await callback.message.edit_text(
                    "💥 <b>One-Time Media Expired</b>\n\n<i>This media has already expired.</i>",
                    reply_markup=None,
                    parse_mode="HTML",
                )
            except Exception:
                pass
        return

    if data["receiver_id"] != user_id:
        await callback.answer("⚠️ Only the designated recipient can view this media.", show_alert=True)
        return

    if data["viewed"]:
        await callback.answer("⚠️ This media is already being viewed or has expired.", show_alert=True)
        return

    # Check that partner is still connected in an active chat
    async with get_db_session() as session:
        db_user = await get_user(session, user_id=user_id)
        if not db_user or db_user.current_partner_id != data["sender_id"]:
            await callback.answer("⚠️ Chat has ended. Cannot view media.", show_alert=True)
            if callback.message:
                try:
                    await callback.message.edit_text(
                        "💥 <b>One-Time Media Expired</b>\n\n<i>This chat has already ended.</i>",
                        reply_markup=None,
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
            one_time_media_manager.clear_token(token)
            return

    # Mark as viewed
    one_time_media_manager.mark_viewed(token)

    duration = data.get("duration_seconds", DEFAULT_OTM_DURATION_SECONDS)

    # Update placeholder status in recipient's chat
    if callback.message:
        try:
            await callback.message.edit_text(
                "👁️ <b>Viewing One-Time Media...</b>\n\n"
                f"⏳ <b>Self-destructing in {duration} seconds...</b>\n"
                "<i>Screenshots, forwarding, and saving are blocked.</i>",
                reply_markup=get_otm_viewing_keyboard(duration=duration),
                parse_mode="HTML",
            )
        except Exception as e:
            logger.debug("Failed to edit OTM placeholder during reveal: %s", e)

    await callback.answer(f"Media revealed! Self-destructing in {duration} seconds.", show_alert=False)

    # Deliver media with protect_content=True
    revealed_msg_ids = []
    batch = data.get("batch")
    clean_cap = data.get("caption")

    if batch:
        for idx, item in enumerate(batch):
            cap = clean_cap if idx == 0 else None
            try:
                sent = await item.copy_to(
                    chat_id=user_id,
                    caption=cap,
                    protect_content=True,
                )
                if sent and getattr(sent, "message_id", None):
                    revealed_msg_ids.append(sent.message_id)
            except Exception as e:
                logger.error("Failed to copy one-time album item %s to %s: %s", item.message_id, user_id, e)
    else:
        orig_msg = data["message"]
        try:
            sent = await orig_msg.copy_to(
                chat_id=user_id,
                caption=clean_cap,
                protect_content=True,
            )
            if sent and getattr(sent, "message_id", None):
                revealed_msg_ids.append(sent.message_id)
        except Exception as e:
            logger.error("Failed to copy one-time media message to %s: %s", user_id, e)

    # Notify sender that media was opened
    try:
        await bot.send_message(
            chat_id=data["sender_id"],
            text=(
                "👁️ <b>Your one-time media was opened!</b>\n\n"
                f"It is currently being viewed and will self-destruct in {duration} seconds."
            ),
            parse_mode="HTML",
        )
    except Exception:
        pass

    # Launch background auto-destruct task
    placeholder_msg_id = callback.message.message_id if callback.message else data.get("placeholder_msg_id")
    asyncio.create_task(
        _auto_destruct_one_time_media(
            bot=bot,
            token=token,
            receiver_id=user_id,
            sender_id=data["sender_id"],
            revealed_msg_ids=revealed_msg_ids,
            placeholder_msg_id=placeholder_msg_id,
            duration_seconds=duration,
        )
    )


async def _auto_destruct_one_time_media(
    bot: Bot,
    token: str,
    receiver_id: int,
    sender_id: int,
    revealed_msg_ids: list[int],
    placeholder_msg_id: Optional[int],
    duration_seconds: int = DEFAULT_OTM_DURATION_SECONDS,
) -> None:
    """Wait duration_seconds, delete revealed media, edit placeholder to expired, and clean up token."""
    try:
        await asyncio.sleep(duration_seconds)
    except asyncio.CancelledError:
        pass
    finally:
        # 1. Delete revealed media messages from recipient's chat
        for msg_id in revealed_msg_ids:
            try:
                await bot.delete_message(chat_id=receiver_id, message_id=msg_id)
            except Exception as e:
                logger.debug("Failed to delete revealed OTM message %s for user %s: %s", msg_id, receiver_id, e)

        # 2. Update placeholder message to expired state
        if placeholder_msg_id:
            try:
                await bot.edit_message_text(
                    chat_id=receiver_id,
                    message_id=placeholder_msg_id,
                    text=(
                        "💥 <b>One-Time Media Expired</b>\n\n"
                        "<i>This media was viewed and has permanently self-destructed.</i>"
                    ),
                    reply_markup=None,
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.debug("Failed to edit placeholder message %s: %s", placeholder_msg_id, e)

        # 3. Notify sender that media self-destructed
        try:
            await bot.send_message(
                chat_id=sender_id,
                text=f"💥 <b>One-time media destroyed.</b>\n\nThe {duration_seconds}-second viewing window expired and the media was permanently deleted.",
                parse_mode="HTML",
            )
        except Exception:
            pass

        # 4. Clean up token
        one_time_media_manager.clear_token(token)


@router.callback_query(F.data == "otm_noop")
async def handle_otm_noop(callback: CallbackQuery) -> None:
    """No-op callback while media is in viewing countdown."""
    await callback.answer("⏳ Media is currently in self-destruct countdown...", show_alert=False)



