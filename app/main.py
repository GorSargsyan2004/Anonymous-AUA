"""Application entry point: initializes Bot, Dispatcher, Database, and polling."""

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeDefault

from app.config import settings
from app.database.session import init_db
from app.handlers import register_handlers

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
        BotCommand(command="profile", description="Update sex or orientation"),
        BotCommand(command="suggestion", description="Send suggestions to improve the bot"),
    ]
    await bot.set_my_commands(commands, scope=BotCommandScopeDefault())



async def main() -> None:
    """Main application lifecycle runner."""
    logger.info("Starting Anonymous AUA Bot...")

    # 1. Initialize PostgreSQL tables
    try:
        logger.info("Initializing database tables...")
        await init_db()
        logger.info("Database tables initialized successfully.")
    except Exception as e:
        logger.critical("Failed to initialize database: %s", e)
        sys.exit(1)

    # 2. Initialize Bot instance
    bot = Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    # 3. Initialize Dispatcher with in-memory FSM storage
    dp = Dispatcher(storage=MemoryStorage())

    # 4. Register all route handlers
    register_handlers(dp)

    # 5. Set Telegram UI commands
    try:
        await setup_bot_commands(bot)
    except Exception as e:
        logger.warning("Could not set bot commands: %s", e)

    # 6. Start Polling
    logger.info("Starting polling with admin IDs: %s", settings.ADMIN_IDS)
    try:
        # Drop pending updates to avoid processing backlog on startup
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
        )
    finally:
        logger.info("Closing bot session...")
        await bot.session.close()
        logger.info("Anonymous AUA Bot shut down cleanly.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped by system signal.")
