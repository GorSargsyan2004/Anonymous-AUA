"""Handlers package. Registers all sub-routers to the main Dispatcher."""

from aiogram import Dispatcher

from app.handlers.admin import router as admin_router
from app.handlers.feedback import router as feedback_router
from app.handlers.inbox import router as inbox_router
from app.handlers.invites import router as invites_router
from app.handlers.matchmaking import router as matchmaking_router
from app.handlers.start import router as start_router
from app.handlers.verification import router as verification_router


def register_handlers(dp: Dispatcher) -> None:
    """Register all routers to the dispatcher in prioritized order.

    Order is critical:
    1. admin_router (handles admin commands and report actions)
    2. start_router (handles onboarding commands and callback queries)
    3. inbox_router (handles /link personal inbox and two-way replies)
    4. invites_router (handles /invite direct chat links)
    5. verification_router (handles /verify email confirmation flow)
    6. feedback_router (handles ratings and report reason input)
    7. matchmaking_router (handles /search, /stop, /next, and anonymous message relay fallback)
    """
    dp.include_router(admin_router)
    dp.include_router(start_router)
    dp.include_router(inbox_router)
    dp.include_router(invites_router)
    dp.include_router(verification_router)
    dp.include_router(feedback_router)
    dp.include_router(matchmaking_router)

