"""Multi-Party Gemini AI Moderator for Anonymous Group Chats (100-Message Context Window)."""

import asyncio
from datetime import datetime, timezone
import html
import json
import logging
import time
from typing import Any, Dict, List, Literal, Optional, Tuple

from aiogram import Bot
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None
    types = None

from app.config import settings
from app.database.crud import (
    apply_admin_penalty,
    ban_and_kick_user_from_group,
    ban_user_by_admin,
    count_active_group_members,
    create_group_moderation_log,
    get_group_chat_by_id,
    get_group_member_record,
    get_recent_group_messages,
)
from app.database.models import GroupChatMessage
from app.services.ban_manager import add_banned_user
from app.services.group_chat_service import broadcast_group_system_message

logger = logging.getLogger(__name__)

# Anti-stampede window: ignore duplicate reports on the same group within 2 minutes (120s)
GROUP_MODERATION_COOLDOWN_SECONDS = 120.0
# Per-user report cooldown inside group chats
USER_GROUP_REPORT_COOLDOWN_SECONDS = 30.0


class GroupModerationAction(BaseModel):
    """Single participant action produced by the Multi-Party Gemini AI Moderator."""
    slot: str = Field(description="Immutable participant slot ID, e.g. U1, U2, U3")
    severity: Literal["none", "warn", "kick_group", "temp_ban", "perm_ban"]
    duration_hours: int = Field(default=0, ge=0)
    reason: str


class GroupModerationVerdict(BaseModel):
    """Structured JSON verdict returned by Gemini for a multi-party group chat evaluation."""
    summary: str
    actions: List[GroupModerationAction] = Field(default_factory=list)


GROUP_GEMINI_SYSTEM_INSTRUCTION = """You are the automated Multi-Party AI Safety and Moderation Judge for the Anonymous AUA (American University of Armenia) Chat Bot.
You are evaluating a 100-message rolling transcript of an anonymous student group chat.
Every unique participant in the transcript is assigned an immutable slot ID (U1, U2, U3, ...).

CRITICAL MULTI-PARTY ACCOUNTABILITY RULES:
1. Evaluate the ENTIRE group conversation objectively.
2. You may penalize:
   - the REPORTED user,
   - the REPORTER (for false/malicious reporting, baiting, or mutual rule violations), AND/OR
   - ANY OTHER group participant (U1..Un) who committed rule violations in the 100-message context!
3. Language Context:
   - Students speak English, Armenian (both Armenian script and Latin transliteration / Armlish), and Russian.
4. Severity levels per offending slot:
   - "none": No violation.
   - "warn": Minor rudeness, mild spam, or frivolous/spiteful false reporting.
   - "kick_group": Harassment, targeted insults, persistent spam, or unwanted behavior warranting removal and ban from this group.
   - "temp_ban": Severe harassment, hate speech, doxxing attempts, or NSFW spam warranting group removal + temporary bot restriction (set duration_hours e.g. 24 or 72).
   - "perm_ban": Extreme violations (doxxing personal phone/address/full name, blackmail, threats of violence, CSAM, illegal content) warranting permanent bot ban.

Return a JSON object conforming strictly to the GroupModerationVerdict schema:
{
  "summary": "Brief moderation summary",
  "actions": [
    {
      "slot": "U2",
      "severity": "none|warn|kick_group|temp_ban|perm_ban",
      "duration_hours": 0,
      "reason": "Clear rule violation reason"
    }
  ]
}
"""


def build_group_transcript_and_slots(
    messages: List[GroupChatMessage],
    reporter_user_id: int,
    reported_user_id: int,
    reporter_nickname: str = "Reporter",
    reported_nickname: str = "ReportedUser",
) -> Tuple[str, Dict[str, int], Dict[int, str], Dict[int, str]]:
    """Map every unique participant to an immutable slot (U1, U2, ...) and build the 100-message transcript.

    Format of each transcript line:
        [msg_id | timestamp | U2 | BlueFox]: message text or [Photo, caption: "..."]

    Returns:
        (transcript_text, slot_to_user_id, user_id_to_slot, user_id_to_nickname)
    """
    slot_to_user_id: Dict[str, int] = {}
    user_id_to_slot: Dict[int, str] = {}
    user_id_to_nickname: Dict[int, str] = {}

    def _assign_slot(uid: int, nick: Optional[str] = None) -> str:
        if uid not in user_id_to_slot:
            slot_id = f"U{len(user_id_to_slot) + 1}"
            user_id_to_slot[uid] = slot_id
            slot_to_user_id[slot_id] = uid
        if nick and uid not in user_id_to_nickname:
            user_id_to_nickname[uid] = nick
        return user_id_to_slot[uid]

    # Always assign deterministic slots for reporter and reported user first, then all speakers in chronological order
    _assign_slot(reporter_user_id, reporter_nickname)
    _assign_slot(reported_user_id, reported_nickname)

    lines: List[str] = []
    for msg in messages[-100:]:
        slot = _assign_slot(msg.sender_user_id, msg.sender_nickname)
        if msg.sender_nickname:
            user_id_to_nickname[msg.sender_user_id] = msg.sender_nickname
        ts = msg.created_at
        if ts:
            ts_utc = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
            ts_str = ts_utc.strftime("%Y-%m-%d %H:%M:%S UTC")
        else:
            ts_str = "UTC"
        content = (msg.text_content or f"[{msg.msg_type}]").replace("\n", " ").strip()
        lines.append(f"[{msg.id} | {ts_str} | {slot} | {msg.sender_nickname}]: {content}")

    transcript_text = "\n".join(lines) if lines else "[No messages in group window]"
    return transcript_text, slot_to_user_id, user_id_to_slot, user_id_to_nickname


class GroupAIModerator:
    """Multi-Party Gemini AI Moderator with per-group anti-stampede lock and 2-minute window cooldown."""

    def __init__(self, api_key: Optional[str] = None) -> None:
        self.api_key: Optional[str] = api_key or settings.GEMINI_API_KEY
        self._client: Any = None
        self._group_locks: Dict[int, asyncio.Lock] = {}
        self._group_last_moderated_at: Dict[int, float] = {}
        self._user_last_report_at: Dict[int, float] = {}

    def _get_group_lock(self, group_id: int) -> asyncio.Lock:
        if group_id not in self._group_locks:
            self._group_locks[group_id] = asyncio.Lock()
        return self._group_locks[group_id]

    def _get_client(self) -> Any:
        if self._client is None:
            key = self.api_key or settings.GEMINI_API_KEY
            if not key or genai is None:
                return None
            self._client = genai.Client(api_key=key)
        return self._client

    def check_anti_stampede_and_cooldown(
        self,
        group_id: int,
        reporter_user_id: int,
    ) -> Tuple[bool, str]:
        """Check per-group lock, 2-minute anti-stampede window, and per-user report cooldown."""
        lock = self._get_group_lock(group_id)
        if lock.locked():
            return (
                False,
                "⏳ <b>Moderation Review Already in Progress</b>\n\n"
                "An AI moderation review is currently running for this group chat. Please wait for it to finish.",
            )

        now = time.monotonic()
        last_group_mod = self._group_last_moderated_at.get(group_id, 0.0)
        if last_group_mod > 0.0 and (now - last_group_mod) < GROUP_MODERATION_COOLDOWN_SECONDS:
            rem = int(GROUP_MODERATION_COOLDOWN_SECONDS - (now - last_group_mod)) + 1
            return (
                False,
                "🛡️ <b>Moderation Review Recently Completed</b>\n\n"
                f"This group chat's recent message window was just evaluated by the AI Moderator. "
                f"Duplicate reports are paused for {rem}s to prevent double penalties.",
            )

        last_user_rep = self._user_last_report_at.get(reporter_user_id, 0.0)
        if last_user_rep > 0.0 and (now - last_user_rep) < USER_GROUP_REPORT_COOLDOWN_SECONDS:
            rem = int(USER_GROUP_REPORT_COOLDOWN_SECONDS - (now - last_user_rep)) + 1
            return (
                False,
                f"⏳ Please wait {rem}s before submitting another report.",
            )

        return True, ""

    async def evaluate_group_transcript(
        self,
        transcript_text: str,
        reporter_slot: str,
        reported_slot: str,
        reason_text: str,
        messages: List[GroupChatMessage],
        user_id_to_slot: Dict[int, str],
    ) -> GroupModerationVerdict:
        """Call Gemini (with thinking_budget=0) to judge all participants in the 100-message window."""
        client = self._get_client()
        if client is None or types is None:
            return self._fallback_evaluate_group(
                reporter_slot=reporter_slot,
                reported_slot=reported_slot,
                reason_text=reason_text,
                messages=messages,
                user_id_to_slot=user_id_to_slot,
            )

        prompt = (
            f"Report filed by slot {reporter_slot} against slot {reported_slot}.\n"
            f"Reporter's stated reason: {reason_text}\n\n"
            "Evaluate the following 100-message chronological group chat transcript. "
            "You may assign actions to the reported slot, the reporter slot (if malicious/false report or mutual violation), "
            "and/or any other participant slot (U1..Un) that violated rules:\n\n"
            f"{transcript_text}"
        )

        try:
            config_kwargs: Dict[str, Any] = {
                "response_mime_type": "application/json",
                "response_schema": GroupModerationVerdict,
                "temperature": 0.1,
                "system_instruction": GROUP_GEMINI_SYSTEM_INSTRUCTION,
            }
            if hasattr(types, "ThinkingConfig"):
                config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)

            config = types.GenerateContentConfig(**config_kwargs)
            response = await client.aio.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=config,
            )
            if hasattr(response, "parsed") and response.parsed is not None:
                return response.parsed
            return GroupModerationVerdict.model_validate_json(response.text)
        except Exception as e:
            logger.exception("Group Gemini AI evaluation failed, falling back to heuristic: %s", e)
            return self._fallback_evaluate_group(
                reporter_slot=reporter_slot,
                reported_slot=reported_slot,
                reason_text=reason_text,
                messages=messages,
                user_id_to_slot=user_id_to_slot,
            )

    def _fallback_evaluate_group(
        self,
        reporter_slot: str,
        reported_slot: str,
        reason_text: str,
        messages: List[GroupChatMessage],
        user_id_to_slot: Dict[int, str],
    ) -> GroupModerationVerdict:
        """Deterministic multi-party fallback when Gemini API key is unavailable or during offline tests."""
        actions: List[GroupModerationAction] = []
        severe_ban_keywords = ("csam", "kill you", "bomb", "terrorist", "extort", "real name is", "phone number is")
        kick_keywords = ("bitch", "whore", "idiot", "slut", "nude", "fuck you", "harass", "hate speech")
        warn_keywords = ("spam", "stupid", "shut up", "annoying")

        # Inspect every speaker in the 100-message window
        texts_by_slot: Dict[str, List[str]] = {}
        for m in messages[-100:]:
            slot = user_id_to_slot.get(m.sender_user_id)
            if not slot:
                continue
            texts_by_slot.setdefault(slot, []).append((m.text_content or "").lower())

        for slot, slot_texts in texts_by_slot.items():
            joined = " ".join(slot_texts)
            if any(kw in joined for kw in severe_ban_keywords):
                actions.append(
                    GroupModerationAction(
                        slot=slot,
                        severity="perm_ban",
                        duration_hours=0,
                        reason="Severe safety/doxxing/illegal violation detected in group transcript.",
                    )
                )
            elif any(kw in joined for kw in kick_keywords):
                actions.append(
                    GroupModerationAction(
                        slot=slot,
                        severity="kick_group",
                        duration_hours=0,
                        reason="Abusive language or harassment detected in group transcript.",
                    )
                )
            elif any(kw in joined for kw in warn_keywords):
                actions.append(
                    GroupModerationAction(
                        slot=slot,
                        severity="warn",
                        duration_hours=0,
                        reason="Minor rudeness or spam detected in group transcript.",
                    )
                )

        return GroupModerationVerdict(
            summary="Multi-party AI moderation review completed over the group chat transcript.",
            actions=actions,
        )

    async def run_group_moderation(
        self,
        bot: Bot,
        session: AsyncSession,
        group_id: int,
        reporter_user_id: int,
        reported_user_id: int,
        reason_text: str,
    ) -> Tuple[bool, str, Optional[GroupModerationVerdict]]:
        """Execute full multi-party AI moderation pipeline with anti-stampede lock and penalty application."""
        allowed, cooldown_msg = self.check_anti_stampede_and_cooldown(group_id, reporter_user_id)
        if not allowed:
            return False, cooldown_msg, None

        lock = self._get_group_lock(group_id)
        if lock.locked():
            return (
                False,
                "⏳ <b>Moderation Review Already in Progress</b>\n\n"
                "An AI moderation review is currently running for this group chat.",
                None,
            )

        async with lock:
            # Double-check timestamp inside the lock
            now = time.monotonic()
            last_group_mod = self._group_last_moderated_at.get(group_id, 0.0)
            if last_group_mod > 0.0 and (now - last_group_mod) < GROUP_MODERATION_COOLDOWN_SECONDS:
                return (
                    False,
                    "🛡️ <b>Moderation Review Recently Completed</b>\n\n"
                    "This group chat's recent message window was just evaluated by the AI Moderator.",
                    None,
                )

            self._user_last_report_at[reporter_user_id] = now

            group = await get_group_chat_by_id(session, group_id)
            if not group:
                return False, "⚠️ Group chat not found.", None

            reporter_rec = await get_group_member_record(session, group_id, reporter_user_id)
            reported_rec = await get_group_member_record(session, group_id, reported_user_id)
            reporter_nick = reporter_rec.nickname if reporter_rec else "Reporter"
            reported_nick = reported_rec.nickname if reported_rec else "ReportedUser"

            messages = await get_recent_group_messages(session, group_id, limit=100)
            (
                transcript_text,
                slot_to_user_id,
                user_id_to_slot,
                user_id_to_nickname,
            ) = build_group_transcript_and_slots(
                messages=messages,
                reporter_user_id=reporter_user_id,
                reported_user_id=reported_user_id,
                reporter_nickname=reporter_nick,
                reported_nickname=reported_nick,
            )

            reporter_slot = user_id_to_slot[reporter_user_id]
            reported_slot = user_id_to_slot[reported_user_id]

            verdict = await self.evaluate_group_transcript(
                transcript_text=transcript_text,
                reporter_slot=reporter_slot,
                reported_slot=reported_slot,
                reason_text=reason_text,
                messages=messages,
                user_id_to_slot=user_id_to_slot,
            )

            # Record completion timestamp for the 2-minute anti-stampede window
            self._group_last_moderated_at[group_id] = time.monotonic()

            # Execute penalties for each slot in verdict.actions
            applied_actions_log: List[Dict[str, Any]] = []
            for act in verdict.actions:
                target_uid = slot_to_user_id.get(act.slot)
                if target_uid is None or act.severity == "none":
                    continue

                member_rec = await get_group_member_record(session, group_id, target_uid)
                target_nick = (
                    member_rec.nickname
                    if member_rec
                    else user_id_to_nickname.get(target_uid, act.slot)
                )
                safe_nick = html.escape(target_nick)
                safe_reason = html.escape(act.reason or "Community guideline violation")

                applied_actions_log.append(
                    {
                        "slot": act.slot,
                        "user_id": target_uid,
                        "nickname": target_nick,
                        "severity": act.severity,
                        "duration_hours": act.duration_hours,
                        "reason": act.reason,
                    }
                )

                if act.severity == "warn":
                    await apply_admin_penalty(session, user_id=target_uid, penalty=3)
                    try:
                        await bot.send_message(
                            chat_id=target_uid,
                            text=(
                                "⚠️ <b>Official AI Moderator Warning</b> 🛡️\n\n"
                                f"During a moderation review of group chat <b>{html.escape(group.name)}</b>, "
                                f"your messages under nickname <b>{safe_nick}</b> received an official warning:\n"
                                f"• <b>Reason:</b> <i>{safe_reason}</i>\n"
                                "• <b>Penalty:</b> -3 priority points.\n\n"
                                "Continued violations will result in removal or a bot ban."
                            ),
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass

                elif act.severity == "kick_group":
                    await ban_and_kick_user_from_group(
                        session=session,
                        group_id=group_id,
                        user_id=target_uid,
                        reason=act.reason,
                    )
                    await apply_admin_penalty(session, user_id=target_uid, penalty=5)
                    try:
                        await bot.send_message(
                            chat_id=target_uid,
                            text=(
                                "🛡️ <b>Removed from Group Chat by AI Moderator</b>\n\n"
                                f"You have been removed and banned from rejoining <b>{html.escape(group.name)}</b>.\n"
                                f"• <b>Reason:</b> <i>{safe_reason}</i>"
                            ),
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass
                    await broadcast_group_system_message(
                        bot=bot,
                        session=session,
                        group_id=group_id,
                        html_text=f"🛡️ <b>{safe_nick}</b> was removed by AI Moderator.",
                        exclude_user_id=target_uid,
                    )

                elif act.severity in ("temp_ban", "perm_ban"):
                    await ban_and_kick_user_from_group(
                        session=session,
                        group_id=group_id,
                        user_id=target_uid,
                        reason=act.reason,
                    )
                    await apply_admin_penalty(session, user_id=target_uid, penalty=10)
                    await ban_user_by_admin(session, user_id=target_uid)
                    add_banned_user(target_uid)

                    ban_label = (
                        f"Temporarily Banned ({act.duration_hours or 24}h)"
                        if act.severity == "temp_ban"
                        else "Permanently Banned"
                    )
                    try:
                        await bot.send_message(
                            chat_id=target_uid,
                            text=(
                                f"🚫 <b>Account {ban_label} by AI Moderator</b> 🛡️\n\n"
                                f"Due to severe violations in group chat <b>{html.escape(group.name)}</b>, "
                                f"you have been removed from the group and banned from Anonymous AUA.\n"
                                f"• <b>Reason:</b> <i>{safe_reason}</i>"
                            ),
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass
                    await broadcast_group_system_message(
                        bot=bot,
                        session=session,
                        group_id=group_id,
                        html_text=f"🛡️ <b>{safe_nick}</b> was removed by AI Moderator.",
                        exclude_user_id=target_uid,
                    )

            # Persist judgment in GroupModerationLog
            await create_group_moderation_log(
                session=session,
                group_id=group_id,
                reporter_user_id=reporter_user_id,
                reported_user_id=reported_user_id,
                ai_summary=verdict.summary,
                actions_json=json.dumps(applied_actions_log),
            )

            safe_summary = html.escape(verdict.summary or "Moderation review completed.")
            reporter_reply = (
                "✅ <b>AI Safety Moderation Review Complete</b> 🛡️\n\n"
                f"📝 <b>Summary:</b> <i>{safe_summary}</i>\n\n"
                "Appropriate actions have been enforced in accordance with AUA Community & Privacy Rules."
            )
            return True, reporter_reply, verdict

    def clear_for_testing(self) -> None:
        """Reset all locks and cooldown timers for test runs."""
        self._group_locks.clear()
        self._group_last_moderated_at.clear()
        self._user_last_report_at.clear()


group_ai_moderator = GroupAIModerator()
