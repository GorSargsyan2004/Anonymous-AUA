"""Inactivity & Ghost Chat Monitor Service.

Monitors active anonymous chat sessions for complete silence.
- Stage 1: If neither partner speaks within 2 minutes of connecting, pings both with action buttons.
- Stage 2: If no response/message occurs within the grace period (2 minutes), automatically closes the chat cleanly.
"""

import asyncio
from datetime import datetime, timezone
import logging
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from app.database.crud import (
    end_session_by_id,
    get_expired_paused_sessions,
    get_inactive_sessions_for_closure,
    get_inactive_sessions_for_warning,
    mark_chat_session_inactivity_warned,
)
from app.database.session import get_db_session
from app.keyboards.inline import get_feedback_keyboard, get_inactivity_warning_keyboard
from app.services.one_time_media import one_time_media_manager
from app.services.reply_manager import reply_manager

logger = logging.getLogger(__name__)

# Configurable timeouts
GHOST_CHAT_TIMEOUT_SECONDS = 120       # 2 minutes of 0 messages since connection
MID_CHAT_TIMEOUT_SECONDS = 1800        # 30 minutes of silence during active chat (relaxing for genuine conversations)
INACTIVITY_GRACE_PERIOD_SECONDS = 180  # 3 minutes to respond to warning before auto-close
MAX_PAUSE_HOURS = 24                   # 24 hours maximum duration for Sleep Mode
INACTIVITY_CHECK_INTERVAL_SECONDS = 25 # Loop sleep interval


async def check_and_process_inactivity(bot: Bot) -> None:
    """Perform one check cycle: send silence warnings and close expired sessions."""
    async with get_db_session() as session:
        # 1. Process Stage 1: Send warning to idle sessions that haven't been warned yet
        sessions_to_warn = await get_inactive_sessions_for_warning(
            session,
            ghost_timeout_seconds=GHOST_CHAT_TIMEOUT_SECONDS,
            mid_chat_timeout_seconds=MID_CHAT_TIMEOUT_SECONDS,
        )

        for chat_sess in sessions_to_warn:
            try:
                await mark_chat_session_inactivity_warned(session, chat_sess.id)
                msg_count = chat_sess.messages_count or 0
                is_ghost = (msg_count == 0)

                if is_ghost:
                    warning_text = (
                        "⏳ <b>Are you both still there?</b> 🎭\n\n"
                        "You were connected to an anonymous partner, but neither of you has sent a message yet.\n\n"
                        "If you're busy or didn't notice, please choose an option below so your partner isn't left waiting:"
                    )
                else:
                    warning_text = (
                        "⏳ <b>Chat Inactivity Notice</b> 🎭\n\n"
                        "This conversation has been silent for a while.\n\n"
                        "Tap an option below to let your partner know you're still here, or end the conversation:"
                    )

                keyboard = get_inactivity_warning_keyboard(chat_sess.id)

                for uid in (chat_sess.user1_id, chat_sess.user2_id):
                    try:
                        await bot.send_message(
                            chat_id=uid,
                            text=warning_text,
                            reply_markup=keyboard,
                            parse_mode="HTML",
                        )
                    except (TelegramForbiddenError, TelegramBadRequest) as e:
                        logger.debug("Could not send inactivity warning to user %s: %s", uid, e)
                logger.info(
                    "Sent inactivity warning for session %s (users: %s, %s, msgs: %s)",
                    chat_sess.id,
                    chat_sess.user1_id,
                    chat_sess.user2_id,
                    msg_count,
                )
            except Exception as e:
                logger.error("Error warning session %s for inactivity: %s", chat_sess.id, e)

        # 2. Process Stage 2: Automatically close sessions where grace period expired
        sessions_to_close = await get_inactive_sessions_for_closure(
            session,
            grace_period_seconds=INACTIVITY_GRACE_PERIOD_SECONDS,
        )

        for chat_sess in sessions_to_close:
            try:
                sess_id = chat_sess.id
                msg_count = chat_sess.messages_count or 0
                res = await end_session_by_id(session, sess_id)
                if not res:
                    continue
                ended_session, u1_id, u2_id = res

                await reply_manager.clear_chat(u1_id, u2_id, session=session, session_id=sess_id)
                one_time_media_manager.clear_session(sess_id)

                # If 0 messages were sent, exit neutrally with no feedback rating prompt
                if msg_count == 0:
                    closure_text = (
                        "⚠️ <b>Chat ended due to inactivity.</b> 🎭\n\n"
                        "Neither partner sent a message.\n\n"
                        "Press /search when you are ready to chat with a partner! ×͜×"
                    )
                    for uid in (u1_id, u2_id):
                        try:
                            await bot.send_message(
                                chat_id=uid,
                                text=closure_text,
                                parse_mode="HTML",
                            )
                        except Exception:
                            pass
                else:
                    # Messages were exchanged earlier, offer normal feedback prompt
                    for uid, pid in ((u1_id, u2_id), (u2_id, u1_id)):
                        try:
                            await bot.send_message(
                                chat_id=uid,
                                text=(
                                    "⚠️ <b>Chat closed due to inactivity.</b>\n\n"
                                    "How was your conversation?"
                                ),
                                reply_markup=get_feedback_keyboard(sess_id, pid),
                                parse_mode="HTML",
                            )
                        except Exception:
                            pass

                logger.info(
                    "Auto-closed inactive session %s (users: %s, %s, msgs: %s)",
                    sess_id,
                    u1_id,
                    u2_id,
                    msg_count,
                )
            except Exception as e:
                logger.error("Error auto-closing inactive session %s: %s", chat_sess.id, e)

        # 3. Process Step 3: Automatically expire paused sessions that exceeded MAX_PAUSE_HOURS (24 hours)
        expired_paused = await get_expired_paused_sessions(session, max_pause_hours=MAX_PAUSE_HOURS)
        for chat_sess in expired_paused:
            try:
                sess_id = chat_sess.id
                res = await end_session_by_id(session, sess_id)
                if not res:
                    continue
                ended_session, u1_id, u2_id = res
                await reply_manager.clear_chat(u1_id, u2_id, session=session, session_id=sess_id)
                one_time_media_manager.clear_session(sess_id)

                closure_text = (
                    "⚠️ <b>Sleep Mode expired.</b> 🎭\n\n"
                    "Neither partner resumed the chat within 24 hours. The conversation was ended.\n\n"
                    "Press /search when you are ready to chat with someone new! ×͜×"
                )
                for uid in (u1_id, u2_id):
                    try:
                        await bot.send_message(
                            chat_id=uid,
                            text=closure_text,
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass
                logger.info("Closed 24h expired paused session %s (users: %s, %s)", sess_id, u1_id, u2_id)
            except Exception as e:
                logger.error("Error expiring paused session %s: %s", chat_sess.id, e)


async def inactivity_monitor_worker(bot: Bot) -> None:
    """Continuous background worker loop for ghost and inactive chat cleanup."""
    logger.info("Inactivity monitor worker started. Checking every %ss.", INACTIVITY_CHECK_INTERVAL_SECONDS)
    try:
        while True:
            await check_and_process_inactivity(bot)
            await asyncio.sleep(INACTIVITY_CHECK_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        logger.info("Inactivity monitor worker stopped cleanly.")
    except Exception as e:
        logger.exception("Unexpected exception in inactivity_monitor_worker: %s", e)
