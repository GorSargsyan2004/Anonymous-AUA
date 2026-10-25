"""Daily reminder background service for unverified users and incomplete profiles.

- Unfinished setup reminders: Sent once every 24 hours to users who started the bot but
  haven't finished selecting sex/orientation.
- Unverified student reminders: Sent once every 24 hours to users who finished setup but
  haven't verified their @edu.aua.am email, explaining trust, gender verification, and anonymity.
"""

import asyncio
import logging
from typing import Dict, Optional

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

from app.database.crud import (
    get_users_for_setup_reminder,
    get_users_for_verification_reminder,
    record_setup_reminder_sent,
    record_verification_reminder_sent,
)
from app.database.session import get_db_session

logger = logging.getLogger(__name__)

# Configurable check interval and delays
REMINDERS_CHECK_INTERVAL_SECONDS = 1800  # Check every 30 minutes
REMINDERS_INITIAL_DELAY_SECONDS = 60    # 60s settling delay after bot start
INTER_MESSAGE_DELAY_SECONDS = 0.05      # 50ms rate limit protection (<= 20 msg/sec)

SETUP_REMINDER_TEXT = (
    "👋 <b>Finish setting up your profile!</b> 🎭\n\n"
    "You're just one step away from connecting with fellow AUA students anonymously!\n\n"
    "Complete your quick profile (Sex & Orientation) to start chatting and having fun.\n\n"
    "👉 Send /start to finish setup and find a partner!"
)

VERIFICATION_REMINDER_TEXT = (
    "🎓 <b>Verify your AUA Student status!</b> 🛡️\n\n"
    "Get the verified student badge and build instant trust in your chats:\n"
    "• Confirms you are an AUA student (@edu.aua.am) 🎓\n"
    "• Automatically verifies your sex for genuine conversations 🔒\n"
    "• Completely anonymous: your email is only used for verification and is never shown to partners\n\n"
    "👉 Send /verify to get verified in 1 minute!"
)


async def process_daily_reminders(bot: Bot, hours_threshold: int = 24, batch_limit: int = 500) -> Dict[str, int]:
    """Execute one pass of 24-hour reminder broadcasts for unfinished profiles and unverified students."""
    stats = {"setup_sent": 0, "verification_sent": 0, "blocked_or_failed": 0}

    async with get_db_session() as session:
        # 1. Process Unfinished Setup Reminders
        users_setup = await get_users_for_setup_reminder(
            session, hours_threshold=hours_threshold, limit=batch_limit
        )
        for user in users_setup:
            try:
                await bot.send_message(
                    chat_id=user.id,
                    text=SETUP_REMINDER_TEXT,
                    parse_mode="HTML",
                )
                stats["setup_sent"] += 1
            except TelegramForbiddenError:
                # User blocked or deactivated the bot
                logger.debug("User %s blocked bot during setup reminder broadcast", user.id)
                stats["blocked_or_failed"] += 1
            except TelegramRetryAfter as e:
                logger.warning("Rate limit hit during setup reminder. Sleeping %ss", e.retry_after)
                await asyncio.sleep(e.retry_after)
            except Exception as e:
                logger.warning("Failed to send setup reminder to user %s: %s", user.id, e)
                stats["blocked_or_failed"] += 1
            finally:
                # Mark reminder sent so we don't retry until another 24 hours pass
                await record_setup_reminder_sent(session, user.id)
                await asyncio.sleep(INTER_MESSAGE_DELAY_SECONDS)

        # 2. Process Unverified Student Email Reminders
        users_verif = await get_users_for_verification_reminder(
            session, hours_threshold=hours_threshold, limit=batch_limit
        )
        for user in users_verif:
            try:
                await bot.send_message(
                    chat_id=user.id,
                    text=VERIFICATION_REMINDER_TEXT,
                    parse_mode="HTML",
                )
                stats["verification_sent"] += 1
            except TelegramForbiddenError:
                logger.debug("User %s blocked bot during verification reminder broadcast", user.id)
                stats["blocked_or_failed"] += 1
            except TelegramRetryAfter as e:
                logger.warning("Rate limit hit during verification reminder. Sleeping %ss", e.retry_after)
                await asyncio.sleep(e.retry_after)
            except Exception as e:
                logger.warning("Failed to send verification reminder to user %s: %s", user.id, e)
                stats["blocked_or_failed"] += 1
            finally:
                await record_verification_reminder_sent(session, user.id)
                await asyncio.sleep(INTER_MESSAGE_DELAY_SECONDS)

    if stats["setup_sent"] > 0 or stats["verification_sent"] > 0:
        logger.info(
            "Daily reminders broadcast cycle completed: %s setup reminders, %s verification reminders sent (%s failed/blocked).",
            stats["setup_sent"],
            stats["verification_sent"],
            stats["blocked_or_failed"],
        )

    return stats


async def daily_reminders_worker(
    bot: Bot,
    interval_seconds: int = REMINDERS_CHECK_INTERVAL_SECONDS,
    initial_delay_seconds: int = REMINDERS_INITIAL_DELAY_SECONDS,
) -> None:
    """Background worker that periodically dispatches 24-hour setup and email verification reminders."""
    logger.info(
        "Daily reminders worker started. Initial check scheduled in %s seconds.",
        initial_delay_seconds,
    )
    try:
        if initial_delay_seconds > 0:
            await asyncio.sleep(initial_delay_seconds)
    except asyncio.CancelledError:
        logger.info("Daily reminders worker cancelled during initial settling delay.")
        return

    while True:
        try:
            await process_daily_reminders(bot)
        except asyncio.CancelledError:
            logger.info("Daily reminders worker cancelled.")
            break
        except Exception as e:
            logger.exception("Unexpected exception in daily_reminders_worker: %s", e)

        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            logger.info("Daily reminders worker sleep cancelled.")
            break
