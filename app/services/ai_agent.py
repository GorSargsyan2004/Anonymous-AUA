"""Multi-Source RAG AI Assistant for AUA Hub (`app/services/ai_agent.py`).

Provides a Gemini-powered (`gemini-2.5-flash`) retrieval-augmented generation (RAG)
orchestrator with 6 specialized campus tools:
1. Course & Professor Feedbacks Database (`jenzareviews` local mirror + `AcademicFeedback`)
2. Global Campus Chat History (`SpamChatMessage`)
3. Anonymous Campus Posts & Q&A Context (`AnonymousPublicPost` & anonymized `InboxMessage`)
4. AUA Newsroom Events Parser (`https://newsroom.aua.am/events/`)
5. The Highlander Student Magazine Parser (`https://thehighlander.aua.am/?s=...`)
6. AUA Directory & Faculty/Staff Profiles (`directory.aua.am`, `people.aua.am`, + Web Search Fallback)

Enforces a strict maximum of 5 prompts per chat session and strips all personal user
identifiers from internal community data before synthesis.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import html
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus, urljoin
import uuid

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None
    types = None

from app.config import settings
from app.database.models import (
    AcademicFeedback,
    AnonymousPublicPost,
    Course,
    HaveNeedPost,
    InboxMessage,
    JenzaProfessor,
    JenzaReview,
    SpamChatMessage,
    TelegramGroup,
    WebUserAIChatSession,
)

logger = logging.getLogger(__name__)

MAX_PROMPTS_PER_SESSION: int = 5
SESSION_TTL_SECONDS: int = 3600 * 6  # 6 hours

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

STOPWORDS = {
    "what", "when", "where", "which", "who", "whom", "whose", "why", "how",
    "are", "is", "was", "were", "be", "been", "being", "have", "has", "had",
    "do", "does", "did", "will", "would", "shall", "should", "can", "could",
    "may", "might", "must", "the", "a", "an", "and", "or", "but", "if", "then",
    "about", "from", "into", "during", "including", "until", "against", "among",
    "throughout", "despite", "towards", "upon", "concerning", "for", "with",
    "without", "at", "by", "to", "in", "on", "of", "off", "over", "under",
    "tell", "me", "please", "find", "search", "show", "give", "any", "there",
    "aua", "american", "university", "armenia", "professor", "prof", "dr",
    "instructor", "teacher", "course", "courses", "class", "classes",
    "events", "event", "upcoming", "week", "today", "month", "article",
    "articles", "highlander", "newsroom", "directory", "chat", "discussions",
    "feedback", "feedbacks", "review", "reviews", "grading", "policy",
}


# ---------------------------------------------------------------------------
# Helpers: HTML Cleaning & Cloudflare Email De-obfuscation
# ---------------------------------------------------------------------------

def decode_cfemail_hex(hex_str: str) -> str:
    """Decode Cloudflare email protection hex string (`data-cfemail`)."""
    try:
        key = int(hex_str[:2], 16)
        return "".join(
            chr(int(hex_str[i : i + 2], 16) ^ key)
            for i in range(2, len(hex_str), 2)
        )
    except Exception:
        return ""


def decode_cfemails_in_html(raw_html: str) -> str:
    """Replace all Cloudflare `<a/span data-cfemail="...">` tags with decoded emails."""
    if not raw_html or "data-cfemail" not in raw_html:
        return raw_html

    def _repl(match: re.Match) -> str:
        hx = match.group(1) or match.group(2) or ""
        decoded = decode_cfemail_hex(hx)
        return decoded if decoded else match.group(0)

    pattern = (
        r'<a[^>]*data-cfemail=["\']([0-9a-fA-F]+)["\'][^>]*>.*?</a>|'
        r'<span[^>]*data-cfemail=["\']([0-9a-fA-F]+)["\'][^>]*>.*?</span>'
    )
    return re.sub(pattern, _repl, raw_html, flags=re.IGNORECASE | re.DOTALL)


def strip_html_tags(raw_html: str) -> str:
    """Decode Cloudflare emails, strip HTML tags, unescape entities, and normalize spaces."""
    if not raw_html:
        return ""
    text = decode_cfemails_in_html(raw_html)
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return " ".join(text.split())


def extract_keywords(query: str, min_len: int = 2) -> List[str]:
    """Extract meaningful search keywords from a natural-language user prompt."""
    cleaned = re.sub(r"[^\w\s+-]", " ", (query or "").lower())
    tokens = [t.strip() for t in cleaned.split() if len(t.strip()) >= min_len]
    keywords = [t for t in tokens if t not in STOPWORDS]
    if not keywords and tokens:
        keywords = tokens[:4]
    # Preserve order while deduplicating
    return list(dict.fromkeys(keywords))[:8]


def extract_person_name_candidate(query: str) -> str:
    """Extract a likely person/professor name from a prompt if present."""
    q = (query or "").strip()
    patterns = [
        r"(?:[Pp]rofessor|[Pp]rof\.?|[Dd]octor|[Dd]r\.?|[Ii]nstructor|[Dd]ean)\s+([A-Z][a-zA-Z\-]+(?:\s+[A-Z][a-zA-Z\-]+){0,2})",
        r"(?:[Ww]ho\s+is|[Aa]bout|[Cc]ontact|[Ee]mail\s+for|[Oo]ffice\s+of|[Ff]ind)\s+([A-Z][a-zA-Z\-]+(?:\s+[A-Z][a-zA-Z\-]+){1,2})",
        r"([A-Z][a-zA-Z\-]+(?:\s+[A-Z][a-zA-Z\-]+){1,2})'s\b",
    ]
    for pat in patterns:
        m = re.search(pat, q)
        if m:
            candidate = m.group(1).strip()
            candidate = re.sub(r"'s$", "", candidate, flags=re.IGNORECASE)
            candidate = re.sub(
                r"^(?:professor|prof\.?|doctor|dr\.?|instructor|dean)\s+",
                "",
                candidate,
                flags=re.IGNORECASE,
            ).strip()
            if candidate and candidate.lower() not in STOPWORDS:
                return candidate

    # Fallback: capitalized words not at the very start of question words
    words = re.findall(r"\b[A-Za-z][a-zA-Z\-]+\b", q)
    cap_words = [
        w for w in words
        if w[0].isupper() and w.lower() not in STOPWORDS and w.upper() not in ("CS", "AUA", "PAB", "MB", "AI", "GPA")
    ]
    if cap_words:
        return " ".join(cap_words[:3])

    kws = extract_keywords(q)
    return " ".join(kws[:3]) if kws else q[:60]


# ---------------------------------------------------------------------------
# Tool 1: Course & Professor Feedbacks Database (Local Mirror + AcademicFeedback)
# ---------------------------------------------------------------------------

async def search_course_and_professor_feedbacks(
    session: AsyncSession,
    query: str,
    limit: int = 8,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    """Search local jenzareviews mirror (`JenzaProfessor`, `JenzaReview`) and `AcademicFeedback`."""
    results: List[Dict[str, Any]] = []
    sources: List[Dict[str, str]] = []

    keywords = extract_keywords(query)
    name_cand = extract_person_name_candidate(query)
    search_terms = list(dict.fromkeys(([name_cand] if name_cand else []) + keywords))
    if not search_terms:
        search_terms = [query.strip()[:50]]

    # 1. Search JenzaProfessor & JenzaReview
    prof_conds = []
    for term in search_terms[:5]:
        if len(term) >= 2:
            prof_conds.append(JenzaProfessor.name.ilike(f"%{term}%"))
            prof_conds.append(JenzaProfessor.department.ilike(f"%{term}%"))

    matched_profs: List[JenzaProfessor] = []
    if prof_conds:
        stmt_prof = select(JenzaProfessor).where(or_(*prof_conds)).limit(5)
        matched_profs = list((await session.execute(stmt_prof)).scalars().all())

    for prof in matched_profs:
        stmt_rev = (
            select(JenzaReview)
            .where(JenzaReview.professor_id == prof.id)
            .order_by(desc(JenzaReview.created_at))
            .limit(6)
        )
        revs = list((await session.execute(stmt_rev)).scalars().all())
        rated_revs = [r for r in revs if r.rating is not None]
        avg_overall = (
            round(sum(float(r.rating) for r in rated_revs) / len(rated_revs), 2)
            if rated_revs
            else None
        )
        results.append(
            {
                "type": "professor_feedback_summary",
                "professor_name": prof.name,
                "department": prof.department or "AUA Faculty",
                "review_count_sampled": len(revs),
                "avg_overall_rating": avg_overall,
                "reviews": [
                    {
                        "course_code": r.course or "General",
                        "overall_rating": float(r.rating) if r.rating is not None else None,
                        "would_take_again": r.would_take_again,
                        "upvotes": r.upvotes,
                        "downvotes": r.downvotes,
                        "comment": (r.comment or "")[:450],
                    }
                    for r in revs
                ],
            }
        )
        sources.append(
            {
                "title": f"Feedbacks: {prof.name}",
                "url": "#feedbacks",
                "source_type": "AUA Feedbacks (JenzaReviews)",
            }
        )

    # 2. Search JenzaReview directly by course or comment keywords
    rev_conds = []
    for term in search_terms[:5]:
        if len(term) >= 2:
            rev_conds.append(JenzaReview.course.ilike(f"%{term}%"))
            rev_conds.append(JenzaReview.comment.ilike(f"%{term}%"))

    if rev_conds and len(results) < limit:
        stmt_direct_rev = (
            select(JenzaReview, JenzaProfessor.name)
            .join(JenzaProfessor, JenzaReview.professor_id == JenzaProfessor.id)
            .where(or_(*rev_conds))
            .order_by(desc(JenzaReview.created_at))
            .limit(6)
        )
        rows = (await session.execute(stmt_direct_rev)).all()
        for rev, prof_name in rows:
            results.append(
                {
                    "type": "course_or_professor_review",
                    "professor_name": prof_name,
                    "course_code": rev.course or "General",
                    "overall_rating": float(rev.rating) if rev.rating is not None else None,
                    "would_take_again": rev.would_take_again,
                    "comment": (rev.comment or "")[:400],
                }
            )

    # 3. Search local AcademicFeedback table
    fb_conds = []
    for term in search_terms[:5]:
        if len(term) >= 2:
            fb_conds.append(AcademicFeedback.target_title.ilike(f"%{term}%"))
            fb_conds.append(AcademicFeedback.target_key.ilike(f"%{term}%"))
            fb_conds.append(AcademicFeedback.comment.ilike(f"%{term}%"))

    if fb_conds:
        stmt_fb = (
            select(AcademicFeedback)
            .where(or_(*fb_conds))
            .order_by(desc(AcademicFeedback.created_at))
            .limit(6)
        )
        fbs = list((await session.execute(stmt_fb)).scalars().all())
        for fb in fbs:
            results.append(
                {
                    "type": f"local_{fb.target_type}_feedback",
                    "target": fb.target_title,
                    "subtitle": fb.target_subtitle or "",
                    "rating": fb.rating,
                    "comment": (fb.comment or "")[:400],
                    "created_at": fb.created_at.strftime("%Y-%m-%d") if fb.created_at else "",
                }
            )
            if not any(s["title"] == f"Feedbacks: {fb.target_title}" for s in sources):
                sources.append(
                    {
                        "title": f"Feedbacks: {fb.target_title}",
                        "url": "#feedbacks",
                        "source_type": "AUA Hub Feedbacks",
                    }
                )

    # 4. Also check official Sonis Course schedule if course/instructor matches
    course_conds = []
    for term in search_terms[:4]:
        if len(term) >= 2:
            course_conds.append(Course.course_code.ilike(f"%{term}%"))
            course_conds.append(Course.course_title.ilike(f"%{term}%"))
            course_conds.append(Course.instructor.ilike(f"%{term}%"))

    if course_conds:
        stmt_c = select(Course).where(or_(*course_conds)).limit(8)
        courses = list((await session.execute(stmt_c)).scalars().all())
        for c in courses:
            results.append(
                {
                    "type": "sonis_course_section",
                    "semester": c.semester_label,
                    "course_code": c.course_code,
                    "course_title": c.course_title,
                    "section": c.section,
                    "instructor": c.instructor,
                    "schedule": c.schedule_times,
                    "location": c.location,
                }
            )

    return results[:14], sources[:4]


# ---------------------------------------------------------------------------
# Tool 2: Global Campus Chat History (`SpamChatMessage`)
# ---------------------------------------------------------------------------

async def search_global_chat_history(
    session: AsyncSession,
    query: str,
    limit: int = 10,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    """Search global campus chat messages (`SpamChatMessage`) with anonymized author labels."""
    keywords = extract_keywords(query)
    conds = [
        SpamChatMessage.content_html.ilike(f"%{kw}%")
        for kw in keywords[:6]
        if len(kw) >= 2
    ]

    if conds:
        stmt = (
            select(SpamChatMessage)
            .where(or_(*conds))
            .order_by(desc(SpamChatMessage.created_at))
            .limit(limit)
        )
        msgs = list((await session.execute(stmt)).scalars().all())
    else:
        stmt = (
            select(SpamChatMessage)
            .order_by(desc(SpamChatMessage.created_at))
            .limit(min(limit, 6))
        )
        msgs = list((await session.execute(stmt)).scalars().all())

    # If keyword search returned nothing, fall back to most recent chat discussions
    if not msgs:
        stmt_recent = (
            select(SpamChatMessage)
            .order_by(desc(SpamChatMessage.created_at))
            .limit(6)
        )
        msgs = list((await session.execute(stmt_recent)).scalars().all())

    results: List[Dict[str, Any]] = []
    for m in msgs:
        clean_text = strip_html_tags(m.content_html or "")
        if not clean_text:
            continue
        results.append(
            {
                "message_id": m.id,
                "author": "AUA Student (Anonymized)",
                "text": clean_text[:350],
                "timestamp": m.created_at.strftime("%Y-%m-%d %H:%M") if m.created_at else "",
            }
        )

    sources = (
        [{"title": "AUA Hub Campus Chat", "url": "#spam-chat", "source_type": "Campus Chat"}]
        if results
        else []
    )
    return results, sources


# ---------------------------------------------------------------------------
# Tool 3: Anonymous Posts, Inbox Q&A, & Community Listings
# ---------------------------------------------------------------------------

async def search_anonymous_posts_and_inbox(
    session: AsyncSession,
    query: str,
    limit: int = 8,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    """Search public anonymous posts, anonymized Q&A notes, and Telegram groups."""
    keywords = extract_keywords(query)
    results: List[Dict[str, Any]] = []
    sources: List[Dict[str, str]] = []

    # 1. Public Anonymous Posts (`AnonymousPublicPost`)
    post_conds = [
        AnonymousPublicPost.content_text.ilike(f"%{kw}%")
        for kw in keywords[:6]
        if len(kw) >= 2
    ]
    if post_conds:
        stmt_posts = (
            select(AnonymousPublicPost)
            .where(AnonymousPublicPost.is_hidden == False, or_(*post_conds))
            .order_by(desc(AnonymousPublicPost.created_at))
            .limit(limit)
        )
    else:
        stmt_posts = (
            select(AnonymousPublicPost)
            .where(AnonymousPublicPost.is_hidden == False)
            .order_by(desc(AnonymousPublicPost.created_at))
            .limit(5)
        )
    anon_posts = list((await session.execute(stmt_posts)).scalars().all())
    for p in anon_posts:
        results.append(
            {
                "type": "anonymous_campus_post",
                "post_number": f"#{p.id}",
                "content": (p.content_text or "")[:400],
                "date": p.created_at.strftime("%Y-%m-%d") if p.created_at else "",
            }
        )
    if anon_posts:
        sources.append(
            {
                "title": "Anonymous Campus Posts",
                "url": "#anonymous",
                "source_type": "Anonymous Feed",
            }
        )

    # 2. Anonymized Q&A notes from `InboxMessage` (strictly stripping owner/sender IDs and tokens)
    if keywords:
        inbox_conds = []
        for kw in keywords[:5]:
            if len(kw) >= 3:
                inbox_conds.append(InboxMessage.content_text.ilike(f"%{kw}%"))
                inbox_conds.append(InboxMessage.reply_text.ilike(f"%{kw}%"))
        if inbox_conds:
            stmt_inb = (
                select(InboxMessage)
                .where(or_(*inbox_conds))
                .order_by(desc(InboxMessage.created_at))
                .limit(4)
            )
            inb_rows = list((await session.execute(stmt_inb)).scalars().all())
            for item in inb_rows:
                results.append(
                    {
                        "type": "anonymized_student_qa",
                        "question_snippet": (item.content_text or "")[:280],
                        "reply_snippet": (item.reply_text or "")[:280] if item.reply_text else None,
                    }
                )

    # 3. Also check shared Telegram Groups if relevant
    grp_conds = [
        or_(
            TelegramGroup.name.ilike(f"%{kw}%"),
            TelegramGroup.description.ilike(f"%{kw}%"),
        )
        for kw in keywords[:5]
        if len(kw) >= 2
    ]
    if grp_conds:
        stmt_grp = select(TelegramGroup).where(or_(*grp_conds)).limit(4)
    else:
        stmt_grp = select(TelegramGroup).order_by(desc(TelegramGroup.created_at)).limit(4)
    grps = list((await session.execute(stmt_grp)).scalars().all())
    for g in grps:
        results.append(
            {
                "type": "student_telegram_group",
                "name": g.name,
                "description": g.description or "",
                "invite_link": g.invite_link,
            }
        )

    return results[:12], sources


# ---------------------------------------------------------------------------
# Tool 4: AUA Newsroom Events Parser (`https://newsroom.aua.am/events/`)
# ---------------------------------------------------------------------------

async def fetch_aua_newsroom_events(
    query: str = "",
    limit: int = 8,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    """Fetch and parse upcoming/past campus events from `https://newsroom.aua.am/events/`."""
    events: List[Dict[str, Any]] = []
    sources: List[Dict[str, str]] = []
    keywords = extract_keywords(query)

    try:
        async with httpx.AsyncClient(
            timeout=10.0, follow_redirects=True, headers=HTTP_HEADERS
        ) as client:
            resp = await client.get("https://newsroom.aua.am/events/")
            if resp.status_code == 200 and resp.text:
                for m in re.finditer(
                    r"<article[^>]*>(.*?)</article>",
                    resp.text,
                    flags=re.IGNORECASE | re.DOTALL,
                ):
                    block = m.group(1)
                    link_m = re.search(
                        r'href=["\'](https://newsroom\.aua\.am/event/[^"\']+)["\']',
                        block,
                        flags=re.IGNORECASE,
                    )
                    event_url = link_m.group(1).strip() if link_m else "https://newsroom.aua.am/events/"

                    title_m = re.search(
                        r"<h[2-4][^>]*>\s*(?:<a[^>]*>)?(.*?)(?:</a>)?\s*</h[2-4]>",
                        block,
                        flags=re.IGNORECASE | re.DOTALL,
                    )
                    title = strip_html_tags(title_m.group(1)) if title_m else ""
                    full_clean = strip_html_tags(block)
                    if not title and full_clean:
                        title = full_clean[:90]

                    # Extract date/time & venue details from full_clean
                    details = full_clean
                    if title and details.startswith(title):
                        details = details[len(title) :].strip(" .:-")

                    events.append(
                        {
                            "title": title,
                            "details": details[:420],
                            "url": event_url,
                        }
                    )

            # If user asked for a specific keyword not in the default calendar view, also query Newsroom search
            if keywords:
                q_str = " ".join(keywords[:4])
                matched = [
                    ev for ev in events
                    if any(kw in f"{ev['title']} {ev['details']}".lower() for kw in keywords)
                ]
                if len(matched) < 2 and q_str:
                    search_url = f"https://newsroom.aua.am/?s={quote_plus(q_str)}"
                    s_resp = await client.get(search_url)
                    if s_resp.status_code == 200 and s_resp.text:
                        for m in re.finditer(
                            r"<article[^>]*>(.*?)</article>",
                            s_resp.text,
                            flags=re.IGNORECASE | re.DOTALL,
                        ):
                            block = m.group(1)
                            link_m = re.search(
                                r'href=["\'](https://newsroom\.aua\.am/[^"\']+)["\']',
                                block,
                                flags=re.IGNORECASE,
                            )
                            url = link_m.group(1).strip() if link_m else search_url
                            title_m = re.search(
                                r"<h[1-4][^>]*>(.*?)</h[1-4]>",
                                block,
                                flags=re.IGNORECASE | re.DOTALL,
                            )
                            title = strip_html_tags(title_m.group(1)) if title_m else "AUA Newsroom Item"
                            snippet = strip_html_tags(block)[:380]
                            if not any(e["url"] == url for e in events):
                                events.append(
                                    {
                                        "title": title,
                                        "details": snippet,
                                        "url": url,
                                    }
                                )
    except Exception as exc:
        logger.warning("AUA Newsroom events fetch warning: %s", exc)

    # Rank events matching keywords first, while keeping upcoming events
    if keywords and events:
        events.sort(
            key=lambda e: sum(
                1 for kw in keywords if kw in f"{e['title']} {e['details']}".lower()
            ),
            reverse=True,
        )

    selected = events[:limit]
    for ev in selected[:3]:
        sources.append(
            {
                "title": ev["title"][:65] or "AUA Newsroom Events",
                "url": ev["url"],
                "source_type": "AUA Newsroom",
            }
        )
    if not sources and selected:
        sources.append(
            {
                "title": "AUA Newsroom Events Calendar",
                "url": "https://newsroom.aua.am/events/",
                "source_type": "AUA Newsroom",
            }
        )
    return selected, sources


# ---------------------------------------------------------------------------
# Tool 5: The Highlander Student Magazine Parser (`https://thehighlander.aua.am/?s=...`)
# ---------------------------------------------------------------------------

async def search_highlander_magazine(
    query: str,
    limit: int = 5,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    """Query `https://thehighlander.aua.am/?s=...` and extract student articles."""
    articles: List[Dict[str, Any]] = []
    sources: List[Dict[str, str]] = []

    keywords = extract_keywords(query)
    search_q = " ".join(keywords[:4]) if keywords else query.strip()[:50]
    if not search_q:
        search_q = "AUA student"

    search_url = f"https://thehighlander.aua.am/?s={quote_plus(search_q)}"
    try:
        async with httpx.AsyncClient(
            timeout=10.0, follow_redirects=True, headers=HTTP_HEADERS
        ) as client:
            resp = await client.get(search_url)
            if resp.status_code == 200 and resp.text:
                # Parse <h2 ...> article blocks on The Highlander
                for m in re.finditer(
                    r"<h2[^>]*>(.*?)</h2>",
                    resp.text,
                    flags=re.IGNORECASE | re.DOTALL,
                ):
                    h2_inner = m.group(1)
                    h2_text = strip_html_tags(h2_inner)
                    if not h2_text or "search results for" in h2_text.lower() or "pagination" in h2_text.lower():
                        continue

                    start_pos = m.start()
                    window = resp.text[start_pos : start_pos + 1600]
                    link_m = re.search(
                        r'href=["\'](https://thehighlander\.aua\.am/(?!category/|tag/|author/|page/)[^"\']+)["\']',
                        window,
                        flags=re.IGNORECASE,
                    )
                    article_url = link_m.group(1).strip() if link_m else search_url
                    snippet = strip_html_tags(window)
                    if snippet.startswith(h2_text):
                        snippet = snippet[len(h2_text) :].strip(" •-:")

                    if not any(a["title"] == h2_text for a in articles):
                        articles.append(
                            {
                                "title": h2_text[:140],
                                "excerpt": snippet[:360],
                                "url": article_url,
                            }
                        )
                    if len(articles) >= limit:
                        break
    except Exception as exc:
        logger.warning("The Highlander search warning: %s", exc)

    for art in articles[:3]:
        sources.append(
            {
                "title": art["title"][:65],
                "url": art["url"],
                "source_type": "The Highlander",
            }
        )
    return articles[:limit], sources


# ---------------------------------------------------------------------------
# Tool 6: AUA Directory & Faculty/Team Members (3-Tier Fallback Pipeline)
# ---------------------------------------------------------------------------

async def _scrape_people_profile_page(
    client: httpx.AsyncClient,
    profile_url: str,
) -> Optional[Dict[str, Any]]:
    """Tier 2: Follow `https://people.aua.am/team_member/...` and scrape full bio & education."""
    try:
        resp = await client.get(profile_url)
        if resp.status_code != 200 or not resp.text:
            return None

        decoded_html = decode_cfemails_in_html(resp.text)
        title_m = re.search(r"<title[^>]*>(.*?)</title>", decoded_html, re.IGNORECASE | re.DOTALL)
        page_title = strip_html_tags(title_m.group(1)).split("–")[0].split("-")[0].strip() if title_m else ""

        art_m = re.search(
            r"<article[^>]*>(.*?)</article>",
            decoded_html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        body_text = strip_html_tags(art_m.group(1) if art_m else decoded_html)
        if not body_text:
            return None

        email_m = re.search(r"[a-zA-Z0-9._%+-]+@aua\.am", body_text, re.IGNORECASE)
        phone_m = re.search(r"(?:\+374|060)[\s\-0-9]{6,14}", body_text)

        return {
            "source_tier": "Tier 2 (AUA People Detailed Profile)",
            "name": page_title or "AUA Faculty / Team Member",
            "email": email_m.group(0) if email_m else None,
            "phone": phone_m.group(0).strip() if phone_m else None,
            "profile_url": profile_url,
            "full_profile_details": body_text[:1400],
        }
    except Exception as exc:
        logger.debug("People profile page scrape failed for %s: %s", profile_url, exc)
        return None


async def lookup_aua_directory_and_people(
    query: str,
    deep_profile: bool = True,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    """3-Tier AUA Directory & People Lookup:
    - Tier 1: `directory.aua.am` POST search + `people.aua.am/?s=<Name>` search
    - Tier 2: Follow `people.aua.am/team_member/...` ("Read More") for education, bio, courses taught
    - Tier 3: Web Search Fallback (`site:people.aua.am/team_member/ <Name> AUA` + slug probing)
    """
    results: List[Dict[str, Any]] = []
    sources: List[Dict[str, str]] = []

    person_query = extract_person_name_candidate(query)
    if not person_query:
        person_query = " ".join(extract_keywords(query)[:3]) or query.strip()[:50]

    # Try both full candidate name and individual last/first name tokens if multi-word
    tokens = [t for t in re.findall(r"[A-Za-z\-]{2,}", person_query) if t.lower() not in STOPWORDS]
    search_variants = list(dict.fromkeys([person_query] + (tokens[::-1][:2] if len(tokens) > 1 else [])))

    discovered_profile_urls: List[str] = []

    try:
        async with httpx.AsyncClient(
            timeout=11.0, follow_redirects=True, headers=HTTP_HEADERS
        ) as client:
            # ------------------------------------------------------------------
            # TIER 1A: `https://directory.aua.am/` (Phone, Email, Room, Dept, Role)
            # ------------------------------------------------------------------
            for q_var in search_variants[:2]:
                try:
                    dir_resp = await client.post(
                        "https://directory.aua.am/",
                        data={"q": q_var, "doSearch": "Search"},
                    )
                    if dir_resp.status_code == 200 and dir_resp.text:
                        clean_dir = strip_html_tags(dir_resp.text)
                        # Remove boilerplate header/footer
                        clean_dir = re.sub(
                            r"^.*?AUA Directory Search\s*",
                            "",
                            clean_dir,
                            flags=re.IGNORECASE,
                        )
                        clean_dir = re.sub(
                            r"\s*Back to AUA Home.*$",
                            "",
                            clean_dir,
                            flags=re.IGNORECASE,
                        ).strip()
                        if clean_dir and len(clean_dir) > 15 and "no results" not in clean_dir.lower():
                            results.append(
                                {
                                    "source_tier": "Tier 1 (directory.aua.am)",
                                    "query_used": q_var,
                                    "directory_record": clean_dir[:700],
                                    "url": "https://directory.aua.am/",
                                }
                            )
                            sources.append(
                                {
                                    "title": f"AUA Directory: {q_var}",
                                    "url": "https://directory.aua.am/",
                                    "source_type": "AUA Directory",
                                }
                            )
                            break
                except Exception as exc:
                    logger.debug("Tier 1A directory.aua.am warning: %s", exc)

            # ------------------------------------------------------------------
            # TIER 1B: `https://people.aua.am/?s=<Name>`
            # ------------------------------------------------------------------
            for q_var in search_variants[:2]:
                try:
                    people_search_url = f"https://people.aua.am/?s={quote_plus(q_var)}"
                    p_resp = await client.get(people_search_url)
                    if p_resp.status_code == 200 and p_resp.text:
                        for m in re.finditer(
                            r"<article[^>]*class=[\"'][^\"']*team_member[^\"']*[\"'][^>]*>(.*?)</article>",
                            p_resp.text,
                            flags=re.IGNORECASE | re.DOTALL,
                        ):
                            block = m.group(1)
                            link_m = re.search(
                                r'href=["\'](https://people\.aua\.am/team_member/[^"\']+)["\']',
                                block,
                                flags=re.IGNORECASE,
                            )
                            prof_url = link_m.group(1).strip() if link_m else ""
                            snippet = strip_html_tags(block)
                            head_check = f"{prof_url} {snippet[:120]}".lower()
                            if tokens and not any(t.lower() in head_check for t in tokens):
                                continue
                            if prof_url and prof_url not in discovered_profile_urls:
                                discovered_profile_urls.append(prof_url)
                            results.append(
                                {
                                    "source_tier": "Tier 1 (people.aua.am search)",
                                    "summary": snippet[:400],
                                    "profile_url": prof_url,
                                }
                            )
                        if discovered_profile_urls:
                            break
                except Exception as exc:
                    logger.debug("Tier 1B people.aua.am search warning: %s", exc)

            # ------------------------------------------------------------------
            # TIER 3: Web Search Fallback + Slug Probing if Tier 1 found no profile URL
            # ------------------------------------------------------------------
            if not discovered_profile_urls and tokens:
                # 3a. Direct slug probing on people.aua.am/team_member/
                slug_base = "-".join(t.lower() for t in tokens[:2])
                for candidate_slug in (slug_base, f"dr-{slug_base}"):
                    probe_url = f"https://people.aua.am/team_member/{candidate_slug}/"
                    try:
                        probe_resp = await client.get(probe_url)
                        if probe_resp.status_code == 200 and "team_member" in probe_resp.text:
                            discovered_profile_urls.append(probe_url)
                            break
                    except Exception:
                        pass

                # 3b. External Web Search Fallback targeting `https://people.aua.am/team_member/`
                if not discovered_profile_urls:
                    try:
                        ddg_query = f"site:people.aua.am/team_member/ {person_query} AUA"
                        ddg_resp = await client.post(
                            "https://html.duckduckgo.com/html/",
                            data={"q": ddg_query},
                        )
                        if ddg_resp.status_code == 200 and ddg_resp.text:
                            found_urls = re.findall(
                                r"https?://people\.aua\.am/team_member/[a-zA-Z0-9_-]+/?",
                                ddg_resp.text,
                            )
                            for u in dict.fromkeys(found_urls):
                                discovered_profile_urls.append(u)
                                if len(discovered_profile_urls) >= 2:
                                    break
                    except Exception as exc:
                        logger.debug("Tier 3 web search fallback warning: %s", exc)

            # ------------------------------------------------------------------
            # TIER 2: Follow "Read More" / `people.aua.am/team_member/...` Profile Page
            # ------------------------------------------------------------------
            if deep_profile and discovered_profile_urls:
                for prof_url in discovered_profile_urls[:2]:
                    detail = await _scrape_people_profile_page(client, prof_url)
                    if detail:
                        results.append(detail)
                        sources.append(
                            {
                                "title": detail.get("name") or "AUA Faculty Profile",
                                "url": prof_url,
                                "source_type": "AUA People Profile",
                            }
                        )
    except Exception as exc:
        logger.warning("AUA Directory & People lookup error: %s", exc)

    return results, sources[:4]


# ---------------------------------------------------------------------------
# Session Store (5-Prompt Limit Enforcement)
# ---------------------------------------------------------------------------

@dataclass
class AIChatTurn:
    role: str  # "user" | "assistant"
    content: str
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass
class AIChatSessionState:
    session_id: str
    prompts_used: int = 0
    turns: List[AIChatTurn] = field(default_factory=list)
    exchanges: List[Dict[str, Any]] = field(default_factory=list)
    updated_at: float = field(default_factory=time.time)

    def restore_from_client_history(self, client_history: List[Dict[str, Any]]) -> None:
        """Restore session state from client-persisted history if server session is empty."""
        if self.exchanges or not isinstance(client_history, list):
            return
        valid_exchanges: List[Dict[str, Any]] = []
        restored_turns: List[AIChatTurn] = []
        for item in client_history[:MAX_PROMPTS_PER_SESSION]:
            if not isinstance(item, dict):
                continue
            p_txt = str(item.get("prompt") or "").strip()
            a_txt = str(item.get("answer") or "").strip()
            if not p_txt or not a_txt:
                continue
            ts = str(item.get("timestamp") or datetime.now(timezone.utc).isoformat())
            steps = item.get("steps") if isinstance(item.get("steps"), list) else []
            sources = item.get("sources") if isinstance(item.get("sources"), list) else []
            valid_exchanges.append(
                {
                    "prompt": p_txt[:1200],
                    "answer": a_txt,
                    "steps": steps[:10],
                    "sources": sources[:10],
                    "timestamp": ts,
                }
            )
            restored_turns.append(AIChatTurn(role="user", content=p_txt[:1200], timestamp=ts))
            restored_turns.append(AIChatTurn(role="assistant", content=a_txt, timestamp=ts))

        if valid_exchanges:
            self.exchanges = valid_exchanges
            self.turns = restored_turns
            self.prompts_used = max(self.prompts_used, len(valid_exchanges))
            self.updated_at = time.time()

    def get_history_payload(self) -> List[Dict[str, Any]]:
        """Return serialized list of exchanges for frontend persistence/restoration."""
        if self.exchanges:
            return list(self.exchanges)
        reconstructed: List[Dict[str, Any]] = []
        i = 0
        while i + 1 < len(self.turns):
            u_turn = self.turns[i]
            a_turn = self.turns[i + 1]
            if u_turn.role == "user" and a_turn.role == "assistant":
                reconstructed.append(
                    {
                        "prompt": u_turn.content,
                        "answer": a_turn.content,
                        "steps": [],
                        "sources": [],
                        "timestamp": a_turn.timestamp,
                    }
                )
                i += 2
            else:
                i += 1
        return reconstructed


class AISessionStore:
    """In-memory session store enforcing the strict 5-prompt per session limit."""

    def __init__(self, max_sessions: int = 2000) -> None:
        self._sessions: Dict[str, AIChatSessionState] = {}
        self._max_sessions = max_sessions

    def _prune(self) -> None:
        now = time.time()
        expired = [
            sid
            for sid, st in self._sessions.items()
            if now - st.updated_at > SESSION_TTL_SECONDS
        ]
        for sid in expired:
            self._sessions.pop(sid, None)
        if len(self._sessions) > self._max_sessions:
            oldest = sorted(self._sessions.items(), key=lambda kv: kv[1].updated_at)[
                : len(self._sessions) - self._max_sessions
            ]
            for sid, _ in oldest:
                self._sessions.pop(sid, None)

    def get_or_create(self, session_id: Optional[str] = None) -> AIChatSessionState:
        self._prune()
        sid = (session_id or "").strip() or uuid.uuid4().hex
        if sid not in self._sessions:
            self._sessions[sid] = AIChatSessionState(session_id=sid)
        self._sessions[sid].updated_at = time.time()
        return self._sessions[sid]

    def reset_session(self, old_session_id: Optional[str] = None) -> AIChatSessionState:
        if old_session_id and old_session_id in self._sessions:
            self._sessions.pop(old_session_id, None)
        new_sid = uuid.uuid4().hex
        state = AIChatSessionState(session_id=new_sid)
        self._sessions[new_sid] = state
        return state


ai_session_store = AISessionStore()


def _parse_iso_dt(ts_str: Optional[str]) -> Optional[datetime]:
    """Safely parse an ISO-8601 timestamp string to a timezone-aware UTC datetime."""
    if not ts_str or not isinstance(ts_str, str):
        return None
    try:
        cleaned = ts_str.strip()
        if cleaned.endswith("Z"):
            cleaned = cleaned[:-1] + "+00:00"
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _merge_session_exchanges(
    existing: List[Dict[str, Any]],
    incoming: Optional[List[Dict[str, Any]]],
    reset_at: Optional[datetime] = None,
) -> Tuple[List[Dict[str, Any]], bool]:
    """Merge and deduplicate exchanges across devices (e.g. mobile + desktop) while
    filtering out any stale turns created prior to `reset_at` (when New Chat was pressed).
    Returns (merged_list, changed).
    """
    if not isinstance(incoming, list) or not incoming:
        return list(existing), False

    reset_utc = reset_at.astimezone(timezone.utc) if (reset_at and reset_at.tzinfo) else (
        reset_at.replace(tzinfo=timezone.utc) if reset_at else None
    )

    combined: List[Dict[str, Any]] = []
    seen_keys = set()

    for item in existing:
        if not isinstance(item, dict):
            continue
        p_txt = str(item.get("prompt") or "").strip()
        a_txt = str(item.get("answer") or "").strip()
        if not p_txt or not a_txt:
            continue
        key = (p_txt.lower(), a_txt[:120].lower())
        if key not in seen_keys:
            seen_keys.add(key)
            combined.append(item)

    added_any = False
    for item in incoming:
        if not isinstance(item, dict):
            continue
        p_txt = str(item.get("prompt") or "").strip()
        a_txt = str(item.get("answer") or "").strip()
        if not p_txt or not a_txt:
            continue
        ts_raw = str(item.get("timestamp") or "").strip()
        item_dt = _parse_iso_dt(ts_raw)
        if reset_utc is not None:
            # Discard any turn from before the most recent New Chat reset
            if item_dt is None or item_dt <= reset_utc:
                continue

        key = (p_txt.lower(), a_txt[:120].lower())
        if key in seen_keys:
            continue
        seen_keys.add(key)
        steps = item.get("steps") if isinstance(item.get("steps"), list) else []
        sources = item.get("sources") if isinstance(item.get("sources"), list) else []
        combined.append(
            {
                "prompt": p_txt[:1200],
                "answer": a_txt,
                "steps": steps[:10],
                "sources": sources[:10],
                "timestamp": ts_raw or datetime.now(timezone.utc).isoformat(),
            }
        )
        added_any = True

    if added_any:
        combined.sort(key=lambda x: str(x.get("timestamp") or ""))
        combined = combined[:MAX_PROMPTS_PER_SESSION]

    return combined, added_any


async def load_or_sync_ai_session(
    db_session: AsyncSession,
    session_id: Optional[str] = None,
    web_user_id: Optional[int] = None,
    client_history: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[AIChatSessionState, bool]:
    """Load or synchronize the AI chat session for an authenticated user (persisted in PostgreSQL
    so mobile and desktop views stay synchronized) or an unauthenticated browser session.
    Returns (chat_state, was_updated).
    """
    client_sid = (session_id or "").strip() or None

    if not web_user_id:
        chat_state = ai_session_store.get_or_create(client_sid)
        prev_len = len(chat_state.get_history_payload())
        if isinstance(client_history, list) and client_history:
            chat_state.restore_from_client_history(client_history)
        return chat_state, len(chat_state.get_history_payload()) > prev_len

    res = await db_session.execute(
        select(WebUserAIChatSession).where(WebUserAIChatSession.web_user_id == web_user_id)
    )
    row = res.scalar_one_or_none()

    # Gather any in-memory turns associated with client_sid (e.g., created just before login or sync)
    mem_exchanges: List[Dict[str, Any]] = []
    if client_sid and client_sid in ai_session_store._sessions:
        mem_exchanges = ai_session_store._sessions[client_sid].get_history_payload()

    if row is None:
        canonical_sid = f"user_{web_user_id}_{uuid.uuid4().hex[:12]}"
        merged, _ = _merge_session_exchanges([], mem_exchanges, reset_at=None)
        merged, _ = _merge_session_exchanges(merged, client_history, reset_at=None)

        chat_state = AIChatSessionState(session_id=canonical_sid)
        if merged:
            chat_state.restore_from_client_history(merged)

        row = WebUserAIChatSession(
            web_user_id=web_user_id,
            session_id=canonical_sid,
            prompts_used=chat_state.prompts_used,
            history_json=json.dumps(chat_state.get_history_payload(), ensure_ascii=False),
            reset_at=None,
        )
        db_session.add(row)
        await db_session.commit()
        ai_session_store._sessions[canonical_sid] = chat_state
        ai_session_store._sessions[f"user_{web_user_id}"] = chat_state
        return chat_state, len(chat_state.exchanges) > 0

    try:
        db_history = json.loads(row.history_json or "[]")
        if not isinstance(db_history, list):
            db_history = []
    except Exception:
        db_history = []

    merged, chg1 = _merge_session_exchanges(db_history, mem_exchanges, reset_at=row.reset_at)
    merged, chg2 = _merge_session_exchanges(merged, client_history, reset_at=row.reset_at)
    was_updated = chg1 or chg2

    chat_state = AIChatSessionState(session_id=row.session_id)
    if merged:
        chat_state.restore_from_client_history(merged)
        chat_state.prompts_used = max(int(row.prompts_used or 0), len(chat_state.exchanges))
    else:
        chat_state.prompts_used = 0

    if was_updated:
        row.prompts_used = chat_state.prompts_used
        row.history_json = json.dumps(chat_state.get_history_payload(), ensure_ascii=False)
        await db_session.commit()

    ai_session_store._sessions[chat_state.session_id] = chat_state
    ai_session_store._sessions[f"user_{web_user_id}"] = chat_state
    return chat_state, was_updated


async def persist_ai_session(
    db_session: AsyncSession,
    web_user_id: Optional[int],
    chat_state: AIChatSessionState,
) -> None:
    """Persist the latest AIChatSessionState to PostgreSQL for cross-device account synchronization."""
    ai_session_store._sessions[chat_state.session_id] = chat_state
    if not web_user_id:
        return
    ai_session_store._sessions[f"user_{web_user_id}"] = chat_state
    res = await db_session.execute(
        select(WebUserAIChatSession).where(WebUserAIChatSession.web_user_id == web_user_id)
    )
    row = res.scalar_one_or_none()
    history_str = json.dumps(chat_state.get_history_payload(), ensure_ascii=False)
    if row is None:
        row = WebUserAIChatSession(
            web_user_id=web_user_id,
            session_id=chat_state.session_id,
            prompts_used=chat_state.prompts_used,
            history_json=history_str,
        )
        db_session.add(row)
    else:
        row.session_id = chat_state.session_id
        row.prompts_used = chat_state.prompts_used
        row.history_json = history_str
    await db_session.commit()


async def reset_ai_session_persistent(
    db_session: AsyncSession,
    old_session_id: Optional[str] = None,
    web_user_id: Optional[int] = None,
) -> AIChatSessionState:
    """Reset the AI chat session across all devices of a signed-in user (or single anonymous browser)."""
    if not web_user_id:
        return ai_session_store.reset_session(old_session_id)

    if old_session_id:
        ai_session_store._sessions.pop(old_session_id, None)
    ai_session_store._sessions.pop(f"user_{web_user_id}", None)

    new_sid = f"user_{web_user_id}_{uuid.uuid4().hex[:12]}"
    now_utc = datetime.now(timezone.utc)
    fresh_state = AIChatSessionState(session_id=new_sid, prompts_used=0)
    ai_session_store._sessions[new_sid] = fresh_state
    ai_session_store._sessions[f"user_{web_user_id}"] = fresh_state

    res = await db_session.execute(
        select(WebUserAIChatSession).where(WebUserAIChatSession.web_user_id == web_user_id)
    )
    row = res.scalar_one_or_none()
    if row is None:
        row = WebUserAIChatSession(
            web_user_id=web_user_id,
            session_id=new_sid,
            prompts_used=0,
            history_json="[]",
            reset_at=now_utc,
        )
        db_session.add(row)
    else:
        if row.session_id:
            ai_session_store._sessions.pop(row.session_id, None)
        row.session_id = new_sid
        row.prompts_used = 0
        row.history_json = "[]"
        row.reset_at = now_utc
    await db_session.commit()
    return fresh_state


# ---------------------------------------------------------------------------
# Tool Selection Planner & Orchestrator
# ---------------------------------------------------------------------------

class ToolSelectionPlan(BaseModel):
    """Structured tool routing plan chosen by Gemini or heuristic classifier."""
    use_feedbacks: bool = Field(
        default=False,
        description="Search Course & Professor Feedbacks database (ratings, grading policy, workload, courses).",
    )
    use_directory: bool = Field(
        default=False,
        description="Look up AUA Directory & Faculty/Staff profiles (email, phone, office room, bio, education).",
    )
    use_newsroom_events: bool = Field(
        default=False,
        description="Check AUA Newsroom Events calendar for upcoming/past campus events, workshops, talks.",
    )
    use_highlander: bool = Field(
        default=False,
        description="Search The Highlander student magazine for student articles, clubs, and campus features.",
    )
    use_chat_history: bool = Field(
        default=False,
        description="Search Global Campus Chat history for student discussions, Q&As, or shared resources.",
    )
    use_anonymous_inbox: bool = Field(
        default=False,
        description="Search Anonymous Campus Posts, student Q&As, and Telegram groups.",
    )
    search_query_override: Optional[str] = Field(
        default=None,
        description="Focused search keywords or person/course name extracted from the user prompt.",
    )


def classify_tools_heuristic(prompt: str) -> ToolSelectionPlan:
    """Fast, deterministic multi-tool intent router combined with LLM planner."""
    q = (prompt or "").lower()

    use_feedbacks = any(
        w in q
        for w in (
            "professor", "prof", "dr.", "dr ", "instructor", "teacher", "grading",
            "grade", "policy", "workload", "exam", "midterm", "final", "quiz",
            "course", "class", "section", "review", "feedback", "rating", "easy",
            "hard", "strict", "harsh", "fair", "attendance", "syllabus", "cs",
            "math", "econ", "bus", "eng", "law", "cis", "data", "capstone",
        )
    )
    use_directory = any(
        w in q
        for w in (
            "professor", "prof", "dr.", "dr ", "dean", "chair", "director",
            "faculty", "staff", "office", "room", "phone", "email", "contact",
            "department", "education", "degree", "phd", "biography", "bio",
            "who is", "where is", "directory", "people", "teach", "teaches",
        )
    )
    use_newsroom = any(
        w in q
        for w in (
            "event", "events", "upcoming", "workshop", "masterclass", "seminar",
            "conference", "talk", "lecture", "open house", "newsroom", "calendar",
            "happening", "this week", "today", "tomorrow", "november", "december",
            "activity", "activities", "gallery", "hall",
        )
    )
    use_highlander = any(
        w in q
        for w in (
            "highlander", "magazine", "article", "articles", "newspaper",
            "journalism", "story", "stories", "club", "clubs", "student council",
            "student life", "editorial", "opinion",
        )
    )
    use_chat = any(
        w in q
        for w in (
            "chat", "discussion", "discussions", "talking", "said", "mention",
            "mentioned", "students say", "people say", "lounge", "spam",
            "lost", "found", "resource", "resources", "group", "telegram",
        )
    )
    use_anon = any(
        w in q
        for w in (
            "anonymous", "inbox", "confession", "post", "posts", "secret",
            "rumor", "question", "questions", "telegram group", "groups", "club",
        )
    )

    # If the user asks about a person by capitalized name (e.g. "Aram Hajian"), enable directory + feedbacks
    person_cand = extract_person_name_candidate(prompt)
    if person_cand and len(person_cand.split()) >= 2:
        use_directory = True
        use_feedbacks = True

    # Ensure at least 2 relevant sources are checked if nothing specific matched
    if not any([use_feedbacks, use_directory, use_newsroom, use_highlander, use_chat, use_anon]):
        use_feedbacks = True
        use_chat = True
        use_newsroom = True

    return ToolSelectionPlan(
        use_feedbacks=use_feedbacks,
        use_directory=use_directory,
        use_newsroom_events=use_newsroom,
        use_highlander=use_highlander,
        use_chat_history=use_chat,
        use_anonymous_inbox=use_anon,
        search_query_override=person_cand or None,
    )


AI_SYSTEM_PROMPT = """You are **AUA Hub AI**, the flagship intelligent campus assistant for students and alumni of the **American University of Armenia (AUA)**.

You have direct access to real-time retrieved context from 6 official and community AUA sources:
1. **Course & Professor Feedbacks Database (`jenzareviews` & AUA Hub Feedbacks)**: Student ratings, difficulty scores, grading fairness, workload hours, attendance rules, and written reviews.
2. **AUA Directory & People (`directory.aua.am` & `people.aua.am`)**: Faculty/staff emails, phone extensions, office rooms (PAB / Main Building), academic departments, biographies, and higher education degrees.
3. **AUA Newsroom Events (`newsroom.aua.am/events/`)**: Upcoming and recent campus conferences, masterclasses, workshops, open houses, dates, times, and room locations.
4. **The Highlander Student Magazine (`thehighlander.aua.am`)**: Student-written articles, campus culture features, and club spotlights.
5. **Global Campus Chat History**: Real student discussions and Q&As on AUA Hub.
6. **Anonymous Campus Posts & Student Telegram Groups**: Public anonymous posts, Q&As, and student group invite links.

GUIDELINES:
- Base your response directly on the **Retrieved Campus Context** provided below.
- Format your response cleanly using **Markdown** (bullet points, bold key facts like ratings/office rooms/dates/emails, and clear section headers when appropriate).
- When citing professor ratings, mention the numerical ratings (e.g. Overall Rating, Difficulty) and summarize consensus from student comments客观 and constructively.
- When citing events, include the exact **Date, Time, Building/Room** (e.g. PAB, Main Building, Akian Art Gallery), and a brief summary.
- When citing faculty directory info, include their **Title/Role, Department, Office Room, Phone, Email**, and **Higher Education** background if available.
- Protect student privacy: never invent or expose private student identities from anonymous reviews or chat logs.
- If a specific piece of information is not found in the retrieved context, state that transparently and offer the closest verified AUA details available.
"""


class AUAAssistantOrchestrator:
    """Orchestrates multi-source RAG tool execution and Gemini synthesis."""

    def __init__(self, api_key: Optional[str] = None) -> None:
        self.api_key = api_key or settings.GEMINI_API_KEY
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            key = self.api_key or settings.GEMINI_API_KEY
            if not key or genai is None:
                return None
            self._client = genai.Client(api_key=key)
        return self._client

    async def answer_prompt(
        self,
        db_session: AsyncSession,
        user_prompt: str,
        chat_state: AIChatSessionState,
        web_user_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Run tool selection, parallel RAG retrieval, and Gemini synthesis."""
        plan = classify_tools_heuristic(user_prompt)

        executed_steps: List[Dict[str, str]] = []
        aggregated_context: Dict[str, Any] = {}
        all_sources: List[Dict[str, str]] = []

        # Run DB tools sequentially on the async session, and external HTTP tools concurrently!
        if plan.use_feedbacks:
            executed_steps.append(
                {
                    "id": "feedbacks",
                    "icon": "🎓",
                    "label": "Searching course & professor feedbacks...",
                }
            )
            fb_data, fb_sources = await search_course_and_professor_feedbacks(
                db_session, user_prompt
            )
            aggregated_context["course_and_professor_feedbacks"] = fb_data
            all_sources.extend(fb_sources)

        if plan.use_chat_history:
            executed_steps.append(
                {
                    "id": "chat",
                    "icon": "💬",
                    "label": "Scanning global campus chat discussions...",
                }
            )
            chat_data, chat_sources = await search_global_chat_history(
                db_session, user_prompt
            )
            aggregated_context["campus_chat_history"] = chat_data
            all_sources.extend(chat_sources)

        if plan.use_anonymous_inbox:
            executed_steps.append(
                {
                    "id": "anonymous",
                    "icon": "🎭",
                    "label": "Checking anonymous posts & community groups...",
                }
            )
            anon_data, anon_sources = await search_anonymous_posts_and_inbox(
                db_session, user_prompt
            )
            aggregated_context["anonymous_posts_and_groups"] = anon_data
            all_sources.extend(anon_sources)

        # Launch external web parsers concurrently
        web_tasks: Dict[str, asyncio.Task] = {}
        if plan.use_directory:
            executed_steps.append(
                {
                    "id": "directory",
                    "icon": "🏛️",
                    "label": "Querying AUA Directory & People profiles...",
                }
            )
            web_tasks["aua_directory_and_faculty_profiles"] = asyncio.create_task(
                lookup_aua_directory_and_people(user_prompt, deep_profile=True)
            )

        if plan.use_newsroom_events:
            executed_steps.append(
                {
                    "id": "newsroom",
                    "icon": "📅",
                    "label": "Checking AUA Newsroom events calendar...",
                }
            )
            web_tasks["aua_newsroom_events"] = asyncio.create_task(
                fetch_aua_newsroom_events(user_prompt)
            )

        if plan.use_highlander:
            executed_steps.append(
                {
                    "id": "highlander",
                    "icon": "📰",
                    "label": "Searching The Highlander student magazine...",
                }
            )
            web_tasks["the_highlander_articles"] = asyncio.create_task(
                search_highlander_magazine(user_prompt)
            )

        if web_tasks:
            results_map = await asyncio.gather(*web_tasks.values(), return_exceptions=True)
            for key, res in zip(web_tasks.keys(), results_map):
                if isinstance(res, tuple) and len(res) == 2:
                    data_items, src_items = res
                    aggregated_context[key] = data_items
                    all_sources.extend(src_items)
                else:
                    aggregated_context[key] = []

        executed_steps.append(
            {
                "id": "synthesize",
                "icon": "✨",
                "label": "Synthesizing response from retrieved sources...",
            }
        )

        # Deduplicate sources by (title, url)
        unique_sources: List[Dict[str, str]] = []
        seen_src = set()
        for s in all_sources:
            k = (s.get("title", ""), s.get("url", ""))
            if k not in seen_src:
                seen_src.add(k)
                unique_sources.append(s)

        answer_markdown = await self._synthesize_with_gemini(
            user_prompt=user_prompt,
            chat_state=chat_state,
            retrieved_context=aggregated_context,
        )

        # Record turns and increment prompt counter
        now_iso = datetime.now(timezone.utc).isoformat()
        chat_state.prompts_used += 1
        chat_state.turns.append(AIChatTurn(role="user", content=user_prompt, timestamp=now_iso))
        chat_state.turns.append(AIChatTurn(role="assistant", content=answer_markdown, timestamp=now_iso))
        chat_state.exchanges.append(
            {
                "prompt": user_prompt,
                "answer": answer_markdown,
                "steps": executed_steps,
                "sources": unique_sources[:8],
                "timestamp": now_iso,
            }
        )
        chat_state.updated_at = time.time()
        if web_user_id:
            await persist_ai_session(db_session, web_user_id, chat_state)

        return {
            "ok": True,
            "session_id": chat_state.session_id,
            "prompts_used": chat_state.prompts_used,
            "max_prompts": MAX_PROMPTS_PER_SESSION,
            "limit_reached": chat_state.prompts_used >= MAX_PROMPTS_PER_SESSION,
            "answer": answer_markdown,
            "steps": executed_steps,
            "sources": unique_sources[:8],
            "history": chat_state.get_history_payload(),
        }

    async def _synthesize_with_gemini(
        self,
        user_prompt: str,
        chat_state: AIChatSessionState,
        retrieved_context: Dict[str, Any],
    ) -> str:
        """Synthesize a comprehensive markdown response using Gemini 2.5 Flash, with offline fallback."""
        client = self._get_client()
        if client is not None and types is not None:
            try:
                history_lines = []
                for t in chat_state.turns[-6:]:
                    role_lbl = "Student" if t.role == "user" else "AUA Hub AI"
                    history_lines.append(f"{role_lbl}: {t.content[:500]}")
                history_block = "\n".join(history_lines) if history_lines else "None (First prompt in session)"

                context_json = json.dumps(retrieved_context, ensure_ascii=False, indent=2)
                full_prompt = (
                    f"Prior Conversation in this Session:\n{history_block}\n\n"
                    f"Retrieved Campus Context (JSON):\n{context_json}\n\n"
                    f"Student Prompt: {user_prompt}\n\n"
                    "Provide a helpful, well-structured Markdown answer grounded in the retrieved AUA data above."
                )

                config = types.GenerateContentConfig(
                    temperature=0.25,
                    system_instruction=AI_SYSTEM_PROMPT,
                )
                response = await client.aio.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=full_prompt,
                    config=config,
                )
                if response and response.text:
                    return response.text.strip()
            except Exception as exc:
                logger.warning("Gemini synthesis fallback triggered: %s", exc)

        return self._synthesize_fallback(user_prompt, retrieved_context)

    def _synthesize_fallback(
        self,
        user_prompt: str,
        ctx: Dict[str, Any],
    ) -> str:
        """Deterministic structured Markdown synthesis when Gemini API key is absent or offline."""
        sections: List[str] = []

        # 1. Directory & Faculty Profiles
        dir_items = ctx.get("aua_directory_and_faculty_profiles") or []
        if dir_items:
            lines = ["### 🏛️ AUA Directory & Faculty Profile"]
            for item in dir_items[:3]:
                if item.get("directory_record"):
                    lines.append(f"- **Directory Record:** {item['directory_record']}")
                if item.get("full_profile_details"):
                    lines.append(
                        f"- **{item.get('name', 'Faculty Profile')}:** {item['full_profile_details'][:550]}"
                    )
                elif item.get("summary"):
                    lines.append(f"- {item['summary']}")
            sections.append("\n".join(lines))

        # 2. Course & Professor Feedbacks
        fb_items = ctx.get("course_and_professor_feedbacks") or []
        if fb_items:
            lines = ["### 🎓 Course & Professor Feedbacks"]
            for item in fb_items[:5]:
                itype = item.get("type", "")
                if itype == "professor_feedback_summary":
                    avg_r = item.get("avg_overall_rating")
                    avg_d = item.get("avg_difficulty")
                    rating_str = f"**{avg_r}/5.0**" if avg_r is not None else "N/A"
                    diff_str = f" (Avg Difficulty: **{avg_d}/5.0**)" if avg_d is not None else ""
                    lines.append(
                        f"- **{item.get('professor_name')}** ({item.get('department')}): "
                        f"Overall Rating {rating_str}{diff_str} across {item.get('review_count_sampled', 0)} recent reviews."
                    )
                    for rev in (item.get("reviews") or [])[:3]:
                        if rev.get("comment"):
                            lines.append(
                                f"  - *[{rev.get('course_code')}, Rating {rev.get('overall_rating')}/5]*: \"{rev['comment'][:240]}\""
                            )
                elif itype == "course_or_professor_review":
                    lines.append(
                        f"- **{item.get('professor_name')} ({item.get('course_code')})** — "
                        f"Rating **{item.get('overall_rating')}/5**: \"{item.get('comment', '')[:220]}\""
                    )
                elif "feedback" in itype:
                    lines.append(
                        f"- **{item.get('target')}** ({item.get('rating')}/5): \"{item.get('comment', '')[:220]}\""
                    )
                elif itype == "sonis_course_section":
                    lines.append(
                        f"- **Course Section:** `{item.get('course_code')}` (Sec {item.get('section')}) — "
                        f"{item.get('course_title')} • Instructor: **{item.get('instructor')}** • {item.get('schedule')}"
                    )
            sections.append("\n".join(lines))

        # 3. AUA Newsroom Events
        ev_items = ctx.get("aua_newsroom_events") or []
        if ev_items:
            lines = ["### 📅 AUA Newsroom Events"]
            for ev in ev_items[:5]:
                lines.append(f"- **[{ev.get('title')}]({ev.get('url')})** — {ev.get('details', '')[:240]}")
            sections.append("\n".join(lines))

        # 4. The Highlander Student Magazine
        hl_items = ctx.get("the_highlander_articles") or []
        if hl_items:
            lines = ["### 📰 The Highlander Student Magazine"]
            for art in hl_items[:4]:
                lines.append(f"- **[{art.get('title')}]({art.get('url')})** — {art.get('excerpt', '')[:220]}")
            sections.append("\n".join(lines))

        # 5. Global Campus Chat & Anonymous Posts
        chat_items = ctx.get("campus_chat_history") or []
        if chat_items:
            lines = ["### 💬 Campus Chat Discussions"]
            for msg in chat_items[:4]:
                lines.append(f"- *[{msg.get('timestamp')}]* \"{msg.get('text')}\"")
            sections.append("\n".join(lines))

        anon_items = ctx.get("anonymous_posts_and_groups") or []
        if anon_items:
            lines = ["### 🎭 Community Posts & Student Groups"]
            for item in anon_items[:4]:
                if item.get("type") == "student_telegram_group":
                    lines.append(
                        f"- **Telegram Group — {item.get('name')}:** {item.get('description')} ([Join]({item.get('invite_link')}))"
                    )
                elif item.get("type") == "anonymous_campus_post":
                    lines.append(f"- **Post {item.get('post_number')}:** \"{item.get('content', '')[:200]}\"")
            sections.append("\n".join(lines))

        if not sections:
            return (
                "I searched across AUA Hub's feedbacks, directory, Newsroom events, The Highlander, "
                "and campus chat, but didn't find exact records matching that query. Try asking about a "
                "specific professor, course code, campus event, or student club!"
            )

        return "\n\n".join(sections)


ai_orchestrator = AUAAssistantOrchestrator()
