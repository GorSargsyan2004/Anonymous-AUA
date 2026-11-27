"""Comprehensive automated tests for Telegram Anonymous Group Chats and Multi-Party AI Moderation."""

from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.exceptions import TelegramForbiddenError
from sqlalchemy import delete, select

from app.database.crud import (
    ban_and_kick_user_from_group,
    count_active_group_members,
    create_group_chat_with_creator,
    get_group_member_record,
    get_or_create_user,
    get_recent_group_messages,
    get_reportable_group_participants,
    get_user,
    get_user_active_group_member,
    is_user_banned_from_group,
    join_group_chat_atomic,
    leave_group_chat,
    list_eligible_active_groups,
    record_group_chat_message,
    validate_creator_anti_lurker,
)
from app.database.models import (
    GROUP_CHAT_MAX_CAPACITY,
    GROUP_CHAT_MIN_CAPACITY,
    GroupChat,
    GroupChatBan,
    GroupChatMember,
    GroupChatMessage,
    GroupModerationLog,
    User,
)
from app.database.session import get_db_session, init_db
from app.handlers.group_chats import (
    _build_group_details_text,
    execute_leave_group_for_user,
    handle_members_command,
)
from app.handlers.matchmaking import perform_search
from app.keyboards.inline import (
    get_group_browser_keyboard,
    get_group_details_keyboard,
    get_group_report_targets_keyboard,
    get_search_mode_keyboard,
)
from app.services.ban_manager import clear_bans_for_testing, is_user_banned
from app.services.group_chat_service import (
    CURATED_RANDOM_NICKNAMES,
    check_and_close_inactive_group_chats,
    format_group_media_caption,
    format_group_text_message,
    generate_unique_random_nickname,
    group_reply_buffer,
    group_slow_mode,
    relay_group_edited_message,
    relay_group_message,
    relay_group_message_reaction,
    validate_group_description,
    validate_group_name,
    validate_group_nickname,
)
from app.services.group_moderator import (
    GroupModerationAction,
    GroupModerationVerdict,
    build_group_transcript_and_slots,
    group_ai_moderator,
)


class TestGroupChatsAndModeration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await init_db()
        group_reply_buffer.clear_all_for_testing()
        group_slow_mode.clear_for_testing()
        group_ai_moderator.clear_for_testing()
        clear_bans_for_testing()

        self.test_user_ids = [8877001 + i for i in range(15)]
        async with get_db_session() as session:
            # Clean any leftover test rows
            await session.execute(delete(GroupChat).where(GroupChat.creator_id.in_(self.test_user_ids)))
            await session.execute(delete(User).where(User.id.in_(self.test_user_ids)))
            await session.commit()

            # Create diverse test users
            # u0: male, straight, verified
            u0 = User(
                id=self.test_user_ids[0],
                username="u0_male_verif",
                sex="male",
                orientation="straight",
                is_verified=True,
                priority=50,
            )
            # u1: female, straight, verified
            u1 = User(
                id=self.test_user_ids[1],
                username="u1_female_verif",
                sex="female",
                orientation="straight",
                is_verified=True,
                priority=50,
            )
            # u2: male, bisexual, unverified
            u2 = User(
                id=self.test_user_ids[2],
                username="u2_male_unverif",
                sex="male",
                orientation="bisexual",
                is_verified=False,
                priority=50,
            )
            # u3: female, lesbian, unverified
            u3 = User(
                id=self.test_user_ids[3],
                username="u3_female_lesbian",
                sex="female",
                orientation="lesbian",
                is_verified=False,
                priority=50,
            )
            session.add_all([u0, u1, u2, u3])
            for idx in range(4, 15):
                session.add(
                    User(
                        id=self.test_user_ids[idx],
                        username=f"u{idx}",
                        sex="male" if idx % 2 == 0 else "female",
                        orientation="straight",
                        is_verified=True,
                        priority=50,
                    )
                )
            await session.commit()

    async def asyncTearDown(self):
        group_reply_buffer.clear_all_for_testing()
        group_slow_mode.clear_for_testing()
        group_ai_moderator.clear_for_testing()
        clear_bans_for_testing()
        async with get_db_session() as session:
            await session.execute(delete(GroupChat).where(GroupChat.creator_id.in_(self.test_user_ids)))
            await session.execute(delete(User).where(User.id.in_(self.test_user_ids)))
            await session.commit()
        from app.database.session import engine
        await engine.dispose()

    # -------------------------------------------------------------------------
    # 1. Pagination Math, Keyboards & Eligibility Filtering
    # -------------------------------------------------------------------------

    async def test_pagination_and_eligibility_filtering(self):
        async with get_db_session() as session:
            u1_init = await get_user(session, self.test_user_ids[1])
            u2_init = await get_user(session, self.test_user_ids[2])
            _, base_u1_cnt, _ = await list_eligible_active_groups(session, u1_init, page=1, page_size=5)
            _, base_u2_cnt, _ = await list_eligible_active_groups(session, u2_init, page=1, page_size=5)

            # Create 11 open groups by distinct creators + 1 female-only + 1 verified-only + 1 banned group
            open_groups = []
            for i in range(4, 15):
                creator = await get_user(session, self.test_user_ids[i])
                grp, _, err = await create_group_chat_with_creator(
                    session=session,
                    creator=creator,
                    name=f"Campus Talk {i}",
                    description=f"Desc {i}" if i % 2 == 0 else None,
                    max_participants=10,
                    allowed_gender=None,
                    allowed_orientation=None,
                    verified_only=False,
                    creator_nickname=f"Creator{i}",
                )
                self.assertIsNotNone(grp, err)
                open_groups.append(grp)

            # Create a Female-only group by u1 (female, verified)
            u1 = await get_user(session, self.test_user_ids[1])
            female_grp, _, err = await create_group_chat_with_creator(
                session=session,
                creator=u1,
                name="Girls Study Lounge",
                description="Female students only",
                max_participants=8,
                allowed_gender="female",
                allowed_orientation=None,
                verified_only=False,
                creator_nickname="JadeOwl",
            )
            self.assertIsNotNone(female_grp, err)

            # Create a Verified-only group by u0 (male, verified)
            u0 = await get_user(session, self.test_user_ids[0])
            verif_grp, _, err = await create_group_chat_with_creator(
                session=session,
                creator=u0,
                name="Verified AUA Room",
                description=None,
                max_participants=12,
                allowed_gender=None,
                allowed_orientation=None,
                verified_only=True,
                creator_nickname="BlueFox",
            )
            self.assertIsNotNone(verif_grp, err)

            # Ban u2 from open_groups[0]
            await ban_and_kick_user_from_group(
                session, open_groups[0].id, self.test_user_ids[2], "test ban"
            )

            # Query eligible groups for u2 (male, unverified, banned from open_groups[0]):
            # Should see base_u2_cnt + 10 open groups (11 - 1 banned), and NOT female_grp or verif_grp
            u2 = await get_user(session, self.test_user_ids[2])
            all_u2_items, total_cnt, total_pages = await list_eligible_active_groups(
                session, u2, page=1, page_size=50
            )
            self.assertEqual(total_cnt, base_u2_cnt + 10)
            page1_items, _, total_pages = await list_eligible_active_groups(
                session, u2, page=1, page_size=5
            )
            self.assertEqual(len(page1_items), 5)
            returned_ids = {g.id for g, _ in all_u2_items}
            self.assertNotIn(female_grp.id, returned_ids)
            self.assertNotIn(verif_grp.id, returned_ids)
            self.assertNotIn(open_groups[0].id, returned_ids)

            # Query eligible groups for u1 (female, straight, verified):
            # Should see base_u1_cnt + all 11 open groups + female_grp + verif_grp = base_u1_cnt + 13 groups
            p1, tot13, pages3 = await list_eligible_active_groups(session, u1, page=1, page_size=5)
            self.assertEqual(tot13, base_u1_cnt + 13)
            self.assertGreaterEqual(pages3, 3)
            self.assertEqual(len(p1), 5)

            # Verify keyboard format: button text ONLY "Name (count/max)", [<] [1/3] [>], [+ Create Group] [🔄 Refresh]
            kb = get_group_browser_keyboard(p1, page=1, total_pages=3)
            rows = kb.inline_keyboard
            self.assertEqual(len(rows), 5 + 1 + 1)
            first_btn = rows[0][0]
            self.assertRegex(first_btn.text, r"^.+\(\d+/\d+\)$")
            nav_row = rows[5]
            self.assertEqual([b.text for b in nav_row], ["<", "1/3", ">"])
            self.assertEqual(nav_row[0].callback_data, "grp_noop")
            self.assertEqual(nav_row[1].callback_data, "grp_noop")
            self.assertEqual(nav_row[2].callback_data, "grp_page:2")

            # Verify details keyboard preserves exact page number on [« Back]
            details_kb = get_group_details_keyboard(female_grp.id, current_count=1, max_participants=8, back_page=2)
            self.assertEqual(details_kb.inline_keyboard[0][0].text, "Join Group")
            self.assertEqual(details_kb.inline_keyboard[1][0].text, "« Back")
            self.assertEqual(details_kb.inline_keyboard[1][0].callback_data, "grp_page:2")

            # Full group details keyboard
            full_kb = get_group_details_keyboard(female_grp.id, current_count=8, max_participants=8, back_page=3)
            self.assertEqual(full_kb.inline_keyboard[0][0].text, "Group Full")
            self.assertEqual(full_kb.inline_keyboard[1][0].callback_data, "grp_page:3")

            # Details text omits description/limitations when unset, includes them when set
            txt_with = _build_group_details_text(female_grp, 1)
            self.assertIn("Description:", txt_with)
            self.assertIn("Gender: Female only", txt_with)
            txt_without = _build_group_details_text(open_groups[1], 1)  # odd index -> description=None, no lims
            self.assertNotIn("Description:", txt_without)
            self.assertNotIn("Limitations:", txt_without)

            # Search menu keyboard includes Group chats button
            search_kb = get_search_mode_keyboard("preferences")
            all_cb = [b.callback_data for row in search_kb.inline_keyboard for b in row]
            self.assertIn("search_open_groups", all_cb)

    # -------------------------------------------------------------------------
    # 2. Anti-Lurker Validation, Capacity Bounds & Creation Anti-Abuse
    # -------------------------------------------------------------------------

    async def test_anti_lurker_and_capacity_bounds(self):
        async with get_db_session() as session:
            u2 = await get_user(session, self.test_user_ids[2])  # male, bisexual, unverified

            # Unverified user cannot enable verified_only
            ok, err = validate_creator_anti_lurker(u2, None, None, verified_only=True)
            self.assertFalse(ok)
            self.assertIn("Anti-Lurker", err)

            # Male user cannot create Female-only group
            ok, err = validate_creator_anti_lurker(u2, allowed_gender="female", allowed_orientation=None, verified_only=False)
            self.assertFalse(ok)

            # Bisexual user cannot create Straight-only group
            ok, err = validate_creator_anti_lurker(u2, allowed_gender="male", allowed_orientation="straight", verified_only=False)
            self.assertFalse(ok)

            # Matching restrictions succeed
            ok, _ = validate_creator_anti_lurker(u2, allowed_gender="male", allowed_orientation="bisexual", verified_only=False)
            self.assertTrue(ok)

            # Capacity bounds (3..15)
            grp_low, _, err_low = await create_group_chat_with_creator(
                session, u2, "Too Small", None, 2, None, None, False, "Wolf"
            )
            self.assertIsNone(grp_low)
            self.assertIn("between 3 and 15", err_low)

            grp_high, _, err_high = await create_group_chat_with_creator(
                session, u2, "Too Big", None, 16, None, None, False, "Wolf"
            )
            self.assertIsNone(grp_high)
            self.assertIn("between 3 and 15", err_high)

            # Valid creation
            grp_ok, _, err_ok = await create_group_chat_with_creator(
                session, u2, "Valid Group", "Clean desc", 5, "male", None, False, "SilverWolf"
            )
            self.assertIsNotNone(grp_ok, err_ok)

            # Enforce 1 active created group per user even if creator leaves their group
            await leave_group_chat(session, user_id=u2.id, group_id=grp_ok.id)
            # Re-open grp_ok with another member so it remains active
            grp_ok.is_active = True
            await session.commit()
            grp_dup, _, err_dup = await create_group_chat_with_creator(
                session, u2, "Second Group", None, 5, None, None, False, "GoldFox"
            )
            self.assertIsNone(grp_dup)
            self.assertIn("Only 1 active created group", err_dup)

    # -------------------------------------------------------------------------
    # 3. Session Exclusivity, Sticky Nicknames & Nickname Hygiene
    # -------------------------------------------------------------------------

    async def test_session_exclusivity_and_sticky_nicknames(self):
        # Curated random nickname pool has >= 80 names
        self.assertGreaterEqual(len(CURATED_RANDOM_NICKNAMES), 80)

        # Reserved names rejected
        for bad_nick in ("Admin", "moderator", "MOD", "System_Bot", "aua_hub", "Support", "staff"):
            ok, _, _ = validate_group_nickname(bad_nick)
            self.assertFalse(ok, f"Expected reserved nickname {bad_nick!r} to be rejected")

        # HTML characters / invalid symbols rejected in nickname validator, and formatters escape HTML
        ok, _, _ = validate_group_nickname("<b>Hacker</b>")
        self.assertFalse(ok)
        escaped_msg = format_group_text_message("Fox<script>", "Hello & welcome <3")
        self.assertIn("Fox&lt;script&gt;", escaped_msg)
        self.assertIn("Hello &amp; welcome &lt;3", escaped_msg)

        async with get_db_session() as session:
            u0 = await get_user(session, self.test_user_ids[0])
            u1 = await get_user(session, self.test_user_ids[1])
            u2 = await get_user(session, self.test_user_ids[2])

            grp, _, _ = await create_group_chat_with_creator(
                session, u0, "CS Study Room", None, 5, None, None, False, "BlueFox"
            )

            # u0 is now in an active group chat -> 1-on-1 /search must be blocked
            mock_msg = MagicMock()
            mock_msg.from_user = SimpleNamespace(id=u0.id, username=u0.username)
            mock_msg.answer = AsyncMock()
            mock_bot = MagicMock()
            await perform_search(user_id=u0.id, bot=mock_bot, message=mock_msg)
            mock_msg.answer.assert_awaited()
            sent_text = mock_msg.answer.call_args[0][0]
            self.assertIn("/leave", sent_text)

            # u1 is in a 1-on-1 chat (current_partner_id set) -> joining group must be blocked
            u1.current_partner_id = 999999
            await session.commit()
            _, mem_fail, _, join_err = await join_group_chat_atomic(session, u1, grp.id, "CrimsonOwl")
            self.assertIsNone(mem_fail)
            self.assertIn("/stop", join_err)

            # Clear u1's 1-on-1 state -> u1 joins group as CrimsonOwl
            u1.current_partner_id = None
            await session.commit()
            _, mem_u1, cnt, join_err = await join_group_chat_atomic(session, u1, grp.id, "CrimsonOwl")
            self.assertIsNotNone(mem_u1, join_err)
            self.assertEqual(cnt, 2)

            # u1 leaves group -> row stays with is_active=False (sticky nickname)
            leave_res = await leave_group_chat(session, user_id=u1.id, group_id=grp.id)
            self.assertIsNotNone(leave_res)
            _, left_member, rem_cnt, closed = leave_res
            self.assertFalse(left_member.is_active)
            self.assertIsNotNone(left_member.left_at)
            self.assertEqual(rem_cnt, 1)
            self.assertFalse(closed)

            # u2 tries to hijack "crimsonowl" (case-insensitive) while u1 is gone -> must be rejected!
            _, mem_hijack, _, hijack_err = await join_group_chat_atomic(session, u2, grp.id, "crimsonowl")
            self.assertIsNone(mem_hijack)
            self.assertIn("already taken", hijack_err)

            # u1 rejoins without specifying a nickname -> automatically restores "CrimsonOwl"!
            _, mem_restored, cnt_after, err_restore = await join_group_chat_atomic(
                session, u1, grp.id, requested_nickname=None
            )
            self.assertEqual(err_restore, "")
            self.assertIsNotNone(mem_restored)
            self.assertTrue(mem_restored.is_active)
            self.assertEqual(mem_restored.nickname, "CrimsonOwl")
            self.assertEqual(cnt_after, 2)

    # -------------------------------------------------------------------------
    # 4. Relay Engine: Media Attribution, Threaded Replies, Albums & 403 Cleanup
    # -------------------------------------------------------------------------

    async def test_relay_engine_media_attribution_replies_and_403_cleanup(self):
        async with get_db_session() as session:
            u0 = await get_user(session, self.test_user_ids[0])
            u1 = await get_user(session, self.test_user_ids[1])
            u2 = await get_user(session, self.test_user_ids[2])

            grp, mem0, _ = await create_group_chat_with_creator(
                session, u0, "Relay Test Group", None, 6, None, None, False, "BlueFox"
            )
            _, mem1, _, _ = await join_group_chat_atomic(session, u1, grp.id, "CrimsonOwl")
            _, mem2, _, _ = await join_group_chat_atomic(session, u2, grp.id, "SilverWolf")

            bot = MagicMock()
            bot.send_message = AsyncMock(side_effect=lambda **kw: SimpleNamespace(message_id=500 + kw["chat_id"] % 100))
            bot.send_photo = AsyncMock(side_effect=lambda **kw: SimpleNamespace(message_id=600 + kw["chat_id"] % 100))
            bot.send_sticker = AsyncMock(side_effect=lambda **kw: SimpleNamespace(message_id=700 + kw["chat_id"] % 100))

            # A. Text message relay & threaded reply mapping
            msg1 = SimpleNamespace(
                message_id=101,
                text="Hello everyone!",
                caption=None,
                photo=None,
                video=None,
                animation=None,
                sticker=None,
                voice=None,
                video_note=None,
                audio=None,
                document=None,
                media_group_id=None,
                reply_to_message=None,
                answer=AsyncMock(),
            )
            ok, reason = await relay_group_message(bot, session, grp, mem0, msg1)
            self.assertTrue(ok, reason)
            self.assertEqual(bot.send_message.await_count, 2)  # sent to u1 and u2

            # u1 replies to the relayed message in u1's chat (message_id = 500 + u1.id % 100)
            u1_received_msg_id = 500 + (u1.id % 100)
            u2_received_msg_id = 500 + (u2.id % 100)
            group_slow_mode.clear_for_testing()
            bot.send_message.reset_mock()

            reply_msg = SimpleNamespace(
                message_id=202,
                text="Replying to BlueFox!",
                caption=None,
                photo=None,
                video=None,
                animation=None,
                sticker=None,
                voice=None,
                video_note=None,
                audio=None,
                document=None,
                media_group_id=None,
                reply_to_message=SimpleNamespace(message_id=u1_received_msg_id),
                answer=AsyncMock(),
            )
            ok, _ = await relay_group_message(bot, session, grp, mem1, reply_msg)
            self.assertTrue(ok)
            # Check that u0 got reply_to_message_id=101 (u0's original msg) and u2 got reply_to_message_id=u2_received_msg_id
            calls_by_chat = {c.kwargs["chat_id"]: c.kwargs for c in bot.send_message.await_args_list}
            self.assertEqual(calls_by_chat[u0.id]["reply_to_message_id"], 101)
            self.assertEqual(calls_by_chat[u2.id]["reply_to_message_id"], u2_received_msg_id)

            # B. Captionable media (Photo) embeds nickname in caption
            group_slow_mode.clear_for_testing()
            photo_msg = SimpleNamespace(
                message_id=303,
                text=None,
                caption="Check out this sunset",
                photo=[SimpleNamespace(file_id="photo_fid_123")],
                video=None,
                animation=None,
                sticker=None,
                voice=None,
                video_note=None,
                audio=None,
                document=None,
                media_group_id=None,
                reply_to_message=None,
                answer=AsyncMock(),
            )
            ok, _ = await relay_group_message(bot, session, grp, mem0, photo_msg)
            self.assertTrue(ok)
            self.assertEqual(bot.send_photo.await_count, 2)
            photo_call_kwargs = bot.send_photo.await_args_list[0].kwargs
            self.assertIn("<b>BlueFox</b>\nCheck out this sunset", photo_call_kwargs["caption"])

            # C. Non-captionable media (Sticker) sends sticker then immediate follow-up reply with nickname
            group_slow_mode.clear_for_testing()
            bot.send_message.reset_mock()
            sticker_msg = SimpleNamespace(
                message_id=404,
                text=None,
                caption=None,
                photo=None,
                video=None,
                animation=None,
                sticker=SimpleNamespace(file_id="stk_fid_999", emoji="🦊"),
                voice=None,
                video_note=None,
                audio=None,
                document=None,
                media_group_id=None,
                reply_to_message=None,
                answer=AsyncMock(),
            )
            ok, _ = await relay_group_message(bot, session, grp, mem0, sticker_msg)
            self.assertTrue(ok)
            self.assertEqual(bot.send_sticker.await_count, 2)
            self.assertEqual(bot.send_message.await_count, 2)
            followup_kwargs = bot.send_message.await_args_list[0].kwargs
            self.assertIn("👤 <b>BlueFox</b>", followup_kwargs["text"])
            expected_sticker_msg_id = 700 + (followup_kwargs["chat_id"] % 100)
            self.assertEqual(followup_kwargs["reply_to_message_id"], expected_sticker_msg_id)

            # D. Album rejection (media_group_id): notifies once per media_group_id
            album_msg1 = SimpleNamespace(
                message_id=501,
                media_group_id="alb_777",
                photo=[SimpleNamespace(file_id="p1")],
                answer=AsyncMock(),
            )
            album_msg2 = SimpleNamespace(
                message_id=502,
                media_group_id="alb_777",
                photo=[SimpleNamespace(file_id="p2")],
                answer=AsyncMock(),
            )
            ok1, r1 = await relay_group_message(bot, session, grp, mem0, album_msg1)
            ok2, r2 = await relay_group_message(bot, session, grp, mem0, album_msg2)
            self.assertFalse(ok1)
            self.assertFalse(ok2)
            self.assertEqual(r1, "album_rejected")
            album_msg1.answer.assert_awaited_once()
            album_msg2.answer.assert_not_awaited()

            # E. 403 Forbidden dead-member auto-deactivation
            group_slow_mode.clear_for_testing()

            async def _send_msg_with_403(**kw):
                if kw["chat_id"] == u2.id:
                    raise TelegramForbiddenError(method=MagicMock(), message="Forbidden: bot was blocked by the user")
                return SimpleNamespace(message_id=888)

            bot.send_message = AsyncMock(side_effect=_send_msg_with_403)
            msg_after = SimpleNamespace(
                message_id=601,
                text="Testing 403 cleanup",
                caption=None,
                photo=None,
                video=None,
                animation=None,
                sticker=None,
                voice=None,
                video_note=None,
                audio=None,
                document=None,
                media_group_id=None,
                reply_to_message=None,
                answer=AsyncMock(),
            )
            await relay_group_message(bot, session, grp, mem0, msg_after)
            u2_rec = await get_group_member_record(session, grp.id, u2.id)
            self.assertFalse(u2_rec.is_active)
            self.assertIsNotNone(u2_rec.left_at)

            # Re-join u2 so all 3 members (u0, u1, u2) are active for edit, reaction, typing, and Link Shield tests
            _, mem2, _, _ = await join_group_chat_atomic(session, u2, grp.id, "SilverWolf")
            group_slow_mode.clear_for_testing()

            bot.send_message = AsyncMock(side_effect=lambda **kw: SimpleNamespace(message_id=500 + kw["chat_id"] % 100))
            bot.send_photo = AsyncMock(side_effect=lambda **kw: SimpleNamespace(message_id=600 + kw["chat_id"] % 100))
            bot.send_chat_action = AsyncMock()
            bot.edit_message_text = AsyncMock()
            bot.edit_message_caption = AsyncMock()
            bot.set_message_reaction = AsyncMock()

            # F. Typing / Chat Action Indicator (respects user.show_typing)
            u0.show_typing = True
            await session.commit()
            msg_typing = SimpleNamespace(
                message_id=701,
                text="Initial message before edit",
                caption=None,
                photo=None,
                video=None,
                animation=None,
                sticker=None,
                voice=None,
                video_note=None,
                audio=None,
                document=None,
                media_group_id=None,
                reply_to_message=None,
                answer=AsyncMock(),
            )
            ok, _ = await relay_group_message(bot, session, grp, mem0, msg_typing)
            self.assertTrue(ok)
            self.assertEqual(bot.send_chat_action.await_count, 2)
            action_chats = {c.kwargs["chat_id"]: c.kwargs["action"] for c in bot.send_chat_action.await_args_list}
            self.assertEqual(action_chats, {u1.id: "typing", u2.id: "typing"})

            # When show_typing is False, send_chat_action is suppressed
            bot.send_chat_action.reset_mock()
            group_slow_mode.clear_for_testing()
            u0.show_typing = False
            await session.commit()
            photo_for_edit = SimpleNamespace(
                message_id=702,
                text=None,
                caption="Original photo caption",
                photo=[SimpleNamespace(file_id="photo_edit_fid")],
                video=None,
                animation=None,
                sticker=None,
                voice=None,
                video_note=None,
                audio=None,
                document=None,
                media_group_id=None,
                reply_to_message=None,
                answer=AsyncMock(),
            )
            ok, _ = await relay_group_message(bot, session, grp, mem0, photo_for_edit)
            self.assertTrue(ok)
            self.assertEqual(bot.send_chat_action.await_count, 0)
            u0.show_typing = True
            await session.commit()

            # G. Editing text message & photo caption updates all recipients' copies and rolling DB transcript
            edited_text_msg = SimpleNamespace(
                message_id=701,
                text="Updated message after edit!",
                caption=None,
                photo=None,
                video=None,
                animation=None,
                sticker=None,
                voice=None,
                video_note=None,
                audio=None,
                document=None,
                answer=AsyncMock(),
            )
            ok_edit, err_edit = await relay_group_edited_message(bot, session, grp, mem0, edited_text_msg)
            self.assertTrue(ok_edit, err_edit)
            self.assertEqual(bot.edit_message_text.await_count, 2)
            edit_calls = {c.kwargs["chat_id"]: c.kwargs for c in bot.edit_message_text.await_args_list}
            self.assertEqual(edit_calls[u1.id]["message_id"], 500 + (u1.id % 100))
            self.assertEqual(edit_calls[u2.id]["message_id"], 500 + (u2.id % 100))
            self.assertIn("<b>BlueFox:</b>\nUpdated message after edit!", edit_calls[u1.id]["text"])

            edited_photo_msg = SimpleNamespace(
                message_id=702,
                text=None,
                caption="Updated photo caption!",
                photo=[SimpleNamespace(file_id="photo_edit_fid")],
                video=None,
                animation=None,
                sticker=None,
                voice=None,
                video_note=None,
                audio=None,
                document=None,
                answer=AsyncMock(),
            )
            ok_cap_edit, err_cap_edit = await relay_group_edited_message(bot, session, grp, mem0, edited_photo_msg)
            self.assertTrue(ok_cap_edit, err_cap_edit)
            self.assertEqual(bot.edit_message_caption.await_count, 2)
            cap_calls = {c.kwargs["chat_id"]: c.kwargs for c in bot.edit_message_caption.await_args_list}
            self.assertEqual(cap_calls[u1.id]["message_id"], 600 + (u1.id % 100))
            self.assertIn("<b>BlueFox</b>\nUpdated photo caption!", cap_calls[u1.id]["caption"])

            # Verify rolling DB transcript reflects the edited text & caption
            recent_db_msgs = await get_recent_group_messages(session, grp.id)
            db_texts = [m.text_content for m in recent_db_msgs]
            self.assertIn("Updated message after edit!", db_texts)
            self.assertIn('[Photo, caption: "Updated photo caption!"]', db_texts)

            # H. Emoji Reactions: u1 reacts to u0's message (in u1's chat: 500 + u1.id % 100) -> relayed to u0 (701) and u2 (500 + u2.id % 100)
            reaction_event = SimpleNamespace(
                message_id=500 + (u1.id % 100),
                user=SimpleNamespace(id=u1.id),
                new_reaction=[SimpleNamespace(type="emoji", emoji="🔥")],
            )
            ok_rx, err_rx = await relay_group_message_reaction(bot, session, grp, mem1, reaction_event)
            self.assertTrue(ok_rx, err_rx)
            self.assertEqual(bot.set_message_reaction.await_count, 2)
            rx_calls = {c.kwargs["chat_id"]: c.kwargs for c in bot.set_message_reaction.await_args_list}
            self.assertEqual(rx_calls[u0.id]["message_id"], 701)
            self.assertEqual(rx_calls[u2.id]["message_id"], 500 + (u2.id % 100))
            self.assertEqual(rx_calls[u0.id]["reaction"][0].emoji, "🔥")

            # I. Link Shield blocks IP-logger URLs in group messages and group message edits
            group_slow_mode.clear_for_testing()
            malicious_msg = SimpleNamespace(
                message_id=703,
                text="Click my link https://grabify.link/track123 right now",
                caption=None,
                entities=None,
                caption_entities=None,
                photo=None,
                video=None,
                animation=None,
                sticker=None,
                voice=None,
                video_note=None,
                audio=None,
                document=None,
                media_group_id=None,
                reply_to_message=None,
                answer=AsyncMock(),
                reply=AsyncMock(),
            )
            ok_mal, reason_mal = await relay_group_message(bot, session, grp, mem0, malicious_msg)
            self.assertFalse(ok_mal)
            self.assertEqual(reason_mal, "link_blocked")
            malicious_msg.reply.assert_awaited_once()

    # -------------------------------------------------------------------------
    # 5. Multi-Party AI Moderation: 100-Msg Rolling Window, (left) Speakers & Penalties
    # -------------------------------------------------------------------------

    async def test_multi_party_ai_moderation_and_rolling_100_window(self):
        async with get_db_session() as session:
            u0 = await get_user(session, self.test_user_ids[0])  # Reporter (BlueFox)
            u1 = await get_user(session, self.test_user_ids[1])  # Reported hit-and-run user (CrimsonOwl)
            u2 = await get_user(session, self.test_user_ids[2])  # Third participant who also broke rules (SilverWolf)
            u3 = await get_user(session, self.test_user_ids[3])  # Fourth participant who gets perm_ban (JadePanda)

            grp, _, _ = await create_group_chat_with_creator(
                session, u0, "Moderation Test Room", None, 10, None, None, False, "BlueFox"
            )
            await join_group_chat_atomic(session, u1, grp.id, "CrimsonOwl")
            await join_group_chat_atomic(session, u2, grp.id, "SilverWolf")
            await join_group_chat_atomic(session, u3, grp.id, "JadePanda")

            # Insert 105 messages to verify rolling 100-message pruning
            for i in range(105):
                await record_group_chat_message(
                    session=session,
                    group_id=grp.id,
                    sender_user_id=u0.id,
                    sender_nickname="BlueFox",
                    msg_type="text",
                    text_content=f"Normal message #{i}",
                )

            await record_group_chat_message(
                session, grp.id, u1.id, "CrimsonOwl", "text", "You are an idiot and bitch"
            )
            await record_group_chat_message(
                session, grp.id, u2.id, "SilverWolf", "text", "shut up stupid spam"
            )
            await record_group_chat_message(
                session, grp.id, u3.id, "JadePanda", "text", "I will kill you and doxx your phone number is 123"
            )

            recent_msgs = await get_recent_group_messages(session, grp.id, limit=100)
            self.assertEqual(len(recent_msgs), 100)

            # Hit-and-run: u1 leaves the group after sending abusive messages
            await leave_group_chat(session, user_id=u1.id, group_id=grp.id)

            # Reportable participants for u0 must include u1 with is_active=False -> labeled "(left)"
            reportable = await get_reportable_group_participants(session, grp.id, reporter_user_id=u0.id)
            by_uid = {p["user_id"]: p for p in reportable}
            self.assertIn(u1.id, by_uid)
            self.assertFalse(by_uid[u1.id]["is_active"])

            rep_kb = get_group_report_targets_keyboard(reportable, grp.id)
            btn_texts = [b.text for row in rep_kb.inline_keyboard for b in row]
            self.assertTrue(any("CrimsonOwl (left)" in t for t in btn_texts))

            # Build transcript and verify slot mapping format: [msg_id | timestamp | U2 | CrimsonOwl]: ...
            transcript, slot_to_uid, uid_to_slot, _ = build_group_transcript_and_slots(
                messages=recent_msgs,
                reporter_user_id=u0.id,
                reported_user_id=u1.id,
                reporter_nickname="BlueFox",
                reported_nickname="CrimsonOwl",
            )
            self.assertEqual(uid_to_slot[u0.id], "U1")
            self.assertEqual(uid_to_slot[u1.id], "U2")
            self.assertIn(f"| {uid_to_slot[u1.id]} | CrimsonOwl]: You are an idiot and bitch", transcript)

            # Run Multi-Party AI Moderation (mocking evaluate_group_transcript to return distinct penalties for U2, U3, U4)
            mock_verdict = GroupModerationVerdict(
                summary="Penalized CrimsonOwl (kick_group), SilverWolf (warn), and JadePanda (perm_ban).",
                actions=[
                    GroupModerationAction(
                        slot=uid_to_slot[u1.id],
                        severity="kick_group",
                        duration_hours=0,
                        reason="Harassment and insults before leaving.",
                    ),
                    GroupModerationAction(
                        slot=uid_to_slot[u2.id],
                        severity="warn",
                        duration_hours=0,
                        reason="Minor rudeness.",
                    ),
                    GroupModerationAction(
                        slot=uid_to_slot[u3.id],
                        severity="perm_ban",
                        duration_hours=0,
                        reason="Severe threats and doxxing.",
                    ),
                ],
            )

            bot = MagicMock()
            bot.send_message = AsyncMock()
            with patch.object(
                group_ai_moderator,
                "evaluate_group_transcript",
                new=AsyncMock(return_value=mock_verdict),
            ):
                ok, reply_html, verdict = await group_ai_moderator.run_group_moderation(
                    bot=bot,
                    session=session,
                    group_id=grp.id,
                    reporter_user_id=u0.id,
                    reported_user_id=u1.id,
                    reason_text="Harassment and threats",
                )
                self.assertTrue(ok)
                self.assertIsNotNone(verdict)
                self.assertIn("AI Safety Moderation Review Complete", reply_html)

                # Verify u1 is banned from the group (GroupChatBan)
                self.assertTrue(await is_user_banned_from_group(session, grp.id, u1.id))

                # Verify u2 received a priority penalty (50 -> 47) but is still active in the group
                u2_after = await get_user(session, u2.id)
                self.assertEqual(u2_after.priority, 47)
                u2_mem = await get_group_member_record(session, grp.id, u2.id)
                self.assertTrue(u2_mem.is_active)

                # Verify u3 is removed from the group, in GroupChatBan, and permanently banned from the bot
                self.assertTrue(await is_user_banned_from_group(session, grp.id, u3.id))
                u3_after = await get_user(session, u3.id)
                self.assertTrue(u3_after.is_banned)
                self.assertTrue(is_user_banned(u3.id))

                # Verify GroupModerationLog row was persisted
                log_stmt = select(GroupModerationLog).where(GroupModerationLog.group_id == grp.id)
                log_row = (await session.execute(log_stmt)).scalar_one_or_none()
                self.assertIsNotNone(log_row)
                logged_actions = json.loads(log_row.actions_json)
                self.assertEqual(len(logged_actions), 3)

                # Verify 2-minute anti-stampede lock blocks immediate duplicate report on the same group
                ok_dup, dup_msg, _ = await group_ai_moderator.run_group_moderation(
                    bot=bot,
                    session=session,
                    group_id=grp.id,
                    reporter_user_id=u2.id,
                    reported_user_id=u0.id,
                    reason_text="Duplicate stampede report",
                )
                self.assertFalse(ok_dup)
                self.assertIn("Recently Completed", dup_msg)

    # -------------------------------------------------------------------------
    # 6. Egalitarian Lifecycle & 3-Hour Inactivity Auto-Closure
    # -------------------------------------------------------------------------

    async def test_egalitarian_lifecycle_and_3h_inactivity_cleanup(self):
        async with get_db_session() as session:
            u0 = await get_user(session, self.test_user_ids[0])
            u1 = await get_user(session, self.test_user_ids[1])

            grp, _, _ = await create_group_chat_with_creator(
                session, u0, "Late Night Coding", None, 5, None, None, False, "BlueFox"
            )
            await join_group_chat_atomic(session, u1, grp.id, "CrimsonOwl")

            # Creator (u0) leaves -> group stays active because u1 is still inside!
            res1 = await leave_group_chat(session, user_id=u0.id, group_id=grp.id)
            self.assertIsNotNone(res1)
            self.assertFalse(res1[3])  # group_closed is False
            self.assertTrue(res1[0].is_active)

            # Last member (u1) leaves -> group automatically closes (is_active=False)
            res2 = await leave_group_chat(session, user_id=u1.id, group_id=grp.id)
            self.assertIsNotNone(res2)
            self.assertTrue(res2[3])  # group_closed is True
            self.assertFalse(res2[0].is_active)

            # Create another group and backdate last_activity_at by 3 hours + 5 minutes
            grp_stale, _, _ = await create_group_chat_with_creator(
                session, u0, "Stale Group", None, 5, None, None, False, "BlueFox"
            )
            grp_stale.last_activity_at = datetime.now(timezone.utc) - timedelta(hours=3, minutes=5)
            await session.commit()

            bot = MagicMock()
            bot.send_message = AsyncMock()
            closed_cnt = await check_and_close_inactive_group_chats(bot, session)
            self.assertGreaterEqual(closed_cnt, 1)
            await session.refresh(grp_stale)
            self.assertFalse(grp_stale.is_active)
            bot.send_message.assert_awaited()

    # -------------------------------------------------------------------------
    # 7. Web API (GET /api/anonymous/groups) & Bot Deep-Link (/start group_<id>)
    # -------------------------------------------------------------------------

    async def test_web_api_anonymous_groups_and_start_deeplink_join(self):
        from app.handlers.start import handle_start
        from app.states.states import GroupChatJoinState
        from app.web.server import api_list_anonymous_group_chats

        async with get_db_session() as session:
            u0 = await get_user(session, self.test_user_ids[0])  # male, straight, verified
            u1 = await get_user(session, self.test_user_ids[1])  # female, straight, verified
            u2 = await get_user(session, self.test_user_ids[2])  # male, bisexual, unverified
            u4 = await get_user(session, self.test_user_ids[4])  # male, straight, verified

            # Create an open group with capacity 3 (lowest bound)
            grp_open, _, _ = await create_group_chat_with_creator(
                session=session,
                creator=u0,
                name="DeepLink Open Room",
                description="Join from auahub.am",
                max_participants=3,
                allowed_gender=None,
                allowed_orientation=None,
                verified_only=False,
                creator_nickname="BlueFox",
            )
            # Create a verified-only group by u1
            grp_verif, _, _ = await create_group_chat_with_creator(
                session=session,
                creator=u1,
                name="Verified DeepLink Room",
                description=None,
                max_participants=5,
                allowed_gender=None,
                allowed_orientation=None,
                verified_only=True,
                creator_nickname="CrimsonOwl",
            )
            # Create an inactive group by u4 to ensure it's omitted
            grp_closed, _, _ = await create_group_chat_with_creator(
                session=session,
                creator=u4,
                name="Closed DeepLink Room",
                description=None,
                max_participants=5,
                allowed_gender=None,
                allowed_orientation=None,
                verified_only=False,
                creator_nickname="SilverWolf",
            )
            await leave_group_chat(session, user_id=u4.id, group_id=grp_closed.id)

        # 1. Verify GET /api/anonymous/groups response and strict privacy scope
        mock_req = MagicMock()
        mock_req.headers = {}
        mock_req.remote = "127.0.0.1"
        mock_req.query = {}
        resp = await api_list_anonymous_group_chats(mock_req)
        self.assertEqual(resp.status, 200)
        payload = json.loads(resp.text)
        self.assertTrue(payload["ok"])
        self.assertIn("bot_username", payload)

        by_id = {g["id"]: g for g in payload["groups"]}
        self.assertIn(grp_open.id, by_id)
        self.assertIn(grp_verif.id, by_id)
        self.assertNotIn(grp_closed.id, by_id)

        open_item = by_id[grp_open.id]
        self.assertEqual(open_item["name"], "DeepLink Open Room")
        self.assertEqual(open_item["current_participants"], 1)
        self.assertEqual(open_item["max_participants"], 3)
        self.assertFalse(open_item["is_full"])
        self.assertTrue(open_item["join_url"].endswith(f"?start=group_{grp_open.id}"))
        # Strict privacy: never leak creator_id, member IDs, nicknames, or messages
        for forbidden_key in ("creator_id", "members", "nicknames", "messages", "user_id"):
            self.assertNotIn(forbidden_key, open_item)

        # 2. Test /start group_<id> deep-link join flow
        bot = MagicMock()
        bot.send_message = AsyncMock()

        def _make_state():
            st = MagicMock()
            st._state = None
            st._data = {}
            st.clear = AsyncMock(side_effect=lambda: st._data.clear() or setattr(st, "_state", None))
            st.set_state = AsyncMock(side_effect=lambda s: setattr(st, "_state", s))
            st.update_data = AsyncMock(side_effect=lambda **kw: st._data.update(kw))
            st.get_data = AsyncMock(side_effect=lambda: dict(st._data))
            return st

        # A. Valid eligible new member (u4) -> prompts for nickname picker
        state_u4 = _make_state()
        msg_u4 = SimpleNamespace(
            from_user=SimpleNamespace(id=u4.id, username="u4", first_name="U4"),
            answer=AsyncMock(),
        )
        cmd_open = SimpleNamespace(args=f"group_{grp_open.id}")
        await handle_start(message=msg_u4, command=cmd_open, state=state_u4, bot=bot)
        self.assertEqual(state_u4._state, GroupChatJoinState.waiting_for_nickname)
        self.assertEqual(state_u4._data.get("join_group_id"), grp_open.id)
        msg_u4.answer.assert_awaited_once()
        self.assertIn("Choose Your Nickname", msg_u4.answer.call_args[0][0])

        # B. Returning member with sticky nickname: u4 joins as "JadeFalcon", leaves, then clicks /start group_<id>
        async with get_db_session() as session:
            u4_db = await get_user(session, u4.id)
            await join_group_chat_atomic(session, u4_db, grp_open.id, "JadeFalcon")
            await leave_group_chat(session, user_id=u4.id, group_id=grp_open.id)

        msg_u4.answer.reset_mock()
        await handle_start(message=msg_u4, command=cmd_open, state=state_u4, bot=bot)
        # Should automatically restore sticky nickname "JadeFalcon" without prompting picker!
        self.assertIsNone(state_u4._state)
        self.assertIn("Joined Group Chat", msg_u4.answer.call_args[0][0])
        self.assertIn("JadeFalcon", msg_u4.answer.call_args[0][0])

        # C. Unverified user (u2) tries to join verified_only group (grp_verif) -> rejected with limitation explanation
        state_u2 = _make_state()
        msg_u2 = SimpleNamespace(
            from_user=SimpleNamespace(id=u2.id, username="u2", first_name="U2"),
            answer=AsyncMock(),
        )
        cmd_verif = SimpleNamespace(args=f"group_{grp_verif.id}")
        await handle_start(message=msg_u2, command=cmd_verif, state=state_u2, bot=bot)
        self.assertIn("verified AUA students", msg_u2.answer.call_args[0][0])

        # D. Closed group -> fails gracefully with [Browse Active Groups]
        msg_u2.answer.reset_mock()
        cmd_closed = SimpleNamespace(args=f"group_{grp_closed.id}")
        await handle_start(message=msg_u2, command=cmd_closed, state=state_u2, bot=bot)
        self.assertIn("Closed or Not Found", msg_u2.answer.call_args[0][0])

        # E. Session exclusivity: u1 is already in grp_verif, tries /start group_<grp_open.id> -> blocked
        state_u1 = _make_state()
        msg_u1 = SimpleNamespace(
            from_user=SimpleNamespace(id=u1.id, username="u1", first_name="U1"),
            answer=AsyncMock(),
        )
        await handle_start(message=msg_u1, command=cmd_open, state=state_u1, bot=bot)
        self.assertIn("Already in Another Group Chat", msg_u1.answer.call_args[0][0])

        # F. GroupChatBan: ban u2 from grp_open, then u2 tries /start group_<grp_open.id> -> blocked
        async with get_db_session() as session:
            await ban_and_kick_user_from_group(session, grp_open.id, u2.id, "banned test")
        msg_u2.answer.reset_mock()
        await handle_start(message=msg_u2, command=cmd_open, state=state_u2, bot=bot)
        self.assertIn("not able to join this group chat", msg_u2.answer.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
