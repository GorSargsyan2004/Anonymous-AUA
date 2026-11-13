"""Zero-Knowledge Ephemeral RAM Buffer & Automated Multimodal Gemini AI Moderator.

Protects student privacy by holding recent session messages in volatile RAM only
(no DB or disk logs). Automatically destroys buffered turns after 5 minutes (TTL).
On report, evaluates the conversation using Google Gemini (gemini-2.5-flash) with
bidirectional accountability for both reporter and reported user.
"""

import asyncio
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
import html
import logging
import re
import time
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, Field

try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None
    types = None

from app.config import settings
from app.database.crud import (
    add_or_update_user_block,
    apply_admin_penalty,
    ban_user_by_admin,
    create_report,
    get_user,
)
from app.services.ban_manager import add_banned_user

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data Models & Schemas
# ---------------------------------------------------------------------------

@dataclass
class SessionTurn:
    """Represents a single message turn held in volatile RAM."""
    sender_id: int
    text: Optional[str] = None
    image_bytes: Optional[bytes] = None
    had_image_evicted: bool = False
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class ModerationVerdict(BaseModel):
    """Structured output verdict produced by Gemini AI Moderator."""
    guilty_party: Literal["Reported_User", "Reporter", "Both", "Neither"]
    severity_category: Literal[
        "None",
        "Minor_Spam_Rudeness",
        "Privacy_Violation_Doxxing",
        "Severe_Harassment_NSFW",
        "Illegal_Extreme",
    ]
    reported_user_penalty: int = Field(ge=0, le=10)
    reporter_penalty: int = Field(ge=0, le=10)
    block_reported_user: bool
    block_reporter: bool
    reveal_identity_for_illegal_content: bool
    illegal_perpetrator: Literal["Reported_User", "Reporter", "Both", "None"]
    anonymized_summary: str


# ---------------------------------------------------------------------------
# 4-Layer Protected Ephemeral RAM Buffer
# ---------------------------------------------------------------------------

class SessionBufferEntry:
    """Tracks turns and rate limits for an active or recently completed session."""
    def __init__(self, session_id: Any, maxlen: int = 60):
        self.session_id: Any = session_id
        self.turns: deque[SessionTurn] = deque(maxlen=maxlen)
        # Tracks per-user image activity: user_id -> {'count': int, 'last_image_time': float}
        self.user_image_stats: Dict[int, Dict[str, Any]] = defaultdict(
            lambda: {"count": 0, "last_image_time": 0.0}
        )
        self.cleanup_task: Optional[asyncio.Task] = None


class ProtectedSessionBuffer:
    """4-Layer Protected Ephemeral RAM Buffer (Zero-Knowledge).

    1. Bounded Circular Queue (deque maxlen=60, text truncated to 500 chars).
    2. Anti-Flood Per-Session Image Rate Limiting (max 1 image / 10s, max 10 images / session).
    3. Compressed Medium Image Capture & Sliding Window Eviction (max 8 images in RAM).
    4. Auto-Destruction TTL (5 minutes after live chat ends; 24h for inbox Q&A messages).
    """

    MAX_TURNS: int = 60
    MAX_TEXT_LENGTH: int = 500
    MAX_IMAGES_IN_BUFFER: int = 8
    IMAGE_RATE_LIMIT_SECONDS: float = 10.0
    MAX_IMAGES_PER_USER_SESSION: int = 10
    SESSION_TTL_SECONDS: int = 300  # 5 minutes
    INBOX_TTL_SECONDS: int = 86400  # 24 hours

    def __init__(self) -> None:
        self._sessions: Dict[Any, SessionBufferEntry] = {}
        self._lock = asyncio.Lock()

    def _get_or_create(self, session_id: Any) -> SessionBufferEntry:
        if session_id not in self._sessions:
            self._sessions[session_id] = SessionBufferEntry(session_id, maxlen=self.MAX_TURNS)
        return self._sessions[session_id]

    def check_image_rate_limit(self, session_id: Any, user_id: int) -> Tuple[bool, str]:
        """Verify if the user is allowed to send an image in this session.

        Rules:
        - Max 1 image every 10 seconds.
        - Max 10 images total per session per user.

        Returns:
            (allowed: bool, reason_message: str)
        """
        entry = self._get_or_create(session_id)
        stats = entry.user_image_stats[user_id]
        now = time.monotonic()

        # Check total per-session image quota
        if stats["count"] >= self.MAX_IMAGES_PER_USER_SESSION:
            return (
                False,
                "⚠️ <b>Image limit reached:</b> To protect user privacy and prevent spam, "
                f"you can send up to {self.MAX_IMAGES_PER_USER_SESSION} images per chat session.",
            )

        # Check interval between consecutive images
        time_since_last = now - stats["last_image_time"]
        if stats["last_image_time"] > 0 and time_since_last < self.IMAGE_RATE_LIMIT_SECONDS:
            retry_after = int(self.IMAGE_RATE_LIMIT_SECONDS - time_since_last) + 1
            return (
                False,
                f"⏳ <b>Image cooldown active:</b> Please wait {retry_after}s before sending another photo.",
            )

        return True, ""

    def record_image_sent(self, session_id: Any, user_id: int) -> None:
        """Record an image sent by a user to update rate-limiting statistics."""
        entry = self._get_or_create(session_id)
        stats = entry.user_image_stats[user_id]
        stats["count"] += 1
        stats["last_image_time"] = time.monotonic()

    def add_text(self, session_id: Any, user_id: int, text: str) -> None:
        """Store a text message turn in the ephemeral circular queue."""
        if not text:
            return
        entry = self._get_or_create(session_id)
        truncated_text = text[: self.MAX_TEXT_LENGTH]
        entry.turns.append(
            SessionTurn(
                sender_id=user_id,
                text=truncated_text,
                image_bytes=None,
                had_image_evicted=False,
            )
        )

    def add_image(
        self,
        session_id: Any,
        user_id: int,
        image_bytes: bytes,
        caption: Optional[str] = None,
    ) -> None:
        """Store a medium compressed image turn in volatile memory with sliding eviction."""
        entry = self._get_or_create(session_id)
        self.record_image_sent(session_id, user_id)

        truncated_caption = caption[: self.MAX_TEXT_LENGTH] if caption else None

        # Check if sliding eviction is needed (max 8 images per session)
        current_images = [turn for turn in entry.turns if turn.image_bytes is not None]
        if len(current_images) >= self.MAX_IMAGES_IN_BUFFER:
            # Evict the oldest image bytes to free RAM immediately
            oldest = current_images[0]
            oldest.image_bytes = None
            oldest.had_image_evicted = True

        entry.turns.append(
            SessionTurn(
                sender_id=user_id,
                text=truncated_caption,
                image_bytes=image_bytes,
                had_image_evicted=False,
            )
        )

    def schedule_cleanup(self, session_id: Any, ttl: int = SESSION_TTL_SECONDS) -> None:
        """Schedule automatic deletion of the session buffer after TTL seconds."""
        entry = self._sessions.get(session_id)
        if not entry:
            return

        if entry.cleanup_task and not entry.cleanup_task.done():
            entry.cleanup_task.cancel()

        async def _cleanup():
            try:
                await asyncio.sleep(ttl)
                self._sessions.pop(session_id, None)
                logger.debug("Ephemeral session buffer %s auto-destroyed after %ds TTL", session_id, ttl)
            except asyncio.CancelledError:
                pass

        entry.cleanup_task = asyncio.create_task(_cleanup())

    def pop_session(self, session_id: Any) -> Optional[List[SessionTurn]]:
        """Atomically pop the buffer for evaluation, immediately erasing it from RAM."""
        entry = self._sessions.pop(session_id, None)
        if not entry:
            return None

        if entry.cleanup_task and not entry.cleanup_task.done():
            entry.cleanup_task.cancel()

        return list(entry.turns)

    def has_session(self, session_id: Any) -> bool:
        """Check if an active or unexpired buffer exists for a session."""
        return session_id in self._sessions

    def clear_for_testing(self) -> None:
        """Reset all in-memory buffers (used in test teardowns)."""
        for entry in self._sessions.values():
            if entry.cleanup_task and not entry.cleanup_task.done():
                entry.cleanup_task.cancel()
        self._sessions.clear()


session_buffer = ProtectedSessionBuffer()


# ---------------------------------------------------------------------------
# Bidirectional Multimodal Gemini AI Moderator
# ---------------------------------------------------------------------------

GEMINI_SYSTEM_INSTRUCTION = """You are the automated AI Safety and Moderation Judge for the Anonymous AUA (American University of Armenia) Chat Bot.
Your mission is to evaluate an ephemeral chat conversation between two anonymous university students: [Reporter] and [Reported_User].
You must judge BOTH participants fairly and impartially based on the Community Code of Conduct and Privacy Rules.

COMMUNITY CODE OF CONDUCT & JUDGING CRITERIA:

0. Language Context:
- AUA students communicate in English, Armenian (in standard Armenian script as well as Latin transliteration / Armlish, e.g. "vonc es axper", "inch ka", "lav em", slang, idioms, or insults/threats in Latin script), and Russian.
- You must accurately comprehend Latin-script Armenian (Armlish), colloquial youth slang, and transliteration when judging the tone and violations in the conversation.

1. Rule A: Privacy Violations & Anti-Doxxing
- Sharing, guessing, outing, or threatening to expose another student's real full name, phone number, residential address, student ID, social media handles, or private photos without explicit consent.
- Repeatedly pressuring, coercing, or stalking a partner to reveal their identity after they have declined.
- Outing or blackmailing someone regarding their sexual orientation or private statements.
-> Severity: "Privacy_Violation_Doxxing", Penalty: 6 to 10 points, Block: True.

2. Rule B: Harassment, NSFW, and Abuse
- Unsolicited explicit media (cyber-flashing): sending sexually explicit photos or videos without consent.
- Hate speech, slurs, severe degradation, or sustained abusive bullying (including text within images detected via OCR).
-> Severity: "Severe_Harassment_NSFW", Penalty: 6 to 10 points, Block: True.
- Minor trolling, mild rudeness, commercial links, or spam.
-> Severity: "Minor_Spam_Rudeness", Penalty: 1 to 5 points, Block: False (or True if persistent).

3. Rule C: Reporter Accountability (Crucial: Filing a report grants NO immunity)
- If the Reporter themselves harassed, sent unsolicited NSFW content, or doxxed the partner:
  guilty_party = "Reporter", apply full penalty (1-10) and block_reporter = True.
- If the Reporter intentionally baited, provoked, or insulted the partner to elicit an angry response:
  guilty_party = "Both" or "Reporter", penalize both or the Reporter.
- If the report is Frivolous / Spiteful / False (the conversation was innocent, respectful, or the partner simply declined to share social info or politely left the chat):
  guilty_party = "Reporter", severity_category = "Minor_Spam_Rudeness", reporter_penalty = 2 to 5 points, reported_user_penalty = 0, block_reporter = False.

4. Rule D: Tier 3 Illegal / Extreme Content (Identity Unmasking)
- ONLY set reveal_identity_for_illegal_content = True if there is:
  * Child Sexual Abuse Material (CSAM) or exploitation.
  * Credible threats of physical violence, mass harm, terrorism, or encouraging suicide/self-harm.
  * Extortion, blackmail, or non-consensual distribution of intimate images (revenge porn).
- If present, set severity_category = "Illegal_Extreme", reveal_identity_for_illegal_content = True, illegal_perpetrator = ("Reported_User" | "Reporter" | "Both"), penalty = 10, block = True.
- Otherwise, reveal_identity_for_illegal_content = False and illegal_perpetrator = "None".

OUTPUT FORMAT:
Return a JSON object conforming strictly to the ModerationVerdict schema.
Ensure anonymized_summary provides a concise, objective description without revealing private identities.
"""


class GeminiModerator:
    """Automated multimodal AI judge using google-genai and gemini-2.5-flash."""

    def __init__(self, api_key: Optional[str] = None) -> None:
        self.api_key: Optional[str] = api_key or settings.GEMINI_API_KEY
        self._semaphore = asyncio.Semaphore(3)
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            key = self.api_key or settings.GEMINI_API_KEY
            if not key or genai is None:
                return None
            self._client = genai.Client(api_key=key)
        return self._client

    async def evaluate_session(
        self,
        turns: List[SessionTurn],
        reporter_id: int,
        reported_id: int,
        reason_text: str,
    ) -> ModerationVerdict:
        """Evaluate chat turns with gemini-2.5-flash and return a structured verdict."""
        async with self._semaphore:
            client = self._get_client()

            # Fallback for environments without API key or test environments
            if client is None or types is None:
                logger.warning("Gemini AI client not available. Executing heuristic fallback.")
                return self._fallback_evaluate(turns, reporter_id, reported_id, reason_text)

            # Build multimodal contents list
            contents: List[Any] = [
                f"Complaint filed by Reporter: {reason_text}\n"
                f"Total chronological turns: {len(turns)}\n\n"
                "Conversation Transcript (labeled by role):\n"
            ]

            for idx, turn in enumerate(turns, start=1):
                role = "Reporter" if turn.sender_id == reporter_id else "Reported_User"
                if turn.text:
                    contents.append(f"Turn #{idx} [{role}]: {turn.text}")

                if turn.image_bytes:
                    try:
                        part = types.Part.from_bytes(data=turn.image_bytes, mime_type="image/jpeg")
                        contents.append(f"Turn #{idx} [{role}] attached image:")
                        contents.append(part)
                    except Exception as e:
                        logger.error("Failed to package image bytes for turn %d: %s", idx, e)

                if turn.had_image_evicted:
                    contents.append(f"Turn #{idx} [{role}]: [Older image bytes evicted from RAM buffer]")

            try:
                config = types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=ModerationVerdict,
                    temperature=0.1,
                    media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW,
                    system_instruction=GEMINI_SYSTEM_INSTRUCTION,
                )

                response = await client.aio.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=contents,
                    config=config,
                )

                if hasattr(response, "parsed") and response.parsed is not None:
                    return response.parsed

                return ModerationVerdict.model_validate_json(response.text)

            except Exception as e:
                logger.exception("Gemini AI evaluation failed: %s", e)
                return self._fallback_evaluate(turns, reporter_id, reported_id, reason_text)

    def _fallback_evaluate(
        self,
        turns: List[SessionTurn],
        reporter_id: int,
        reported_id: int,
        reason_text: str,
    ) -> ModerationVerdict:
        """Heuristic evaluation when Gemini API is unavailable (or during offline tests)."""
        reason_lower = (reason_text or "").lower()
        all_text = " ".join([t.text or "" for t in turns]).lower()

        # Check for Tier 3 illegal keywords
        illegal_keywords = ["csam", "kill you", "bomb", "suicide", "terrorist", "extort money or leak"]
        if any(kw in all_text for kw in illegal_keywords) or any(kw in reason_lower for kw in illegal_keywords):
            return ModerationVerdict(
                guilty_party="Reported_User",
                severity_category="Illegal_Extreme",
                reported_user_penalty=10,
                reporter_penalty=0,
                block_reported_user=True,
                block_reporter=False,
                reveal_identity_for_illegal_content=True,
                illegal_perpetrator="Reported_User",
                anonymized_summary="Heuristic detected severe illegal threats or extortion.",
            )

        # Check for doxxing / privacy violations
        doxx_keywords = ["real name is", "phone number is", "instagram is @", "i know where you live"]
        if any(kw in all_text for kw in doxx_keywords):
            return ModerationVerdict(
                guilty_party="Reported_User",
                severity_category="Privacy_Violation_Doxxing",
                reported_user_penalty=7,
                reporter_penalty=0,
                block_reported_user=True,
                block_reporter=False,
                reveal_identity_for_illegal_content=False,
                illegal_perpetrator="None",
                anonymized_summary="Heuristic detected potential exposure of personal identifiable information.",
            )

        # Check for harassment / NSFW
        nsfw_keywords = ["nude", "send pic or die", "bitch", "whore", "idiot"]
        if any(kw in all_text for kw in nsfw_keywords):
            return ModerationVerdict(
                guilty_party="Reported_User",
                severity_category="Severe_Harassment_NSFW",
                reported_user_penalty=6,
                reporter_penalty=0,
                block_reported_user=True,
                block_reporter=False,
                reveal_identity_for_illegal_content=False,
                illegal_perpetrator="None",
                anonymized_summary="Heuristic detected offensive or abusive language.",
            )

        # Default fallback: Minor rudeness or false report
        if len(turns) <= 2:
            return ModerationVerdict(
                guilty_party="Neither",
                severity_category="None",
                reported_user_penalty=0,
                reporter_penalty=0,
                block_reported_user=True,
                block_reporter=False,
                reveal_identity_for_illegal_content=False,
                illegal_perpetrator="None",
                anonymized_summary="Brief conversation with insufficient turns to establish violation.",
            )

        return ModerationVerdict(
            guilty_party="Reported_User",
            severity_category="Minor_Spam_Rudeness",
            reported_user_penalty=2,
            reporter_penalty=0,
            block_reported_user=True,
            block_reporter=False,
            reveal_identity_for_illegal_content=False,
            illegal_perpetrator="None",
            anonymized_summary="Report evaluated; minor conduct penalty applied.",
        )

    async def evaluate_anonymous_post(self, text: str) -> Literal["VALID", "SPAM"]:
        """Tier 2 AI Content Moderation for anonymous student posts via Google Gemini API.

        Returns 'VALID' if coherent/meaningful, or 'SPAM' if gibberish/test spam.
        """
        cleaned = (text or "").strip()
        if not cleaned:
            return "SPAM"

        async with self._semaphore:
            client = self._get_client()
            if client is None or types is None:
                return self._fallback_evaluate_anonymous_post(cleaned)

            try:
                config = types.GenerateContentConfig(
                    system_instruction=ANON_POST_MODERATION_SYSTEM_INSTRUCTION,
                    temperature=0.0,
                    max_output_tokens=16,
                )
                response = await client.aio.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=cleaned,
                    config=config,
                )
                raw_reply = (getattr(response, "text", None) or "").strip().upper()
                first_word = re.sub(r"[^A-Z]", "", raw_reply.split()[0] if raw_reply.split() else "")
                if first_word == "SPAM" or "SPAM" in raw_reply:
                    return "SPAM"
                if first_word == "VALID" or "VALID" in raw_reply:
                    return "VALID"
                return self._fallback_evaluate_anonymous_post(cleaned)
            except Exception as e:
                logger.warning("Tier 2 Gemini post moderation call failed, using fallback: %s", e)
                return self._fallback_evaluate_anonymous_post(cleaned)

    def _fallback_evaluate_anonymous_post(self, text: str) -> Literal["VALID", "SPAM"]:
        """Deterministic fallback for Tier 2 when Gemini API is unreachable or in offline tests."""
        ok, _ = validate_anonymous_post_tier1(text)
        if not ok:
            return "SPAM"
        lower = text.lower().strip()
        spam_patterns = [
            r"\b(?:test|testing)\s*(?:post|spam|123|1234|message)?\b$",
            r"^(?:hello|hi|hey|test|testing|spam|asdf|qwerty|lol|lmao|bruh|ok|okay)[\s\d!?.]*$",
            r"(?:asdf|qwer|zxcv|hjkl|fghj|dfgh|cvbn|jkl;)",
        ]
        for pat in spam_patterns:
            if re.search(pat, lower):
                return "SPAM"
        return "VALID"


# ---------------------------------------------------------------------------
# Multi-Tiered Anonymous Public Post Moderation (Tier 1 & Tier 2 Constants)
# ---------------------------------------------------------------------------

TIER1_REJECTION_MESSAGE: str = (
    "Your post is too short or appears to be test gibberish. Please write a meaningful message."
)

TIER2_REJECTION_MESSAGE: str = (
    "⚠️ <b>Post Not Published</b>\n\n"
    "Your post did not meet our community standards (detected as test spam or unmeaningful content). "
    "Please write a coherent campus question, thought, announcement, or message."
)

ANON_POST_MODERATION_SYSTEM_INSTRUCTION: str = (
    "Analyze the following anonymous student post. Determine if it is a coherent thought, "
    "question, lost-and-found item, club announcement, or meaningful campus message, "
    "OR if it is meaningless test spam, gibberish, or keyboard mashing. "
    "Respond with EXACTLY one word: either 'VALID' or 'SPAM'."
)

# Vowels across Latin, Armenian, and Cyrillic scripts
_LATIN_VOWELS = set("aeiouy")
_ARMENIAN_VOWELS = set("աեէըիոօ")
_CYRILLIC_VOWELS = set("аеёиоуыэюя")
_ALL_VOWELS = _LATIN_VOWELS | _ARMENIAN_VOWELS | _CYRILLIC_VOWELS

_KEYBOARD_MASH_SUBSTRINGS = (
    "asdf", "sdfg", "dfgh", "fghj", "ghjk", "hjkl",
    "qwer", "wert", "werty", "rtyu", "tyui", "yuio", "uiop",
    "zxcv", "xcvb", "cvbn", "vbnm",
    "lkjh", "kjhg", "jhgf", "hgfd", "gfds", "fdsa",
    "poiu", "oiuy", "iuyt", "uytr", "ytre", "ytrew", "rewq",
    "mnbv", "nbvc", "bvcx", "vcxz",
)


def validate_anonymous_post_tier1(raw_text: str) -> Tuple[bool, str]:
    """Tier 1 Deterministic Algorithmic Checks (Fast & Zero-Cost) for anonymous posts.

    Rules enforced:
    1. Minimum Length Enforcement: Reject any message shorter than 12 characters.
    2. Keyboard Smash / Character Analysis:
       - Vowel presence (reject strings with zero vowels or bizarre consonant-to-vowel ratios).
       - Repetitive characters, symbols, or n-gram loops.
       - Keyboard row smashing or unbroken gibberish tokens.

    Returns:
        (is_valid: bool, error_message: str)
    """
    text = re.sub(r"\s+", " ", (raw_text or "").strip())

    # 1. Minimum Length Enforcement (< 12 chars)
    if len(text) < 12:
        return False, TIER1_REJECTION_MESSAGE

    non_ws = [c for c in text if not c.isspace()]
    if len(non_ws) < 10:
        return False, TIER1_REJECTION_MESSAGE

    lower_text = text.lower()
    no_space_lower = "".join(c for c in lower_text if not c.isspace())

    # 2. Repetitive characters or symbols
    # 5+ identical consecutive characters (e.g. "hhhhh", "aaaaa", "!!!!!", "11111")
    if re.search(r"(.)\1{4,}", lower_text):
        return False, TIER1_REJECTION_MESSAGE

    # Repeating 2-5 character sequence 3+ times in a row (e.g. "asdasdasd", "jkjkjkjk", "fghfghfgh")
    if re.search(r"(.{2,5})\1{2,}", no_space_lower):
        return False, TIER1_REJECTION_MESSAGE

    # Must contain enough alphabetic characters (not just numbers/symbols/emojis)
    alpha_chars = [c for c in lower_text if c.isalpha()]
    if len(alpha_chars) < 6 or (len(alpha_chars) / len(non_ws)) < 0.40:
        return False, TIER1_REJECTION_MESSAGE

    # Minimum unique character variety
    if len(set(alpha_chars)) < 5:
        return False, TIER1_REJECTION_MESSAGE

    # Single letter dominating > 50% of all alphabetic characters (e.g. "hahahahaha", "jjjjkjjjj")
    most_common_char, most_common_cnt = Counter(alpha_chars).most_common(1)[0]
    if (most_common_cnt / len(alpha_chars)) > 0.50:
        return False, TIER1_REJECTION_MESSAGE

    # 3. Vowel presence & Consonant-to-Vowel Ratio
    vowel_count = sum(1 for c in alpha_chars if c in _ALL_VOWELS)
    if vowel_count == 0:
        return False, TIER1_REJECTION_MESSAGE

    vowel_ratio = vowel_count / len(alpha_chars)
    # Natural text across English/Armlish/Armenian/Russian has vowel ratio ~0.20 - 0.65
    if vowel_ratio < 0.15 or vowel_ratio > 0.82:
        return False, TIER1_REJECTION_MESSAGE

    # 6+ consecutive Latin consonants (e.g. "fjjdksl", "bcdfghj")
    if re.search(r"[bcdfghjklmnpqrstvwxz]{6,}", lower_text):
        return False, TIER1_REJECTION_MESSAGE

    # 5+ consecutive Latin vowels (e.g. "aeioua", "ooaaee")
    if re.search(r"[aeiouy]{5,}", lower_text):
        return False, TIER1_REJECTION_MESSAGE

    # Keyboard row mash substrings (e.g. "asdf", "qwer", "hjkl", "zxcv")
    if any(mash in lower_text for mash in _KEYBOARD_MASH_SUBSTRINGS):
        return False, TIER1_REJECTION_MESSAGE

    # Word-level analysis
    words = [w for w in re.split(r"\s+", lower_text) if w]
    # Single unbroken string without spaces (e.g. 22+ chars in 1 token)
    if len(words) < 2 and len(text) >= 18:
        return False, TIER1_REJECTION_MESSAGE

    # Check if words are just the same word repeated (e.g. "test test test", "hello hello hello")
    cleaned_words = [re.sub(r"[^a-zа-яёա-ֆ0-9]", "", w) for w in words]
    cleaned_words = [w for w in cleaned_words if w]
    if len(cleaned_words) >= 3 and len(set(cleaned_words)) == 1:
        return False, TIER1_REJECTION_MESSAGE

    # Check individual alphabetic words of length >= 5 for zero vowels (e.g. "fjjdksl hello world")
    alpha_words = re.findall(r"[a-zа-яёա-ֆ]+", lower_text)
    for w in alpha_words:
        if len(w) >= 5 and not any(c in _ALL_VOWELS for c in w):
            return False, TIER1_REJECTION_MESSAGE
        if len(w) > 24:
            return False, TIER1_REJECTION_MESSAGE

    return True, ""


gemini_moderator = GeminiModerator()
