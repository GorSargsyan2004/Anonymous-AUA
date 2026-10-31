"""Script to broadcast messages to registered users."""

import argparse
import asyncio
import logging
import sys
from typing import List, Optional

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
from sqlalchemy import select

from app.config import settings
from app.database.crud import get_all_banned_user_ids, set_user_bot_blocked
from app.database.models import User
from app.database.session import get_db_session

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("broadcast")

BROADCAST_MESSAGE = (
    "👩 <b>Message from the Admin to girls.</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    "Dear girls, I have received feedback that most of the guys you connect with behave horny and in somewhat disgusting way. 😅\n\n"
    "Please help eliminate or at least punish such guys to ensure a better experience in the anonymous chat. Just report them; the AI Moderator will judge their behavior and penalize them, so they will appear at the bottom of the queue. ⚖️\n\n"
    "Be more strict with such behavior, and don't get disappointed, as not all guys behave that way. 🤝\n\n"
    "Best wishes,\n"
    "<b>Gor Sargsyan</b>"
)


async def get_target_users(gender: Optional[str] = "female", test_user_id: Optional[int] = None) -> List[int]:
    """Retrieve list of user IDs to receive broadcast."""
    if test_user_id:
        return [test_user_id]

    async with get_db_session() as session:
        banned_ids = set(await get_all_banned_user_ids(session))
        stmt = select(User.id).where(User.id.notin_(banned_ids))
        if gender and gender.lower() != "all":
            stmt = stmt.where(User.sex == gender.lower())
        stmt = stmt.order_by(User.id)
        result = await session.execute(stmt)
        user_ids = list(result.scalars().all())
    return user_ids


async def run_broadcast(gender: Optional[str] = "female", test_user_id: Optional[int] = None) -> None:
    """Execute the broadcast safely with rate limit handling."""
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
        user_ids = await get_target_users(gender=gender, test_user_id=test_user_id)
        total_targets = len(user_ids)
        target_desc = f"test user {test_user_id}" if test_user_id else f"{gender or 'all'} user(s)"
        logger.info("Starting broadcast to %d target(s) [%s]...", total_targets, target_desc)

        successful = 0
        blocked = 0
        failed = 0

        for idx, user_id in enumerate(user_ids, start=1):
            sent = False
            for attempt in range(3):
                try:
                    await bot.send_message(
                        chat_id=user_id,
                        text=BROADCAST_MESSAGE,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                    successful += 1
                    sent = True
                    break
                except TelegramRetryAfter as e:
                    logger.warning("Rate limit hit! Sleeping for %d seconds...", e.retry_after)
                    await asyncio.sleep(e.retry_after)
                except TelegramForbiddenError:
                    blocked += 1
                    logger.debug("User %d has blocked the bot or deleted account.", user_id)
                    try:
                        async with get_db_session() as db_session:
                            await set_user_bot_blocked(db_session, user_id=user_id, is_blocked=True)
                    except Exception:
                        pass
                    sent = True
                    break
                except TelegramBadRequest as e:
                    err_msg = str(e).lower()
                    if any(term in err_msg for term in ["chat not found", "user is deactivated", "bot was blocked"]):
                        blocked += 1
                        try:
                            async with get_db_session() as db_session:
                                await set_user_bot_blocked(db_session, user_id=user_id, is_blocked=True)
                        except Exception:
                            pass
                    else:
                        failed += 1
                        logger.warning("Bad request sending to user %d: %s", user_id, e)
                    sent = True
                    break
                except TelegramAPIError as e:
                    logger.warning("Telegram API error sending to user %d (attempt %d): %s", user_id, attempt, e)
                    await asyncio.sleep(0.5)
                except Exception as e:
                    logger.error("Unexpected error sending to user %d: %s", user_id, e)
                    failed += 1
                    sent = True
                    break

            if not sent:
                failed += 1

            # Log periodic progress every 25 users
            if idx % 25 == 0 or idx == total_targets:
                logger.info(
                    "Progress: %d/%d (%.1f%%) | Success: %d | Blocked/Left: %d | Failed: %d",
                    idx,
                    total_targets,
                    (idx / total_targets) * 100,
                    successful,
                    blocked,
                    failed,
                )

            # Small delay between messages (20 msgs/second max)
            await asyncio.sleep(0.05)

        summary_msg = (
            f"📢 <b>Broadcast Complete!</b>\n\n"
            f"• <b>Audience:</b> {target_desc}\n"
            f"• <b>Total Targets:</b> {total_targets}\n"
            f"• <b>Delivered Successfully:</b> {successful}\n"
            f"• <b>Blocked/Deleted:</b> {blocked}\n"
            f"• <b>Failed:</b> {failed}"
        )
        logger.info(summary_msg.replace("<b>", "").replace("</b>", ""))

        # Notify admins if this was a full broadcast
        if not test_user_id and settings.ADMIN_IDS:
            for admin_id in settings.ADMIN_IDS:
                try:
                    await bot.send_message(chat_id=admin_id, text=summary_msg, parse_mode="HTML")
                except Exception:
                    pass

    finally:
        await bot.session.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Broadcast message to bot users.")
    parser.add_argument("pos_test_id", nargs="?", type=int, default=None, help="Positional test user ID")
    parser.add_argument("--test", type=int, default=None, help="Test user ID")
    parser.add_argument("--gender", type=str, default="female", help="Target gender: female, male, all (default: female)")
    args = parser.parse_args()

    test_id = args.pos_test_id or args.test
    if test_id:
        logger.info("Running test broadcast to user ID: %d", test_id)
    else:
        logger.info("Target gender filter: %s", args.gender)

    asyncio.run(run_broadcast(gender=args.gender, test_user_id=test_id))
