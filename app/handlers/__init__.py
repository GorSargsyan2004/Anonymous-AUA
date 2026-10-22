"""Handlers package. Registers all sub-routers to the main Dispatcher."""

from aiogram import Dispatcher

from app.handlers.admin import router as admin_router
from app.handlers.feedback import router as feedback_router
from app.handlers.matchmaking import router as matchmaking_router
from app.handlers.start import router as start_router
from app.handlers.verification import router as verification_router


def register_handlers(dp: Dispatcher) -> None:
    """Register all routers to the dispatcher in prioritized order.

    Order is critical:
    1. admin_router (handles admin commands and report actions)
    2. start_router (handles onboarding commands and callback queries)
    3. verification_router (handles /verify email confirmation flow)
    4. feedback_router (handles ratings and report reason input)
    5. matchmaking_router (handles /search, /stop, /next, and anonymous message relay fallback)
    """
    dp.include_router(admin_router)
    dp.include_router(start_router)
    dp.include_router(verification_router)
    dp.include_router(feedback_router)
    dp.include_router(matchmaking_router)
