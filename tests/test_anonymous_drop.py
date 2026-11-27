"""Comprehensive tests for the Hardened Anonymous Post Dropbox (drop.auahub.am & Tor v3 .onion Mirror)."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import time
from typing import Optional, Tuple
from urllib.parse import urlencode
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import delete, select

from app.config import settings
from app.database.models import AnonymousDropQueue, AnonymousPublicPost, WebUser
from app.database.session import engine, get_db_session, init_db
from app.database.web_crud import (
    create_anon_post_comment,
    create_anonymous_public_post,
    list_anon_post_comments,
    toggle_anon_comment_reaction,
    toggle_anon_post_reaction,
)
from app.web.drop_service import (
    CAPTCHA_ALPHABET,
    CAPTCHA_LENGTH,
    DropModerationVerdict,
    GLOBAL_MAX_ACCEPTED_PER_HOUR,
    bucket_timestamp_5min,
    create_stateless_captcha,
    drop_ai_moderator,
    drop_state,
    process_due_drop_queue_items,
    verify_stateless_captcha,
)
from app.web.server import create_web_app

TEST_PROXY_SECRET = "test_drop_internal_proxy_secret_9f8e7d6c5b4a3210"
TEST_CAPTCHA_SECRET = "test_drop_captcha_hmac_secret_0123456789abcdef"
TEST_ONION_HOST = "auahubdrop23456723456723456723456723456723456723456723ad.onion"


class TestAnonymousDropService(unittest.IsolatedAsyncioTestCase):
    """End-to-end unit and integration tests for `drop_service.py` and `/_drop/*` routes."""

    async def asyncSetUp(self) -> None:
        await init_db()
        drop_state.reset_for_testing()

        self._orig_proxy_secret = settings.DROP_INTERNAL_PROXY_SECRET
        self._orig_captcha_secret = settings.DROP_CAPTCHA_SECRET
        self._orig_onion_host = settings.DROP_ONION_HOSTNAME

        settings.DROP_INTERNAL_PROXY_SECRET = TEST_PROXY_SECRET
        settings.DROP_CAPTCHA_SECRET = TEST_CAPTCHA_SECRET
        settings.DROP_ONION_HOSTNAME = TEST_ONION_HOST

        self.app = create_web_app(bot=None)
        self.server = TestServer(self.app)
        self.client = TestClient(self.server)
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        settings.DROP_INTERNAL_PROXY_SECRET = self._orig_proxy_secret
        settings.DROP_CAPTCHA_SECRET = self._orig_captcha_secret
        settings.DROP_ONION_HOSTNAME = self._orig_onion_host
        drop_state.reset_for_testing()

        async with get_db_session() as session:
            await session.execute(
                delete(AnonymousDropQueue).where(
                    AnonymousDropQueue.content.like("%[DROP_TEST_MARKER]%")
                )
            )
            await session.execute(
                delete(AnonymousPublicPost).where(
                    AnonymousPublicPost.content_text.like("%[DROP_TEST_MARKER]%")
                )
            )
            await session.commit()
        await engine.dispose()

    def _proxy_headers(self, transport: str = "clearnet", secret: Optional[str] = TEST_PROXY_SECRET) -> dict:
        headers = {
            "X-Drop-Transport": transport,
        }
        if secret is not None:
            headers["X-Drop-Proxy-Secret"] = secret
        return headers

    # --------------------------------------------------------------------------
    # 1. Route & Secret Isolation
    # --------------------------------------------------------------------------

    async def test_route_isolation_blocks_missing_or_wrong_secret_with_404(self) -> None:
        """Requests to `/_drop/*` without a valid `X-Drop-Proxy-Secret` must return 404 (not 403)."""
        for path in ("/_drop/", "/_drop", "/_drop/healthz"):
            resp_no_hdr = await self.client.get(path)
            self.assertEqual(resp_no_hdr.status, 404)

            resp_bad_hdr = await self.client.get(path, headers={"X-Drop-Proxy-Secret": "wrong_secret"})
            self.assertEqual(resp_bad_hdr.status, 404)

        resp_post_no_hdr = await self.client.post(
            "/_drop/submit",
            data=urlencode({"content": "Hello world test content"}),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(resp_post_no_hdr.status, 404)

        resp_post_bad_hdr = await self.client.post(
            "/_drop/submit",
            data=urlencode({"content": "Hello world test content"}),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Drop-Proxy-Secret": "invalid_secret",
            },
        )
        self.assertEqual(resp_post_bad_hdr.status, 404)

        # Valid secret succeeds
        resp_ok = await self.client.get("/_drop/healthz", headers=self._proxy_headers())
        self.assertEqual(resp_ok.status, 200)
        data = await resp_ok.json()
        self.assertTrue(data.get("ok"))

    async def test_fail_closed_when_internal_proxy_secret_is_unset(self) -> None:
        """If DROP_INTERNAL_PROXY_SECRET is empty/unset, `/_drop/*` must fail closed with 404."""
        settings.DROP_INTERNAL_PROXY_SECRET = ""
        resp = await self.client.get("/_drop/", headers={"X-Drop-Proxy-Secret": ""})
        self.assertEqual(resp.status, 404)

    # --------------------------------------------------------------------------
    # 2. Zero-JS, Privacy & Transport Hygiene (Clearnet vs .onion)
    # --------------------------------------------------------------------------

    async def test_zero_js_zero_cookie_and_security_headers_clearnet_and_onion(self) -> None:
        """Verify zero <script>, zero inline event handlers, zero external URLs, zero Set-Cookie, and strict CSP."""
        # Clearnet request
        resp_clear = await self.client.get("/_drop/", headers=self._proxy_headers("clearnet"))
        self.assertEqual(resp_clear.status, 200)
        html_clear = await resp_clear.text()

        # Zero cookies
        self.assertNotIn("Set-Cookie", resp_clear.headers)
        self.assertEqual(len(resp_clear.cookies), 0)

        # Required security headers
        self.assertIn("default-src 'none'", resp_clear.headers.get("Content-Security-Policy", ""))
        self.assertEqual(resp_clear.headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(resp_clear.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(resp_clear.headers.get("Referrer-Policy"), "no-referrer")
        self.assertIn("no-store", resp_clear.headers.get("Cache-Control", ""))

        # Zero JavaScript or inline event handlers
        self.assertNotIn("<script", html_clear.lower())
        self.assertIsNone(re.search(r"\bon(?:click|load|error|submit|change|input|focus|blur|keydown|keyup)\s*=", html_clear, re.I))

        # Inline base64 CAPTCHA image only (no external http/https image or stylesheet assets)
        self.assertIn('src="data:image/png;base64,', html_clear)
        self.assertIsNone(re.search(r'src\s*=\s*["\']https?://', html_clear, re.I))
        self.assertIsNone(re.search(r'<link[^>]+href\s*=\s*["\']https?://', html_clear, re.I))

        # Clearnet page displays the Tor Browser download link and selectable .onion address box
        self.assertIn("https://www.torproject.org/download/", html_clear)
        self.assertIn(f"http://{TEST_ONION_HOST}/", html_clear)

        # Tor .onion request
        resp_onion = await self.client.get("/_drop/", headers=self._proxy_headers("onion"))
        self.assertEqual(resp_onion.status, 200)
        html_onion = await resp_onion.text()

        self.assertNotIn("Set-Cookie", resp_onion.headers)
        self.assertNotIn("<script", html_onion.lower())
        self.assertIn("Connected directly via Tor (.onion)", html_onion)

        # CRITICAL: .onion response must contain ZERO clearnet `auahub.am` links or references
        self.assertNotIn("auahub.am", html_onion.lower())
        self.assertIsNone(re.search(r"https?://", html_onion, re.I))

    async def test_main_site_auth_cookies_are_host_only(self) -> None:
        """Audit `server.py` to ensure no Set-Cookie call sets `domain=.auahub.am`."""
        server_py = (Path(__file__).resolve().parent.parent / "app" / "web" / "server.py").read_text(encoding="utf-8")
        self.assertNotIn("domain=", server_py.lower())

    # --------------------------------------------------------------------------
    # 3. CAPTCHA, Replay, Honeypot & Spam Filters
    # --------------------------------------------------------------------------

    def test_stateless_captcha_alphabet_expiration_and_replay(self) -> None:
        """Test stateless CAPTCHA generation, unambiguous alphabet, expiration, and single-use replay blocking."""
        for _ in range(15):
            ans, _, _ = create_stateless_captcha()
            self.assertEqual(len(ans), CAPTCHA_LENGTH)
            for ch in ans:
                self.assertIn(ch, CAPTCHA_ALPHABET)
                self.assertNotIn(ch, "0OQD1Il8B")

        # Valid solve (case-insensitive)
        ans, token, _ = create_stateless_captcha(custom_answer="K7M9P2")
        ok, err = verify_stateless_captcha(token, "k7m9p2")
        self.assertTrue(ok, err)

        # Replay of the exact same valid token must fail on second submit
        ok_replay, err_replay = verify_stateless_captcha(token, "K7M9P2")
        self.assertFalse(ok_replay)
        self.assertIn("already used", err_replay.lower())

        # Expired token must fail
        _, expired_token, _ = create_stateless_captcha(custom_answer="W4X8Z3", ttl_seconds=-5)
        ok_exp, err_exp = verify_stateless_captcha(expired_token, "W4X8Z3")
        self.assertFalse(ok_exp)
        self.assertIn("expired", err_exp.lower())

    async def test_wrong_captcha_preserves_escaped_user_text(self) -> None:
        """When CAPTCHA fails, the form re-renders with a new CAPTCHA and preserves escaped user text."""
        _, token, _ = create_stateless_captcha(custom_answer="A2B3C4")
        user_draft = 'My anonymous confession with <script>alert("xss")</script> & special chars [DROP_TEST_MARKER]'

        resp = await self.client.post(
            "/_drop/submit",
            data=urlencode({
                "website": "",
                "challenge_token": token,
                "captcha_answer": "WRONG9",
                "content": user_draft,
            }),
            headers={
                **self._proxy_headers("onion"),
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        self.assertEqual(resp.status, 400)
        body = await resp.text()
        self.assertIn("Incorrect security code", body)
        # User text preserved and strictly HTML-escaped
        self.assertIn("&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt;", body)
        self.assertNotIn('<script>alert("xss")</script>', body)
        self.assertNotIn("auahub.am", body.lower())

        # Zero-JS "↻ New Code" button (`refresh_captcha=1`) generates a fresh CAPTCHA with HTTP 200 and no error banner while preserving draft text
        _, token_ref, _ = create_stateless_captcha(custom_answer="A2C3E4")
        resp_ref = await self.client.post(
            "/_drop/submit",
            data=urlencode({
                "website": "",
                "challenge_token": token_ref,
                "captcha_answer": "",
                "refresh_captcha": "1",
                "content": user_draft,
            }),
            headers={
                **self._proxy_headers("onion"),
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        self.assertEqual(resp_ref.status, 200)
        body_ref = await resp_ref.text()
        self.assertNotIn("Submission Not Accepted:", body_ref)
        self.assertIn("&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt;", body_ref)
        # Old token nonce must also be spent after refresh
        ok_old, _ = verify_stateless_captcha(token_ref, "A2C3E4")
        self.assertFalse(ok_old)

    async def test_honeypot_blocks_without_calling_gemini(self) -> None:
        """Filled honeypot (`website`) must reject immediately without invoking Gemini."""
        _, token, _ = create_stateless_captcha(custom_answer="A2B3C4")
        with patch.object(drop_ai_moderator, "screen_submission", new_callable=AsyncMock) as mock_gemini:
            resp = await self.client.post(
                "/_drop/submit",
                data=urlencode({
                    "website": "http://spambot.example",
                    "challenge_token": token,
                    "captcha_answer": "A2B3C4",
                    "content": "This is a sufficiently long post from a bot that filled the honeypot field.",
                }),
                headers={
                    **self._proxy_headers("clearnet"),
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            self.assertEqual(resp.status, 400)
            mock_gemini.assert_not_called()

    async def test_content_length_and_url_link_filters(self) -> None:
        """Verify <20 chars, >2000 chars, and URLs/links are rejected before calling Gemini."""
        with patch.object(drop_ai_moderator, "screen_submission", new_callable=AsyncMock) as mock_gemini:
            # Too short (< 20 chars)
            _, token1, _ = create_stateless_captcha(custom_answer="A2B3C4")
            resp_short = await self.client.post(
                "/_drop/submit",
                data=urlencode({
                    "website": "",
                    "challenge_token": token1,
                    "captcha_answer": "A2B3C4",
                    "content": "Too short post",
                }),
                headers={**self._proxy_headers(), "Content-Type": "application/x-www-form-urlencoded"},
            )
            self.assertEqual(resp_short.status, 400)
            self.assertIn("too short", (await resp_short.text()).lower())

            # Too long (> 2000 chars)
            _, token2, _ = create_stateless_captcha(custom_answer="B2C3D4")
            resp_long = await self.client.post(
                "/_drop/submit",
                data=urlencode({
                    "website": "",
                    "challenge_token": token2,
                    "captcha_answer": "B2C3D4",
                    "content": "A" * 2005,
                }),
                headers={**self._proxy_headers(), "Content-Type": "application/x-www-form-urlencoded"},
            )
            self.assertEqual(resp_long.status, 400)
            self.assertIn("maximum length", (await resp_long.text()).lower())

            # Contains URLs / links
            for bad_link_text in (
                "Check out this link https://example.com for free course notes at AUA",
                "Join our group at t.me/some_spam_channel right now everyone",
                "Visit www.scamsite.org or casino.xyz for prizes today students",
            ):
                _, token_url, _ = create_stateless_captcha(custom_answer="C2D3E4")
                resp_url = await self.client.post(
                    "/_drop/submit",
                    data=urlencode({
                        "website": "",
                        "challenge_token": token_url,
                        "captcha_answer": "C2D3E4",
                        "content": bad_link_text,
                    }),
                    headers={**self._proxy_headers(), "Content-Type": "application/x-www-form-urlencoded"},
                )
                self.assertEqual(resp_url.status, 400)
                self.assertIn("links, urls, and domain names", (await resp_url.text()).lower())

            mock_gemini.assert_not_called()

    # --------------------------------------------------------------------------
    # 4. AI Moderation, Fail-Closed, Duplicate Filter, Circuit Breaker & Queue Worker
    # --------------------------------------------------------------------------

    async def test_ai_moderation_rejects_doxxing_and_fails_closed_on_error(self) -> None:
        """Doxxing/PII verdict is rejected before queuing; Gemini timeout/error fails closed (503) without queuing."""
        # 1. Doxxing rejection
        _, token1, _ = create_stateless_captcha(custom_answer="D2E3F4")
        doxx_verdict = DropModerationVerdict(
            allowed=False,
            category="doxxing",
            user_message="Posts containing full names or private contact information of individuals are not allowed.",
        )
        with patch.object(
            drop_ai_moderator,
            "screen_submission",
            new=AsyncMock(return_value=(doxx_verdict, None)),
        ):
            resp_doxx = await self.client.post(
                "/_drop/submit",
                data=urlencode({
                    "website": "",
                    "challenge_token": token1,
                    "captcha_answer": "D2E3F4",
                    "content": "Some text that attempts to dox a private student [DROP_TEST_MARKER]",
                }),
                headers={**self._proxy_headers(), "Content-Type": "application/x-www-form-urlencoded"},
            )
            self.assertEqual(resp_doxx.status, 400)
            self.assertIn("private contact information", await resp_doxx.text())

        # 2. Gemini API timeout / error -> Fail-Closed (503)
        _, token2, _ = create_stateless_captcha(custom_answer="E2F3G4")
        with patch.object(
            drop_ai_moderator,
            "screen_submission",
            new=AsyncMock(return_value=(None, "Automated safety screening is temporarily unavailable.")),
        ):
            resp_err = await self.client.post(
                "/_drop/submit",
                data=urlencode({
                    "website": "",
                    "challenge_token": token2,
                    "captcha_answer": "E2F3G4",
                    "content": "Valid looking campus confession during Gemini outage [DROP_TEST_MARKER]",
                }),
                headers={**self._proxy_headers(), "Content-Type": "application/x-www-form-urlencoded"},
            )
            self.assertEqual(resp_err.status, 503)
            self.assertIn("temporarily unavailable", (await resp_err.text()).lower())

        # Verify zero rows were queued
        async with get_db_session() as session:
            rows = (
                await session.execute(
                    select(AnonymousDropQueue).where(
                        AnonymousDropQueue.content.like("%[DROP_TEST_MARKER]%")
                    )
                )
            ).scalars().all()
            self.assertEqual(len(rows), 0)

    async def test_approved_submission_queues_with_delay_buckets_timestamp_and_worker_publishes_and_purges(self) -> None:
        """Approved post enters `AnonymousDropQueue` with 120-480s delay & 5-min bucketed timestamp, then publishes & purges."""
        _, token, _ = create_stateless_captcha(custom_answer="F2G3H4")
        ok_verdict = DropModerationVerdict(
            allowed=True,
            category="ok",
            user_message="Approved.",
        )
        post_text = "Honestly the PAB library 3rd floor is the best quiet study spot during midterms [DROP_TEST_MARKER]"

        before_submit = datetime.now(timezone.utc)
        with patch.object(
            drop_ai_moderator,
            "screen_submission",
            new=AsyncMock(return_value=(ok_verdict, None)),
        ):
            resp = await self.client.post(
                "/_drop/submit",
                data=urlencode({
                    "website": "",
                    "challenge_token": token,
                    "captcha_answer": "F2G3H4",
                    "content": post_text,
                }),
                headers={**self._proxy_headers("onion"), "Content-Type": "application/x-www-form-urlencoded"},
            )
            self.assertEqual(resp.status, 200)
            body = await resp.text()
            self.assertIn("queued for anonymous publication", body)
            self.assertNotIn("auahub.am", body.lower())

            # Duplicate submission of the exact same text within 6h must be rejected
            _, token_dup, _ = create_stateless_captcha(custom_answer="G2H3J4")
            resp_dup = await self.client.post(
                "/_drop/submit",
                data=urlencode({
                    "website": "",
                    "challenge_token": token_dup,
                    "captcha_answer": "G2H3J4",
                    "content": post_text,
                }),
                headers={**self._proxy_headers("onion"), "Content-Type": "application/x-www-form-urlencoded"},
            )
            self.assertEqual(resp_dup.status, 400)
            self.assertIn("already submitted recently", await resp_dup.text())

        after_submit = datetime.now(timezone.utc)

        # Inspect queued row in DB
        async with get_db_session() as session:
            queued_row = (
                await session.execute(
                    select(AnonymousDropQueue).where(AnonymousDropQueue.content == post_text)
                )
            ).scalar_one_or_none()
            self.assertIsNotNone(queued_row)
            self.assertEqual(queued_row.status, "queued")

            # Verify 5-minute bucketed created_at
            self.assertEqual(queued_row.created_at.second, 0)
            self.assertEqual(queued_row.created_at.microsecond, 0)
            self.assertEqual(queued_row.created_at.minute % 5, 0)

            # Verify publish_after is between 120s and 480s from submission
            min_expected = before_submit + timedelta(seconds=118)
            max_expected = after_submit + timedelta(seconds=482)
            self.assertGreaterEqual(queued_row.publish_after, min_expected)
            self.assertLessEqual(queued_row.publish_after, max_expected)

            # Verify zero identifying columns exist on AnonymousDropQueue
            col_names = {c.name for c in AnonymousDropQueue.__table__.columns}
            self.assertEqual(col_names, {"id", "content", "publish_after", "status", "created_at"})

            # Fast-forward publish_after so the worker publishes it now
            queued_row.publish_after = datetime.now(timezone.utc) - timedelta(seconds=5)
            await session.commit()
            queued_id = queued_row.id

        published_cnt = await process_due_drop_queue_items()
        self.assertGreaterEqual(published_cnt, 1)

        # Verify queue row was purged and public post exists with telegram_user_id=None and source='web_drop'
        async with get_db_session() as session:
            remaining_q = (
                await session.execute(
                    select(AnonymousDropQueue).where(AnonymousDropQueue.id == queued_id)
                )
            ).scalar_one_or_none()
            self.assertIsNone(remaining_q)

            pub_post = (
                await session.execute(
                    select(AnonymousPublicPost).where(AnonymousPublicPost.content_text == post_text)
                )
            ).scalar_one_or_none()
            self.assertIsNotNone(pub_post)
            self.assertIsNone(pub_post.telegram_user_id)
            self.assertEqual(pub_post.source, "web_drop")

    async def test_global_circuit_breaker_trips_at_hourly_limit(self) -> None:
        """Verify global circuit breaker trips cleanly when 20 accepted submissions/hour is reached."""
        now_mono = time.monotonic()
        for _ in range(GLOBAL_MAX_ACCEPTED_PER_HOUR):
            drop_state.accepted_timestamps.append(now_mono)

        _, token, _ = create_stateless_captcha(custom_answer="H2J3K4")
        resp = await self.client.post(
            "/_drop/submit",
            data=urlencode({
                "website": "",
                "challenge_token": token,
                "captcha_answer": "H2J3K4",
                "content": "Another valid post when the hourly circuit breaker is full [DROP_TEST_MARKER]",
            }),
            headers={**self._proxy_headers(), "Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(resp.status, 429)
        self.assertIn("temporarily busy", (await resp.text()).lower())

    def test_gitignore_protects_tor_secret_keys(self) -> None:
        """Confirm `hs_ed25519_secret_key`, `tor_keys/`, and `hidden_service/` are in `.gitignore`."""
        gitignore = (Path(__file__).resolve().parent.parent / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("tor_keys/", gitignore)
        self.assertIn("hidden_service/", gitignore)
        self.assertIn("hs_ed25519_secret_key", gitignore)

    async def test_anonymous_post_and_comment_reactions_crud_and_privacy(self) -> None:
        """Verify anonymous reactions on Anonymous Posts and Comments toggle/switch cleanly and expose zero user identities."""
        async with get_db_session() as session:
            await session.execute(
                delete(WebUser).where(WebUser.email.in_(["react_u1@edu.aua.am", "react_u2@edu.aua.am"]))
            )
            await session.commit()

            w1 = WebUser(name="Reactor One", email="react_u1@edu.aua.am")
            w2 = WebUser(name="Reactor Two", email="react_u2@edu.aua.am")
            session.add_all([w1, w2])
            await session.commit()
            await session.refresh(w1)
            await session.refresh(w2)

            post = await create_anonymous_public_post(
                session=session,
                telegram_user_id=None,
                content_text="Testing anonymous reactions on posts and comments [DROP_TEST_MARKER]",
                source="web_drop",
            )
            _, comment, _ = await create_anon_post_comment(
                session=session,
                post_id=post.id,
                web_user_id=w1.id,
                content_text="First comment to react to!",
                is_anonymous=True,
            )

            # 1. Post reactions: w1 and w2 react with 🔥
            found1, active1, list1 = await toggle_anon_post_reaction(session, post.id, w1.id, "🔥")
            self.assertTrue(found1)
            self.assertEqual(active1, "🔥")
            self.assertEqual(list1, [{"emoji": "🔥", "count": 1, "reacted_by_me": True}])

            found2, active2, list2 = await toggle_anon_post_reaction(session, post.id, w2.id, "🔥")
            self.assertTrue(found2)
            self.assertEqual(active2, "🔥")
            self.assertEqual(list2, [{"emoji": "🔥", "count": 2, "reacted_by_me": True}])
            # Ensure zero user_ids or users list are exposed
            self.assertNotIn("user_ids", list2[0])
            self.assertNotIn("users", list2[0])

            # w1 switches reaction from 🔥 to ❤️
            _, active_switch, list_switch = await toggle_anon_post_reaction(session, post.id, w1.id, "❤️")
            self.assertEqual(active_switch, "❤️")
            by_emoji = {r["emoji"]: r for r in list_switch}
            self.assertEqual(by_emoji["🔥"]["count"], 1)
            self.assertFalse(by_emoji["🔥"]["reacted_by_me"])
            self.assertEqual(by_emoji["❤️"]["count"], 1)
            self.assertTrue(by_emoji["❤️"]["reacted_by_me"])

            # w1 toggles ❤️ off
            _, active_off, list_off = await toggle_anon_post_reaction(session, post.id, w1.id, "❤️")
            self.assertIsNone(active_off)
            self.assertEqual(list_off, [{"emoji": "🔥", "count": 1, "reacted_by_me": False}])

            # 2. Comment reactions: w2 reacts to comment with 👍
            c_found, c_active, c_list = await toggle_anon_comment_reaction(
                session, post.id, comment.id, w2.id, "👍"
            )
            self.assertTrue(c_found)
            self.assertEqual(c_active, "👍")
            self.assertEqual(c_list, [{"emoji": "👍", "count": 1, "reacted_by_me": True}])

            # Verify list_anon_post_comments includes the reaction with reacted_by_me scoped to current_user_id
            _, comments_for_w1 = await list_anon_post_comments(session, post.id, current_user_id=w1.id)
            self.assertEqual(comments_for_w1[0]["reactions"], [{"emoji": "👍", "count": 1, "reacted_by_me": False}])

            _, comments_for_w2 = await list_anon_post_comments(session, post.id, current_user_id=w2.id)
            self.assertEqual(comments_for_w2[0]["reactions"], [{"emoji": "👍", "count": 1, "reacted_by_me": True}])

            await session.execute(
                delete(WebUser).where(WebUser.id.in_([w1.id, w2.id]))
            )
            await session.commit()


if __name__ == "__main__":
    unittest.main()
