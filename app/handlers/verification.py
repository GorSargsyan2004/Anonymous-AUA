"""Handler for AUA student university email verification (/verify) and gender conflict resolution."""

from datetime import datetime, timedelta, timezone
import html
import logging
from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.config import settings
from app.database.crud import (
    admin_override_gender,
    can_request_verification_email,
    check_and_verify_user_code,
    get_or_create_user,
    is_email_already_verified,
    record_verification_email_sent,
    resolve_gender_mismatch,
    set_gender_appeal_status,
    set_user_verification_code,
    update_user_gender_verification,
)
from app.database.session import get_db_session
from app.keyboards.inline import (
    get_admin_gender_appeal_keyboard,
    get_gender_mismatch_keyboard,
)
from app.services.email_verification import (
    check_global_email_rate_limit,
    generate_otp_code,
    is_valid_aua_email,
    send_verification_email,
    validate_aua_email_format,
)
from app.services.gender_detector import infer_gender_from_email
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
            if getattr(user, "gender_mismatch", False):
                inferred = (user.inferred_gender or "other").capitalize()
                current = (user.sex or "other").capitalize()
                kb = get_gender_mismatch_keyboard(inferred_gender=user.inferred_gender or "other")
                await message.answer(
                    f"⚠️ <b>Action Required: Gender Verification Conflict</b>\n\n"
                    f"Your account is verified with <code>{masked_email}</code>.\n\n"
                    f"However, your university email name indicates <b>{inferred}</b>, while your profile is set to <b>{current}</b>.\n"
                    f"To prevent catfishing and identity impersonation on campus, matchmaking is paused until this is resolved.\n\n"
                    f"<b>Select an option below:</b>",
                    reply_markup=kb,
                    parse_mode="HTML",
                )
                return

            gender_status = (
                f"• <b>Gender Status:</b> Confirmed ({user.sex.capitalize()} ✅)\n"
                if getattr(user, "is_gender_verified", False) and user.sex
                else ""
            )
            await message.answer(
                f"🎓 <b>AUA Student Status: Verified</b> ✅\n\n"
                f"• <b>Email:</b> <code>{masked_email}</code>\n"
                f"{gender_status}\n"
                f"<i>When you connect to chat partners, they will see that you are a verified AUA student! 🎭</i>",
                parse_mode="HTML",
            )
            return

        # Check rate limits before starting a new verification flow
        allowed, limit_err = await can_request_verification_email(session, user_id=user_id)
        if not allowed:
            await message.answer(
                f"⏳ <b>Verification Limit Active</b>\n\n{limit_err}\n\n"
                "Please wait before requesting another verification code.",
                parse_mode="HTML",
            )
            return

    await state.set_state(VerificationState.waiting_for_email)
    await message.answer(
        "🎓 <b>AUA Student Verification</b> 🛡️\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Confirm your student status with your official American University of Armenia email address.\n\n"
        "<b>Why verify?</b>\n"
        "• 🎓 <b>Verified Badge</b>: Partners see you are an authentic, verified AUA student\n"
        "• 🛡️ <b>Anti-Catfish Shield</b>: Verified gender badge protects students from fake profiles\n"
        "• 🤝 <b>Higher Trust</b>: Students feel safer connecting and chatting with you\n"
        "• ⚖️ <b>Shielded Reputation</b>: Immunity from false 'Not from AUA' reports\n\n"
        "✍️ <i>Please enter your AUA student email address below (e.g. <code>firstname_lastname@edu.aua.am</code>):</i>\n\n"
        "<i>(Send /cancel at any time to abort)</i>",
        parse_mode="HTML",
    )


@router.message(VerificationState.waiting_for_email, F.text)
async def handle_email_input(message: Message, state: FSMContext) -> None:
    """Handle student email input, validate domain, send OTP, and transition to code input."""
    if message.text.startswith("/"):
        return

    email = message.text.strip().lower()
    user_id = message.from_user.id

    # 1. Syntax, name structure, and dummy term validation
    is_valid, format_err = validate_aua_email_format(email)
    if not is_valid:
        await message.answer(
            f"⚠️ <b>Invalid Email Address</b>\n\n{format_err}\n\n"
            "Please try entering your email again, or send /cancel to abort:",
            parse_mode="HTML",
        )
        return

    # 2. Global outbound rate limit check (protects SMTP reputation)
    global_allowed, global_err = check_global_email_rate_limit()
    if not global_allowed:
        await message.answer(
            f"⚠️ <b>High Traffic</b>\n\n{global_err}",
            parse_mode="HTML",
        )
        return

    # 3. Per-user cooldown and daily cap check
    async with get_db_session() as session:
        user_allowed, user_limit_err = await can_request_verification_email(session, user_id=user_id)
        if not user_allowed:
            await message.answer(
                f"⏳ <b>Verification Limit Active</b>\n\n{user_limit_err}",
                parse_mode="HTML",
            )
            return

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

    # Record successful email dispatch for cooldown tracking
    async with get_db_session() as session:
        await record_verification_email_sent(session, user_id=user_id)

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
    """Validate entered OTP code and grant verified student status with gender inference."""
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
            email_str = user.email or "@edu.aua.am"
            name_token, inferred = infer_gender_from_email(email_str)

            if inferred == "unknown":
                await update_user_gender_verification(
                    session=session,
                    user_id=user_id,
                    inferred_gender="unknown",
                    is_gender_verified=False,
                    gender_mismatch=False,
                )
                mismatch = False
                matched_gender = False
            elif user.sex and inferred.lower() == user.sex.lower():
                await update_user_gender_verification(
                    session=session,
                    user_id=user_id,
                    inferred_gender=inferred,
                    is_gender_verified=True,
                    gender_mismatch=False,
                )
                mismatch = False
                matched_gender = True
            else:
                # Direct mismatch between inferred gender and current user.sex
                await update_user_gender_verification(
                    session=session,
                    user_id=user_id,
                    inferred_gender=inferred,
                    is_gender_verified=False,
                    gender_mismatch=True,
                )
                mismatch = True
                matched_gender = False

    if success:
        await state.clear()
        email_str = user.email or "@edu.aua.am"

        if mismatch:
            inferred_cap = (inferred or "other").capitalize()
            current_cap = (user.sex or "other").capitalize()
            kb = get_gender_mismatch_keyboard(inferred_gender=inferred)
            await message.answer(
                f"🎓 <b>AUA Email Verified, but Gender Conflict Detected!</b> ⚠️\n\n"
                f"Your university email (<code>{email_str}</code>) matches name <b>{(name_token or '').capitalize()}</b> ({inferred_cap}), "
                f"but your profile is currently set to <b>{current_cap}</b>.\n\n"
                f"🔒 <b>Anti-Catfish Shield:</b>\n"
                f"To keep anonymous chats authentic and prevent deception, you cannot join the matchmaking queue until this is resolved.\n\n"
                f"<b>Please choose an option:</b>\n"
                f"• Tap <b>Switch Profile to {inferred_cap}</b> to resolve instantly.\n"
                f"• If your name is unisex, rare, or the system misclassified it, tap <b>Appeal to Admin</b>.",
                reply_markup=kb,
                parse_mode="HTML",
            )
        elif matched_gender:
            sex_cap = (user.sex or "").capitalize()
            await message.answer(
                f"🎉 <b>Student Verification Complete!</b> 🎓\n\n"
                f"Your account is officially verified with <b>{email_str}</b>.\n"
                f"✅ <b>Gender Verified:</b> {sex_cap} (matches university records)\n\n"
                f"✨ <b>In-Chat Trust Badge:</b>\n"
                f"When you connect to chat partners, they will see:\n"
                f"🎓 <b>Partner Status:</b> Verified AUA Student ({sex_cap} ✅)\n\n"
                f"<i>Ready to meet fellow students? Type /search to start! ×͜×</i>",
                parse_mode="HTML",
            )
        else:
            await message.answer(
                f"🎉 <b>Student Verification Complete!</b> 🎓\n\n"
                f"Your Telegram account is officially verified with <b>{email_str}</b>.\n\n"
                f"✨ <b>In-Chat Trust Badge:</b>\n"
                f"When you connect to chat partners, they will see that you are a verified AUA student (@edu.aua.am) ✅.\n\n"
                f"<i>Ready to chat? Type /search to begin! ×͜×</i>",
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


@router.callback_query(F.data.startswith("resolve_gender_switch:"))
async def handle_resolve_gender_switch(callback_query: CallbackQuery) -> None:
    """Handle student self-correction to inferred gender to resolve conflict."""
    target_gender = callback_query.data.split(":")[1].strip().lower()
    user_id = callback_query.from_user.id

    async with get_db_session() as session:
        user = await resolve_gender_mismatch(session, user_id=user_id, new_sex=target_gender)

    sex_cap = target_gender.capitalize()
    await callback_query.answer("Profile sex updated and verified! ✅")
    try:
        await callback_query.message.edit_text(
            f"✅ <b>Gender Conflict Resolved!</b> 🎓\n\n"
            f"Your profile has been updated to <b>{sex_cap}</b> and your gender status is now officially verified (✅).\n\n"
            f"Your chat queue is unlocked! Type /search to find a partner. ×͜×",
            parse_mode="HTML",
        )
    except Exception:
        pass


@router.callback_query(F.data == "gender_appeal_start")
async def handle_gender_appeal_start(callback_query: CallbackQuery, bot: Bot) -> None:
    """Handle student request for manual administrator review of gender conflict."""
    user_id = callback_query.from_user.id

    async with get_db_session() as session:
        user = await get_or_create_user(session, user_id=user_id)
        if getattr(user, "gender_appeal_pending", False):
            await callback_query.answer("Your appeal is already under review by administrators.", show_alert=True)
            return

        await set_gender_appeal_status(session, user_id=user_id, pending=True)
        user_email = user.email or "unknown"
        user_sex = user.sex or "unknown"
        inferred = user.inferred_gender or "unknown"
        username_str = f"@{user.username}" if user.username else "No username"

    await callback_query.answer("Appeal submitted to administrators.")
    try:
        await callback_query.message.edit_text(
            "⏳ <b>Appeal Under Review</b>\n\n"
            "Your appeal has been dispatched to bot administrators for verification against student records. "
            "You will receive a notification as soon as it is reviewed.",
            parse_mode="HTML",
        )
    except Exception:
        pass

    # Dispatch to administrators
    if settings.admin_ids:
        appeal_admin_msg = (
            "⚖️ <b>GENDER CONFLICT APPEAL</b>\n\n"
            f"• <b>User ID:</b> <code>{user_id}</code>\n"
            f"• <b>Username:</b> {username_str}\n"
            f"• <b>Email:</b> <code>{user_email}</code>\n"
            f"• <b>Inferred from Email Name:</b> {inferred.upper()}\n"
            f"• <b>Requested Profile Sex:</b> {user_sex.upper()}\n\n"
            "Please review student identity and approve or reject:"
        )
        for admin_id in settings.admin_ids:
            try:
                await bot.send_message(
                    chat_id=admin_id,
                    text=appeal_admin_msg,
                    reply_markup=get_admin_gender_appeal_keyboard(user_id=user_id),
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.warning("Could not dispatch gender appeal to admin %s: %s", admin_id, e)


@router.callback_query(F.data.startswith("admin_gender_appr:"))
async def handle_admin_gender_approve(callback_query: CallbackQuery, bot: Bot) -> None:
    """Handle administrator approval of student gender appeal."""
    admin_id = callback_query.from_user.id
    if admin_id not in settings.admin_ids:
        await callback_query.answer("Unauthorized.", show_alert=True)
        return

    parts = callback_query.data.split(":")
    target_user_id = int(parts[1])
    approved_gender = parts[2].lower().strip()

    async with get_db_session() as session:
        await admin_override_gender(session, user_id=target_user_id, approved_gender=approved_gender)

    await callback_query.answer(f"Approved {approved_gender.capitalize()} for user {target_user_id}")
    try:
        await callback_query.message.edit_text(
            f"✅ <b>Appeal Approved</b>\n\n"
            f"Administrator {callback_query.from_user.mention_html()} approved <b>{approved_gender.capitalize()}</b> "
            f"for user <code>{target_user_id}</code>.",
            parse_mode="HTML",
        )
    except Exception:
        pass

    # Notify student
    try:
        await bot.send_message(
            chat_id=target_user_id,
            text=(
                f"🎉 <b>Gender Appeal Approved!</b> 🎓\n\n"
                f"An administrator has reviewed and confirmed your profile gender as <b>{approved_gender.capitalize()}</b> ✅.\n\n"
                f"Your account is now fully verified and your matchmaking queue is unlocked! Type /search to start."
            ),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning("Failed to notify user %s of appeal approval: %s", target_user_id, e)


@router.callback_query(F.data.startswith("admin_gender_rej:"))
async def handle_admin_gender_reject(callback_query: CallbackQuery, bot: Bot) -> None:
    """Handle administrator rejection of student gender appeal."""
    admin_id = callback_query.from_user.id
    if admin_id not in settings.admin_ids:
        await callback_query.answer("Unauthorized.", show_alert=True)
        return

    target_user_id = int(callback_query.data.split(":")[1])

    async with get_db_session() as session:
        await set_gender_appeal_status(session, user_id=target_user_id, pending=False)

    await callback_query.answer("Appeal rejected.")
    try:
        await callback_query.message.edit_text(
            f"❌ <b>Appeal Rejected</b>\n\n"
            f"Administrator {callback_query.from_user.mention_html()} rejected the gender appeal for user <code>{target_user_id}</code>.",
            parse_mode="HTML",
        )
    except Exception:
        pass

    # Notify student
    try:
        await bot.send_message(
            chat_id=target_user_id,
            text=(
                "❌ <b>Gender Appeal Rejected</b>\n\n"
                "Administrators were unable to verify your requested gender against university records.\n\n"
                "Please run /verify to review your account or correct your profile sex to match your university name."
            ),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning("Failed to notify user %s of appeal rejection: %s", target_user_id, e)
