import html
from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.database.crud import (
    get_or_create_user,
    get_user,
    update_user_preferences,
    update_user_profile,
)
from app.database.session import get_db_session
from app.keyboards.inline import (
    get_orientation_keyboard,
    get_preferences_keyboard,
    get_sex_keyboard,
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
                "💬 <b>You are currently in an active anonymous chat!</b> 🦅🇦🇲\n\n"
                "Your connection is active. Any message you send here will be delivered to your partner.\n\n"
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
                "🔍 <b>You are currently in the search queue...</b> 🦅🇦🇲\n\n"
                "Please wait while we connect you with a compatible partner, or send /stop to cancel.",
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
                f"👋 <b>Welcome back to Anonymous AUA Chat, {safe_name}!</b> 🇦🇲🦅\n\n"
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
                f"• /preferences — Adjust who you connect with\n"
                f"• /profile — Update your sex or orientation\n"
                f"• /suggestion — Send feedback or ideas to improve the bot\n\n"
                f"<i>Ready to meet fellow students? Press /search to start!</i>",
                parse_mode="HTML",
            )
            return

    # 4. User needs onboarding
    await state.set_state(OnboardingState.waiting_for_sex)
    await message.answer(
        "🇦🇲 <b>Welcome to Anonymous AUA Chat!</b> 🦅\n\n"
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
    async with get_db_session() as session:
        user = await get_or_create_user(session, user_id=user_id)
        if user.current_partner_id is not None:
            await message.answer(
                "⚠️ <b>Cannot update profile during an active chat!</b>\n\n"
                "Please end your current chat with /stop before updating your profile.",
                parse_mode="HTML",
            )
            return

    await state.set_state(OnboardingState.waiting_for_sex)
    await message.answer(
        "⚙️ <b>Profile Settings</b>\n\n"
        "Please select your <b>Sex</b> to update your matching preferences:",
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

    await state.set_state(PreferencesState.configuring)
    await state.update_data(
        pref_sexes=selected_sexes,
        pref_orientations=selected_orientations,
        is_onboarding=False,
    )

    await message.answer(
        "⚙️ <b>Matching Preferences</b>\n\n"
        "Select who you want to be able to connect with in anonymous chats.\n"
        "Matches occur when <b>both</b> students' preferences mutually match!\n\n"
        "<i>Toggle checkboxes below and press 'Save Preferences':</i>",
        reply_markup=get_preferences_keyboard(selected_sexes, selected_orientations),
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
                "You are still connected to your partner. Continue chatting! 🦅🇦🇲",
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
    sex = callback.data.split(":")[1]
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
        is_onboarding=True,
    )

    await callback.message.edit_text(
        "🎯 <b>Who would you like to connect with?</b>\n\n"
        "By default, your preferences are set for finding a romantic partner. "
        "You can also select if you'd like to talk to other sexes and orientations!\n\n"
        "<i>Toggle checkboxes below and press 'Save Preferences' when ready:</i>",
        reply_markup=get_preferences_keyboard(selected_sexes, selected_orientations),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("pref_toggle:"))
async def handle_preference_toggle(callback: CallbackQuery, state: FSMContext) -> None:
    """Toggle a sex or orientation checkbox in the preferences menu."""
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer()
        return

    category, item = parts[1], parts[2].lower().strip()
    data = await state.get_data()
    selected_sexes = list(data.get("pref_sexes") or [])
    selected_orientations = list(data.get("pref_orientations") or [])

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
            reply_markup=get_preferences_keyboard(selected_sexes, selected_orientations)
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
        )

    await state.clear()

    formatted_sexes = ", ".join(s.capitalize() for s in selected_sexes)
    formatted_orients = ", ".join(o.capitalize() for o in selected_orientations)

    if is_onboarding:
        await callback.message.edit_text(
            "🎉 <b>You're all set! Welcome to the AUA Community!</b> 🦅🇦🇲\n\n"
            f"👤 <b>Your Profile:</b>\n"
            f"• Sex: <b>{user.sex.capitalize() if user.sex else 'Not set'}</b>\n"
            f"• Orientation: <b>{user.orientation.capitalize() if user.orientation else 'Not set'}</b>\n\n"
            f"🎯 <b>Matching Preferences:</b>\n"
            f"• Target Sexes: <b>{formatted_sexes}</b>\n"
            f"• Target Orientations: <b>{formatted_orients}</b>\n\n"
            "⚡ <b>How to Use:</b>\n"
            "• /search — Find an anonymous partner\n"
            "• /next — Skip to the next student\n"
            "• /stop — End chat or cancel search\n"
            "• /preferences — Adjust who you connect with\n"
            "• /profile — Update your sex or orientation\n"
            "• /suggestion — Send ideas to bot admins\n\n"
            "✨ <i>Remember to be kind and respectful. Have fun connecting!</i>\n"
            "Press /search to begin your first anonymous chat!",
            parse_mode="HTML",
        )
        await callback.answer("Preferences saved successfully!")
    else:
        await callback.message.edit_text(
            "✅ <b>Preferences Saved!</b> 🎯\n\n"
            f"• Target Sexes: <b>{formatted_sexes}</b>\n"
            f"• Target Orientations: <b>{formatted_orients}</b>\n\n"
            "Matches will only be made when both your preferences and the other student's preferences match.\n\n"
            "Press /search to find a partner! 🦅🇦🇲",
            parse_mode="HTML",
        )
        await callback.answer("Preferences saved!")
