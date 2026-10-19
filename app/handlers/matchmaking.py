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

from app.database.crud import (
    end_active_session,
    get_active_session_by_user_id,
    get_or_create_user,
    get_user,
    set_user_searching,
    should_prompt_bot_feedback,
)
from app.database.session import get_db_session
from app.keyboards.inline import get_bot_feedback_keyboard, get_feedback_keyboard
from app.services.matcher import match_user_in_queue
from app.services.reply_manager import reply_manager

logger = logging.getLogger(__name__)
router = Router(name="matchmaking_router")

EMULATED_TYPING_SLEEP = True
_user_relay_locks: Dict[int, asyncio.Lock] = {}


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
                        "⏳ <b>Still looking for a partner...</b> 🦅🇦🇲\n\n"
                        "<b>Anonymous AUA</b> was recently released, so there might be fewer students active at this exact minute!\n\n"
                        "📢 <b>Help grow our campus community!</b>\n"
                        "Share the bot with your AUA classmates and group chats so more people join:\n"
                        "👉 <b>https://t.me/aua_anonymous_bot</b>\n\n"
                        "The more students join, the faster matches happen! You remain in the search queue and will be connected as soon as someone joins.\n\n"
                        "<i>(Send /stop at any time to cancel)</i>"
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
                    "💡 <b>Help Us Improve Anonymous AUA!</b> 🦅🇦🇲\n\n"
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
            "✨ <b>Partner found!</b> 🇦🇲🦅\n\n"
            "You are now connected anonymously. Say hello!\n\n"
            "⚡ <b>Commands:</b>\n"
            "• /next — Skip to the next partner\n"
            "• /stop — End this chat"
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
            "🔍 <b>Searching for a compatible AUA partner...</b> 🦅\n\n"
            "Please wait while we connect you, or send /stop to cancel.",
            parse_mode="HTML",
        )
        # Schedule the 30-second notification if still searching
        asyncio.create_task(schedule_search_notice(bot, user_id))


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
                "Send /search whenever you are ready to find a partner! 🦅🇦🇲",
                parse_mode="HTML",
            )
            return

    # 3. Neither in chat nor searching
    await message.answer(
        "💬 You are not currently in an active chat or search.\n\n"
        "Send /search to find someone to chat with! 🦅",
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
                    "Send /search to find an anonymous partner! 🦅🇦🇲",
                    parse_mode="HTML",
                )
            return

        # Check if incoming message is a reply to a previous message in the conversation
        reply_params: Optional[ReplyParameters] = None
        replied_msg = message.reply_to_message or message.external_reply
        replied_msg_id = getattr(replied_msg, "message_id", None) if replied_msg else None

        active_chat = await get_active_session_by_user_id(session, user_id=user_id)
        session_id = active_chat.id if active_chat else None

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


