"""Automated test suite for Anti-Spite rating features:
1. Streamlined UI (2-tier post-chat keyboards)
2. Chat duration checks (< 15s instant skip protection)
3. Consensus-based penalty for Spam / Not AUA
"""

import asyncio
from datetime import datetime, timezone, timedelta
import logging
from sqlalchemy import delete, select

from app.database.session import get_db_session, init_db
from app.database.models import User, ChatSession, Feedback
from app.database.crud import (
    get_or_create_user,
    process_feedback_with_consensus,
    get_chat_session,
    get_session_duration_seconds,
)
from app.keyboards.inline import (
    get_feedback_keyboard,
    get_report_options_keyboard,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("test_anti_spite")

TEST_RATER_1 = 77770001
TEST_RATER_2 = 77770002
TEST_TARGET = 77770003

ALL_TEST_USERS = [TEST_RATER_1, TEST_RATER_2, TEST_TARGET]


async def cleanup():
    async with get_db_session() as session:
        # Delete feedbacks
        await session.execute(
            delete(Feedback).where(
                Feedback.from_user_id.in_(ALL_TEST_USERS) |
                Feedback.to_user_id.in_(ALL_TEST_USERS)
            )
        )
        # Delete sessions
        await session.execute(
            delete(ChatSession).where(
                ChatSession.user1_id.in_(ALL_TEST_USERS) |
                ChatSession.user2_id.in_(ALL_TEST_USERS)
            )
        )
        # Delete users
        await session.execute(
            delete(User).where(User.id.in_(ALL_TEST_USERS))
        )
        await session.commit()


async def test_anti_spite_system():
    logger.info("Starting Anti-Spite Feedback test suite...")

    # Initialize DB (runs migrations)
    await init_db()
    await cleanup()

    # 1. Test Keyboards (Streamlined UI)
    fb_kb = get_feedback_keyboard(session_id=1, partner_id=100)
    fb_buttons = [btn.text for row in fb_kb.inline_keyboard for btn in row]
    logger.info("Tier 1 feedback buttons: %s", fb_buttons)
    assert "👍 Great Chat" in fb_buttons
    assert "👎 Not for me" in fb_buttons
    assert "⚠️ Report Issue..." in fb_buttons
    assert "🚫 Spam" not in fb_buttons, "Spam button should be moved to Tier 2 report menu"

    rep_kb = get_report_options_keyboard(session_id=1, partner_id=100)
    rep_buttons = [btn.text for row in rep_kb.inline_keyboard for btn in row]
    logger.info("Tier 2 report options buttons: %s", rep_buttons)
    assert "🚫 Spam / Promo" in rep_buttons
    assert "🏫 Not from AUA" in rep_buttons
    assert "🚨 Harassment / Other" in rep_buttons
    assert "🔙 Back" in rep_buttons

    # 2. Test Setup Users
    async with get_db_session() as session:
        u_rater1 = await get_or_create_user(session, TEST_RATER_1, username="rater1")
        u_rater2 = await get_or_create_user(session, TEST_RATER_2, username="rater2")
        u_target = await get_or_create_user(session, TEST_TARGET, username="target_user")
        u_target.priority = 50
        u_target.spam_flags_count = 0
        await session.commit()

    # 3. Test Chat Duration Check (< 15 seconds)
    # Scenario A: Short chat (< 15s) with 'dislike' -> delta MUST BE 0 (no penalty for rage-skip)
    now = datetime.now(timezone.utc)
    async with get_db_session() as session:
        # Create chat session lasting only 5 seconds
        short_session = ChatSession(
            user1_id=TEST_RATER_1,
            user2_id=TEST_TARGET,
            is_active=False,
            started_at=now - timedelta(seconds=5),
            ended_at=now,
        )
        session.add(short_session)
        await session.commit()
        await session.refresh(short_session)
        s_id_1 = short_session.id

        # Verify duration calculation
        dur = get_session_duration_seconds(short_session)
        assert 4.5 <= dur <= 5.5, f"Expected ~5 seconds, got {dur}"

        # Rater 1 disliking target on 5s chat
        res1 = await process_feedback_with_consensus(
            session=session,
            session_id=s_id_1,
            from_user_id=TEST_RATER_1,
            partner_id=TEST_TARGET,
            feedback_type="dislike",
        )
        assert res1["created"] is True
        assert res1["is_short_chat"] is True
        assert res1["delta"] == 0, "Short chat dislike MUST have 0 delta!"
        assert res1["new_priority"] == 50, f"Priority should remain 50, got {res1['new_priority']}"
        logger.info("Scenario A Passed: Short chat (< 15s) dislike protected target (0 delta).")

    # Scenario B: Normal chat (>= 15s) with 'dislike' -> delta MUST BE -1
    async with get_db_session() as session:
        normal_session = ChatSession(
            user1_id=TEST_RATER_2,
            user2_id=TEST_TARGET,
            is_active=False,
            started_at=now - timedelta(seconds=45),
            ended_at=now,
        )
        session.add(normal_session)
        await session.commit()
        await session.refresh(normal_session)
        s_id_2 = normal_session.id

        res2 = await process_feedback_with_consensus(
            session=session,
            session_id=s_id_2,
            from_user_id=TEST_RATER_2,
            partner_id=TEST_TARGET,
            feedback_type="dislike",
        )
        assert res2["created"] is True
        assert res2["is_short_chat"] is False
        assert res2["delta"] == -1
        assert res2["new_priority"] == 49, f"Priority should drop to 49, got {res2['new_priority']}"
        logger.info("Scenario B Passed: Normal chat (>= 15s) dislike applied fair -1 penalty.")

    # 4. Test Consensus-Based Penalty for Spam / Not AUA
    # Reset target priority to 50 and spam_flags_count to 0
    async with get_db_session() as session:
        tgt = await session.get(User, TEST_TARGET)
        tgt.priority = 50
        tgt.spam_flags_count = 0
        # Delete previous feedbacks for clean test
        await session.execute(delete(Feedback).where(Feedback.to_user_id == TEST_TARGET))
        await session.commit()

    # Step 4.1: First Spam Flag (by Rater 1)
    # Even if chat lasted 60s, a single flag should ONLY apply base penalty (-1), NOT heavy -5!
    async with get_db_session() as session:
        sess_spam_1 = ChatSession(
            user1_id=TEST_RATER_1,
            user2_id=TEST_TARGET,
            is_active=False,
            started_at=now - timedelta(seconds=60),
            ended_at=now,
        )
        session.add(sess_spam_1)
        await session.commit()
        await session.refresh(sess_spam_1)

        res_spam_1 = await process_feedback_with_consensus(
            session=session,
            session_id=sess_spam_1.id,
            from_user_id=TEST_RATER_1,
            partner_id=TEST_TARGET,
            feedback_type="spam",
        )
        assert res_spam_1["created"] is True
        assert res_spam_1["spam_flags_count"] == 1
        assert res_spam_1["consensus_reached"] is False, "1st flag should NOT trigger consensus"
        assert res_spam_1["delta"] == -1, f"1st flag should only be base penalty -1, got {res_spam_1['delta']}"
        assert res_spam_1["new_priority"] == 49
        logger.info("Step 4.1 Passed: 1st Spam flag applied single base penalty (-1), consensus NOT triggered.")

    # Step 4.2: Second Spam Flag from a DIFFERENT User (Rater 2)
    # Consensus REACHED! Multiple independent students reported them.
    # Total penalty should now trigger consensus (-4 additional, bringing total to -5)!
    async with get_db_session() as session:
        sess_spam_2 = ChatSession(
            user1_id=TEST_RATER_2,
            user2_id=TEST_TARGET,
            is_active=False,
            started_at=now - timedelta(seconds=60),
            ended_at=now,
        )
        session.add(sess_spam_2)
        await session.commit()
        await session.refresh(sess_spam_2)

        res_spam_2 = await process_feedback_with_consensus(
            session=session,
            session_id=sess_spam_2.id,
            from_user_id=TEST_RATER_2,
            partner_id=TEST_TARGET,
            feedback_type="spam",
        )
        assert res_spam_2["created"] is True
        assert res_spam_2["spam_flags_count"] == 2
        assert res_spam_2["consensus_reached"] is True, "2nd independent flag MUST trigger consensus!"
        assert res_spam_2["delta"] == -5, f"Consensus penalty should be -5, got {res_spam_2['delta']}"
        assert res_spam_2["new_priority"] == 44, f"Expected 44 (49 - 5), got {res_spam_2['new_priority']}"
        logger.info("Step 4.2 Passed: 2nd Spam flag triggered CONSENSUS (-5 penalty applied, new priority: 44).")

    # Step 4.3: Test 'Like' always adds +1
    async with get_db_session() as session:
        sess_like = ChatSession(
            user1_id=TEST_RATER_1,
            user2_id=TEST_TARGET,
            is_active=False,
            started_at=now - timedelta(seconds=20),
            ended_at=now,
        )
        session.add(sess_like)
        await session.commit()
        await session.refresh(sess_like)

        res_like = await process_feedback_with_consensus(
            session=session,
            session_id=sess_like.id,
            from_user_id=TEST_RATER_1,
            partner_id=TEST_TARGET,
            feedback_type="like",
        )
        assert res_like["created"] is True
        assert res_like["delta"] == 1
        assert res_like["new_priority"] == 45, f"Expected 45 (44 + 1), got {res_like['new_priority']}"
        logger.info("Step 4.3 Passed: Positive like added +1 to priority.")

    await cleanup()
    logger.info("ALL ANTI-SPITE FEEDBACK TESTS PASSED SUCCESSFULLY! 🎉")


if __name__ == "__main__":
    asyncio.run(test_anti_spite_system())
