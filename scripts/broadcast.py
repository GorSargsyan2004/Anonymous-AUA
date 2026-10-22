"""Script to broadcast major updates and feature announcements to all registered users."""

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
from app.database.crud import get_all_banned_user_ids
from app.database.models import User
from app.database.session import get_db_session

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("broadcast")

BROADCAST_MESSAGE = (
    "✨ <b>NEW FEATURE UPDATE: 15s View-Once Media is Live!</b> 🔒🎭\n\n"
    "Hey AUA! We just deployed a major privacy upgrade to <b>Anonymous AUA</b>:\n\n"
    "👁️ <b>Self-Destructing View-Once Photos & Videos:</b>\n"
    "You can now share photos, videos, and media that automatically self-destruct "
    "<b>15 seconds</b> after your partner taps to reveal them!\n\n"
    "🛡️ <b>Strict Screenshot & Privacy Shield:</b>\n"
    "• <b>Screenshots Blocked:</b> Taking screenshots, forwarding, saving to gallery, and downloading are completely disabled!\n"
    "• <b>Auto-Destruction:</b> Exactly 15 seconds after opening, the media permanently disappears from both chats.\n\n"
    "⚡ <b>3 Easy Ways to Send View-Once Media:</b>\n"
    "1. <b>Telegram Spoiler:</b> Select <b>\"Hide with Spoiler\"</b> in Telegram before sending photos/videos.\n"
    "2. <b>Caption Tag:</b> Add <code>#once</code> or <code>/once</code> anywhere in your caption (e.g. <i>\"Check this out #once\"</i>).\n"
    "3. <b>Command:</b> Type /onetime to turn View-Once Mode ON for all your outgoing media!\n\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    "💡 <b>Explore All Bot Abilities & Commands:</b>\n"
    "Type /help to see everything our bot can do:\n"
    "• <b>Native Replies:</b> Swipe right or tap Reply on any message to quote it!\n"
    "• <b>Emoji Reactions:</b> Add emoji reactions (👍, ❤️, 🔥, 😂) and they mirror live to your partner.\n"
    "• <b>Live Message Edits:</b> Fix typos live; edited messages update in real time.\n"
    "• <b>Sleep Mode (/pause):</b> Put your chat into Sleep Mode overnight so it never times out.\n"
    "• <b>Recent Partners (/recent):</b> Browse past partners to rate, review, change feedback, or report anytime.\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    "Ready to meet fellow students? Type /search to find a partner! ×͜×"
)


async def get_target_users(dry_run: bool = False, test_user_id: Optional[int] = None) -> List[int]:
    """Retrieve list of user IDs to receive broadcast."""
    if test_user_id:
        return [test_user_id]

    async with get_db_session() as session:
        banned_ids = set(await get_all_banned_user_ids(session))
        stmt = select(User.id).where(User.id.notin_(banned_ids)).order_by(User.id)
        result = await session.execute(stmt)
        user_ids = list(result.scalars().all())
    return user_ids


async def run_broadcast(test_user_id: Optional[int] = None) -> None:
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
        user_ids = await get_target_users(test_user_id=test_user_id)
        total_targets = len(user_ids)
        logger.info("Starting broadcast to %d target(s)...", total_targets)

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
                    sent = True
                    break
                except TelegramBadRequest as e:
                    err_msg = str(e).lower()
                    if any(term in err_msg for term in ["chat not found", "user is deactivated", "bot was blocked"]):
                        blocked += 1
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
    test_id = None
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        test_id = int(sys.argv[1])
        logger.info("Running test broadcast to user ID: %d", test_id)
    asyncio.run(run_broadcast(test_user_id=test_id))
