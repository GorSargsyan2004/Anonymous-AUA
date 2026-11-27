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


class InboxState(StatesGroup):
    """FSM states for anonymous inbox messaging and two-way replies."""
    waiting_for_message = State()
    waiting_for_reply = State()


class WebBridgeState(StatesGroup):
    """FSM states for AUA Hub website integration (/post and /sync)."""
    waiting_for_post_text = State()
    waiting_for_sync_code = State()


class GroupChatCreateState(StatesGroup):
    """Multi-step FSM state machine for creating an anonymous group chat."""
    waiting_for_name = State()
    waiting_for_description = State()
    waiting_for_max_participants = State()
    configuring_limitations = State()
    waiting_for_creator_nickname = State()


class GroupChatJoinState(StatesGroup):
    """FSM state for picking a nickname before entering an anonymous group chat."""
    waiting_for_nickname = State()


class GroupChatReportState(StatesGroup):
    """FSM states for reporting a participant inside an anonymous group chat."""
    selecting_target = State()
    waiting_for_reason = State()





