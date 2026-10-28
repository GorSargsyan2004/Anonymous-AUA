"""Direct Chat Invitation Link (/invite) handlers and instant 1-on-1 pairing."""

import html
import logging
from typing import Optional

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.database.crud import (
    consume_chat_invite,
    create_chat_invite,
    deactivate_chat_invite,
    get_chat_invite_by_code,
    get_or_create_user,
    get_user_active_invites,
)
from app.database.models import ChatSession, User, UserBlock
from app.database.session import get_db_session
from app.keyboards.inline import get_invite_item_keyboard, get_invite_menu_keyboard
from app.services.reply_manager import reply_manager
from sqlalchemy import func, or_, select

logger = logging.getLogger(__name__)
router = Router(name="invites_router")


@router.message(Command("invite", "chatlink"))
async def handle_invite_command(message: Message, bot: Bot) -> None:
    """Display menu to create or manage direct chat invitation links."""
    user_id = message.from_user.id
    username = message.from_user.username

    async with get_db_session() as session:
        await get_or_create_user(session, user_id=user_id, username=username)
        active_invites = await get_user_active_invites(session, creator_id=user_id)

    text = (
        "🔗 <b>Anonymous Chat Invitation Links</b> 🎭\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Invite anyone directly into a private 1-on-1 anonymous chat with you without waiting in the matchmaking queue!\n\n"
        "<b>Choose link type:</b>\n"
        "• <b>⚡ One-Time Invite:</b> Auto-expires immediately after 1 person connects.\n"
        "• <b>♾️ Persistent Invite:</b> Reusable link for your bio, channel, or groups.\n\n"
        "<i>All links can be destroyed/revoked at any time.</i>"
    )
    kb = get_invite_menu_keyboard(has_active=bool(active_invites))
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "invite_menu")
async def handle_invite_menu_callback(callback: CallbackQuery) -> None:
    """Return to main invite links menu."""
    user_id = callback.from_user.id
    async with get_db_session() as session:
        active_invites = await get_user_active_invites(session, creator_id=user_id)

    text = (
        "🔗 <b>Anonymous Chat Invitation Links</b> 🎭\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Invite anyone directly into a private 1-on-1 anonymous chat with you without waiting in the matchmaking queue!\n\n"
        "<b>Choose link type:</b>\n"
        "• <b>⚡ One-Time Invite:</b> Auto-expires immediately after 1 person connects.\n"
        "• <b>♾️ Persistent Invite:</b> Reusable link for your bio, channel, or groups.\n\n"
        "<i>All links can be destroyed/revoked at any time.</i>"
    )
    kb = get_invite_menu_keyboard(has_active=bool(active_invites))
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("invite_create:"))
async def handle_invite_create(callback: CallbackQuery, bot: Bot) -> None:
    """Create a new one-time or persistent chat invitation link."""
    invite_type = callback.data.split(":")[1]
    user_id = callback.from_user.id

    async with get_db_session() as session:
        invite = await create_chat_invite(session, creator_id=user_id, invite_type=invite_type)

    bot_info = await bot.get_me()
    bot_username = bot_info.username or "aua_anonymous_bot"
    link_url = f"https://t.me/{bot_username}?start=chat_{invite.invite_code}"

    type_label = (
        "⚡ <b>One-Time (Single Use)</b>"
        if invite_type == "one_time"
        else "♾️ <b>Persistent (Reusable)</b>"
    )

    text = (
        "🎉 <b>Your Chat Invitation Link is Ready!</b> 🎭\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Type:</b> {type_label}\n\n"
        f"🔗 <b>Invitation Link:</b>\n<code>{link_url}</code>\n\n"
        "☝️ <i>Send this to a friend or post it. Whoever clicks it will instantly connect with you in an anonymous chat!</i>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🗑️ <i>You can destroy/revoke this link anytime using the button below.</i>"
    )
    kb = get_invite_item_keyboard(invite.invite_code)
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer("Invitation link created!")


@router.callback_query(F.data == "invite_list")
async def handle_invite_list(callback: CallbackQuery, bot: Bot) -> None:
    """Show list of all currently active invitation links for the user."""
    user_id = callback.from_user.id
    async with get_db_session() as session:
        active_invites = await get_user_active_invites(session, creator_id=user_id)

    if not active_invites:
        await callback.message.edit_text(
            "📋 <b>You have no active invitation links.</b>\n\nCreate a new one below:",
            reply_markup=get_invite_menu_keyboard(has_active=False),
            parse_mode="HTML",
        )
        await callback.answer()
        return

    bot_info = await bot.get_me()
    bot_username = bot_info.username or "aua_anonymous_bot"

    lines = ["📋 <b>Your Active Invitation Links:</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━"]
    builder = InlineKeyboardBuilder()

    for idx, inv in enumerate(active_invites, start=1):
        t_icon = "⚡" if inv.invite_type == "one_time" else "♾️"
        lines.append(
            f"{idx}. {t_icon} <code>https://t.me/{bot_username}?start=chat_{inv.invite_code}</code>"
        )
        builder.button(
            text=f"🗑️ Destroy Link #{idx}",
            callback_data=f"invite_destroy:{inv.invite_code}",
        )

    builder.button(text="🔙 Back to Invites", callback_data="invite_menu")
    builder.adjust(1)

    lines.append("\nTap a button below to destroy/revoke any link:")
    await callback.message.edit_text(
        "\n".join(lines), reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("invite_destroy:"))
async def handle_invite_destroy(callback: CallbackQuery) -> None:
    """Revoke and deactivate a chat invitation link."""
    invite_code = callback.data.split(":")[1]
    user_id = callback.from_user.id

    async with get_db_session() as session:
        success = await deactivate_chat_invite(session, creator_id=user_id, invite_code=invite_code)

    if success:
        await callback.message.edit_text(
            "🗑️ <b>Invitation link destroyed / revoked!</b>\n\n"
            "This link has been deactivated. Anyone clicking it will no longer be able to connect with you.\n\n"
            "Send /invite to generate a new link anytime.",
            reply_markup=InlineKeyboardBuilder().button(text="➕ Create New Invite", callback_data="invite_menu").as_markup(),
            parse_mode="HTML",
        )
        await callback.answer("Link destroyed.", show_alert=True)
    else:
        await callback.answer("Link was already revoked or not found.", show_alert=True)


async def handle_invite_deeplink(
    message: Message,
    invite_code: str,
    state: FSMContext,
    bot: Bot,
) -> None:
    """Handle guest opening a direct chat invitation link (chat_<invite_code>)."""
    guest_id = message.from_user.id
    username = message.from_user.username

    async with get_db_session() as session:
        guest = await get_or_create_user(session, user_id=guest_id, username=username)

        # 1. Look up invite
        invite = await get_chat_invite_by_code(session, invite_code)
        if not invite or not invite.is_active or (invite.invite_type == "one_time" and invite.is_used):
            await state.clear()
            await message.answer(
                "⚠️ <b>Invitation Expired or Revoked</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "This chat invitation link is invalid, expired, or has already been used by someone else.\n\n"
                "Send /invite to create your own chat invitation link! 🎭",
                parse_mode="HTML",
            )
            return

        # 2. Check self connection
        if guest_id == invite.creator_id:
            await state.clear()
            await message.answer(
                "😄 <b>This is your own invitation link!</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "Send it to a friend or post it in a group so someone else can connect with you!\n\n"
                "Send /invite to manage your links.",
                parse_mode="HTML",
            )
            return

        # 3. Check ban status
        if getattr(guest, "is_banned", False):
            await state.clear()
            await message.answer("🚫 <b>You are banned from using this bot.</b>", parse_mode="HTML")
            return

        # 4. Check block pairs
        block_stmt = select(func.count(UserBlock.id)).where(
            or_(
                (UserBlock.user_id == guest_id) & (UserBlock.blocked_user_id == invite.creator_id),
                (UserBlock.user_id == invite.creator_id) & (UserBlock.blocked_user_id == guest_id),
            )
        )
        block_res = await session.execute(block_stmt)
        if (block_res.scalar() or 0) > 0:
            await state.clear()
            await message.answer("⚠️ You cannot connect with this user.", parse_mode="HTML")
            return

        # 5. Check if guest is already in a chat
        if guest.current_partner_id is not None:
            await state.clear()
            await message.answer(
                "💬 <b>You are already in an active chat!</b>\n\n"
                "Please end your current chat with /stop before connecting to a new invitation link.",
                parse_mode="HTML",
            )
            return

        # 6. Check creator availability
        creator_stmt = select(User).where(User.id == invite.creator_id)
        creator_res = await session.execute(creator_stmt)
        creator = creator_res.scalar_one_or_none()

        if not creator or getattr(creator, "is_banned", False):
            await state.clear()
            await message.answer("⚠️ The host account is currently unavailable.", parse_mode="HTML")
            return

        if creator.current_partner_id is not None:
            # Creator is busy in another chat session
            await state.clear()
            await message.answer(
                "⏳ <b>Host is currently in another chat</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "The creator of this invite link is currently talking to someone else.\n"
                "We have sent them a notification that you are waiting! Try opening the link again in a few moments.",
                parse_mode="HTML",
            )
            try:
                await bot.send_message(
                    chat_id=creator.id,
                    text=(
                        "🔔 <b>Someone clicked your chat invitation link!</b> 🎭\n\n"
                        "A partner is waiting to connect with you right now. Send /stop to finish your current chat and connect!"
                    ),
                    parse_mode="HTML",
                )
            except TelegramAPIError:
                pass
            return

        # 7. Both are free! Connect immediately!
        if invite.invite_type == "one_time":
            await consume_chat_invite(session, invite_id=invite.id, used_by_id=guest.id)

        # Create active chat session
        chat_session = ChatSession(
            user1_id=creator.id,
            user2_id=guest.id,
            is_active=True,
        )
        session.add(chat_session)

        # Clear search status and set partner IDs
        creator.is_searching = False
        creator.current_partner_id = guest.id
        guest.is_searching = False
        guest.current_partner_id = creator.id

        await session.commit()

    # Clear FSM states & reply cache
    await state.clear()
    await reply_manager.clear_chat(creator.id, guest.id)

    connect_msg_creator = (
        "🎉 <b>Connected via your Invitation Link!</b> 🎭\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Someone clicked your invite link and you are now in a private, anonymous session!\n"
        "Everything you send here will be relayed to your partner.\n\n"
        "⚡ <b>Commands:</b>\n"
        "• /next — Skip to a new partner\n"
        "• /stop — End this chat\n"
        "• /report — Emergency report & block\n\n"
        "🔒 <i>Privacy Shield Active: Identities strictly hidden.</i>"
    )

    connect_msg_guest = (
        "🎉 <b>Connected via Invitation Link!</b> 🎭\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "You are now in a private, anonymous session with the link creator!\n"
        "Everything you send here will be relayed to your partner.\n\n"
        "⚡ <b>Commands:</b>\n"
        "• /next — Skip to a new partner\n"
        "• /stop — End this chat\n"
        "• /report — Emergency report & block\n\n"
        "🔒 <i>Privacy Shield Active: Identities strictly hidden.</i>"
    )

    await message.answer(connect_msg_guest, parse_mode="HTML")
    try:
        await bot.send_message(chat_id=creator.id, text=connect_msg_creator, parse_mode="HTML")
    except TelegramAPIError as e:
        logger.error("Failed to notify invite creator %s: %s", creator.id, e)
