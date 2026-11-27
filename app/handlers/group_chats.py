"""Telegram Anonymous Group Chats handlers: browser, creation wizard, sticky nicknames, relay, and /report."""

import html
import logging
from typing import Optional

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud import (
    count_active_group_members,
    create_group_chat_with_creator,
    get_active_created_group_by_user,
    get_active_group_members,
    get_all_taken_nicknames_in_group,
    get_group_chat_by_id,
    get_group_creation_cooldown_remaining,
    get_group_member_record,
    get_or_create_user,
    get_reportable_group_participants,
    get_user_active_group_member,
    is_user_banned_from_group,
    is_user_eligible_for_group,
    join_group_chat_atomic,
    leave_group_chat,
    list_eligible_active_groups,
    validate_creator_anti_lurker,
)
from app.database.models import GROUP_CHAT_MAX_CAPACITY, GROUP_CHAT_MIN_CAPACITY, GroupChat, User
from app.database.session import get_db_session
from app.keyboards.inline import (
    get_group_browser_keyboard,
    get_group_cancel_keyboard,
    get_group_capacity_step_keyboard,
    get_group_description_step_keyboard,
    get_group_details_keyboard,
    get_group_limitations_keyboard,
    get_group_nickname_keyboard,
    get_group_report_reasons_keyboard,
    get_group_report_targets_keyboard,
)
from app.services.group_chat_service import (
    broadcast_group_system_message,
    generate_unique_random_nickname,
    group_reply_buffer,
    relay_group_message,
    validate_group_description,
    validate_group_name,
    validate_group_nickname,
)
from app.services.group_moderator import group_ai_moderator
from app.states.states import (
    GroupChatCreateState,
    GroupChatJoinState,
    GroupChatReportState,
)

logger = logging.getLogger(__name__)
router = Router(name="group_chats_router")


def _build_group_browser_text(
    groups_with_counts: list,
    page: int,
    total_pages: int,
    total_count: int,
) -> str:
    """Build HTML text for the paginated group chat browser."""
    if not groups_with_counts or total_count == 0:
        return (
            "👥 <b>Anonymous Group Chats</b> 🎭\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "There are currently no active group chats matching your profile.\n\n"
            "✨ Tap <b>+ Create Group</b> below to start a new anonymous group chat!"
        )

    return (
        "👥 <b>Anonymous Group Chats</b> 🎭\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Showing active groups you are eligible to join (Page <b>{page}/{total_pages}</b> · <b>{total_count}</b> total).\n\n"
        "<i>Tap a group below to view details and join, or create your own:</i>"
    )


def _build_group_details_text(group: GroupChat, current_count: int) -> str:
    """Build HTML text for the Group Details View.

    Shows:
    - Group Name
    - Participants: current / max
    - Description: displayed only if set by the creator
    - Limitations: displayed only if set
    """
    safe_name = html.escape(group.name)
    lines = [
        f"👥 <b>{safe_name}</b> 🎭",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"👤 <b>Participants:</b> {current_count} / {group.max_participants}",
    ]

    if group.description and group.description.strip():
        safe_desc = html.escape(group.description.strip())
        lines.append(f"📝 <b>Description:</b> {safe_desc}")

    limitations = []
    if group.allowed_gender:
        limitations.append(f"Gender: {html.escape(group.allowed_gender.capitalize())} only")
    if group.allowed_orientation:
        limitations.append(f"Orientation: {html.escape(group.allowed_orientation.capitalize())} only")
    if group.verified_only:
        limitations.append("Verified users only")

    if limitations:
        lines.append("🔒 <b>Limitations:</b> " + " • ".join(limitations))

    return "\n".join(lines)


def _build_limitations_step_text(
    name: str,
    description: Optional[str],
    max_participants: int,
    allowed_gender: Optional[str],
    allowed_orientation: Optional[str],
    verified_only: bool,
) -> str:
    """Build summary text for Step 4 (Preferences / Limitations Menu)."""
    safe_name = html.escape(name)
    desc_line = f"• <b>Description:</b> {html.escape(description)}\n" if description else ""
    lims = []
    if allowed_gender:
        lims.append(f"Gender: {allowed_gender.capitalize()} only")
    if allowed_orientation:
        lims.append(f"Orientation: {allowed_orientation.capitalize()} only")
    if verified_only:
        lims.append("Verified users only")
    lims_str = ", ".join(lims) if lims else "Open to everyone (No restrictions)"

    return (
        "⚙️ <b>Step 4/5: Group Preferences & Limitations</b> 🎭\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Group Name:</b> {safe_name}\n"
        f"{desc_line}"
        f"• <b>Max Participants:</b> {max_participants}\n"
        f"• <b>Current Limitations:</b> {html.escape(lims_str)}\n\n"
        "🛡️ <i>Anti-Lurker Rule: You may only enable restrictions that your own profile satisfies.</i>\n\n"
        "Toggle any filter below, then tap <b>Continue →</b>:"
    )


def _build_in_group_welcome_text(group: GroupChat, nickname: str, current_count: int, is_creator: bool = False) -> str:
    """Build confirmation message sent when creating or joining a group chat."""
    safe_name = html.escape(group.name)
    safe_nick = html.escape(nickname)
    header = "🎉 <b>Group Chat Created & Joined!</b> 🎭" if is_creator else "🎉 <b>Joined Group Chat!</b> 🎭"
    return (
        f"{header}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Group:</b> {safe_name} ({current_count}/{group.max_participants})\n"
        f"• <b>Your Nickname:</b> <b>{safe_nick}</b>\n\n"
        "💬 Any message or media you send now will be shared with the group under your nickname.\n\n"
        "⚡ <b>In-Group Commands:</b>\n"
        "• /members — View active members in this group\n"
        "• /report — Report a participant to the Multi-Party AI Moderator\n"
        "• /leave (or /stop) — Leave this group chat"
    )


async def render_group_browser_for_user(
    session: AsyncSession,
    user: User,
    page: int = 1,
) -> tuple[str, object]:
    """Fetch paginated eligible groups and return (html_text, inline_keyboard)."""
    groups_with_counts, total_count, total_pages = await list_eligible_active_groups(
        session=session,
        user=user,
        page=page,
        page_size=5,
    )
    safe_page = max(1, min(page, total_pages))
    text = _build_group_browser_text(groups_with_counts, safe_page, total_pages, total_count)
    kb = get_group_browser_keyboard(groups_with_counts, page=safe_page, total_pages=total_pages)
    return text, kb


# ==============================================================================
# Entry Points: /groups, /rooms, and /search -> Group chats button
# ==============================================================================

@router.message(Command("groups", "rooms"))
async def handle_groups_command(message: Message, state: FSMContext) -> None:
    """Open the paginated anonymous group chat browser via /groups (or /rooms alias)."""
    await state.clear()
    user_id = message.from_user.id
    username = message.from_user.username

    async with get_db_session() as session:
        user = await get_or_create_user(session, user_id=user_id, username=username)
        if getattr(user, "is_banned", False):
            await message.answer(
                "🚫 <b>Access Denied</b>\n\nYou have been banned from using Anonymous AUA.",
                parse_mode="HTML",
            )
            return

        if not user.sex or not user.orientation:
            await message.answer(
                "⚠️ <b>Profile not set up yet!</b>\n\n"
                "Please configure your sex and sexual orientation first.\n"
                "Send /start to complete your profile setup.",
                parse_mode="HTML",
            )
            return

        text, kb = await render_group_browser_for_user(session, user, page=1)

    await message.answer(text, reply_markup=kb, parse_mode="HTML")


def _get_browse_active_groups_keyboard(label: str = "👥 Browse Active Groups") -> InlineKeyboardMarkup:
    """Return a single-button inline keyboard that opens page 1 of the group browser."""
    builder = InlineKeyboardBuilder()
    builder.button(text=label, callback_data="grp_page:1")
    builder.adjust(1)
    return builder.as_markup()


async def handle_group_deeplink(
    message: Message,
    raw_group_id: str,
    state: FSMContext,
    bot: Bot,
) -> None:
    """Handle /start group_<id> deep-link join flow from auahub.am."""
    await state.clear()
    cleaned_id = (raw_group_id or "").strip()
    if not cleaned_id.isdigit() or int(cleaned_id) <= 0:
        await message.answer(
            "⚠️ <b>Invalid Group Link</b>\n\n"
            "This group chat link is invalid or has expired.",
            reply_markup=_get_browse_active_groups_keyboard("Browse Active Groups"),
            parse_mode="HTML",
        )
        return

    group_id = int(cleaned_id)
    user_id = message.from_user.id
    username = message.from_user.username

    async with get_db_session() as session:
        user = await get_or_create_user(session, user_id=user_id, username=username)

        # 1. Bot Ban Check
        if getattr(user, "is_banned", False):
            await message.answer(
                "🚫 <b>Access Denied</b>\n\n"
                "You have been banned from using Anonymous AUA.",
                parse_mode="HTML",
            )
            return

        # Look up GroupChat by id
        group = await get_group_chat_by_id(session, group_id)
        if not group or not group.is_active:
            await message.answer(
                "⚠️ <b>Group Chat Closed or Not Found</b>\n\n"
                "This anonymous group chat has closed or no longer exists.",
                reply_markup=_get_browse_active_groups_keyboard("Browse Active Groups"),
                parse_mode="HTML",
            )
            return

        safe_group_name = html.escape(group.name)

        # 2. Already in This Group
        active_group_res = await get_user_active_group_member(session, user_id)
        if active_group_res is not None:
            active_member, active_group = active_group_res
            if active_group.id == group.id:
                await message.answer(
                    f'💬 You are already in "<b>{safe_group_name}</b>" as <b>{html.escape(active_member.nickname)}</b>. '
                    "Just send a message to chat!",
                    parse_mode="HTML",
                )
                return

            # 3. Session Exclusivity (different group chat)
            await message.answer(
                f'⚠️ <b>Already in Another Group Chat</b>\n\n'
                f'You are currently in "<b>{html.escape(active_group.name)}</b>". '
                f'Please send /leave (or /stop) to exit your current group before joining "<b>{safe_group_name}</b>".',
                parse_mode="HTML",
            )
            return

        # 3b. Session Exclusivity (1-on-1 chat or /search queue)
        if user.current_partner_id is not None or user.is_searching:
            await message.answer(
                "⚠️ <b>Active 1-on-1 Session Detected</b>\n\n"
                f'You are currently in a 1-on-1 chat or search queue. '
                f'Please send /stop first before joining "<b>{safe_group_name}</b>".',
                parse_mode="HTML",
            )
            return

        # 4. Group Ban Check
        if await is_user_banned_from_group(session, group.id, user_id):
            await message.answer(
                "⚠️ <b>Unable to Join Group</b>\n\n"
                "You are not able to join this group chat.",
                reply_markup=_get_browse_active_groups_keyboard("Browse Active Groups"),
                parse_mode="HTML",
            )
            return

        # 5. Profile / Registration Check
        if not user.sex or not user.orientation:
            await state.update_data(pending_group_id=group.id)
            await message.answer(
                "⚠️ <b>Profile Setup Required</b>\n\n"
                f'Before joining "<b>{safe_group_name}</b>", please complete your basic profile setup (sex and orientation).\n'
                "Send /start to set up your profile, then tap the group link again!",
                parse_mode="HTML",
            )
            return

        # 6. Capacity Check
        current_count = await count_active_group_members(session, group.id)
        if current_count >= group.max_participants:
            await message.answer(
                f"🔒 <b>Group Full ({current_count}/{group.max_participants})</b>\n\n"
                f'The group "<b>{safe_group_name}</b>" is currently full.',
                reply_markup=_get_browse_active_groups_keyboard("Browse Active Groups"),
                parse_mode="HTML",
            )
            return

        # 7. Eligibility / Limitation Checks (verified_only, allowed_gender, allowed_orientation)
        eligible, reason = is_user_eligible_for_group(user, group)
        if not eligible:
            await message.answer(
                f"🔒 <b>Cannot Join {safe_group_name}</b>\n\n"
                f"{html.escape(reason)}\n"
                "You don't match this group's preferences.",
                reply_markup=_get_browse_active_groups_keyboard("Browse Eligible Groups"),
                parse_mode="HTML",
            )
            return

        # 8. Passed all checks! Attempt atomic join (auto-restores sticky nickname if returning member)
        joined_group, member, new_count, status_or_err = await join_group_chat_atomic(
            session=session,
            user=user,
            group_id=group.id,
            requested_nickname=None,
        )

        if status_or_err == "needs_nickname" and joined_group:
            taken_lower = await get_all_taken_nicknames_in_group(session, group.id)
            random_nick = generate_unique_random_nickname(taken_lower)
            await state.set_state(GroupChatJoinState.waiting_for_nickname)
            await state.update_data(
                join_group_id=group.id,
                join_back_page=1,
                suggested_random_nickname=random_nick,
            )

            desc_line = (
                f"📝 <b>Description:</b> {html.escape(group.description.strip())}\n"
                if (group.description and group.description.strip())
                else ""
            )
            prompt_html = (
                f"👥 <b>Joining: {safe_group_name} ({current_count}/{group.max_participants})</b>\n"
                f"{desc_line}\n"
                "🎭 <b>Choose Your Nickname</b>\n"
                "Type a custom nickname (2–20 characters: letters, numbers, spaces, <code>_</code>, <code>-</code>) "
                "or tap the button below to use a random nickname:\n\n"
                "🔒 <i>Your nickname is sticky in this group and will be remembered if you leave and rejoin.</i>"
            )
            kb = get_group_nickname_keyboard(
                random_nickname=random_nick,
                cancel_callback=f"grp_view:{group.id}:1",
            )
            await message.answer(prompt_html, reply_markup=kb, parse_mode="HTML")
            return

        if status_or_err or not joined_group or not member:
            await message.answer(
                f"⚠️ <b>Could not join group:</b> {html.escape(status_or_err or 'Group unavailable.')}",
                reply_markup=_get_browse_active_groups_keyboard("Browse Active Groups"),
                parse_mode="HTML",
            )
            return

        # Sticky nickname was automatically restored!
        safe_nick = html.escape(member.nickname)
        join_broadcast = f"🎭 <b>{safe_nick}</b> joined the group ({new_count}/{joined_group.max_participants})"
        await broadcast_group_system_message(
            bot=bot,
            session=session,
            group_id=joined_group.id,
            html_text=join_broadcast,
            exclude_user_id=user_id,
        )

    welcome_html = _build_in_group_welcome_text(
        joined_group,
        member.nickname,
        current_count=new_count,
        is_creator=False,
    )
    await message.answer(welcome_html, parse_mode="HTML")


@router.callback_query(F.data == "search_open_groups")
async def handle_search_open_groups_callback(callback: CallbackQuery, state: FSMContext) -> None:
    """Open the group chat browser directly from the 'Group chats' button inside /search."""
    await state.clear()
    user_id = callback.from_user.id
    username = callback.from_user.username

    async with get_db_session() as session:
        user = await get_or_create_user(session, user_id=user_id, username=username)
        if getattr(user, "is_banned", False):
            await callback.answer("Access denied.", show_alert=True)
            return
        if not user.sex or not user.orientation:
            await callback.answer("Please complete /start setup first!", show_alert=True)
            return

        text, kb = await render_group_browser_for_user(session, user, page=1)

    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except TelegramAPIError:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("grp_page:") | F.data.startswith("grp_refresh:"))
async def handle_group_page_or_refresh(callback: CallbackQuery, state: FSMContext) -> None:
    """Navigate pages or refresh the group chat browser list in place."""
    await state.clear()
    parts = callback.data.split(":")
    try:
        page = max(1, int(parts[1]))
    except (IndexError, ValueError):
        page = 1

    user_id = callback.from_user.id
    async with get_db_session() as session:
        user = await get_or_create_user(session, user_id=user_id, username=callback.from_user.username)
        text, kb = await render_group_browser_for_user(session, user, page=page)

    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except TelegramAPIError:
        pass
    await callback.answer()


@router.callback_query(F.data == "grp_noop")
async def handle_group_noop(callback: CallbackQuery) -> None:
    """No-op handler for pagination center indicator or boundary arrows."""
    await callback.answer()


@router.callback_query(F.data == "grp_full_noop")
async def handle_group_full_noop(callback: CallbackQuery) -> None:
    """Inform user when clicking a full group button."""
    await callback.answer("This group is currently full!", show_alert=True)


# ==============================================================================
# Group Details View (on clicking a group button)
# ==============================================================================

@router.callback_query(F.data.startswith("grp_view:"))
async def handle_group_details_view(callback: CallbackQuery, state: FSMContext) -> None:
    """Edit message in place to display Group Name, Participants, Description, and Limitations."""
    await state.clear()
    parts = callback.data.split(":")
    try:
        group_id = int(parts[1])
        back_page = max(1, int(parts[2])) if len(parts) > 2 else 1
    except (IndexError, ValueError):
        await callback.answer("Invalid group selection.", show_alert=True)
        return

    user_id = callback.from_user.id
    async with get_db_session() as session:
        user = await get_or_create_user(session, user_id=user_id, username=callback.from_user.username)
        group = await get_group_chat_by_id(session, group_id)
        if not group or not group.is_active:
            await callback.answer("This group chat is no longer active.", show_alert=True)
            text, kb = await render_group_browser_for_user(session, user, page=back_page)
            try:
                await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
            except TelegramAPIError:
                pass
            return

        if await is_user_banned_from_group(session, group_id, user.id):
            await callback.answer("You are not eligible to join this group.", show_alert=True)
            return

        eligible, reason = is_user_eligible_for_group(user, group)
        if not eligible:
            await callback.answer(reason, show_alert=True)
            return

        current_count = await count_active_group_members(session, group_id)

    details_text = _build_group_details_text(group, current_count)
    details_kb = get_group_details_keyboard(
        group_id=group.id,
        current_count=current_count,
        max_participants=group.max_participants,
        back_page=back_page,
    )
    try:
        await callback.message.edit_text(details_text, reply_markup=details_kb, parse_mode="HTML")
    except TelegramAPIError:
        pass
    await callback.answer()


# ==============================================================================
# Group Creation Flow (5-Step FSM Wizard with Anti-Abuse & Anti-Lurker Rules)
# ==============================================================================

@router.callback_query(F.data == "grp_create_start")
async def handle_group_create_start(callback: CallbackQuery, state: FSMContext) -> None:
    """Start the 5-step group creation flow after verifying exclusivity, active created limit, and cooldown."""
    user_id = callback.from_user.id
    async with get_db_session() as session:
        user = await get_or_create_user(session, user_id=user_id, username=callback.from_user.username)

        if getattr(user, "is_banned", False):
            await callback.answer("You are banned from using Anonymous AUA.", show_alert=True)
            return

        if not user.sex or not user.orientation:
            await callback.answer("Please complete your /start profile setup first!", show_alert=True)
            return

        # 1. Exclusivity: 1-on-1 chat or search queue
        if user.current_partner_id is not None or user.is_searching:
            await callback.answer(
                "You cannot create a group while in a 1-on-1 chat or search queue. Send /stop first!",
                show_alert=True,
            )
            return

        # 2. Exclusivity: already in a group chat
        active_group = await get_user_active_group_member(session, user_id)
        if active_group is not None:
            _, grp = active_group
            await callback.answer(
                f"You are already in an active group chat ({grp.name}). Send /leave first!",
                show_alert=True,
            )
            return

        # 3. Anti-abuse: 1 active created group per user at a time
        existing_created = await get_active_created_group_by_user(session, user_id)
        if existing_created is not None:
            await callback.answer(
                f"You already have an active created group ('{existing_created.name}'). "
                "Only 1 active created group per user is allowed at a time.",
                show_alert=True,
            )
            return

        # 4. Anti-abuse: 10-minute cooldown between creations
        cooldown_rem = await get_group_creation_cooldown_remaining(session, user_id)
        if cooldown_rem > 0:
            mins, secs = cooldown_rem // 60, cooldown_rem % 60
            time_str = f"{mins}m {secs}s" if mins > 0 else f"{secs}s"
            await callback.answer(
                f"Creation cooldown active: Please wait {time_str} before creating another group.",
                show_alert=True,
            )
            return

    await state.set_state(GroupChatCreateState.waiting_for_name)
    await state.update_data(
        allowed_gender=None,
        allowed_orientation=None,
        verified_only=False,
    )

    prompt_text = (
        "✨ <b>Create Anonymous Group Chat — Step 1/5: Group Name</b> 🎭\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Please enter a <b>Name</b> for your group chat (3–40 characters):\n\n"
        "<i>Examples: Late Night CS Talk, Math Study Room, Campus Chill</i>"
    )
    try:
        await callback.message.edit_text(
            prompt_text,
            reply_markup=get_group_cancel_keyboard(),
            parse_mode="HTML",
        )
    except TelegramAPIError:
        await callback.message.answer(
            prompt_text,
            reply_markup=get_group_cancel_keyboard(),
            parse_mode="HTML",
        )
    await callback.answer()


@router.callback_query(F.data == "grp_create_cancel")
async def handle_group_create_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    """Cancel group creation or nickname selection and return to the group browser."""
    await state.clear()
    user_id = callback.from_user.id
    async with get_db_session() as session:
        user = await get_or_create_user(session, user_id=user_id, username=callback.from_user.username)
        text, kb = await render_group_browser_for_user(session, user, page=1)

    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except TelegramAPIError:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer("Cancelled.")


@router.message(GroupChatCreateState.waiting_for_name, F.text)
async def handle_group_create_name_input(message: Message, state: FSMContext) -> None:
    """Step 1: Validate group name (3-40 chars) and advance to Step 2 (Description)."""
    if message.text.strip().lower() in ("/cancel", "/stop"):
        await state.clear()
        await message.answer("✅ <b>Group creation cancelled.</b>", parse_mode="HTML")
        return

    if message.text.startswith("/"):
        return

    ok, cleaned_name, err = validate_group_name(message.text)
    if not ok:
        await message.answer(
            f"⚠️ <b>Invalid Group Name:</b> {html.escape(err)}\n\nPlease enter a valid group name (3–40 chars):",
            reply_markup=get_group_cancel_keyboard(),
            parse_mode="HTML",
        )
        return

    await state.update_data(name=cleaned_name)
    await state.set_state(GroupChatCreateState.waiting_for_description)

    await message.answer(
        "📝 <b>Step 2/5: Group Description (Optional)</b> 🎭\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Group Name:</b> {html.escape(cleaned_name)}\n\n"
        "Send a short description (up to 200 characters) telling students what this group is about, "
        "or tap <b>Skip</b> below:",
        reply_markup=get_group_description_step_keyboard(),
        parse_mode="HTML",
    )


@router.callback_query(GroupChatCreateState.waiting_for_description, F.data == "grp_desc_skip")
async def handle_group_create_desc_skip(callback: CallbackQuery, state: FSMContext) -> None:
    """Step 2 Skip: Set description=None and advance to Step 3 (Max Participants)."""
    await state.update_data(description=None)
    await state.set_state(GroupChatCreateState.waiting_for_max_participants)

    text = (
        "👥 <b>Step 3/5: Max Participants</b> 🎭\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Choose the maximum number of participants allowed in this group (<b>{GROUP_CHAT_MIN_CAPACITY}</b> to <b>{GROUP_CHAT_MAX_CAPACITY}</b>).\n\n"
        "Tap a quick-select button below or type any number between 3 and 15:"
    )
    try:
        await callback.message.edit_text(
            text,
            reply_markup=get_group_capacity_step_keyboard(),
            parse_mode="HTML",
        )
    except TelegramAPIError:
        await callback.message.answer(
            text,
            reply_markup=get_group_capacity_step_keyboard(),
            parse_mode="HTML",
        )
    await callback.answer()


@router.message(GroupChatCreateState.waiting_for_description, F.text)
async def handle_group_create_desc_input(message: Message, state: FSMContext) -> None:
    """Step 2 Input: Validate description (<= 200 chars) and advance to Step 3 (Max Participants)."""
    if message.text.strip().lower() in ("/cancel", "/stop"):
        await state.clear()
        await message.answer("✅ <b>Group creation cancelled.</b>", parse_mode="HTML")
        return

    if message.text.startswith("/"):
        return

    ok, cleaned_desc, err = validate_group_description(message.text)
    if not ok:
        await message.answer(
            f"⚠️ <b>Invalid Description:</b> {html.escape(err)}\n\n"
            "Please enter a description up to 200 characters, or tap <b>Skip</b>:",
            reply_markup=get_group_description_step_keyboard(),
            parse_mode="HTML",
        )
        return

    await state.update_data(description=cleaned_desc)
    await state.set_state(GroupChatCreateState.waiting_for_max_participants)

    await message.answer(
        "👥 <b>Step 3/5: Max Participants</b> 🎭\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Choose the maximum number of participants allowed in this group (<b>{GROUP_CHAT_MIN_CAPACITY}</b> to <b>{GROUP_CHAT_MAX_CAPACITY}</b>).\n\n"
        "Tap a quick-select button below or type any number between 3 and 15:",
        reply_markup=get_group_capacity_step_keyboard(),
        parse_mode="HTML",
    )


async def _enter_limitations_step(
    user_id: int,
    state: FSMContext,
    max_participants: int,
    message_or_callback: Message | CallbackQuery,
) -> None:
    """Transition to Step 4 (Preferences / Limitations Menu)."""
    await state.update_data(max_participants=max_participants)
    await state.set_state(GroupChatCreateState.configuring_limitations)
    data = await state.get_data()

    async with get_db_session() as session:
        user = await get_or_create_user(session, user_id=user_id)

    text = _build_limitations_step_text(
        name=data.get("name", "Group"),
        description=data.get("description"),
        max_participants=max_participants,
        allowed_gender=data.get("allowed_gender"),
        allowed_orientation=data.get("allowed_orientation"),
        verified_only=bool(data.get("verified_only", False)),
    )
    kb = get_group_limitations_keyboard(
        creator_sex=user.sex,
        creator_orientation=user.orientation,
        creator_is_verified=bool(getattr(user, "is_verified", False)),
        allowed_gender=data.get("allowed_gender"),
        allowed_orientation=data.get("allowed_orientation"),
        verified_only=bool(data.get("verified_only", False)),
    )

    if isinstance(message_or_callback, CallbackQuery):
        try:
            await message_or_callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except TelegramAPIError:
            await message_or_callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
        await message_or_callback.answer()
    else:
        await message_or_callback.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(GroupChatCreateState.waiting_for_max_participants, F.data.startswith("grp_cap:"))
async def handle_group_create_capacity_button(callback: CallbackQuery, state: FSMContext) -> None:
    """Step 3 Quick-Select Button: Validate capacity in 3..15 and advance to Step 4."""
    try:
        cap = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Invalid capacity.", show_alert=True)
        return

    if not (GROUP_CHAT_MIN_CAPACITY <= cap <= GROUP_CHAT_MAX_CAPACITY):
        await callback.answer(
            f"Max participants must be between {GROUP_CHAT_MIN_CAPACITY} and {GROUP_CHAT_MAX_CAPACITY}.",
            show_alert=True,
        )
        return

    await _enter_limitations_step(callback.from_user.id, state, cap, callback)


@router.message(GroupChatCreateState.waiting_for_max_participants, F.text)
async def handle_group_create_capacity_input(message: Message, state: FSMContext) -> None:
    """Step 3 Typed Number: Validate integer in 3..15 and advance to Step 4."""
    if message.text.strip().lower() in ("/cancel", "/stop"):
        await state.clear()
        await message.answer("✅ <b>Group creation cancelled.</b>", parse_mode="HTML")
        return

    if message.text.startswith("/"):
        return

    raw = message.text.strip()
    try:
        cap = int(raw)
    except ValueError:
        await message.answer(
            f"⚠️ Please enter a whole number between <b>{GROUP_CHAT_MIN_CAPACITY}</b> and <b>{GROUP_CHAT_MAX_CAPACITY}</b>:",
            reply_markup=get_group_capacity_step_keyboard(),
            parse_mode="HTML",
        )
        return

    if not (GROUP_CHAT_MIN_CAPACITY <= cap <= GROUP_CHAT_MAX_CAPACITY):
        await message.answer(
            f"⚠️ Max participants must be between <b>{GROUP_CHAT_MIN_CAPACITY}</b> and <b>{GROUP_CHAT_MAX_CAPACITY}</b>. Please try again:",
            reply_markup=get_group_capacity_step_keyboard(),
            parse_mode="HTML",
        )
        return

    await _enter_limitations_step(message.from_user.id, state, cap, message)


@router.callback_query(
    GroupChatCreateState.configuring_limitations,
    F.data.startswith("grp_lim_gender:") | F.data.startswith("grp_lim_orient:") | F.data.startswith("grp_lim_verified:"),
)
async def handle_group_limitations_toggle(callback: CallbackQuery, state: FSMContext) -> None:
    """Step 4 Toggles: Toggle gender, orientation, or verified_only while enforcing the Anti-Lurker Rule."""
    user_id = callback.from_user.id
    async with get_db_session() as session:
        user = await get_or_create_user(session, user_id=user_id)

    data = await state.get_data()
    allowed_gender = data.get("allowed_gender")
    allowed_orientation = data.get("allowed_orientation")
    verified_only = bool(data.get("verified_only", False))

    cb = callback.data
    if cb.startswith("grp_lim_gender:"):
        target_gender = cb.split(":", 1)[1].strip().lower()
        if (user.sex or "").strip().lower() != target_gender:
            await callback.answer(
                "Anti-Lurker Rule: You can only restrict a group to a gender matching your own profile!",
                show_alert=True,
            )
            return
        allowed_gender = None if allowed_gender == target_gender else target_gender

    elif cb.startswith("grp_lim_orient:"):
        target_orient = cb.split(":", 1)[1].strip().lower()
        if (user.orientation or "").strip().lower() != target_orient:
            await callback.answer(
                "Anti-Lurker Rule: You can only restrict a group to an orientation matching your own profile!",
                show_alert=True,
            )
            return
        allowed_orientation = None if allowed_orientation == target_orient else target_orient

    elif cb.startswith("grp_lim_verified:"):
        action = cb.split(":", 1)[1]
        if action == "locked" or not getattr(user, "is_verified", False):
            await callback.answer(
                "Anti-Lurker Rule: Only verified AUA students (@edu.aua.am) can enable 'Only Verified Users'. Use /verify first!",
                show_alert=True,
            )
            return
        verified_only = not verified_only

    ok, err = validate_creator_anti_lurker(user, allowed_gender, allowed_orientation, verified_only)
    if not ok:
        await callback.answer(err, show_alert=True)
        return

    await state.update_data(
        allowed_gender=allowed_gender,
        allowed_orientation=allowed_orientation,
        verified_only=verified_only,
    )

    text = _build_limitations_step_text(
        name=data.get("name", "Group"),
        description=data.get("description"),
        max_participants=int(data.get("max_participants", 10)),
        allowed_gender=allowed_gender,
        allowed_orientation=allowed_orientation,
        verified_only=verified_only,
    )
    kb = get_group_limitations_keyboard(
        creator_sex=user.sex,
        creator_orientation=user.orientation,
        creator_is_verified=bool(getattr(user, "is_verified", False)),
        allowed_gender=allowed_gender,
        allowed_orientation=allowed_orientation,
        verified_only=verified_only,
    )
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except TelegramAPIError:
        pass
    await callback.answer()


@router.callback_query(GroupChatCreateState.configuring_limitations, F.data == "grp_lims_continue")
async def handle_group_limitations_continue(callback: CallbackQuery, state: FSMContext) -> None:
    """Step 4 Continue -> Step 5 (Creator Nickname Selection)."""
    random_nick = generate_unique_random_nickname(set())
    await state.update_data(suggested_random_nickname=random_nick)
    await state.set_state(GroupChatCreateState.waiting_for_creator_nickname)

    text = (
        "🎭 <b>Step 5/5: Choose Your Group Nickname</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Pick an anonymous nickname that will be shown on your messages in this group:\n"
        "• 2–20 characters (letters, numbers, spaces, <code>_</code>, <code>-</code>)\n"
        "• Or tap the button below to use a random nickname!"
    )
    kb = get_group_nickname_keyboard(random_nickname=random_nick, cancel_callback="grp_create_cancel")
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except TelegramAPIError:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


async def _finalize_group_creation(
    user_id: int,
    username: Optional[str],
    chosen_nickname: str,
    state: FSMContext,
    message_or_callback: Message | CallbackQuery,
) -> None:
    """Validate nickname, create group + creator membership, clear state, and send confirmation."""
    ok, clean_nick, err = validate_group_nickname(chosen_nickname, taken_lower=set())
    if not ok:
        data = await state.get_data()
        rand_nick = data.get("suggested_random_nickname") or generate_unique_random_nickname(set())
        err_html = f"⚠️ <b>Invalid Nickname:</b> {html.escape(err)}\n\nPlease enter a valid nickname (2–20 chars):"
        kb = get_group_nickname_keyboard(random_nickname=rand_nick, cancel_callback="grp_create_cancel")
        if isinstance(message_or_callback, CallbackQuery):
            await message_or_callback.answer(err, show_alert=True)
        else:
            await message_or_callback.answer(err_html, reply_markup=kb, parse_mode="HTML")
        return

    data = await state.get_data()
    name = data.get("name", "Campus Group")
    description = data.get("description")
    max_participants = int(data.get("max_participants", 10))
    allowed_gender = data.get("allowed_gender")
    allowed_orientation = data.get("allowed_orientation")
    verified_only = bool(data.get("verified_only", False))

    async with get_db_session() as session:
        user = await get_or_create_user(session, user_id=user_id, username=username)
        group, member, create_err = await create_group_chat_with_creator(
            session=session,
            creator=user,
            name=name,
            description=description,
            max_participants=max_participants,
            allowed_gender=allowed_gender,
            allowed_orientation=allowed_orientation,
            verified_only=verified_only,
            creator_nickname=clean_nick,
        )

    await state.clear()
    if not group or not member:
        msg_err = f"⚠️ <b>Could not create group:</b> {html.escape(create_err)}"
        if isinstance(message_or_callback, CallbackQuery):
            try:
                await message_or_callback.message.edit_text(msg_err, parse_mode="HTML")
            except TelegramAPIError:
                await message_or_callback.message.answer(msg_err, parse_mode="HTML")
            await message_or_callback.answer()
        else:
            await message_or_callback.answer(msg_err, parse_mode="HTML")
        return

    welcome_html = _build_in_group_welcome_text(group, member.nickname, current_count=1, is_creator=True)
    if isinstance(message_or_callback, CallbackQuery):
        try:
            await message_or_callback.message.edit_text(welcome_html, parse_mode="HTML")
        except TelegramAPIError:
            await message_or_callback.message.answer(welcome_html, parse_mode="HTML")
        await message_or_callback.answer("Group created!")
    else:
        await message_or_callback.answer(welcome_html, parse_mode="HTML")


@router.callback_query(GroupChatCreateState.waiting_for_creator_nickname, F.data.startswith("grp_nick_random:"))
async def handle_group_create_random_nickname(callback: CallbackQuery, state: FSMContext) -> None:
    """Step 5 Random Nickname Button: Finalize group creation with the pre-generated nickname."""
    random_nick = callback.data.split(":", 1)[1]
    await _finalize_group_creation(
        user_id=callback.from_user.id,
        username=callback.from_user.username,
        chosen_nickname=random_nick,
        state=state,
        message_or_callback=callback,
    )


@router.message(GroupChatCreateState.waiting_for_creator_nickname, F.text)
async def handle_group_create_custom_nickname(message: Message, state: FSMContext) -> None:
    """Step 5 Custom Nickname Input: Validate and finalize group creation."""
    if message.text.strip().lower() in ("/cancel", "/stop"):
        await state.clear()
        await message.answer("✅ <b>Group creation cancelled.</b>", parse_mode="HTML")
        return

    if message.text.startswith("/"):
        return

    await _finalize_group_creation(
        user_id=message.from_user.id,
        username=message.from_user.username,
        chosen_nickname=message.text,
        state=state,
        message_or_callback=message,
    )


# ==============================================================================
# Atomic Joining & Sticky Nicknames
# ==============================================================================

@router.callback_query(F.data.startswith("grp_join:"))
async def handle_group_join_click(callback: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    """Handle [Join Group] click: restore sticky nickname if returning, or open nickname picker."""
    await state.clear()
    parts = callback.data.split(":")
    try:
        group_id = int(parts[1])
        back_page = max(1, int(parts[2])) if len(parts) > 2 else 1
    except (IndexError, ValueError):
        await callback.answer("Invalid group.", show_alert=True)
        return

    user_id = callback.from_user.id
    username = callback.from_user.username

    async with get_db_session() as session:
        user = await get_or_create_user(session, user_id=user_id, username=username)
        group, member, current_count, status_or_err = await join_group_chat_atomic(
            session=session,
            user=user,
            group_id=group_id,
            requested_nickname=None,
        )

        if status_or_err == "already_in_this_group" and group and member:
            await callback.answer("You are already in this group!", show_alert=True)
            return

        if status_or_err == "needs_nickname" and group:
            taken_lower = await get_all_taken_nicknames_in_group(session, group_id)
            random_nick = generate_unique_random_nickname(taken_lower)
            await state.set_state(GroupChatJoinState.waiting_for_nickname)
            await state.update_data(
                join_group_id=group_id,
                join_back_page=back_page,
                suggested_random_nickname=random_nick,
            )

            prompt_html = (
                f"🎭 <b>Join {html.escape(group.name)} — Choose Your Nickname</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "Type a custom nickname (2–20 characters: letters, numbers, spaces, <code>_</code>, <code>-</code>) "
                "or tap the button below to use a random nickname:\n\n"
                "🔒 <i>Note: Your nickname is sticky in this group and will be remembered if you leave and rejoin.</i>"
            )
            kb = get_group_nickname_keyboard(
                random_nickname=random_nick,
                cancel_callback=f"grp_view:{group_id}:{back_page}",
            )
            try:
                await callback.message.edit_text(prompt_html, reply_markup=kb, parse_mode="HTML")
            except TelegramAPIError:
                await callback.message.answer(prompt_html, reply_markup=kb, parse_mode="HTML")
            await callback.answer()
            return

        if status_or_err or not group or not member:
            await callback.answer(status_or_err or "Could not join group.", show_alert=True)
            return

        # Sticky nickname was automatically restored! Broadcast join to other members
        safe_nick = html.escape(member.nickname)
        join_broadcast = f"🎭 <b>{safe_nick}</b> joined the group ({current_count}/{group.max_participants})"
        await broadcast_group_system_message(
            bot=bot,
            session=session,
            group_id=group.id,
            html_text=join_broadcast,
            exclude_user_id=user_id,
        )

    welcome_html = _build_in_group_welcome_text(group, member.nickname, current_count=current_count, is_creator=False)
    try:
        await callback.message.edit_text(welcome_html, parse_mode="HTML")
    except TelegramAPIError:
        await callback.message.answer(welcome_html, parse_mode="HTML")
    await callback.answer(f"Joined as {member.nickname}!")


async def _finalize_group_join_with_nickname(
    bot: Bot,
    user_id: int,
    username: Optional[str],
    raw_nickname: str,
    state: FSMContext,
    message_or_callback: Message | CallbackQuery,
) -> None:
    """Validate nickname for joining user, commit membership atomically, and broadcast join message."""
    data = await state.get_data()
    group_id = data.get("join_group_id")
    back_page = int(data.get("join_back_page", 1))
    if not group_id:
        await state.clear()
        return

    async with get_db_session() as session:
        taken_lower = await get_all_taken_nicknames_in_group(session, group_id)
        ok, clean_nick, err = validate_group_nickname(raw_nickname, taken_lower=taken_lower)
        if not ok:
            rand_nick = generate_unique_random_nickname(taken_lower)
            await state.update_data(suggested_random_nickname=rand_nick)
            kb = get_group_nickname_keyboard(
                random_nickname=rand_nick,
                cancel_callback=f"grp_view:{group_id}:{back_page}",
            )
            if isinstance(message_or_callback, CallbackQuery):
                await message_or_callback.answer(err, show_alert=True)
                try:
                    await message_or_callback.message.edit_reply_markup(reply_markup=kb)
                except TelegramAPIError:
                    pass
            else:
                await message_or_callback.answer(
                    f"⚠️ <b>Invalid Nickname:</b> {html.escape(err)}\n\nPlease enter another nickname (2–20 chars):",
                    reply_markup=kb,
                    parse_mode="HTML",
                )
            return

        user = await get_or_create_user(session, user_id=user_id, username=username)
        group, member, current_count, join_err = await join_group_chat_atomic(
            session=session,
            user=user,
            group_id=group_id,
            requested_nickname=clean_nick,
        )
        if join_err or not group or not member:
            await state.clear()
            err_msg = f"⚠️ <b>Could not join group:</b> {html.escape(join_err or 'Group unavailable.')}"
            if isinstance(message_or_callback, CallbackQuery):
                await message_or_callback.answer(join_err or "Could not join group.", show_alert=True)
            else:
                await message_or_callback.answer(err_msg, parse_mode="HTML")
            return

        safe_nick = html.escape(member.nickname)
        join_broadcast = f"🎭 <b>{safe_nick}</b> joined the group ({current_count}/{group.max_participants})"
        await broadcast_group_system_message(
            bot=bot,
            session=session,
            group_id=group.id,
            html_text=join_broadcast,
            exclude_user_id=user_id,
        )

    await state.clear()
    welcome_html = _build_in_group_welcome_text(group, member.nickname, current_count=current_count, is_creator=False)
    if isinstance(message_or_callback, CallbackQuery):
        try:
            await message_or_callback.message.edit_text(welcome_html, parse_mode="HTML")
        except TelegramAPIError:
            await message_or_callback.message.answer(welcome_html, parse_mode="HTML")
        await message_or_callback.answer(f"Joined as {member.nickname}!")
    else:
        await message_or_callback.answer(welcome_html, parse_mode="HTML")


@router.callback_query(GroupChatJoinState.waiting_for_nickname, F.data.startswith("grp_nick_random:"))
async def handle_group_join_random_nickname(callback: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    """Join group using the pre-generated random nickname button."""
    random_nick = callback.data.split(":", 1)[1]
    await _finalize_group_join_with_nickname(
        bot=bot,
        user_id=callback.from_user.id,
        username=callback.from_user.username,
        raw_nickname=random_nick,
        state=state,
        message_or_callback=callback,
    )


@router.message(GroupChatJoinState.waiting_for_nickname, F.text)
async def handle_group_join_custom_nickname(message: Message, bot: Bot, state: FSMContext) -> None:
    """Join group using a typed custom nickname."""
    if message.text.strip().lower() in ("/cancel", "/stop"):
        await state.clear()
        await message.answer("✅ <b>Joining group cancelled.</b>", parse_mode="HTML")
        return

    if message.text.startswith("/"):
        return

    await _finalize_group_join_with_nickname(
        bot=bot,
        user_id=message.from_user.id,
        username=message.from_user.username,
        raw_nickname=message.text,
        state=state,
        message_or_callback=message,
    )


# ==============================================================================
# In-Group Lifecycle Commands: /leave, /members, and /report
# ==============================================================================

async def execute_leave_group_for_user(
    bot: Bot,
    session: AsyncSession,
    user_id: int,
    message: Message,
) -> bool:
    """Mark active group member inactive, broadcast departure, and confirm exit. Returns True if user was in a group."""
    res = await leave_group_chat(session, user_id=user_id)
    if not res:
        return False

    group, member, remaining_count, group_closed = res
    safe_nick = html.escape(member.nickname)
    safe_group_name = html.escape(group.name)

    if group_closed:
        group_reply_buffer.clear_group(group.id)
    else:
        leave_broadcast = f"👋 <b>{safe_nick}</b> left the group ({remaining_count}/{group.max_participants})"
        await broadcast_group_system_message(
            bot=bot,
            session=session,
            group_id=group.id,
            html_text=leave_broadcast,
            exclude_user_id=user_id,
        )

    await message.answer(
        f"👋 <b>You left {safe_group_name}.</b>\n\n"
        f"Your nickname (<b>{safe_nick}</b>) remains reserved for you if you rejoin later.\n"
        "Send /groups to browse group chats or /search for a 1-on-1 chat! 🎭",
        parse_mode="HTML",
    )
    return True


@router.message(Command("leave"))
async def handle_leave_command(message: Message, bot: Bot, state: FSMContext) -> None:
    """Handle /leave command to exit an active anonymous group chat."""
    await state.clear()
    user_id = message.from_user.id
    async with get_db_session() as session:
        was_in_group = await execute_leave_group_for_user(bot, session, user_id, message)

    if not was_in_group:
        await message.answer(
            "💬 <b>You are not currently in an active group chat.</b>\n\n"
            "Send /groups to browse or create a group chat!",
            parse_mode="HTML",
        )


@router.message(Command("members"))
async def handle_members_command(message: Message) -> None:
    """Show group name, current/max count, and list of active member nicknames (marking requester with (you))."""
    user_id = message.from_user.id
    async with get_db_session() as session:
        active_res = await get_user_active_group_member(session, user_id)
        if not active_res:
            await message.answer(
                "💬 <b>You are not currently in an active group chat.</b>\n\n"
                "Send /groups to browse or join a group chat!",
                parse_mode="HTML",
            )
            return

        _, group = active_res
        members = await get_active_group_members(session, group.id)

    safe_group_name = html.escape(group.name)
    lines = [
        f"👥 <b>{safe_group_name} — Active Members ({len(members)}/{group.max_participants})</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    for idx, m in enumerate(members, start=1):
        safe_nick = html.escape(m.nickname)
        you_tag = " <i>(you)</i>" if m.user_id == user_id else ""
        lines.append(f"{idx}. 🎭 <b>{safe_nick}</b>{you_tag}")

    await message.answer("\n".join(lines), parse_mode="HTML")


async def open_group_report_menu(
    session: AsyncSession,
    user_id: int,
    group: GroupChat,
    message: Message,
    state: FSMContext,
) -> None:
    """Display inline list of reportable participants (active + recently-left in 100-msg window)."""
    allowed, cooldown_msg = group_ai_moderator.check_anti_stampede_and_cooldown(group.id, user_id)
    if not allowed:
        await message.answer(cooldown_msg, parse_mode="HTML")
        return

    participants = await get_reportable_group_participants(
        session=session,
        group_id=group.id,
        reporter_user_id=user_id,
    )
    if not participants:
        await message.answer(
            "ℹ️ <b>No reportable participants found in this group yet.</b>",
            parse_mode="HTML",
        )
        return

    await state.set_state(GroupChatReportState.selecting_target)
    await state.update_data(report_group_id=group.id)

    kb = get_group_report_targets_keyboard(participants, group_id=group.id)
    await message.answer(
        "🚨 <b>Report Group Participant to AI Moderator</b> 🛡️\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Select the participant you are reporting (includes active members and anyone who spoke in the last 100 messages and left):\n\n"
        "🤖 <i>Note: Our Multi-Party AI Moderator evaluates the full 100-message context across all participants.</i>",
        reply_markup=kb,
        parse_mode="HTML",
    )


@router.callback_query(F.data == "grp_rep_cancel")
async def handle_group_report_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    """Cancel in-group report selection."""
    await state.clear()
    try:
        await callback.message.edit_text("✅ <b>Group report cancelled.</b>", parse_mode="HTML")
    except TelegramAPIError:
        pass
    await callback.answer("Cancelled.")


@router.callback_query(F.data.startswith("grp_rep_target:"))
async def handle_group_report_target_selected(callback: CallbackQuery, state: FSMContext) -> None:
    """Prompt reporter for a quick reason button or typed explanation for the selected target."""
    parts = callback.data.split(":")
    try:
        group_id = int(parts[1])
        target_uid = int(parts[2])
    except (IndexError, ValueError):
        await callback.answer("Invalid selection.", show_alert=True)
        return

    async with get_db_session() as session:
        member_rec = await get_group_member_record(session, group_id, target_uid)
        target_nick = member_rec.nickname if member_rec else "Participant"

    await state.set_state(GroupChatReportState.waiting_for_reason)
    await state.update_data(
        report_group_id=group_id,
        report_target_user_id=target_uid,
        report_target_nickname=target_nick,
    )

    safe_nick = html.escape(target_nick)
    text = (
        f"🚨 <b>Reporting: {safe_nick}</b> 🛡️\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Select a violation category below, or type a brief description of what happened:"
    )
    kb = get_group_report_reasons_keyboard(group_id=group_id, target_user_id=target_uid)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except TelegramAPIError:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


_REASON_KEY_LABELS = {
    "harassment": "Harassment / Bullying",
    "hate_speech": "Hate Speech / Slurs",
    "spam_scam": "Spam / Scam",
    "nsfw_illegal": "NSFW / Illegal Content",
    "other": "Other Community Rule Violation",
}


@router.callback_query(F.data.startswith("grp_rep_reason:"))
async def handle_group_report_reason_button(callback: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    """Execute multi-party AI moderation when a quick reason button is clicked."""
    parts = callback.data.split(":")
    try:
        group_id = int(parts[1])
        target_uid = int(parts[2])
        reason_key = parts[3]
    except (IndexError, ValueError):
        await callback.answer("Invalid report action.", show_alert=True)
        return

    await state.clear()
    reason_label = _REASON_KEY_LABELS.get(reason_key, "Community rule violation")
    reporter_id = callback.from_user.id

    try:
        await callback.message.edit_text(
            "🤖 <b>Multi-Party AI Safety Moderator Evaluating Group Transcript...</b>\n\n"
            "Analyzing the latest 100 messages across all participants. Please wait...",
            parse_mode="HTML",
        )
    except TelegramAPIError:
        pass
    await callback.answer()

    async with get_db_session() as session:
        _, reply_html, _ = await group_ai_moderator.run_group_moderation(
            bot=bot,
            session=session,
            group_id=group_id,
            reporter_user_id=reporter_id,
            reported_user_id=target_uid,
            reason_text=reason_label,
        )

    try:
        await callback.message.edit_text(reply_html, parse_mode="HTML")
    except TelegramAPIError:
        await callback.message.answer(reply_html, parse_mode="HTML")


@router.message(GroupChatReportState.waiting_for_reason, F.text)
async def handle_group_report_reason_text(message: Message, bot: Bot, state: FSMContext) -> None:
    """Execute multi-party AI moderation when a custom text reason is typed."""
    if message.text.strip().lower() in ("/cancel", "/stop"):
        await state.clear()
        await message.answer("✅ <b>Group report cancelled.</b>", parse_mode="HTML")
        return

    if message.text.startswith("/"):
        return

    data = await state.get_data()
    group_id = data.get("report_group_id")
    target_uid = data.get("report_target_user_id")
    await state.clear()

    if not group_id or not target_uid:
        await message.answer("⚠️ <b>Report session expired.</b> Send /report to try again.", parse_mode="HTML")
        return

    status_msg = await message.answer(
        "🤖 <b>Multi-Party AI Safety Moderator Evaluating Group Transcript...</b>\n\n"
        "Analyzing the latest 100 messages across all participants. Please wait...",
        parse_mode="HTML",
    )

    async with get_db_session() as session:
        _, reply_html, _ = await group_ai_moderator.run_group_moderation(
            bot=bot,
            session=session,
            group_id=int(group_id),
            reporter_user_id=message.from_user.id,
            reported_user_id=int(target_uid),
            reason_text=message.text.strip(),
        )

    try:
        await status_msg.edit_text(reply_html, parse_mode="HTML")
    except TelegramAPIError:
        await message.answer(reply_html, parse_mode="HTML")
