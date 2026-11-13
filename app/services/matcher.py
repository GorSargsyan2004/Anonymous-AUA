"""Compatibility matrix and matchmaking logic for Anonymous AUA Bot."""

from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import ChatSession, User, UserBlock

SEARCH_MODE_FRIENDSHIP = "friendship"
SEARCH_MODE_KILLING_TIME = "killing_time"
SEARCH_MODE_ROMANCE = "romance"
SEARCH_MODE_PREFERENCES = "preferences"

VALID_SEARCH_MODES = (
    SEARCH_MODE_FRIENDSHIP,
    SEARCH_MODE_KILLING_TIME,
    SEARCH_MODE_ROMANCE,
    SEARCH_MODE_PREFERENCES,
)

ALL_SEXES = ["male", "female"]
ALL_ORIENTATIONS = ["straight", "bisexual", "lesbian", "gay"]

SEARCH_MODE_LABELS = {
    SEARCH_MODE_FRIENDSHIP: "🤝 Making friendships",
    SEARCH_MODE_KILLING_TIME: "⏳ Killing time",
    SEARCH_MODE_ROMANCE: "💘 Looking for romance",
    SEARCH_MODE_PREFERENCES: "⚙️ By set /preferences",
}


def get_search_mode_label(mode: Optional[str]) -> str:
    """Return human-readable label for a given search mode."""
    norm = (mode or SEARCH_MODE_PREFERENCES).strip().lower()
    return SEARCH_MODE_LABELS.get(norm, SEARCH_MODE_LABELS[SEARCH_MODE_PREFERENCES])


def resolve_search_mode_preferences(
    user: User,
    mode: Optional[str] = None,
) -> Tuple[str, List[str], List[str]]:
    """Resolve effective (mode, target_sexes, target_orientations) for a user's chosen search mode.

    Modes:
      - friendship: Any gender & orientation
      - killing_time: Any gender & orientation
      - romance: Romantic compatibility based on user's sex & orientation
      - preferences: User's custom saved /preferences (falling back to romantic defaults)
    """
    norm_mode = (mode or getattr(user, "search_mode", None) or SEARCH_MODE_PREFERENCES).strip().lower()
    if norm_mode not in VALID_SEARCH_MODES:
        norm_mode = SEARCH_MODE_PREFERENCES

    if norm_mode in (SEARCH_MODE_FRIENDSHIP, SEARCH_MODE_KILLING_TIME):
        return norm_mode, list(ALL_SEXES), list(ALL_ORIENTATIONS)

    if norm_mode == SEARCH_MODE_ROMANCE:
        rom_sexes, rom_orients = get_default_preferences(user.sex, user.orientation)
        return norm_mode, rom_sexes, rom_orients

    # norm_mode == "preferences"
    raw_sexes = getattr(user, "custom_target_sexes", None) or user.target_sexes or ""
    raw_orients = getattr(user, "custom_target_orientations", None) or user.target_orientations or ""
    custom_sexes = [s.strip().lower() for s in raw_sexes.split(",") if s.strip()]
    custom_orients = [o.strip().lower() for o in raw_orients.split(",") if o.strip()]

    if not custom_sexes or not custom_orients:
        def_sexes, def_orients = get_default_preferences(user.sex, user.orientation)
        if not custom_sexes:
            custom_sexes = def_sexes
        if not custom_orients:
            custom_orients = def_orients

    return norm_mode, custom_sexes, custom_orients


def normalize_orientation(sex: str, orientation: str) -> str:
    """Normalize cross-gender orientation selections to standard compatible terms."""
    sex = sex.lower().strip()
    orientation = orientation.lower().strip()

    if sex == "male" and orientation == "lesbian":
        return "gay"
    if sex == "female" and orientation == "gay":
        return "lesbian"
    return orientation


def get_compatible_criteria(sex: str, orientation: str) -> List[Tuple[str, List[str]]]:
    """Determine the compatible partner sex and orientation(s) based on user profile.

    Compatibility rules:
      - Straight male <-> Straight or Bisexual female
      - Straight female <-> Straight or Bisexual male
      - Gay male <-> Gay or Bisexual male
      - Lesbian female <-> Lesbian or Bisexual female
      - Bisexual male <-> (Straight or Bisexual female) OR (Gay or Bisexual male)
      - Bisexual female <-> (Straight or Bisexual male) OR (Lesbian or Bisexual female)
    """
    sex = sex.lower().strip()
    normalized_orient = normalize_orientation(sex, orientation)

    if normalized_orient == "straight":
        if sex == "male":
            return [("female", ["straight", "bisexual"])]
        else:
            return [("male", ["straight", "bisexual"])]

    elif normalized_orient in ("gay", "lesbian"):
        if sex == "male":
            return [("male", ["gay", "lesbian", "bisexual"])]
        else:
            return [("female", ["lesbian", "gay", "bisexual"])]

    elif normalized_orient == "bisexual":
        if sex == "male":
            return [
                ("female", ["straight", "bisexual"]),
                ("male", ["gay", "lesbian", "bisexual"]),
            ]
        else:
            return [
                ("male", ["straight", "bisexual"]),
                ("female", ["lesbian", "gay", "bisexual"]),
            ]

    target_sex = "female" if sex == "male" else "male"
    return [(target_sex, ["straight", "bisexual"])]

def get_default_preferences(
    sex: Optional[str],
    orientation: Optional[str],
) -> Tuple[List[str], List[str]]:
    """Determine default romantic partner preferences (target_sexes, target_orientations).

    Romantic defaults:
      - Straight male -> [female], [straight, bisexual]
      - Straight female -> [male], [straight, bisexual]
      - Gay male -> [male], [gay, bisexual]
      - Lesbian female -> [female], [lesbian, bisexual]
      - Bisexual male -> [female, male], [straight, bisexual, gay]
      - Bisexual female -> [male, female], [straight, bisexual, lesbian]
    """
    if not sex or not orientation:
        return ["male", "female"], ["straight", "bisexual", "lesbian", "gay"]

    sex = sex.lower().strip()
    orientation = normalize_orientation(sex, orientation)

    if sex == "male":
        if orientation == "straight":
            return ["female"], ["straight", "bisexual"]
        elif orientation == "gay":
            return ["male"], ["gay", "bisexual"]
        elif orientation == "bisexual":
            return ["female", "male"], ["straight", "bisexual", "gay"]
        else:
            return ["female"], ["straight", "bisexual"]
    elif sex == "female":
        if orientation == "straight":
            return ["male"], ["straight", "bisexual"]
        elif orientation == "lesbian":
            return ["female"], ["lesbian", "bisexual"]
        elif orientation == "bisexual":
            return ["male", "female"], ["straight", "bisexual", "lesbian"]
        else:
            return ["male"], ["straight", "bisexual"]

    return ["male", "female"], ["straight", "bisexual", "lesbian", "gay"]


async def match_user_in_queue(
    session: AsyncSession,
    user_id: int,
    allowed_candidate_ids: Optional[List[int]] = None,
    ignore_rematch_cooldown: bool = False,
) -> Tuple[Optional[User], Optional[ChatSession]]:
    """Attempt to pair the given user with a compatible searching candidate based on preferences.

    Criteria for matching User A and Candidate:
      1. Candidate's sex is in User A's configured target_sexes
      2. Candidate's orientation is in User A's configured target_orientations
      3. User A's sex is in Candidate's configured target_sexes
      4. User A's orientation is in Candidate's configured target_orientations

    Uses row-level locking (with_for_update(skip_locked=True)) and sorts by:
      1. Smallest absolute difference in priority (ABS(candidate.priority - user.priority) ASC)
      2. Highest priority (candidate.priority DESC)
      3. Longest waiting time (candidate.created_at ASC)

    Returns:
      (matched_partner, new_chat_session) if a match was successfully found and committed.
      (None, None) if no compatible candidate is available (user will be set to is_searching=True).
    """
    # Fetch and lock current user
    user_stmt = select(User).where(User.id == user_id).with_for_update()
    user_res = await session.execute(user_stmt)
    current_user = user_res.scalar_one_or_none()

    if not current_user or not current_user.sex or not current_user.orientation or getattr(current_user, "is_banned", False):
        return None, None

    # Load or initialize current user preferences
    if current_user.target_sexes and current_user.target_orientations:
        user_target_sexes = [
            s.strip().lower()
            for s in current_user.target_sexes.split(",")
            if s.strip()
        ]
        user_target_orientations = [
            o.strip().lower()
            for o in current_user.target_orientations.split(",")
            if o.strip()
        ]
    else:
        user_target_sexes, user_target_orientations = get_default_preferences(
            current_user.sex, current_user.orientation
        )
        current_user.target_sexes = ",".join(user_target_sexes)
        current_user.target_orientations = ",".join(user_target_orientations)
        await session.commit()

    # Subqueries for active blocks in either direction
    blocked_by_user_subq = (
        select(UserBlock.blocked_user_id)
        .where(
            UserBlock.user_id == current_user.id,
            or_(UserBlock.expires_at.is_(None), UserBlock.expires_at > func.now()),
        )
    )
    blocking_user_subq = (
        select(UserBlock.user_id)
        .where(
            UserBlock.blocked_user_id == current_user.id,
            or_(UserBlock.expires_at.is_(None), UserBlock.expires_at > func.now()),
        )
    )

    # Rematch cooldown subqueries (prevent win-trading and immediate rematching)
    recent_partners_subqs = []
    if not ignore_rematch_cooldown and settings.REMATCH_COOLDOWN_MINUTES > 0:
        cooldown_cutoff = datetime.now(timezone.utc) - timedelta(
            minutes=settings.REMATCH_COOLDOWN_MINUTES
        )
        recent_partners_subq1 = (
            select(ChatSession.user2_id)
            .where(
                ChatSession.user1_id == current_user.id,
                or_(
                    ChatSession.ended_at >= cooldown_cutoff,
                    ChatSession.started_at >= cooldown_cutoff,
                    ChatSession.last_activity_at >= cooldown_cutoff,
                ),
            )
        )
        recent_partners_subq2 = (
            select(ChatSession.user1_id)
            .where(
                ChatSession.user2_id == current_user.id,
                or_(
                    ChatSession.ended_at >= cooldown_cutoff,
                    ChatSession.started_at >= cooldown_cutoff,
                    ChatSession.last_activity_at >= cooldown_cutoff,
                ),
            )
        )
        recent_partners_subqs = [recent_partners_subq1, recent_partners_subq2]

    # Search for the best candidate whose preferences match mutually
    conditions = [
        User.id != current_user.id,
        User.is_searching == True,  # noqa: E712
        User.is_banned == False,  # noqa: E712
        User.current_partner_id.is_(None),
        User.id.not_in(blocked_by_user_subq),
        User.id.not_in(blocking_user_subq),
        # User A accepts Candidate
        User.sex.in_(user_target_sexes),
        User.orientation.in_(user_target_orientations),
        # Candidate accepts User A
        func.concat(",", func.coalesce(User.target_sexes, ""), ",").like(
            f"%,{current_user.sex},%"
        ),
        func.concat(",", func.coalesce(User.target_orientations, ""), ",").like(
            f"%,{current_user.orientation},%"
        ),
    ]

    for subq in recent_partners_subqs:
        conditions.append(User.id.not_in(subq))

    if allowed_candidate_ids is not None:
        conditions.append(User.id.in_(allowed_candidate_ids))

    current_mode = (getattr(current_user, "search_mode", None) or SEARCH_MODE_PREFERENCES).strip().lower()
    if current_mode in (SEARCH_MODE_FRIENDSHIP, SEARCH_MODE_KILLING_TIME):
        intent_rank = case(
            (User.search_mode == current_mode, 0),
            (User.search_mode.in_([SEARCH_MODE_FRIENDSHIP, SEARCH_MODE_KILLING_TIME]), 1),
            else_=2,
        )
    else:
        intent_rank = case(
            (User.search_mode == current_mode, 0),
            else_=1,
        )

    candidate_stmt = (
        select(User)
        .where(*conditions)
        .order_by(
            func.abs(User.priority - current_user.priority).asc(),
            intent_rank.asc(),
            User.priority.desc(),
            User.created_at.asc(),
        )
        .with_for_update(skip_locked=True)
        .limit(1)
    )

    candidate_res = await session.execute(candidate_stmt)
    candidate = candidate_res.scalar_one_or_none()

    if candidate is not None:
        # Match found! Establish connection
        current_user.is_searching = False
        current_user.current_partner_id = candidate.id

        candidate.is_searching = False
        candidate.current_partner_id = current_user.id

        chat_session = ChatSession(
            user1_id=current_user.id,
            user2_id=candidate.id,
            is_active=True,
            last_activity_at=datetime.now(timezone.utc),
            messages_count=0,
            inactivity_warned=False,
        )
        session.add(chat_session)
        await session.commit()
        await session.refresh(candidate)
        await session.refresh(chat_session)
        return candidate, chat_session
    else:
        # No candidate found. Put current user into search queue
        current_user.is_searching = True
        current_user.current_partner_id = None
        await session.commit()
        return None, None
