"""Reusable database CRUD queries and operations."""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import ChatSession, Feedback, Report, Suggestion, User
from app.services.matcher import get_default_preferences, normalize_orientation



def clamp_priority(value: int) -> int:
    """Clamp priority score between 0 and 100."""
    return max(0, min(100, value))


async def get_or_create_user(
    session: AsyncSession,
    user_id: int,
    username: Optional[str] = None,
) -> User:
    """Retrieve existing user or create a new user record with default priority 50."""
    stmt = select(User).where(User.id == user_id)
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()

    if user is None:
        user = User(
            id=user_id,
            username=username,
            priority=50,
            is_searching=False,
            current_partner_id=None,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
    else:
        if username and user.username != username:
            user.username = username
            await session.commit()
            await session.refresh(user)

    return user


async def get_user(session: AsyncSession, user_id: int) -> Optional[User]:
    """Retrieve a user by their Telegram ID."""
    stmt = select(User).where(User.id == user_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def update_user_profile(
    session: AsyncSession,
    user_id: int,
    sex: str,
    orientation: str,
) -> User:
    """Update user's sex and orientation after onboarding."""
    stmt = select(User).where(User.id == user_id)
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()

    if not user:
        user = User(id=user_id, priority=50)
        session.add(user)

    normalized_orient = normalize_orientation(sex, orientation)
    user.sex = sex.lower().strip()
    user.orientation = normalized_orient
    user.priority = clamp_priority(user.priority or 50)

    # Set default romantic target preferences
    def_sexes, def_orients = get_default_preferences(user.sex, user.orientation)
    user.target_sexes = ",".join(def_sexes)
    user.target_orientations = ",".join(def_orients)

    await session.commit()
    await session.refresh(user)
    return user


async def update_user_preferences(
    session: AsyncSession,
    user_id: int,
    target_sexes: List[str],
    target_orientations: List[str],
) -> Optional[User]:
    """Update user's configured matching preferences for target sexes and target orientations."""
    stmt = select(User).where(User.id == user_id).with_for_update()
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()

    if user:
        clean_sexes = list(dict.fromkeys(s.lower().strip() for s in target_sexes if s.strip()))
        clean_orients = list(dict.fromkeys(o.lower().strip() for o in target_orientations if o.strip()))
        user.target_sexes = ",".join(clean_sexes)
        user.target_orientations = ",".join(clean_orients)
        await session.commit()
        await session.refresh(user)
    return user


async def set_user_searching(
    session: AsyncSession,
    user_id: int,
    is_searching: bool,
) -> Optional[User]:
    """Toggle the searching status of a user."""
    stmt = select(User).where(User.id == user_id)
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()

    if user:
        user.is_searching = is_searching
        if not is_searching and user.current_partner_id is None:
            user.is_searching = False
        await session.commit()
        await session.refresh(user)
    return user


async def get_active_session_by_user_id(
    session: AsyncSession,
    user_id: int,
) -> Optional[ChatSession]:
    """Find the active chat session associated with the user, if one exists."""
    stmt = (
        select(ChatSession)
        .where(
            or_(ChatSession.user1_id == user_id, ChatSession.user2_id == user_id),
            ChatSession.is_active == True,  # noqa: E712
        )
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def sync_active_sessions_on_startup(session: AsyncSession) -> int:
    """Verify, synchronize, and preserve active chat sessions on bot startup.

    1. Loads all ChatSessions where is_active=True.
    2. Ensures user1 and user2 both have current_partner_id correctly pointing to
       each other, and that neither is stuck in is_searching=True.
    3. If an active session is orphaned (e.g., one user deleted), safely closes it.
    4. Cleans up any dangling User.current_partner_id that lacks an active session.

    Returns:
        int: Number of verified active chat sessions preserved across restart.
    """
    stmt = select(ChatSession).where(ChatSession.is_active == True)  # noqa: E712
    result = await session.execute(stmt)
    active_sessions = result.scalars().all()

    verified_count = 0
    active_user_ids = set()

    now_utc = datetime.now(timezone.utc)

    for cs in active_sessions:
        u1_stmt = select(User).where(User.id == cs.user1_id).with_for_update()
        u2_stmt = select(User).where(User.id == cs.user2_id).with_for_update()
        u1 = (await session.execute(u1_stmt)).scalar_one_or_none()
        u2 = (await session.execute(u2_stmt)).scalar_one_or_none()

        if u1 is None or u2 is None:
            # Orphaned session - close it cleanly
            cs.is_active = False
            cs.ended_at = now_utc
            if u1:
                u1.current_partner_id = None
                u1.is_searching = False
            if u2:
                u2.current_partner_id = None
                u2.is_searching = False
        else:
            # Re-affirm bidirectional partnership and search flags
            u1.current_partner_id = u2.id
            u1.is_searching = False
            u2.current_partner_id = u1.id
            u2.is_searching = False
            active_user_ids.add(u1.id)
            active_user_ids.add(u2.id)
            verified_count += 1

    # Clean up any users that have current_partner_id set but are not in active sessions
    if active_user_ids:
        dangling_stmt = (
            select(User)
            .where(
                User.current_partner_id.is_not(None),
                User.id.not_in(list(active_user_ids)),
            )
            .with_for_update()
        )
    else:
        dangling_stmt = (
            select(User)
            .where(User.current_partner_id.is_not(None))
            .with_for_update()
        )

    dangling_users = (await session.execute(dangling_stmt)).scalars().all()
    for du in dangling_users:
        du.current_partner_id = None
        du.is_searching = False

    await session.commit()
    return verified_count


async def end_active_session(
    session: AsyncSession,
    user_id: int,
) -> Optional[Tuple[ChatSession, int]]:
    """End any active chat session involving user_id, clearing partner IDs and is_searching.

    Returns:
      (ended_chat_session, partner_user_id) if an active session was found and closed.
      None if no active session existed.
    """
    stmt = (
        select(ChatSession)
        .where(
            or_(ChatSession.user1_id == user_id, ChatSession.user2_id == user_id),
            ChatSession.is_active == True,  # noqa: E712
        )
        .with_for_update()
    )
    res = await session.execute(stmt)
    chat_session = res.scalar_one_or_none()

    if not chat_session:
        # Also clean up searching state just in case
        user_stmt = select(User).where(User.id == user_id)
        user_res = await session.execute(user_stmt)
        u = user_res.scalar_one_or_none()
        if u and (u.current_partner_id or u.is_searching):
            u.current_partner_id = None
            u.is_searching = False
            await session.commit()
        return None

    partner_id = (
        chat_session.user2_id
        if chat_session.user1_id == user_id
        else chat_session.user1_id
    )

    # Mark session inactive
    chat_session.is_active = False
    chat_session.ended_at = datetime.now(timezone.utc)

    # Clear current partner from both user records and increment completed sessions count
    users_stmt = select(User).where(User.id.in_([user_id, partner_id])).with_for_update()
    users_res = await session.execute(users_stmt)
    for u in users_res.scalars().all():
        u.current_partner_id = None
        u.is_searching = False
        u.completed_sessions_count = (u.completed_sessions_count or 0) + 1

    await session.commit()
    await session.refresh(chat_session)
    return chat_session, partner_id



async def update_user_priority(
    session: AsyncSession,
    user_id: int,
    delta: int,
) -> int:
    """Adjust a user's priority by delta, clamping the result to [0, 100]."""
    stmt = select(User).where(User.id == user_id).with_for_update()
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()

    if not user:
        return 50

    new_priority = clamp_priority(user.priority + delta)
    user.priority = new_priority
    await session.commit()
    return new_priority


async def apply_admin_penalty(
    session: AsyncSession,
    user_id: int,
    penalty: int,
) -> int:
    """Subtract penalty points from a user's priority, clamped at minimum 0."""
    stmt = select(User).where(User.id == user_id).with_for_update()
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()

    if not user:
        return 0

    new_priority = clamp_priority(user.priority - penalty)
    user.priority = new_priority
    await session.commit()
    return new_priority


async def record_feedback(
    session: AsyncSession,
    session_id: int,
    from_user_id: int,
    to_user_id: int,
    feedback_type: str,
) -> Tuple[Optional[Feedback], bool]:
    """Record session feedback if not already recorded.

    Returns:
      (feedback, created: bool)
    """
    check_stmt = select(Feedback).where(
        Feedback.session_id == session_id,
        Feedback.from_user_id == from_user_id,
    )
    check_res = await session.execute(check_stmt)
    existing = check_res.scalar_one_or_none()

    if existing:
        return existing, False

    feedback = Feedback(
        session_id=session_id,
        from_user_id=from_user_id,
        to_user_id=to_user_id,
        feedback_type=feedback_type,
    )
    session.add(feedback)
    await session.commit()
    await session.refresh(feedback)
    return feedback, True


async def create_report(
    session: AsyncSession,
    session_id: int,
    reporter_id: int,
    reported_id: int,
    reason_text: str,
) -> Report:
    """Create a new report filed against a user."""
    report = Report(
        session_id=session_id,
        reporter_id=reporter_id,
        reported_id=reported_id,
        reason_text=reason_text,
        is_resolved=False,
        penalty_applied=None,
    )
    session.add(report)
    await session.commit()
    await session.refresh(report)
    return report


async def get_report_by_id(
    session: AsyncSession,
    report_id: int,
) -> Optional[Report]:
    """Retrieve a report by its ID."""
    stmt = select(Report).where(Report.id == report_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def resolve_report(
    session: AsyncSession,
    report_id: int,
    penalty_applied: int,
) -> Optional[Report]:
    """Mark a report resolved with an applied penalty."""
    stmt = select(Report).where(Report.id == report_id).with_for_update()
    result = await session.execute(stmt)
    report = result.scalar_one_or_none()

    if report:
        report.is_resolved = True
        report.penalty_applied = penalty_applied
        await session.commit()
        await session.refresh(report)
    return report


async def dismiss_report(
    session: AsyncSession,
    report_id: int,
) -> Optional[Report]:
    """Dismiss a report with 0 penalty applied."""
    return await resolve_report(session, report_id, penalty_applied=0)


async def should_prompt_bot_feedback(session: AsyncSession, user_id: int) -> bool:
    """Determine if user should receive a prompt asking for bot improvement suggestions.

    Conditions:
      1. User has completed at least 2 sessions and count is an even number (2, 4, 6, ...).
      2. User has NOT already submitted a suggestion today (UTC date).
    """
    stmt = select(User).where(User.id == user_id)
    res = await session.execute(stmt)
    user = res.scalar_one_or_none()
    if not user:
        return False

    if user.completed_sessions_count <= 0 or user.completed_sessions_count % 2 != 0:
        return False

    if user.last_suggestion_date is not None:
        now_utc = datetime.now(timezone.utc)
        if user.last_suggestion_date.date() == now_utc.date():
            return False

    return True


async def create_suggestion(
    session: AsyncSession,
    user_id: int,
    text: str,
) -> Suggestion:
    """Save user suggestion for improving the bot and record last_suggestion_date."""
    now_utc = datetime.now(timezone.utc)
    suggestion = Suggestion(
        user_id=user_id,
        suggestion_text=text,
        is_replied=False,
        created_at=now_utc,
    )
    session.add(suggestion)

    # Update user's last_suggestion_date so prompt is suppressed for today
    user_stmt = select(User).where(User.id == user_id).with_for_update()
    user_res = await session.execute(user_stmt)
    user = user_res.scalar_one_or_none()
    if user:
        user.last_suggestion_date = now_utc

    await session.commit()
    await session.refresh(suggestion)
    return suggestion


async def get_suggestion_by_id(
    session: AsyncSession,
    suggestion_id: int,
) -> Optional[Suggestion]:
    """Retrieve a bot suggestion by ID."""
    stmt = select(Suggestion).where(Suggestion.id == suggestion_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def reply_to_suggestion(
    session: AsyncSession,
    suggestion_id: int,
    reply_text: str,
) -> Optional[Suggestion]:
    """Record administrator reply to a student's suggestion."""
    stmt = select(Suggestion).where(Suggestion.id == suggestion_id).with_for_update()
    result = await session.execute(stmt)
    suggestion = result.scalar_one_or_none()

    if suggestion:
        suggestion.admin_reply = reply_text
        suggestion.is_replied = True
        suggestion.replied_at = datetime.now(timezone.utc)
        await session.commit()
        await session.refresh(suggestion)
    return suggestion


async def get_admin_stats(session: AsyncSession) -> Dict[str, Any]:
    """Aggregate statistics for the admin /stata dashboard."""
    # Total Users
    total_users = (await session.execute(select(func.count(User.id)))).scalar_one() or 0

    # Sex Breakdown
    male_count = (
        await session.execute(select(func.count(User.id)).where(User.sex == "male"))
    ).scalar_one() or 0
    female_count = (
        await session.execute(select(func.count(User.id)).where(User.sex == "female"))
    ).scalar_one() or 0
    unset_sex_count = total_users - (male_count + female_count)

    # Orientation Breakdown
    straight_count = (
        await session.execute(select(func.count(User.id)).where(User.orientation == "straight"))
    ).scalar_one() or 0
    bisexual_count = (
        await session.execute(select(func.count(User.id)).where(User.orientation == "bisexual"))
    ).scalar_one() or 0
    lesbian_count = (
        await session.execute(select(func.count(User.id)).where(User.orientation == "lesbian"))
    ).scalar_one() or 0
    gay_count = (
        await session.execute(select(func.count(User.id)).where(User.orientation == "gay"))
    ).scalar_one() or 0

    # Feedback Breakdown
    likes_count = (
        await session.execute(select(func.count(Feedback.id)).where(Feedback.feedback_type == "like"))
    ).scalar_one() or 0
    dislikes_count = (
        await session.execute(select(func.count(Feedback.id)).where(Feedback.feedback_type == "dislike"))
    ).scalar_one() or 0
    spam_count = (
        await session.execute(select(func.count(Feedback.id)).where(Feedback.feedback_type == "spam"))
    ).scalar_one() or 0
    not_aua_count = (
        await session.execute(select(func.count(Feedback.id)).where(Feedback.feedback_type == "not_aua"))
    ).scalar_one() or 0

    # Reports Breakdown
    total_reports = (await session.execute(select(func.count(Report.id)))).scalar_one() or 0
    resolved_reports = (
        await session.execute(select(func.count(Report.id)).where(Report.is_resolved == True))  # noqa: E712
    ).scalar_one() or 0
    pending_reports = total_reports - resolved_reports

    # Suggestions Breakdown
    total_suggestions = (await session.execute(select(func.count(Suggestion.id)))).scalar_one() or 0
    replied_suggestions = (
        await session.execute(select(func.count(Suggestion.id)).where(Suggestion.is_replied == True))  # noqa: E712
    ).scalar_one() or 0
    pending_suggestions = total_suggestions - replied_suggestions

    # Active Stats
    active_chats = (
        await session.execute(select(func.count(ChatSession.id)).where(ChatSession.is_active == True))  # noqa: E712
    ).scalar_one() or 0
    searching_users = (
        await session.execute(select(func.count(User.id)).where(User.is_searching == True))  # noqa: E712
    ).scalar_one() or 0

    return {
        "total_users": total_users,
        "male_count": male_count,
        "female_count": female_count,
        "unset_sex_count": unset_sex_count,
        "straight_count": straight_count,
        "bisexual_count": bisexual_count,
        "lesbian_count": lesbian_count,
        "gay_count": gay_count,
        "likes_count": likes_count,
        "dislikes_count": dislikes_count,
        "spam_count": spam_count,
        "not_aua_count": not_aua_count,
        "total_reports": total_reports,
        "resolved_reports": resolved_reports,
        "pending_reports": pending_reports,
        "total_suggestions": total_suggestions,
        "replied_suggestions": replied_suggestions,
        "pending_suggestions": pending_suggestions,
        "active_chats": active_chats,
        "searching_users": searching_users,
    }

