"""Reusable database CRUD queries and operations."""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from sqlalchemy import and_, delete, desc, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

import secrets

from app.config import settings
from app.database.models import (
    AnonymousInbox,
    ChatInvite,
    ChatMessageMap,
    ChatSession,
    Feedback,
    InboxBlock,
    InboxMessage,
    Report,
    Suggestion,
    User,
    UserBlock,
)



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
        changed = False
        if username and user.username != username:
            user.username = username
            changed = True
        if getattr(user, "is_bot_blocked", False):
            user.is_bot_blocked = False
            user.bot_blocked_at = None
            changed = True
        if changed:
            await session.commit()
            await session.refresh(user)

    return user


async def set_user_bot_blocked(
    session: AsyncSession,
    user_id: int,
    is_blocked: bool = True,
) -> None:
    """Update user's bot blocked status when they stop/block or unblock the bot."""
    stmt = select(User).where(User.id == user_id)
    user = (await session.execute(stmt)).scalar_one_or_none()
    if user:
        user.is_bot_blocked = is_blocked
        user.bot_blocked_at = datetime.now(timezone.utc) if is_blocked else None
        await session.commit()


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

    from app.services.matcher import get_default_preferences, normalize_orientation

    normalized_orient = normalize_orientation(sex, orientation)
    new_sex = sex.lower().strip()
    if user.sex != new_sex or user.sex_updated_at is None:
        user.sex_updated_at = datetime.now(timezone.utc)
    user.sex = new_sex
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
    target_sexes: Optional[List[str]] = None,
    target_orientations: Optional[List[str]] = None,
    show_typing: Optional[bool] = None,
) -> Optional[User]:
    """Update user's configured matching preferences and typing status settings."""
    stmt = select(User).where(User.id == user_id).with_for_update()
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()

    if user:
        if target_sexes is not None:
            clean_sexes = list(dict.fromkeys(s.lower().strip() for s in target_sexes if s.strip()))
            user.target_sexes = ",".join(clean_sexes)
        if target_orientations is not None:
            clean_orients = list(dict.fromkeys(o.lower().strip() for o in target_orientations if o.strip()))
            user.target_orientations = ",".join(clean_orients)
        if show_typing is not None:
            user.show_typing = show_typing
        await session.commit()
        await session.refresh(user)
    return user


async def set_user_typing_preference(
    session: AsyncSession,
    user_id: int,
    show_typing: bool,
) -> Optional[User]:
    """Toggle or update user's typing indicator visibility preference."""
    stmt = select(User).where(User.id == user_id).with_for_update()
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()
    if user:
        user.show_typing = show_typing
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


async def get_chat_session(
    session: AsyncSession,
    session_id: int,
) -> Optional[ChatSession]:
    """Retrieve chat session by ID."""
    stmt = select(ChatSession).where(ChatSession.id == session_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


def get_session_duration_seconds(chat_session: Optional[ChatSession]) -> float:
    """Calculate the duration of a chat session in seconds."""
    if not chat_session or not chat_session.started_at:
        return 0.0
    end_time = chat_session.ended_at or datetime.now(timezone.utc)
    start_time = chat_session.started_at
    if start_time.tzinfo is None:
        start_time = start_time.replace(tzinfo=timezone.utc)
    if end_time.tzinfo is None:
        end_time = end_time.replace(tzinfo=timezone.utc)
    return max(0.0, (end_time - start_time).total_seconds())


async def check_is_serial_downvoter(
    session: AsyncSession,
    user_id: int,
    current_session_id: Optional[int] = None,
) -> bool:
    """Check if a user is exhibiting serial downvoting behavior.
    
    Returns True if:
    1. The user's last 2 prior feedbacks were both negative (making this vote their 3rd+ consecutive downvote).
    2. OR out of their last 4 prior feedbacks, 3 or more (>=75%) were negative.
    """
    stmt = select(Feedback.feedback_type).where(
        Feedback.from_user_id == user_id,
    )
    if current_session_id is not None:
        stmt = stmt.where(Feedback.session_id != current_session_id)

    stmt = stmt.order_by(Feedback.created_at.desc(), Feedback.id.desc()).limit(4)
    result = await session.execute(stmt)
    recent_types = list(result.scalars().all())

    if not recent_types:
        return False

    negative_types = {"dislike", "spam", "not_aua"}

    # Check 1: Last 2 are consecutive negative (this next negative would be >= 3 consecutive)
    if len(recent_types) >= 2 and all(t in negative_types for t in recent_types[:2]):
        return True

    # Check 2: >= 75% negative in recent window (e.g. 3 of 4)
    if len(recent_types) >= 4:
        neg_count = sum(1 for t in recent_types if t in negative_types)
        if neg_count / len(recent_types) >= 0.75:
            return True

    return False


def _calculate_feedback_delta(
    chat_sess: Optional[ChatSession],
    partner: User,
    feedback_type: str,
    is_serial_downvoter: bool = False,
) -> Tuple[int, bool]:
    """Calculate the priority delta and consensus status for a feedback type.
    
    Protections:
    - Serial downvoter neutralization: negative deltas forced to 0, spam flags suppressed.
    - Engagement threshold: unengaged chats (<15s duration or <4 messages) force dislike
      and base spam deltas to 0.
    """
    duration_sec = get_session_duration_seconds(chat_sess)
    messages_count = getattr(chat_sess, "messages_count", 0) or 0
    is_short_chat = duration_sec < 15.0
    is_unengaged = is_short_chat or (messages_count < 4)
    delta = 0
    consensus_reached = False

    if feedback_type == "like":
        delta = 1
    elif feedback_type == "dislike":
        if is_serial_downvoter or is_unengaged:
            delta = 0
        else:
            delta = -1
    elif feedback_type in ("spam", "not_aua"):
        if is_serial_downvoter:
            delta = 0
        elif is_unengaged:
            delta = 0
            partner.spam_flags_count = (partner.spam_flags_count or 0) + 1
            if partner.spam_flags_count >= 2:
                consensus_reached = True
                delta = -4
        else:
            base_delta = -1
            partner.spam_flags_count = (partner.spam_flags_count or 0) + 1
            if partner.spam_flags_count >= 2:
                consensus_reached = True
                delta = base_delta - 4
            else:
                delta = base_delta

    return delta, consensus_reached


def _revert_feedback_delta(
    chat_sess: Optional[ChatSession],
    partner: User,
    feedback_type: str,
    priority_delta_applied: Optional[int] = None,
) -> int:
    """Calculate the priority delta required to undo/revert a previously applied feedback."""
    duration_sec = get_session_duration_seconds(chat_sess)
    messages_count = getattr(chat_sess, "messages_count", 0) or 0
    is_short_chat = duration_sec < 15.0
    is_unengaged = is_short_chat or (messages_count < 4)

    if priority_delta_applied is not None:
        delta = -priority_delta_applied
        if feedback_type in ("spam", "not_aua"):
            if priority_delta_applied != 0:
                partner.spam_flags_count = max(0, (partner.spam_flags_count or 0) - 1)
            elif is_unengaged and (partner.spam_flags_count or 0) > 0:
                partner.spam_flags_count = max(0, (partner.spam_flags_count or 0) - 1)
        return delta

    # Fallback if priority_delta_applied was not recorded (legacy records)
    delta = 0
    if feedback_type == "like":
        delta = -1
    elif feedback_type == "dislike":
        delta = 0 if is_unengaged else 1
    elif feedback_type in ("spam", "not_aua"):
        base_revert = 0 if is_unengaged else 1
        prev_count = partner.spam_flags_count or 0
        partner.spam_flags_count = max(0, prev_count - 1)
        if prev_count >= 2:
            delta = base_revert + 4
        else:
            delta = base_revert

    return delta


async def get_feedback_by_session_and_user(
    session: AsyncSession,
    session_id: int,
    from_user_id: int,
) -> Optional[Feedback]:
    """Retrieve existing feedback submitted by a user for a session."""
    stmt = select(Feedback).where(
        Feedback.session_id == session_id,
        Feedback.from_user_id == from_user_id,
    )
    res = await session.execute(stmt)
    return res.scalar_one_or_none()


async def update_or_override_feedback(
    session: AsyncSession,
    session_id: int,
    from_user_id: int,
    partner_id: int,
    new_feedback_type: str,
) -> Dict[str, Any]:
    """Create or override feedback for a session, cleanly adjusting partner priority and flags."""
    check_stmt = (
        select(Feedback)
        .where(
            Feedback.session_id == session_id,
            Feedback.from_user_id == from_user_id,
        )
        .with_for_update()
    )
    res = await session.execute(check_stmt)
    existing = res.scalar_one_or_none()

    chat_sess = await get_chat_session(session, session_id)
    duration_sec = get_session_duration_seconds(chat_sess)
    messages_count = getattr(chat_sess, "messages_count", 0) or 0
    is_short_chat = duration_sec < 15.0
    is_unengaged = is_short_chat or (messages_count < 4)

    is_serial_downvoter = await check_is_serial_downvoter(
        session, from_user_id, current_session_id=session_id
    )

    partner_stmt = select(User).where(User.id == partner_id).with_for_update()
    partner = (await session.execute(partner_stmt)).scalar_one_or_none()

    if not partner:
        return {
            "created": False,
            "updated": False,
            "delta": 0,
            "is_short_chat": is_short_chat,
            "is_unengaged": is_unengaged,
            "is_serial_downvoter": is_serial_downvoter,
            "consensus_reached": False,
            "spam_flags_count": 0,
            "new_priority": 50,
            "partner": None,
            "previous_type": None,
            "already_set": False,
        }

    previous_type = existing.feedback_type if existing else None

    # If same feedback type already submitted, no changes needed
    if existing and existing.feedback_type == new_feedback_type:
        return {
            "created": False,
            "updated": False,
            "delta": 0,
            "is_short_chat": is_short_chat,
            "is_unengaged": is_unengaged,
            "is_serial_downvoter": is_serial_downvoter,
            "consensus_reached": False,
            "spam_flags_count": partner.spam_flags_count,
            "new_priority": partner.priority,
            "partner": partner,
            "previous_type": previous_type,
            "already_set": True,
        }

    # 1. If overriding existing, revert the previous feedback first
    if existing:
        revert_delta = _revert_feedback_delta(
            chat_sess,
            partner,
            existing.feedback_type,
            priority_delta_applied=getattr(existing, "priority_delta_applied", None),
        )
        partner.priority = clamp_priority(partner.priority + revert_delta)

    # 2. Apply new feedback delta
    apply_delta, consensus_reached = _calculate_feedback_delta(
        chat_sess,
        partner,
        new_feedback_type,
        is_serial_downvoter=is_serial_downvoter,
    )
    partner.priority = clamp_priority(partner.priority + apply_delta)

    # 3. Update or create Feedback row
    if existing:
        existing.feedback_type = new_feedback_type
        existing.priority_delta_applied = apply_delta
        existing.created_at = datetime.now(timezone.utc)
        is_created = False
        is_updated = True
    else:
        feedback = Feedback(
            session_id=session_id,
            from_user_id=from_user_id,
            to_user_id=partner_id,
            feedback_type=new_feedback_type,
            priority_delta_applied=apply_delta,
        )
        session.add(feedback)
        is_created = True
        is_updated = False

    await session.commit()
    await session.refresh(partner)

    return {
        "created": is_created,
        "updated": is_updated,
        "delta": apply_delta,
        "is_short_chat": is_short_chat,
        "is_unengaged": is_unengaged,
        "is_serial_downvoter": is_serial_downvoter,
        "consensus_reached": consensus_reached,
        "spam_flags_count": partner.spam_flags_count,
        "new_priority": partner.priority,
        "partner": partner,
        "previous_type": previous_type,
        "already_set": False,
    }


async def delete_or_reset_feedback(
    session: AsyncSession,
    session_id: int,
    from_user_id: int,
) -> bool:
    """Delete feedback for a session and revert its priority/spam impact on the partner."""
    check_stmt = (
        select(Feedback)
        .where(
            Feedback.session_id == session_id,
            Feedback.from_user_id == from_user_id,
        )
        .with_for_update()
    )
    res = await session.execute(check_stmt)
    feedback = res.scalar_one_or_none()
    if not feedback:
        return False

    partner_id = feedback.to_user_id
    feedback_type = feedback.feedback_type
    priority_delta_applied = getattr(feedback, "priority_delta_applied", None)

    chat_sess = await get_chat_session(session, session_id)
    partner_stmt = select(User).where(User.id == partner_id).with_for_update()
    partner_res = await session.execute(partner_stmt)
    partner = partner_res.scalar_one_or_none()

    if partner:
        revert_delta = _revert_feedback_delta(
            chat_sess,
            partner,
            feedback_type,
            priority_delta_applied=priority_delta_applied,
        )
        partner.priority = clamp_priority(partner.priority + revert_delta)

    await session.delete(feedback)
    await session.commit()
    return True


async def process_feedback_with_consensus(
    session: AsyncSession,
    session_id: int,
    from_user_id: int,
    partner_id: int,
    feedback_type: str,
) -> Dict[str, Any]:
    """Compatibility wrapper around update_or_override_feedback."""
    return await update_or_override_feedback(
        session=session,
        session_id=session_id,
        from_user_id=from_user_id,
        partner_id=partner_id,
        new_feedback_type=feedback_type,
    )


async def get_recent_sessions_for_user(
    session: AsyncSession,
    user_id: int,
    limit: int = 5,
) -> List[ChatSession]:
    """Retrieve the most recent completed chat sessions for a user."""
    stmt = (
        select(ChatSession)
        .where(
            or_(ChatSession.user1_id == user_id, ChatSession.user2_id == user_id),
            ChatSession.is_active == False,  # noqa: E712
        )
        .order_by(ChatSession.ended_at.desc().nullslast(), ChatSession.id.desc())
        .limit(limit)
    )
    res = await session.execute(stmt)
    return list(res.scalars().all())


async def create_report(
    session: AsyncSession,
    session_id: int,
    reporter_id: int,
    reported_id: int,
    reason_text: str,
    evidence_file_id: Optional[str] = None,
) -> Report:
    """Create a new report filed against a user."""
    report = Report(
        session_id=session_id,
        reporter_id=reporter_id,
        reported_id=reported_id,
        reason_text=reason_text,
        evidence_file_id=evidence_file_id,
        is_resolved=False,
        penalty_applied=None,
    )
    session.add(report)
    await session.commit()
    await session.refresh(report)
    return report


async def get_latest_session_for_user(
    session: AsyncSession,
    user_id: int,
) -> Optional[ChatSession]:
    """Retrieve the most recent chat session for a user (active or completed)."""
    stmt = (
        select(ChatSession)
        .where(or_(ChatSession.user1_id == user_id, ChatSession.user2_id == user_id))
        .order_by(desc(ChatSession.id))
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


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

    # User Reach & Ban Stats
    banned_count = (
        await session.execute(select(func.count(User.id)).where(User.is_banned == True))  # noqa: E712
    ).scalar_one() or 0
    active_chat_users = (
        await session.execute(select(func.count(User.id)).where(User.is_bot_blocked == False))  # noqa: E712
    ).scalar_one() or 0
    blocked_bot_users = (
        await session.execute(select(func.count(User.id)).where(User.is_bot_blocked == True))  # noqa: E712
    ).scalar_one() or 0

    # Verification Breakdown
    from app.services.email_verification import get_current_academic_year
    current_acad_year = get_current_academic_year()

    total_verified = (
        await session.execute(select(func.count(User.id)).where(User.is_verified == True))  # noqa: E712
    ).scalar_one() or 0
    verified_alumni = (
        await session.execute(
            select(func.count(User.id)).where(User.is_verified == True, User.is_alumni == True)  # noqa: E712
        )
    ).scalar_one() or 0
    verified_students = (
        await session.execute(
            select(func.count(User.id)).where(
                User.is_verified == True,  # noqa: E712
                User.is_alumni == False,  # noqa: E712
                User.academic_year_verified >= current_acad_year,
            )
        )
    ).scalar_one() or 0
    verified_expired = (
        await session.execute(
            select(func.count(User.id)).where(
                User.is_verified == True,  # noqa: E712
                User.is_alumni == False,  # noqa: E712
                or_(User.academic_year_verified.is_(None), User.academic_year_verified < current_acad_year),
            )
        )
    ).scalar_one() or 0
    gender_verified = (
        await session.execute(
            select(func.count(User.id)).where(User.is_verified == True, User.is_gender_verified == True)  # noqa: E712
        )
    ).scalar_one() or 0
    unverified_count = total_users - total_verified
    gender_mismatch_count = (
        await session.execute(select(func.count(User.id)).where(User.gender_mismatch == True))  # noqa: E712
    ).scalar_one() or 0
    gender_appeal_count = (
        await session.execute(select(func.count(User.id)).where(User.gender_appeal_pending == True))  # noqa: E712
    ).scalar_one() or 0

    # Active Stats
    active_chats = (
        await session.execute(select(func.count(ChatSession.id)).where(ChatSession.is_active == True))  # noqa: E712
    ).scalar_one() or 0
    searching_users = (
        await session.execute(select(func.count(User.id)).where(User.is_searching == True))  # noqa: E712
    ).scalar_one() or 0

    return {
        "total_users": total_users,
        "banned_count": banned_count,
        "active_chat_users": active_chat_users,
        "blocked_bot_users": blocked_bot_users,
        "total_verified": total_verified,
        "verified_students": verified_students,
        "verified_alumni": verified_alumni,
        "verified_expired": verified_expired,
        "gender_verified": gender_verified,
        "unverified_count": unverified_count,
        "gender_mismatch_count": gender_mismatch_count,
        "gender_appeal_count": gender_appeal_count,
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


async def save_chat_message_mapping(
    session: AsyncSession,
    session_id: int,
    sender_id: int,
    sender_msg_id: int,
    receiver_id: int,
    receiver_msg_id: int,
) -> None:
    """Persist bidirectional message ID mapping in PostgreSQL for active session."""
    mapping = ChatMessageMap(
        session_id=session_id,
        sender_id=sender_id,
        sender_msg_id=sender_msg_id,
        receiver_id=receiver_id,
        receiver_msg_id=receiver_msg_id,
    )
    session.add(mapping)
    await session.commit()


async def get_mapped_target_message_id_from_db(
    session: AsyncSession,
    sender_id: int,
    partner_id: int,
    replied_msg_id: int,
) -> Optional[int]:
    """Resolve target message ID from PostgreSQL when in-memory cache misses (e.g. after restart).

    Case 1: sender_id received this message from partner_id (replying to partner).
    Case 2: sender_id sent this message earlier (self-quote / replying to own message).
    """
    # Case 1: sender_id received replied_msg_id from partner_id
    stmt1 = (
        select(ChatMessageMap.sender_msg_id)
        .where(
            ChatMessageMap.receiver_id == sender_id,
            ChatMessageMap.receiver_msg_id == replied_msg_id,
            ChatMessageMap.sender_id == partner_id,
        )
        .order_by(ChatMessageMap.id.desc())
        .limit(1)
    )
    res1 = await session.execute(stmt1)
    target1 = res1.scalar_one_or_none()
    if isinstance(target1, int):
        return target1

    # Case 2: sender_id was the sender of replied_msg_id (self-quote)
    stmt2 = (
        select(ChatMessageMap.receiver_msg_id)
        .where(
            ChatMessageMap.sender_id == sender_id,
            ChatMessageMap.sender_msg_id == replied_msg_id,
            ChatMessageMap.receiver_id == partner_id,
        )
        .order_by(ChatMessageMap.id.desc())
        .limit(1)
    )
    res2 = await session.execute(stmt2)
    target2 = res2.scalar_one_or_none()
    if isinstance(target2, int):
        return target2

    return None


async def delete_chat_message_mappings(
    session: AsyncSession,
    session_id: int,
) -> None:
    """Delete all message mappings for terminated chat session."""
    stmt = delete(ChatMessageMap).where(ChatMessageMap.session_id == session_id)
    await session.execute(stmt)
    await session.commit()


async def ban_user_by_admin(
    session: AsyncSession,
    user_id: int,
) -> Tuple[bool, Optional[int]]:
    """Ban a user by admin. Terminates any active chat session and cancels queue search.

    Returns:
        (success: bool, partner_id: Optional[int])
    """
    stmt = select(User).where(User.id == user_id).with_for_update()
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()

    if user is None:
        user = User(id=user_id, is_banned=True, is_searching=False)
        session.add(user)
        await session.commit()
        return True, None

    user.is_banned = True
    user.is_searching = False
    partner_id = user.current_partner_id

    if partner_id is not None:
        await end_active_session(session, user_id=user_id)

    await session.commit()
    return True, partner_id


async def unban_user_by_admin(
    session: AsyncSession,
    user_id: int,
) -> bool:
    """Unban a previously banned user."""
    stmt = select(User).where(User.id == user_id).with_for_update()
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()

    if user:
        user.is_banned = False
        await session.commit()
        return True
    return False


async def get_all_banned_user_ids(session: AsyncSession) -> List[int]:
    """Retrieve all user IDs that are currently marked as banned."""
    stmt = select(User.id).where(User.is_banned == True)  # noqa: E712
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def add_or_update_user_block(
    session: AsyncSession,
    user_id: int,
    blocked_user_id: int,
    expires_at: Optional[datetime],
) -> UserBlock:
    """Add or update a user-to-partner block/cooldown."""
    stmt = select(UserBlock).where(
        UserBlock.user_id == user_id,
        UserBlock.blocked_user_id == blocked_user_id,
    ).with_for_update()
    result = await session.execute(stmt)
    block = result.scalar_one_or_none()

    now = datetime.now(timezone.utc)
    if block:
        block.created_at = now
        block.expires_at = expires_at
    else:
        block = UserBlock(
            user_id=user_id,
            blocked_user_id=blocked_user_id,
            created_at=now,
            expires_at=expires_at,
        )
        session.add(block)

    await session.commit()
    await session.refresh(block)
    return block


async def get_user_blocks(
    session: AsyncSession,
    user_id: int,
    page: int = 1,
    page_size: int = 5,
) -> Tuple[List[UserBlock], int]:
    """Retrieve active blocked partners for a user with pagination.

    Returns:
        (blocks: List[UserBlock], total_count: int)
    """
    now = datetime.now(timezone.utc)
    base_filter = [
        UserBlock.user_id == user_id,
        or_(UserBlock.expires_at.is_(None), UserBlock.expires_at > now),
    ]

    count_stmt = select(func.count(UserBlock.id)).where(*base_filter)
    total_count = (await session.execute(count_stmt)).scalar_one() or 0

    offset = max(0, (page - 1) * page_size)
    list_stmt = (
        select(UserBlock)
        .where(*base_filter)
        .order_by(UserBlock.created_at.desc(), UserBlock.id.desc())
        .offset(offset)
        .limit(page_size)
    )
    res = await session.execute(list_stmt)
    blocks = list(res.scalars().all())
    return blocks, total_count


async def delete_user_block(
    session: AsyncSession,
    user_id: int,
    block_id: int,
) -> bool:
    """Delete a user block by block ID for the owning user."""
    stmt = delete(UserBlock).where(
        UserBlock.id == block_id,
        UserBlock.user_id == user_id,
    )
    result = await session.execute(stmt)
    await session.commit()
    return bool(result.rowcount > 0)


async def get_public_community_stats(session: AsyncSession) -> Dict[str, Any]:
    """Aggregate public community metrics for /stats and /stata."""
    total_users = (
        await session.execute(
            select(func.count(User.id)).where(User.is_banned == False)  # noqa: E712
        )
    ).scalar_one() or 0

    active_chats = (
        await session.execute(
            select(func.count(ChatSession.id)).where(ChatSession.is_active == True)  # noqa: E712
        )
    ).scalar_one() or 0

    male_count = (
        await session.execute(
            select(func.count(User.id)).where(User.sex == "male", User.is_banned == False)  # noqa: E712
        )
    ).scalar_one() or 0

    female_count = (
        await session.execute(
            select(func.count(User.id)).where(User.sex == "female", User.is_banned == False)  # noqa: E712
        )
    ).scalar_one() or 0

    straight_count = (
        await session.execute(
            select(func.count(User.id)).where(User.orientation == "straight", User.is_banned == False)  # noqa: E712
        )
    ).scalar_one() or 0

    bisexual_count = (
        await session.execute(
            select(func.count(User.id)).where(User.orientation == "bisexual", User.is_banned == False)  # noqa: E712
        )
    ).scalar_one() or 0

    lesbian_count = (
        await session.execute(
            select(func.count(User.id)).where(User.orientation == "lesbian", User.is_banned == False)  # noqa: E712
        )
    ).scalar_one() or 0

    gay_count = (
        await session.execute(
            select(func.count(User.id)).where(User.orientation == "gay", User.is_banned == False)  # noqa: E712
        )
    ).scalar_one() or 0

    return {
        "total_users": total_users,
        "active_chats": active_chats,
        "male_count": male_count,
        "female_count": female_count,
        "straight_count": straight_count,
        "bisexual_count": bisexual_count,
        "lesbian_count": lesbian_count,
        "gay_count": gay_count,
    }


async def touch_chat_session_activity(
    session: AsyncSession,
    session_id: int,
) -> None:
    """Update last_activity_at timestamp, increment messages_count, and reset inactivity_warned flag."""
    stmt = (
        update(ChatSession)
        .where(ChatSession.id == session_id, ChatSession.is_active == True)  # noqa: E712
        .values(
            last_activity_at=datetime.now(timezone.utc),
            messages_count=ChatSession.messages_count + 1,
            inactivity_warned=False,
            inactivity_warned_at=None,
        )
    )
    await session.execute(stmt)
    await session.commit()


async def reset_chat_session_inactivity_warning(
    session: AsyncSession,
    session_id: int,
) -> None:
    """Reset inactivity warning flag and refresh last_activity_at when user clicks 'I'm Here'."""
    stmt = (
        update(ChatSession)
        .where(ChatSession.id == session_id, ChatSession.is_active == True)  # noqa: E712
        .values(
            last_activity_at=datetime.now(timezone.utc),
            inactivity_warned=False,
            inactivity_warned_at=None,
        )
    )
    await session.execute(stmt)
    await session.commit()


async def mark_chat_session_inactivity_warned(
    session: AsyncSession,
    session_id: int,
) -> None:
    """Mark an active session as warned for inactivity and record the timestamp."""
    stmt = (
        update(ChatSession)
        .where(ChatSession.id == session_id, ChatSession.is_active == True)  # noqa: E712
        .values(
            inactivity_warned=True,
            inactivity_warned_at=datetime.now(timezone.utc),
        )
    )
    await session.execute(stmt)
    await session.commit()


async def pause_chat_session(
    session: AsyncSession,
    session_id: int,
) -> bool:
    """Put an active chat session into sleep mode (pause). Freezes inactivity warnings."""
    stmt = (
        update(ChatSession)
        .where(ChatSession.id == session_id, ChatSession.is_active == True)  # noqa: E712
        .values(
            is_paused=True,
            paused_at=datetime.now(timezone.utc),
            inactivity_warned=False,
            inactivity_warned_at=None,
        )
    )
    res = await session.execute(stmt)
    await session.commit()
    rowcount = getattr(res, "rowcount", None)
    return bool(rowcount > 0) if isinstance(rowcount, int) else True


async def unpause_chat_session(
    session: AsyncSession,
    session_id: int,
) -> bool:
    """Awaken a paused chat session. Resets activity timestamp and normal monitoring."""
    stmt = (
        update(ChatSession)
        .where(ChatSession.id == session_id, ChatSession.is_active == True)  # noqa: E712
        .values(
            is_paused=False,
            paused_at=None,
            last_activity_at=datetime.now(timezone.utc),
            inactivity_warned=False,
            inactivity_warned_at=None,
        )
    )
    res = await session.execute(stmt)
    await session.commit()
    rowcount = getattr(res, "rowcount", None)
    return bool(rowcount > 0) if isinstance(rowcount, int) else True


async def get_inactive_sessions_for_warning(
    session: AsyncSession,
    ghost_timeout_seconds: int = 120,
    mid_chat_timeout_seconds: int = 1800,
) -> List[ChatSession]:
    """Fetch active sessions that have exceeded the inactivity threshold and haven't been warned yet.

    - Excludes sessions in Sleep Mode (is_paused == True).
    - Ghost connections: messages_count == 0 and time since started_at >= ghost_timeout_seconds (default 2 min)
    - Mid-chat silence: messages_count > 0 and time since last_activity_at >= mid_chat_timeout_seconds (default 30 min)
    """
    now_utc = datetime.now(timezone.utc)
    ghost_cutoff = now_utc - timedelta(seconds=ghost_timeout_seconds)
    mid_chat_cutoff = now_utc - timedelta(seconds=mid_chat_timeout_seconds)

    stmt = (
        select(ChatSession)
        .where(
            ChatSession.is_active == True,  # noqa: E712
            ChatSession.is_paused == False,  # noqa: E712
            ChatSession.inactivity_warned == False,  # noqa: E712
            or_(
                and_(
                    ChatSession.messages_count == 0,
                    ChatSession.started_at <= ghost_cutoff,
                ),
                and_(
                    ChatSession.messages_count > 0,
                    ChatSession.last_activity_at <= mid_chat_cutoff,
                ),
            ),
        )
    )
    res = await session.execute(stmt)
    return list(res.scalars().all())


async def get_inactive_sessions_for_closure(
    session: AsyncSession,
    grace_period_seconds: int = 180,
) -> List[ChatSession]:
    """Fetch active sessions that were warned for inactivity and have exceeded the grace period without response.
    Excludes paused sessions.
    """
    now_utc = datetime.now(timezone.utc)
    closure_cutoff = now_utc - timedelta(seconds=grace_period_seconds)

    stmt = (
        select(ChatSession)
        .where(
            ChatSession.is_active == True,  # noqa: E712
            ChatSession.is_paused == False,  # noqa: E712
            ChatSession.inactivity_warned == True,  # noqa: E712
            ChatSession.inactivity_warned_at.is_not(None),
            ChatSession.inactivity_warned_at <= closure_cutoff,
        )
    )
    res = await session.execute(stmt)
    return list(res.scalars().all())


async def get_expired_paused_sessions(
    session: AsyncSession,
    max_pause_hours: int = 24,
) -> List[ChatSession]:
    """Fetch active sessions in sleep mode that have exceeded max_pause_hours (default 24h) without activity."""
    now_utc = datetime.now(timezone.utc)
    cutoff = now_utc - timedelta(hours=max_pause_hours)

    stmt = (
        select(ChatSession)
        .where(
            ChatSession.is_active == True,  # noqa: E712
            ChatSession.is_paused == True,  # noqa: E712
            ChatSession.paused_at.is_not(None),
            ChatSession.paused_at <= cutoff,
        )
    )
    res = await session.execute(stmt)
    return list(res.scalars().all())


async def end_session_by_id(
    session: AsyncSession,
    session_id: int,
) -> Optional[Tuple[ChatSession, int, int]]:
    """End an active chat session by its ID, clearing both users' current_partner_id.

    Returns:
        (ended_chat_session, user1_id, user2_id) if session was active and closed.
        None if not found or already inactive.
    """
    stmt = (
        select(ChatSession)
        .where(ChatSession.id == session_id, ChatSession.is_active == True)  # noqa: E712
        .with_for_update()
    )
    res = await session.execute(stmt)
    chat_session = res.scalar_one_or_none()
    if not chat_session:
        return None

    u1_id = chat_session.user1_id
    u2_id = chat_session.user2_id

    chat_session.is_active = False
    chat_session.ended_at = datetime.now(timezone.utc)

    users_stmt = select(User).where(User.id.in_([u1_id, u2_id])).with_for_update()
    users_res = await session.execute(users_stmt)
    for u in users_res.scalars().all():
        u.current_partner_id = None
        u.is_searching = False
        if (chat_session.messages_count or 0) > 0:
            u.completed_sessions_count = (u.completed_sessions_count or 0) + 1

    await session.commit()
    await session.refresh(chat_session)
    return chat_session, u1_id, u2_id


async def set_user_verification_code(
    session: AsyncSession,
    user_id: int,
    email: str,
    code: str,
    expires_at: datetime,
) -> User:
    """Store pending verification code and target email for a user."""
    stmt = select(User).where(User.id == user_id).with_for_update()
    user = (await session.execute(stmt)).scalar_one_or_none()
    if not user:
        user = User(id=user_id)
        session.add(user)

    user.email = email.strip().lower()
    user.verification_code = str(code).strip()
    user.verification_code_expires_at = expires_at
    user.verification_attempts = 0
    await session.commit()
    await session.refresh(user)
    return user


async def check_and_verify_user_code(
    session: AsyncSession,
    user_id: int,
    entered_code: str,
) -> Tuple[bool, str]:
    """Verify an entered OTP code against user's stored code.
    
    Returns:
        (success: bool, message: str)
    """
    stmt = select(User).where(User.id == user_id).with_for_update()
    user = (await session.execute(stmt)).scalar_one_or_none()
    if not user:
        return False, "User not found."

    if not user.verification_code or not user.verification_code_expires_at:
        if user.is_verified:
            return True, "Already verified."
        return False, "No pending verification code found. Please run /verify to request a new code."

    now = datetime.now(timezone.utc)
    exp = user.verification_code_expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)

    if now > exp:
        user.verification_code = None
        user.verification_code_expires_at = None
        await session.commit()
        return False, "Verification code has expired. Please run /verify again."

    user.verification_attempts = (user.verification_attempts or 0) + 1
    if user.verification_attempts > 5:
        user.verification_code = None
        user.verification_code_expires_at = None
        user.verification_attempts = 0
        await session.commit()
        return False, "Too many incorrect attempts. Please run /verify to request a new code."

    if user.verification_code.strip() != entered_code.strip():
        rem = max(0, 5 - user.verification_attempts)
        await session.commit()
        return False, f"Incorrect verification code. ({rem} attempts remaining)"

    # Success!
    user.is_verified = True
    user_email = (user.email or "").strip().lower()
    user.is_alumni = user_email.endswith("@alumni.aua.am")
    user.verified_at = now
    from app.services.email_verification import get_current_academic_year
    user.academic_year_verified = get_current_academic_year(now)
    user.verification_code = None
    user.verification_code_expires_at = None
    user.verification_attempts = 0
    await session.commit()
    await session.refresh(user)
    return True, "Verification successful!"


async def is_email_already_verified(
    session: AsyncSession,
    email: str,
    exclude_user_id: int,
) -> bool:
    """Check if an email address is already verified by another user."""
    stmt = select(User.id).where(
        User.email == email.strip().lower(),
        User.is_verified == True,  # noqa: E712
        User.id != exclude_user_id,
    ).limit(1)
    res = await session.execute(stmt)
    return res.scalar_one_or_none() is not None


async def update_user_gender_verification(
    session: AsyncSession,
    user_id: int,
    inferred_gender: Optional[str],
    is_gender_verified: bool,
    gender_mismatch: bool,
    gender_appeal_pending: bool = False,
) -> Optional[User]:
    """Update gender verification and mismatch states for a user."""
    user = await get_or_create_user(session, user_id=user_id)
    user.inferred_gender = inferred_gender
    user.is_gender_verified = is_gender_verified
    user.gender_mismatch = gender_mismatch
    user.gender_appeal_pending = gender_appeal_pending
    await session.commit()
    await session.refresh(user)
    return user


async def resolve_gender_mismatch(
    session: AsyncSession,
    user_id: int,
    new_sex: str,
) -> Optional[User]:
    """Correct user profile sex to match verified email name, resolving conflict and bypassing cooldown."""
    user = await get_or_create_user(session, user_id=user_id)
    user.sex = new_sex.lower().strip()
    user.is_gender_verified = True
    user.gender_mismatch = False
    user.gender_appeal_pending = False
    user.sex_updated_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(user)
    return user


async def set_gender_appeal_status(
    session: AsyncSession,
    user_id: int,
    pending: bool,
) -> Optional[User]:
    """Update gender conflict appeal status for a user."""
    user = await get_or_create_user(session, user_id=user_id)
    user.gender_appeal_pending = pending
    await session.commit()
    await session.refresh(user)
    return user


async def admin_override_gender(
    session: AsyncSession,
    user_id: int,
    approved_gender: str,
) -> Optional[User]:
    """Administrator manual approval/override of student gender."""
    from app.services.matcher import get_default_preferences

    user = await get_or_create_user(session, user_id=user_id)
    user.sex = approved_gender.lower().strip()
    user.inferred_gender = approved_gender.lower().strip()
    user.is_gender_verified = True
    user.gender_mismatch = False
    user.gender_appeal_pending = False
    user.sex_updated_at = None  # Clear change cooldown penalty
    def_sexes, def_orients = get_default_preferences(user.sex, user.orientation or "straight")
    user.target_sexes = ",".join(def_sexes)
    user.target_orientations = ",".join(def_orients)
    await session.commit()
    await session.refresh(user)
    return user


async def can_request_verification_email(
    session: AsyncSession,
    user_id: int,
) -> Tuple[bool, Optional[str]]:
    """Check if a user is allowed to request a verification email based on cooldown and daily limit."""
    user = await get_or_create_user(session, user_id=user_id)
    now = datetime.now(timezone.utc)

    # 1. Reset daily count if 24 hours have elapsed
    if user.verification_emails_reset_at:
        reset_at = user.verification_emails_reset_at
        if reset_at.tzinfo is None:
            reset_at = reset_at.replace(tzinfo=timezone.utc)
        if (now - reset_at).total_seconds() >= 86400:
            user.verification_emails_sent_today = 0
            user.verification_emails_reset_at = now
            await session.commit()
    else:
        user.verification_emails_reset_at = now
        user.verification_emails_sent_today = 0
        await session.commit()

    # 2. Daily limit check
    daily_limit = getattr(settings, "VERIFICATION_DAILY_LIMIT", 5)
    if (user.verification_emails_sent_today or 0) >= daily_limit:
        return (
            False,
            f"You have reached the maximum daily limit ({daily_limit}) of verification emails. "
            "Please try again tomorrow.",
        )

    # 3. Cooldown check
    cooldown_mins = getattr(settings, "VERIFICATION_EMAIL_COOLDOWN_MINUTES", 3)
    if user.verification_email_sent_at:
        sent_at = user.verification_email_sent_at
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=timezone.utc)
        elapsed = (now - sent_at).total_seconds()
        required = cooldown_mins * 60
        if elapsed < required:
            remaining = int(required - elapsed)
            mins = remaining // 60
            secs = remaining % 60
            time_str = f"{mins}m {secs}s" if mins > 0 else f"{secs}s"
            return (
                False,
                f"Please wait {time_str} before requesting another verification code.",
            )

    return True, None


async def record_verification_email_sent(
    session: AsyncSession,
    user_id: int,
) -> None:
    """Record that a verification email was successfully dispatched to a user."""
    user = await get_or_create_user(session, user_id=user_id)
    now = datetime.now(timezone.utc)
    user.verification_email_sent_at = now
    user.verification_emails_sent_today = (user.verification_emails_sent_today or 0) + 1
    if not user.verification_emails_reset_at:
        user.verification_emails_reset_at = now
    await session.commit()


async def get_users_for_setup_reminder(
    session: AsyncSession,
    hours_threshold: int = 24,
    limit: int = 50,
) -> List[User]:
    """Retrieve users who have not completed profile setup and are due for a 24-hour reminder.
    
    Criteria:
    - User is not banned.
    - Profile setup is incomplete (sex or orientation is NULL).
    - Account was created at least `hours_threshold` hours ago (to avoid immediate ping).
    - Either never reminded or last reminder was sent at least `hours_threshold` hours ago.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_threshold)
    stmt = (
        select(User)
        .where(
            User.is_banned.is_(False),
            or_(User.sex.is_(None), User.orientation.is_(None)),
            User.created_at <= cutoff,
            or_(
                User.last_setup_reminder_at.is_(None),
                User.last_setup_reminder_at <= cutoff,
            ),
        )
        .order_by(User.last_setup_reminder_at.nulls_first(), User.created_at.asc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_users_for_verification_reminder(
    session: AsyncSession,
    hours_threshold: int = 24,
    limit: int = 50,
) -> List[User]:
    """Retrieve users who finished profile setup but are not verified, due for a 24-hour reminder.
    
    Criteria:
    - User is not banned.
    - Profile setup is complete (sex and orientation are set).
    - User is not verified (is_verified = False).
    - Account was created at least `hours_threshold` hours ago.
    - Either never reminded or last reminder was sent at least `hours_threshold` hours ago.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_threshold)
    stmt = (
        select(User)
        .where(
            User.is_banned.is_(False),
            User.sex.is_not(None),
            User.orientation.is_not(None),
            User.is_verified.is_(False),
            User.created_at <= cutoff,
            or_(
                User.last_verification_reminder_at.is_(None),
                User.last_verification_reminder_at <= cutoff,
            ),
        )
        .order_by(User.last_verification_reminder_at.nulls_first(), User.created_at.asc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def record_setup_reminder_sent(
    session: AsyncSession,
    user_id: int,
) -> None:
    """Record that a setup completion reminder was sent to a user."""
    stmt = select(User).where(User.id == user_id)
    res = await session.execute(stmt)
    user = res.scalar_one_or_none()
    if user:
        user.last_setup_reminder_at = datetime.now(timezone.utc)
        await session.commit()


async def record_verification_reminder_sent(
    session: AsyncSession,
    user_id: int,
) -> None:
    """Record that an email verification reminder was sent to a user."""
    stmt = select(User).where(User.id == user_id)
    res = await session.execute(stmt)
    user = res.scalar_one_or_none()
    if user:
        user.last_verification_reminder_at = datetime.now(timezone.utc)
        await session.commit()


async def get_users_for_annual_reverification(
    session: AsyncSession,
    current_academic_year: int,
    limit: int = 500,
) -> List[User]:
    """Retrieve verified students whose verification belongs to a past academic year and have not been prompted yet this academic year.
    
    Alumni accounts do not expire and are excluded.
    """
    stmt = (
        select(User)
        .where(
            User.is_banned.is_(False),
            User.is_verified.is_(True),
            User.is_alumni.is_(False),
            or_(
                User.academic_year_verified.is_(None),
                User.academic_year_verified < current_academic_year,
            ),
            or_(
                User.last_annual_reverification_prompt_year.is_(None),
                User.last_annual_reverification_prompt_year < current_academic_year,
            ),
        )
        .order_by(User.academic_year_verified.nulls_first(), User.created_at.asc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def record_annual_reverification_prompt_sent(
    session: AsyncSession,
    user_id: int,
    year: int,
) -> None:
    """Record that the annual September 1st re-verification prompt was sent for academic year `year`."""
    stmt = select(User).where(User.id == user_id)
    res = await session.execute(stmt)
    user = res.scalar_one_or_none()
    if user:
        user.last_annual_reverification_prompt_year = year
        await session.commit()


async def get_pending_reports(session: AsyncSession, limit: int = 50) -> List[Report]:
    """Retrieve unresolved user reports ordered chronologically."""
    stmt = (
        select(Report)
        .where(Report.is_resolved.is_(False))
        .order_by(Report.created_at.asc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


# ==============================================================================
# Anonymous Message Inbox (/link) CRUD Operations
# ==============================================================================

async def get_user_active_inbox(session: AsyncSession, user_id: int) -> Optional[AnonymousInbox]:
    """Retrieve the currently active anonymous inbox link for a user."""
    stmt = (
        select(AnonymousInbox)
        .where(AnonymousInbox.user_id == user_id, AnonymousInbox.is_active.is_(True))
        .order_by(AnonymousInbox.created_at.desc())
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def create_or_regenerate_inbox(session: AsyncSession, user_id: int) -> AnonymousInbox:
    """Deactivate existing active links and generate a fresh unique inbox token."""
    # Deactivate current active inboxes
    await session.execute(
        update(AnonymousInbox)
        .where(AnonymousInbox.user_id == user_id, AnonymousInbox.is_active.is_(True))
        .values(is_active=False, deactivated_at=func.now())
    )

    # Generate unique 8-character token
    while True:
        token = secrets.token_hex(4)
        existing = await session.execute(
            select(AnonymousInbox.id).where(AnonymousInbox.token == token)
        )
        if existing.scalar_one_or_none() is None:
            break

    inbox = AnonymousInbox(user_id=user_id, token=token, is_active=True)
    session.add(inbox)
    await session.commit()
    await session.refresh(inbox)
    return inbox


async def deactivate_user_inbox(session: AsyncSession, user_id: int) -> bool:
    """Deactivate/destroy any active anonymous inbox link for a user."""
    stmt = (
        update(AnonymousInbox)
        .where(AnonymousInbox.user_id == user_id, AnonymousInbox.is_active.is_(True))
        .values(is_active=False, deactivated_at=func.now())
    )
    res = await session.execute(stmt)
    await session.commit()
    return (res.rowcount or 0) > 0


async def get_inbox_by_token(session: AsyncSession, token: str) -> Optional[AnonymousInbox]:
    """Look up an inbox by its unique token, returning none if missing."""
    stmt = select(AnonymousInbox).where(AnonymousInbox.token == token)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def is_inbox_sender_blocked(session: AsyncSession, owner_id: int, sender_id: int) -> bool:
    """Check if the owner has blocked this sender from their anonymous inbox."""
    stmt = select(func.count(InboxBlock.id)).where(
        InboxBlock.owner_id == owner_id,
        InboxBlock.blocked_sender_id == sender_id,
    )
    result = await session.execute(stmt)
    return (result.scalar() or 0) > 0


async def block_inbox_sender(session: AsyncSession, owner_id: int, sender_id: int) -> bool:
    """Block a sender from sending messages to this owner's anonymous inbox."""
    stmt = select(InboxBlock).where(
        InboxBlock.owner_id == owner_id,
        InboxBlock.blocked_sender_id == sender_id,
    )
    res = await session.execute(stmt)
    if res.scalar_one_or_none() is None:
        block = InboxBlock(owner_id=owner_id, blocked_sender_id=sender_id)
        session.add(block)
        await session.commit()
        return True
    return False


async def get_inbox_cooldown_remaining(
    session: AsyncSession,
    owner_id: int,
    sender_id: int,
    cooldown_seconds: int = 1200,  # 20 minutes
) -> int:
    """Return remaining cooldown seconds between sender and owner, or 0 if allowed."""
    stmt = (
        select(InboxMessage.created_at)
        .where(InboxMessage.owner_id == owner_id, InboxMessage.sender_id == sender_id)
        .order_by(InboxMessage.created_at.desc())
        .limit(1)
    )
    res = await session.execute(stmt)
    last_sent = res.scalar_one_or_none()
    if not last_sent:
        return 0

    now = datetime.now(timezone.utc)
    last_sent_utc = last_sent if last_sent.tzinfo else last_sent.replace(tzinfo=timezone.utc)
    elapsed = (now - last_sent_utc).total_seconds()
    if elapsed < cooldown_seconds:
        return int(cooldown_seconds - elapsed)
    return 0


async def record_inbox_message(
    session: AsyncSession,
    owner_id: int,
    sender_id: int,
    message_type: str = "text",
) -> InboxMessage:
    """Record an anonymous inbox message and generate a thread token for replies."""
    thread_token = secrets.token_hex(8)
    msg = InboxMessage(
        owner_id=owner_id,
        sender_id=sender_id,
        thread_token=thread_token,
        message_type=message_type,
    )
    session.add(msg)
    await session.commit()
    await session.refresh(msg)
    return msg


async def get_inbox_message_by_thread(
    session: AsyncSession,
    thread_token: str,
) -> Optional[InboxMessage]:
    """Retrieve an inbox message record by its unique reply thread token."""
    stmt = select(InboxMessage).where(InboxMessage.thread_token == thread_token)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


# ==============================================================================
# Direct Chat Invitation Links (/invite) CRUD Operations
# ==============================================================================

async def create_chat_invite(
    session: AsyncSession,
    creator_id: int,
    invite_type: str = "one_time",
) -> ChatInvite:
    """Create a new one-time or persistent direct chat invitation link."""
    while True:
        code = secrets.token_hex(4)
        existing = await session.execute(
            select(ChatInvite.id).where(ChatInvite.invite_code == code)
        )
        if existing.scalar_one_or_none() is None:
            break

    invite = ChatInvite(
        creator_id=creator_id,
        invite_code=code,
        invite_type=invite_type,
        is_used=False,
        is_active=True,
    )
    session.add(invite)
    await session.commit()
    await session.refresh(invite)
    return invite


async def get_chat_invite_by_code(
    session: AsyncSession,
    invite_code: str,
) -> Optional[ChatInvite]:
    """Retrieve a chat invite by code."""
    stmt = select(ChatInvite).where(ChatInvite.invite_code == invite_code)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_user_active_invites(
    session: AsyncSession,
    creator_id: int,
) -> List[ChatInvite]:
    """Return all active (usable) invites for a user."""
    stmt = (
        select(ChatInvite)
        .where(
            ChatInvite.creator_id == creator_id,
            ChatInvite.is_active.is_(True),
            or_(ChatInvite.invite_type == "persistent", ChatInvite.is_used.is_(False)),
        )
        .order_by(ChatInvite.created_at.desc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def deactivate_chat_invite(
    session: AsyncSession,
    creator_id: int,
    invite_code: str,
) -> bool:
    """Revoke/destroy an invitation link created by a user."""
    stmt = (
        update(ChatInvite)
        .where(ChatInvite.creator_id == creator_id, ChatInvite.invite_code == invite_code)
        .values(is_active=False)
    )
    res = await session.execute(stmt)
    await session.commit()
    return (res.rowcount or 0) > 0


async def consume_chat_invite(
    session: AsyncSession,
    invite_id: int,
    used_by_id: int,
) -> None:
    """Mark a one-time chat invite as used."""
    stmt = (
        update(ChatInvite)
        .where(ChatInvite.id == invite_id, ChatInvite.invite_type == "one_time")
        .values(is_used=True, used_by_id=used_by_id, used_at=func.now())
    )
    await session.execute(stmt)
    await session.commit()









