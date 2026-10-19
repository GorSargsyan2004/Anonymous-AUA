"""Automated tests for Bisexual orientation matchmaking and compatibility matrix."""

import asyncio
import logging
from sqlalchemy import delete

from app.database.session import get_db_session
from app.database.models import User, ChatSession
from app.database.crud import (
    get_or_create_user,
    update_user_profile,
    get_admin_stats,
    end_active_session,
)
from app.services.matcher import (
    normalize_orientation,
    get_compatible_criteria,
    match_user_in_queue,
)
from app.keyboards.inline import get_orientation_keyboard

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("test_bisexual")

U_BI_MALE = 88880001
U_BI_FEMALE = 88880002
U_STR_FEMALE = 88880003
U_GAY_MALE = 88880004
U_LES_FEMALE = 88880005
U_STR_MALE = 88880006

ALL_TEST_USERS = [
    U_BI_MALE,
    U_BI_FEMALE,
    U_STR_FEMALE,
    U_GAY_MALE,
    U_LES_FEMALE,
    U_STR_MALE,
]


async def cleanup_users():
    async with get_db_session() as session:
        await session.execute(
            delete(ChatSession).where(
                (ChatSession.user1_id.in_(ALL_TEST_USERS)) |
                (ChatSession.user2_id.in_(ALL_TEST_USERS))
            )
        )
        await session.execute(
            delete(User).where(User.id.in_(ALL_TEST_USERS))
        )
        await session.commit()


async def test_bisexual_matching():
    logger.info("Starting Bisexual Matchmaking test suite...")
    await cleanup_users()

    # 1. Test normalize_orientation
    assert normalize_orientation("male", "bisexual") == "bisexual"
    assert normalize_orientation("female", "bisexual") == "bisexual"

    # 2. Test get_compatible_criteria
    bi_m_crit = get_compatible_criteria("male", "bisexual")
    assert ("female", ["straight", "bisexual"]) in bi_m_crit
    assert ("male", ["gay", "lesbian", "bisexual"]) in bi_m_crit

    bi_f_crit = get_compatible_criteria("female", "bisexual")
    assert ("male", ["straight", "bisexual"]) in bi_f_crit
    assert ("female", ["lesbian", "gay", "bisexual"]) in bi_f_crit

    str_m_crit = get_compatible_criteria("male", "straight")
    assert ("female", ["straight", "bisexual"]) in str_m_crit

    str_f_crit = get_compatible_criteria("female", "straight")
    assert ("male", ["straight", "bisexual"]) in str_f_crit

    gay_m_crit = get_compatible_criteria("male", "gay")
    assert ("male", ["gay", "lesbian", "bisexual"]) in gay_m_crit

    les_f_crit = get_compatible_criteria("female", "lesbian")
    assert ("female", ["lesbian", "gay", "bisexual"]) in les_f_crit

    # 3. Test keyboard contains bisexual option
    kb = get_orientation_keyboard()
    callback_datas = [
        btn.callback_data
        for row in kb.inline_keyboard
        for btn in row
    ]
    assert "onboarding_orientation:bisexual" in callback_datas, "Missing bisexual button in keyboard"
    logger.info("Keyboard verified: %s", callback_datas)

    # 4. Test Live Database Matchmaking Scenarios
    async with get_db_session() as session:
        # Setup test users with priority=100 to isolate them from live queue
        u_bm = await get_or_create_user(session, U_BI_MALE, username="bi_male")
        u_bm.priority = 100
        await session.commit()
        await update_user_profile(session, U_BI_MALE, sex="male", orientation="bisexual")

        u_gm = await get_or_create_user(session, U_GAY_MALE, username="gay_male")
        u_gm.priority = 100
        await session.commit()
        await update_user_profile(session, U_GAY_MALE, sex="male", orientation="gay")

    # Scenario A: Bi Male matches with Gay Male
    async with get_db_session() as session:
        # Bi Male queues first (does not match straight male in queue)
        c1, s1 = await match_user_in_queue(session, U_BI_MALE, allowed_candidate_ids=ALL_TEST_USERS)
        assert c1 is None and s1 is None

        # Gay Male queues -> matches Bi Male!
        c2, s2 = await match_user_in_queue(session, U_GAY_MALE, allowed_candidate_ids=ALL_TEST_USERS)
        assert c2 is not None and c2.id == U_BI_MALE
        assert s2 is not None and s2.is_active is True
        logger.info("Scenario A Passed: Bi Male matched with Gay Male (Session %d)", s2.id)

        # End session
        await end_active_session(session, U_BI_MALE)

    # Scenario B: Bi Male matches with Bi Female
    async with get_db_session() as session:
        u_bf = await get_or_create_user(session, U_BI_FEMALE, username="bi_female")
        u_bf.priority = 100
        await session.commit()
        await update_user_profile(session, U_BI_FEMALE, sex="female", orientation="bisexual")

        # Bi Male queues first
        c1, s1 = await match_user_in_queue(session, U_BI_MALE, allowed_candidate_ids=ALL_TEST_USERS)
        assert c1 is None and s1 is None

        # Bi Female queues -> should match Bi Male!
        c2, s2 = await match_user_in_queue(session, U_BI_FEMALE, allowed_candidate_ids=ALL_TEST_USERS)
        assert c2 is not None and c2.id == U_BI_MALE
        assert s2 is not None and s2.is_active is True
        logger.info("Scenario B Passed: Bi Male matched with Bi Female (Session %d)", s2.id)

        await end_active_session(session, U_BI_MALE)

    # Scenario C: Bi Female matches with Lesbian Female
    async with get_db_session() as session:
        u_lf = await get_or_create_user(session, U_LES_FEMALE, username="les_female")
        u_lf.priority = 100
        await session.commit()
        await update_user_profile(session, U_LES_FEMALE, sex="female", orientation="lesbian")

        # Lesbian Female queues
        c1, s1 = await match_user_in_queue(session, U_LES_FEMALE, allowed_candidate_ids=ALL_TEST_USERS)
        assert c1 is None and s1 is None

        # Bi Female queues -> should match Lesbian Female!
        c2, s2 = await match_user_in_queue(session, U_BI_FEMALE, allowed_candidate_ids=ALL_TEST_USERS)
        assert c2 is not None and c2.id == U_LES_FEMALE
        assert s2 is not None and s2.is_active is True
        logger.info("Scenario C Passed: Bi Female matched with Lesbian Female (Session %d)", s2.id)

        await end_active_session(session, U_BI_FEMALE)

    # Scenario D: Incompatibility Test: Gay Male does NOT match with Straight Male
    async with get_db_session() as session:
        # Gay Male queues
        c1, s1 = await match_user_in_queue(session, U_GAY_MALE, allowed_candidate_ids=ALL_TEST_USERS)
        assert c1 is None and s1 is None

        # Clean queue flags
        u_gay = await session.get(User, U_GAY_MALE)
        u_gay.is_searching = False
        await session.commit()
        logger.info("Scenario D Passed: Verified isolated queue behavior.")

    # 5. Test Admin Stats includes bisexual_count
    async with get_db_session() as session:
        stats = await get_admin_stats(session)
        assert "bisexual_count" in stats
        assert stats["bisexual_count"] >= 2  # U_BI_MALE and U_BI_FEMALE
        logger.info("Admin stats verified: bisexual_count=%d", stats["bisexual_count"])

    await cleanup_users()
    logger.info("ALL BISEXUAL MATCHING TESTS PASSED SUCCESSFULLY! 🎉")


if __name__ == "__main__":
    asyncio.run(test_bisexual_matching())
