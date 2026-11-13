"""Reusable database CRUD queries and operations."""

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import statistics
from typing import Any, Dict, List, Optional, Tuple
from sqlalchemy import and_, delete, desc, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

import secrets

from app.config import settings
from app.database.models import (
    AnonymousInbox,
    BotActivitySnapshot,
    ChatInvite,
    ChatMessageMap,
    ChatSession,
    Feedback,
    InboxBlock,
    InboxMessage,
    ReconnectRequest,
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
    user.custom_target_sexes = user.target_sexes
    user.custom_target_orientations = user.target_orientations
    if not getattr(user, "search_mode", None):
        user.search_mode = "preferences"

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
            joined_sexes = ",".join(clean_sexes)
            user.target_sexes = joined_sexes
            user.custom_target_sexes = joined_sexes
        if target_orientations is not None:
            clean_orients = list(dict.fromkeys(o.lower().strip() for o in target_orientations if o.strip()))
            joined_orients = ",".join(clean_orients)
            user.target_orientations = joined_orients
            user.custom_target_orientations = joined_orients
        if target_sexes is not None or target_orientations is not None:
            user.search_mode = "preferences"
        if show_typing is not None:
            user.show_typing = show_typing
        await session.commit()
        await session.refresh(user)
    return user


async def apply_user_search_mode(
    session: AsyncSession,
    user_id: int,
    mode: Optional[str] = None,
) -> Optional[User]:
    """Set or re-apply user's active search mode and update active target_sexes & target_orientations.

    Preserves user's saved custom /preferences in custom_target_sexes and custom_target_orientations.
    """
    from app.services.matcher import resolve_search_mode_preferences

    stmt = select(User).where(User.id == user_id).with_for_update()
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()
    if not user:
        return None

    # Preserve custom preferences if not backed up yet
    if not user.custom_target_sexes and user.target_sexes:
        user.custom_target_sexes = user.target_sexes
    if not user.custom_target_orientations and user.target_orientations:
        user.custom_target_orientations = user.target_orientations

    resolved_mode, sexes, orients = resolve_search_mode_preferences(user, mode=mode)
    user.search_mode = resolved_mode
    user.target_sexes = ",".join(sexes)
    user.target_orientations = ",".join(orients)

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


async def delete_user_account(
    session: AsyncSession,
    user_id: int,
) -> Tuple[bool, Optional[int]]:
    """Permanently delete a user account and cascade all associated data.

    Returns:
        (success: bool, partner_id: Optional[int])
        partner_id is populated if an active session was ended so the partner can be notified.
    """
    # 1. End active session if any
    ended = await end_active_session(session, user_id)
    partner_id = ended[1] if ended else None

    # 2. Clear any lingering partner reference
    stmt_partner_clear = (
        update(User)
        .where(User.current_partner_id == user_id)
        .values(current_partner_id=None)
    )
    await session.execute(stmt_partner_clear)

    # 3. Delete the user (cascades to all foreign keys in PostgreSQL)
    stmt = delete(User).where(User.id == user_id)
    res = await session.execute(stmt)
    await session.commit()
    return (res.rowcount > 0, partner_id)


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

    # Schedule 5-minute auto-destruction of ephemeral RAM buffer
    try:
        from app.services.ai_moderator import session_buffer
        session_buffer.schedule_cleanup(chat_session.id, ttl=300)
    except Exception:
        pass

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
    offset: int = 0,
) -> List[ChatSession]:
    """Retrieve the most recent completed chat sessions for a user."""
    stmt = (
        select(ChatSession)
        .where(
            or_(ChatSession.user1_id == user_id, ChatSession.user2_id == user_id),
            ChatSession.is_active == False,  # noqa: E712
        )
        .order_by(ChatSession.ended_at.desc().nullslast(), ChatSession.id.desc())
        .offset(offset)
        .limit(limit)
    )
    res = await session.execute(stmt)
    return list(res.scalars().all())


async def count_recent_sessions_for_user(
    session: AsyncSession,
    user_id: int,
) -> int:
    """Return the total number of completed chat sessions for a user."""
    stmt = select(func.count(ChatSession.id)).where(
        or_(ChatSession.user1_id == user_id, ChatSession.user2_id == user_id),
        ChatSession.is_active == False,  # noqa: E712
    )
    res = await session.execute(stmt)
    return int(res.scalar() or 0)


async def create_report(
    session: AsyncSession,
    session_id: Optional[int] = None,
    inbox_message_id: Optional[int] = None,
    thread_token: Optional[str] = None,
    reporter_id: Optional[int] = None,
    reported_id: Optional[int] = None,
    reason_text: Optional[str] = None,
    evidence_file_id: Optional[str] = None,
    guilty_party: Optional[str] = None,
    severity_category: Optional[str] = None,
    reported_user_penalty: int = 0,
    reporter_penalty: int = 0,
    reported_user_blocked: bool = False,
    reporter_blocked: bool = False,
    identity_revealed_for_illegal: bool = False,
    anonymized_summary: Optional[str] = None,
    is_resolved: bool = True,
    penalty_applied: Optional[int] = None,
) -> Report:
    """Create a new report filed against a session or inbox message with automated AI moderation audit fields."""
    report = Report(
        session_id=session_id,
        inbox_message_id=inbox_message_id,
        thread_token=thread_token,
        reporter_id=reporter_id,
        reported_id=reported_id,
        reason_text=reason_text,
        evidence_file_id=evidence_file_id,
        guilty_party=guilty_party,
        severity_category=severity_category,
        reported_user_penalty=reported_user_penalty,
        reporter_penalty=reporter_penalty,
        reported_user_blocked=reported_user_blocked,
        reporter_blocked=reporter_blocked,
        identity_revealed_for_illegal=identity_revealed_for_illegal,
        anonymized_summary=anonymized_summary,
        is_resolved=is_resolved,
        penalty_applied=penalty_applied,
    )
    session.add(report)
    await session.commit()
    await session.refresh(report)
    return report


async def has_report_for_session(
    session: AsyncSession,
    session_id: int,
) -> bool:
    """Check if a report has already been submitted for a given chat session."""
    stmt = select(func.count(Report.id)).where(Report.session_id == session_id)
    count = (await session.execute(stmt)).scalar_one()
    return count > 0


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

    # Anonymous Inbox & Q&A Stats
    inbox_users_count = (
        await session.execute(select(func.count(func.distinct(AnonymousInbox.user_id))))
    ).scalar_one() or 0
    active_inboxes_count = (
        await session.execute(
            select(func.count(AnonymousInbox.id)).where(AnonymousInbox.is_active == True)  # noqa: E712
        )
    ).scalar_one() or 0
    total_inbox_messages = (
        await session.execute(select(func.count(InboxMessage.id)))
    ).scalar_one() or 0
    replied_inbox_messages = (
        await session.execute(
            select(func.count(InboxMessage.id)).where(InboxMessage.is_replied == True)  # noqa: E712
        )
    ).scalar_one() or 0
    total_inbox_replies = (
        await session.execute(select(func.coalesce(func.sum(InboxMessage.reply_count), 0)))
    ).scalar_one() or 0
    inbox_reply_rate = (
        round((replied_inbox_messages / total_inbox_messages) * 100, 1)
        if total_inbox_messages > 0
        else 0.0
    )

    # Direct Chat Invites Stats
    total_chat_invites = (
        await session.execute(select(func.count(ChatInvite.id)))
    ).scalar_one() or 0
    active_chat_invites = (
        await session.execute(
            select(func.count(ChatInvite.id)).where(ChatInvite.is_active == True)  # noqa: E712
        )
    ).scalar_one() or 0
    used_chat_invites = (
        await session.execute(
            select(func.count(ChatInvite.id)).where(ChatInvite.is_used == True)  # noqa: E712
        )
    ).scalar_one() or 0

    advanced_chat = await get_advanced_bot_chat_stats(session)

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
        "inbox_users_count": inbox_users_count,
        "active_inboxes_count": active_inboxes_count,
        "total_inbox_messages": total_inbox_messages,
        "replied_inbox_messages": replied_inbox_messages,
        "total_inbox_replies": total_inbox_replies,
        "inbox_reply_rate": inbox_reply_rate,
        "total_chat_invites": total_chat_invites,
        "active_chat_invites": active_chat_invites,
        "used_chat_invites": used_chat_invites,
        "advanced_chat": advanced_chat,
    }


YEREVAN_TZ = timezone(timedelta(hours=4))


def _ensure_utc(dt: Optional[datetime], fallback: datetime) -> datetime:
    if dt is None:
        return fallback
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _sweep_concurrency(
    intervals: List[Tuple[float, float]],
    grid_timestamps: List[float],
) -> List[int]:
    """Compute exact concurrent active sessions at every sorted timestamp in O(N log N + M)."""
    if not grid_timestamps:
        return []
    if not intervals:
        return [0] * len(grid_timestamps)

    starts = sorted(st for st, _ in intervals)
    ends = sorted(en for _, en in intervals)
    si = 0
    ei = 0
    active = 0
    n = len(starts)
    out: List[int] = []
    for t in grid_timestamps:
        while si < n and starts[si] <= t:
            active += 1
            si += 1
        while ei < n and ends[ei] < t:
            active -= 1
            ei += 1
        out.append(max(0, active))
    return out


def _safe_mean(vals: List[float]) -> float:
    return round(float(statistics.mean(vals)), 2) if vals else 0.0


def _safe_median(vals: List[float]) -> float:
    return round(float(statistics.median(vals)), 2) if vals else 0.0


async def record_bot_activity_snapshot(
    session: AsyncSession,
    web_ws_clients: int = 0,
) -> BotActivitySnapshot:
    """Capture a point-in-time snapshot of active anonymous chats, searching queue, and web sockets."""
    active_chats = (
        await session.execute(select(func.count(ChatSession.id)).where(ChatSession.is_active == True))  # noqa: E712
    ).scalar_one() or 0
    non_paused_chats = (
        await session.execute(
            select(func.count(ChatSession.id)).where(
                ChatSession.is_active == True,  # noqa: E712
                ChatSession.is_paused == False,  # noqa: E712
            )
        )
    ).scalar_one() or 0
    searching_users = (
        await session.execute(select(func.count(User.id)).where(User.is_searching == True))  # noqa: E712
    ).scalar_one() or 0

    snap = BotActivitySnapshot(
        active_chats=int(active_chats),
        non_paused_chats=int(non_paused_chats),
        searching_users=int(searching_users),
        web_ws_clients=int(max(0, web_ws_clients)),
    )
    session.add(snap)
    await session.commit()
    return snap


async def get_advanced_bot_chat_stats(session: AsyncSession) -> Dict[str, Any]:
    """Compute deep anonymous chat telemetry (mean/median active sessions, 24h & 30d metrics, peak hours AMT)."""
    now = datetime.now(timezone.utc)
    now_amt = now.astimezone(YEREVAN_TZ)
    day_ago = now - timedelta(hours=24)
    month_ago = now - timedelta(days=30)

    # Fetch lightweight columns for all chat sessions
    cs_stmt = select(
        ChatSession.started_at,
        ChatSession.ended_at,
        ChatSession.is_active,
        ChatSession.is_paused,
        ChatSession.messages_count,
    ).order_by(ChatSession.started_at.asc())
    cs_rows = (await session.execute(cs_stmt)).all()

    # Fetch bot user registration timestamps for growth curve
    u_rows = (
        await session.execute(select(User.created_at).order_by(User.created_at.asc()))
    ).scalars().all()

    # Fetch recent snapshots (optional queue depth telemetry)
    snap_rows = (
        await session.execute(
            select(BotActivitySnapshot)
            .where(BotActivitySnapshot.created_at >= day_ago)
            .order_by(BotActivitySnapshot.created_at.asc())
        )
    ).scalars().all()

    intervals: List[Tuple[float, float]] = []
    sessions_data: List[Dict[str, Any]] = []
    live_active = 0
    live_paused = 0

    for started_at, ended_at, is_active, is_paused, msgs_count in cs_rows:
        st = _ensure_utc(started_at, now)
        en = _ensure_utc(ended_at, now) if not is_active and ended_at is not None else now
        if ended_at is not None and not is_active:
            en = _ensure_utc(ended_at, now)
        if en < st:
            en = st
        dur_sec = (en - st).total_seconds()
        msgs = int(msgs_count or 0)
        if is_active:
            live_active += 1
            if is_paused:
                live_paused += 1

        intervals.append((st.timestamp(), en.timestamp()))
        sessions_data.append(
            {
                "started_at": st,
                "ended_at": en,
                "amt_start": st.astimezone(YEREVAN_TZ),
                "is_active": bool(is_active),
                "is_paused": bool(is_paused),
                "messages": msgs,
                "duration_sec": dur_sec,
            }
        )

    # 1. Concurrency Sweep for 24h (5-minute resolution) and 30d (15-minute resolution)
    grid_24h_dt: List[datetime] = []
    t = day_ago
    while t <= now:
        grid_24h_dt.append(t)
        t += timedelta(minutes=5)
    grid_24h_ts = [dt.timestamp() for dt in grid_24h_dt]
    conc_24h = _sweep_concurrency(intervals, grid_24h_ts)

    grid_30d_dt: List[datetime] = []
    t = month_ago
    while t <= now:
        grid_30d_dt.append(t)
        t += timedelta(minutes=15)
    grid_30d_ts = [dt.timestamp() for dt in grid_30d_dt]
    conc_30d = _sweep_concurrency(intervals, grid_30d_ts)

    # All-time concurrency from earliest session (30-min resolution)
    if sessions_data:
        first_st = max(sessions_data[0]["started_at"], now - timedelta(days=365))
    else:
        first_st = month_ago
    grid_all_ts: List[float] = []
    t_all = first_st.timestamp()
    end_ts = now.timestamp()
    while t_all <= end_ts:
        grid_all_ts.append(t_all)
        t_all += 1800.0
    conc_all = _sweep_concurrency(intervals, grid_all_ts)

    # 2. Initiated, Engaged, Messages & Duration stats across 24h, 30d, All-Time
    s_24h = [s for s in sessions_data if s["started_at"] >= day_ago]
    s_30d = [s for s in sessions_data if s["started_at"] >= month_ago]

    def _summarize_bucket(bucket: List[Dict[str, Any]]) -> Dict[str, Any]:
        total_cnt = len(bucket)
        engaged = [s for s in bucket if s["messages"] > 0]
        msgs_list = [float(s["messages"]) for s in bucket]
        dur_list = [float(s["duration_sec"]) for s in bucket]
        eng_dur_list = [float(s["duration_sec"]) for s in engaged]
        return {
            "initiated": total_cnt,
            "engaged": len(engaged),
            "engagement_rate": round((len(engaged) / total_cnt) * 100, 1) if total_cnt > 0 else 0.0,
            "messages_total": int(sum(msgs_list)),
            "messages_mean": _safe_mean(msgs_list),
            "messages_median": _safe_median(msgs_list),
            "duration_mean_sec": _safe_mean(dur_list),
            "duration_median_sec": _safe_median(dur_list),
            "engaged_duration_median_sec": _safe_median(eng_dur_list),
        }

    sum_24h = _summarize_bucket(s_24h)
    sum_30d = _summarize_bucket(s_30d)
    sum_all = _summarize_bucket(sessions_data)

    # 3. 30-Day Daily Series (AMT calendar days)
    day_keys: List[str] = []
    day_labels: Dict[str, str] = {}
    for offset in range(29, -1, -1):
        d = (now_amt - timedelta(days=offset)).date()
        dk = d.isoformat()
        day_keys.append(dk)
        day_labels[dk] = d.strftime("%b %d")

    daily_init: Dict[str, int] = defaultdict(int)
    daily_engaged: Dict[str, int] = defaultdict(int)
    daily_msgs: Dict[str, int] = defaultdict(int)
    daily_new_users: Dict[str, int] = defaultdict(int)
    daily_conc_samples: Dict[str, List[int]] = defaultdict(list)

    for s in s_30d:
        dk = s["amt_start"].date().isoformat()
        if dk in day_labels:
            daily_init[dk] += 1
            if s["messages"] > 0:
                daily_engaged[dk] += 1
            daily_msgs[dk] += s["messages"]

    for u_dt in u_rows:
        if u_dt is not None:
            u_amt = _ensure_utc(u_dt, now).astimezone(YEREVAN_TZ)
            dk = u_amt.date().isoformat()
            if dk in day_labels:
                daily_new_users[dk] += 1

    for dt, val in zip(grid_30d_dt, conc_30d):
        dk = dt.astimezone(YEREVAN_TZ).date().isoformat()
        if dk in day_labels:
            daily_conc_samples[dk].append(val)

    daily_series_30d = []
    daily_init_values = []
    for dk in day_keys:
        c_list = daily_conc_samples.get(dk, [])
        init_v = daily_init.get(dk, 0)
        daily_init_values.append(float(init_v))
        daily_series_30d.append(
            {
                "date": dk,
                "label": day_labels[dk],
                "chats_initiated": init_v,
                "engaged_chats": daily_engaged.get(dk, 0),
                "messages_total": daily_msgs.get(dk, 0),
                "new_bot_users": daily_new_users.get(dk, 0),
                "concurrent_mean": _safe_mean([float(x) for x in c_list]),
                "concurrent_median": _safe_median([float(x) for x in c_list]),
                "concurrent_peak": max(c_list) if c_list else 0,
            }
        )

    # 4. Rolling 24h Hourly Breakdown & 15-min Concurrency Curve
    hour_buckets_24h: Dict[str, Dict[str, Any]] = {}
    hour_keys_24h: List[str] = []
    base_hour_amt = now_amt.replace(minute=0, second=0, microsecond=0)
    for offset in range(23, -1, -1):
        h_dt = base_hour_amt - timedelta(hours=offset)
        hk = h_dt.strftime("%Y-%m-%d %H")
        hour_keys_24h.append(hk)
        hour_buckets_24h[hk] = {
            "label": h_dt.strftime("%H:00"),
            "chats_initiated": 0,
            "messages_total": 0,
            "samples": [],
        }

    for s in s_24h:
        hk = s["amt_start"].strftime("%Y-%m-%d %H")
        if hk in hour_buckets_24h:
            hour_buckets_24h[hk]["chats_initiated"] += 1
            hour_buckets_24h[hk]["messages_total"] += s["messages"]

    for dt, val in zip(grid_24h_dt, conc_24h):
        hk = dt.astimezone(YEREVAN_TZ).strftime("%Y-%m-%d %H")
        if hk in hour_buckets_24h:
            hour_buckets_24h[hk]["samples"].append(val)

    rolling_24h_series = []
    for hk in hour_keys_24h:
        b = hour_buckets_24h[hk]
        s_vals = b["samples"]
        rolling_24h_series.append(
            {
                "label": b["label"],
                "chats_initiated": b["chats_initiated"],
                "messages_total": b["messages_total"],
                "concurrent_mean": _safe_mean([float(x) for x in s_vals]),
                "concurrent_median": _safe_median([float(x) for x in s_vals]),
                "concurrent_peak": max(s_vals) if s_vals else 0,
            }
        )

    # 24h fine-grained concurrency curve (sample every 3rd point = 15-min steps, 96 points)
    concurrency_24h_curve = [
        {
            "label": dt.astimezone(YEREVAN_TZ).strftime("%H:%M"),
            "active_sessions": val,
        }
        for dt, val in list(zip(grid_24h_dt, conc_24h))[::3]
    ]

    # 5. Clock-Hour Activity Profile (00..23 AMT) & 7x24 Heatmap Matrix
    clock_init_30d = [0] * 24
    clock_init_24h = [0] * 24
    clock_init_all = [0] * 24
    clock_msgs_30d = [0] * 24
    clock_conc_30d: List[List[int]] = [[] for _ in range(24)]
    clock_conc_24h: List[List[int]] = [[] for _ in range(24)]
    heatmap_matrix = [[0 for _ in range(24)] for _ in range(7)]  # weekday (0=Mon..6=Sun) x hour (0..23)

    for s in sessions_data:
        h = s["amt_start"].hour
        wd = s["amt_start"].weekday()
        clock_init_all[h] += 1
        if s["started_at"] >= month_ago:
            clock_init_30d[h] += 1
            clock_msgs_30d[h] += s["messages"]
            heatmap_matrix[wd][h] += 1
        if s["started_at"] >= day_ago:
            clock_init_24h[h] += 1

    for dt, val in zip(grid_30d_dt, conc_30d):
        clock_conc_30d[dt.astimezone(YEREVAN_TZ).hour].append(val)

    for dt, val in zip(grid_24h_dt, conc_24h):
        clock_conc_24h[dt.astimezone(YEREVAN_TZ).hour].append(val)

    hourly_profile = []
    for h in range(24):
        c30_list = clock_conc_30d[h]
        c24_list = clock_conc_24h[h]
        hourly_profile.append(
            {
                "hour": h,
                "label": f"{h:02d}:00",
                "window_label": f"{h:02d}:00 - {(h + 1) % 24:02d}:00 AMT",
                "initiated_30d": clock_init_30d[h],
                "initiated_24h": clock_init_24h[h],
                "initiated_all": clock_init_all[h],
                "messages_30d": clock_msgs_30d[h],
                "avg_concurrent_30d": _safe_mean([float(x) for x in c30_list]),
                "median_concurrent_30d": _safe_median([float(x) for x in c30_list]),
                "avg_concurrent_24h": _safe_mean([float(x) for x in c24_list]),
            }
        )

    peak_by_initiated = sorted(
        hourly_profile,
        key=lambda x: (x["initiated_30d"], x["avg_concurrent_30d"]),
        reverse=True,
    )
    peak_by_concurrency = sorted(
        hourly_profile,
        key=lambda x: (x["avg_concurrent_30d"], x["initiated_30d"]),
        reverse=True,
    )
    top_peak_hours = peak_by_initiated[:5]
    top_conc_hours = peak_by_concurrency[:5]

    best_init = top_peak_hours[0] if top_peak_hours else {"window_label": "N/A", "initiated_30d": 0}
    best_conc = top_conc_hours[0] if top_conc_hours else {"window_label": "N/A", "avg_concurrent_30d": 0.0}

    # Mean & median of queue depth from snapshots if available
    queue_24h_vals = [float(sn.searching_users) for sn in snap_rows]
    ws_24h_vals = [float(sn.web_ws_clients) for sn in snap_rows]

    return {
        "timezone": "Asia/Yerevan (UTC+4)",
        "live": {
            "active_sessions_now": live_active,
            "actively_chatting_now": max(0, live_active - live_paused),
            "paused_now": live_paused,
            "queue_mean_24h": _safe_mean(queue_24h_vals),
            "ws_clients_mean_24h": _safe_mean(ws_24h_vals),
            "snapshots_24h_count": len(snap_rows),
        },
        "window_24h": {
            "concurrent_mean": _safe_mean([float(x) for x in conc_24h]),
            "concurrent_median": _safe_median([float(x) for x in conc_24h]),
            "concurrent_peak": max(conc_24h) if conc_24h else 0,
            "concurrent_min": min(conc_24h) if conc_24h else 0,
            **sum_24h,
        },
        "window_30d": {
            "concurrent_mean": _safe_mean([float(x) for x in conc_30d]),
            "concurrent_median": _safe_median([float(x) for x in conc_30d]),
            "concurrent_peak": max(conc_30d) if conc_30d else 0,
            "concurrent_min": min(conc_30d) if conc_30d else 0,
            "chats_initiated_daily_mean": _safe_mean(daily_init_values),
            "chats_initiated_daily_median": _safe_median(daily_init_values),
            **sum_30d,
        },
        "window_all_time": {
            "concurrent_mean": _safe_mean([float(x) for x in conc_all]),
            "concurrent_median": _safe_median([float(x) for x in conc_all]),
            "concurrent_peak": max(conc_all) if conc_all else 0,
            **sum_all,
        },
        "peak_hours": {
            "top_by_initiated_30d": top_peak_hours,
            "top_by_concurrency_30d": top_conc_hours,
            "most_active_init_window": best_init["window_label"],
            "most_active_conc_window": best_conc["window_label"],
        },
        "hourly_profile": hourly_profile,
        "rolling_24h_series": rolling_24h_series,
        "concurrency_24h_curve": concurrency_24h_curve,
        "daily_series_30d": daily_series_30d,
        "heatmap_7x24": {
            "days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
            "hours": [f"{h:02d}" for h in range(24)],
            "matrix": heatmap_matrix,
        },
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
    reason: Optional[str] = None,
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
        if reason is not None:
            block.reason = reason
    else:
        block = UserBlock(
            user_id=user_id,
            blocked_user_id=blocked_user_id,
            created_at=now,
            expires_at=expires_at,
            reason=reason,
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
    - Verification reminders are not permanently disabled (verification_reminders_disabled = False).
    - Reminder snooze period has expired (verification_reminder_snoozed_until is None or <= now).
    - Account was created at least `hours_threshold` hours ago.
    - Either never reminded or last reminder was sent at least `hours_threshold` hours ago.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours_threshold)
    stmt = (
        select(User)
        .where(
            User.is_banned.is_(False),
            User.sex.is_not(None),
            User.orientation.is_not(None),
            User.is_verified.is_(False),
            User.verification_reminders_disabled.is_(False),
            or_(
                User.verification_reminder_snoozed_until.is_(None),
                User.verification_reminder_snoozed_until <= now,
            ),
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


async def snooze_verification_reminder(
    session: AsyncSession,
    user_id: int,
    days: int = 7,
) -> None:
    """Snooze verification reminders for a given number of days (default 7 days)."""
    stmt = select(User).where(User.id == user_id)
    res = await session.execute(stmt)
    user = res.scalar_one_or_none()
    if user:
        user.verification_reminder_snoozed_until = datetime.now(timezone.utc) + timedelta(days=days)
        await session.commit()


async def disable_verification_reminders(
    session: AsyncSession,
    user_id: int,
) -> None:
    """Permanently disable verification reminders for a user."""
    stmt = select(User).where(User.id == user_id)
    res = await session.execute(stmt)
    user = res.scalar_one_or_none()
    if user:
        user.verification_reminders_disabled = True
        await session.commit()


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
    content_text: Optional[str] = None,
) -> InboxMessage:
    """Record an anonymous inbox message and generate a thread token for replies."""
    thread_token = secrets.token_hex(8)
    msg = InboxMessage(
        owner_id=owner_id,
        sender_id=sender_id,
        thread_token=thread_token,
        message_type=message_type,
        content_text=content_text,
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


async def mark_inbox_message_replied(
    session: AsyncSession,
    thread_token: str,
    reply_text: Optional[str] = None,
) -> bool:
    """Mark an anonymous inbox message as replied and increment its reply count."""
    values_dict = {
        "is_replied": True,
        "reply_count": InboxMessage.reply_count + 1,
        "replied_at": func.now(),
    }
    if reply_text is not None:
        values_dict["reply_text"] = reply_text

    stmt = (
        update(InboxMessage)
        .where(InboxMessage.thread_token == thread_token)
        .values(**values_dict)
    )
    res = await session.execute(stmt)
    await session.commit()
    return res.rowcount > 0


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


# ==============================================================================
# Reconnection Requests (/recent -> Request Reconnect) CRUD Operations
# ==============================================================================

async def get_recent_reconnect_request_between(
    session: AsyncSession,
    sender_id: int,
    recipient_id: int,
    cooldown_hours: int = 24,
) -> Optional[ReconnectRequest]:
    """Return an active or recent reconnection request sent from sender to recipient within cooldown window."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=cooldown_hours)
    stmt = (
        select(ReconnectRequest)
        .where(
            ReconnectRequest.sender_id == sender_id,
            ReconnectRequest.recipient_id == recipient_id,
            ReconnectRequest.created_at >= cutoff,
        )
        .order_by(ReconnectRequest.created_at.desc(), ReconnectRequest.id.desc())
        .limit(1)
    )
    res = await session.execute(stmt)
    return res.scalar_one_or_none()


async def is_user_blocked_by_partner(
    session: AsyncSession,
    partner_id: int,
    user_id: int,
) -> bool:
    """Check if partner_id has an active block or cooldown on user_id."""
    now = datetime.now(timezone.utc)
    stmt = select(func.count(UserBlock.id)).where(
        UserBlock.user_id == partner_id,
        UserBlock.blocked_user_id == user_id,
        or_(UserBlock.expires_at.is_(None), UserBlock.expires_at > now),
    )
    res = await session.execute(stmt)
    return bool((res.scalar() or 0) > 0)


async def create_reconnect_request(
    session: AsyncSession,
    session_id: int,
    sender_id: int,
    recipient_id: int,
    reason_tag: Optional[str] = None,
    status: str = "pending",
    ttl_hours: int = 24,
) -> ReconnectRequest:
    """Create a reconnection request and auto-lift any block the sender had placed on recipient."""
    now = datetime.now(timezone.utc)
    # Auto-lift sender's own block/closed-chapter on recipient
    del_stmt = delete(UserBlock).where(
        UserBlock.user_id == sender_id,
        UserBlock.blocked_user_id == recipient_id,
    )
    await session.execute(del_stmt)

    req = ReconnectRequest(
        session_id=session_id,
        sender_id=sender_id,
        recipient_id=recipient_id,
        reason_tag=reason_tag,
        status=status,
        expires_at=now + timedelta(hours=ttl_hours),
    )
    session.add(req)
    await session.commit()
    await session.refresh(req)
    return req


async def get_reconnect_request_by_id(
    session: AsyncSession,
    request_id: int,
) -> Optional[ReconnectRequest]:
    """Retrieve a reconnection request by primary key."""
    stmt = select(ReconnectRequest).where(ReconnectRequest.id == request_id)
    res = await session.execute(stmt)
    return res.scalar_one_or_none()


async def update_reconnect_request_status(
    session: AsyncSession,
    request_id: int,
    status: str,
) -> bool:
    """Update the status of a reconnection request."""
    stmt = (
        update(ReconnectRequest)
        .where(ReconnectRequest.id == request_id)
        .values(status=status)
    )
    res = await session.execute(stmt)
    await session.commit()
    return bool((res.rowcount or 0) > 0)










