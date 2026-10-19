"""Test suite to verify that active chat sessions persist across bot restarts."""

import asyncio
import logging
from datetime import datetime, timezone
from sqlalchemy import select, delete

from app.database.session import get_db_session
from app.database.models import User, ChatSession, Feedback, Report
from app.database.crud import (
    get_or_create_user,
    update_user_profile,
    sync_active_sessions_on_startup,
    end_active_session,
    get_active_session_by_user_id,
)
from app.services.matcher import match_user_in_queue

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("test_persistence")

TEST_USER_1 = 99999001
TEST_USER_2 = 99999002


async def cleanup_test_data():
    """Remove test users and their sessions."""
    async with get_db_session() as session:
        # Delete sessions involving test users
        stmt_sessions = (
            delete(ChatSession)
            .where(
                (ChatSession.user1_id.in_([TEST_USER_1, TEST_USER_2])) |
                (ChatSession.user2_id.in_([TEST_USER_1, TEST_USER_2]))
            )
        )
        await session.execute(stmt_sessions)

        # Delete users
        stmt_users = (
            delete(User)
            .where(User.id.in_([TEST_USER_1, TEST_USER_2]))
        )
        await session.execute(stmt_users)
        await session.commit()
    logger.info("Cleaned up any previous test data.")


async def test_session_lifecycle_and_restart():
    await cleanup_test_data()

    async with get_db_session() as session:
        # 1. Setup two test users
        u1 = await get_or_create_user(session, user_id=TEST_USER_1, username="test_user_1")
        u2 = await get_or_create_user(session, user_id=TEST_USER_2, username="test_user_2")
        await update_user_profile(session, user_id=TEST_USER_1, sex="female", orientation="lesbian")
        await update_user_profile(session, user_id=TEST_USER_2, sex="female", orientation="lesbian")

        # 2. Put user 2 in queue
        candidate, chat_session = await match_user_in_queue(session, user_id=TEST_USER_2)
        assert candidate is None
        assert chat_session is None

        # 3. User 1 searches and matches User 2
        partner, session_obj = await match_user_in_queue(session, user_id=TEST_USER_1)
        assert partner is not None
        assert partner.id == TEST_USER_2
        assert session_obj is not None
        assert session_obj.is_active is True
        session_id = session_obj.id
        logger.info("Matched users %s and %s in session %d", TEST_USER_1, TEST_USER_2, session_id)

    # 4. Verify initial active state in DB
    async with get_db_session() as session:
        u1 = await session.get(User, TEST_USER_1)
        u2 = await session.get(User, TEST_USER_2)
        cs = await session.get(ChatSession, session_id)

        assert u1.current_partner_id == TEST_USER_2, f"Expected {TEST_USER_2}, got {u1.current_partner_id}"
        assert u2.current_partner_id == TEST_USER_1, f"Expected {TEST_USER_1}, got {u2.current_partner_id}"
        assert u1.is_searching is False
        assert u2.is_searching is False
        assert cs.is_active is True
        logger.info("Initial state verified: both users partnered, session active.")

    # 5. SIMULATE BOT RESTART:
    # Run the startup synchronization logic that executes when the bot container boots up
    logger.info("Simulating bot restart and running sync_active_sessions_on_startup...")
    async with get_db_session() as session:
        verified_count = await sync_active_sessions_on_startup(session)
        logger.info("Startup sync verified %d active session(s)", verified_count)
        assert verified_count >= 1, "Expected at least 1 active session"

    # 6. Verify that after the restart, the session and partner connections remain INTACT
    async with get_db_session() as session:
        u1_after = await session.get(User, TEST_USER_1)
        u2_after = await session.get(User, TEST_USER_2)
        cs_after = await session.get(ChatSession, session_id)

        assert u1_after.current_partner_id == TEST_USER_2, "Partner ID for User 1 was lost on restart!"
        assert u2_after.current_partner_id == TEST_USER_1, "Partner ID for User 2 was lost on restart!"
        assert u1_after.is_searching is False
        assert u2_after.is_searching is False
        assert cs_after.is_active is True, "Active session was marked inactive on restart!"
        logger.info("Post-restart state verified: Session %d is STILL ACTIVE and users remain CONNECTED!", session_id)

    # 7. Test Self-Healing:
    # Intentionally corrupt u1's partner pointer (e.g. simulated crash mid-write)
    async with get_db_session() as session:
        u1 = await session.get(User, TEST_USER_1)
        u1.current_partner_id = None
        u1.is_searching = True
        await session.commit()

    # Now run startup sync - it should self-heal and reconnect u1 to u2
    async with get_db_session() as session:
        healed_count = await sync_active_sessions_on_startup(session)
        assert healed_count >= 1

    async with get_db_session() as session:
        u1_healed = await session.get(User, TEST_USER_1)
        u2_healed = await session.get(User, TEST_USER_2)
        assert u1_healed.current_partner_id == TEST_USER_2, "Failed to self-heal u1 partner pointer!"
        assert u1_healed.is_searching is False, "Failed to reset is_searching flag during heal!"
        assert u2_healed.current_partner_id == TEST_USER_1
        logger.info("Self-healing test passed: Incomplete state repaired successfully on startup.")

    # 8. Test end active session
    async with get_db_session() as session:
        result = await end_active_session(session, user_id=TEST_USER_1)
        assert result is not None
        ended_session, partner_id = result
        assert ended_session.id == session_id
        assert ended_session.is_active is False
        assert partner_id == TEST_USER_2

        # Verify both cleared
        u1_ended = await session.get(User, TEST_USER_1)
        u2_ended = await session.get(User, TEST_USER_2)
        assert u1_ended.current_partner_id is None
        assert u2_ended.current_partner_id is None
        assert u1_ended.completed_sessions_count == 1
        assert u2_ended.completed_sessions_count == 1
        logger.info("Session cleanly ended after testing.")

    # 9. Test dangling user pointer cleanup
    async with get_db_session() as session:
        u1 = await session.get(User, TEST_USER_1)
        u1.current_partner_id = 123456789  # Non-existent session
        await session.commit()

    async with get_db_session() as session:
        await sync_active_sessions_on_startup(session)

    async with get_db_session() as session:
        u1_cleaned = await session.get(User, TEST_USER_1)
        assert u1_cleaned.current_partner_id is None, "Dangling pointer was not cleaned up!"
        logger.info("Dangling pointer cleanup test passed.")

    await cleanup_test_data()
    logger.info("ALL TESTS PASSED SUCCESSFULLY!")


if __name__ == "__main__":
    asyncio.run(test_session_lifecycle_and_restart())
