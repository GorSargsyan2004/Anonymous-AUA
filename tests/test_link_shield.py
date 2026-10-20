"""Unit tests for Link Shield & Anti-Phishing service."""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.types import Message, User
from app.services.link_shield import (
    LinkScanVerdict,
    LinkShield,
    link_shield,
)


def make_message(text: str, entities: list = None, caption: str = None, user_id: int = 12345) -> Message:
    msg = MagicMock(spec=Message)
    msg.message_id = 999
    msg.text = text
    msg.caption = caption
    msg.entities = entities or []
    msg.caption_entities = []
    msg.photo = None
    msg.video = None
    msg.document = None
    msg.audio = None
    msg.animation = None
    msg.voice = None
    msg.from_user = MagicMock(spec=User)
    msg.from_user.id = user_id
    msg.reply_to_message = None
    msg.external_reply = None
    msg.media_group_id = None
    msg.reply = AsyncMock()
    msg.answer = AsyncMock()
    msg.copy_to = AsyncMock()
    return msg


class TestLinkShield(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.shield = LinkShield()

    def test_ip_logger_detection(self):
        # Direct domains
        self.assertTrue(self.shield.is_ip_logger("grabify.link"))
        self.assertTrue(self.shield.is_ip_logger("iplogger.org"))
        self.assertTrue(self.shield.is_ip_logger("2no.co"))
        self.assertTrue(self.shield.is_ip_logger("blasze.com"))
        self.assertTrue(self.shield.is_ip_logger("lovebird.guru"))
        self.assertTrue(self.shield.is_ip_logger("curiouscat.club"))

        # Subdomains
        self.assertTrue(self.shield.is_ip_logger("track.grabify.link"))
        self.assertTrue(self.shield.is_ip_logger("sub.iplogger.org"))

        # Clean domains
        self.assertFalse(self.shield.is_ip_logger("instagram.com"))
        self.assertFalse(self.shield.is_ip_logger("aua.am"))
        self.assertFalse(self.shield.is_ip_logger("google.com"))

    def test_dangerous_file_detection(self):
        self.assertTrue(self.shield.has_dangerous_file("/downloads/game.apk"))
        self.assertTrue(self.shield.has_dangerous_file("/files/setup.exe"))
        self.assertTrue(self.shield.has_dangerous_file("/script.scr"))
        self.assertTrue(self.shield.has_dangerous_file("/setup.bat"))
        self.assertTrue(self.shield.has_dangerous_file("/install.msi"))

        # Query params
        self.assertTrue(self.shield.has_dangerous_file("/download.apk?token=123"))

        # Clean paths
        self.assertFalse(self.shield.has_dangerous_file("/song.mp3"))
        self.assertFalse(self.shield.has_dangerous_file("/image.png"))
        self.assertFalse(self.shield.has_dangerous_file("/p/B9xyz123/"))

    def test_trusted_domains(self):
        self.assertTrue(self.shield.is_trusted("aua.am"))
        self.assertTrue(self.shield.is_trusted("instagram.com"))
        self.assertTrue(self.shield.is_trusted("open.spotify.com"))
        self.assertTrue(self.shield.is_trusted("youtu.be"))
        self.assertTrue(self.shield.is_trusted("youtube.com"))
        self.assertTrue(self.shield.is_trusted("t.me"))
        self.assertFalse(self.shield.is_trusted("random-suspicious-site.xyz"))

    async def test_scan_message_no_links(self):
        msg = make_message("Hey, how was your CS class today?")
        res = await self.shield.scan_message(msg)
        self.assertFalse(res.has_links)
        self.assertFalse(res.is_blocked)

    async def test_scan_message_trusted_link(self):
        msg = make_message("Listen to this song: https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT")
        res = await self.shield.scan_message(msg)
        self.assertTrue(res.has_links)
        self.assertFalse(res.is_blocked)
        self.assertTrue(res.is_all_trusted)
        self.assertEqual(res.verdict, LinkScanVerdict.SAFE_TRUSTED)

    async def test_scan_message_direct_ip_logger(self):
        msg = make_message("Click here to see pictures https://grabify.link/ABC123")
        res = await self.shield.scan_message(msg)
        self.assertTrue(res.has_links)
        self.assertTrue(res.is_blocked)
        self.assertEqual(res.verdict, LinkScanVerdict.BLOCKED_IP_LOGGER)
        self.assertIn("IP-tracking domain", res.block_reason)

    async def test_scan_message_dangerous_apk(self):
        msg = make_message("Download the new app: https://suspicious-site.com/hacked-game.apk")
        res = await self.shield.scan_message(msg)
        self.assertTrue(res.has_links)
        self.assertTrue(res.is_blocked)
        self.assertEqual(res.verdict, LinkScanVerdict.BLOCKED_DANGEROUS_FILE)

    async def test_scan_message_shortener_expanding_to_logger(self):
        msg = make_message("Vote for my photo here: https://bit.ly/fake-photo-contest")

        with patch.object(self.shield, "unshorten_url", new=AsyncMock(return_value="https://grabify.link/stolen-token")):
            res = await self.shield.scan_message(msg)
            self.assertTrue(res.has_links)
            self.assertTrue(res.is_blocked)
            self.assertEqual(res.verdict, LinkScanVerdict.BLOCKED_IP_LOGGER)
            self.assertIn("expands to IP-tracker", res.block_reason)

    async def test_scan_message_cloudflare_malware_detection(self):
        msg = make_message("Check this link: https://known-phishing-login.com")

        # Mock Cloudflare DoH flagging domain as malware
        with patch.object(self.shield, "check_cloudflare_doh", new=AsyncMock(return_value=True)):
            res = await self.shield.scan_message(msg)
            self.assertTrue(res.has_links)
            self.assertTrue(res.is_blocked)
            self.assertEqual(res.verdict, LinkScanVerdict.BLOCKED_MALWARE)

    async def test_scan_message_general_external_allowed(self):
        msg = make_message("Interesting article on https://medium.com/some-tech-blog")

        with patch.object(self.shield, "check_cloudflare_doh", new=AsyncMock(return_value=False)):
            res = await self.shield.scan_message(msg)
            self.assertTrue(res.has_links)
            self.assertFalse(res.is_blocked)
            self.assertFalse(res.is_all_trusted)
            self.assertEqual(res.verdict, LinkScanVerdict.SAFE_EXTERNAL)
            self.assertIn("medium.com", res.final_domains)


if __name__ == "__main__":
    unittest.main()
