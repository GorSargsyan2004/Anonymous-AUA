"""Personal Anonymous Message Inbox (/link) handlers and two-way threaded replies."""

import html
import logging
import random
from typing import Optional

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.database.crud import (
    block_inbox_sender,
    create_or_regenerate_inbox,
    deactivate_user_inbox,
    get_inbox_by_token,
    get_inbox_cooldown_remaining,
    get_inbox_message_by_thread,
    get_or_create_user,
    get_user_active_inbox,
    is_inbox_sender_blocked,
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
)
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
