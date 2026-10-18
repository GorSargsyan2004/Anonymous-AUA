"""Start command, onboarding flow, and suggestion command handlers."""

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.database.crud import get_or_create_user, update_user_profile
from app.database.session import get_db_session
from app.keyboards.inline import get_orientation_keyboard, get_sex_keyboard
from app.states.states import OnboardingState, SuggestionState

router = Router(name="start_router")


@router.message(CommandStart())
async def handle_start(message: Message, state: FSMContext) -> None:
    """Handle /start command. Check if profile is configured or initiate onboarding."""
    user_id = message.from_user.id
    username = message.from_user.username

    async with get_db_session() as session:
        user = await get_or_create_user(session, user_id=user_id, username=username)

        # Profile already complete
        if user.sex and user.orientation:
            await state.clear()
            await message.answer(
                f"👋 <b>Welcome back to Anonymous AUA Chat, {message.from_user.first_name}!</b> 🇦🇲🦅\n\n"
                f"👤 <b>Your Profile:</b>\n"
                f"• Sex: <b>{user.sex.capitalize()}</b>\n"
                f"• Orientation: <b>{user.orientation.capitalize()}</b>\n\n"
                f"⚡ <b>Available Commands:</b>\n"
                f"• /search — Find an anonymous chat partner\n"
                f"• /next — Skip to the next partner\n"
                f"• /stop — End current chat or cancel search\n"
                f"• /profile — Update your profile\n"
                f"• /suggestion — Send feedback or ideas to improve the bot\n\n"
                f"<i>Ready to meet fellow students? Press /search to start!</i>",
                parse_mode="HTML",
            )
            return

    # User needs onboarding
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
    await state.set_state(OnboardingState.waiting_for_sex)
    await message.answer(
        "⚙️ <b>Profile Settings</b>\n\n"
        "Please select your <b>Sex</b> to update your matching preferences:",
        reply_markup=get_sex_keyboard(),
        parse_mode="HTML",
    )


@router.message(Command("suggestion"))
async def handle_suggestion_command(message: Message, state: FSMContext) -> None:
    """Allow user to submit feedback, ideas, or feature requests to administrators."""
    await state.set_state(SuggestionState.waiting_for_suggestion)
    await message.answer(
        "💡 <b>Send Us Your Suggestions & Feedback</b>\n\n"
        "Have an idea to improve the bot, found a bug, or want a new feature for AUA students?\n\n"
        "✍️ <i>Please write your message below and send it. Our administrative team will review it and reply back to you!</i>\n\n"
        "<i>(Send /stop at any time to cancel)</i>",
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
    """Handle orientation selection, persist to DB, and finalize onboarding."""
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

    await state.clear()

    await callback.message.edit_text(
        "🎉 <b>You're all set! Welcome to the AUA Community!</b> 🦅🇦🇲\n\n"
        f"👤 <b>Your Profile:</b>\n"
        f"• Sex: <b>{user.sex.capitalize()}</b>\n"
        f"• Orientation: <b>{user.orientation.capitalize()}</b>\n\n"
        "⚡ <b>How to Use:</b>\n"
        "• /search — Find an anonymous partner\n"
        "• /next — Skip to the next student\n"
        "• /stop — End chat or cancel search\n"
        "• /suggestion — Send ideas to bot admins\n\n"
        "✨ <i>Remember to be kind and respectful. Have fun connecting!</i>\n"
        "Press /search to begin your first anonymous chat!",
        parse_mode="HTML",
    )
    await callback.answer("Profile saved successfully!")
