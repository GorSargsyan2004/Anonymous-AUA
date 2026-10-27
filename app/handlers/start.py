from datetime import datetime, timezone
import html
import math
from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, ChatMemberUpdated, Message

from app.config import settings
from app.database.crud import (
    delete_user_block,
    get_or_create_user,
    get_public_community_stats,
    get_user,
    get_user_blocks,
    set_user_bot_blocked,
    set_user_typing_preference,
    update_user_preferences,
    update_user_profile,
)
from app.database.session import get_db_session
from app.keyboards.inline import (
    get_blocklist_keyboard,
    get_orientation_keyboard,
    get_preferences_keyboard,
    get_sex_keyboard,
    get_unblock_confirmation_keyboard,
)
from app.services.matcher import get_default_preferences
from app.states.states import OnboardingState, PreferencesState, SuggestionState

router = Router(name="start_router")


@router.message(CommandStart())
async def handle_start(message: Message, state: FSMContext) -> None:
    """Handle /start command. Check if profile is configured or initiate onboarding."""
    user_id = message.from_user.id
    username = message.from_user.username

    async with get_db_session() as session:
        user = await get_or_create_user(session, user_id=user_id, username=username)

        # 1. User is currently connected in an active anonymous chat
        if user.current_partner_id is not None:
            await state.clear()
            await message.answer(
                "💬 <b>You are currently in an active anonymous chat!</b> 🎭\n\n"
                "Your connection is active and private. Any message you send here will be delivered to your partner.\n\n"
                "⚡ <b>Commands:</b>\n"
                "• /next — Skip to the next partner\n"
                "• /stop — End this chat\n"
                "• /preferences — Adjust who you connect with\n"
                "• /suggestion — Send feedback or ideas to admins",
                parse_mode="HTML",
            )
            return

        # 2. User is currently waiting in the search queue
        if user.is_searching:
            await state.clear()
            await message.answer(
                "⏳ <b>You are in the matchmaking queue...</b> 🥷\n\n"
                "Looking for a compatible campus partner. Please wait a moment, or send /stop to cancel.",
                parse_mode="HTML",
            )
            return

        # 3. Profile already complete
        if user.sex and user.orientation:
            await state.clear()
            safe_name = html.escape(message.from_user.first_name or "Student")
            def_sexes, def_orients = get_default_preferences(user.sex, user.orientation)
            p_sexes = [s.strip().capitalize() for s in (user.target_sexes or "").split(",") if s.strip()] or [s.capitalize() for s in def_sexes]
            p_orients = [o.strip().capitalize() for o in (user.target_orientations or "").split(",") if o.strip()] or [o.capitalize() for o in def_orients]

            await message.answer(
                f"👋 <b>Welcome back to Anonymous AUA, {safe_name}!</b> 🎭\n\n"
                f"👤 <b>Your Profile:</b>\n"
                f"• Sex: <b>{user.sex.capitalize()}</b>\n"
                f"• Orientation: <b>{user.orientation.capitalize()}</b>\n\n"
                f"🎯 <b>Your Matching Preferences:</b>\n"
                f"• Target Sexes: <b>{', '.join(p_sexes)}</b>\n"
                f"• Target Orientations: <b>{', '.join(p_orients)}</b>\n\n"
                f"⚡ <b>Available Commands:</b>\n"
                f"• /search — Find an anonymous chat partner\n"
                f"• /next — Skip to the next partner\n"
                f"• /stop — End current chat or cancel search\n"
                f"• /pause — Put chat into Sleep Mode for the night 🌙\n"
                f"• /onetime — Toggle 15s View-Once mode for media 🔒\n"
                f"• /recent — View recent partners to rate or report\n"
                f"• /preferences — Adjust who you connect with\n"
                f"• /profile — Update your sex or orientation\n"
                f"• /verify — Verify your AUA student email (@edu.aua.am) 🎓\n"
                f"• /blocklist — View & manage blocked partners\n"
                f"• /safety — Anti-scam & blackmail protection guide\n"
                f"• /help — Show full commands & features guide\n"
                f"• /suggestion — Send feedback or ideas to improve the bot\n\n"
                f"<i>Ready to meet fellow students? Press /search to start! ×͜×</i>",
                parse_mode="HTML",
            )
            return

    # 4. User needs onboarding
    await state.set_state(OnboardingState.waiting_for_sex)
    await message.answer(
        "🎭 <b>Welcome to Anonymous AUA!</b> 🥷\n\n"
        "Connect anonymously and chat honestly with students from the "
        "<b>American University of Armenia</b>.\n\n"
        "To get started and match you with compatible students, please select your <b>Sex</b>:",
        reply_markup=get_sex_keyboard(),
        parse_mode="HTML",
    )


@router.message(Command("profile"))
async def handle_profile_reset(message: Message, state: FSMContext) -> None:
    """Allow user to update their sex and orientation."""
    user_id = message.from_user.id
    cooldown_note = ""
    async with get_db_session() as session:
        user = await get_or_create_user(session, user_id=user_id)
        if user.current_partner_id is not None:
            await message.answer(
                "⚠️ <b>Cannot update profile during an active chat!</b>\n\n"
                "Please end your current chat with /stop before updating your profile.",
                parse_mode="HTML",
            )
            return

        if user.sex and user.sex_updated_at:
            now = datetime.now(timezone.utc)
            updated_at = (
                user.sex_updated_at
                if user.sex_updated_at.tzinfo
                else user.sex_updated_at.replace(tzinfo=timezone.utc)
            )
            elapsed = (now - updated_at).total_seconds()
            cooldown_sec = settings.SEX_CHANGE_COOLDOWN_HOURS * 3600
            if elapsed < cooldown_sec:
                rem = int(cooldown_sec - elapsed)
                hours, mins = rem // 3600, (rem % 3600) // 60
                time_str = f"{hours}h {mins}m" if hours > 0 else f"{mins}m"
                cooldown_note = (
                    f"\n\n⏳ <b>Sex Change Cooldown Active:</b> Profile sex can only be changed once every 2 days "
                    f"(<b>{time_str}</b> remaining). You may re-select <b>{user.sex.capitalize()}</b> if you only want to change your orientation."
                )

    await state.set_state(OnboardingState.waiting_for_sex)
    await message.answer(
        "⚙️ <b>Profile Settings</b>\n\n"
        f"Please select your <b>Sex</b> to update your matching preferences:{cooldown_note}",
        reply_markup=get_sex_keyboard(),
        parse_mode="HTML",
    )


@router.message(Command("preferences"))
async def handle_preferences_command(message: Message, state: FSMContext) -> None:
    """Allow user to view and adjust their partner matching preferences."""
    user_id = message.from_user.id
    async with get_db_session() as session:
        user = await get_or_create_user(session, user_id=user_id)
        if user.current_partner_id is not None:
            await message.answer(
                "⚠️ <b>Cannot update preferences during an active chat!</b>\n\n"
                "Please end your current chat with /stop before updating preferences.",
                parse_mode="HTML",
            )
            return

        if not user.sex or not user.orientation:
            await message.answer(
                "⚠️ <b>Please complete your profile setup first!</b>\n\n"
                "Send /start to set your sex and sexual orientation.",
                parse_mode="HTML",
            )
            return

        def_sexes, def_orients = get_default_preferences(user.sex, user.orientation)
        selected_sexes = [
            s.strip().lower()
            for s in (user.target_sexes or "").split(",")
            if s.strip()
        ] or def_sexes
        selected_orientations = [
            o.strip().lower()
            for o in (user.target_orientations or "").split(",")
            if o.strip()
        ] or def_orients
        show_typing = getattr(user, "show_typing", True)
        if show_typing is None:
            show_typing = True

    await state.set_state(PreferencesState.configuring)
    await state.update_data(
        pref_sexes=selected_sexes,
        pref_orientations=selected_orientations,
        pref_show_typing=show_typing,
        is_onboarding=False,
    )

    await message.answer(
        "⚙️ <b>Preferences</b>\n\n"
        "• Select who you want to be able to connect with in anonymous chats.\n"
        "• Matches occur when <b>both</b> students' preferences mutually match!\n"
        "• Toggle whether your partner sees when you are typing.\n\n"
        "<i>Toggle checkboxes below and press 'Save Preferences':</i>",
        reply_markup=get_preferences_keyboard(selected_sexes, selected_orientations, show_typing=show_typing),
        parse_mode="HTML",
    )


@router.message(Command("cancel"))
async def handle_cancel(message: Message, state: FSMContext) -> None:
    """Cancel any active input/FSM state without interrupting an active chat."""
    current_state = await state.get_state()
    await state.clear()
    user_id = message.from_user.id

    async with get_db_session() as session:
        user = await get_or_create_user(session, user_id=user_id)
        if user.current_partner_id is not None:
            await message.answer(
                "✅ <b>Action cancelled.</b>\n\n"
                "You are still connected to your partner. Continue chatting! 🎭",
                parse_mode="HTML",
            )
            return

    if current_state:
        await message.answer("✅ <b>Action cancelled.</b>", parse_mode="HTML")
    else:
        await message.answer("💬 No active action to cancel.", parse_mode="HTML")


@router.message(Command("suggestion"))
async def handle_suggestion_command(message: Message, state: FSMContext) -> None:
    """Allow user to submit feedback, ideas, or feature requests to administrators."""
    user_id = message.from_user.id
    async with get_db_session() as session:
        user = await get_or_create_user(session, user_id=user_id)
        if user.last_suggestion_date:
            now = datetime.now(timezone.utc)
            last_date = (
                user.last_suggestion_date
                if user.last_suggestion_date.tzinfo
                else user.last_suggestion_date.replace(tzinfo=timezone.utc)
            )
            elapsed = (now - last_date).total_seconds()
            cooldown_sec = settings.SUGGESTION_COOLDOWN_MINUTES * 60
            if elapsed < cooldown_sec:
                rem = int(cooldown_sec - elapsed)
                mins, secs = rem // 60, rem % 60
                time_str = f"{mins}m {secs}s" if mins > 0 else f"{secs}s"
                await message.answer(
                    f"⏳ <b>Suggestion Cooldown Active:</b>\n\n"
                    f"Thank you for sharing your thoughts! To prevent inbox spam, suggestions can be submitted once every {settings.SUGGESTION_COOLDOWN_MINUTES} minutes.\n\n"
                    f"Please wait <b>{time_str}</b> before submitting another suggestion.",
                    parse_mode="HTML",
                )
                return

    await state.set_state(SuggestionState.waiting_for_suggestion)
    await message.answer(
        "💡 <b>Send Us Your Suggestions & Feedback</b>\n\n"
        "Have an idea to improve the bot, found a bug, or want a new feature for AUA students?\n\n"
        "✍️ <i>Please write your message below and send it. Our administrative team will review it and reply back to you!</i>\n\n"
        "<i>(Send /cancel at any time to abort)</i>",
        parse_mode="HTML",
    )


@router.callback_query(OnboardingState.waiting_for_sex, F.data.startswith("onboarding_sex:"))
async def handle_sex_selected(callback: CallbackQuery, state: FSMContext) -> None:
    """Handle sex selection and transition to orientation selection."""
    user_id = callback.from_user.id
    sex = callback.data.split(":")[1]

    async with get_db_session() as session:
        user = await get_or_create_user(session, user_id=user_id)
        if user.sex and user.sex_updated_at and user.sex.lower().strip() != sex.lower().strip():
            now = datetime.now(timezone.utc)
            updated_at = (
                user.sex_updated_at
                if user.sex_updated_at.tzinfo
                else user.sex_updated_at.replace(tzinfo=timezone.utc)
            )
            elapsed = (now - updated_at).total_seconds()
            cooldown_sec = settings.SEX_CHANGE_COOLDOWN_HOURS * 3600
            if elapsed < cooldown_sec:
                rem = int(cooldown_sec - elapsed)
                hours, mins = rem // 3600, (rem % 3600) // 60
                time_str = f"{hours}h {mins}m" if hours > 0 else f"{mins}m"
                await callback.answer(
                    f"⏳ Profile sex can only be changed once every 2 days.\nPlease wait {time_str}.",
                    show_alert=True,
                )
                return

    await state.update_data(sex=sex)
    await state.set_state(OnboardingState.waiting_for_orientation)

    await callback.message.edit_text(
        f"Selected sex: <b>{sex.capitalize()}</b>\n\n"
        "Next, please select your <b>Sexual Orientation</b>:",
        reply_markup=get_orientation_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(
    OnboardingState.waiting_for_orientation, F.data.startswith("onboarding_orientation:")
)
async def handle_orientation_selected(callback: CallbackQuery, state: FSMContext) -> None:
    """Handle orientation selection, initialize default preferences, and prompt preferences menu."""
    orientation = callback.data.split(":")[1]
    data = await state.get_data()
    sex = data.get("sex", "male")

    async with get_db_session() as session:
        user = await update_user_profile(
            session=session,
            user_id=callback.from_user.id,
            sex=sex,
            orientation=orientation,
        )
        def_sexes, def_orients = get_default_preferences(user.sex, user.orientation)
        selected_sexes = [
            s.strip().lower()
            for s in (user.target_sexes or "").split(",")
            if s.strip()
        ] or def_sexes
        selected_orientations = [
            o.strip().lower()
            for o in (user.target_orientations or "").split(",")
            if o.strip()
        ] or def_orients

    await state.set_state(PreferencesState.configuring)
    await state.update_data(
        pref_sexes=selected_sexes,
        pref_orientations=selected_orientations,
        pref_show_typing=True,
        is_onboarding=True,
    )

    await callback.message.edit_text(
        "🎯 <b>Who would you like to connect with?</b>\n\n"
        "By default, your preferences are set for finding a romantic partner. "
        "You can also select if you'd like to talk to other sexes and orientations, "
        "and toggle whether partners see when you are typing!\n\n"
        "<i>Toggle checkboxes below and press 'Save Preferences' when ready:</i>",
        reply_markup=get_preferences_keyboard(selected_sexes, selected_orientations, show_typing=True),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("pref_toggle:"))
async def handle_preference_toggle(callback: CallbackQuery, state: FSMContext) -> None:
    """Toggle a sex, orientation, or typing status checkbox in the preferences menu."""
    parts = callback.data.split(":")
    if len(parts) < 2:
        await callback.answer()
        return

    category = parts[1]
    data = await state.get_data()
    selected_sexes = list(data.get("pref_sexes") or [])
    selected_orientations = list(data.get("pref_orientations") or [])
    show_typing = data.get("pref_show_typing", True)

    # If state was lost or empty, recover from database
    if not selected_sexes or not selected_orientations:
        async with get_db_session() as session:
            user = await get_user(session, user_id=callback.from_user.id)
            if user:
                def_sexes, def_orients = get_default_preferences(user.sex, user.orientation)
                selected_sexes = [
                    s.strip().lower()
                    for s in (user.target_sexes or "").split(",")
                    if s.strip()
                ] or def_sexes
                selected_orientations = [
                    o.strip().lower()
                    for o in (user.target_orientations or "").split(",")
                    if o.strip()
                ] or def_orients
                if user.show_typing is not None:
                    show_typing = user.show_typing

    if category == "typing":
        show_typing = not show_typing
        await state.update_data(pref_show_typing=show_typing)
        try:
            await callback.message.edit_reply_markup(
                reply_markup=get_preferences_keyboard(
                    selected_sexes, selected_orientations, show_typing=show_typing
                )
            )
        except Exception:
            pass
        status_label = "enabled" if show_typing else "disabled (stealth mode)"
        await callback.answer(f"Typing status {status_label}")
        return

    if len(parts) != 3:
        await callback.answer()
        return

    item = parts[2].lower().strip()
    if category == "sex":
        if item in selected_sexes:
            if len(selected_sexes) <= 1:
                await callback.answer("⚠️ You must select at least one sex!", show_alert=True)
                return
            selected_sexes.remove(item)
        else:
            selected_sexes.append(item)
    elif category == "orient":
        if item in selected_orientations:
            if len(selected_orientations) <= 1:
                await callback.answer("⚠️ You must select at least one orientation!", show_alert=True)
                return
            selected_orientations.remove(item)
        else:
            selected_orientations.append(item)

    await state.update_data(pref_sexes=selected_sexes, pref_orientations=selected_orientations)

    try:
        await callback.message.edit_reply_markup(
            reply_markup=get_preferences_keyboard(
                selected_sexes, selected_orientations, show_typing=show_typing
            )
        )
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data == "pref_save")
async def handle_preference_save(callback: CallbackQuery, state: FSMContext) -> None:
    """Save selected preferences to DB and show confirmation or finalize onboarding."""
    data = await state.get_data()
    selected_sexes = list(data.get("pref_sexes") or [])
    selected_orientations = list(data.get("pref_orientations") or [])
    show_typing = data.get("pref_show_typing", True)
    is_onboarding = data.get("is_onboarding", False)

    async with get_db_session() as session:
        user = await get_user(session, user_id=callback.from_user.id)
        if not user:
            user = await get_or_create_user(session, user_id=callback.from_user.id)

        if not selected_sexes or not selected_orientations:
            def_sexes, def_orients = get_default_preferences(user.sex, user.orientation)
            if not selected_sexes:
                selected_sexes = [
                    s.strip().lower()
                    for s in (user.target_sexes or "").split(",")
                    if s.strip()
                ] or def_sexes
            if not selected_orientations:
                selected_orientations = [
                    o.strip().lower()
                    for o in (user.target_orientations or "").split(",")
                    if o.strip()
                ] or def_orients

        await update_user_preferences(
            session=session,
            user_id=callback.from_user.id,
            target_sexes=selected_sexes,
            target_orientations=selected_orientations,
            show_typing=show_typing,
        )

    await state.clear()

    formatted_sexes = ", ".join(s.capitalize() for s in selected_sexes)
    formatted_orients = ", ".join(o.capitalize() for o in selected_orientations)
    typing_status_str = "Enabled ✅" if show_typing else "Disabled ❌ (Stealth Mode)"

    if is_onboarding:
        await callback.message.edit_text(
            "🎉 <b>You're all set! Welcome to the AUA Community!</b> 🎭\n\n"
            f"👤 <b>Your Profile:</b>\n"
            f"• Sex: <b>{user.sex.capitalize() if user.sex else 'Not set'}</b>\n"
            f"• Orientation: <b>{user.orientation.capitalize() if user.orientation else 'Not set'}</b>\n\n"
            f"🎯 <b>Matching Preferences:</b>\n"
            f"• Target Sexes: <b>{formatted_sexes}</b>\n"
            f"• Target Orientations: <b>{formatted_orients}</b>\n"
            f"• Typing Indicator: <b>{typing_status_str}</b>\n\n"
            "⚡ <b>How to Use:</b>\n"
            "• /search — Find an anonymous partner\n"
            "• /next — Skip to the next student\n"
            "• /stop — End chat or cancel search\n"
            "• /preferences — Adjust who you connect with & typing status\n"
            "• /profile — Update your sex or orientation\n"
            "• /suggestion — Send ideas to bot admins\n\n"
            "✨ <i>Remember to be kind and respectful. Have fun connecting!</i>\n"
            "Press /search to begin your first anonymous chat! ×͜×",
            parse_mode="HTML",
        )
        await callback.answer("Preferences saved successfully!")
    else:
        await callback.message.edit_text(
            "✅ <b>Preferences Saved!</b> 🎯\n\n"
            f"• Target Sexes: <b>{formatted_sexes}</b>\n"
            f"• Target Orientations: <b>{formatted_orients}</b>\n"
            f"• Typing Indicator: <b>{typing_status_str}</b>\n\n"
            "Matches will only be made when both your preferences and the other student's preferences match.\n\n"
            "Press /search to find a partner! 🥷",
            parse_mode="HTML",
        )
        await callback.answer("Preferences saved!")


@router.message(Command("typing"))
async def handle_typing_toggle_command(message: Message) -> None:
    """Quickly toggle typing indicator visibility without leaving current chat."""
    user_id = message.from_user.id
    async with get_db_session() as session:
        user = await get_or_create_user(session, user_id=user_id)
        current_val = getattr(user, "show_typing", True)
        if current_val is None:
            current_val = True
        new_val = not current_val
        await set_user_typing_preference(session, user_id=user_id, show_typing=new_val)

    if new_val:
        await message.answer(
            "💬 <b>Typing Indicator: Enabled ✅</b>\n\n"
            "Your chat partner will see '<i>typing...</i>' at the top of the chat when you compose messages.\n"
            "You can toggle this off anytime via /preferences or /typing.",
            parse_mode="HTML",
        )
    else:
        await message.answer(
            "🔕 <b>Typing Indicator: Disabled ❌ (Stealth Mode)</b>\n\n"
            "Your chat partner will not see when you are typing.\n"
            "You can re-enable this anytime via /preferences or /typing.",
            parse_mode="HTML",
        )


@router.message(Command("help"))
async def handle_help_command(message: Message) -> None:
    """Briefly display all available user commands and how to use the bot."""
    help_text = (
        "🎭 <b>Anonymous AUA — Help & Commands Guide</b> 🥷\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "⚡ <b>Core Chat Commands:</b>\n"
        "• /search — Find an anonymous partner matching your preferences\n"
        "• /next — Skip to the next conversation partner\n"
        "• /stop — End your current chat or leave the search queue\n"
        "• /pause (or /sleep) — Put chat in Sleep Mode for the night (freezes inactivity up to 24h) 🌙\n"
        "• /report — Emergency panic button: severs chat, auto-blocks, and accepts evidence 🚨\n"
        "• /safety — Anti-scam shield, blackmail defense & verifiable privacy facts 🛡️\n\n"
        "🔒 <b>Media & Privacy Features:</b>\n"
        "• /onetime (or /once, /viewonce) — Toggle 15s View-Once mode for outgoing media\n"
        "  ↳ <i>Tip: You can also send view-once photos/videos anytime by adding <code>#once</code> to the caption or using Telegram's 'Hide with Spoiler'! Media self-destructs after 15s and blocks screenshots/saving.</i>\n"
        "• /typing — Toggle 'typing...' stealth mode on/off\n\n"
        "⚙️ <b>Settings & Partner Management:</b>\n"
        "• /recent (or /history) — Browse recent partners to rate, change/reset feedback, or report\n"
        "• /blocklist — View and manage blocked partners with pagination\n"
        "• /preferences — Customize partner gender and orientation (mutual matching)\n"
        "• /profile — Update your own sex or sexual orientation\n\n"
        "💬 <b>In-Chat Interactive Features:</b>\n"
        "• <b>Native Replies:</b> Swipe right or tap Reply on any message to quote it with a native Telegram bubble.\n"
        "• <b>Emoji Reactions:</b> React with any emoji (👍, ❤️, 🔥, 😂) to mirror it directly onto your partner's message.\n"
        "• <b>Live Message Edits:</b> Editing text or captions automatically updates the partner's message in real time.\n"
        "• <b>Feedback Toolbar:</b> After any chat, use <code>[✏️ Change Rating]</code> to update or <code>[🗑️ Reset / Undo]</code> to completely clear feedback.\n\n"
        "📊 <b>Campus Info & Feedback:</b>\n"
        "• /verify — Verify your AUA student email (@edu.aua.am) for a verified student badge 🎓\n"
        "• /stats (or /stata) — View registered users, active chats, and community demographics\n"
        "• /suggestion — Submit ideas or suggestions directly to AUA admins\n"
        "• /help — Show this commands guide\n"
        "• /cancel — Cancel any active input / action"
    )
    await message.answer(help_text, parse_mode="HTML")


@router.message(Command("safety"))
async def handle_safety_command(message: Message) -> None:
    """Display comprehensive security, anti-scam, and blackmail protection facts."""
    safety_text = (
        "🛡️ <b>Anonymous AUA — Privacy & Safety Shield</b> 🥷\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Your safety and peace of mind are our highest priorities. Here is how Anonymous AUA protects you:\n\n"
        "🔒 <b>1. Zero Identity Exposure:</b>\n"
        "• <b>No Phone Numbers:</b> Telegram never reveals your phone number or account credentials to partners.\n"
        "• <b>No Usernames or Profiles:</b> All messages are relayed as anonymous copies stripped of sender metadata.\n"
        "• <b>No Avatar or Reaction Leaks:</b> When you react with emojis or type, the bot generates the action from its own account.\n"
        "• <b>No IP Address Sharing:</b> Connections route entirely through Telegram's encrypted data centers. Direct peer-to-peer IP exposure is technically impossible.\n\n"
        "🔗 <b>2. Automated Link & Anti-Phishing Shield:</b>\n"
        "• <b>IP-Logger Auto-Block:</b> Any message containing known tracking or IP-logger links (Grabify, 2no.co, IPLogger, etc.) or dangerous executables (.apk, .exe) is <b>instantly blocked and dropped</b>.\n"
        "• <b>Real-Time Link Unshortening:</b> Shortened links (bit.ly, tinyurl, etc.) are unmasked automatically in real-time to expose hidden traps before delivery.\n"
        "• <b>Cloudflare Zero-Trust Verification:</b> External domains are verified against Cloudflare's malware and phishing intelligence.\n"
        "• <b>Suspicious Link Warnings:</b> If a partner sends an external or unfamiliar link, the bot displays a prominent security advisory warning you not to enter credentials or passwords.\n\n"
        "🎭 <b>3. Spotting Scammer & Blackmailer Bluffs:</b>\n"
        "• <b>'I know who you are / I tracked your IP':</b> This is a <b>100% fake bluff</b>. Scammers use fear, guesswork (major/dorm floors), and intimidation to trick students.\n"
        "• <b>'Send money / photos or I will leak':</b> Never comply with blackmailers. Immediately type /report and cut them off.\n\n"
        "⚡ <b>4. What to do if someone threatens you:</b>\n"
        "• Type /report — Instantly disconnects, blocks the user, and alerts AUA admins with your evidence (text or screenshots).\n"
        "• Tap /blocklist — Verify and manage blocked users.\n"
        "• Use /stop — Terminate the chat at any second.\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<i>Have questions or need assistance? Send /suggestion to speak with bot admins.</i>"
    )
    await message.answer(safety_text, parse_mode="HTML")


@router.message(Command("stats", "stata"))
async def handle_user_stats(message: Message) -> None:
    """Display public community statistics to users."""
    async with get_db_session() as session:
        stats = await get_public_community_stats(session)

    stats_text = (
        "📊 <b>Anonymous AUA Statistics</b> 🎭\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "👥 <b>Community:</b>\n"
        f"• Registered Students: <b>{stats['total_users']}</b>\n"
        f"• Active Chats: <b>{stats['active_chats']}</b>\n\n"
        "🚻 <b>Sex:</b>\n"
        f"• Male: <b>{stats['male_count']}</b>\n"
        f"• Female: <b>{stats['female_count']}</b>\n\n"
        "🧭 <b>Orientations:</b>\n"
        f"• Straight: <b>{stats['straight_count']}</b>\n"
        f"• Bisexual: <b>{stats['bisexual_count']}</b>\n"
        f"• Lesbian: <b>{stats['lesbian_count']}</b>\n"
        f"• Gay: <b>{stats['gay_count']}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🎭 <i>American University of Armenia • Anonymous Network</i>"
    )
    await message.answer(stats_text, parse_mode="HTML")


@router.message(Command("blocklist"))
async def handle_blocklist_command(message: Message) -> None:
    """Display user's blocked partners list with pagination."""
    user_id = message.from_user.id
    async with get_db_session() as session:
        blocks, total_count = await get_user_blocks(session, user_id=user_id, page=1, page_size=5)

    if total_count == 0:
        await message.answer(
            "📋 <b>Your Blocklist is empty.</b>\n\n"
            "You haven't blocked any partners yet. After stopping any conversation, "
            "you can tap <b>'🚫 Don't connect again'</b> to avoid matching with that partner for 1 day, 3 days, 1 week, or permanently.",
            parse_mode="HTML",
        )
        return

    total_pages = math.ceil(total_count / 5)
    reply_markup = get_blocklist_keyboard(blocks, page=1, total_pages=total_pages)

    await message.answer(
        f"📋 <b>Your Blocklist ({total_count} blocked):</b>\n\n"
        "Tap any date button to unblock that partner and allow matching with them again:",
        reply_markup=reply_markup,
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("bl_page:"))
async def handle_blocklist_page(callback: CallbackQuery) -> None:
    """Handle page turning for blocklist menu."""
    page = int(callback.data.split(":")[1])
    user_id = callback.from_user.id

    async with get_db_session() as session:
        blocks, total_count = await get_user_blocks(session, user_id=user_id, page=page, page_size=5)

    if total_count == 0:
        await callback.message.edit_text(
            "📋 <b>Your Blocklist is empty.</b>\n\n"
            "You haven't blocked any partners.",
            parse_mode="HTML",
        )
        await callback.answer()
        return

    total_pages = math.ceil(total_count / 5)
    page = min(page, total_pages)
    reply_markup = get_blocklist_keyboard(blocks, page=page, total_pages=total_pages)

    await callback.message.edit_text(
        f"📋 <b>Your Blocklist ({total_count} blocked):</b>\n\n"
        "Tap any date button to unblock that partner and allow matching with them again:",
        reply_markup=reply_markup,
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "bl_noop")
async def handle_blocklist_noop(callback: CallbackQuery) -> None:
    """Handle click on page indicator or disabled arrow."""
    await callback.answer()


@router.callback_query(F.data.startswith("bl_select:"))
async def handle_blocklist_select(callback: CallbackQuery) -> None:
    """Prompt user with confirmation before unblocking a partner."""
    parts = callback.data.split(":")
    block_id = int(parts[1])
    page = int(parts[2])

    user_id = callback.from_user.id
    async with get_db_session() as session:
        from app.database.models import UserBlock
        block = await session.get(UserBlock, block_id)

    if not block or block.user_id != user_id:
        await callback.answer("Block record not found or already unblocked.", show_alert=True)
        return

    date_str = block.created_at.strftime("%Y-%m-%d %H:%M")
    await callback.message.edit_text(
        "❓ <b>Unblock Partner?</b>\n\n"
        f"Do you want to unblock the partner you blocked on <b>{date_str}</b>?\n\n"
        "Once unblocked, you will be able to connect with them again in future anonymous chats.",
        reply_markup=get_unblock_confirmation_keyboard(block_id=block_id, page=page),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("bl_unblock_confirm:"))
async def handle_unblock_confirm(callback: CallbackQuery) -> None:
    """Execute unblock action, remove block record, and refresh list."""
    parts = callback.data.split(":")
    block_id = int(parts[1])
    page = int(parts[2])
    user_id = callback.from_user.id

    async with get_db_session() as session:
        deleted = await delete_user_block(session, user_id=user_id, block_id=block_id)
        blocks, total_count = await get_user_blocks(session, user_id=user_id, page=page, page_size=5)

    if not deleted:
        await callback.answer("Already unblocked.", show_alert=True)
    else:
        await callback.answer("Partner unblocked! ✅", show_alert=False)

    if total_count == 0:
        await callback.message.edit_text(
            "✅ <b>Partner unblocked!</b>\n\n"
            "Your blocklist is now empty. You can now connect with this partner again in future chats! 🎭",
            parse_mode="HTML",
        )
        return

    total_pages = math.ceil(total_count / 5)
    page = min(page, total_pages)
    reply_markup = get_blocklist_keyboard(blocks, page=page, total_pages=total_pages)

    await callback.message.edit_text(
        f"✅ <b>Partner unblocked!</b>\n\n"
        f"📋 <b>Your Blocklist ({total_count} blocked):</b>\n\n"
        "Tap any date button to unblock that partner and allow matching with them again:",
        reply_markup=reply_markup,
        parse_mode="HTML",
    )


@router.my_chat_member()
async def handle_my_chat_member_update(event: ChatMemberUpdated) -> None:
    """Track in real-time when a user blocks/stops or unblocks/restarts the bot."""
    user_id = event.from_user.id
    new_status = event.new_chat_member.status
    is_blocked = new_status in ["kicked", "left"]
    async with get_db_session() as session:
        await set_user_bot_blocked(session, user_id=user_id, is_blocked=is_blocked)
