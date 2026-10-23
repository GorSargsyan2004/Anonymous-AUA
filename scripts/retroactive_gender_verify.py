"""Script to retroactively verify student gender for all currently email-verified users."""

import asyncio
import logging
import sys
from typing import Optional

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from sqlalchemy import select

from app.config import settings
from app.database.crud import update_user_gender_verification
from app.database.models import User
from app.database.session import get_db_session
from app.keyboards.inline import get_gender_mismatch_keyboard
from app.services.gender_detector import infer_gender_from_email

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("gender_sync")


async def run_retroactive_gender_verification(notify_mismatches: bool = True) -> None:
    """Scan all verified student accounts, infer gender from email, and apply verification or mismatch flags."""
    bot = None
    if notify_mismatches:
        session = None
        if settings.TELEGRAM_BOT_API_URL:
            api_server = TelegramAPIServer.from_base(settings.TELEGRAM_BOT_API_URL, is_local=True)
            session = AiohttpSession(api=api_server)
        bot = Bot(
            token=settings.BOT_TOKEN,
            session=session,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )

    try:
        async with get_db_session() as db:
            stmt = select(User).where(User.is_verified == True, User.email.isnot(None))  # noqa: E712
            result = await db.execute(stmt)
            verified_users = list(result.scalars().all())

            total = len(verified_users)
            logger.info("Found %d verified student account(s) to process.", total)

            matched_count = 0
            mismatch_count = 0
            unknown_count = 0

            for user in verified_users:
                name_token, inferred = infer_gender_from_email(user.email or "")

                if inferred == "unknown":
                    unknown_count += 1
                    user.inferred_gender = "unknown"
                    user.is_gender_verified = False
                    user.gender_mismatch = False
                    logger.info("User %d (%s) -> Name '%s' is UNKNOWN.", user.id, user.email, name_token)

                elif user.sex and inferred.lower() == user.sex.lower():
                    matched_count += 1
                    user.inferred_gender = inferred
                    user.is_gender_verified = True
                    user.gender_mismatch = False
                    logger.info("User %d (%s) -> Matched gender: %s.", user.id, user.email, inferred)

                else:
                    mismatch_count += 1
                    user.inferred_gender = inferred
                    user.is_gender_verified = False
                    user.gender_mismatch = True
                    logger.warning(
                        "MISMATCH on user %d (%s): Email indicates '%s' (%s), but profile is '%s'.",
                        user.id,
                        user.email,
                        name_token,
                        inferred,
                        user.sex,
                    )

                    # Notify user of conflict
                    if bot and notify_mismatches:
                        try:
                            inferred_cap = (inferred or "other").capitalize()
                            current_cap = (user.sex or "other").capitalize()
                            kb = get_gender_mismatch_keyboard(inferred_gender=inferred)
                            await bot.send_message(
                                chat_id=user.id,
                                text=(
                                    f"⚠️ <b>Action Required: Gender Verification Conflict</b>\n\n"
                                    f"Your account is verified with university email <code>{user.email}</code>.\n\n"
                                    f"However, your email name (<b>{(name_token or '').capitalize()}</b>) indicates <b>{inferred_cap}</b>, "
                                    f"while your profile is currently registered as <b>{current_cap}</b>.\n\n"
                                    f"🔒 <b>Community Safeguard:</b>\n"
                                    f"To keep anonymous chats authentic and prevent deception, matchmaking is paused until this conflict is resolved.\n\n"
                                    f"<b>Please select an option:</b>\n"
                                    f"• Tap <b>Switch Profile to {inferred_cap}</b> to resolve instantly.\n"
                                    f"• If your name is unisex, rare, or misclassified, tap <b>Appeal to Admin</b>.",
                                ),
                                reply_markup=kb,
                                parse_mode="HTML",
                            )
                        except Exception as e:
                            logger.warning("Could not dispatch mismatch notice to user %d: %s", user.id, e)

            await db.commit()

            summary = (
                f"\n========================================\n"
                f"📊 RETROACTIVE GENDER VERIFICATION SUMMARY\n"
                f"========================================\n"
                f"• Total Verified Accounts Scanned: {total}\n"
                f"• Successfully Verified (Matched): {matched_count}\n"
                f"• Gender Conflicts Flagged:        {mismatch_count}\n"
                f"• Unknown / Ambiguous Names:       {unknown_count}\n"
                f"========================================"
            )
            logger.info(summary)

    finally:
        if bot:
            await bot.session.close()


if __name__ == "__main__":
    asyncio.run(run_retroactive_gender_verification(notify_mismatches=True))
