from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import delete

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("BOT_TOKEN", "test_token_123")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/anonymous_aua")

from app.database.crud import (
    _sweep_concurrency,
    get_advanced_bot_chat_stats,
    record_bot_activity_snapshot,
)
from app.database.models import (
    AIUsageLog,
    BotActivitySnapshot,
    ChatSession,
    User,
    WebOtpCode,
    WebUser,
    WebUserAIChatSession,
)
from app.database.session import engine, get_db_session, init_db
from app.database.web_crud import (
    create_web_registration_otp,
    get_ai_assistant_stats,
    get_full_admin_stata_payload,
    get_web_user_by_email,
    verify_web_otp_and_register,
)
from app.web.server import create_jwt_token, create_web_app, reset_rate_limits


class TestStataAnalytics(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        await init_db()
        reset_rate_limits()
        self.non_admin_email = "stata_nonadmin_test@edu.aua.am"
        self.test_tg_a = 9811001
        self.test_tg_b = 9811002
        self.created_chat_ids: list[int] = []
        self.created_snap_ids: list[int] = []
        self.test_ai_session_ids = ["stata_test_ai_1", "stata_test_ai_2"]

    async def asyncTearDown(self) -> None:
        async with get_db_session() as session:
            if self.created_chat_ids:
                await session.execute(delete(ChatSession).where(ChatSession.id.in_(self.created_chat_ids)))
            await session.execute(
                delete(ChatSession).where(ChatSession.user1_id.in_([self.test_tg_a, self.test_tg_b]))
            )
            if self.created_snap_ids:
                await session.execute(
                    delete(BotActivitySnapshot).where(BotActivitySnapshot.id.in_(self.created_snap_ids))
                )
            await session.execute(
                delete(AIUsageLog).where(AIUsageLog.session_id.in_(self.test_ai_session_ids))
            )
            await session.execute(
                delete(WebUserAIChatSession).where(
                    WebUserAIChatSession.session_id.in_(self.test_ai_session_ids)
                )
            )
            await session.execute(delete(WebOtpCode).where(WebOtpCode.email == self.non_admin_email))
            await session.execute(delete(WebUser).where(WebUser.email == self.non_admin_email))
            await session.execute(delete(User).where(User.id.in_([self.test_tg_a, self.test_tg_b])))
            await session.commit()
        await engine.dispose()

    def test_sweep_concurrency_mean_median(self) -> None:
        t0 = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
        intervals = [
            ((t0 + timedelta(minutes=0)).timestamp(), (t0 + timedelta(minutes=15)).timestamp()),
            ((t0 + timedelta(minutes=10)).timestamp(), (t0 + timedelta(minutes=25)).timestamp()),
        ]
        # +0 -> 1 active, +10 -> 2 active, +20 -> 1 active, +30 -> 0 active
        samples = [
            (t0 + timedelta(minutes=0)).timestamp(),
            (t0 + timedelta(minutes=10)).timestamp(),
            (t0 + timedelta(minutes=20)).timestamp(),
            (t0 + timedelta(minutes=30)).timestamp(),
        ]
        counts = _sweep_concurrency(intervals, samples)
        self.assertEqual(counts, [1, 2, 1, 0])

    async def test_advanced_bot_chat_and_ai_stats(self) -> None:
        now = datetime.now(timezone.utc)
        async with get_db_session() as session:
            session.add_all([User(id=self.test_tg_a), User(id=self.test_tg_b)])
            await session.flush()

            c1 = ChatSession(
                user1_id=self.test_tg_a,
                user2_id=self.test_tg_b,
                is_active=False,
                messages_count=12,
                started_at=now - timedelta(minutes=40),
                ended_at=now - timedelta(minutes=10),
            )
            c2 = ChatSession(
                user1_id=self.test_tg_a,
                user2_id=self.test_tg_b,
                is_active=False,
                messages_count=0,
                started_at=now - timedelta(days=5, minutes=15),
                ended_at=now - timedelta(days=5),
            )
            session.add_all([c1, c2])
            await session.flush()
            self.created_chat_ids.extend([c1.id, c2.id])

            snap = await record_bot_activity_snapshot(session, web_ws_clients=3)
            self.created_snap_ids.append(snap.id)

            session.add_all(
                [
                    AIUsageLog(
                        session_id="stata_test_ai_1",
                        prompt_index=1,
                        tools_used_json='["course_feedbacks", "campus_chat"]',
                        sources_count=3,
                        latency_ms=420,
                        used_gemini=True,
                        prompt_length=38,
                        answer_length=290,
                        created_at=now - timedelta(minutes=20),
                    ),
                    AIUsageLog(
                        session_id="stata_test_ai_1",
                        prompt_index=2,
                        tools_used_json='["newsroom_events"]',
                        sources_count=2,
                        latency_ms=680,
                        used_gemini=True,
                        prompt_length=45,
                        answer_length=410,
                        created_at=now - timedelta(minutes=10),
                    ),
                    AIUsageLog(
                        session_id="stata_test_ai_2",
                        prompt_index=1,
                        tools_used_json='["aua_directory"]',
                        sources_count=1,
                        latency_ms=510,
                        used_gemini=False,
                        prompt_length=25,
                        answer_length=180,
                        created_at=now - timedelta(days=3),
                    ),
                ]
            )
            await session.commit()

        async with get_db_session() as session:
            adv = await get_advanced_bot_chat_stats(session)
            self.assertIn("window_24h", adv)
            self.assertIn("window_30d", adv)
            self.assertIn("window_all_time", adv)
            self.assertGreaterEqual(adv["window_24h"]["initiated"], 1)
            self.assertGreaterEqual(adv["window_30d"]["initiated"], 2)
            self.assertEqual(len(adv["hourly_profile"]), 24)
            self.assertEqual(len(adv["rolling_24h_series"]), 24)
            self.assertEqual(len(adv["concurrency_24h_curve"]), 97)
            self.assertEqual(len(adv["daily_series_30d"]), 30)
            self.assertEqual(len(adv["heatmap_7x24"]["matrix"]), 7)
            self.assertEqual(len(adv["heatmap_7x24"]["matrix"][0]), 24)
            self.assertTrue(adv["peak_hours"]["most_active_init_window"])

            ai_stats = await get_ai_assistant_stats(session)
            self.assertGreaterEqual(ai_stats["total_prompts"], 3)
            self.assertGreaterEqual(ai_stats["prompts_24h"], 2)
            self.assertGreaterEqual(ai_stats["prompts_30d"], 3)
            self.assertEqual(len(ai_stats["tool_breakdown"]), 6)
            self.assertEqual(len(ai_stats["prompt_depth_distribution"]), 5)

            full_payload = await get_full_admin_stata_payload(
                session,
                live_ws_count=4,
                jenzareviews_sync_status={"last_sync_at": None, "synced_count": 0},
            )
            self.assertEqual(full_payload["live"]["web_ws_clients"], 4)
            for key in ("live", "anonymous_chat_advanced", "ai_assistant", "bot_overview", "website"):
                self.assertIn(key, full_payload)

    async def test_stata_strict_admin_access_control(self) -> None:
        async with get_db_session() as session:
            otp_code, _ = await create_web_registration_otp(
                session=session,
                name="Non Admin",
                email=self.non_admin_email,
                password="StrongPassword123!",
            )
            ok, non_admin_user, msg = await verify_web_otp_and_register(
                session=session,
                email=self.non_admin_email,
                otp_code=otp_code,
            )
            self.assertTrue(ok, msg)
            self.assertIsNotNone(non_admin_user)
            non_admin_token = create_jwt_token(non_admin_user.id)

            admin_user = await get_web_user_by_email(session, "gor_sargsyan@edu.aua.am")
            self.assertIsNotNone(admin_user, "gor_sargsyan@edu.aua.am must exist in DB for /stata admin check")
            admin_token = create_jwt_token(admin_user.id)

        app = create_web_app()
        async with TestClient(TestServer(app)) as client:
            # 1. Unauthenticated access: gate only, zero telemetry data, API/assets 401
            r_unauth_page = await client.get("/stata")
            self.assertEqual(r_unauth_page.status, 200)
            unauth_html = await r_unauth_page.text()
            self.assertIn("Administrator Sign In", unauth_html)
            self.assertNotIn("chartAnonConcOrDaily", unauth_html)

            r_unauth_api = await client.get("/api/admin/stata")
            self.assertEqual(r_unauth_api.status, 401)

            r_unauth_js = await client.get("/stata/assets/stata.js")
            self.assertEqual(r_unauth_js.status, 401)

            # 2. Non-admin AUA student/alumni: strictly 403 Forbidden everywhere
            non_admin_hdr = {"Authorization": f"Bearer {non_admin_token}"}
            r_user_page = await client.get("/stata", headers=non_admin_hdr)
            self.assertEqual(r_user_page.status, 403)
            self.assertIn("Administrator Console Only", await r_user_page.text())

            r_user_api = await client.get("/api/admin/stata", headers=non_admin_hdr)
            self.assertEqual(r_user_api.status, 403)

            r_user_js = await client.get("/stata/assets/stata.js", headers=non_admin_hdr)
            self.assertEqual(r_user_js.status, 403)

            # 3. Authorized admin (gor_sargsyan@edu.aua.am): full access
            admin_hdr = {"Authorization": f"Bearer {admin_token}"}
            r_admin_page = await client.get("/stata", headers=admin_hdr)
            self.assertEqual(r_admin_page.status, 200)
            admin_html = await r_admin_page.text()
            self.assertIn("chartAnonConcOrDaily", admin_html)
            self.assertIn("/static/vendor/chart.umd.min.js", admin_html)

            r_admin_css = await client.get("/stata/assets/stata.css", headers=admin_hdr)
            self.assertEqual(r_admin_css.status, 200)

            r_admin_js = await client.get("/stata/assets/stata.js", headers=admin_hdr)
            self.assertEqual(r_admin_js.status, 200)

            r_admin_bad_asset = await client.get("/stata/assets/stata_gate.html", headers=admin_hdr)
            self.assertEqual(r_admin_bad_asset.status, 404)

            r_admin_api = await client.get("/api/admin/stata", headers=admin_hdr)
            self.assertEqual(r_admin_api.status, 200)
            data = await r_admin_api.json()
            self.assertIn("anonymous_chat_advanced", data)
            self.assertIn("ai_assistant", data)
            self.assertIn("bot_overview", data)
            self.assertIn("website", data)

            # 4. Local Chart.js vendor bundle served with 200
            r_chartjs = await client.get("/static/vendor/chart.umd.min.js")
            self.assertEqual(r_chartjs.status, 200)


if __name__ == "__main__":
    unittest.main()
