"""Personal Anonymous Message Inbox (/link) handlers and two-way threaded replies."""

import html
import io
import logging
import random
from typing import Optional

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.config import settings
from app.database.crud import (
    add_or_update_user_block,
    apply_admin_penalty,
    ban_user_by_admin,
    block_inbox_sender,
    create_or_regenerate_inbox,
    create_report,
    deactivate_user_inbox,
    get_inbox_by_token,
    get_inbox_cooldown_remaining,
    get_inbox_message_by_thread,
    get_or_create_user,
    get_user,
    get_user_active_inbox,
    is_inbox_sender_blocked,
    mark_inbox_message_replied,
    record_inbox_message,
)
from app.database.models import User
from app.database.session import get_db_session
from app.keyboards.inline import (
    get_inbox_block_confirm_keyboard,
    get_inbox_message_keyboard,
    get_inbox_owner_keyboard,
    get_inbox_prompt_keyboard,
    get_inbox_reply_keyboard,
    get_inbox_report_confirm_keyboard,
)
from app.services.ai_moderator import SessionTurn, gemini_moderator, session_buffer
from app.services.ban_manager import add_banned_user
from app.services.link_shield import link_shield
from app.states.states import InboxState
from sqlalchemy import select

logger = logging.getLogger(__name__)
router = Router(name="inbox_router")

RANDOM_QUESTIONS = [
    "What's your honest opinion on the AUA cafeteria coffee? ☕",
    "What's the funniest thing you've ever witnessed during finals week? 📚",
    "Who was your first campus crush at AUA? 🤫",
    "What's your biggest guilty pleasure when you should be studying? 🍕",
    "If you could change one rule at AUA, what would it be? 🏛️",
    "What's the most awkward elevator encounter you've had on campus? 🛗",
    "What's one secret hobby you have that almost nobody knows about? 🎨",
    "If you could take any professor out for drinks, who would it be? 🍹",
    "What is your comfort song after failing a midterm? 🎧",
    "Rate your sleep schedule on a scale from 1 to 10 right now. 💤",
    "What's the best advice you'd give to an incoming freshman? 🎓",
    "What's your most memorable memory at AUA so far? ✨",
]


@router.message(Command("link", "inbox"))
async def handle_link_command(message: Message, bot: Bot) -> None:
    """Display user's active personal anonymous message link or prompt creation."""
    user_id = message.from_user.id
    username = message.from_user.username

    async with get_db_session() as session:
        await get_or_create_user(session, user_id=user_id, username=username)
        active_inbox = await get_user_active_inbox(session, user_id=user_id)

    bot_info = await bot.get_me()
    bot_username = bot_info.username or "aua_anonymous_bot"

    if active_inbox:
        link_url = f"https://t.me/{bot_username}?start=msg_{active_inbox.token}"
        text = (
            "🎭 <b>Receive anonymous messages & questions!</b>\n\n"
            f"🔗 <b>Your personal link:</b>\n<code>{link_url}</code>\n\n"
            "☝️ <i>Post this link in your Telegram bio, channel, or Instagram stories!</i>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "🗑️ <i>Want to deactivate this link? Use the button below to destroy it.</i>"
        )
        kb = get_inbox_owner_keyboard(has_active_link=True, token=active_inbox.token, bot_username=bot_username)
    else:
        text = (
            "🎭 <b>Receive anonymous messages & questions!</b>\n\n"
            "You don't currently have an active anonymous inbox link.\n\n"
            "Tap <b>➕ Generate New Link</b> below to get your personal link and start receiving anonymous messages!"
        )
        kb = get_inbox_owner_keyboard(has_active_link=False, bot_username=bot_username)

    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "inbox_generate")
async def handle_inbox_generate(callback: CallbackQuery, bot: Bot) -> None:
    """Generate or regenerate a new anonymous message link."""
    user_id = callback.from_user.id
    async with get_db_session() as session:
        inbox = await create_or_regenerate_inbox(session, user_id=user_id)

    bot_info = await bot.get_me()
    bot_username = bot_info.username or "aua_anonymous_bot"
    link_url = f"https://t.me/{bot_username}?start=msg_{inbox.token}"

    text = (
        "🎭 <b>Receive anonymous messages & questions!</b>\n\n"
        f"🔗 <b>Your personal link:</b>\n<code>{link_url}</code>\n\n"
        "☝️ <i>Post this link in your Telegram bio, channel, or Instagram stories!</i>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🗑️ <i>Want to deactivate this link? Use the button below to destroy it.</i>"
    )
    kb = get_inbox_owner_keyboard(has_active_link=True, token=inbox.token, bot_username=bot_username)
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer("New anonymous link generated!")


@router.callback_query(F.data == "inbox_destroy")
async def handle_inbox_destroy(callback: CallbackQuery, bot: Bot) -> None:
    """Destroy/deactivate the user's active anonymous message link."""
    user_id = callback.from_user.id
    async with get_db_session() as session:
        await deactivate_user_inbox(session, user_id=user_id)

    bot_info = await bot.get_me()
    bot_username = bot_info.username or "aua_anonymous_bot"

    text = (
        "🗑️ <b>Anonymous Link Destroyed!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Your previous link has been permanently deactivated. Anyone opening it will no longer be able to message you.\n\n"
        "Tap <b>➕ Generate New Link</b> whenever you wish to create a fresh one!"
    )
    kb = get_inbox_owner_keyboard(has_active_link=False, bot_username=bot_username)
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer("Link destroyed.", show_alert=True)


async def handle_inbox_deeplink(
    message: Message,
    token: str,
    state: FSMContext,
    bot: Bot,
) -> None:
    """Handle entry when someone clicks an anonymous inbox link (msg_<token>)."""
    user_id = message.from_user.id
    username = message.from_user.username

    async with get_db_session() as session:
        await get_or_create_user(session, user_id=user_id, username=username)
        inbox = await get_inbox_by_token(session, token=token)

        if not inbox or not inbox.is_active:
            await state.clear()
            await message.answer(
                "⚠️ <b>Link Expired or Deactivated</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "The owner has destroyed or deactivated this anonymous message link.\n\n"
                "Send /link to generate your own anonymous inbox! 🎭",
                parse_mode="HTML",
            )
            return

        if user_id == inbox.user_id:
            await state.clear()
            await message.answer(
                "😄 <b>This is your own anonymous link!</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "Share it on your social media, Telegram bio, or stories so people can send you anonymous questions and messages!\n\n"
                "Send /link to view or manage your link.",
                parse_mode="HTML",
            )
            return

        # Check if sender is blocked by owner
        if await is_inbox_sender_blocked(session, owner_id=inbox.user_id, sender_id=user_id):
            await state.clear()
            await message.answer(
                "⚠️ <b>Unavailable</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "You cannot send anonymous messages to this user right now.",
                parse_mode="HTML",
            )
            return

        # Check 20-minute cooldown
        remaining = await get_inbox_cooldown_remaining(
            session, owner_id=inbox.user_id, sender_id=user_id, cooldown_seconds=1200
        )
        if remaining > 0:
            mins = int(remaining // 60) + 1
            await state.clear()
            await message.answer(
                f"⏳ <b>Cooldown Active (20 min)</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "You recently sent an anonymous message to this student.\n"
                f"To prevent spam, please wait <b>{mins} minute(s)</b> before sending another message.",
                parse_mode="HTML",
            )
            return

    # Ready for composition
    await state.set_state(InboxState.waiting_for_message)
    await state.update_data(owner_id=inbox.user_id, inbox_token=token)

    prompt = (
        "🎭 <b>Send an Anonymous Message!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Write your secret message, confession, or question below.\n"
        "You can send <b>text, a photo, or a voice message</b>!\n\n"
        "🔒 <i>100% Anonymous: The recipient will never know who sent it.</i>\n\n"
        "<i>(Send /cancel to abort)</i>"
    )
    await message.answer(prompt, reply_markup=get_inbox_prompt_keyboard(), parse_mode="HTML")


@router.callback_query(F.data == "inbox_cancel")
async def handle_inbox_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    """Cancel drafting an anonymous message."""
    await state.clear()
    await callback.message.edit_text("❌ <b>Anonymous message cancelled.</b>", parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "inbox_random_prompt")
async def handle_random_prompt_callback(callback: CallbackQuery) -> None:
    """Provide a random campus icebreaker question to the sender."""
    chosen = random.choice(RANDOM_QUESTIONS)
    await callback.answer("🎲 Question rolled!")
    await callback.message.reply(
        f"💡 <b>Question Idea:</b>\n<i>\"{chosen}\"</i>\n\n"
        "Feel free to copy or type your answer below:",
        parse_mode="HTML",
    )


@router.message(InboxState.waiting_for_message)
async def handle_inbox_message_submission(message: Message, bot: Bot, state: FSMContext) -> None:
    """Deliver incoming anonymous message or media to the inbox owner."""
    if message.text and message.text.strip().startswith("/"):
        await state.clear()
        await message.reply("Action cancelled.")
        return

    data = await state.get_data()
    owner_id = data.get("owner_id")
    inbox_token = data.get("inbox_token")
    sender_id = message.from_user.id

    if not owner_id:
        await state.clear()
        await message.reply("Session expired. Please click the link again.")
        return

    # Multi-tier Link Shield scanning
    scan_res = await link_shield.scan_message(message)
    if scan_res.is_blocked:
        await message.reply(
            "🚫 <b>Message Blocked by Link Shield</b>\n\n"
            "Your message contains a link detected as unsafe, an IP-logger, or a suspicious file.\n\n"
            f"<code>{html.escape(scan_res.block_reason)}</code>",
            parse_mode="HTML",
        )
        return

    async with get_db_session() as session:
        # Verify inbox still active and sender not blocked
        inbox = await get_inbox_by_token(session, token=inbox_token)
        if not inbox or not inbox.is_active:
            await state.clear()
            await message.reply("⚠️ This link has been deactivated by the owner.")
            return

        if await is_inbox_sender_blocked(session, owner_id=owner_id, sender_id=sender_id):
            await state.clear()
            await message.reply("⚠️ You cannot send messages to this user.")
            return

        # Double check 20-min cooldown
        remaining = await get_inbox_cooldown_remaining(
            session, owner_id=owner_id, sender_id=sender_id, cooldown_seconds=1200
        )
        if remaining > 0:
            mins = int(remaining // 60) + 1
            await state.clear()
            await message.reply(
                f"⏳ Cooldown active. Please wait {mins} minute(s) before sending another message."
            )
            return

        # Determine message type
        msg_type = "text"
        if message.photo:
            msg_type = "photo"
        elif message.voice:
            msg_type = "voice"
        elif message.video:
            msg_type = "video"
        elif message.video_note:
            msg_type = "video_note"
        elif message.sticker:
            msg_type = "sticker"

        # Record inbox message
        inbox_msg = await record_inbox_message(
            session, owner_id=owner_id, sender_id=sender_id, message_type=msg_type
        )

        # Sender badge verification status
        stmt = select(User).where(User.id == sender_id)
        res = await session.execute(stmt)
        sender_user = res.scalar_one_or_none()
        if sender_user and sender_user.is_verified:
            badge = "🎓 Verified AUA Student" if not sender_user.is_alumni else "🎓🏛️ Verified AUA Alumni"
        else:
            badge = "👤 Anonymous User"

    await state.clear()

    # Buffer in volatile RAM for potential AI Moderation review (24h TTL)
    buffer_key = f"inbox_{inbox_msg.thread_token}"
    if msg_type == "text":
        session_buffer.add_text(buffer_key, sender_id, message.text or "")
    elif msg_type == "photo" and message.photo:
        try:
            photo_item = message.photo[-2] if len(message.photo) > 1 else message.photo[-1]
            img_bio = io.BytesIO()
            await bot.download(photo_item, destination=img_bio)
            session_buffer.add_image(
                session_id=buffer_key,
                user_id=sender_id,
                image_bytes=img_bio.getvalue(),
                caption=message.caption,
            )
        except Exception as e:
            logger.debug("Could not buffer inbox photo: %s", e)
            session_buffer.add_text(buffer_key, sender_id, message.caption or "[Photo]")
    else:
        session_buffer.add_text(buffer_key, sender_id, message.caption or f"[{msg_type.title()} attachment]")

    session_buffer.schedule_cleanup(buffer_key, ttl=session_buffer.INBOX_TTL_SECONDS)

    # Deliver to owner
    keyboard = get_inbox_message_keyboard(inbox_msg.thread_token)
    header = (
        "📬 <b>New Anonymous Message!</b> 🎭\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    )
    footer = (
        "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"✨ <b>Sender:</b> {badge}\n"
        "🕒 <i>Just now</i>"
    )

    try:
        if msg_type == "text":
            body = html.escape(message.text or "")
            await bot.send_message(
                chat_id=owner_id,
                text=f"{header}<i>\"{body}\"</i>{footer}",
                reply_markup=keyboard,
                parse_mode="HTML",
            )
        else:
            caption = html.escape(message.caption or "")
            body_caption = f"{header}<i>\"{caption}\"</i>{footer}" if caption else f"{header}<i>[Media Attachment]</i>{footer}"
            try:
                await message.copy_to(
                    chat_id=owner_id,
                    caption=body_caption,
                    reply_markup=keyboard,
                    parse_mode="HTML",
                )
            except TelegramAPIError:
                # Fallback for media without caption support (like stickers)
                await message.copy_to(chat_id=owner_id)
                await bot.send_message(
                    chat_id=owner_id,
                    text=f"{header}<i>[Sticker / Video Note]</i>{footer}",
                    reply_markup=keyboard,
                    parse_mode="HTML",
                )

        await message.reply(
            "✅ <b>Your anonymous message has been delivered!</b> 🚀\n\n"
            "The recipient will read it anonymously. If they reply, you will receive their response right here!\n\n"
            "<i>(Type /link to create your own anonymous inbox)</i>",
            parse_mode="HTML",
        )
    except TelegramAPIError as e:
        logger.error("Failed to deliver inbox message to %s: %s", owner_id, e)
        await message.reply(
            "⚠️ Could not deliver message to the owner (they may have blocked or stopped the bot)."
        )


# ==============================================================================
# Two-Way Threaded Replies
# ==============================================================================

@router.callback_query(F.data.startswith("inbox_reply:"))
async def handle_inbox_reply_start(callback: CallbackQuery, state: FSMContext) -> None:
    """Prompt user to compose a reply to an anonymous inbox message."""
    thread_token = callback.data.split(":")[1]
    user_id = callback.from_user.id

    async with get_db_session() as session:
        msg = await get_inbox_message_by_thread(session, thread_token)
        if not msg:
            await callback.answer("Message thread not found.", show_alert=True)
            return

        is_owner = (user_id == msg.owner_id)
        is_sender = (user_id == msg.sender_id)
        if not is_owner and not is_sender:
            await callback.answer("Unauthorized.", show_alert=True)
            return

        recipient_id = msg.sender_id if is_owner else msg.owner_id
        if await is_inbox_sender_blocked(session, owner_id=msg.owner_id, sender_id=msg.sender_id):
            await callback.answer("This thread has been blocked.", show_alert=True)
            return

    await state.set_state(InboxState.waiting_for_reply)
    await state.update_data(
        thread_token=thread_token,
        recipient_id=recipient_id,
        is_owner_reply=is_owner,
    )

    role_desc = "the anonymous sender" if is_owner else "the inbox owner"
    await callback.message.reply(
        f"✍️ <b>Replying to {role_desc}</b>\n\n"
        "Type your message below. It will be delivered anonymously:\n\n"
        "<i>(Send /cancel to abort)</i>",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(InboxState.waiting_for_reply)
async def handle_inbox_reply_submit(message: Message, bot: Bot, state: FSMContext) -> None:
    """Deliver reply to the target user while maintaining anonymity."""
    if message.text and message.text.strip().startswith("/"):
        await state.clear()
        await message.reply("Reply cancelled.")
        return

    data = await state.get_data()
    thread_token = data.get("thread_token")
    recipient_id = data.get("recipient_id")
    is_owner_reply = data.get("is_owner_reply", False)

    if not thread_token or not recipient_id:
        await state.clear()
        await message.reply("Session expired.")
        return

    scan_res = await link_shield.scan_message(message)
    if scan_res.is_blocked:
        await message.reply(
            "🚫 <b>Message Blocked by Link Shield</b>\n\n"
            f"<code>{html.escape(scan_res.block_reason)}</code>",
            parse_mode="HTML",
        )
        return

    await state.clear()

    # Buffer reply in volatile RAM for AI Moderation
    buffer_key = f"inbox_{thread_token}"
    replier_id = message.from_user.id
    if message.text:
        session_buffer.add_text(buffer_key, replier_id, message.text)
    elif message.photo:
        try:
            photo_item = message.photo[-2] if len(message.photo) > 1 else message.photo[-1]
            img_bio = io.BytesIO()
            await bot.download(photo_item, destination=img_bio)
            session_buffer.add_image(
                session_id=buffer_key,
                user_id=replier_id,
                image_bytes=img_bio.getvalue(),
                caption=message.caption,
            )
        except Exception as e:
            logger.debug("Could not buffer inbox reply photo: %s", e)
            session_buffer.add_text(buffer_key, replier_id, message.caption or "[Photo]")
    elif message.caption:
        session_buffer.add_text(buffer_key, replier_id, message.caption)

    session_buffer.schedule_cleanup(buffer_key, ttl=session_buffer.INBOX_TTL_SECONDS)

    reply_kb = get_inbox_reply_keyboard(thread_token)
    header_label = (
        "📬 <b>Reply from the Inbox Owner:</b> 💬"
        if is_owner_reply
        else "📬 <b>Follow-up from Anonymous Sender:</b> 💬"
    )

    header = f"{header_label}\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    footer = "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

    try:
        if message.text:
            text_body = f"{header}<i>\"{html.escape(message.text)}\"</i>{footer}"
            await bot.send_message(
                chat_id=recipient_id,
                text=text_body,
                reply_markup=reply_kb,
                parse_mode="HTML",
            )
        else:
            caption = html.escape(message.caption or "")
            body_caption = f"{header}<i>\"{caption}\"</i>{footer}" if caption else f"{header}<i>[Media]</i>{footer}"
            try:
                await message.copy_to(
                    chat_id=recipient_id,
                    caption=body_caption,
                    reply_markup=reply_kb,
                    parse_mode="HTML",
                )
            except TelegramAPIError:
                await message.copy_to(chat_id=recipient_id)
                await bot.send_message(
                    chat_id=recipient_id,
                    text=f"{header}<i>[Attachment]</i>{footer}",
                    reply_markup=reply_kb,
                    parse_mode="HTML",
                )

        await message.reply("✅ <b>Reply delivered successfully!</b>", parse_mode="HTML")
        async with get_db_session() as session:
            await mark_inbox_message_replied(session, thread_token=thread_token)
    except TelegramAPIError as e:
        logger.error("Failed to deliver reply to %s: %s", recipient_id, e)
        await message.reply("⚠️ Could not deliver reply (recipient may have blocked the bot).")


# ==============================================================================
# Emoji Reactions
# ==============================================================================

@router.callback_query(F.data.startswith("inbox_react:"))
async def handle_inbox_reaction(callback: CallbackQuery, bot: Bot) -> None:
    """Deliver anonymous emoji reaction to the message sender."""
    parts = callback.data.split(":")
    thread_token = parts[1]
    emoji = parts[2]
    user_id = callback.from_user.id

    async with get_db_session() as session:
        msg = await get_inbox_message_by_thread(session, thread_token)
        if not msg:
            await callback.answer("Message thread not found.", show_alert=True)
            return

        target_id = msg.sender_id if user_id == msg.owner_id else msg.owner_id

    try:
        await bot.send_message(
            chat_id=target_id,
            text=f"{emoji} <b>The recipient reacted to your anonymous message with: {emoji}</b>",
            parse_mode="HTML",
        )
        await callback.answer(f"Reacted with {emoji}")
    except TelegramAPIError:
        await callback.answer("Reaction sent.")


# ==============================================================================
# Blocking Senders
# ==============================================================================

@router.callback_query(F.data.startswith("inbox_block_confirm:"))
async def handle_inbox_block_confirm(callback: CallbackQuery) -> None:
    """Prompt owner with confirmation before permanently blocking an anonymous sender."""
    thread_token = callback.data.split(":")[1]
    await callback.message.reply(
        "🚫 <b>Block Anonymous Sender?</b>\n\n"
        "They will permanently be prevented from sending messages to your anonymous inbox.",
        reply_markup=get_inbox_block_confirm_keyboard(thread_token),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("inbox_block_cancel:"))
async def handle_inbox_block_cancel(callback: CallbackQuery) -> None:
    """Cancel block action."""
    await callback.message.delete()
    await callback.answer("Block cancelled.")


@router.callback_query(F.data.startswith("inbox_block_do:"))
async def handle_inbox_block_do(callback: CallbackQuery) -> None:
    """Permanently block the anonymous sender from messaging this owner."""
    thread_token = callback.data.split(":")[1]
    user_id = callback.from_user.id

    async with get_db_session() as session:
        msg = await get_inbox_message_by_thread(session, thread_token)
        if not msg or msg.owner_id != user_id:
            await callback.answer("Unauthorized.", show_alert=True)
            return

        await block_inbox_sender(session, owner_id=msg.owner_id, sender_id=msg.sender_id)

    await callback.message.edit_text(
        "🚫 <b>Sender has been blocked!</b>\n\n"
        "They can no longer send messages or questions to your anonymous link.",
        parse_mode="HTML",
    )
    await callback.answer("Sender blocked permanently.")


# ==============================================================================
# AI Moderation Reporting for Inbox Messages
# ==============================================================================

@router.callback_query(F.data.startswith("inbox_report_prompt:"))
async def handle_inbox_report_prompt(callback: CallbackQuery) -> None:
    """Prompt user before reporting an anonymous message to the AI Moderator."""
    thread_token = callback.data.split(":")[1]
    await callback.message.reply(
        "🚨 <b>Report Anonymous Message to AI Moderator?</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "The automated Gemini AI Moderator will review this message against AUA Community Code of Conduct & Anti-Doxxing rules.\n\n"
        "⚖️ <b>Accountability & Rules:</b>\n"
        "• Evaluates harassment, death threats, doxxing, hate speech, or unsolicited NSFW.\n"
        "• Automatically blocks the sender from messaging your inbox.\n"
        "• Deducts matchmaking priority points from guilty senders (up to -10).\n"
        "• Extreme illegal acts (violence threats, CSAM, extortion) result in immediate bans and identity unmasking to administration.\n"
        "• <i>Filing false or spiteful reports against innocent messages will penalize the reporter.</i>\n\n"
        "👉 <i>Would you like to submit this report?</i>",
        reply_markup=get_inbox_report_confirm_keyboard(thread_token),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("inbox_report_cancel:"))
async def handle_inbox_report_cancel(callback: CallbackQuery) -> None:
    """Cancel reporting action."""
    await callback.message.delete()
    await callback.answer("Report cancelled.")


@router.callback_query(F.data.startswith("inbox_report_do:"))
async def handle_inbox_report_do(callback: CallbackQuery, bot: Bot) -> None:
    """Evaluate anonymous inbox thread with Gemini AI Moderator and apply penalties/blocks."""
    thread_token = callback.data.split(":")[1]
    reporter_id = callback.from_user.id

    async with get_db_session() as session:
        msg = await get_inbox_message_by_thread(session, thread_token)
        if not msg:
            await callback.answer("Message thread not found.", show_alert=True)
            return

        is_owner = (reporter_id == msg.owner_id)
        is_sender = (reporter_id == msg.sender_id)
        if not is_owner and not is_sender:
            await callback.answer("Unauthorized.", show_alert=True)
            return

        reported_id = msg.sender_id if is_owner else msg.owner_id

    # Immediate status feedback
    await callback.message.edit_text(
        "🤖 <b>AI Safety Moderator Evaluating Message...</b>\n\n"
        "Analyzing conversation turns against AUA Safety & Privacy Rules. Please wait...",
        parse_mode="HTML",
    )

    buffer_key = f"inbox_{thread_token}"
    turns = session_buffer.pop_session(buffer_key)
    if not turns:
        # Fallback if buffer expired or bot restarted
        raw_text = callback.message.text or callback.message.caption or f"[{msg.message_type.title()} anonymous message]"
        turns = [SessionTurn(sender_id=reported_id, text=raw_text)]

    verdict = await gemini_moderator.evaluate_session(
        turns=turns,
        reporter_id=reporter_id,
        reported_id=reported_id,
        reason_text="Anonymous Inbox Q&A Report",
    )

    async with get_db_session() as session:
        # 1. Matchmaking priority penalties
        if verdict.reported_user_penalty > 0:
            await apply_admin_penalty(session, user_id=reported_id, penalty=verdict.reported_user_penalty)

        if verdict.reporter_penalty > 0:
            await apply_admin_penalty(session, user_id=reporter_id, penalty=verdict.reporter_penalty)

        # 2. Always block sender from this owner's inbox
        await block_inbox_sender(session, owner_id=msg.owner_id, sender_id=msg.sender_id)

        # 3. Matchmaking pairwise block
        if verdict.block_reported_user:
            await add_or_update_user_block(session, user_id=reporter_id, blocked_user_id=reported_id, expires_at=None)

        if verdict.block_reporter:
            await add_or_update_user_block(session, user_id=reported_id, blocked_user_id=reporter_id, expires_at=None)

        # 4. Tier 3 Extreme Illegal Violations
        if verdict.reveal_identity_for_illegal_content:
            if verdict.illegal_perpetrator in ("Reported_User", "Both"):
                await ban_user_by_admin(session, user_id=reported_id)
                add_banned_user(reported_id)
            if verdict.illegal_perpetrator in ("Reporter", "Both"):
                await ban_user_by_admin(session, user_id=reporter_id)
                add_banned_user(reporter_id)

            # Store unmasked IDs in Report audit
            report = await create_report(
                session=session,
                session_id=None,
                inbox_message_id=msg.id,
                thread_token=thread_token,
                reporter_id=reporter_id,
                reported_id=reported_id,
                reason_text="Anonymous Inbox Q&A Illegal Violation",
                guilty_party=verdict.guilty_party,
                severity_category=verdict.severity_category,
                reported_user_penalty=verdict.reported_user_penalty,
                reporter_penalty=verdict.reporter_penalty,
                reported_user_blocked=True,
                reporter_blocked=verdict.block_reporter,
                identity_revealed_for_illegal=True,
                anonymized_summary=verdict.anonymized_summary,
            )

            # Alert administrators immediately
            reported_obj = await get_user(session, reported_id)
            reporter_obj = await get_user(session, reporter_id)
            reported_un = f"@{reported_obj.username}" if (reported_obj and reported_obj.username) else f"ID: {reported_id}"
            reporter_un = f"@{reporter_obj.username}" if (reporter_obj and reporter_obj.username) else f"ID: {reporter_id}"

            admin_alert = (
                "🚨 <b>CRITICAL SAFETY ALERT: TIER 3 ILLEGAL CONTENT IN INBOX!</b> ⚠️\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"• <b>Report ID:</b> #{report.id}\n"
                f"• <b>Thread Token:</b> <code>{thread_token}</code>\n"
                f"• <b>Perpetrator:</b> {verdict.illegal_perpetrator}\n"
                f"• <b>Reported User:</b> {reported_un} (<code>{reported_id}</code>)\n"
                f"• <b>Reporter:</b> {reporter_un} (<code>{reporter_id}</code>)\n"
                f"• <b>Category:</b> {verdict.severity_category}\n"
                f"• <b>Summary:</b> {html.escape(verdict.anonymized_summary)}\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "<i>Offender account has been permanently banned from Anonymous AUA.</i>"
            )
            for admin_id in settings.ADMIN_IDS:
                try:
                    await bot.send_message(chat_id=admin_id, text=admin_alert, parse_mode="HTML")
                except TelegramAPIError as e:
                    logger.warning("Failed to dispatch inbox admin alert to %s: %s", admin_id, e)
        else:
            # Fully anonymized report record
            await create_report(
                session=session,
                session_id=None,
                inbox_message_id=msg.id,
                thread_token=thread_token,
                reporter_id=None,
                reported_id=None,
                reason_text="Anonymous Inbox Q&A Report",
                guilty_party=verdict.guilty_party,
                severity_category=verdict.severity_category,
                reported_user_penalty=verdict.reported_user_penalty,
                reporter_penalty=verdict.reporter_penalty,
                reported_user_blocked=verdict.block_reported_user,
                reporter_blocked=verdict.block_reporter,
                identity_revealed_for_illegal=False,
                anonymized_summary=verdict.anonymized_summary,
            )

    guilty_desc = {
        "Reported_User": "Reported Sender",
        "Reporter": "Reporter (False/Frivolous Report)",
        "Both": "Both Parties",
        "Neither": "Neither Party (No Violation)",
    }.get(verdict.guilty_party, verdict.guilty_party)

    summary_text = (
        "🛡️ <b>Report Evaluated by AI Moderator</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Verdict:</b> {guilty_desc}\n"
        f"• <b>Category:</b> {verdict.severity_category}\n"
        f"• <b>Summary:</b> <i>\"{html.escape(verdict.anonymized_summary)}\"</i>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🔒 <b>Action Taken:</b> The sender has been blocked from your anonymous inbox."
    )
    await callback.message.edit_text(summary_text, parse_mode="HTML")
    await callback.answer("Report evaluated by AI.", show_alert=False)
