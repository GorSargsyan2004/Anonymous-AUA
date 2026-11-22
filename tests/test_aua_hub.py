"""Unit and integration tests for AUA Hub website & bot bridge features."""

from datetime import datetime, timedelta, timezone
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from aiogram.filters import CommandObject
from sqlalchemy import delete, select, text

from app.database.crud import create_or_regenerate_inbox, get_or_create_user, record_inbox_message
from app.database.models import (
    AcademicFeedback,
    AnonymousInbox,
    AnonymousPublicPost,
    Course,
    HaveNeedPost,
    InboxMessage,
    SpamChatMessage,
    SpamChatReaction,
    SpamChatRead,
    TelegramGroup,
    User,
    UserNotification,
    WebAnalyticsEvent,
    WebEmailCooldown,
    WebOtpCode,
    WebPasswordResetCode,
    WebSyncCode,
    WebUser,
    WebUserAIChatSession,
    AIUsageLog,
)
from app.database.session import engine, get_db_session, init_db
from app.database.web_crud import (
    authenticate_web_user,
    consume_sync_code_and_link_bot,
    create_academic_feedback,
    create_anonymous_public_post,
    create_have_need_post,
    create_spam_chat_message,
    create_telegram_group,
    create_user_notification,
    create_web_password_reset_otp,
    create_web_registration_otp,
    delete_academic_feedback,
    delete_have_need_post,
    delete_or_anonymize_web_user,
    delete_spam_chat_message,
    delete_telegram_group,
    edit_spam_chat_message,
    find_reciprocal_have_need_matches,
    generate_web_sync_code,
    get_feedback_catalog,
    get_spam_chat_unread_count,
    get_synced_web_inbox,
    get_unread_notification_count,
    get_web_admin_stats,
    get_web_user_by_email,
    get_web_user_by_id,
    hash_password,
    list_academic_feedbacks,
    list_anonymous_public_posts,
    list_have_need_posts,
    list_spam_chat_messages,
    list_spam_chat_messages_for_user,
    list_telegram_groups,
    list_user_notifications,
    mark_all_notifications_read,
    mark_notification_read,
    mark_spam_chat_read,
    record_web_analytics_event,
    serialize_user_notification,
    toggle_spam_chat_reaction,
    update_have_need_post,
    update_telegram_group,
    update_web_user_profile,
    verify_web_otp_and_register,
    verify_web_password_reset_otp,
)
from app.services.email_verification import reset_global_email_rate_limit
from app.services.booking_scraper import (
    merge_and_filter_free_slots,
    parse_booking_day_html,
)
from app.services.sonis_scraper import (
    get_semester_candidates,
    parse_sonis_courses_html,
)
from app.handlers.web_bridge import (
    handle_post_command,
    handle_post_text_input,
)
from app.config import settings
from app.web.server import (
    ADMIN_STATA_EMAIL,
    check_rate_limit,
    create_jwt_token,
    decode_jwt_token,
    is_safe_upload_url,
    is_web_admin,
    render_safe_chat_html,
    reset_rate_limits,
    scan_web_text_for_malicious_links,
    serialize_academic_feedback,
    serialize_have_need,
    serialize_spam_message,
    serialize_web_user,
    validate_image_magic_bytes,
    validate_media_magic_bytes,
)


class TestAuaHub(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        await init_db()
        self.tg_uid_1 = 9911001
        self.tg_uid_2 = 9911002
        self.test_email_1 = "gor_teststudent@edu.aua.am"
        self.test_email_2 = "anna_testalumni@alumni.aua.am"

    async def asyncTearDown(self) -> None:
        async with get_db_session() as session:
            await session.execute(
                delete(WebOtpCode).where(
                    WebOtpCode.email.in_([self.test_email_1, self.test_email_2])
                    | WebOtpCode.email.like("cooldown_%@edu.aua.am")
                    | WebOtpCode.email.like("ipcap_%@edu.aua.am")
                    | WebOtpCode.email.like("chat_perm_%@edu.aua.am")
                )
            )
            await session.execute(
                delete(WebPasswordResetCode).where(
                    WebPasswordResetCode.email.in_([self.test_email_1, self.test_email_2])
                )
            )
            await session.execute(
                delete(WebEmailCooldown).where(
                    WebEmailCooldown.email.in_(
                        [self.test_email_1, self.test_email_2, "nonexistent_aua_person@edu.aua.am"]
                    )
                    | WebEmailCooldown.email.like("cooldown_%@edu.aua.am")
                    | WebEmailCooldown.email.like("ipcap_%@edu.aua.am")
                )
            )
            await session.execute(
                delete(AnonymousPublicPost).where(
                    AnonymousPublicPost.telegram_user_id.in_([self.tg_uid_1, self.tg_uid_2])
                )
            )
            await session.execute(
                delete(InboxMessage).where(
                    InboxMessage.owner_id.in_([self.tg_uid_1, self.tg_uid_2])
                    | InboxMessage.sender_id.in_([self.tg_uid_1, self.tg_uid_2])
                )
            )
            await session.execute(
                delete(AnonymousInbox).where(AnonymousInbox.user_id.in_([self.tg_uid_1, self.tg_uid_2]))
            )
            await session.execute(
                delete(Course).where(Course.semester_label == "TestSem 2099")
            )
            await session.execute(
                delete(WebUser).where(
                    WebUser.email.in_([self.test_email_1, self.test_email_2])
                    | WebUser.email.like("deleted_%@deleted.local")
                    | (WebUser.email == "del_student@edu.aua.am")
                )
            )
            await session.execute(
                delete(User).where(User.id.in_([self.tg_uid_1, self.tg_uid_2, 99887711]))
            )
            await session.execute(
                delete(WebAnalyticsEvent).where(WebAnalyticsEvent.event_type.like("test_%"))
            )
            await session.execute(
                delete(AIUsageLog).where(
                    AIUsageLog.session_id.in_(["test_session_123", "mobile_local_sid", "desktop_local_sid"])
                    | AIUsageLog.session_id.like("user:%")
                )
            )
            # Re-sync anonymous_public_posts sequence to actual current max id
            await session.execute(
                text("SELECT setval(pg_get_serial_sequence('anonymous_public_posts', 'id'), COALESCE((SELECT MAX(id) FROM anonymous_public_posts), 1), true)")
            )
            await session.commit()
        await engine.dispose()

    def test_sonis_semester_last_month_switch_rule(self) -> None:
        """Verify semester candidates stay on current semester and switch on the last month."""
        # October 2026 (Fall in progress) -> primary is Fall 2026
        oct_dt = datetime(2026, 10, 15, tzinfo=timezone.utc)
        oct_cands = get_semester_candidates(oct_dt)
        self.assertEqual(oct_cands[0], ("202627", "1 ", "Fall 2026"))

        # December 2026 (Last month of Fall) -> switches to Spring 2027!
        dec_dt = datetime(2026, 12, 5, tzinfo=timezone.utc)
        dec_cands = get_semester_candidates(dec_dt)
        self.assertEqual(dec_cands[0], ("202627", "3 ", "Spring 2027"))

        # March 2027 (Spring in progress) -> stays on Spring 2027
        mar_dt = datetime(2027, 3, 10, tzinfo=timezone.utc)
        mar_cands = get_semester_candidates(mar_dt)
        self.assertEqual(mar_cands[0], ("202627", "3 ", "Spring 2027"))

        # May 2027 (Last month of Spring) -> switches to Fall 2027!
        may_dt = datetime(2027, 5, 12, tzinfo=timezone.utc)
        may_cands = get_semester_candidates(may_dt)
        self.assertEqual(may_cands[0], ("202728", "1 ", "Fall 2027"))

    def test_parse_sonis_courses_html(self) -> None:
        """Verify Jenzabar Sonis <table id='crsbysemester'> parser."""
        sample_html = """
        <table id="crsbysemester">
          <tr>
            <td><span class="title">Data Structures</span> <span class="crsbysemester_course_id">(CS121)</span></td>
            <td>A</td>
            <td>15W</td>
            <td>3.00</td>
            <td>AUA Main Campus</td>
            <td>Sargsyan, Gor</td>
            <td>MWF 10:30AM-11:20AM<br>M 01:30PM-02:20PM</td>
            <td>30/30</td><td>0</td><td></td><td></td>
            <td>PAB 314W</td>
          </tr>
        </table>
        """
        rows = parse_sonis_courses_html(sample_html)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["course_code"], "CS121")
        self.assertEqual(rows[0]["course_name"], "Data Structures")
        self.assertEqual(rows[0]["section"], "A")
        self.assertEqual(rows[0]["instructor"], "Sargsyan, Gor")
        self.assertIn("MWF 10:30AM-11:20AM | M 01:30PM-02:20PM", rows[0]["times"])

    def test_booking_free_rooms_10min_gap_filter(self) -> None:
        """Verify 10-minute passing gaps between classes are filtered out while >=20m blocks are kept."""
        # 08:00 (480) to 09:00 (540) -> 6 slots (60m, KEEP)
        # 10:20 (620) to 10:30 (630) -> 1 slot (10m passing gap between classes, DISCARD!)
        # 12:00 (720) to 12:20 (740) -> 2 slots (20m, KEEP)
        slot_starts = [480, 490, 500, 510, 520, 530, 620, 720, 730]
        windows, is_free_now, free_until = merge_and_filter_free_slots(slot_starts, now_minutes=505)

        self.assertEqual(len(windows), 2)
        self.assertEqual(windows[0]["label"], "08:00 – 09:00")
        self.assertEqual(windows[0]["duration_minutes"], 60)
        self.assertTrue(windows[0]["is_now"])
        self.assertEqual(windows[1]["label"], "12:00 – 12:20")
        self.assertEqual(windows[1]["duration_minutes"], 20)
        self.assertTrue(is_free_now)
        self.assertEqual(free_until, "09:00")

        # Test HTML table parsing
        sample_mrbs_html = """
        <thead>
          <tr>
            <th>Time:</th>
            <th data-room="11"><a href="#">308E<span class="capacity">40</span></a></th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td class="new"><a href="edit_entry.php?view=day&amp;room=11&amp;hour=9&amp;minute=0"></a></td>
            <td class="new"><a href="edit_entry.php?view=day&amp;room=11&amp;hour=9&amp;minute=10"></a></td>
            <td class="new"><a href="edit_entry.php?view=day&amp;room=11&amp;hour=11&amp;minute=20"></a></td>
          </tr>
        </tbody>
        """
        rooms = parse_booking_day_html(sample_mrbs_html, area_key="main", now_minutes=545)
        self.assertEqual(len(rooms), 1)
        self.assertEqual(rooms[0]["room_name"], "308E")
        self.assertEqual(rooms[0]["capacity"], 40)
        # Only the 09:00-09:20 block (20m) is kept; the single 11:20-11:30 (10m) gap is ignored!
        self.assertEqual(len(rooms[0]["free_windows"]), 1)
        self.assertEqual(rooms[0]["free_windows"][0]["label"], "09:00 – 09:20")

    def test_safe_markdown_and_html_renderer(self) -> None:
        """Verify Spam Chat supports Markdown & safe HTML tags while escaping XSS and attribute breakouts."""
        raw = "Hello <b>AUA</b> **bold** *italic* ~~strike~~ <u>under</u> <script>alert(1)</script>"
        rendered = render_safe_chat_html(raw)
        self.assertIn("<b>AUA</b>", rendered)
        self.assertIn("<b>bold</b>", rendered)
        self.assertIn("<i>italic</i>", rendered)
        self.assertIn("<s>strike</s>", rendered)
        self.assertIn("<u>under</u>", rendered)
        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)

        # Ensure URL regex stops before escaped quotes and preserves query ampersands
        injection_attempt = 'Visit https://aua.am/events?id=1&day=2"onmouseover="alert(1)'
        safe_rendered = render_safe_chat_html(injection_attempt)
        self.assertIn('href="https://aua.am/events?id=1&amp;day=2"', safe_rendered)
        self.assertNotIn('onmouseover="alert(1)"', safe_rendered)

    def test_security_controls_and_hardening(self) -> None:
        """Verify image magic-byte checks, local upload URL validation, link shield, and rate limiting."""
        # 1. Media Magic Bytes validation (Images, GIFs, Videos)
        valid_png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        valid_jpg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01"
        valid_gif = b"GIF89a\x01\x00\x01\x00\x80\x00"
        valid_webp = b"RIFF\x24\x00\x00\x00WEBPVP8 "
        valid_mp4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00"
        valid_webm = b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81"
        valid_mov = b"\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00"
        fake_svg_as_png = b"<svg onload=alert(1)></svg>"
        fake_exe_as_mp4 = b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00"

        self.assertTrue(validate_image_magic_bytes(valid_png, ".png"))
        self.assertTrue(validate_image_magic_bytes(valid_jpg, ".jpg"))
        self.assertTrue(validate_image_magic_bytes(valid_gif, ".gif"))
        self.assertTrue(validate_image_magic_bytes(valid_webp, ".webp"))
        self.assertFalse(validate_image_magic_bytes(fake_svg_as_png, ".png"))
        self.assertFalse(validate_image_magic_bytes(valid_png, ".jpg"))

        # Video magic byte checks
        mp4_ok, mp4_type = validate_media_magic_bytes(valid_mp4, ".mp4")
        self.assertTrue(mp4_ok)
        self.assertEqual(mp4_type, "video")

        webm_ok, webm_type = validate_media_magic_bytes(valid_webm, ".webm")
        self.assertTrue(webm_ok)
        self.assertEqual(webm_type, "video")

        mov_ok, mov_type = validate_media_magic_bytes(valid_mov, ".mov")
        self.assertTrue(mov_ok)
        self.assertEqual(mov_type, "video")

        gif_ok, gif_type = validate_media_magic_bytes(valid_gif, ".gif")
        self.assertTrue(gif_ok)
        self.assertEqual(gif_type, "gif")

        fake_ok, _ = validate_media_magic_bytes(fake_exe_as_mp4, ".mp4")
        self.assertFalse(fake_ok)

        # 2. Safe upload URL validation (Images, GIFs, Videos)
        self.assertTrue(is_safe_upload_url(""))
        self.assertTrue(is_safe_upload_url("/uploads/1762000000_0123456789abcdef.png"))
        self.assertTrue(is_safe_upload_url("/uploads/1762000000_0123456789abcdef.gif"))
        self.assertTrue(is_safe_upload_url("/uploads/1762000000_0123456789abcdef.mp4"))
        self.assertTrue(is_safe_upload_url("/uploads/1762000000_0123456789abcdef.webm"))
        self.assertTrue(is_safe_upload_url("/uploads/1762000000_0123456789abcdef.mov"))
        self.assertFalse(is_safe_upload_url("https://evil.example.com/tracker.png"))
        self.assertFalse(is_safe_upload_url("javascript:alert(1)"))
        self.assertFalse(is_safe_upload_url("/uploads/../app/config.py"))
        self.assertFalse(is_safe_upload_url("/uploads/1762000000_0123456789abcdef.exe"))

        # 3. Malicious link scanner (IP-loggers & executables)
        self.assertIsNone(scan_web_text_for_malicious_links("Join our study session at https://aua.am!"))
        self.assertIsNotNone(scan_web_text_for_malicious_links("Click here https://grabify.link/ABC123"))
        self.assertIsNotNone(scan_web_text_for_malicious_links("Download https://example.com/cheat.apk"))

        # 4. Sliding-window rate limiter
        reset_rate_limits()
        for _ in range(3):
            ok, _ = check_rate_limit("test_bucket", "127.0.0.1", max_calls=3, window_sec=60)
            self.assertTrue(ok)
        blocked_ok, retry_sec = check_rate_limit("test_bucket", "127.0.0.1", max_calls=3, window_sec=60)
        self.assertFalse(blocked_ok)
        self.assertGreaterEqual(retry_sec, 1)
        reset_rate_limits()

    async def test_web_registration_sync_autoverify_and_modules(self) -> None:
        """End-to-end test of registration OTP, /sync bot auto-verification + gender check, Have-Need, Groups, and Spam Chat."""
        async with get_db_session() as session:
            # 1. Register User 1 with OTP
            code1, _ = await create_web_registration_otp(
                session, name="Gor Test", email=self.test_email_1, password="MyCustomHubPass123"
            )
            ok, u1, msg = await verify_web_otp_and_register(session, email=self.test_email_1, otp_code=code1)
            self.assertTrue(ok, msg)
            self.assertIsNotNone(u1)

            # Verify login & JWT
            auth_u1 = await authenticate_web_user(session, email=self.test_email_1, password="MyCustomHubPass123")
            self.assertIsNotNone(auth_u1)
            token = create_jwt_token(u1.id)
            self.assertEqual(decode_jwt_token(token), u1.id)

            # 2. Create an unverified Telegram user with sex="male" and sync via /sync
            tg_user = await get_or_create_user(session, user_id=self.tg_uid_1, username="gor_tg")
            tg_user.sex = "male"
            tg_user.is_verified = False
            await session.commit()

            sync_code = await generate_web_sync_code(session, web_user_id=u1.id)
            sync_ok, synced_web, synced_tg, _ = await consume_sync_code_and_link_bot(
                session=session,
                code=sync_code,
                telegram_user_id=self.tg_uid_1,
                telegram_username="gor_tg",
            )
            self.assertTrue(sync_ok)
            self.assertEqual(synced_web.telegram_user_id, self.tg_uid_1)
            self.assertTrue(synced_web.inbox_link.startswith("https://t.me/"))
            # Bot user must be auto-verified and gender-verified ("gor" -> male == male)
            self.assertTrue(synced_tg.is_verified)
            self.assertEqual(synced_tg.email, self.test_email_1)
            self.assertTrue(synced_tg.is_gender_verified)
            self.assertFalse(synced_tg.gender_mismatch)

            # 3. Record an inbox message and verify it appears in synced web inbox
            await get_or_create_user(session, user_id=self.tg_uid_2, username="sender_tg")
            inbox_msg = await record_inbox_message(
                session=session,
                owner_id=self.tg_uid_1,
                sender_id=self.tg_uid_2,
                message_type="text",
                content_text="Hey from anonymous /link!",
            )
            web_inbox = await get_synced_web_inbox(session, web_user=synced_web)
            self.assertTrue(web_inbox["is_synced"])
            self.assertEqual(len(web_inbox["messages"]), 1)
            self.assertEqual(web_inbox["messages"][0]["content_text"], "Hey from anonymous /link!")

            # 4. Create anonymous public posts (/post) and verify strictly sequential IDs
            pub_post = await create_anonymous_public_post(
                session=session,
                telegram_user_id=self.tg_uid_1,
                content_text="Anonymous campus thought #1",
            )
            pub_post2 = await create_anonymous_public_post(
                session=session,
                telegram_user_id=self.tg_uid_1,
                content_text="Lost brown wallet near MB cafeteria",
            )
            self.assertEqual(pub_post2.id, pub_post.id + 1)

            # Test search by ID (#id or id)
            posts_by_id, _ = await list_anonymous_public_posts(session, search_query=f"#{pub_post.id}")
            self.assertEqual(len(posts_by_id), 1)
            self.assertEqual(posts_by_id[0].id, pub_post.id)

            # Test search by words sequence
            posts_by_words, _ = await list_anonymous_public_posts(session, search_query="wallet cafeteria")
            self.assertEqual(len(posts_by_words), 1)
            self.assertEqual(posts_by_words[0].id, pub_post2.id)

            # Test search in personal inbox
            inbox_searched = await get_synced_web_inbox(session, web_user=synced_web, search_query="anonymous /link")
            self.assertEqual(len(inbox_searched["messages"]), 1)
            self.assertEqual(inbox_searched["messages"][0]["id"], inbox_msg.id)

            # Test web analytics event and admin reporting stats
            await record_web_analytics_event(session, event_type="test_click_rooms", web_user_id=synced_web.id)
            admin_stats = await get_web_admin_stats(session)
            self.assertIn("users", admin_stats)
            self.assertIn("content", admin_stats)
            self.assertIn("analytics", admin_stats)
            self.assertGreaterEqual(admin_stats["users"]["total"], 1)
            self.assertGreaterEqual(admin_stats["analytics"]["total_clicks"], 1)

            # 5. Courses & Have-Need CRUD
            c1 = Course(
                semester_label="TestSem 2099",
                school_year_code="209900",
                semester_code="1",
                course_code="CS901",
                course_title="Quantum Computing",
                section="A",
                instructor="Dr. Alpha",
                schedule_times="MWF 09:30AM-10:20AM",
            )
            c2 = Course(
                semester_label="TestSem 2099",
                school_year_code="209900",
                semester_code="1",
                course_code="CS901",
                course_title="Quantum Computing",
                section="B",
                instructor="Dr. Beta",
                schedule_times="TR 12:00PM-01:15PM",
            )
            session.add_all([c1, c2])
            await session.commit()
            await session.refresh(c1)
            await session.refresh(c2)

            hn_post = await create_have_need_post(
                session=session,
                web_user_id=u1.id,
                have_course_id=c1.id,
                need_course_id=c2.id,
                contact_dm="@gor_tg",
                notes="Prefer afternoon slot",
            )
            hn_list = await list_have_need_posts(session, only_user_id=u1.id)
            self.assertEqual(len(hn_list), 1)
            self.assertEqual(hn_list[0].have_course.section, "A")
            self.assertEqual(hn_list[0].need_course.section, "B")

            updated_hn = await update_have_need_post(
                session=session,
                post_id=hn_post.id,
                web_user_id=u1.id,
                contact_dm="@gor_updated",
            )
            self.assertEqual(updated_hn.contact_dm, "@gor_updated")
            deleted_hn = await delete_have_need_post(session, post_id=hn_post.id, web_user_id=u1.id)
            self.assertTrue(deleted_hn)

            # 6. Telegram Groups CRUD, Edit & Search
            grp = await create_telegram_group(
                session=session,
                web_user_id=u1.id,
                name="AUA Robotics Club Test",
                invite_link="https://t.me/aua_robotics_test",
                description="Robotics and embedded systems",
            )
            grps, total_grps, _ = await list_telegram_groups(session, search_query="Robotics")
            self.assertGreaterEqual(total_grps, 1)

            updated_grp = await update_telegram_group(
                session=session,
                group_id=grp.id,
                web_user_id=u1.id,
                name="AUA Robotics & AI Club",
                invite_link="https://t.me/aua_robotics_ai",
                description="Updated description for Robotics & AI",
            )
            self.assertIsNotNone(updated_grp)
            self.assertEqual(updated_grp.name, "AUA Robotics & AI Club")
            self.assertEqual(updated_grp.invite_link, "https://t.me/aua_robotics_ai")

            # Another user cannot edit User 1's group
            forbidden_grp_edit = await update_telegram_group(
                session=session,
                group_id=grp.id,
                web_user_id=u1.id + 999999,
                name="Hijacked Group",
                invite_link="https://t.me/hijacked",
            )
            self.assertIsNone(forbidden_grp_edit)

            self.assertTrue(await delete_telegram_group(session, group_id=grp.id, web_user_id=u1.id))

            # 7. Spam Chat & Unread Counter for a second user
            code2, _ = await create_web_registration_otp(
                session, name="Anna Alumni", email=self.test_email_2, password="Pass234!"
            )
            _, u2, _ = await verify_web_otp_and_register(session, email=self.test_email_2, otp_code=code2)
            await mark_spam_chat_read(session, web_user_id=u2.id)
            self.assertEqual(await get_spam_chat_unread_count(session, u2.id), 0)

            # User 1 posts a message in Spam Chat -> User 2's unread count increments to 1
            spam_msg = await create_spam_chat_message(
                session=session,
                web_user_id=u1.id,
                content_html="<b>Chess Night</b> at PAB!",
            )
            self.assertEqual(await get_spam_chat_unread_count(session, u1.id), 0)
            self.assertEqual(await get_spam_chat_unread_count(session, u2.id), 1)

            # User 1 posts a video message in Campus Chat
            video_msg = await create_spam_chat_message(
                session=session,
                web_user_id=u1.id,
                content_html="Check out the hackathon highlights!",
                image_url="/uploads/1762000000_1122334455667788.mp4",
                media_type="video",
            )
            serialized_vid = serialize_spam_message(video_msg)
            self.assertEqual(serialized_vid["media_type"], "video")
            self.assertEqual(serialized_vid["image_url"], "/uploads/1762000000_1122334455667788.mp4")

            # Check read cursor context & Telegram unread anchor (`first_unread_message_id`)
            msgs_u2, uc_u2, lr_u2, first_unread_u2 = await list_spam_chat_messages_for_user(
                session, web_user_id=u2.id
            )
            self.assertEqual(uc_u2, 2)
            self.assertLess(lr_u2, spam_msg.id)
            self.assertEqual(first_unread_u2, spam_msg.id)

            # Partial read up to spam_msg -> remaining unread is 1 (video_msg)
            await mark_spam_chat_read(session, web_user_id=u2.id, latest_message_id=spam_msg.id)
            msgs_p, uc_p, lr_p, first_unread_p = await list_spam_chat_messages_for_user(
                session, web_user_id=u2.id
            )
            self.assertEqual(uc_p, 1)
            self.assertEqual(lr_p, spam_msg.id)
            self.assertEqual(first_unread_p, video_msg.id)
            self.assertTrue(any(m.id == spam_msg.id for m in msgs_p))
            self.assertTrue(any(m.id == video_msg.id for m in msgs_p))

            # User 2 reads Spam Chat -> unread count resets to 0
            await mark_spam_chat_read(session, web_user_id=u2.id, latest_message_id=video_msg.id)
            self.assertEqual(await get_spam_chat_unread_count(session, u2.id), 0)

            # User 2 replies to User 1's message (Telegram replying)
            reply_msg = await create_spam_chat_message(
                session=session,
                web_user_id=u2.id,
                content_html="I will definitely join!",
                reply_to_id=spam_msg.id,
            )
            self.assertEqual(reply_msg.reply_to_id, spam_msg.id)
            ser_reply = serialize_spam_message(reply_msg)
            self.assertIsNotNone(ser_reply["reply_to"])
            self.assertEqual(ser_reply["reply_to"]["id"], spam_msg.id)
            self.assertIn("Chess Night", ser_reply["reply_to"]["snippet"])

            # User 1 reacts with ❤️ (Telegram reactions)
            active_emoji, reactions = await toggle_spam_chat_reaction(
                session=session,
                message_id=reply_msg.id,
                web_user_id=u1.id,
                emoji="❤️",
            )
            self.assertEqual(active_emoji, "❤️")
            self.assertEqual(len(reactions), 1)
            self.assertEqual(reactions[0]["emoji"], "❤️")
            self.assertEqual(reactions[0]["count"], 1)
            self.assertEqual(len(reactions[0]["users"]), 1)
            self.assertEqual(reactions[0]["users"][0]["id"], u1.id)
            self.assertEqual(reactions[0]["users"][0]["name"], u1.name)
            self.assertIsNotNone(reactions[0]["users"][0]["created_at"])

            # User 1 changes reaction to 🔥
            active_emoji, reactions = await toggle_spam_chat_reaction(
                session=session,
                message_id=reply_msg.id,
                web_user_id=u1.id,
                emoji="🔥",
            )
            self.assertEqual(active_emoji, "🔥")
            self.assertEqual(len(reactions), 1)
            self.assertEqual(reactions[0]["emoji"], "🔥")

            # User 1 toggles off reaction by clicking 🔥 again
            active_emoji, reactions = await toggle_spam_chat_reaction(
                session=session,
                message_id=reply_msg.id,
                web_user_id=u1.id,
                emoji="🔥",
            )
            self.assertIsNone(active_emoji)
            self.assertEqual(len(reactions), 0)

            # User 2 edits their reply message
            edited_msg = await edit_spam_chat_message(
                session=session,
                message_id=reply_msg.id,
                web_user_id=u2.id,
                new_content_html="<i>Updated:</i> I will bring snacks!",
            )
            self.assertIsNotNone(edited_msg)
            self.assertTrue(edited_msg.is_edited)
            self.assertIsNotNone(edited_msg.edited_at)
            self.assertIn("Updated:", edited_msg.content_html)
            ser_edited = serialize_spam_message(edited_msg)
            self.assertTrue(ser_edited["is_edited"])
            self.assertIsNotNone(ser_edited["edited_at"])

            # User 1 tries to edit User 2's message -> Rejected
            forbidden_edit = await edit_spam_chat_message(
                session=session,
                message_id=reply_msg.id,
                web_user_id=u1.id,
                new_content_html="Hacked!",
            )
            self.assertIsNone(forbidden_edit)

            # User 1 tries to delete User 2's message without admin -> Rejected
            forbidden_del = await delete_spam_chat_message(
                session=session,
                message_id=reply_msg.id,
                web_user_id=u1.id,
                is_admin=False,
            )
            self.assertFalse(forbidden_del)

            # User 2 deletes their own reply message -> Success
            deleted_ok = await delete_spam_chat_message(
                session=session,
                message_id=reply_msg.id,
                web_user_id=u2.id,
                is_admin=False,
            )
            self.assertTrue(deleted_ok)

            await update_web_user_profile(session, web_user_id=u1.id, bio="Senior CS Student")

            # 8. Test /post handler flow (handle_post_command -> handle_post_text_input)
            mock_msg = MagicMock()
            mock_msg.from_user = MagicMock(id=self.tg_uid_1, username="gor_tg")
            mock_msg.text = "Testing /post handler directly from Telegram!"
            mock_msg.entities = []
            mock_msg.caption = None
            mock_msg.caption_entities = []
            mock_msg.answer = AsyncMock()
            mock_state = AsyncMock()

            await handle_post_command(mock_msg, CommandObject(command="post", args=None), mock_state)
            mock_state.set_state.assert_called_once()

            mock_msg.answer.reset_mock()
            with patch(
                "app.handlers.web_bridge.gemini_moderator.evaluate_anonymous_post",
                new=AsyncMock(return_value="VALID"),
            ):
                await handle_post_text_input(mock_msg, mock_state)
            mock_msg.answer.assert_called_once()
            reply_arg = mock_msg.answer.call_args[0][0]
            self.assertIn("Anonymous Post Published to AUA Hub!", reply_arg)

            # 8b. Test 3-Tier Moderation Pipeline (Tier 1 Algorithmic, Tier 2 Gemini AI, Tier 3 Community Reporting)
            from app.services.ai_moderator import TIER1_REJECTION_MESSAGE, validate_anonymous_post_tier1
            from app.database.web_crud import report_anonymous_public_post

            # Tier 1: Rejects short (<12 chars), zero-vowel ("fjjdkslmnpqr"), repetitive ("hhhhhhhhhhhh"), and keyboard smash ("asdfghjklzxcv")
            for bad_input in ["hi", "fjjd", "hhh", "fjjdkslmnpqr", "hhhhhhhhhhhhhh", "asdfghjklzxcv", "asdasdasdasd"]:
                t1_ok, t1_msg = validate_anonymous_post_tier1(bad_input)
                self.assertFalse(t1_ok, f"Expected Tier 1 to reject: {bad_input}")
                self.assertEqual(t1_msg, TIER1_REJECTION_MESSAGE)

            mock_msg.answer.reset_mock()
            mock_msg.text = "fjjd"
            await handle_post_text_input(mock_msg, mock_state)
            mock_msg.answer.assert_called_once_with(TIER1_REJECTION_MESSAGE)

            # Tier 2: Rejects post when Gemini AI moderator returns 'SPAM'
            mock_msg.answer.reset_mock()
            mock_msg.text = "Random meaningless words that pass length check"
            with patch(
                "app.handlers.web_bridge.gemini_moderator.evaluate_anonymous_post",
                new=AsyncMock(return_value="SPAM"),
            ):
                await handle_post_text_input(mock_msg, mock_state)
            mock_msg.answer.assert_called_once()
            self.assertIn("did not meet our community standards", mock_msg.answer.call_args[0][0])

            # Tier 3: Community reporting hides post after 3 unique reports
            rep1 = await report_anonymous_public_post(session, post_id=pub_post.id, reporter_key=f"user:{u1.id}", web_user_id=u1.id)
            self.assertTrue(rep1["ok"])
            self.assertFalse(rep1["already_reported"])
            self.assertEqual(rep1["report_count"], 1)
            self.assertFalse(rep1["auto_hidden"])

            # Duplicate report by same user does not increment count
            rep1_dup = await report_anonymous_public_post(session, post_id=pub_post.id, reporter_key=f"user:{u1.id}", web_user_id=u1.id)
            self.assertTrue(rep1_dup["already_reported"])
            self.assertEqual(rep1_dup["report_count"], 1)

            rep2 = await report_anonymous_public_post(session, post_id=pub_post.id, reporter_key=f"user:{u2.id}", web_user_id=u2.id)
            self.assertEqual(rep2["report_count"], 2)
            self.assertFalse(rep2["auto_hidden"])

            rep3 = await report_anonymous_public_post(session, post_id=pub_post.id, reporter_key="ip:203.0.113.99")
            self.assertEqual(rep3["report_count"], 3)
            self.assertTrue(rep3["auto_hidden"])
            self.assertTrue(rep3["newly_hidden"])

            # Verify hidden post no longer appears in public feed listing
            visible_posts, _ = await list_anonymous_public_posts(session, search_query=f"#{pub_post.id}")
            self.assertEqual(len(visible_posts), 0)

            # 9. Test Academic Feedbacks (Instructors & Courses)
            fb_inst = await create_academic_feedback(
                session=session,
                web_user_id=u1.id,
                target_type="instructor",
                target_key="Dr. Alpha",
                target_title="Dr. Alpha",
                target_subtitle="CS901",
                rating=5,
                comment="Amazing lectures and clear grading criteria!",
                is_anonymous=True,
                push_to_remote=False,
            )
            fb_course = await create_academic_feedback(
                session=session,
                web_user_id=u2.id,
                target_type="course",
                target_key="CS901",
                target_title="CS901 — Quantum Computing",
                target_subtitle="Dr. Alpha, Dr. Beta",
                rating=4,
                comment="Challenging homework but super rewarding.",
                is_anonymous=False,
                push_to_remote=False,
            )

            inst_fbs, inst_sum = await list_academic_feedbacks(
                session, target_type="instructor", target_key="Dr. Alpha"
            )
            self.assertEqual(len(inst_fbs), 1)
            self.assertEqual(inst_sum["avg_rating"], 5.0)
            self.assertTrue(inst_fbs[0].is_anonymous)

            crs_fbs, crs_sum = await list_academic_feedbacks(
                session, target_type="course", search_query="Quantum"
            )
            self.assertGreaterEqual(len(crs_fbs), 1)
            self.assertEqual(crs_sum["avg_rating"], 4.0)

            catalog = await get_feedback_catalog(session)
            self.assertIn("instructors", catalog)
            self.assertIn("courses", catalog)

            self.assertTrue(await delete_academic_feedback(session, feedback_id=fb_inst.id, web_user_id=u1.id))
            self.assertTrue(await delete_academic_feedback(session, feedback_id=fb_course.id, web_user_id=u2.id))

            # 10. Real-Time In-App Notifications & Have-Need Reciprocal Matching
            # User 1 posts: Has c1 (Section A), Needs c2 (Section B) with notify_email=True
            hn1 = await create_have_need_post(
                session=session,
                web_user_id=u1.id,
                have_course_id=c1.id,
                need_course_id=c2.id,
                contact_dm="@gor_tg",
                notes="Morning for afternoon section",
                notify_email=True,
            )
            self.assertTrue(hn1.notify_email)
            hn1_toggled = await update_have_need_post(
                session=session,
                post_id=hn1.id,
                web_user_id=u1.id,
                notify_email=False,
            )
            self.assertFalse(hn1_toggled.notify_email)
            hn1 = await update_have_need_post(
                session=session,
                post_id=hn1.id,
                web_user_id=u1.id,
                notify_email=True,
            )
            self.assertTrue(hn1.notify_email)
            # User 2 checks for reciprocal match: Has c2 (Section B), Needs c1 (Section A)
            reciprocal_matches = await find_reciprocal_have_need_matches(
                session=session,
                have_course_id=c2.id,
                need_course_id=c1.id,
                exclude_user_id=u2.id,
            )
            self.assertEqual(len(reciprocal_matches), 1)
            self.assertEqual(reciprocal_matches[0].id, hn1.id)
            self.assertEqual(reciprocal_matches[0].web_user_id, u1.id)

            # Create in-app notifications for User 1
            notif1 = await create_user_notification(
                session=session,
                recipient_id=u1.id,
                sender_id=u2.id,
                category="chat_reply",
                title="Anna replied to your chat",
                content="Hey Gor, let's coordinate the swap!",
                target_type="chat_message",
                target_id="101",
                extra_data={"message_id": 101, "sender_name": "Anna Alumni"},
            )
            notif2 = await create_user_notification(
                session=session,
                recipient_id=u1.id,
                sender_id=u2.id,
                category="have_need_match",
                title="Reciprocal Match Found!",
                content="Anna Alumni wants to swap CS901 Section A for Section B",
                target_type="have_need",
                target_id=str(hn1.id),
                extra_data={"have_course_code": "CS901", "need_course_code": "CS901"},
            )
            notif3 = await create_user_notification(
                session=session,
                recipient_id=u1.id,
                category="inbox_message",
                title="New Anonymous Message",
                content="Someone sent you an anonymous note",
                target_type="inbox",
                target_id="token_xyz",
            )

            # Test unread notification count
            unread_cnt = await get_unread_notification_count(session, recipient_id=u1.id)
            self.assertEqual(unread_cnt, 3)

            # Test list notifications
            notif_list = await list_user_notifications(session, recipient_id=u1.id)
            self.assertEqual(len(notif_list), 3)
            # Most recent first
            self.assertEqual(notif_list[0].id, notif3.id)
            self.assertEqual(notif_list[1].id, notif2.id)
            self.assertEqual(notif_list[2].id, notif1.id)
            # Sender eager loading
            self.assertEqual(notif_list[2].sender.name, "Anna Alumni")

            # Test serialization
            serialized = serialize_user_notification(notif1)
            self.assertEqual(serialized["title"], "Anna replied to your chat")
            self.assertEqual(serialized["target_type"], "chat_message")
            self.assertEqual(serialized["target_id"], "101")
            self.assertFalse(serialized["is_read"])

            # Test mark single notification as read
            mark_ok = await mark_notification_read(session, notification_id=notif3.id, recipient_id=u1.id)
            self.assertTrue(mark_ok)
            unread_cnt_after = await get_unread_notification_count(session, recipient_id=u1.id)
            self.assertEqual(unread_cnt_after, 2)

            # Test mark all notifications read
            marked_total = await mark_all_notifications_read(session, recipient_id=u1.id)
            self.assertEqual(marked_total, 2)
            unread_cnt_final = await get_unread_notification_count(session, recipient_id=u1.id)
            self.assertEqual(unread_cnt_final, 0)

            # Cleanup Have-Need post
            await delete_have_need_post(session, post_id=hn1.id, web_user_id=u1.id)

    async def test_account_deletion_and_data_scrubbing(self) -> None:
        """Verify account deletion scrubs PII, purges inbox, preserves chat/feedbacks as Deleted Account."""
        async with get_db_session() as session:
            # 1. Register a test student user
            email = "del_student@edu.aua.am"
            pwd = "StrongPassword123!"
            otp, _ = await create_web_registration_otp(
                session=session,
                name="John Doe",
                email=email,
                password=pwd,
            )
            ok, u, _ = await verify_web_otp_and_register(session=session, email=email, otp_code=otp)
            self.assertTrue(ok)
            self.assertIsNotNone(u)
            uid = u.id

            # 2. Link Telegram bot & inbox
            tg_id = 99887711
            await get_or_create_user(session, tg_id)
            await get_or_create_user(session, self.tg_uid_1)
            code = await generate_web_sync_code(session, web_user_id=uid)
            linked = await consume_sync_code_and_link_bot(session, code=code, telegram_user_id=tg_id)
            self.assertTrue(linked)

            # Create an anonymous inbox and message for this user
            inbox_token = await create_or_regenerate_inbox(session, tg_id)
            self.assertIsNotNone(inbox_token)
            await record_inbox_message(
                session=session,
                owner_id=tg_id,
                sender_id=self.tg_uid_1,
                message_type="text",
                content_text="Hello before deletion",
            )

            # Create Have-Need post
            c_have = Course(
                semester_label="TestSem 2099",
                school_year_code="2099",
                semester_code="1",
                course_code="DEL101",
                course_title="Deletion Basics",
                section="A",
            )
            c_need = Course(
                semester_label="TestSem 2099",
                school_year_code="2099",
                semester_code="1",
                course_code="DEL102",
                course_title="Advanced Deletion",
                section="B",
            )
            session.add_all([c_have, c_need])
            await session.commit()
            await session.refresh(c_have)
            await session.refresh(c_need)

            hn = await create_have_need_post(
                session=session,
                web_user_id=uid,
                have_course_id=c_have.id,
                need_course_id=c_need.id,
                contact_dm="@johndoe",
                notes="Need section B",
            )

            # Create Chat message & Reaction
            chat_msg = await create_spam_chat_message(
                session=session,
                web_user_id=uid,
                content_html="<b>Important question before deletion</b>",
            )
            await toggle_spam_chat_reaction(
                session=session,
                message_id=chat_msg.id,
                web_user_id=uid,
                emoji="❤️",
            )

            # Create Academic feedback
            fb = await create_academic_feedback(
                session=session,
                web_user_id=uid,
                target_type="course",
                target_key="DEL101",
                target_title="Deletion Basics",
                rating=5,
                comment="Great course!",
                is_anonymous=False,
                push_to_remote=False,
            )

            # Create in-app notification
            await create_user_notification(
                session=session,
                recipient_id=uid,
                category="system",
                title="Welcome to AUA Hub",
                content="Welcome message",
                target_type="general",
            )

            # 3. Perform account deletion / data scrubbing
            success = await delete_or_anonymize_web_user(session, uid)
            self.assertTrue(success)

            # Re-fetch user
            u_after = await get_web_user_by_id(session, uid)
            self.assertIsNotNone(u_after)
            self.assertTrue(u_after.is_deleted)
            self.assertIsNotNone(u_after.deleted_at)
            self.assertEqual(u_after.name, "Deleted Account")
            self.assertTrue(u_after.email.startswith(f"deleted_{uid}_"))
            self.assertEqual(u_after.password_hash, "DELETED")
            self.assertEqual(u_after.avatar_url, "/static/bot-logo.svg")
            self.assertEqual(u_after.bio, "")
            self.assertIsNone(u_after.telegram_user_id)

            # Verify original email is now freed up for registration
            u_original_email = await get_web_user_by_email(session, email)
            self.assertIsNone(u_original_email)

            # Verify login rejection
            auth_result = await authenticate_web_user(session, email, pwd)
            self.assertIsNone(auth_result)

            # Verify Anonymous inbox & messages purged
            inbox_row = (await session.execute(
                select(AnonymousInbox).where(AnonymousInbox.user_id == tg_id)
            )).scalar_one_or_none()
            self.assertIsNone(inbox_row)

            inbox_msgs = (await session.execute(
                select(InboxMessage).where(InboxMessage.owner_id == tg_id)
            )).scalars().all()
            self.assertEqual(len(inbox_msgs), 0)

            # Verify Have-Need listing is deactivated and contact_dm scrubbed
            hn_after = (await session.execute(
                select(HaveNeedPost).where(HaveNeedPost.id == hn.id)
            )).scalar_one_or_none()
            self.assertIsNotNone(hn_after)
            self.assertFalse(hn_after.is_active)
            self.assertEqual(hn_after.contact_dm, "[deleted]")

            # Verify Have-Need serialization displays Deleted Account
            ser_hn = serialize_have_need(hn_after)
            self.assertEqual(ser_hn["author"]["name"], "Deleted Account")
            self.assertEqual(ser_hn["author"]["avatar_url"], "/static/bot-logo.svg")
            self.assertTrue(ser_hn["author"]["is_deleted"])

            # Verify Chat message is PRESERVED, but serialized as Deleted Account
            chat_after = (await session.execute(
                select(SpamChatMessage).where(SpamChatMessage.id == chat_msg.id)
            )).scalar_one_or_none()
            self.assertIsNotNone(chat_after)
            ser_msg = serialize_spam_message(chat_after)
            self.assertEqual(ser_msg["author_name"], "Deleted Account")
            self.assertEqual(ser_msg["author_avatar"], "/static/bot-logo.svg")
            self.assertTrue(ser_msg["is_deleted"])

            # Verify Reactions of deleted user were removed
            reactions_after = (await session.execute(
                select(SpamChatReaction).where(SpamChatReaction.web_user_id == uid)
            )).scalars().all()
            self.assertEqual(len(reactions_after), 0)

            # Verify Feedback is PRESERVED, but author is Deleted Account
            fb_after = (await session.execute(
                select(AcademicFeedback).where(AcademicFeedback.id == fb.id)
            )).scalar_one_or_none()
            self.assertIsNotNone(fb_after)
            ser_fb = serialize_academic_feedback(fb_after)
            self.assertEqual(ser_fb["author_name"], "Deleted Account")
            self.assertEqual(ser_fb["author_avatar"], "/static/bot-logo.svg")
            self.assertTrue(ser_fb["is_deleted"])

            # Verify Notifications for deleted user were purged
            notifs_after = (await session.execute(
                select(UserNotification).where(UserNotification.recipient_id == uid)
            )).scalars().all()
            self.assertEqual(len(notifs_after), 0)

            # Verify second call returns False
            second_del = await delete_or_anonymize_web_user(session, uid)
            self.assertFalse(second_del)

    async def test_jenzareviews_anonymized_writethrough_and_mirror(self) -> None:
        """Verify jenzareviews anonymization strips all PII/user IDs and write-through & serialization work."""
        from datetime import datetime, timedelta, timezone
        from app.services.jenzareviews_sync import (
            _extract_candidate_professor_name,
            _extract_project_ref,
            anonymize_review_payload,
            push_anonymized_review_to_remote,
        )
        from app.database.models import JenzaReview

        # 1. Test anonymize_review_payload strips every user-identifying key
        raw_input = {
            "web_user_id": 42,
            "user_id": "550e8400-e29b-41d4-a716-446655440000",
            "telegram_id": 123456789,
            "telegram_user_id": 123456789,
            "email": "student@edu.aua.am",
            "author_name": "Real Student Name",
            "author_email": "student@edu.aua.am",
            "author_avatar": "/uploads/avatar.png",
            "username": "real_student",
            "ip_address": "192.0.2.1",
            "professor_id": 302,
            "rating": 5,
            "comment": "  Fantastic seminar, learned a lot!  ",
            "course": "CHSS195",
            "would_take_again": True,
        }
        anon = anonymize_review_payload(raw_input)
        self.assertIsNone(anon["user_id"])
        self.assertEqual(anon["professor_id"], 302)
        self.assertEqual(anon["rating"], 5.0)
        self.assertEqual(anon["comment"], "Fantastic seminar, learned a lot!")
        self.assertEqual(anon["course"], "CHSS195")
        self.assertTrue(anon["would_take_again"])
        for forbidden_key in (
            "web_user_id",
            "telegram_id",
            "telegram_user_id",
            "email",
            "author_name",
            "author_email",
            "author_avatar",
            "username",
            "ip_address",
        ):
            self.assertNotIn(forbidden_key, anon)

        # 2. Test Supabase project ref extraction
        self.assertEqual(
            _extract_project_ref("db.yfvshmfkyxcwgyhfhqms.supabase.co", ""),
            "yfvshmfkyxcwgyhfhqms",
        )
        self.assertEqual(
            _extract_project_ref("", "https://yfvshmfkyxcwgyhfhqms.supabase.co"),
            "yfvshmfkyxcwgyhfhqms",
        )

        # 3. Test professor extraction from instructor vs course feedback
        dummy_inst_fb = AcademicFeedback(
            web_user_id=1,
            target_type="instructor",
            target_key="Arsen Bobokhyan",
            target_title="Arsen Bobokhyan",
            target_subtitle="CBE",
            rating=5,
            comment="Great!",
            course_name="CHSS195",
        )
        prof_name, dept, crs = _extract_candidate_professor_name(dummy_inst_fb)
        self.assertEqual(prof_name, "Arsen Bobokhyan")
        self.assertEqual(dept, "CBE")
        self.assertEqual(crs, "CHSS195")

        dummy_staff_fb = AcademicFeedback(
            web_user_id=1,
            target_type="course",
            target_key="CS100",
            target_title="CS100 — Intro",
            target_subtitle="TBA",
            rating=4,
            comment="Good!",
        )
        prof_name2, _, crs2 = _extract_candidate_professor_name(dummy_staff_fb)
        self.assertIsNone(prof_name2)
        self.assertEqual(crs2, "CS100")

        # 4. Test serialize_academic_feedback for remote-mirrored review (web_user_id=None)
        mirrored_fb = AcademicFeedback(
            id=999999,
            web_user_id=None,
            target_type="instructor",
            target_key="Arpi Stepanyan",
            target_title="Arpi Stepanyan",
            target_subtitle="CHSS",
            rating=5,
            comment="Mirrored from jenzareviews",
            is_anonymous=True,
            remote_review_id=777777,
            remote_professor_id=243,
            course_name="ENG101",
            would_take_again=True,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        ser_mirrored = serialize_academic_feedback(mirrored_fb, current_user_id=1)
        self.assertTrue(ser_mirrored["is_anonymous"])
        self.assertFalse(ser_mirrored["is_owner"])
        self.assertIsNone(ser_mirrored["author_id"])
        self.assertEqual(ser_mirrored["author_name"], "Anonymous AUA Student")
        self.assertEqual(ser_mirrored["course_name"], "ENG101")
        self.assertTrue(ser_mirrored["would_take_again"])

        # 5. Test write-through with mocked remote asyncpg connection verifying user_id=None in SQL args
        async with get_db_session() as session:
            local_fb = AcademicFeedback(
                web_user_id=None,
                target_type="instructor",
                target_key="Arsen Bobokhyan",
                target_title="Arsen Bobokhyan",
                target_subtitle="CHSS",
                rating=5,
                comment="Unit test write-through verification",
                is_anonymous=False,
                remote_professor_id=302,
                course_name="CHSS195",
                would_take_again=True,
            )
            session.add(local_fb)
            await session.commit()

            mock_remote_conn = AsyncMock()
            # First fetchrow checks for existing duplicate (returns None), second fetchrow is INSERT RETURNING
            mock_remote_conn.fetchrow = AsyncMock(
                side_effect=[
                    None,
                    {"id": 999888777, "created_at": datetime.now(timezone.utc)},
                ]
            )

            pushed_id = await push_anonymized_review_to_remote(
                session, local_fb, remote_conn=mock_remote_conn
            )
            self.assertEqual(pushed_id, 999888777)
            self.assertEqual(local_fb.remote_review_id, 999888777)

            # Verify the INSERT call passed user_id=None as the first parameter ($1)
            insert_call_args = mock_remote_conn.fetchrow.call_args_list[1][0]
            self.assertIn("INSERT INTO public.reviews", insert_call_args[0])
            self.assertIsNone(insert_call_args[1])  # $1 = user_id IS NULL
            self.assertEqual(insert_call_args[2], 302)  # $2 = professor_id
            self.assertEqual(insert_call_args[3], 5.0)  # $3 = rating
            self.assertEqual(insert_call_args[4], "Unit test write-through verification")

            # Clean up test row and mirror row
            await session.execute(delete(JenzaReview).where(JenzaReview.id == 999888777))
            await session.execute(delete(AcademicFeedback).where(AcademicFeedback.id == local_fb.id))
            await session.commit()

    async def test_ai_assistant_rag_tools_and_session_limit(self) -> None:
        """Verify AI Assistant tool routing, Cloudflare email decoding, DB RAG tools, and strict 5-prompt session limit."""
        from app.services.ai_agent import (
            AISessionStore,
            AUAAssistantOrchestrator,
            MAX_PROMPTS_PER_SESSION,
            classify_tools_heuristic,
            decode_cfemails_in_html,
            extract_person_name_candidate,
            load_or_sync_ai_session,
            reset_ai_session_persistent,
            search_course_and_professor_feedbacks,
        )

        # 1. Verify Cloudflare email de-obfuscation
        cf_sample = '<a href="/cdn-cgi/l/email-protection" class="__cf_email__" data-cfemail="f3929b92999a929db3928692dd929e">[email&#160;protected]</a>'
        self.assertEqual(decode_cfemails_in_html(cf_sample), "ahajian@aua.am")

        # 2. Verify intent routing & candidate person extraction
        self.assertEqual(
            extract_person_name_candidate("How is Professor Aram Hajian's grading policy?"),
            "Aram Hajian",
        )
        plan1 = classify_tools_heuristic("How is Professor Aram Hajian's grading policy?")
        self.assertTrue(plan1.use_feedbacks)
        self.assertTrue(plan1.use_directory)

        plan2 = classify_tools_heuristic("Are there any upcoming events or masterclasses at AUA this week?")
        self.assertTrue(plan2.use_newsroom_events)

        plan3 = classify_tools_heuristic("Find student articles in The Highlander about clubs")
        self.assertTrue(plan3.use_highlander)

        plan4 = classify_tools_heuristic("Find discussions about CS courses in chat")
        self.assertTrue(plan4.use_chat_history)

        # 3. Verify 5-prompt session limit and reset
        store = AISessionStore()
        st = store.get_or_create("test_session_123")
        self.assertEqual(st.prompts_used, 0)

        orchestrator = AUAAssistantOrchestrator(api_key="")  # Deterministic fallback mode
        async with get_db_session() as session:
            # Test DB feedback retrieval directly
            fb_results, _ = await search_course_and_professor_feedbacks(
                session, "course feedback grading"
            )
            self.assertIsInstance(fb_results, list)

            # Execute 5 prompts in the session (using internal chat/feedback query to avoid slow external calls)
            for i in range(1, MAX_PROMPTS_PER_SESSION + 1):
                resp = await orchestrator.answer_prompt(
                    db_session=session,
                    user_prompt=f"Find discussions about CS courses in chat #{i}",
                    chat_state=st,
                )
                self.assertTrue(resp["ok"])
                self.assertEqual(resp["prompts_used"], i)
                self.assertEqual(resp["max_prompts"], 5)
                self.assertEqual(len(resp["history"]), i)
                self.assertEqual(
                    resp["history"][-1]["prompt"],
                    f"Find discussions about CS courses in chat #{i}",
                )
                self.assertTrue(resp["history"][-1]["answer"])
                if i < 5:
                    self.assertFalse(resp["limit_reached"])
                else:
                    self.assertTrue(resp["limit_reached"])

        # Verify session history persistence and restoration from client_history
        saved_history = st.get_history_payload()
        self.assertEqual(len(saved_history), 5)

        restored_st = store.get_or_create("restored_session_after_restart")
        self.assertEqual(restored_st.prompts_used, 0)
        restored_st.restore_from_client_history(saved_history[:3])
        self.assertEqual(restored_st.prompts_used, 3)
        self.assertEqual(len(restored_st.get_history_payload()), 3)
        self.assertEqual(len(restored_st.turns), 6)

        # Reset session -> prompts_used back to 0 and history cleared
        fresh_st = store.reset_session("test_session_123")
        self.assertNotEqual(fresh_st.session_id, "test_session_123")
        self.assertEqual(fresh_st.prompts_used, 0)
        self.assertEqual(fresh_st.get_history_payload(), [])

        # 4. Verify cross-device (Mobile <-> Desktop) DB-backed synchronization for same WebUser account
        async with get_db_session() as session:
            await session.execute(delete(WebUser).where(WebUser.email == "sync_student@edu.aua.am"))
            await session.commit()
            otp_m, _ = await create_web_registration_otp(
                session=session,
                name="Sync Student",
                email="sync_student@edu.aua.am",
                password="StrongPassword123!",
            )
            ok_m, sync_user, _ = await verify_web_otp_and_register(
                session=session,
                email="sync_student@edu.aua.am",
                otp_code=otp_m,
            )
            self.assertTrue(ok_m)
            self.assertIsNotNone(sync_user)

            # Mobile sends a prompt
            mobile_state, _ = await load_or_sync_ai_session(
                db_session=session,
                session_id="mobile_local_sid",
                web_user_id=sync_user.id,
            )
            await orchestrator.answer_prompt(
                db_session=session,
                user_prompt="Find discussions about CS courses in chat from mobile",
                chat_state=mobile_state,
                web_user_id=sync_user.id,
            )

            # Desktop loads session with its own different local session_id -> receives Mobile's prompt & response!
            desktop_state, _ = await load_or_sync_ai_session(
                db_session=session,
                session_id="desktop_local_sid",
                web_user_id=sync_user.id,
            )
            self.assertEqual(desktop_state.session_id, mobile_state.session_id)
            self.assertEqual(desktop_state.prompts_used, 1)
            self.assertEqual(len(desktop_state.get_history_payload()), 1)
            self.assertEqual(
                desktop_state.get_history_payload()[0]["prompt"],
                "Find discussions about CS courses in chat from mobile",
            )

            # Pressing New Chat on Desktop resets both Desktop and Mobile
            reset_state = await reset_ai_session_persistent(
                db_session=session,
                old_session_id=desktop_state.session_id,
                web_user_id=sync_user.id,
            )
            self.assertEqual(reset_state.prompts_used, 0)
            self.assertEqual(reset_state.get_history_payload(), [])

            # Even if Mobile tries to sync its pre-reset client_history, reset_at filters it out
            mobile_after_reset, _ = await load_or_sync_ai_session(
                db_session=session,
                session_id=mobile_state.session_id,
                web_user_id=sync_user.id,
                client_history=desktop_state.get_history_payload(),
            )
            self.assertEqual(mobile_after_reset.prompts_used, 0)
            self.assertEqual(mobile_after_reset.get_history_payload(), [])
            await session.execute(delete(WebUser).where(WebUser.id == sync_user.id))
            await session.commit()

    async def test_ai_assistant_transliteration_followup_and_source_filtering(self) -> None:
        """Verify Armenian -yan/-ian transliteration, stopword safety, follow-up resolution, and zero random instructor sources."""
        from app.services.ai_agent import (
            AIChatSessionState,
            AIChatTurn,
            AUAAssistantOrchestrator,
            classify_tools_heuristic,
            extract_person_name_candidate,
            search_course_and_professor_feedbacks,
        )

        self.assertEqual(
            extract_person_name_candidate("are you sure there is no student feedbacks about Apig ?"),
            "Apig",
        )

        # Follow-up single-name & pronoun resolution from conversation turns
        prior_turns = [
            AIChatTurn(role="user", content="Tell me about Apig Aramyan"),
            AIChatTurn(role="assistant", content="Apig Aramian is an Adjunct Lecturer in ACSE."),
        ]
        plan_single = classify_tools_heuristic(
            "are you sure there is no student feedbacks about Apig ?",
            prior_turns=prior_turns,
        )
        self.assertEqual(plan_single.search_query_override, "Apig Aramyan")
        self.assertTrue(plan_single.use_feedbacks)
        self.assertTrue(plan_single.use_directory)

        plan_pronoun = classify_tools_heuristic(
            "what courses does he teach?",
            prior_turns=prior_turns,
        )
        self.assertEqual(plan_pronoun.search_query_override, "Apig Aramyan")
        self.assertTrue(plan_pronoun.use_feedbacks)

        async with get_db_session() as session:
            # 1. Query using -yan spelling ("Apig Aramyan") finds Apig Aramian & both reviews, no random profs
            res_yan, src_yan = await search_course_and_professor_feedbacks(
                session, "Tell me about Apig Aramyan and student feedbacks about him"
            )
            prof_summaries = [r for r in res_yan if r.get("type") == "professor_feedback_summary"]
            self.assertEqual([r["professor_name"] for r in prof_summaries], ["Apig Aramian"])
            self.assertEqual(prof_summaries[0]["review_count"], 2)
            self.assertEqual([s["title"] for s in src_yan], ["Feedbacks: Apig Aramian"])

            # 2. Conversational stopword-heavy follow-up query must still match Apig Aramian cleanly
            res_q2, src_q2 = await search_course_and_professor_feedbacks(
                session, "are you sure there is no student feedbacks about Apig ?"
            )
            prof_q2 = [r for r in res_q2 if r.get("type") == "professor_feedback_summary"]
            self.assertEqual([r["professor_name"] for r in prof_q2], ["Apig Aramian"])
            self.assertEqual([s["title"] for s in src_q2], ["Feedbacks: Apig Aramian"])

            # 3. Unmatched First+Last name with a shared Armenian first name must NOT pull wrong surnames
            res_fake, src_fake = await search_course_and_professor_feedbacks(
                session, "Tell me about Aram ZzzNonexistent"
            )
            self.assertEqual(res_fake, [])
            self.assertEqual(src_fake, [])

            # 4. Deterministic orchestrator follow-up with pronoun carries entity context into DB search
            orch = AUAAssistantOrchestrator(api_key="")
            st = AIChatSessionState(session_id="followup_test_sid", turns=list(prior_turns))
            # Turn off directory URL fetch by verifying heuristic+feedback retrieval directly via pronoun query
            # Wait: call answer_prompt without external hang and clean AIUsageLog
            ans = await orch.answer_prompt(
                db_session=session,
                user_prompt="what courses and feedbacks exist for him?",
                chat_state=st,
            )
            self.assertTrue(ans["ok"])
            self.assertIn("Apig Aramian", ans["answer"])
            fb_titles = [s["title"] for s in ans["sources"] if str(s.get("title", "")).startswith("Feedbacks:")]
            self.assertEqual(fb_titles, ["Feedbacks: Apig Aramian"])
            await session.execute(delete(AIUsageLog).where(AIUsageLog.session_id == "followup_test_sid"))
            await session.commit()

    async def test_ai_assistant_and_endpoint_security_hardening(self) -> None:
        """Verify privacy isolation, XSS/link sanitization, anti-spoofing, CSRF, SSRF, and concurrency caps."""
        from app.services.ai_agent import (
            AIChatSessionState,
            MAX_PROMPTS_PER_SESSION,
            _is_allowed_ai_fetch_url,
            _sanitize_ai_markdown,
            _sanitize_source_list,
            ai_orchestrator,
            ai_session_store,
            load_or_sync_ai_session,
            search_anonymous_posts_and_inbox,
        )
        from app.web.server import create_web_app, get_client_ip

        reset_rate_limits()

        # 1. URL / Markdown XSS / SSRF allowlist validation
        self.assertTrue(_is_allowed_ai_fetch_url("https://people.aua.am/team_member/apig-aramian/"))
        self.assertTrue(_is_allowed_ai_fetch_url("https://directory.aua.am/"))
        self.assertFalse(_is_allowed_ai_fetch_url("http://127.0.0.1:8000/api/admin/stata"))
        self.assertFalse(_is_allowed_ai_fetch_url("https://169.254.169.254/latest/meta-data/"))
        self.assertFalse(_is_allowed_ai_fetch_url("https://evil.com/people.aua.am"))
        self.assertFalse(_is_allowed_ai_fetch_url("http://newsroom.aua.am/events/"))

        sanitized_sources = _sanitize_source_list(
            [
                {"title": "XSS", "url": "javascript:alert(1)", "source_type": "Bad"},
                {"title": "Breakout", "url": "#chat');alert(1);//", "source_type": "Bad"},
                {"title": "Logger", "url": "https://grabify.link/abc123", "source_type": "Bad"},
                {"title": "Executable", "url": "https://example.com/payload.apk", "source_type": "Bad"},
                {"title": "ValidInternal", "url": "#feedbacks", "source_type": "Feedbacks"},
                {"title": "ValidAUA", "url": "https://newsroom.aua.am/events/", "source_type": "Events"},
            ]
        )
        self.assertEqual([s["title"] for s in sanitized_sources], ["ValidInternal", "ValidAUA"])

        cleaned_md = _sanitize_ai_markdown(
            "Click [here](javascript:alert(1)) or [bad](https://grabify.link/x) or [ok](https://newsroom.aua.am/)"
        )
        self.assertNotIn("javascript:", cleaned_md.lower())
        self.assertNotIn("grabify.link", cleaned_md.lower())
        self.assertIn("https://newsroom.aua.am/", cleaned_md)

        # Future-dated forged history timestamp must not outlive resets
        st_hist = AIChatSessionState(session_id="hist_sec_check")
        st_hist.restore_from_client_history(
            [
                {
                    "prompt": "p1",
                    "answer": "a1 [x](javascript:alert(1))",
                    "sources": [{"title": "XSS", "url": "javascript:alert(1)"}],
                    "timestamp": "2099-01-01T00:00:00+00:00",
                }
            ]
        )
        self.assertEqual(len(st_hist.exchanges), 1)
        self.assertEqual(st_hist.exchanges[0]["sources"], [])
        self.assertNotIn("javascript:", st_hist.exchanges[0]["answer"].lower())
        self.assertFalse(st_hist.exchanges[0]["timestamp"].startswith("2099"))

        # 2. X-Forwarded-For spoofing vs trusted Cloudflare Tunnel IP extraction
        untrusted_req = make_mocked_request(
            "GET",
            "/api/ai/session",
            headers={"X-Forwarded-For": "8.8.8.8", "CF-Connecting-IP": "1.1.1.1"},
        )
        # Override transport peername to simulate direct public client IP
        untrusted_req_patch = patch.object(
            type(untrusted_req), "remote", new="203.0.113.9"
        )
        with untrusted_req_patch:
            self.assertEqual(get_client_ip(untrusted_req), "203.0.113.9")

        trusted_req = make_mocked_request(
            "GET",
            "/api/ai/session",
            headers={
                "X-Forwarded-For": "8.8.8.8, 198.51.100.42",
                "CF-Connecting-IP": "198.51.100.42",
            },
        )
        with patch.object(type(trusted_req), "remote", new="127.0.0.1"):
            self.assertEqual(get_client_ip(trusted_req), "198.51.100.42")

        # 3. Private Secret Inbox DMs MUST NEVER leak into AI RAG retrieval
        sec_user_id = None
        async with get_db_session() as session:
            tg_user = await get_or_create_user(session, 987654321, "inboxsec")
            inbox = await create_or_regenerate_inbox(session, tg_user.id)
            await record_inbox_message(
                session,
                owner_id=tg_user.id,
                sender_id=tg_user.id,
                content_text="super_secret_inbox_dm_xyz confidential text",
            )
            rag_rows, _ = await search_anonymous_posts_and_inbox(
                session, "super_secret_inbox_dm_xyz confidential"
            )
            self.assertFalse(
                any("super_secret_inbox_dm_xyz" in str(r) for r in rag_rows),
                f"Private InboxMessage leaked into AI context: {rag_rows}",
            )
            await session.execute(delete(InboxMessage).where(InboxMessage.owner_id == tg_user.id))
            await session.execute(delete(AnonymousInbox).where(AnonymousInbox.id == inbox.id))
            await session.execute(delete(User).where(User.id == tg_user.id))

            # Prepare an authenticated WebUser for HTTP endpoint security tests
            await session.execute(delete(WebUser).where(WebUser.email == "sec_tester@edu.aua.am"))
            await session.commit()
            otp_s, _ = await create_web_registration_otp(
                session=session,
                name="Sec Tester",
                email="sec_tester@edu.aua.am",
                password="StrongPassword123!",
            )
            ok_s, sec_user, _ = await verify_web_otp_and_register(
                session=session, email="sec_tester@edu.aua.am", otp_code=otp_s
            )
            self.assertTrue(ok_s)
            sec_user_id = sec_user.id

            # Seed an authenticated AI session turn
            auth_state, _ = await load_or_sync_ai_session(
                db_session=session,
                session_id="initial_seed_sid",
                web_user_id=sec_user_id,
            )
            auth_sid = auth_state.session_id
            auth_state.exchanges = [
                {
                    "prompt": "Secret private user prompt",
                    "answer": "Private response",
                    "steps": [],
                    "sources": [],
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            ]
            auth_state.prompts_used = 1
            from app.services.ai_agent import persist_ai_session

            await persist_ai_session(session, sec_user_id, auth_state)

        app = create_web_app()
        async with TestClient(TestServer(app)) as client:
            auth_token = create_jwt_token(sec_user_id)

            # 4a. Unauthenticated callers are strictly rejected (401) across all AI endpoints & cannot probe sessions
            for probe_sid in (f"user_{sec_user_id}", auth_sid):
                r_probe = await client.get(f"/api/ai/session?session_id={probe_sid}")
                self.assertEqual(r_probe.status, 401)
            r_unauth_sess_post = await client.post("/api/ai/session", json={"session_id": auth_sid})
            self.assertEqual(r_unauth_sess_post.status, 401)
            r_unauth_chat = await client.post("/api/ai/chat", json={"prompt": "Hello AI"})
            self.assertEqual(r_unauth_chat.status, 401)
            r_unauth_reset = await client.post("/api/ai/reset", json={"session_id": auth_sid})
            self.assertEqual(r_unauth_reset.status, 401)

            # 4b. CSRF check blocks `Origin: null` on state-changing POST
            r_csrf = await client.post(
                "/api/ai/reset",
                json={},
                headers={"Origin": "null"},
            )
            self.assertEqual(r_csrf.status, 403)

            # 4b-2. Cross-Site WebSocket Hijacking (CSWSH) Origin check blocks foreign Origin
            import aiohttp

            with self.assertRaises(aiohttp.WSServerHandshakeError) as ws_err:
                await client.ws_connect("/ws/chat", headers={"Origin": "https://evil.example"})
            self.assertEqual(ws_err.exception.status, 403)

            # 4c. Malicious IP-logger link rejected before AI call (signed-in)
            r_bad_url = await client.post(
                "/api/ai/chat",
                json={"prompt": "Check https://grabify.link/abc right now"},
                headers={"Authorization": f"Bearer {auth_token}"},
            )
            self.assertEqual(r_bad_url.status, 400)

            # 4d. Forged client_history on an existing authenticated DB session cannot inject assistant answers
            r_sync = await client.post(
                "/api/ai/session",
                json={
                    "session_id": auth_sid,
                    "client_history": [
                        {
                            "prompt": "Fake Q",
                            "answer": "Fake injected assistant answer",
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }
                    ],
                },
                headers={"Authorization": f"Bearer {auth_token}"},
            )
            self.assertEqual(r_sync.status, 200)
            d_sync = await r_sync.json()
            self.assertEqual(len(d_sync["history"]), 1)
            self.assertEqual(d_sync["history"][0]["prompt"], "Secret private user prompt")

            # 4e. Parallel `/api/ai/chat` burst strictly respects 5-prompt cap (no TOCTOU bypass)
            async def _fake_answer_prompt(db_session, user_prompt, chat_state, web_user_id=None):
                await asyncio.sleep(0.05)
                chat_state.prompts_used += 1
                now_iso = datetime.now(timezone.utc).isoformat()
                chat_state.exchanges.append(
                    {
                        "prompt": user_prompt,
                        "answer": f"Reply {chat_state.prompts_used}",
                        "steps": [],
                        "sources": [],
                        "timestamp": now_iso,
                    }
                )
                await persist_ai_session(db_session, web_user_id, chat_state)
                return {
                    "ok": True,
                    "session_id": chat_state.session_id,
                    "prompts_used": chat_state.prompts_used,
                    "max_prompts": MAX_PROMPTS_PER_SESSION,
                    "limit_reached": chat_state.prompts_used >= MAX_PROMPTS_PER_SESSION,
                    "answer": f"Reply {chat_state.prompts_used}",
                    "steps": [],
                    "sources": [],
                    "history": chat_state.get_history_payload(),
                }

            # User already has 1 prompt used -> only 4 more allowed out of 6 concurrent requests
            with patch.object(ai_orchestrator, "answer_prompt", side_effect=_fake_answer_prompt):
                responses = await asyncio.gather(
                    *[
                        client.post(
                            "/api/ai/chat",
                            json={"prompt": f"Concurrent Q{i}", "session_id": auth_sid},
                            headers={"Authorization": f"Bearer {auth_token}"},
                        )
                        for i in range(6)
                    ]
                )
            statuses = sorted([r.status for r in responses])
            self.assertEqual(statuses, [200, 200, 200, 200, 429, 429])

        # Cleanup DB artifacts & reset in-memory buckets
        async with get_db_session() as session:
            if sec_user_id is not None:
                await session.execute(
                    delete(WebUserAIChatSession).where(WebUserAIChatSession.web_user_id == sec_user_id)
                )
                await session.execute(delete(WebUser).where(WebUser.id == sec_user_id))
                await session.commit()
        ai_session_store._sessions.clear()
        ai_session_store._user_sessions.clear()
        reset_rate_limits()

    async def test_spam_chat_telegram_unread_cursor_api(self) -> None:
        """Verify Campus Chat API returns last_read_message_id, first_unread_message_id, and partial remaining unread counts."""
        from app.database.models import SpamChatRead
        from app.web.server import create_web_app

        reset_rate_limits()
        uid_a = uid_b = None
        msg_ids = []
        async with get_db_session() as session:
            await session.execute(delete(WebUser).where(WebUser.email.in_(["chat_a@edu.aua.am", "chat_b@edu.aua.am"])))
            await session.commit()

            otp_a, _ = await create_web_registration_otp(session, "Chat Author", "chat_a@edu.aua.am", "Pass1234!")
            _, u_a, _ = await verify_web_otp_and_register(session, "chat_a@edu.aua.am", otp_a)
            otp_b, _ = await create_web_registration_otp(session, "Chat Reader", "chat_b@edu.aua.am", "Pass1234!")
            _, u_b, _ = await verify_web_otp_and_register(session, "chat_b@edu.aua.am", otp_b)
            uid_a, uid_b = u_a.id, u_b.id

            m1 = await create_spam_chat_message(session, web_user_id=uid_a, content_html="Read baseline msg")
            await mark_spam_chat_read(session, web_user_id=uid_b, latest_message_id=m1.id)
            m2 = await create_spam_chat_message(session, web_user_id=uid_a, content_html="Unread msg #1")
            m3 = await create_spam_chat_message(session, web_user_id=uid_a, content_html="Unread msg #2")
            msg_ids = [m1.id, m2.id, m3.id]

        app = create_web_app()
        tok_b = create_jwt_token(uid_b)
        async with TestClient(TestServer(app)) as client:
            # 1. Guest gets zero unread / null anchor cleanly
            r_guest = await client.get("/api/spam-chat/messages")
            self.assertEqual(r_guest.status, 200)
            d_guest = await r_guest.json()
            self.assertEqual(d_guest["unread_count"], 0)
            self.assertEqual(d_guest["last_read_message_id"], 0)
            self.assertIsNone(d_guest["first_unread_message_id"])

            # 2. Signed-in reader receives last_read_message_id=m1, first_unread_message_id=m2, unread_count=2
            r_list = await client.get("/api/spam-chat/messages", headers={"Authorization": f"Bearer {tok_b}"})
            self.assertEqual(r_list.status, 200)
            d_list = await r_list.json()
            self.assertEqual(d_list["last_read_message_id"], msg_ids[0])
            self.assertEqual(d_list["first_unread_message_id"], msg_ids[1])
            self.assertGreaterEqual(d_list["unread_count"], 2)
            returned_ids = [int(m["id"]) for m in d_list["messages"]]
            for mid in msg_ids:
                self.assertIn(mid, returned_ids)

            # 3. Partial scroll read up to m2 leaves m3 unread
            r_part = await client.post(
                "/api/spam-chat/read",
                json={"latest_message_id": msg_ids[1]},
                headers={"Authorization": f"Bearer {tok_b}"},
            )
            self.assertEqual(r_part.status, 200)
            d_part = await r_part.json()
            self.assertEqual(d_part["last_read_message_id"], msg_ids[1])
            self.assertGreaterEqual(d_part["unread_count"], 1)

            # 4. Reading up to m3 clears remaining unread from m1..m3
            r_full = await client.post(
                "/api/spam-chat/read",
                json={"latest_message_id": msg_ids[2]},
                headers={"Authorization": f"Bearer {tok_b}"},
            )
            self.assertEqual(r_full.status, 200)
            d_full = await r_full.json()
            self.assertEqual(d_full["last_read_message_id"], msg_ids[2])
            self.assertEqual(d_full["unread_count"], 0)

            r_after = await client.get("/api/spam-chat/messages", headers={"Authorization": f"Bearer {tok_b}"})
            d_after = await r_after.json()
            self.assertEqual(d_after["unread_count"], 0)
            self.assertIsNone(d_after["first_unread_message_id"])

        async with get_db_session() as session:
            await session.execute(delete(SpamChatMessage).where(SpamChatMessage.id.in_(msg_ids)))
            await session.execute(delete(SpamChatRead).where(SpamChatRead.web_user_id.in_([uid_a, uid_b])))
            await session.execute(delete(WebUser).where(WebUser.id.in_([uid_a, uid_b])))
            await session.commit()
        reset_rate_limits()

    async def test_main_page_performance_compression_and_cache_headers(self) -> None:
        """Verify main-page asset optimization, immutable versioned caching, and gzip JSON compression."""
        from app.web.server import create_web_app

        app = create_web_app()
        async with TestClient(TestServer(app)) as client:
            # 1. Versioned static bundle gets immutable cache header & dropdown stacking + auth reset & Google OAuth styles
            r_css = await client.get("/static/styles.css?v=20261115_1")
            self.assertEqual(r_css.status, 200)
            self.assertIn("max-age=604800", r_css.headers.get("Cache-Control", ""))
            css_body = await r_css.text()
            self.assertIn("@media (hover: none), (pointer: coarse), (max-width: 1024px)", css_body)
            self.assertNotIn("rgba(6, 10, 20, 0.98)", css_body)
            self.assertIn("linear-gradient(170deg, rgba(11, 20, 42, 0.80) 0%, rgba(7, 13, 28, 0.88) 100%)", css_body)
            self.assertIn(".fb-filter-bar", css_body)
            self.assertIn(".glass-panel:has(.course-dropdown.open)", css_body)
            self.assertIn(".auth-inline-link", css_body)
            self.assertIn(".btn-google-oauth", css_body)
            self.assertIn(".auth-turnstile-slot", css_body)
            self.assertIn(".ai-auth-gate", css_body)
            self.assertIn("minmax(265px, 1fr)", css_body)

            # 2. Index HTML remains uncached & serves lightweight PNG partner logos + async fonts + forgot password & Google OAuth UI
            r_home = await client.get("/")
            self.assertEqual(r_home.status, 200)
            self.assertIn("no-cache", r_home.headers.get("Cache-Control", ""))
            csp = r_home.headers.get("Content-Security-Policy", "")
            self.assertIn("https://challenges.cloudflare.com", csp)
            self.assertIn("frame-src", csp)
            home_html = await r_home.text()
            self.assertIn("/static/partners/jenzareviews-logo.png", home_html)
            self.assertIn("/static/partners/auautils-logo.png", home_html)
            self.assertIn("https://chromewebstore.google.com/detail/aua-utils/echkbfkpejnpbjfmjoagfijakbnbglgp", home_html)
            self.assertIn('id="aiAuthGateCard"', home_html)
            self.assertIn("Sign In to Use", home_html)
            self.assertIn('id="googleFontsCss"', home_html)
            self.assertIn('class="glass-panel fb-filter-bar"', home_html)
            self.assertIn('id="forgotForm"', home_html)
            self.assertIn('id="resetPasswordForm"', home_html)
            self.assertIn('id="googleSignInBtn"', home_html)
            self.assertIn('id="turnstileSlot_login"', home_html)
            self.assertIn('id="turnstileSlot_register"', home_html)
            self.assertIn('id="turnstileSlot_forgot"', home_html)
            self.assertIn("Please use your <b>@edu.aua.am</b> or <b>@alumni.aua.am</b> university Google account.", home_html)
            self.assertNotIn("ai-suggestions-grid", home_html)

            # 3. Optimized partner PNG assets serve cleanly
            for asset in (
                "/static/partners/jenzareviews-logo.png",
                "/static/partners/findaua-logo.png",
                "/static/partners/auautils-logo.png",
            ):
                r_img = await client.get(asset)
                self.assertEqual(r_img.status, 200)
                self.assertLess(len(await r_img.read()), 30_000)

            # 4. Large JSON payloads compress with gzip when supported
            r_courses = await client.get("/api/courses?limit=700", headers={"Accept-Encoding": "gzip"})
            self.assertEqual(r_courses.status, 200)
            raw_json = await r_courses.read()
            if len(raw_json) >= 1024:
                self.assertEqual(r_courses.headers.get("Content-Encoding"), "gzip")

    async def test_ai_token_budget_rate_bucket_and_guest_flood_shield(self) -> None:
        """Verify rate-bucket window pruning, guest flood caps, Gemini token/concurrency caps, and global DB budget circuit breaker."""
        from sqlalchemy import func

        from app.config import settings
        from app.services.ai_agent import (
            AIChatSessionState,
            AUAAssistantOrchestrator,
            ToolSelectionPlan,
            ai_orchestrator,
            check_global_ai_gemini_budget,
        )
        from app.web.server import _RATE_BUCKETS, create_web_app

        # 1. Rate-limit cleanup with >5000 keys must NOT prematurely evict longer-window hourly buckets
        reset_rate_limits()
        fake_now = [10000.0]
        with patch("app.web.server.time.monotonic", side_effect=lambda: fake_now[0]):
            ok_hr, _ = check_rate_limit("hourly_bucket", "target_ip", max_calls=1, window_sec=3600)
            self.assertTrue(ok_hr)
            fake_now[0] += 120.0  # 2 minutes later: hourly bucket is STILL valid (3600s window)
            for idx in range(5005):
                check_rate_limit("short_noise", f"noise_{idx}", max_calls=5, window_sec=60)
            self.assertIn("hourly_bucket:target_ip", _RATE_BUCKETS)
            blocked_hr, _ = check_rate_limit("hourly_bucket", "target_ip", max_calls=1, window_sec=3600)
            self.assertFalse(blocked_hr)
        reset_rate_limits()

        # 2. Gemini GenerateContentConfig includes explicit max_output_tokens, ThinkingConfig, and semaphore cap
        observed_configs = []
        active_calls = 0
        max_active_calls = 0

        async def _mock_generate_content(*, model, contents, config):
            nonlocal active_calls, max_active_calls
            observed_configs.append(config)
            active_calls += 1
            max_active_calls = max(max_active_calls, active_calls)
            await asyncio.sleep(0.03)
            active_calls -= 1
            resp = MagicMock()
            if getattr(config, "response_mime_type", None) == "application/json":
                resp.text = ToolSelectionPlan(use_feedbacks=True, search_query_override="Aram Hajian").model_dump_json()
            else:
                resp.text = "Bounded grounded response."
            return resp

        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(side_effect=_mock_generate_content)

        # Clean slate for synthetic budget test logs
        test_sid_prefix = "budget_test_"
        try:
            async with get_db_session() as session:
                await session.execute(delete(AIUsageLog).where(AIUsageLog.session_id.like(f"{test_sid_prefix}%")))
                await session.commit()

                now_utc = datetime.now(timezone.utc)
                base_1h = (
                    await session.execute(
                        select(func.count(AIUsageLog.id)).where(
                            AIUsageLog.used_gemini.is_(True),
                            AIUsageLog.created_at >= now_utc - timedelta(hours=1),
                        )
                    )
                ).scalar_one() or 0
                base_24h = (
                    await session.execute(
                        select(func.count(AIUsageLog.id)).where(
                            AIUsageLog.used_gemini.is_(True),
                            AIUsageLog.created_at >= now_utc - timedelta(hours=24),
                        )
                    )
                ).scalar_one() or 0

                # Test semaphore concurrency cap across 5 simultaneous Gemini calls with limit=2
                orch = AUAAssistantOrchestrator(api_key="test-key-present")
                orch._client = mock_client
                with patch.object(settings, "AI_MAX_CONCURRENT_GEMINI_CALLS", 2):
                    # Clear loop semaphore cache for isolated limit=2 check
                    from app.services import ai_agent as ai_mod

                    ai_mod._GEMINI_SEMAPHORES.clear()
                    await asyncio.gather(
                        *[
                            orch._generate_gemini_content(mock_client, f"prompt {i}", MagicMock(), 4.0)
                            for i in range(5)
                        ]
                    )
                    ai_mod._GEMINI_SEMAPHORES.clear()
                self.assertLessEqual(max_active_calls, 2)

                # Reset mock observations & verify full answer_prompt + global circuit breaker
                mock_client.aio.models.generate_content.reset_mock()
                observed_configs.clear()

                with (
                    patch.object(settings, "AI_GLOBAL_HOURLY_PROMPT_LIMIT", int(base_1h) + 1),
                    patch.object(settings, "AI_GLOBAL_DAILY_PROMPT_LIMIT", int(base_24h) + 10),
                ):
                    ok_before, reason_before = await check_global_ai_gemini_budget(session)
                    self.assertTrue(ok_before, reason_before)

                    st1 = AIChatSessionState(session_id=f"{test_sid_prefix}1")
                    res1 = await orch.answer_prompt(
                        db_session=session,
                        user_prompt="Tell me about CS courses and grading",
                        chat_state=st1,
                    )
                    self.assertTrue(res1["ok"])
                    self.assertEqual(mock_client.aio.models.generate_content.call_count, 2)
                    self.assertEqual(len(observed_configs), 2)
                    self.assertEqual(observed_configs[0].max_output_tokens, settings.AI_PLANNER_MAX_OUTPUT_TOKENS)
                    self.assertEqual(observed_configs[1].max_output_tokens, settings.AI_SYNTHESIS_MAX_OUTPUT_TOKENS)
                    self.assertIsNotNone(observed_configs[0].thinking_config)
                    self.assertEqual(observed_configs[0].thinking_config.thinking_budget, settings.AI_PLANNER_THINKING_BUDGET)
                    self.assertEqual(observed_configs[1].thinking_config.thinking_budget, settings.AI_SYNTHESIS_THINKING_BUDGET)

                    # Second call hits hourly circuit breaker -> ZERO Gemini calls, offline RAG answer still served cleanly!
                    ok_after, reason_after = await check_global_ai_gemini_budget(session)
                    self.assertFalse(ok_after)
                    self.assertEqual(reason_after, "hourly")

                    mock_client.aio.models.generate_content.reset_mock()
                    st2 = AIChatSessionState(session_id=f"{test_sid_prefix}2")
                    res2 = await orch.answer_prompt(
                        db_session=session,
                        user_prompt="Tell me about CS courses and grading",
                        chat_state=st2,
                    )
                    self.assertTrue(res2["ok"])
                    self.assertTrue(res2["answer"])
                    self.assertEqual(mock_client.aio.models.generate_content.call_count, 0)

                    log2 = (
                        await session.execute(
                            select(AIUsageLog).where(AIUsageLog.session_id == f"{test_sid_prefix}2")
                        )
                    ).scalar_one()
                    self.assertFalse(log2.used_gemini)

                    ok_daily, reason_daily = await check_global_ai_gemini_budget(
                        session, hourly_limit=9999, daily_limit=int(base_24h) + 1
                    )
                    self.assertFalse(ok_daily)
                    self.assertEqual(reason_daily, "daily")

                    ok_disabled, reason_disabled = await check_global_ai_gemini_budget(
                        session, hourly_limit=0, daily_limit=100
                    )
                    self.assertFalse(ok_disabled)
                    self.assertEqual(reason_disabled, "disabled")

            # 3. Signed-out AI endpoints reject guests (401); signed-in AI chat enforces oversized payload (400) & per-user rate limits (15/min)
            ai_uid = None
            async with get_db_session() as session:
                flood_user = WebUser(
                    name="AI Flood Shield User",
                    email=self.test_email_1,
                    password_hash=hash_password("FloodPass123!"),
                )
                session.add(flood_user)
                await session.commit()
                await session.refresh(flood_user)
                ai_uid = flood_user.id
            ai_token = create_jwt_token(ai_uid)
            ai_headers = {"Authorization": f"Bearer {ai_token}"}

            app = create_web_app()
            async with TestClient(TestServer(app)) as client:
                with patch.object(ai_orchestrator, "answer_prompt") as mock_guest_ai:
                    self.assertEqual((await client.get("/api/ai/session")).status, 401)
                    self.assertEqual(
                        (await client.post("/api/ai/session", json={})).status,
                        401,
                    )
                    self.assertEqual(
                        (await client.post("/api/ai/chat", json={"prompt": "Guest ping"})).status,
                        401,
                    )
                    self.assertEqual(
                        (await client.post("/api/ai/reset", json={})).status,
                        401,
                    )
                    mock_guest_ai.assert_not_called()

                r_big = await client.post(
                    "/api/ai/chat",
                    data=b'{"prompt":"' + (b"x" * 140_000) + b'"}',
                    headers={"Content-Type": "application/json", **ai_headers},
                )
                self.assertEqual(r_big.status, 400)

                async def _quick_ok(db_session, user_prompt, chat_state, web_user_id=None):
                    chat_state.prompts_used += 1
                    return {
                        "ok": True,
                        "session_id": chat_state.session_id,
                        "prompts_used": chat_state.prompts_used,
                        "max_prompts": 5,
                        "limit_reached": False,
                        "answer": "ok",
                        "steps": [],
                        "sources": [],
                        "history": [],
                    }

                with patch.object(ai_orchestrator, "answer_prompt", side_effect=_quick_ok):
                    chat_statuses = []
                    for i in range(15):
                        # Reset prompt counter every 4 turns so the HTTP per-user 15/min limiter fires before session 5-prompt lock
                        resp = await client.post(
                            "/api/ai/chat",
                            json={"prompt": f"User ping {i}", "reset": (i % 4 == 0)},
                            headers=ai_headers,
                        )
                        chat_statuses.append(resp.status)
                # 1 from r_big + 14 from loop = 15 passed; 15th loop iteration (16th total call) hits 429
                self.assertEqual(chat_statuses, [200] * 14 + [429])

                reset_rate_limits()
                reset_statuses = [
                    (await client.post("/api/ai/reset", json={}, headers=ai_headers)).status
                    for _ in range(16)
                ]
                self.assertEqual(reset_statuses, [200] * 15 + [429])
        finally:
            async with get_db_session() as session:
                await session.execute(delete(AIUsageLog).where(AIUsageLog.session_id.like(f"{test_sid_prefix}%")))
                await session.execute(delete(WebUserAIChatSession).where(WebUserAIChatSession.web_user_id == ai_uid))
                await session.execute(delete(WebUser).where(WebUser.email == self.test_email_1))
                await session.commit()
            reset_rate_limits()

    async def test_web_forgot_and_reset_password_flow(self) -> None:
        """Verify end-to-end forgot/reset password OTP hashing, anti-enumeration, attempt lockouts, and JWT rotation."""
        from app.config import settings
        from app.services.email_verification import send_password_reset_email
        from app.web.server import create_web_app

        reset_rate_limits()
        reset_global_email_rate_limit()

        async with get_db_session() as session:
            reg_code, _ = await create_web_registration_otp(
                session, name="Reset Flow User", email=self.test_email_1, password="InitialPassword123!"
            )
            ok_reg, user, _ = await verify_web_otp_and_register(
                session, email=self.test_email_1, otp_code=reg_code
            )
            self.assertTrue(ok_reg)
            self.assertIsNotNone(user)
            self.assertEqual(getattr(user, "token_version", 1), 1)
            old_jwt = create_jwt_token(user.id, token_version=user.token_version)

        self.enterContext(patch.object(settings, "TURNSTILE_SITE_KEY", ""))
        self.enterContext(patch.object(settings, "TURNSTILE_SECRET_KEY", ""))
        app = create_web_app()
        try:
            async with TestClient(TestServer(app)) as client:
                # 1. Invalid domain / blocked local prefix rejected with 400
                r_bad_dom = await client.post("/api/auth/forgot-password", json={"email": "user@gmail.com"})
                self.assertEqual(r_bad_dom.status, 400)
                r_dummy = await client.post("/api/auth/forgot-password", json={"email": "test@edu.aua.am"})
                self.assertEqual(r_dummy.status, 400)

                # 2. Anti-enumeration: non-existent AUA email returns same 200 payload and sends no email
                captured_codes: list[tuple[str, str]] = []

                async def _fake_send_reset_email(to_email: str, code: str):
                    captured_codes.append((to_email, code))
                    return True, None

                with patch("app.web.server.send_password_reset_email", side_effect=_fake_send_reset_email):
                    r_ghost = await client.post(
                        "/api/auth/forgot-password",
                        json={"email": "nonexistent_aua_person@edu.aua.am"},
                    )
                    self.assertEqual(r_ghost.status, 200)
                    ghost_body = await r_ghost.json()
                    self.assertTrue(ghost_body["ok"])
                    self.assertEqual(len(captured_codes), 0)

                    # 3. Existing active account issues hashed OTP & dispatches email
                    r_real = await client.post(
                        "/api/auth/forgot-password",
                        json={"email": self.test_email_1},
                    )
                    self.assertEqual(r_real.status, 200)
                    real_body = await r_real.json()
                    self.assertEqual(real_body["message"], ghost_body["message"])
                    self.assertEqual(len(captured_codes), 1)
                    sent_to, otp_code = captured_codes[0]
                    self.assertEqual(sent_to, self.test_email_1)
                    self.assertEqual(len(otp_code), 6)

                # 4. DB record stores salted hash instead of raw OTP
                async with get_db_session() as session:
                    rec = (
                        await session.execute(
                            select(WebPasswordResetCode).where(WebPasswordResetCode.email == self.test_email_1)
                        )
                    ).scalar_one()
                    self.assertNotEqual(rec.otp_hash, otp_code)
                    self.assertIn("$", rec.otp_hash)

                # 5. Mismatched passwords rejected BEFORE consuming OTP attempts
                r_mismatch = await client.post(
                    "/api/auth/reset-password",
                    json={
                        "email": self.test_email_1,
                        "code": "000000",
                        "new_password": "NewSecurePassword456!",
                        "confirm_password": "DifferentPassword456!",
                    },
                )
                self.assertEqual(r_mismatch.status, 400)

                # 6. Five wrong OTP guesses delete the reset code on the 5th attempt
                wrong_code = "000000" if otp_code != "000000" else "999999"
                for attempt_idx in range(5):
                    r_bad = await client.post(
                        "/api/auth/reset-password",
                        json={
                            "email": self.test_email_1,
                            "code": wrong_code,
                            "new_password": "NewSecurePassword456!",
                            "confirm_password": "NewSecurePassword456!",
                        },
                    )
                    self.assertEqual(r_bad.status, 400)
                    err_txt = (await r_bad.json())["error"]
                    if attempt_idx < 4:
                        self.assertIn("attempts remaining", err_txt)
                    else:
                        self.assertIn("Too many failed attempts", err_txt)

                # Even with the originally correct OTP now, it must fail because record was deleted
                r_after_lock = await client.post(
                    "/api/auth/reset-password",
                    json={
                        "email": self.test_email_1,
                        "code": otp_code,
                        "new_password": "NewSecurePassword456!",
                        "confirm_password": "NewSecurePassword456!",
                    },
                )
                self.assertEqual(r_after_lock.status, 400)

                # 7. Simulated email helper works cleanly without SMTP_HOST
                with patch("app.services.email_verification.settings.SMTP_HOST", ""):
                    sim_ok, sim_info = await send_password_reset_email(self.test_email_1, "654321")
                    self.assertTrue(sim_ok)
                    self.assertEqual(sim_info, "simulated")

                # 8. Clear cooldown & rate limits, issue a fresh reset code via HTTP, then reset password and verify JWT + credential rotation
                reset_rate_limits()
                async with get_db_session() as session:
                    await session.execute(
                        delete(WebEmailCooldown).where(WebEmailCooldown.email == self.test_email_1)
                    )
                    await session.commit()
                captured_codes.clear()
                with patch("app.web.server.send_password_reset_email", side_effect=_fake_send_reset_email):
                    r_reissue = await client.post(
                        "/api/auth/forgot-password",
                        json={"email": self.test_email_1},
                    )
                    self.assertEqual(r_reissue.status, 200)
                new_otp = captured_codes[0][1]

                # Confirm pre-reset JWT still works before reset
                r_me_before = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {old_jwt}"})
                self.assertEqual(r_me_before.status, 200)
                self.assertTrue((await r_me_before.json())["authenticated"])

                r_reset_ok = await client.post(
                    "/api/auth/reset-password",
                    json={
                        "email": self.test_email_1,
                        "code": new_otp,
                        "new_password": "NewSecurePassword456!",
                        "confirm_password": "NewSecurePassword456!",
                    },
                )
                self.assertEqual(r_reset_ok.status, 200)
                reset_payload = await r_reset_ok.json()
                self.assertTrue(reset_payload["ok"])
                new_jwt = reset_payload["token"]

                # Pre-reset JWT must now be rejected because token_version incremented
                r_me_old = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {old_jwt}"})
                self.assertEqual(r_me_old.status, 200)
                self.assertFalse((await r_me_old.json())["authenticated"])

                # New JWT returned from reset-password must authenticate cleanly
                r_me_new = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {new_jwt}"})
                self.assertEqual(r_me_new.status, 200)
                self.assertTrue((await r_me_new.json())["authenticated"])

                # Old password fails login; new password succeeds
                r_old_pwd = await client.post(
                    "/api/auth/login",
                    json={"email": self.test_email_1, "password": "InitialPassword123!"},
                )
                self.assertEqual(r_old_pwd.status, 401)
                r_new_pwd = await client.post(
                    "/api/auth/login",
                    json={"email": self.test_email_1, "password": "NewSecurePassword456!"},
                )
                self.assertEqual(r_new_pwd.status, 200)

                # 9. Expired reset OTP check at CRUD layer
                async with get_db_session() as session:
                    exp_code, _ = await create_web_password_reset_otp(session, email=self.test_email_1)
                    self.assertIsNotNone(exp_code)
                    rec_exp = (
                        await session.execute(
                            select(WebPasswordResetCode).where(WebPasswordResetCode.email == self.test_email_1)
                        )
                    ).scalar_one()
                    rec_exp.expires_at = datetime.now(timezone.utc) - timedelta(minutes=2)
                    await session.commit()

                    ok_exp, _, msg_exp = await verify_web_password_reset_otp(
                        session,
                        email=self.test_email_1,
                        otp_code=exp_code,
                        new_password="AnotherPassword789!",
                    )
                    self.assertFalse(ok_exp)
                    self.assertIn("expired", msg_exp.lower())
        finally:
            reset_rate_limits()
            reset_global_email_rate_limit()

    async def test_web_google_oauth_domain_gate_and_account_linking(self) -> None:
        """Verify Google OAuth PKCE/state CSRF, strict @edu.aua.am/@alumni.aua.am gate, rejection redirect, and existing WebUser linking."""
        import urllib.parse
        from app.config import settings
        from app.web.server import _CONSUMED_GOOGLE_OAUTH_JTIS, create_web_app

        reset_rate_limits()
        _CONSUMED_GOOGLE_OAUTH_JTIS.clear()

        async with get_db_session() as session:
            # Pre-register User 1 with password (to verify automatic Google linking)
            reg_code, _ = await create_web_registration_otp(
                session, name="Existing Linked User", email=self.test_email_1, password="LinkedPassword123!"
            )
            ok_reg, existing_user, _ = await verify_web_otp_and_register(
                session, email=self.test_email_1, otp_code=reg_code
            )
            self.assertTrue(ok_reg)
            self.assertIsNotNone(existing_user)

        app = create_web_app()
        try:
            async with TestClient(TestServer(app)) as client:
                # 1. Placeholder config redirects gracefully with user-friendly error
                with (
                    patch.object(settings, "GOOGLE_CLIENT_ID", "your_google_client_id_here"),
                    patch.object(settings, "GOOGLE_CLIENT_SECRET", "your_google_client_secret_here"),
                ):
                    r_unconf = await client.get("/api/auth/google", allow_redirects=False)
                    self.assertEqual(r_unconf.status, 302)
                    self.assertIn("auth_error=", r_unconf.headers.get("Location", ""))

                with (
                    patch.object(settings, "GOOGLE_CLIENT_ID", "test-google-client-id.apps.googleusercontent.com"),
                    patch.object(settings, "GOOGLE_CLIENT_SECRET", "test-google-client-secret"),
                    patch.object(settings, "GOOGLE_REDIRECT_URI", "https://auahub.am/api/auth/callback/google"),
                    patch.object(settings, "TURNSTILE_SITE_KEY", ""),
                    patch.object(settings, "TURNSTILE_SECRET_KEY", ""),
                ):
                    async def _start_and_get_state() -> str:
                        r_start = await client.get("/api/auth/google", allow_redirects=False)
                        self.assertEqual(r_start.status, 302)
                        loc = r_start.headers["Location"]
                        self.assertTrue(loc.startswith("https://accounts.google.com/o/oauth2/v2/auth?"))
                        parsed_qs = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)
                        self.assertEqual(parsed_qs["code_challenge_method"][0], "S256")
                        self.assertEqual(parsed_qs["redirect_uri"][0], "https://auahub.am/api/auth/callback/google")
                        return parsed_qs["state"][0]

                    # 2. Invalid/missing state fails cleanly without issuing a session cookie
                    r_bad_state = await client.get(
                        "/api/auth/callback/google?code=fake&state=wrong-state",
                        allow_redirects=False,
                    )
                    self.assertEqual(r_bad_state.status, 302)
                    self.assertIn("auth_error=", r_bad_state.headers["Location"])
                    self.assertNotIn("aua_hub_token", r_bad_state.cookies)

                    # 3. Personal @gmail.com email is blocked post-auth, no DB user created, no session cookie
                    st_gmail = await _start_and_get_state()
                    with patch(
                        "app.web.server._exchange_google_code_for_profile",
                        new=AsyncMock(
                            return_value={
                                "sub": "google-sub-gmail-001",
                                "email": "random.person@gmail.com",
                                "email_verified": True,
                                "name": "Random Gmail",
                            }
                        ),
                    ):
                        r_gmail = await client.get(
                            f"/api/auth/callback/google?code=authcode1&state={urllib.parse.quote(st_gmail)}",
                            allow_redirects=False,
                        )
                    self.assertEqual(r_gmail.status, 302)
                    loc_gmail = r_gmail.headers["Location"]
                    self.assertIn("auth_error=", loc_gmail)
                    self.assertIn("Access+Restricted", loc_gmail)
                    self.assertNotIn("aua_hub_token", r_gmail.cookies)
                    async with get_db_session() as session:
                        self.assertIsNone(await get_web_user_by_email(session, "random.person@gmail.com"))

                    # 4. Staff @aua.am or unverified email is also blocked cleanly
                    st_unverified = await _start_and_get_state()
                    with patch(
                        "app.web.server._exchange_google_code_for_profile",
                        new=AsyncMock(
                            return_value={
                                "sub": "google-sub-unverified-001",
                                "email": self.test_email_2,
                                "email_verified": False,
                                "name": "Unverified Alum",
                            }
                        ),
                    ):
                        r_unv = await client.get(
                            f"/api/auth/google/callback?code=authcode2&state={urllib.parse.quote(st_unverified)}",
                            allow_redirects=False,
                        )
                    self.assertEqual(r_unv.status, 302)
                    self.assertIn("auth_error=", r_unv.headers["Location"])
                    self.assertNotIn("aua_hub_token", r_unv.cookies)

                    # 5. Replaying an already-consumed OAuth state fails cleanly
                    r_replay = await client.get(
                        f"/api/auth/google/callback?code=authcode1&state={urllib.parse.quote(st_gmail)}",
                        allow_redirects=False,
                    )
                    self.assertEqual(r_replay.status, 302)
                    self.assertIn("auth_error=", r_replay.headers["Location"])

                    # 6. Existing password-registered @edu.aua.am user links automatically on Google Sign-In
                    st_link = await _start_and_get_state()
                    with patch(
                        "app.web.server._exchange_google_code_for_profile",
                        new=AsyncMock(
                            return_value={
                                "sub": "google-sub-edu-existing-001",
                                "email": self.test_email_1,
                                "email_verified": True,
                                "name": "Google Override Name",
                            }
                        ),
                    ):
                        r_link = await client.get(
                            f"/api/auth/callback/google?code=authcode3&state={urllib.parse.quote(st_link)}",
                            allow_redirects=False,
                        )
                    self.assertEqual(r_link.status, 302)
                    self.assertEqual(r_link.headers["Location"], "/?auth_success=google")
                    self.assertIn("aua_hub_token", r_link.cookies)

                    r_me_linked = await client.get("/api/auth/me")
                    me_linked = await r_me_linked.json()
                    self.assertTrue(me_linked["authenticated"])
                    self.assertEqual(me_linked["user"]["id"], existing_user.id)
                    self.assertEqual(me_linked["user"]["name"], "Existing Linked User")

                    async with get_db_session() as session:
                        linked_db_user = await get_web_user_by_id(session, existing_user.id)
                        self.assertEqual(linked_db_user.google_sub, "google-sub-edu-existing-001")

                    # Sign out before testing new @alumni.aua.am signup via Google
                    await client.post("/api/auth/logout")

                    # 7. Brand-new @alumni.aua.am user creates passwordless Google WebUser cleanly
                    st_alumni = await _start_and_get_state()
                    with patch(
                        "app.web.server._exchange_google_code_for_profile",
                        new=AsyncMock(
                            return_value={
                                "sub": "google-sub-alumni-new-002",
                                "email": self.test_email_2,
                                "email_verified": True,
                                "name": "Anna Alumni Google",
                            }
                        ),
                    ):
                        r_alumni = await client.get(
                            f"/api/auth/google/callback?code=authcode4&state={urllib.parse.quote(st_alumni)}",
                            allow_redirects=False,
                        )
                    self.assertEqual(r_alumni.status, 302)
                    self.assertEqual(r_alumni.headers["Location"], "/?auth_success=google")
                    self.assertIn("aua_hub_token", r_alumni.cookies)

                    r_me_alumni = await client.get("/api/auth/me")
                    me_alumni = await r_me_alumni.json()
                    self.assertTrue(me_alumni["authenticated"])
                    self.assertEqual(me_alumni["user"]["email"], self.test_email_2)
                    self.assertTrue(me_alumni["user"]["is_alumni"])

                    # Password login returns 401 cleanly on passwordless Google account
                    r_pwd_fail = await client.post(
                        "/api/auth/login",
                        json={"email": self.test_email_2, "password": "anything"},
                    )
                    self.assertEqual(r_pwd_fail.status, 401)
        finally:
            _CONSUMED_GOOGLE_OAUTH_JTIS.clear()
            reset_rate_limits()

    async def test_auth_rate_limits_email_cooldown_and_turnstile(self) -> None:
        """Verify Turnstile pre-DB 403 gate, strict 15m per-email cooldown (silent 200 anti-enum), IP logging, and IP limits."""
        from app.config import settings
        from app.web.server import _get_email_cooldown_seconds, create_web_app

        reset_rate_limits()
        reset_global_email_rate_limit()

        # Verify server strictly enforces at least 15 minutes (900s) even if configured lower
        with patch.object(settings, "AUTH_EMAIL_COOLDOWN_SECONDS", 180):
            self.assertGreaterEqual(_get_email_cooldown_seconds(), 900)

        # Ensure pre-existing user for forgot-password cooldown checks
        async with get_db_session() as session:
            user_1 = WebUser(
                name="Cooldown Test User",
                email=self.test_email_1,
                password_hash=hash_password("InitialPassword123!"),
            )
            session.add(user_1)
            await session.commit()

        app = create_web_app()
        try:
            async with TestClient(TestServer(app)) as client:
                # 1. Public config reflects placeholder vs active Turnstile keys without exposing secret + >=900s cooldown
                with (
                    patch.object(settings, "TURNSTILE_SITE_KEY", "your_turnstile_site_key_here"),
                    patch.object(settings, "TURNSTILE_SECRET_KEY", "your_turnstile_secret_key_here"),
                ):
                    r_cfg_off = await client.get("/api/auth/config")
                    self.assertEqual(r_cfg_off.status, 200)
                    cfg_off = await r_cfg_off.json()
                    self.assertFalse(cfg_off["turnstile_enabled"])
                    self.assertIsNone(cfg_off["turnstile_site_key"])
                    self.assertGreaterEqual(cfg_off["email_cooldown_seconds"], 900)

                with (
                    patch.object(settings, "TURNSTILE_SITE_KEY", "test_site_key_123"),
                    patch.object(settings, "TURNSTILE_SECRET_KEY", "test_secret_key_xyz"),
                ):
                    r_cfg_on = await client.get("/api/auth/config")
                    self.assertEqual(r_cfg_on.status, 200)
                    cfg_on = await r_cfg_on.json()
                    self.assertTrue(cfg_on["turnstile_enabled"])
                    self.assertEqual(cfg_on["turnstile_site_key"], "test_site_key_123")
                    self.assertNotIn("test_secret_key_xyz", await r_cfg_on.text())

                    # 2. When Turnstile is configured, missing or invalid tokens return 403 BEFORE DB lookups or email dispatches
                    with (
                        patch("app.web.server.authenticate_web_user") as m_auth_db,
                        patch("app.web.server.get_web_user_by_email") as m_get_user,
                        patch("app.web.server.create_web_password_reset_otp") as m_create_reset,
                        patch("app.web.server.send_verification_email") as m_send_verif,
                        patch("app.web.server.send_password_reset_email") as m_send_reset,
                    ):
                        r_login_notok = await client.post(
                            "/api/auth/login",
                            json={"email": self.test_email_1, "password": "InitialPassword123!"},
                        )
                        self.assertEqual(r_login_notok.status, 403)

                        r_reg_notok = await client.post(
                            "/api/auth/register",
                            json={
                                "name": "Bot User",
                                "email": "cooldown_bot@edu.aua.am",
                                "password": "Password123!",
                                "confirm_password": "Password123!",
                            },
                        )
                        self.assertEqual(r_reg_notok.status, 403)

                        r_forgot_notok = await client.post(
                            "/api/auth/forgot-password",
                            json={"email": self.test_email_1},
                        )
                        self.assertEqual(r_forgot_notok.status, 403)

                        # Failed Cloudflare verification also stops BEFORE DB lookup / email dispatch
                        with patch(
                            "app.web.server._verify_turnstile_token",
                            return_value=(False, "Human verification failed."),
                        ):
                            r_forgot_bad = await client.post(
                                "/api/auth/forgot-password",
                                json={
                                    "email": self.test_email_1,
                                    "cf_turnstile_response": "bad-token",
                                },
                            )
                            self.assertEqual(r_forgot_bad.status, 403)

                        m_auth_db.assert_not_called()
                        m_get_user.assert_not_called()
                        m_create_reset.assert_not_called()
                        m_send_verif.assert_not_called()
                        m_send_reset.assert_not_called()

                    # 3. Valid Turnstile token proceeds cleanly and siteverify sends secret + token
                    verify_calls = []

                    class _FakeSiteVerifyResp:
                        status = 200

                        async def __aenter__(self):
                            return self

                        async def __aexit__(self, *args):
                            return None

                        async def json(self):
                            return {"success": True}

                    def _fake_cf_post(self_session, url, data=None, **kwargs):
                        verify_calls.append((url, dict(data or {})))
                        return _FakeSiteVerifyResp()

                    forgot_sent = []

                    async def _track_reset_send(to_email: str, code: str):
                        forgot_sent.append(to_email)
                        return True, "ok"

                    with (
                        patch("aiohttp.ClientSession.post", new=_fake_cf_post),
                        patch("app.web.server.send_password_reset_email", side_effect=_track_reset_send),
                    ):
                        r_forgot_ok = await client.post(
                            "/api/auth/forgot-password",
                            json={
                                "email": self.test_email_1,
                                "cf_turnstile_response": "valid-cf-token-1",
                            },
                        )
                        self.assertEqual(r_forgot_ok.status, 200)
                        self.assertEqual(len(verify_calls), 1)
                        self.assertEqual(
                            verify_calls[0][0],
                            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
                        )
                        self.assertEqual(verify_calls[0][1]["secret"], "test_secret_key_xyz")
                        self.assertEqual(verify_calls[0][1]["response"], "valid-cf-token-1")
                        self.assertEqual(forgot_sent, [self.test_email_1])

                        # 4. Persistent DB 15-minute cooldown survives in-memory rate-limit reset:
                        # forgot-password returns uniform 200 anti-enumeration response WITHOUT sending duplicate email and logs IP
                        reset_rate_limits()
                        with self.assertLogs("aua_hub_web", level="WARNING") as forgot_logs:
                            r_forgot_cd = await client.post(
                                "/api/auth/forgot-password",
                                json={
                                    "email": self.test_email_1,
                                    "cf_turnstile_response": "valid-cf-token-2",
                                },
                            )
                        self.assertEqual(r_forgot_cd.status, 200)
                        cd_payload = await r_forgot_cd.json()
                        self.assertTrue(cd_payload["ok"])
                        self.assertIn("If an active AUA Hub account exists", cd_payload["message"])
                        self.assertEqual(len(forgot_sent), 1)
                        self.assertTrue(
                            any(
                                "/api/auth/forgot-password" in msg and self.test_email_1 in msg and "ip=" in msg
                                for msg in forgot_logs.output
                            )
                        )

                        # Unregistered email receives identical 200 & also enforces cooldown without duplicate DB churn
                        r_unreg_1 = await client.post(
                            "/api/auth/forgot-password",
                            json={
                                "email": "cooldown_unknown@edu.aua.am",
                                "cf_turnstile_response": "valid-cf-token-3",
                            },
                        )
                        self.assertEqual(r_unreg_1.status, 200)
                        self.assertEqual((await r_unreg_1.json())["message"], cd_payload["message"])
                        self.assertEqual(len(forgot_sent), 1)

                # 5. Registration persistent DB 15m cooldown returns silent HTTP 200, avoids regenerating OTP, & logs IP
                reg_sent = []

                async def _track_reg_send(to_email: str, code: str):
                    reg_sent.append(to_email)
                    return True, "ok"

                reset_rate_limits()
                with (
                    patch.object(settings, "TURNSTILE_SITE_KEY", ""),
                    patch.object(settings, "TURNSTILE_SECRET_KEY", ""),
                    patch("app.web.server.send_verification_email", side_effect=_track_reg_send),
                    patch("app.web.server.send_password_reset_email", side_effect=_track_reg_send),
                ):
                    r_reg_1 = await client.post(
                        "/api/auth/register",
                        json={
                            "name": "Cooldown Victim",
                            "email": "cooldown_victim@edu.aua.am",
                            "password": "Password123!",
                            "confirm_password": "Password123!",
                        },
                    )
                    self.assertEqual(r_reg_1.status, 200)
                    reg_payload_1 = await r_reg_1.json()
                    self.assertTrue(reg_payload_1["ok"])
                    self.assertEqual(reg_sent, ["cooldown_victim@edu.aua.am"])

                    async with get_db_session() as session:
                        first_otp_row = (
                            await session.execute(
                                select(WebOtpCode)
                                .where(WebOtpCode.email == "cooldown_victim@edu.aua.am")
                                .order_by(WebOtpCode.created_at.desc())
                            )
                        ).scalar_one()
                        first_otp_id = first_otp_row.id
                        first_otp_code = first_otp_row.otp_code

                    # Repeat request within 15m returns silent 200, logs IP, keeps original OTP unchanged, & sends NO email
                    reset_rate_limits()
                    with self.assertLogs("aua_hub_web", level="WARNING") as reg_logs:
                        r_reg_cd = await client.post(
                            "/api/auth/register",
                            json={
                                "name": "Cooldown Victim",
                                "email": "cooldown_victim@edu.aua.am",
                                "password": "OtherPassword999!",
                                "confirm_password": "OtherPassword999!",
                            },
                        )
                    self.assertEqual(r_reg_cd.status, 200)
                    self.assertEqual(await r_reg_cd.json(), reg_payload_1)
                    self.assertEqual(len(reg_sent), 1)
                    self.assertTrue(
                        any(
                            "/api/auth/register" in msg and "cooldown_victim@edu.aua.am" in msg and "ip=" in msg
                            for msg in reg_logs.output
                        )
                    )

                    async with get_db_session() as session:
                        otp_rows_after = (
                            await session.execute(
                                select(WebOtpCode).where(WebOtpCode.email == "cooldown_victim@edu.aua.am")
                            )
                        ).scalars().all()
                        self.assertEqual(len(otp_rows_after), 1)
                        self.assertEqual(otp_rows_after[0].id, first_otp_id)
                        self.assertEqual(otp_rows_after[0].otp_code, first_otp_code)

                    # Cross-endpoint protection: hitting /forgot-password on same email inside 15m also stays suppressed
                    with self.assertLogs("aua_hub_web", level="WARNING") as cross_logs:
                        r_cross = await client.post(
                            "/api/auth/forgot-password",
                            json={"email": "cooldown_victim@edu.aua.am"},
                        )
                    self.assertEqual(r_cross.status, 200)
                    self.assertEqual(len(reg_sent), 1)
                    self.assertTrue(any("ip=" in msg for msg in cross_logs.output))

                    # 185s elapsed is STILL within strict 15-minute (900s) cooldown window
                    async with get_db_session() as session:
                        row = (
                            await session.execute(
                                select(WebEmailCooldown).where(
                                    WebEmailCooldown.email == "cooldown_victim@edu.aua.am",
                                    WebEmailCooldown.purpose == "register_otp",
                                )
                            )
                        ).scalar_one()
                        row.last_sent_at = datetime.now(timezone.utc) - timedelta(seconds=185)
                        await session.commit()

                    r_reg_still_cd = await client.post(
                        "/api/auth/register",
                        json={
                            "name": "Cooldown Victim",
                            "email": "cooldown_victim@edu.aua.am",
                            "password": "Password123!",
                            "confirm_password": "Password123!",
                        },
                    )
                    self.assertEqual(r_reg_still_cd.status, 200)
                    self.assertEqual(len(reg_sent), 1)

                    # Once full 15-minute cooldown window elapses (905s), dispatch succeeds again
                    async with get_db_session() as session:
                        row = (
                            await session.execute(
                                select(WebEmailCooldown).where(
                                    WebEmailCooldown.email == "cooldown_victim@edu.aua.am",
                                    WebEmailCooldown.purpose == "register_otp",
                                )
                            )
                        ).scalar_one()
                        row.last_sent_at = datetime.now(timezone.utc) - timedelta(seconds=905)
                        await session.commit()

                    r_reg_after = await client.post(
                        "/api/auth/register",
                        json={
                            "name": "Cooldown Victim",
                            "email": "cooldown_victim@edu.aua.am",
                            "password": "Password123!",
                            "confirm_password": "Password123!",
                        },
                    )
                    self.assertEqual(r_reg_after.status, 200)
                    self.assertEqual(len(reg_sent), 2)

                    # Concurrent burst requests targeting one email send at most 1 email (lock + cooldown check)
                    reset_rate_limits()
                    burst_responses = await asyncio.gather(
                        *[
                            client.post(
                                "/api/auth/register",
                                json={
                                    "name": "Burst Target",
                                    "email": "cooldown_burst@edu.aua.am",
                                    "password": "Password123!",
                                    "confirm_password": "Password123!",
                                },
                            )
                            for _ in range(4)
                        ]
                    )
                    self.assertTrue(all(resp.status == 200 for resp in burst_responses))
                    self.assertEqual(reg_sent.count("cooldown_burst@edu.aua.am"), 1)

                    # 6. Shared per-IP limit (max 5 code requests / 15m across /register and /forgot-password) logs blocked IP
                    reset_rate_limits()
                    for idx in range(1, 4):
                        r_ip = await client.post(
                            "/api/auth/register",
                            json={
                                "name": f"IP Cap {idx}",
                                "email": f"ipcap_{idx}@edu.aua.am",
                                "password": "Password123!",
                                "confirm_password": "Password123!",
                            },
                        )
                        self.assertEqual(r_ip.status, 200)
                    for idx in range(4, 6):
                        r_ip = await client.post(
                            "/api/auth/forgot-password",
                            json={"email": f"ipcap_{idx}@edu.aua.am"},
                        )
                        self.assertEqual(r_ip.status, 200)

                    # 6th code-sending attempt from same IP across either endpoint is rejected with 429 & logged with IP
                    with self.assertLogs("aua_hub_web", level="WARNING") as ip_logs:
                        r_ip_blocked = await client.post(
                            "/api/auth/forgot-password",
                            json={"email": "ipcap_6@edu.aua.am"},
                        )
                    self.assertEqual(r_ip_blocked.status, 429)
                    self.assertTrue(any("ip=" in msg and "IP 15m limit" in msg for msg in ip_logs.output))
        finally:
            reset_rate_limits()
            reset_global_email_rate_limit()

    async def test_campus_chat_admin_delete_exclusively_gor_sargsyan(self) -> None:
        """Verify only gor_sargsyan@edu.aua.am (and message authors) can delete Campus Chat messages."""
        from app.web.server import create_web_app

        # 1. Unit checks: admin identity depends exclusively on gor_sargsyan@edu.aua.am (even without Telegram link)
        unsynced_admin = WebUser(
            id=990001,
            name="Gor Admin",
            email=f"  {ADMIN_STATA_EMAIL.upper()}  ",
            password_hash="x",
            telegram_user_id=None,
            is_deleted=False,
        )
        self.assertTrue(is_web_admin(unsynced_admin))
        self.assertTrue(serialize_web_user(unsynced_admin)["is_admin"])
        self.assertTrue(serialize_web_user(unsynced_admin)["is_stata_admin"])

        deleted_admin = WebUser(
            id=990002,
            name="Deleted Admin",
            email=ADMIN_STATA_EMAIL,
            password_hash="x",
            telegram_user_id=self.tg_uid_2,
            is_deleted=True,
        )
        self.assertFalse(is_web_admin(deleted_admin))

        other_with_tg_id = WebUser(
            id=990003,
            name="Other Student",
            email="other_student@edu.aua.am",
            password_hash="x",
            telegram_user_id=self.tg_uid_2,
            is_deleted=False,
        )
        with patch.object(settings, "ADMIN_IDS", [self.tg_uid_2]):
            self.assertIn(self.tg_uid_2, settings.admin_ids)
            self.assertFalse(is_web_admin(other_with_tg_id))
            self.assertFalse(serialize_web_user(other_with_tg_id)["is_admin"])

        reset_rate_limits()
        reset_global_email_rate_limit()
        uid_a: int | None = None
        uid_b: int | None = None
        msg_ids: list[int] = []
        created_temp_admin = False
        temp_admin_id: int | None = None

        try:
            async with get_db_session() as session:
                await session.execute(
                    delete(WebEmailCooldown).where(WebEmailCooldown.email.like("chat_perm_%@edu.aua.am"))
                )
                await session.execute(
                    delete(WebOtpCode).where(WebOtpCode.email.like("chat_perm_%@edu.aua.am"))
                )
                await session.execute(
                    delete(WebUser).where(WebUser.email.like("chat_perm_%@edu.aua.am"))
                )
                await session.commit()

                otp_a, _ = await create_web_registration_otp(
                    session, "Perm Author", "chat_perm_a@edu.aua.am", "Pass1234!"
                )
                _, u_a, _ = await verify_web_otp_and_register(session, "chat_perm_a@edu.aua.am", otp_a)
                otp_b, _ = await create_web_registration_otp(
                    session, "Perm NonAdmin", "chat_perm_b@edu.aua.am", "Pass1234!"
                )
                _, u_b, _ = await verify_web_otp_and_register(session, "chat_perm_b@edu.aua.am", otp_b)
                await get_or_create_user(session, user_id=self.tg_uid_2, username="perm_nonadmin_tg")
                u_b.telegram_user_id = self.tg_uid_2
                await session.commit()

                admin_user = await get_web_user_by_email(session, ADMIN_STATA_EMAIL)
                if admin_user is None:
                    otp_adm, _ = await create_web_registration_otp(
                        session, "Gor Admin", ADMIN_STATA_EMAIL, "AdminPass123!"
                    )
                    _, admin_user, _ = await verify_web_otp_and_register(session, ADMIN_STATA_EMAIL, otp_adm)
                    created_temp_admin = True
                    temp_admin_id = admin_user.id

                uid_a, uid_b = u_a.id, u_b.id
                admin_id = admin_user.id
                admin_ver = int(getattr(admin_user, "token_version", 1) or 1)
                a_ver = int(getattr(u_a, "token_version", 1) or 1)
                b_ver = int(getattr(u_b, "token_version", 1) or 1)

                m_own = await create_spam_chat_message(session, web_user_id=uid_a, content_html="Author self delete")
                m_other = await create_spam_chat_message(session, web_user_id=uid_a, content_html="Admin delete target")
                m_b = await create_spam_chat_message(session, web_user_id=uid_b, content_html="Protected user B msg")
                msg_ids = [m_own.id, m_other.id, m_b.id]

            app = create_web_app()
            tok_a = create_jwt_token(uid_a, token_version=a_ver)
            tok_b = create_jwt_token(uid_b, token_version=b_ver)
            tok_admin = create_jwt_token(admin_id, token_version=admin_ver)

            async with TestClient(TestServer(app)) as client:
                # 2. Unauthenticated caller cannot delete
                r_unauth = await client.delete(f"/api/spam-chat/messages/{m_other.id}")
                self.assertEqual(r_unauth.status, 401)

                # 3. Non-admin student (even with telegram_user_id in settings.admin_ids) is NOT admin & gets 403
                with patch.object(settings, "ADMIN_IDS", [self.tg_uid_2]):
                    self.assertIn(self.tg_uid_2, settings.admin_ids)
                    r_me_b = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {tok_b}"})
                    self.assertEqual(r_me_b.status, 200)
                    me_b = await r_me_b.json()
                    self.assertFalse(me_b["user"]["is_admin"])
                    self.assertFalse(me_b["user"]["is_stata_admin"])

                    r_b_del = await client.delete(
                        f"/api/spam-chat/messages/{m_other.id}",
                        headers={"Authorization": f"Bearer {tok_b}"},
                    )
                    self.assertEqual(r_b_del.status, 403)

                # 4. Author cannot edit or delete someone else's message, CAN delete their own message
                r_a_edit_b = await client.put(
                    f"/api/spam-chat/messages/{m_b.id}",
                    json={"content": "tampered"},
                    headers={"Authorization": f"Bearer {tok_a}"},
                )
                self.assertEqual(r_a_edit_b.status, 403)

                r_a_del_b = await client.delete(
                    f"/api/spam-chat/messages/{m_b.id}",
                    headers={"Authorization": f"Bearer {tok_a}"},
                )
                self.assertEqual(r_a_del_b.status, 403)

                r_a_del_own = await client.delete(
                    f"/api/spam-chat/messages/{m_own.id}",
                    headers={"Authorization": f"Bearer {tok_a}"},
                )
                self.assertEqual(r_a_del_own.status, 200)

                # 5. Sole admin gor_sargsyan@edu.aua.am has is_admin=True, cannot edit others', CAN delete others'
                r_me_adm = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {tok_admin}"})
                self.assertEqual(r_me_adm.status, 200)
                me_adm = await r_me_adm.json()
                self.assertTrue(me_adm["user"]["is_admin"])
                self.assertTrue(me_adm["user"]["is_stata_admin"])

                r_adm_edit_other = await client.put(
                    f"/api/spam-chat/messages/{m_other.id}",
                    json={"content": "admin should not forge edits"},
                    headers={"Authorization": f"Bearer {tok_admin}"},
                )
                self.assertEqual(r_adm_edit_other.status, 403)

                r_adm_del_other = await client.delete(
                    f"/api/spam-chat/messages/{m_other.id}",
                    headers={"Authorization": f"Bearer {tok_admin}"},
                )
                self.assertEqual(r_adm_del_other.status, 200)
        finally:
            async with get_db_session() as session:
                if msg_ids:
                    await session.execute(delete(SpamChatMessage).where(SpamChatMessage.id.in_(msg_ids)))
                clean_uids = [uid for uid in (uid_a, uid_b) if uid is not None]
                if created_temp_admin and temp_admin_id is not None:
                    clean_uids.append(temp_admin_id)
                if clean_uids:
                    await session.execute(delete(SpamChatRead).where(SpamChatRead.web_user_id.in_(clean_uids)))
                    await session.execute(delete(WebUser).where(WebUser.id.in_(clean_uids)))
                await session.execute(
                    delete(WebEmailCooldown).where(WebEmailCooldown.email.like("chat_perm_%@edu.aua.am"))
                )
                await session.execute(
                    delete(WebOtpCode).where(WebOtpCode.email.like("chat_perm_%@edu.aua.am"))
                )
                await session.commit()
            reset_rate_limits()
            reset_global_email_rate_limit()

    async def test_browser_notifications_menu_and_sw_assets(self) -> None:
        """Verify notifications popover includes browser notification switch and matching SW hooks."""
        from app.web.server import create_web_app

        app = create_web_app()
        async with TestClient(TestServer(app)) as client:
            r_html = await client.get("/")
            self.assertEqual(r_html.status, 200)
            html_body = await r_html.text()
            self.assertIn('id="notificationsPopover"', html_body)
            self.assertIn('id="browserNotifToggle"', html_body)
            self.assertIn('onchange="handleBrowserNotifToggleChange(this)"', html_body)
            self.assertIn("/static/styles.css?v=20261122_3", html_body)
            self.assertIn("/static/app.js?v=20261122_3", html_body)

            r_js = await client.get("/static/app.js")
            self.assertEqual(r_js.status, 200)
            js_body = await r_js.text()
            self.assertIn("function handleBrowserNotifToggleChange(", js_body)
            self.assertIn("function dispatchBrowserNotification(", js_body)
            self.assertIn("Notification.requestPermission()", js_body)
            self.assertIn("AUA_HUB_OPEN_NOTIFICATION", js_body)

            r_sw = await client.get("/sw.js")
            self.assertEqual(r_sw.status, 200)
            sw_body = await r_sw.text()
            self.assertIn('addEventListener("notificationclick"', sw_body)
            self.assertIn("AUA_HUB_OPEN_NOTIFICATION", sw_body)
            self.assertIn("/static/styles.css?v=20261122_3", sw_body)
            self.assertIn("/static/app.js?v=20261122_3", sw_body)

    async def test_gpa_tracker_syllabus_cache_rate_limits_and_course_crud(self) -> None:
        """Verify GPA Tracker navbar placement, SyllabusCache hit/miss deduplication, rate limits, and user-scoped Course CRUD."""
        import hashlib
        from aiohttp import FormData
        from app.database.models import SyllabusCache, UserCourse
        import app.web.server as web_server
        from app.web.server import create_web_app

        reset_rate_limits()
        reset_global_email_rate_limit()

        uid_1 = None
        uid_2 = None
        test_hash_Cleanup: List[str] = []

        try:
            async with get_db_session() as session:
                await init_db()
                u1 = WebUser(
                    name="GPA Student One",
                    email="gpa_student_one@edu.aua.am",
                    password_hash="hash1",
                )
                u2 = WebUser(
                    name="GPA Student Two",
                    email="gpa_student_two@edu.aua.am",
                    password_hash="hash2",
                )
                session.add_all([u1, u2])
                await session.commit()
                await session.refresh(u1)
                await session.refresh(u2)
                uid_1, uid_2 = u1.id, u2.id

            tok_1 = create_jwt_token(uid_1, token_version=1)
            tok_2 = create_jwt_token(uid_2, token_version=1)
            h1 = {"Authorization": f"Bearer {tok_1}"}
            h2 = {"Authorization": f"Bearer {tok_2}"}

            app = create_web_app()
            async with TestClient(TestServer(app)) as client:
                # 1. Verify #gpa-tracker is in navbar directly between AI Assistant and Have-Need
                r_index = await client.get("/")
                self.assertEqual(r_index.status, 200)
                index_html = await r_index.text()
                pos_ai = index_html.find('data-view="ai-assistant"')
                pos_gpa = index_html.find('data-view="gpa-tracker"')
                pos_hn = index_html.find('data-view="have-need"')
                self.assertTrue(0 < pos_ai < pos_gpa < pos_hn)
                self.assertIn('id="view-gpa-tracker"', index_html)
                self.assertIn('id="gpaCalculatorSubview"', index_html)
                self.assertIn('id="gpaTrackerSubview"', index_html)

                # 2. Unauthenticated requests to /api/gpa/* return 401
                self.assertEqual((await client.get("/api/gpa/courses")).status, 401)
                self.assertEqual((await client.post("/api/gpa/courses", json={"course_name": "CS 101"})).status, 401)
                self.assertEqual((await client.post("/api/gpa/parse-syllabus")).status, 401)

                # 3. Syllabus parse cache miss -> calls Gemini once, stores SyllabusCache & backup;
                #    second identical upload -> cache hit without calling Gemini or hitting 30s cooldown!
                #    Also verify double-extension filename 'CS121 Data Structures Syllabus.docx.pdf',
                #    Quizzes (4*3.75=15%), and A+..F grading scale handling.
                sample_pdf = b"%PDF-1.4\n% Sample CS121 Syllabus with Grading Breakdown\n%%EOF"
                test_hash_Cleanup.append(hashlib.sha256(sample_pdf).hexdigest())

                self.assertEqual(
                    web_server.extract_syllabus_upload_extension("CS121 Data Structures Syllabus.docx.pdf"),
                    ".pdf",
                )
                valid_sig, mime_sig = web_server.validate_syllabus_file(sample_pdf, ".pdf")
                self.assertTrue(valid_sig)
                self.assertEqual(mime_sig, "application/pdf")
                ext_txt, ext_thash = web_server.extract_syllabus_text_and_hash(sample_pdf, ".pdf")
                self.assertEqual(ext_txt, "")
                self.assertIsNone(ext_thash)

                # Verify heuristic parser also handles 'Quizzes (4*3.75=15%)', 'Midterm I 23%', and A+..F table
                heur_res = web_server._heuristic_parse_syllabus_text(
                    "CS121 - Data Structures\n"
                    "Midterm I 23%\n"
                    "Midterm II 27%\n"
                    "Quizzes (4*3.75=15%)\n"
                    "Final Exam 35%\n"
                    "A+ 97-100\nA 93-96\nA- 90-92\nB+ 87-89\nB 83-86\nB- 80-82\nC+ 77-79\nC 73-76\nC- 70-72\nD+ 67-69\nD 63-66\nD- 60-62\nF 0-59\n",
                    "CS121 Data Structures Syllabus.docx.pdf",
                )
                self.assertTrue(heur_res["has_grading_breakdown"])
                self.assertAlmostEqual(sum(a["weight"] for a in heur_res["assignments"]), 100.0)
                self.assertEqual(heur_res["grading_scale"]["A"], 93.0)
                self.assertEqual(heur_res["grading_scale"]["A-"], 90.0)

                mock_gemini_return = {
                    "has_grading_breakdown": True,
                    "course_name": "CS 121 - Data Structures",
                    "assignments": [
                        {"name": "Midterm I", "weight": 23.0, "is_extra_credit": False},
                        {"name": "Midterm II", "weight": 27.0, "is_extra_credit": False},
                        {"name": "Quiz 1", "weight": 3.75, "is_extra_credit": False},
                        {"name": "Quiz 2", "weight": 3.75, "is_extra_credit": False},
                        {"name": "Quiz 3", "weight": 3.75, "is_extra_credit": False},
                        {"name": "Quiz 4", "weight": 3.75, "is_extra_credit": False},
                        {"name": "Final Exam", "weight": 35.0, "is_extra_credit": False},
                    ],
                    "grading_scale": {"A+": 97.0, "A": 93.0, "A-": 90.0, "B+": 87.0, "B": 83.0, "B-": 80.0},
                    "notes": "4 Quizzes at 3.75% each (15% total).",
                }

                with patch.object(
                    web_server,
                    "parse_syllabus_with_gemini",
                    new=AsyncMock(return_value=mock_gemini_return),
                ) as mock_gem:
                    fd1 = FormData()
                    fd1.add_field(
                        "file",
                        sample_pdf,
                        filename="CS121 Data Structures Syllabus.docx.pdf",
                        content_type="application/pdf",
                    )
                    r_parse1 = await client.post("/api/gpa/parse-syllabus", data=fd1, headers=h1)
                    self.assertEqual(r_parse1.status, 200)
                    body1 = await r_parse1.json()
                    self.assertTrue(body1["ok"])
                    self.assertFalse(body1["cached"])
                    self.assertEqual(body1["syllabus"]["course_name"], "CS 121 - Data Structures")
                    self.assertEqual(len(body1["syllabus"]["assignments"]), 7)
                    self.assertAlmostEqual(
                        sum(a["weight"] for a in body1["syllabus"]["assignments"]),
                        100.0,
                    )
                    self.assertEqual(mock_gem.call_count, 1)
                    cache_id = body1["syllabus_cache_id"]

                    # Immediate repeat upload (within 30s cooldown!) succeeds via SyllabusCache hit without calling Gemini
                    fd2 = FormData()
                    fd2.add_field(
                        "file",
                        sample_pdf,
                        filename="CS121 Data Structures Syllabus.docx.pdf",
                        content_type="application/pdf",
                    )
                    r_parse2 = await client.post("/api/gpa/parse-syllabus", data=fd2, headers=h1)
                    self.assertEqual(r_parse2.status, 200)
                    body2 = await r_parse2.json()
                    self.assertTrue(body2["ok"])
                    self.assertTrue(body2["cached"])
                    self.assertEqual(body2["syllabus_cache_id"], cache_id)
                    self.assertEqual(mock_gem.call_count, 1)

                    # Different non-cached file within 30s triggers 429 cooldown
                    diff_pdf = b"%PDF-1.4\n% Different uncached syllabus bytes 99999\n%%EOF"
                    test_hash_Cleanup.append(hashlib.sha256(diff_pdf).hexdigest())
                    fd3 = FormData()
                    fd3.add_field("file", diff_pdf, filename="other.pdf", content_type="application/pdf")
                    r_parse3 = await client.post("/api/gpa/parse-syllabus", data=fd3, headers=h1)
                    self.assertEqual(r_parse3.status, 429)

                    # After resetting cooldown, test extraction failure response when has_grading_breakdown=False
                    # and verify it is NOT cached in SyllabusCache (no negative cache poisoning!)
                    reset_rate_limits()
                    mock_gem.return_value = {
                        "has_grading_breakdown": False,
                        "course_name": None,
                        "assignments": [],
                        "grading_scale": None,
                        "notes": None,
                    }
                    fd4 = FormData()
                    fd4.add_field("file", diff_pdf, filename="no_grades.pdf", content_type="application/pdf")
                    r_parse4 = await client.post("/api/gpa/parse-syllabus", data=fd4, headers=h1)
                    self.assertEqual(r_parse4.status, 200)
                    body4 = await r_parse4.json()
                    self.assertFalse(body4["ok"])
                    self.assertIsNone(body4["syllabus_cache_id"])
                    self.assertEqual(body4["error"], "AI could not extract grading percentages from this file.")

                    # Verify upstream AI exception returns 502 and is NOT swallowed as "no grading percentages"
                    reset_rate_limits()
                    mock_gem.side_effect = web_server.SyllabusAIParseError(
                        "AI syllabus parser encountered an upstream service error while reading this file. Please try again.",
                        status_code=502,
                    )
                    fd5 = FormData()
                    fd5.add_field("file", diff_pdf, filename="api_error.pdf", content_type="application/pdf")
                    r_parse5 = await client.post("/api/gpa/parse-syllabus", data=fd5, headers=h1)
                    self.assertEqual(r_parse5.status, 502)
                    body5 = await r_parse5.json()
                    self.assertFalse(body5["ok"])
                    self.assertIn("upstream service error", body5["error"])

                # 4. UserCourse CRUD & strict per-user isolation
                r_create = await client.post(
                    "/api/gpa/courses",
                    headers=h1,
                    json={
                        "course_name": "CS 120 - Data Structures",
                        "credits": 3.0,
                        "target_percent": 93.0,
                        "assignments": [
                            {"id": "a1", "name": "Midterm", "weight": 40, "score": 88.5, "is_extra_credit": False},
                            {"id": "a2", "name": "Final", "weight": 60, "score": None, "is_extra_credit": False},
                        ],
                        "notes": "Drop lowest quiz",
                        "syllabus_cache_id": cache_id,
                    },
                )
                self.assertEqual(r_create.status, 201)
                created_course = (await r_create.json())["course"]
                cid = created_course["id"]
                self.assertEqual(created_course["course_name"], "CS 120 - Data Structures")
                self.assertEqual(len(created_course["assignments"]), 2)
                self.assertEqual(created_course["assignments"][0]["score"], 88.5)
                self.assertIsNone(created_course["assignments"][1]["score"])

                # User 2 cannot list, update, or delete User 1's course
                r_list_u2 = await client.get("/api/gpa/courses", headers=h2)
                self.assertEqual(r_list_u2.status, 200)
                self.assertEqual((await r_list_u2.json())["courses"], [])

                r_put_u2 = await client.put(
                    f"/api/gpa/courses/{cid}",
                    headers=h2,
                    json={"course_name": "Hacked Name"},
                )
                self.assertEqual(r_put_u2.status, 404)

                r_del_u2 = await client.delete(f"/api/gpa/courses/{cid}", headers=h2)
                self.assertEqual(r_del_u2.status, 404)

                # User 1 can update and delete their own course
                r_put_u1 = await client.put(
                    f"/api/gpa/courses/{cid}",
                    headers=h1,
                    json={
                        "target_percent": 90.0,
                        "assignments": [
                            {"id": "a1", "name": "Midterm", "weight": 40, "score": 92.0, "is_extra_credit": False},
                            {"id": "a2", "name": "Final", "weight": 60, "score": 89.0, "is_extra_credit": False},
                        ],
                    },
                )
                self.assertEqual(r_put_u1.status, 200)
                updated_c = (await r_put_u1.json())["course"]
                self.assertEqual(updated_c["target_percent"], 90.0)
                self.assertEqual(updated_c["assignments"][1]["score"], 89.0)

                r_del_u1 = await client.delete(f"/api/gpa/courses/{cid}", headers=h1)
                self.assertEqual(r_del_u1.status, 200)
                r_list_u1 = await client.get("/api/gpa/courses", headers=h1)
                self.assertEqual((await r_list_u1.json())["courses"], [])
        finally:
            async with get_db_session() as session:
                if uid_1 or uid_2:
                    uids = [u for u in (uid_1, uid_2) if u is not None]
                    await session.execute(delete(UserCourse).where(UserCourse.user_id.in_(uids)))
                    await session.execute(delete(WebUser).where(WebUser.id.in_(uids)))
                if test_hash_Cleanup:
                    await session.execute(
                        delete(SyllabusCache).where(SyllabusCache.file_hash.in_(test_hash_Cleanup))
                    )
                await session.commit()
            for fh in test_hash_Cleanup:
                bp = web_server.SYLLABUS_BACKUPS_DIR / f"{fh}.pdf"
                if bp.exists():
                    try:
                        bp.unlink()
                    except Exception:
                        pass
            reset_rate_limits()
            reset_global_email_rate_limit()


if __name__ == "__main__":
    unittest.main()
