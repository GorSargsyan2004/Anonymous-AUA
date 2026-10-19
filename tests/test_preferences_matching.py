"""Automated test suite for user matching preferences, checkbox menus, and mutual compatibility."""

import asyncio
import logging
from sqlalchemy import delete, select, text

from app.database.session import get_db_session, init_db
from app.database.models import User, ChatSession
from app.database.crud import (
    get_or_create_user,
    update_user_profile,
    update_user_preferences,
    end_active_session,
)
from app.services.matcher import (
    get_default_preferences,
    match_user_in_queue,
)
from app.keyboards.inline import get_preferences_keyboard

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("test_preferences")

U1 = 99990001  # Straight Male
U2 = 99990002  # Straight Female
U3 = 99990003  # Gay Male
U4 = 99990004  # Lesbian Female
U5 = 99990005  # Bisexual Male
U6 = 99990006  # Straight Male 2

ALL_TEST_USERS = [U1, U2, U3, U4, U5, U6]


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


async def test_preferences_matching():
    logger.info("Starting Preferences & Mutual Matchmaking test suite...")

    # 1. Test database schema migration & table init
    await init_db()
    logger.info("init_db executed successfully.")

    await cleanup_users()

    # 2. Test get_default_preferences logic
    str_m_sex, str_m_ori = get_default_preferences("male", "straight")
    assert str_m_sex == ["female"], f"Unexpected: {str_m_sex}"
    assert "straight" in str_m_ori and "bisexual" in str_m_ori

    str_f_sex, str_f_ori = get_default_preferences("female", "straight")
    assert str_f_sex == ["male"]
    assert "straight" in str_f_ori and "bisexual" in str_f_ori

    gay_m_sex, gay_m_ori = get_default_preferences("male", "gay")
    assert gay_m_sex == ["male"]
    assert "gay" in gay_m_ori and "bisexual" in gay_m_ori

    les_f_sex, les_f_ori = get_default_preferences("female", "lesbian")
    assert les_f_sex == ["female"]
    assert "lesbian" in les_f_ori and "bisexual" in les_f_ori

    bi_m_sex, bi_m_ori = get_default_preferences("male", "bisexual")
    assert "female" in bi_m_sex and "male" in bi_m_sex
    assert "straight" in bi_m_ori and "bisexual" in bi_m_ori and "gay" in bi_m_ori

    bi_f_sex, bi_f_ori = get_default_preferences("female", "bisexual")
    assert "male" in bi_f_sex and "female" in bi_f_sex
    assert "straight" in bi_f_ori and "bisexual" in bi_f_ori and "lesbian" in bi_f_ori

    logger.info("Default preferences logic verified.")

    # 3. Test Keyboard Checkboxes and NO LGBT flags
    kb = get_preferences_keyboard(selected_sexes=["female"], selected_orientations=["straight", "bisexual"])
    button_texts = [btn.text for row in kb.inline_keyboard for btn in row]
    logger.info("Keyboard buttons generated: %s", button_texts)

    # Verify no LGBT flags anywhere in button text
    forbidden_flags = ["🌈", "🏳️‍🌈", "⚧", "👭", "👬"]
    for text_val in button_texts:
        for flag in forbidden_flags:
            assert flag not in text_val, f"Forbidden flag '{flag}' found in button text '{text_val}'"

    # Verify checkboxes present
    assert any("Male" in t for t in button_texts)
    assert any("Female" in t for t in button_texts)
    assert any("Straight" in t for t in button_texts)
    assert any("Bisexual" in t for t in button_texts)
    assert any("Lesbian" in t for t in button_texts)
    assert any("Gay" in t for t in button_texts)
    assert any("Save Preferences" in t for t in button_texts)

    # Check that selected ones have ✅ and unselected have ⬜
    assert "⬜ Male" in button_texts
    assert "✅ Female" in button_texts
    assert "✅ Straight" in button_texts
    assert "✅ Bisexual" in button_texts
    assert "⬜ Lesbian" in button_texts
    assert "⬜ Gay" in button_texts
    logger.info("Preferences keyboard layout and flags restriction verified.")

    # 4. Test Live Database Setup & Onboarding Defaults (using priority=100 to strictly isolate test pairs)
    async with get_db_session() as session:
        # Create users
        u1 = await get_or_create_user(session, U1, username="str_male_1")
        u1.priority = 100
        await session.commit()
        await update_user_profile(session, U1, sex="male", orientation="straight")

        u2 = await get_or_create_user(session, U2, username="str_female")
        u2.priority = 100
        await session.commit()
        await update_user_profile(session, U2, sex="female", orientation="straight")

        u3 = await get_or_create_user(session, U3, username="gay_male")
        u3.priority = 100
        await session.commit()
        await update_user_profile(session, U3, sex="male", orientation="gay")

        u4 = await get_or_create_user(session, U4, username="les_female")
        u4.priority = 100
        await session.commit()
        await update_user_profile(session, U4, sex="female", orientation="lesbian")

        u6 = await get_or_create_user(session, U6, username="str_male_2")
        u6.priority = 100
        await session.commit()
        await update_user_profile(session, U6, sex="male", orientation="straight")

    # 5. Scenario A: Straight Male & Straight Female match by default
    async with get_db_session() as session:
        c1, s1 = await match_user_in_queue(session, U1, allowed_candidate_ids=ALL_TEST_USERS)
        assert c1 is None and s1 is None, "First queued user should not immediately match"

        c2, s2 = await match_user_in_queue(session, U2, allowed_candidate_ids=ALL_TEST_USERS)
        assert c2 is not None and c2.id == U1
        assert s2 is not None and s2.is_active is True
        logger.info("Scenario A Passed: Straight Male and Straight Female connected (Session %d)", s2.id)

        await end_active_session(session, U1)

    # 6. Scenario B: Incompatible defaults do NOT match
    # Straight Male 1 (U1) queues. Straight Male 2 (U6) queues.
    # By default, both only want females. They must NOT match!
    async with get_db_session() as session:
        c1, s1 = await match_user_in_queue(session, U1, allowed_candidate_ids=ALL_TEST_USERS)
        assert c1 is None and s1 is None

        c2, s2 = await match_user_in_queue(session, U6, allowed_candidate_ids=ALL_TEST_USERS)
        assert c2 is None and s2 is None, "Straight males should not match each other by default!"
        logger.info("Scenario B Passed: Two straight males in queue did not match each other.")

        # Clean queue
        u1 = await session.get(User, U1)
        u1.is_searching = False
        u6 = await session.get(User, U6)
        u6.is_searching = False
        await session.commit()

    # 7. Scenario C: Custom Preferences - Straight Male wants to talk with anyone (Male, Female)
    # Gay Male wants to talk with Male (Straight, Bisexual, Gay)
    async with get_db_session() as session:
        # U1 (Straight male) customizes preferences to allow talking to Male
        await update_user_preferences(
            session,
            user_id=U1,
            target_sexes=["male", "female"],
            target_orientations=["straight", "bisexual", "gay", "lesbian"],
        )
        # U3 (Gay male) customizes preferences to allow talking to straight male friends
        await update_user_preferences(
            session,
            user_id=U3,
            target_sexes=["male"],
            target_orientations=["straight", "bisexual", "gay"],
        )

        # U1 queues
        c1, s1 = await match_user_in_queue(session, U1, allowed_candidate_ids=ALL_TEST_USERS)
        assert c1 is None and s1 is None

        # U3 queues -> Should now match U1!
        c2, s2 = await match_user_in_queue(session, U3, allowed_candidate_ids=ALL_TEST_USERS)
        assert c2 is not None and c2.id == U1
        assert s2 is not None and s2.is_active is True
        logger.info("Scenario C Passed: Custom preferences matched U1 and U3 (Session %d)", s2.id)

        await end_active_session(session, U1)

    # 8. Scenario D: Asymmetric Preferences - Only one wants to talk, the other doesn't
    async with get_db_session() as session:
        # U4 (Lesbian female) only wants females
        await update_user_preferences(
            session,
            user_id=U4,
            target_sexes=["female"],
            target_orientations=["lesbian", "bisexual"],
        )
        # U1 (Straight male) wants female and male
        await update_user_preferences(
            session,
            user_id=U1,
            target_sexes=["female", "male"],
            target_orientations=["straight", "bisexual", "lesbian"],
        )

        # U4 queues
        c1, s1 = await match_user_in_queue(session, U4, allowed_candidate_ids=ALL_TEST_USERS)
        assert c1 is None and s1 is None

        # U1 queues -> Even though U1 accepts females (including lesbians), U4 does NOT accept males!
        c2, s2 = await match_user_in_queue(session, U1, allowed_candidate_ids=ALL_TEST_USERS)
        assert c2 is None and s2 is None, "Asymmetric preference should NOT produce a match!"
        logger.info("Scenario D Passed: Asymmetric preferences correctly prevented one-way match.")

        u4 = await session.get(User, U4)
        u4.is_searching = False
        u1 = await session.get(User, U1)
        u1.is_searching = False
        await session.commit()

    # 9. Scenario E: Both customize preferences across orientations
    async with get_db_session() as session:
        # U4 (Lesbian female) wants to chat with Gay Male (U3)
        await update_user_preferences(
            session,
            user_id=U4,
            target_sexes=["male"],
            target_orientations=["gay"],
        )
        # U3 (Gay male) wants to chat with Lesbian Female (U4)
        await update_user_preferences(
            session,
            user_id=U3,
            target_sexes=["female"],
            target_orientations=["lesbian"],
        )

        c1, s1 = await match_user_in_queue(session, U4, allowed_candidate_ids=ALL_TEST_USERS)
        assert c1 is None and s1 is None

        c2, s2 = await match_user_in_queue(session, U3, allowed_candidate_ids=ALL_TEST_USERS)
        assert c2 is not None and c2.id == U4
        assert s2 is not None and s2.is_active is True
        logger.info("Scenario E Passed: Mutual custom preferences connected Lesbian Female and Gay Male (Session %d)", s2.id)

        await end_active_session(session, U4)

    await cleanup_users()
    logger.info("ALL PREFERENCES MATCHING TESTS PASSED SUCCESSFULLY! 🎉")


if __name__ == "__main__":
    asyncio.run(test_preferences_matching())
