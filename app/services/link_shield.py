"""Link Shield & Anti-Phishing Service.

Provides a multi-tier defense system against IP-loggers, phishing pages,
malicious executables, and URL shortener traps in anonymous chat messages.
"""

from dataclasses import dataclass, field
from enum import Enum
import logging
import re
import time
from typing import Optional
from urllib.parse import urlparse

from aiogram import types
import aiohttp

logger = logging.getLogger(__name__)

# Known IP logger and tracking domains
IP_LOGGER_DOMAINS = {
    # Grabify domains
    "grabify.link",
    "bmwforum.co",
    "leancoding.co",
    "spottyfly.com",
    "stopify.co",
    "yooutube.co",
    "discörd.com",
    "minecräft.com",
    "freegiftcards.co",
    "sentry.site",
    "quickstats.club",
    "joinmy.site",
    "dateing.club",
    "otheroptions.club",
    "yourmy.vip",
    "lovebird.guru",
    "trck.at",
    "photovault.life",
    "screenshare.site",
    "gamingfun.me",
    "catsnthings.fun",
    "mymassive.vip",
    "curiouscat.club",
    # IPLogger domains
    "iplogger.org",
    "iplogger.com",
    "iplogger.ru",
    "iplogger.info",
    "iplogger.net",
    "iplogger.co",
    "2no.co",
    "yip.su",
    "iplis.ru",
    "02ip.ru",
    "ezstat.ru",
    "sh2no.co",
    "locations.zone",
    "pogoda.pro",
    "myip.ms",
    # Blasze / PS3CFW
    "blasze.com",
    "blasze.tk",
    "ps3cfw.com",
    # Other known loggers & tracking redirectors
    "ipgrabber.net",
    "trackip.net",
    "whatismyip.host",
    "spyshare.net",
}

# Dangerous executable file extensions in URLs
DANGEROUS_EXTENSIONS = {
    ".apk",
    ".exe",
    ".scr",
    ".bat",
    ".cmd",
    ".msi",
    ".vbs",
    ".ps1",
    ".com",
    ".pif",
    ".jar",
    ".dmg",
}

# Common URL shorteners used to conceal destination URLs
SHORTENER_DOMAINS = {
    "bit.ly",
    "tinyurl.com",
    "t.ly",
    "is.gd",
    "cutt.ly",
    "rb.gy",
    "shorturl.at",
    "t.co",
    "buff.ly",
    "ow.ly",
    "tiny.cc",
    "bc.vc",
    "shorte.st",
    "v.gd",
    "clck.ru",
    "goo.gl",
    "qr.ae",
    "adf.ly",
    "rebrand.ly",
    "bl.ink",
    "lnkd.in",
    "s.id",
}

# Reputable, trusted platforms widely used by students
TRUSTED_DOMAINS = {
    "aua.am",
    "instagram.com",
    "instagr.am",
    "spotify.com",
    "open.spotify.com",
    "youtube.com",
    "youtu.be",
    "music.youtube.com",
    "tiktok.com",
    "pinterest.com",
    "pin.it",
    "linkedin.com",
    "github.com",
    "t.me",
    "telegram.org",
    "apple.com",
    "music.apple.com",
    "soundcloud.com",
    "threads.net",
    "twitter.com",
    "x.com",
    "google.com",
    "drive.google.com",
    "docs.google.com",
    "wikipedia.org",
    "fb.com",
    "facebook.com",
}

# Regex to capture raw URLs and bare domain links in text
URL_REGEX = re.compile(
    r"(?:https?://|www\.)[^\s<>'\"`()]+|"
    r"(?:[a-zA-Z0-9-]+\.)+(?:com|org|net|link|ru|co|site|club|vip|at|io|app|dev|xyz|info|top|su|online|fun|guru|tech|tk|ml|ga|cf|gq)(?:/[^\s<>'\"`()]*)?",
    re.IGNORECASE,
)

# In-memory TTL cache for Cloudflare DoH queries: domain -> (is_malware, timestamp)
_CLOUDFLARE_CACHE: dict[str, tuple[bool, float]] = {}
CACHE_TTL_SECONDS = 3600.0  # 1 hour


class LinkScanVerdict(str, Enum):
    SAFE_TRUSTED = "safe_trusted"
    SAFE_EXTERNAL = "safe_external"
    BLOCKED_IP_LOGGER = "blocked_ip_logger"
    BLOCKED_MALWARE = "blocked_malware"
    BLOCKED_DANGEROUS_FILE = "blocked_dangerous_file"


@dataclass
class LinkScanResult:
    has_links: bool = False
    is_blocked: bool = False
    verdict: LinkScanVerdict = LinkScanVerdict.SAFE_EXTERNAL
    block_reason: str = ""
    detected_links: list[str] = field(default_factory=list)
    final_domains: list[str] = field(default_factory=list)
    unshortened_mappings: dict[str, str] = field(default_factory=dict)
    is_all_trusted: bool = False


class LinkShield:
    """Multi-layer asynchronous link analysis engine."""

    def __init__(self) -> None:
        pass

    @staticmethod
    def extract_links(message: types.Message) -> list[str]:
        """Extract all URLs from message text, caption, and Telegram entities."""
        links: set[str] = set()

        # 1. Telegram text entities (formatting links [text](url) and raw URLs)
        text = message.text or ""
        if message.entities:
            for entity in message.entities:
                if entity.type == "url":
                    links.add(text[entity.offset : entity.offset + entity.length])
                elif entity.type == "text_link" and entity.url:
                    links.add(entity.url)

        # 2. Telegram caption entities
        caption = message.caption or ""
        if message.caption_entities:
            for entity in message.caption_entities:
                if entity.type == "url":
                    links.add(caption[entity.offset : entity.offset + entity.length])
                elif entity.type == "text_link" and entity.url:
                    links.add(entity.url)

        # 3. Regex scan over combined text to catch bare unformatted URLs
        full_text = f"{text}\n{caption}".strip()
        if full_text:
            for match in URL_REGEX.finditer(full_text):
                links.add(match.group(0))

        return list(links)

    @staticmethod
    def parse_domain(url: str) -> tuple[str, str]:
        """Extract (clean_domain, path) from a URL string."""
        url_to_parse = url if url.startswith(("http://", "https://")) else f"http://{url}"
        try:
            parsed = urlparse(url_to_parse)
            domain = (parsed.hostname or "").lower()
            if domain.startswith("www."):
                domain = domain[4:]
            return domain, parsed.path.lower()
        except Exception:
            return "", ""

    @staticmethod
    def is_ip_logger(domain: str) -> bool:
        """Check if domain is a known IP logger or tracking service."""
        domain = domain.lower()
        for logger_dom in IP_LOGGER_DOMAINS:
            if domain == logger_dom or domain.endswith("." + logger_dom):
                return True
        return False

    @staticmethod
    def has_dangerous_file(path: str) -> bool:
        """Check if URL path targets dangerous executables (.apk, .exe, etc.)."""
        path_lower = path.lower()
        for ext in DANGEROUS_EXTENSIONS:
            if path_lower.endswith(ext) or f"{ext}?" in path_lower or f"{ext}/" in path_lower:
                return True
        return False

    @staticmethod
    def is_shortener(domain: str) -> bool:
        """Check if domain is a known URL shortener."""
        domain = domain.lower()
        for s in SHORTENER_DOMAINS:
            if domain == s or domain.endswith("." + s):
                return True
        return False

    @staticmethod
    def is_trusted(domain: str) -> bool:
        """Check if domain belongs to a trusted platform (Instagram, Spotify, AUA, etc.)."""
        domain = domain.lower()
        for t in TRUSTED_DOMAINS:
            if domain == t or domain.endswith("." + t):
                return True
        return False

    async def unshorten_url(self, url: str) -> str:
        """Asynchronously resolve shortened link to its final target."""
        url_to_fetch = url if url.startswith(("http://", "https://")) else f"https://{url}"
        timeout = aiohttp.ClientTimeout(total=2.0)
        headers = {"User-Agent": "Mozilla/5.0 (compatible; AUAAnonymousBot/1.0; +https://t.me/aua_anonymous_bot)"}

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                try:
                    async with session.head(url_to_fetch, allow_redirects=True, headers=headers) as resp:
                        return str(resp.url)
                except Exception:
                    # Fallback to GET with small byte range if server rejects HEAD
                    headers["Range"] = "bytes=0-10"
                    async with session.get(url_to_fetch, allow_redirects=True, headers=headers) as resp:
                        return str(resp.url)
        except Exception as e:
            logger.debug("Could not unshorten URL %s: %s", url, e)
            return url

    async def check_cloudflare_doh(self, domain: str) -> bool:
        """Query Cloudflare 1.1.1.2 DoH (Malware Blocking) to verify domain safety.
        
        Returns True if domain is flagged as MALWARE or PHISHING by Cloudflare.
        """
        if not domain or self.is_trusted(domain):
            return False

        now = time.time()
        if domain in _CLOUDFLARE_CACHE:
            is_malware, ts = _CLOUDFLARE_CACHE[domain]
            if now - ts < CACHE_TTL_SECONDS:
                return is_malware

        url = f"https://security.cloudflare-dns.com/dns-query?name={domain}&type=A"
        headers = {
            "Accept": "application/dns-json",
            "User-Agent": "AUAAnonymousBot/1.0",
        }
        timeout = aiohttp.ClientTimeout(total=1.5)

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        answers = data.get("Answer", [])
                        for ans in answers:
                            # Cloudflare Security 1.1.1.2 returns 0.0.0.0 for malware/phishing
                            if ans.get("data") == "0.0.0.0":
                                _CLOUDFLARE_CACHE[domain] = (True, now)
                                logger.warning("Cloudflare DoH flagged domain as malware: %s", domain)
                                return True
                        _CLOUDFLARE_CACHE[domain] = (False, now)
                        return False
        except Exception as e:
            logger.debug("Cloudflare DoH check skipped for %s: %s", domain, e)

        return False

    async def scan_message(self, message: types.Message) -> LinkScanResult:
        """Comprehensive scan of all links inside a message.
        
        Enforces:
        1. IP-logger blocking (Grabify, IPLogger, etc.)
        2. Dangerous executable extension blocking (.apk, .exe)
        3. Real-time URL unshortening (exposes hidden loggers)
        4. Cloudflare DoH malware checking
        5. Trusted domain categorization
        """
        raw_links = self.extract_links(message)
        if not raw_links:
            return LinkScanResult(has_links=False)

        result = LinkScanResult(
            has_links=True,
            detected_links=raw_links,
        )

        all_trusted = True

        for raw_url in raw_links:
            domain, path = self.parse_domain(raw_url)
            if not domain:
                continue

            # Layer 1: Immediate IP-Logger Check
            if self.is_ip_logger(domain):
                result.is_blocked = True
                result.verdict = LinkScanVerdict.BLOCKED_IP_LOGGER
                result.block_reason = f"IP-tracking domain detected ({domain})"
                logger.warning("Blocked IP-logger link: %s (domain: %s)", raw_url, domain)
                return result

            # Layer 2: Executable File Check
            if self.has_dangerous_file(path):
                result.is_blocked = True
                result.verdict = LinkScanVerdict.BLOCKED_DANGEROUS_FILE
                result.block_reason = "Suspicious executable file download (.apk, .exe)"
                logger.warning("Blocked dangerous file link: %s", raw_url)
                return result

            # Layer 3: URL Unshortener
            final_domain = domain
            if self.is_shortener(domain):
                unshortened = await self.unshorten_url(raw_url)
                result.unshortened_mappings[raw_url] = unshortened
                u_domain, u_path = self.parse_domain(unshortened)

                if self.is_ip_logger(u_domain):
                    result.is_blocked = True
                    result.verdict = LinkScanVerdict.BLOCKED_IP_LOGGER
                    result.block_reason = f"Shortened link expands to IP-tracker ({u_domain})"
                    logger.warning("Blocked disguised IP-logger %s -> %s", raw_url, unshortened)
                    return result

                if self.has_dangerous_file(u_path):
                    result.is_blocked = True
                    result.verdict = LinkScanVerdict.BLOCKED_DANGEROUS_FILE
                    result.block_reason = "Shortened link points to unsafe executable"
                    return result

                final_domain = u_domain

            if final_domain:
                result.final_domains.append(final_domain)

            # Layer 4: Cloudflare DoH Malware Verification
            if not self.is_trusted(final_domain):
                all_trusted = False
                is_malware = await self.check_cloudflare_doh(final_domain)
                if is_malware:
                    result.is_blocked = True
                    result.verdict = LinkScanVerdict.BLOCKED_MALWARE
                    result.block_reason = f"Domain flagged as malware or phishing ({final_domain})"
                    return result
            else:
                pass

        result.is_all_trusted = all_trusted
        if all_trusted:
            result.verdict = LinkScanVerdict.SAFE_TRUSTED
        else:
            result.verdict = LinkScanVerdict.SAFE_EXTERNAL

        return result


# Global singleton instance
link_shield = LinkShield()
