"""FSM States for Anonymous AUA Bot flows."""

from aiogram.fsm.state import State, StatesGroup


class OnboardingState(StatesGroup):
    """User onboarding state machine."""
    waiting_for_sex = State()
    waiting_for_orientation = State()


class PreferencesState(StatesGroup):
    """User matching preferences configuration state machine."""
    configuring = State()


class ReportState(StatesGroup):
    """Report submission state machine."""
    waiting_for_reason = State()


class AdminPenalizeState(StatesGroup):
    """Admin penalty assignment state machine."""
    waiting_for_penalty_amount = State()


class SuggestionState(StatesGroup):
    """User bot improvement suggestion state machine."""
    waiting_for_suggestion = State()


class AdminReplyState(StatesGroup):
    """Admin reply to user suggestion state machine."""
    waiting_for_reply_text = State()


class VerificationState(StatesGroup):
    """Student AUA email verification state machine."""
    waiting_for_email = State()
    waiting_for_code = State()


