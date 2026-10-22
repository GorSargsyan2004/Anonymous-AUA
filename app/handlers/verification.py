"""Handler for AUA student university email verification (/verify)."""

from datetime import datetime, timedelta, timezone
import html
import logging
from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.config import settings
from app.database.crud import (
    check_and_verify_user_code,
    get_or_create_user,
    is_email_already_verified,
    set_user_verification_code,
)
from app.database.session import get_db_session
from app.services.email_verification import (
    generate_otp_code,
    is_valid_aua_email,
    send_verification_email,
)
from app.states.states import VerificationState

logger = logging.getLogger(__name__)
router = Router(name="verification_router")


@router.message(Command("verify"))
async def handle_verify_command(message: Message, state: FSMContext) -> None:
    """Handle /verify command to initiate student email verification."""
    user_id = message.from_user.id

    async with get_db_session() as session:
        user = await get_or_create_user(session, user_id=user_id)
        if user.is_verified:
            masked_email = user.email or "@edu.aua.am"
            await message.answer(
                f"🎓 <b>AUA Student Status: Verified</b> ✅\n\n"
                f"Your account is confirmed with: <code>{masked_email}</code>\n\n"
                f"<i>When you connect to chat partners, they will see that you are a verified AUA student! 🎭</i>",
                parse_mode="HTML",
            )
            return

    await state.set_state(VerificationState.waiting_for_email)
    await message.answer(
        "🎓 <b>AUA Student Verification</b> 🛡️\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Confirm your student status with your official American University of Armenia email address.\n\n"
        "<b>Why verify?</b>\n"
        "• 🎓 <b>Verified Badge</b>: Partners will see you are an authentic AUA student\n"
        "• 🤝 <b>Higher Trust</b>: Students feel safer connecting and chatting with you\n"
        "• 🛡️ <b>Shielded Reputation</b>: Immunity from false 'Not from AUA' reports\n\n"
        "✍️ <i>Please enter your AUA student email address below (must end with <b>@edu.aua.am</b>):</i>\n\n"
        "<i>(Send /cancel at any time to abort)</i>",
        parse_mode="HTML",
    )


@router.message(VerificationState.waiting_for_email, F.text)
async def handle_email_input(message: Message, state: FSMContext) -> None:
    """Handle student email input, validate domain, send OTP, and transition to code input."""
    # Ignore commands
    if message.text.startswith("/"):
        return

    email = message.text.strip().lower()
    user_id = message.from_user.id

    if not is_valid_aua_email(email):
        await message.answer(
            "⚠️ <b>Invalid Email Address</b>\n\n"
            "The email address must be an official student address ending with <b>@edu.aua.am</b>.\n\n"
            "<i>Example: <code>john_smith@edu.aua.am</code></i>\n\n"
            "Please try entering your email again, or send /cancel to abort:",
            parse_mode="HTML",
        )
        return

    async with get_db_session() as session:
        if await is_email_already_verified(session, email=email, exclude_user_id=user_id):
            await message.answer(
                "⚠️ <b>Email Already Verified</b>\n\n"
                "This email address is already associated with another verified Telegram account.\n\n"
                "If you believe this is an error or need help, please contact administrators with /suggestion.",
                parse_mode="HTML",
            )
            await state.clear()
            return

        code = generate_otp_code()
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)
        await set_user_verification_code(
            session=session,
            user_id=user_id,
            email=email,
            code=code,
            expires_at=expires_at,
        )

    # Deliver OTP email
    sent, err = await send_verification_email(to_email=email, code=code)
    if not sent:
        await message.answer(
            "⚠️ <b>Delivery Issue</b>\n\n"
            "We encountered an issue dispatching the verification email. "
            "Please verify the email is correct and try again in a few moments, or contact /suggestion.",
            parse_mode="HTML",
        )
        return

    await state.set_state(VerificationState.waiting_for_code)
    await message.answer(
        f"📧 <b>Verification Code Sent!</b>\n\n"
        f"We sent a 6-digit confirmation code to: <b>{email}</b>\n\n"
        f"Please check your Inbox (and Spam/Junk folder) and enter the <b>6-digit code</b> below to complete verification:\n\n"
        f"<i>(Code expires in 15 minutes. Send /cancel to abort)</i>",
        parse_mode="HTML",
    )


@router.message(VerificationState.waiting_for_code, F.text)
async def handle_code_input(message: Message, state: FSMContext) -> None:
    """Validate entered OTP code and grant verified student status."""
    if message.text.startswith("/"):
        return

    entered_code = message.text.strip()
    user_id = message.from_user.id

    async with get_db_session() as session:
        success, feedback_msg = await check_and_verify_user_code(
            session=session,
            user_id=user_id,
            entered_code=entered_code,
        )
        user = await get_or_create_user(session, user_id=user_id)

    if success:
        await state.clear()
        email_str = user.email or "@edu.aua.am"
        await message.answer(
            f"🎉 <b>Student Verification Complete!</b> 🎓\n\n"
            f"Your Telegram account is now officially verified with <b>{email_str}</b>.\n\n"
            f"✨ <b>What happens now:</b>\n"
            f"• When you connect to an anonymous chat, your partner will receive confirmation that you are a verified AUA student.\n"
            f"• You can search for fellow classmates anytime with /search!\n\n"
            f"<i>Welcome to the verified AUA community! ×͜×</i>",
            parse_mode="HTML",
        )
    else:
        if "expired" in feedback_msg.lower() or "too many" in feedback_msg.lower():
            await state.clear()
            await message.answer(
                f"❌ <b>Verification Session Ended:</b> {feedback_msg}\n\n"
                f"Send /verify to start a new verification attempt.",
                parse_mode="HTML",
            )
        else:
            await message.answer(
                f"⚠️ <b>{feedback_msg}</b>\n\n"
                f"Please check the 6-digit code in your email and enter it again (or send /cancel):",
                parse_mode="HTML",
            )
