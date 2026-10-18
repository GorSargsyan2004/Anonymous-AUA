"""Compatibility matrix and matchmaking logic for Anonymous AUA Bot."""

from typing import List, Optional, Tuple
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import ChatSession, User


def normalize_orientation(sex: str, orientation: str) -> str:
    """Normalize cross-gender orientation selections to standard compatible terms."""
    sex = sex.lower().strip()
    orientation = orientation.lower().strip()

    if sex == "male" and orientation == "lesbian":
        return "gay"
    if sex == "female" and orientation == "gay":
        return "lesbian"
    return orientation


def get_compatible_criteria(sex: str, orientation: str) -> Tuple[str, List[str]]:
    """Determine the compatible partner sex and orientation(s) based on user profile.

    Compatibility rules:
      - male + straight <-> female + straight
      - female + straight <-> male + straight
      - male + gay <-> male + gay
      - female + lesbian <-> female + lesbian
    """
    sex = sex.lower().strip()
    normalized_orient = normalize_orientation(sex, orientation)

    if sex == "male":
        if normalized_orient == "straight":
            return "female", ["straight"]
        # Gay / male seeking male
        return "male", ["gay", "lesbian"]
    else:  # female
        if normalized_orient == "straight":
            return "male", ["straight"]
        # Lesbian / female seeking female
        return "female", ["lesbian", "gay"]


async def match_user_in_queue(
    session: AsyncSession,
    user_id: int,
) -> Tuple[Optional[User], Optional[ChatSession]]:
    """Attempt to pair the given user with a compatible searching candidate.

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

    if not current_user or not current_user.sex or not current_user.orientation:
        return None, None

    target_sex, target_orientations = get_compatible_criteria(
        current_user.sex, current_user.orientation
    )

    # Search for the best candidate
    candidate_stmt = (
        select(User)
        .where(
            User.id != current_user.id,
            User.is_searching == True,  # noqa: E712
            User.current_partner_id.is_(None),
            User.sex == target_sex,
            User.orientation.in_(target_orientations),
        )
        .order_by(
            func.abs(User.priority - current_user.priority).asc(),
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
