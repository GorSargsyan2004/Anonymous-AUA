"""AUA Hub Web Bridge handlers: /post (public indexed anonymous posts), /sync (account & inbox sync + auto-verification), and /start=getlink."""

import html
import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.database.crud import (
    create_or_regenerate_inbox,
    get_or_create_user,
    get_user_active_inbox,
)
from app.database.session import get_db_session
from app.database.web_crud import (
    consume_sync_code_and_link_bot,
    create_anonymous_public_post,
    get_web_user_by_telegram_id,
)
from app.keyboards.inline import (
    get_gender_mismatch_keyboard,
    get_inbox_owner_keyboard,
)
from app.services.ai_moderator import (
    TIER2_REJECTION_MESSAGE,
    gemini_moderator,
    validate_anonymous_post_tier1,
)
from app.services.gender_detector import infer_gender_from_email
from app.services.link_shield import link_shield
from app.states.states import WebBridgeState

logger = logging.getLogger(__name__)
router = Router(name="web_bridge_router")


async def _process_public_post(message: Message, raw_text: str, state: FSMContext) -> None:
    """Validate (Tier 1 & Tier 2 moderation) and publish an anonymous post to the AUA Hub Anonymous feed."""
    text = (raw_text or "").strip()

    # Tier 1: Deterministic Algorithmic Checks (Min length >= 12, vowel ratio, repetition, keyboard smash)
    tier1_ok, tier1_err = validate_anonymous_post_tier1(text)
    if not tier1_ok:
        await message.answer(tier1_err)
        return

    if len(text) > 1200:
        await message.answer(
            f"⚠️ <b>Post Too Long ({len(text)}/1200 chars)</b>\n\n"
            "Please keep your anonymous post under 1,200 characters:",
            parse_mode="HTML",
        )
        return

    # Check Link Shield for malicious/IP-logger links
    shield_res = await link_shield.scan_message(message)
    if shield_res.is_blocked:
        await message.answer(
            "🛡️ <b>Security Shield Blocked Post</b>\n\n"
            f"Your post contains a blocked or unsafe link ({html.escape(shield_res.block_reason or 'unsafe URL')}). "
            "Please remove unsafe links and try again.",
            parse_mode="HTML",
        )
        return

    user_id = message.from_user.id
    username = message.from_user.username

    async with get_db_session() as session:
        user = await get_or_create_user(session, user_id=user_id, username=username)
        if getattr(user, "is_banned", False):
            await state.clear()
            await message.answer(
                "🚫 <b>Access Denied</b>\n\nYou are banned from posting.",
                parse_mode="HTML",
            )
            return

    # Tier 2: AI Content Moderation via Google Gemini API ('VALID' vs 'SPAM')
    verdict = await gemini_moderator.evaluate_anonymous_post(text)
    if verdict == "SPAM":
        await state.clear()
        logger.info("Dropped anonymous post from user %s due to Tier 2 Gemini SPAM verdict", user_id)
        await message.answer(
            TIER2_REJECTION_MESSAGE,
            parse_mode="HTML",
        )
        return

    async with get_db_session() as session:
        post = await create_anonymous_public_post(
            session=session,
            telegram_user_id=user_id,
            content_text=text,
        )

    await state.clear()
    created_str = post.created_at.strftime("%b %d, %Y • %H:%M UTC")
    preview = html.escape(post.content_text)
    await message.answer(
        f"✅ <b>Anonymous Post Published to AUA Hub!</b> 🌐\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔢 <b>Post Index:</b> <code>#{post.id}</code>\n"
        f"🕒 <b>Date:</b> {created_str}\n\n"
        f"💬 <b>Message:</b>\n{preview}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔒 <i>100% Anonymous — Only your post text, index #{post.id}, and date appear on AUA Hub.</i>",
        parse_mode="HTML",
    )


@router.message(Command("post"))
async def handle_post_command(
    message: Message,
    command: CommandObject,
    state: FSMContext,
) -> None:
    """Create an indexed public anonymous post on the AUA Hub Anonymous page."""
    if command and command.args and command.args.strip():
        await _process_public_post(message, command.args.strip(), state)
        return

    await state.set_state(WebBridgeState.waiting_for_post_text)
    await message.answer(
        "📝 <b>Create an Anonymous Post on AUA Hub</b> 🌐\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Your message will be published anonymously in the <b>Anonymous → Posts</b> section of <b>AUA Hub</b>.\n\n"
        "🔒 <b>What is shown publicly:</b>\n"
        "• Post Index (e.g. <code>#42</code>)\n"
        "• Your Message Text\n"
        "• Date & Time\n"
        "<i>(Your identity and Telegram account are never shown.)</i>\n\n"
        "✍️ <b>Send your post text below</b> (or /cancel to abort):",
        parse_mode="HTML",
    )


@router.message(WebBridgeState.waiting_for_post_text, F.text)
async def handle_post_text_input(message: Message, state: FSMContext) -> None:
    """Receive the text for an anonymous public post when in waiting_for_post_text state."""
    if message.text.startswith("/"):
        return
    await _process_public_post(message, message.text, state)


async def execute_sync_flow(
    message: Message,
    code: str,
    state: FSMContext,
    bot: Bot,
) -> None:
    """Consume a 6-digit sync code, link WebUser <-> Telegram User, and auto-verify student & gender status."""
    user_id = message.from_user.id
    username = message.from_user.username

    bot_info = await bot.get_me()
    bot_username = bot_info.username or "aua_anonymous_bot"

    async with get_db_session() as session:
        ok, web_user, tg_user, feedback = await consume_sync_code_and_link_bot(
            session=session,
            code=code,
            telegram_user_id=user_id,
            telegram_username=username,
            bot_username=bot_username,
        )

    if not ok or not web_user or not tg_user:
        await message.answer(
            f"⚠️ <b>Sync Failed</b>\n\n{html.escape(feedback)}\n\n"
            "Please generate a fresh code on <b>AUA Hub → Anonymous → Inbox → Sync with the Bot</b>, "
            "or send /cancel to abort.",
            parse_mode="HTML",
        )
        return

    await state.clear()

    email_str = html.escape(web_user.email)
    name_str = html.escape(web_user.name)
    inbox_link_str = html.escape(web_user.inbox_link or "")

    await message.answer(
        f"🔗 <b>Synced with AUA Hub Successfully!</b> ✨\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>AUA Hub Account:</b> {name_str} (<code>{email_str}</code>)\n"
        f"📬 <b>Personal Inbox:</b> Connected & Synced\n"
        f"🔗 <b>Your Inbox Link:</b> <code>{inbox_link_str}</code>\n"
        f"🎓 <b>Bot Verification:</b> Verified AUA Status Active ✅\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"All anonymous messages sent to your <code>/link</code> will now appear in both this bot and your <b>AUA Hub → Anonymous → Inbox</b> tab!",
        parse_mode="HTML",
    )

    # Check if auto-verification triggered a gender mismatch that requires user action
    if getattr(tg_user, "gender_mismatch", False):
        name_token, inferred = infer_gender_from_email(web_user.email)
        inferred_cap = (inferred or "other").capitalize()
        current_cap = (tg_user.sex or "other").capitalize()
        kb = get_gender_mismatch_keyboard(inferred_gender=inferred or "other")
        await message.answer(
            f"🎓 <b>AUA Email Auto-Verified via Sync — Gender Conflict Detected!</b> ⚠️\n\n"
            f"Your university email (<code>{email_str}</code>) matches name <b>{html.escape((name_token or '').capitalize())}</b> ({inferred_cap}), "
            f"but your bot profile is currently set to <b>{current_cap}</b>.\n\n"
            f"🔒 <b>Anti-Catfish Shield:</b>\n"
            f"To keep anonymous chats authentic, please resolve this below:\n"
            f"• Tap <b>Switch Profile to {inferred_cap}</b> to resolve instantly.\n"
            f"• If your name is unisex or misclassified, tap <b>Appeal to Admin</b>.",
            reply_markup=kb,
            parse_mode="HTML",
        )


@router.message(Command("sync"))
async def handle_sync_command(
    message: Message,
    command: CommandObject,
    state: FSMContext,
    bot: Bot,
) -> None:
    """Sync Telegram bot account & inbox with AUA Hub website account."""
    if command and command.args and command.args.strip():
        await execute_sync_flow(message, command.args.strip(), state, bot)
        return

    await state.set_state(WebBridgeState.waiting_for_sync_code)
    await message.answer(
        "🔄 <b>Sync with AUA Hub</b> 🌐\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Connect your Telegram bot account & personal <code>/link</code> inbox with your <b>AUA Hub</b> profile!\n\n"
        "<b>How to sync:</b>\n"
        "1. Sign in to <b>AUA Hub</b> with your <code>@edu.aua.am</code> (or <code>@alumni.aua.am</code>) account.\n"
        "2. Open <b>Anonymous → Inbox</b> and click <b>\"Sync with the Bot\"</b>.\n"
        "3. Copy the <b>6-digit sync code</b> shown on the website and send it here!\n\n"
        "🎓 <i>Bonus: Syncing with your AUA Hub account automatically verifies your student & gender status in the bot!</i>\n\n"
        "🔢 <b>Please enter your 6-digit sync code below</b> (or /cancel to abort):",
        parse_mode="HTML",
    )


@router.message(WebBridgeState.waiting_for_sync_code, F.text)
async def handle_sync_code_input(
    message: Message,
    state: FSMContext,
    bot: Bot,
) -> None:
    """Handle 6-digit sync code input when in waiting_for_sync_code state."""
    if message.text.startswith("/"):
        return
    await execute_sync_flow(message, message.text.strip(), state, bot)


async def handle_getlink_deeplink(
    message: Message,
    state: FSMContext,
    bot: Bot,
) -> None:
    """Automatically run /link and generate an active inbox link when opened from AUA Hub Profile (?start=getlink)."""
    await state.clear()
    user_id = message.from_user.id
    username = message.from_user.username

    bot_info = await bot.get_me()
    bot_username = bot_info.username or "aua_anonymous_bot"

    async with get_db_session() as session:
        await get_or_create_user(session, user_id=user_id, username=username)
        active_inbox = await get_user_active_inbox(session, user_id=user_id)
        if not active_inbox:
            active_inbox = await create_or_regenerate_inbox(session, user_id=user_id)

        link_url = f"https://t.me/{bot_username}?start=msg_{active_inbox.token}"

        # If this Telegram user is already synced with an AUA Hub account, auto-update their inbox_link too
        web_user = await get_web_user_by_telegram_id(session, telegram_user_id=user_id)
        if web_user and not web_user.inbox_link:
            web_user.inbox_link = link_url
            await session.commit()

    kb = get_inbox_owner_keyboard(has_active_link=True, token=active_inbox.token, bot_username=bot_username)
    await message.answer(
        "🎭 <b>Your Anonymous Inbox Link is Ready!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Tap the link below to copy it, then paste it into your <b>AUA Hub → Profile → Anonymous Inbox Link</b> field and click Save:\n\n"
        f"🔗 <code>{link_url}</code>\n\n"
        "💡 <i>You can also share this link in your bio or stories to receive anonymous messages!</i>",
        reply_markup=kb,
        parse_mode="HTML",
    )
