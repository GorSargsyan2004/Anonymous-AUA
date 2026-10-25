"""Application entry point: initializes Bot, Dispatcher, Database, and polling."""

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeDefault

from app.config import settings
from app.database.crud import sync_active_sessions_on_startup
from app.database.session import engine, get_db_session, init_db
from app.handlers import register_handlers
from app.middlewares.banned import BannedCheckMiddleware
from app.services.backup import hourly_backup_worker
from app.services.ban_manager import sync_banned_users
from app.services.daily_reminders import daily_reminders_worker
from app.services.inactivity_monitor import inactivity_monitor_worker


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("aua_bot")


async def setup_bot_commands(bot: Bot) -> None:
    """Register public bot menu commands visible to users."""
    commands = [
        BotCommand(command="start", description="Start bot / Profile setup"),
        BotCommand(command="search", description="Search for an anonymous partner"),
        BotCommand(command="next", description="Skip to next chat partner"),
        BotCommand(command="stop", description="End current chat or search"),
        BotCommand(command="pause", description="Pause chat / Sleep mode for the night 🌙"),
        BotCommand(command="report", description="Emergency report & block partner"),
        BotCommand(command="safety", description="Privacy facts & anti-scam shield"),
        BotCommand(command="help", description="Show bot commands & help guide"),
        BotCommand(command="stats", description="View community stats & active chats"),
        BotCommand(command="blocklist", description="View & manage blocked partners"),
        BotCommand(command="recent", description="View recent partners / review / report"),
        BotCommand(command="preferences", description="Adjust preferences & typing status"),
        BotCommand(command="typing", description="Toggle 'typing...' indicator on/off"),
        BotCommand(command="onetime", description="Toggle 15s View-Once mode for media 🔒"),
        BotCommand(command="profile", description="Update sex or orientation"),
        BotCommand(command="verify", description="Verify AUA student status (@edu.aua.am) 🎓"),
        BotCommand(command="suggestion", description="Send suggestions to improve the bot"),
        BotCommand(command="cancel", description="Cancel current input / action"),
    ]
    await bot.set_my_commands(commands, scope=BotCommandScopeDefault())


async def setup_bot_profile(bot: Bot) -> None:
    """Enforce official bot name, description, and short description on startup if supported by Telegram."""
    profile_items = [
        (bot.set_my_name, "AUA Anonymous Chat"),
        (
            bot.set_my_description,
            "Welcome to the Anonymous AUA Chat Bot! 🏛💬\n\n"
            "Make friends, chat honestly, or find a campus crush at the American University of Armenia.\n\n"
            "✨ How it works:\n"
            "1. Press /start to set your gender & preferences.\n"
            "2. Use /search to match with a compatible AUA student.\n"
            "3. Chat 100% anonymously & safely.\n\n"
            "🔒 Your identity remains hidden unless revealed. Data privacy is verifiable via the project repo:\n"
            "https://github.com/GorSargsyan2004/Anonymous-AUA",
        ),
        (
            bot.set_my_short_description,
            "Anonymous chat for AUA students. Make friends, chat honestly, or find a campus crush! 🏛💬",
        ),
    ]
    for func, text_value in profile_items:
        try:
            await func(text_value)
        except Exception as e:
            logger.debug("Could not enforce bot profile property (%s): %s", func.__name__, e)



async def main() -> None:
    """Main application lifecycle runner."""
    logger.info("Starting Anonymous AUA Bot...")

    # 1. Initialize PostgreSQL tables and verify active chat sessions
    try:
        logger.info("Initializing database tables...")
        await init_db()
        logger.info("Database tables initialized successfully.")

        async with get_db_session() as session:
            active_count = await sync_active_sessions_on_startup(session)
            logger.info(
                "Active chat sessions verified: %d active session(s) preserved across restart.",
                active_count,
            )
            banned_count = await sync_banned_users(session)
            logger.info("Banned users loaded into memory: %d banned user(s).", banned_count)
    except Exception as e:
        logger.critical("Failed to initialize database: %s", e)
        sys.exit(1)

    # 2. Initialize Bot instance (support optional Local Bot API server)
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

    # 3. Initialize Dispatcher with in-memory FSM storage & anti-ban middleware
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.outer_middleware(BannedCheckMiddleware())
    dp.callback_query.outer_middleware(BannedCheckMiddleware())

    # 4. Register all route handlers
    register_handlers(dp)

    # 5. Set Telegram UI commands and enforce legitimate bot profile
    try:
        await setup_bot_commands(bot)
        logger.info("Bot commands registered successfully.")
    except Exception as e:
        logger.warning("Could not set bot commands: %s", e)

    try:
        await setup_bot_profile(bot)
    except Exception as e:
        logger.debug("Could not enforce bot profile: %s", e)

    # 6. Start background workers
    backup_task = asyncio.create_task(hourly_backup_worker(bot))
    inactivity_task = asyncio.create_task(inactivity_monitor_worker(bot))
    reminders_task = asyncio.create_task(daily_reminders_worker(bot))

    # 7. Start Polling (preserve pending updates so messages sent during restart are not lost)
    logger.info("Starting polling with admin IDs: %s", settings.ADMIN_IDS)
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
            drop_pending_updates=False,
        )
    finally:
        logger.info("Stopping background workers...")
        inactivity_task.cancel()
        backup_task.cancel()
        reminders_task.cancel()
        for task in (inactivity_task, backup_task, reminders_task):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        logger.info("Closing bot session...")
        await bot.session.close()
        logger.info("Disposing database engine...")
        await engine.dispose()
        logger.info("Anonymous AUA Bot shut down cleanly.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped by system signal.")
