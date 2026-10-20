"""Matchmaking handlers (/search, /stop, /next) and anonymous message relay."""

import asyncio
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
from aiogram.types import Message, MessageReactionUpdated, ReplyParameters
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud import (
    end_active_session,
    get_active_session_by_user_id,
    get_latest_session_for_user,
    get_or_create_user,
    get_user,
    set_user_searching,
    should_prompt_bot_feedback,
)
from app.database.session import get_db_session
from app.keyboards.inline import (
    get_bot_feedback_keyboard,
    get_emergency_report_keyboard,
    get_feedback_keyboard,
)
from app.services.matcher import match_user_in_queue
from app.services.media_batcher import media_batcher
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
        match_msg = (
            "✨ <b>Partner found!</b> 🎭\n\n"
            "You are now connected anonymously. Say hello! 👾\n\n"
            "⚡ <b>Commands:</b>\n"
            "• /report — Emergency report & block partner\n"
            "• /next — Skip to the next partner\n"
            "• /stop — End this chat\n\n"
            "🔒 <i>Privacy Shield Active: Your profile, phone number, and IP are strictly hidden.</i>"
        )
        # Notify initiating user
        await message.answer(match_msg, parse_mode="HTML")
        # Notify matched partner
        try:
            await bot.send_message(chat_id=partner.id, text=match_msg, parse_mode="HTML")
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
    """Handle /report command during an active chat or after a chat to report partner."""
    user_id = message.from_user.id
    async with get_db_session() as session:
        user = await get_or_create_user(session, user_id=user_id)
        partner_id = user.current_partner_id
        active_chat = await get_active_session_by_user_id(session, user_id=user_id) if partner_id else None

        if partner_id and active_chat:
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

        # User is not in active chat: look up latest session
        latest_session = await get_latest_session_for_user(session, user_id=user_id)
        if latest_session:
            rep_id = latest_session.user2_id if latest_session.user1_id == user_id else latest_session.user1_id
            await state.set_state(ReportState.waiting_for_reason)
            await state.update_data(session_id=latest_session.id, reported_id=rep_id)
            await message.answer(
                "🚨 <b>Report Previous Partner</b> 🛡️\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "Reporting your last chat partner.\n\n"
                "Please describe what happened (blackmail, threats, harassment, scam) or <b>attach a screenshot</b> as evidence:\n\n"
                "🔒 <i>Your identity is 100% private. (Send /cancel to abort)</i>",
                parse_mode="HTML",
            )
            return

    await message.answer(
        "💬 <b>No recent chat found to report.</b>\n\n"
        "You can use /report whenever you are in an active chat or after completing a conversation.",
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


@router.message(F.text | F.photo | F.sticker | F.voice | F.video_note | F.video | F.audio | F.document | F.animation)
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
                    logger.info(
                        "Relayed album of %d messages from %s to %s",
                        len(batch),
                        user_id,
                        partner_id,
                    )
                except TelegramForbiddenError as e:
                    logger.warning("Partner %s blocked the bot or is deactivated during album relay: %s", partner_id, e)
                    result = await end_active_session(session, user_id=user_id)
                    if result:
                        chat_session, _ = result
                        await reply_manager.clear_chat(user_id, partner_id, session=session, session_id=chat_session.id)
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

                # Phishing & link safety warning for recipient
                text_to_scan = (message.text or message.caption or "").lower()
                suspicious_links = ["http://", "https://", "t.me/", "telegram.me/", "bit.ly", "tinyurl", "iplogger", "grabify", "2no.co"]
                if any(link in text_to_scan for link in suspicious_links):
                    try:
                        await bot.send_message(
                            chat_id=partner_id,
                            text=(
                                "⚠️ <b>Security Notice:</b> Your partner sent an external link.\n\n"
                                "• Never enter passwords or personal data on unfamiliar pages.\n"
                                "• Telegram never shares your IP or identity unless you click external links.\n"
                                "• If this person behaves suspiciously, type /report to immediately block them."
                            ),
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass
            except TelegramForbiddenError as e:
                logger.warning("Partner %s blocked the bot or is deactivated: %s", partner_id, e)
                result = await end_active_session(session, user_id=user_id)
                if result:
                    chat_session, _ = result
                    await reply_manager.clear_chat(user_id, partner_id, session=session, session_id=chat_session.id)
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


