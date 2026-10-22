"""Script to broadcast a setup reminder strictly once to unfinished onboarding users."""

import asyncio
import logging
import sys
from typing import List

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
    TelegramAPIError,
)
from sqlalchemy import or_, select

from app.config import settings
from app.database.crud import get_all_banned_user_ids
from app.database.models import User
from app.database.session import get_db_session

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("broadcast_unfinished")

MESSAGE_TEXT = (
    "👋 <b>Complete Your Setup & Join the AUA Omegle Community!</b> 🎭✨\n\n"
    "Hey there! We noticed you checked out the bot but haven't finished your quick profile setup yet.\n\n"
    "Our campus community is buzzing — nearly <b>200 AUA students</b> are already active and chatting on our campus <b>\"AUA Omegle\"</b>! 🇦🇲🎓\n\n"
    "⚡ <b>What you're missing out on:</b>\n"
    "• <b>100% Anonymous 1-on-1 Chatting:</b> Meet fellow students across all majors and cohorts safely.\n"
    "• <b>15s View-Once Media:</b> Send self-destructing photos & videos protected against screenshots, saving, and forwarding! 🔒\n"
    "• <b>Native Replies & Reactions:</b> Swipe right to reply, react with live emojis, and edit typos on the fly.\n"
    "• <b>Sleep Mode (/pause):</b> Keep great conversations alive overnight without timing out.\n\n"
    "🚀 <b>How to finish setup (takes only 10 seconds):</b>\n"
    "👉 Simply tap /start, select your sex and matching preferences, and press /search to find an anonymous partner!\n\n"
    "<i>Type /help anytime to see all bot abilities. See you in the chats! ×͜×</i>"
)


async def get_unfinished_users() -> List[int]:
    """Retrieve user IDs of non-banned users who haven't completed onboarding."""
    async with get_db_session() as session:
        banned_ids = set(await get_all_banned_user_ids(session))
        stmt = select(User.id).where(
            User.id.notin_(banned_ids),
            or_(User.sex.is_(None), User.orientation.is_(None)),
        ).order_by(User.id)
        result = await session.execute(stmt)
        user_ids = list(result.scalars().all())
    return user_ids


async def main() -> None:
    session = None
    if settings.TELEGRAM_BOT_API_URL:
        logger.info("Connecting Bot via Local Telegram Bot API server at: %s", settings.TELEGRAM_BOT_API_URL)
        api_server = TelegramAPIServer.from_base(settings.TELEGRAM_BOT_API_URL, is_local=True)
        session = AiohttpSession(api=api_server)
    else:
        logger.info("Using standard public Telegram Bot API server.")

    bot = Bot(
        token=settings.BOT_TOKEN,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    try:
        user_ids = await get_unfinished_users()
        total = len(user_ids)
        logger.info("Identified %d unfinished user(s) to notify.", total)

        if total == 0:
            logger.info("No unfinished users found. Exiting.")
            return

        successful = 0
        blocked = 0
        failed = 0

        for idx, user_id in enumerate(user_ids, start=1):
            sent = False
            for attempt in range(3):
                try:
                    await bot.send_message(
                        chat_id=user_id,
                        text=MESSAGE_TEXT,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                    successful += 1
                    sent = True
                    logger.info("[%d/%d] Delivered to user %d", idx, total, user_id)
                    break
                except TelegramRetryAfter as e:
                    logger.warning("Rate limit hit! Sleeping for %d seconds...", e.retry_after)
                    await asyncio.sleep(e.retry_after)
                except TelegramForbiddenError:
                    blocked += 1
                    logger.info("[%d/%d] User %d blocked the bot or deleted account.", idx, total, user_id)
                    sent = True
                    break
                except TelegramBadRequest as e:
                    err_msg = str(e).lower()
                    if any(term in err_msg for term in ["chat not found", "user is deactivated", "bot was blocked"]):
                        blocked += 1
                        logger.info("[%d/%d] User %d unavailable (%s).", idx, total, user_id, err_msg)
                    else:
                        failed += 1
                        logger.warning("[%d/%d] Bad request for user %d: %s", idx, total, user_id, e)
                    sent = True
                    break
                except TelegramAPIError as e:
                    logger.warning("[%d/%d] Telegram API error for user %d: %s", idx, total, user_id, e)
                    await asyncio.sleep(0.5)
                except Exception as e:
                    logger.error("[%d/%d] Unexpected error for user %d: %s", idx, total, user_id, e)
                    failed += 1
                    sent = True
                    break

            if not sent:
                failed += 1

            await asyncio.sleep(0.1)

        logger.info(
            "Finished sending to unfinished users. Total: %d, Delivered: %d, Blocked/Unavailable: %d, Failed: %d",
            total,
            successful,
            blocked,
            failed,
        )
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
