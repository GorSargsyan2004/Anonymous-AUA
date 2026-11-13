"""Unit and integration tests for AUA Hub website & bot bridge features."""

from datetime import datetime, timezone
import unittest
from unittest.mock import AsyncMock, MagicMock

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
    WebOtpCode,
    WebSyncCode,
    WebUser,
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
    list_academic_feedbacks,
    list_anonymous_public_posts,
    list_have_need_posts,
    list_spam_chat_messages,
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
)
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
from app.web.server import (
    check_rate_limit,
    create_jwt_token,
    decode_jwt_token,
    is_safe_upload_url,
    render_safe_chat_html,
    reset_rate_limits,
    scan_web_text_for_malicious_links,
    serialize_academic_feedback,
    serialize_have_need,
    serialize_spam_message,
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
                delete(WebOtpCode).where(WebOtpCode.email.in_([self.test_email_1, self.test_email_2]))
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
            await handle_post_text_input(mock_msg, mock_state)
            mock_msg.answer.assert_called_once()
            reply_arg = mock_msg.answer.call_args[0][0]
            self.assertIn("Anonymous Post Published to AUA Hub!", reply_arg)

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
            # User 1 posts: Has c1 (Section A), Needs c2 (Section B)
            hn1 = await create_have_need_post(
                session=session,
                web_user_id=u1.id,
                have_course_id=c1.id,
                need_course_id=c2.id,
                contact_dm="@gor_tg",
                notes="Morning for afternoon section",
            )
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
        from datetime import datetime, timezone
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


if __name__ == "__main__":
    unittest.main()
