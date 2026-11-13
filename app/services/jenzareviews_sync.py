"""Shared Database Mirroring, 5-Minute Sync Worker, and Anonymized Write-Through Pipeline for jenzareviews."""

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

import asyncpg
import httpx
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import (
    AcademicFeedback,
    JenzaProfessor,
    JenzaProfessorRequest,
    JenzaReview,
    JenzaReviewVote,
)
from app.database.session import get_db_session

logger = logging.getLogger(__name__)

# Fields that must ALWAYS be stripped before any data leaves our backend for remote jenzareviews
_FORBIDDEN_IDENTITY_KEYS = {
    "web_user_id",
    "user_id",
    "telegram_id",
    "telegram_user_id",
    "email",
    "aua_email",
    "author",
    "author_name",
    "author_email",
    "author_avatar",
    "author_id",
    "first_name",
    "last_name",
    "full_name",
    "username",
    "phone",
    "ip",
    "ip_address",
    "session_token",
    "jwt",
}

_NON_PERSON_INSTRUCTOR_NAMES = {
    "",
    "staff",
    "tba",
    "tbd",
    "n/a",
    "none",
    "unknown",
    "faculty",
    "aua faculty",
}

_sync_lock = asyncio.Lock()
_last_sync_status: Dict[str, Any] = {
    "last_sync_at": None,
    "status": "idle",
    "professors_synced": 0,
    "reviews_synced": 0,
    "votes_synced": 0,
    "requests_synced": 0,
    "local_pushed": 0,
    "error": None,
}


def get_jenzareviews_sync_status() -> Dict[str, Any]:
    """Return latest synchronization metrics for health checks / diagnostics."""
    return dict(_last_sync_status)


def _extract_project_ref(hostname: str, supabase_url: str = "") -> Optional[str]:
    """Extract the Supabase project reference ID from DB host or SUPABASE_URL."""
    if hostname:
        m = re.match(r"^db\.([a-z0-9]+)\.supabase\.co$", hostname.strip().lower())
        if m:
            return m.group(1)
    if supabase_url:
        m2 = re.search(r"https?://([a-z0-9]+)\.supabase\.co", supabase_url.strip().lower())
        if m2:
            return m2.group(1)
    return None


async def connect_remote_jenzareviews_db() -> asyncpg.Connection:
    """Connect to the remote Supabase PostgreSQL database (`jenzareviews`).

    Attempts the configured `DATABASE_URL` first. If direct connection fails
    (e.g., IPv6-only `db.<ref>.supabase.co` inside an IPv4 Docker bridge),
    automatically falls back to Supabase's IPv4 Supavisor pooler endpoints.
    """
    raw_url = (settings.DATABASE_URL or "").strip()
    if not raw_url:
        raise RuntimeError("DATABASE_URL is not configured in environment.")

    parsed = urlparse(raw_url)
    host = parsed.hostname or ""
    port = parsed.port or 5432
    user = unquote(parsed.username or "postgres")
    password = unquote(parsed.password or "")
    database = (parsed.path or "/postgres").lstrip("/") or "postgres"

    candidates: List[Tuple[str, int, str]] = [
        (host, port, user),
    ]

    project_ref = _extract_project_ref(host, settings.SUPABASE_URL)
    if project_ref:
        pooler_user = f"postgres.{project_ref}" if "." not in user else user
        for pooler_host in (
            "aws-1-eu-central-1.pooler.supabase.com",
            "aws-0-eu-central-1.pooler.supabase.com",
        ):
            if (pooler_host, 5432, pooler_user) not in candidates:
                candidates.append((pooler_host, 5432, pooler_user))

    last_err: Optional[Exception] = None
    for cand_host, cand_port, cand_user in candidates:
        if not cand_host:
            continue
        try:
            conn = await asyncpg.connect(
                host=cand_host,
                port=cand_port,
                user=cand_user,
                password=password,
                database=database,
                ssl="require",
                statement_cache_size=0,
                timeout=10,
            )
            return conn
        except Exception as exc:
            last_err = exc
            logger.debug(
                "Remote DB connection attempt to %s:%s (user=%s) failed: %s",
                cand_host,
                cand_port,
                cand_user,
                exc,
            )

    raise RuntimeError(f"Could not connect to remote jenzareviews database: {last_err}")


def anonymize_review_payload(raw_payload: Dict[str, Any]) -> Dict[str, Any]:
    """Strip all user-identifying metadata before pushing a review to remote `jenzareviews`.

    Enforces:
    - Any PII / user identity keys (`web_user_id`, `user_id`, `email`, `author_name`, etc.)
      are removed.
    - `user_id` is explicitly set to `None` (`NULL` in PostgreSQL).
    - Only anonymous academic feedback fields (`professor_id`, `rating`, `comment`,
      `course`, `would_take_again`) are preserved.
    """
    cleaned = {
        k: v
        for k, v in (raw_payload or {}).items()
        if k.lower() not in _FORBIDDEN_IDENTITY_KEYS
    }

    prof_id = cleaned.get("professor_id")
    if prof_id is not None:
        prof_id = int(prof_id)

    raw_rating = cleaned.get("rating", 5)
    rating_val: Optional[float] = None
    if raw_rating is not None:
        rating_val = float(max(1.0, min(5.0, float(raw_rating))))

    comment_val = str(cleaned.get("comment") or "").strip()

    course_val = cleaned.get("course") or cleaned.get("course_name")
    if course_val is not None:
        course_val = str(course_val).strip() or None

    wta = cleaned.get("would_take_again")
    if wta is not None:
        wta = bool(wta)

    return {
        "user_id": None,  # Strictly NULL to guarantee complete anonymity on shared DB
        "professor_id": prof_id,
        "rating": rating_val,
        "comment": comment_val,
        "course": course_val,
        "would_take_again": wta,
    }


def _normalize_name(name: str) -> str:
    """Normalize professor name for matching."""
    return " ".join((name or "").strip().split()).lower()


def _extract_candidate_professor_name(feedback: AcademicFeedback) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Determine (professor_name, department, course_label) from a local AcademicFeedback record."""
    if feedback.target_type == "instructor":
        prof_name = (feedback.target_title or feedback.target_key or "").strip()
        dept = (feedback.target_subtitle or "").strip() or None
        course_label = (feedback.course_name or "").strip() or None
        return prof_name, dept, course_label

    # For course feedbacks: target_key is course code (e.g. "CHSS195"), target_subtitle is instructor(s)
    course_label = (feedback.course_name or feedback.target_key or feedback.target_title or "").strip() or None
    subtitle = (feedback.target_subtitle or "").strip()
    if not subtitle:
        return None, None, course_label

    # If multiple instructors are listed comma-separated, take the primary instructor
    first_instructor = subtitle.split(",")[0].strip()
    if _normalize_name(first_instructor) in _NON_PERSON_INSTRUCTOR_NAMES:
        return None, None, course_label
    return first_instructor, None, course_label


def _ensure_aware_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Ensure datetime is timezone-aware UTC."""
    if dt is None:
        return None
    if isinstance(dt, str):
        s = dt.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _to_naive_utc(dt: Optional[datetime]) -> datetime:
    """Convert a datetime to naive UTC for remote `timestamp without time zone` columns."""
    aware = _ensure_aware_utc(dt) or datetime.now(timezone.utc)
    return aware.replace(tzinfo=None)


async def resolve_or_create_remote_professor(
    session: AsyncSession,
    prof_name: str,
    department: Optional[str] = None,
    remote_conn: Optional[asyncpg.Connection] = None,
) -> Optional[int]:
    """Find or create a professor in remote `public.professors` and local `jenza_professors`."""
    clean_name = " ".join((prof_name or "").strip().split())
    if _normalize_name(clean_name) in _NON_PERSON_INSTRUCTOR_NAMES:
        return None

    # 1. Check local mirror first (case-insensitive)
    stmt = select(JenzaProfessor).where(JenzaProfessor.name.ilike(clean_name))
    res = await session.execute(stmt)
    existing_local = res.scalars().first()
    if existing_local:
        return int(existing_local.id)

    # 2. Query / insert in remote database
    owns_conn = False
    conn = remote_conn
    try:
        if conn is None:
            conn = await connect_remote_jenzareviews_db()
            owns_conn = True

        row = await conn.fetchrow(
            "SELECT id, name, department FROM public.professors WHERE lower(btrim(name)) = lower(btrim($1)) LIMIT 1",
            clean_name,
        )
        if not row:
            row = await conn.fetchrow(
                """
                INSERT INTO public.professors (name, department)
                VALUES ($1, $2)
                ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name
                RETURNING id, name, department
                """,
                clean_name,
                department,
            )

        if row:
            prof_id = int(row["id"])
            upsert_stmt = (
                pg_insert(JenzaProfessor)
                .values(
                    id=prof_id,
                    name=row["name"],
                    department=row["department"],
                    synced_at=datetime.now(timezone.utc),
                )
                .on_conflict_do_update(
                    index_elements=[JenzaProfessor.id],
                    set_={
                        "name": row["name"],
                        "department": row["department"],
                        "synced_at": datetime.now(timezone.utc),
                    },
                )
            )
            await session.execute(upsert_stmt)
            return prof_id
    except Exception as exc:
        logger.warning("Failed to resolve/create remote professor '%s' via asyncpg: %s", clean_name, exc)
        # Fallback to Supabase REST API if configured
        prof_id = await _rest_resolve_or_create_professor(clean_name, department)
        if prof_id is not None:
            upsert_stmt = (
                pg_insert(JenzaProfessor)
                .values(
                    id=prof_id,
                    name=clean_name,
                    department=department,
                    synced_at=datetime.now(timezone.utc),
                )
                .on_conflict_do_update(
                    index_elements=[JenzaProfessor.id],
                    set_={"synced_at": datetime.now(timezone.utc)},
                )
            )
            await session.execute(upsert_stmt)
            return prof_id
    finally:
        if owns_conn and conn is not None:
            await conn.close()

    return None


async def _rest_resolve_or_create_professor(clean_name: str, department: Optional[str]) -> Optional[int]:
    """PostgREST fallback to find or create a professor when direct TCP is unavailable."""
    base_url = (settings.SUPABASE_URL or "").rstrip("/")
    api_key = settings.SUPABASE_SECRET_KEY or settings.SUPABASE_PUBLISHABLE_KEY
    if not base_url or not api_key:
        return None
    headers = {
        "apikey": api_key,
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation,resolution=merge-duplicates",
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            f"{base_url}/rest/v1/professors",
            params={"name": f"ilike.{clean_name}", "select": "id,name,department", "limit": "1"},
            headers=headers,
        )
        if r.status_code == 200 and r.json():
            return int(r.json()[0]["id"])
        r2 = await client.post(
            f"{base_url}/rest/v1/professors",
            params={"on_conflict": "name"},
            json={"name": clean_name, "department": department},
            headers=headers,
        )
        if r2.status_code in (200, 201) and r2.json():
            return int(r2.json()[0]["id"])
    return None


async def push_anonymized_review_to_remote(
    session: AsyncSession,
    feedback: AcademicFeedback,
    remote_conn: Optional[asyncpg.Connection] = None,
) -> Optional[int]:
    """Anonymize a local `AcademicFeedback` record and push it to remote `public.reviews`.

    Returns the remote `reviews.id` if pushed (or already linked), or `None` if skipped.
    """
    if feedback.remote_review_id is not None:
        return int(feedback.remote_review_id)

    prof_name, dept, course_label = _extract_candidate_professor_name(feedback)
    if not prof_name:
        logger.info(
            "Skipping remote jenzareviews push for feedback id=%s (no resolvable instructor name).",
            feedback.id,
        )
        return None

    owns_conn = False
    conn = remote_conn
    try:
        if conn is None:
            conn = await connect_remote_jenzareviews_db()
            owns_conn = True

        prof_id = feedback.remote_professor_id
        if not prof_id:
            prof_id = await resolve_or_create_remote_professor(
                session, prof_name, department=dept, remote_conn=conn
            )
        if not prof_id:
            return None

        # Build raw dict containing local fields (including user fields) and pass through anonymizer
        raw_record = {
            "web_user_id": feedback.web_user_id,
            "user_id": feedback.web_user_id,
            "professor_id": prof_id,
            "rating": feedback.rating,
            "comment": feedback.comment,
            "course": course_label,
            "would_take_again": feedback.would_take_again,
        }
        anon_payload = anonymize_review_payload(raw_record)
        assert anon_payload.get("user_id") is None, "Anonymized payload must never include user_id"

        created_naive = _to_naive_utc(feedback.created_at)

        # Check if an identical review already exists on remote to prevent duplicates on retry
        existing_remote = await conn.fetchrow(
            """
            SELECT id, created_at
            FROM public.reviews
            WHERE professor_id = $1
              AND user_id IS NULL
              AND COALESCE(comment, '') = $2
              AND rating = $3
            ORDER BY id DESC
            LIMIT 1
            """,
            anon_payload["professor_id"],
            anon_payload["comment"],
            anon_payload["rating"],
        )

        if existing_remote:
            remote_id = int(existing_remote["id"])
            remote_created = _ensure_aware_utc(existing_remote["created_at"]) or datetime.now(timezone.utc)
        else:
            inserted = await conn.fetchrow(
                """
                INSERT INTO public.reviews (user_id, professor_id, rating, comment, created_at, course, would_take_again)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING id, created_at
                """,
                anon_payload["user_id"],  # None
                anon_payload["professor_id"],
                anon_payload["rating"],
                anon_payload["comment"],
                created_naive,
                anon_payload["course"],
                anon_payload["would_take_again"],
            )
            remote_id = int(inserted["id"])
            remote_created = _ensure_aware_utc(inserted["created_at"]) or datetime.now(timezone.utc)

        feedback.remote_review_id = remote_id
        feedback.remote_professor_id = prof_id
        if anon_payload["course"] and not feedback.course_name:
            feedback.course_name = anon_payload["course"]

        # Mirror in local jenza_reviews table
        mirror_stmt = (
            pg_insert(JenzaReview)
            .values(
                id=remote_id,
                user_id=None,
                professor_id=prof_id,
                rating=anon_payload["rating"],
                comment=anon_payload["comment"],
                created_at=remote_created,
                course=anon_payload["course"],
                would_take_again=anon_payload["would_take_again"],
                local_feedback_id=feedback.id,
                synced_at=datetime.now(timezone.utc),
            )
            .on_conflict_do_update(
                index_elements=[JenzaReview.id],
                set_={
                    "professor_id": prof_id,
                    "rating": anon_payload["rating"],
                    "comment": anon_payload["comment"],
                    "course": anon_payload["course"],
                    "would_take_again": anon_payload["would_take_again"],
                    "local_feedback_id": feedback.id,
                    "synced_at": datetime.now(timezone.utc),
                },
            )
        )
        await session.execute(mirror_stmt)
        await session.commit()
        logger.info(
            "Pushed anonymized review to remote jenzareviews: local_id=%s -> remote_id=%s (professor_id=%s)",
            feedback.id,
            remote_id,
            prof_id,
        )
        return remote_id

    except Exception as exc:
        logger.warning(
            "Direct asyncpg push failed for feedback id=%s (%s); attempting REST fallback.",
            feedback.id,
            exc,
        )
        remote_id = await _rest_push_anonymized_review(session, feedback, prof_name, dept, course_label)
        return remote_id
    finally:
        if owns_conn and conn is not None:
            await conn.close()


async def _rest_push_anonymized_review(
    session: AsyncSession,
    feedback: AcademicFeedback,
    prof_name: str,
    dept: Optional[str],
    course_label: Optional[str],
) -> Optional[int]:
    """Fallback to push an anonymized review via Supabase PostgREST API."""
    base_url = (settings.SUPABASE_URL or "").rstrip("/")
    api_key = settings.SUPABASE_SECRET_KEY or settings.SUPABASE_PUBLISHABLE_KEY
    if not base_url or not api_key:
        return None
    try:
        prof_id = feedback.remote_professor_id or await _rest_resolve_or_create_professor(prof_name, dept)
        if not prof_id:
            return None
        anon_payload = anonymize_review_payload(
            {
                "web_user_id": feedback.web_user_id,
                "professor_id": prof_id,
                "rating": feedback.rating,
                "comment": feedback.comment,
                "course": course_label,
                "would_take_again": feedback.would_take_again,
            }
        )
        headers = {
            "apikey": api_key,
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{base_url}/rest/v1/reviews",
                json=anon_payload,
                headers=headers,
            )
            if resp.status_code in (200, 201) and resp.json():
                row = resp.json()[0]
                remote_id = int(row["id"])
                feedback.remote_review_id = remote_id
                feedback.remote_professor_id = prof_id
                await session.commit()
                return remote_id
    except Exception as exc:
        logger.error("PostgREST fallback push also failed for feedback id=%s: %s", feedback.id, exc)
    return None


async def update_anonymized_review_on_remote(
    session: AsyncSession,
    feedback: AcademicFeedback,
) -> bool:
    """Propagate a local review edit (rating/comment/course/would_take_again) to remote `jenzareviews` anonymously."""
    if not feedback.remote_review_id:
        remote_id = await push_anonymized_review_to_remote(session, feedback)
        return remote_id is not None

    anon_payload = anonymize_review_payload(
        {
            "web_user_id": feedback.web_user_id,
            "professor_id": feedback.remote_professor_id,
            "rating": feedback.rating,
            "comment": feedback.comment,
            "course": feedback.course_name,
            "would_take_again": feedback.would_take_again,
        }
    )

    conn: Optional[asyncpg.Connection] = None
    try:
        conn = await connect_remote_jenzareviews_db()
        await conn.execute(
            """
            UPDATE public.reviews
            SET rating = $1,
                comment = $2,
                course = COALESCE($3, course),
                would_take_again = COALESCE($4, would_take_again),
                user_id = NULL
            WHERE id = $5
            """,
            anon_payload["rating"],
            anon_payload["comment"],
            anon_payload["course"],
            anon_payload["would_take_again"],
            int(feedback.remote_review_id),
        )
        await session.execute(
            update(JenzaReview)
            .where(JenzaReview.id == int(feedback.remote_review_id))
            .values(
                rating=anon_payload["rating"],
                comment=anon_payload["comment"],
                course=anon_payload["course"],
                would_take_again=anon_payload["would_take_again"],
                synced_at=datetime.now(timezone.utc),
            )
        )
        await session.commit()
        return True
    except Exception as exc:
        logger.warning("Failed to update remote review id=%s: %s", feedback.remote_review_id, exc)
        return False
    finally:
        if conn is not None:
            await conn.close()


async def delete_review_on_remote(remote_review_id: Optional[int]) -> bool:
    """Delete a review from remote `public.reviews` when deleted locally by its author/admin."""
    if not remote_review_id:
        return False
    conn: Optional[asyncpg.Connection] = None
    try:
        conn = await connect_remote_jenzareviews_db()
        await conn.execute("DELETE FROM public.reviews WHERE id = $1", int(remote_review_id))
        return True
    except Exception as exc:
        logger.warning("Failed to delete remote review id=%s: %s", remote_review_id, exc)
        return False
    finally:
        if conn is not None:
            await conn.close()


async def _fetch_remote_dataset() -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Fetch professors, reviews, review_votes, and professor_requests from remote jenzareviews."""
    conn: Optional[asyncpg.Connection] = None
    try:
        conn = await connect_remote_jenzareviews_db()
        prof_rows = await conn.fetch("SELECT id, name, department FROM public.professors ORDER BY id ASC")
        rev_rows = await conn.fetch(
            "SELECT id, user_id::text AS user_id, professor_id, rating, comment, created_at, course, would_take_again "
            "FROM public.reviews ORDER BY id ASC"
        )
        vote_rows = await conn.fetch(
            "SELECT id, review_id, user_id::text AS user_id, vote, created_at "
            "FROM public.review_votes ORDER BY id ASC"
        )
        req_rows = await conn.fetch(
            "SELECT id::text AS id, name, department, created_at, user_id::text AS user_id "
            "FROM public.professor_requests ORDER BY created_at ASC"
        )
        return (
            [dict(r) for r in prof_rows],
            [dict(r) for r in rev_rows],
            [dict(r) for r in vote_rows],
            [dict(r) for r in req_rows],
        )
    except Exception as pg_err:
        logger.warning("Direct asyncpg pull failed (%s); falling back to Supabase REST API.", pg_err)
    finally:
        if conn is not None:
            await conn.close()

    # Fallback: fetch via Supabase PostgREST API
    base_url = (settings.SUPABASE_URL or "").rstrip("/")
    api_key = settings.SUPABASE_SECRET_KEY or settings.SUPABASE_PUBLISHABLE_KEY
    if not base_url or not api_key:
        raise RuntimeError("Neither direct DATABASE_URL nor SUPABASE_URL REST credentials are available.")

    headers = {
        "apikey": api_key,
        "Authorization": f"Bearer {api_key}",
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        r_prof = await client.get(f"{base_url}/rest/v1/professors?select=*&order=id.asc&limit=5000", headers=headers)
        r_rev = await client.get(f"{base_url}/rest/v1/reviews?select=*&order=id.asc&limit=10000", headers=headers)
        r_vote = await client.get(f"{base_url}/rest/v1/review_votes?select=*&order=id.asc&limit=20000", headers=headers)
        r_req = await client.get(f"{base_url}/rest/v1/professor_requests?select=*&limit=5000", headers=headers)
        r_prof.raise_for_status()
        r_rev.raise_for_status()
        r_vote.raise_for_status()
        r_req.raise_for_status()
        return r_prof.json(), r_rev.json(), r_vote.json(), r_req.json()


async def sync_jenzareviews_from_remote() -> Dict[str, Any]:
    """Pull fresh data from remote `jenzareviews` and synchronize local mirror & `academic_feedbacks`."""
    if _sync_lock.locked():
        logger.debug("jenzareviews sync already in progress; skipping overlapping trigger.")
        return get_jenzareviews_sync_status()

    async with _sync_lock:
        now_utc = datetime.now(timezone.utc)
        _last_sync_status["status"] = "running"
        _last_sync_status["error"] = None

        try:
            professors, reviews, votes, requests = await _fetch_remote_dataset()

            # Pre-aggregate vote counts per review_id
            vote_totals: Dict[int, Tuple[int, int]] = {}
            for v in votes:
                rid = int(v["review_id"])
                val = int(v.get("vote") or 0)
                up, down = vote_totals.get(rid, (0, 0))
                if val > 0:
                    up += 1
                elif val < 0:
                    down += 1
                vote_totals[rid] = (up, down)

            prof_by_id: Dict[int, Dict[str, Any]] = {}
            for p in professors:
                pid = int(p["id"])
                prof_by_id[pid] = {
                    "id": pid,
                    "name": " ".join(str(p.get("name") or "").strip().split()),
                    "department": (str(p.get("department") or "").strip() or None),
                }

            async with get_db_session() as session:
                # 1. Upsert JenzaProfessor rows
                if prof_by_id:
                    prof_values = [
                        {
                            "id": p["id"],
                            "name": p["name"],
                            "department": p["department"],
                            "synced_at": now_utc,
                        }
                        for p in prof_by_id.values()
                    ]
                    stmt_prof = pg_insert(JenzaProfessor).values(prof_values)
                    stmt_prof = stmt_prof.on_conflict_do_update(
                        index_elements=[JenzaProfessor.id],
                        set_={
                            "name": stmt_prof.excluded.name,
                            "department": stmt_prof.excluded.department,
                            "synced_at": stmt_prof.excluded.synced_at,
                        },
                    )
                    await session.execute(stmt_prof)

                # 2. Upsert JenzaReview rows
                valid_prof_ids = set(prof_by_id.keys())
                if reviews:
                    rev_values = []
                    for r in reviews:
                        rid = int(r["id"])
                        pid = int(r["professor_id"]) if r.get("professor_id") is not None else None
                        if pid is not None and pid not in valid_prof_ids:
                            pid = None
                        up, down = vote_totals.get(rid, (0, 0))
                        rev_values.append(
                            {
                                "id": rid,
                                "user_id": str(r["user_id"]) if r.get("user_id") else None,
                                "professor_id": pid,
                                "rating": float(r["rating"]) if r.get("rating") is not None else None,
                                "comment": r.get("comment"),
                                "created_at": _ensure_aware_utc(r.get("created_at")),
                                "course": (str(r.get("course") or "").strip() or None),
                                "would_take_again": r.get("would_take_again"),
                                "upvotes": up,
                                "downvotes": down,
                                "synced_at": now_utc,
                            }
                        )
                    stmt_rev = pg_insert(JenzaReview).values(rev_values)
                    stmt_rev = stmt_rev.on_conflict_do_update(
                        index_elements=[JenzaReview.id],
                        set_={
                            "user_id": stmt_rev.excluded.user_id,
                            "professor_id": stmt_rev.excluded.professor_id,
                            "rating": stmt_rev.excluded.rating,
                            "comment": stmt_rev.excluded.comment,
                            "created_at": stmt_rev.excluded.created_at,
                            "course": stmt_rev.excluded.course,
                            "would_take_again": stmt_rev.excluded.would_take_again,
                            "upvotes": stmt_rev.excluded.upvotes,
                            "downvotes": stmt_rev.excluded.downvotes,
                            "synced_at": stmt_rev.excluded.synced_at,
                        },
                    )
                    await session.execute(stmt_rev)

                # 3. Upsert JenzaReviewVote rows (in batches of 500 to stay well below parameter limits)
                valid_rev_ids = {int(r["id"]) for r in reviews}
                filtered_votes = [v for v in votes if int(v["review_id"]) in valid_rev_ids]
                for idx in range(0, len(filtered_votes), 500):
                    batch = filtered_votes[idx : idx + 500]
                    vote_values = [
                        {
                            "id": int(v["id"]),
                            "review_id": int(v["review_id"]),
                            "user_id": str(v.get("user_id") or ""),
                            "vote": int(v.get("vote") or 0),
                            "created_at": _ensure_aware_utc(v.get("created_at")),
                        }
                        for v in batch
                    ]
                    stmt_vote = pg_insert(JenzaReviewVote).values(vote_values)
                    stmt_vote = stmt_vote.on_conflict_do_update(
                        index_elements=[JenzaReviewVote.id],
                        set_={
                            "vote": stmt_vote.excluded.vote,
                            "created_at": stmt_vote.excluded.created_at,
                        },
                    )
                    await session.execute(stmt_vote)

                # 4. Upsert JenzaProfessorRequest rows
                if requests:
                    req_values = [
                        {
                            "id": str(req["id"]),
                            "name": " ".join(str(req.get("name") or "").strip().split()),
                            "department": (str(req.get("department") or "").strip() or None),
                            "created_at": _ensure_aware_utc(req.get("created_at")),
                            "user_id": str(req["user_id"]) if req.get("user_id") else None,
                        }
                        for req in requests
                        if req.get("id") and req.get("name")
                    ]
                    if req_values:
                        stmt_req = pg_insert(JenzaProfessorRequest).values(req_values)
                        stmt_req = stmt_req.on_conflict_do_update(
                            index_elements=[JenzaProfessorRequest.id],
                            set_={
                                "name": stmt_req.excluded.name,
                                "department": stmt_req.excluded.department,
                            },
                        )
                        await session.execute(stmt_req)

                await session.commit()

                # 5. Push any un-synced local reviews (where remote_review_id IS NULL) to remote anonymously FIRST
                #    so they receive their `remote_review_id` before we import remote reviews into `academic_feedbacks`.
                unsynced_res = await session.execute(
                    select(AcademicFeedback).where(AcademicFeedback.remote_review_id.is_(None))
                )
                unsynced_fbs = list(unsynced_res.scalars().all())
                pushed_count = 0
                if unsynced_fbs:
                    conn: Optional[asyncpg.Connection] = None
                    try:
                        conn = await connect_remote_jenzareviews_db()
                    except Exception:
                        conn = None
                    try:
                        for local_fb in unsynced_fbs:
                            rid = await push_anonymized_review_to_remote(session, local_fb, remote_conn=conn)
                            if rid is not None:
                                pushed_count += 1
                    finally:
                        if conn is not None:
                            await conn.close()

                # 6. Synchronize remote reviews into local `academic_feedbacks` table
                existing_fb_res = await session.execute(select(AcademicFeedback))
                existing_fbs = list(existing_fb_res.scalars().all())
                fb_by_remote_id: Dict[int, AcademicFeedback] = {
                    int(fb.remote_review_id): fb
                    for fb in existing_fbs
                    if fb.remote_review_id is not None
                }

                for r in reviews:
                    rid = int(r["id"])
                    pid = int(r["professor_id"]) if r.get("professor_id") is not None else None
                    prof_info = prof_by_id.get(pid) if pid is not None else None
                    if not prof_info:
                        continue

                    prof_name = prof_info["name"]
                    prof_dept = prof_info["department"]
                    raw_rating = r.get("rating")
                    int_rating = int(round(float(raw_rating))) if raw_rating is not None else 5
                    int_rating = max(1, min(5, int_rating))
                    comment_text = str(r.get("comment") or "").strip()
                    course_text = str(r.get("course") or "").strip() or None
                    wta = r.get("would_take_again")
                    created_dt = _ensure_aware_utc(r.get("created_at")) or now_utc
                    subtitle = prof_dept or course_text or "AUA Faculty"

                    existing_fb = fb_by_remote_id.get(rid)
                    if existing_fb is not None:
                        # Update fields from remote if changed, preserving local web_user_id & is_anonymous
                        existing_fb.remote_professor_id = pid
                        existing_fb.rating = int_rating
                        if comment_text:
                            existing_fb.comment = comment_text
                        existing_fb.course_name = course_text
                        existing_fb.would_take_again = wta
                        if not existing_fb.target_subtitle and subtitle:
                            existing_fb.target_subtitle = subtitle
                    else:
                        new_fb = AcademicFeedback(
                            web_user_id=None,
                            target_type="instructor",
                            target_key=prof_name,
                            target_title=prof_name,
                            target_subtitle=subtitle,
                            rating=int_rating,
                            comment=comment_text,
                            is_anonymous=True,
                            remote_review_id=rid,
                            remote_professor_id=pid,
                            course_name=course_text,
                            would_take_again=wta,
                            created_at=created_dt,
                            updated_at=created_dt,
                        )
                        session.add(new_fb)
                        fb_by_remote_id[rid] = new_fb

                await session.commit()

            _last_sync_status.update(
                {
                    "last_sync_at": now_utc.isoformat(),
                    "status": "ok",
                    "professors_synced": len(prof_by_id),
                    "reviews_synced": len(reviews),
                    "votes_synced": len(filtered_votes),
                    "requests_synced": len(requests),
                    "local_pushed": pushed_count,
                    "error": None,
                }
            )
            logger.info(
                "jenzareviews sync complete: professors=%d, reviews=%d, votes=%d, requests=%d, local_pushed=%d",
                len(prof_by_id),
                len(reviews),
                len(filtered_votes),
                len(requests),
                pushed_count,
            )
        except Exception as exc:
            _last_sync_status["status"] = "error"
            _last_sync_status["error"] = str(exc)
            logger.error("jenzareviews background sync error: %s", exc, exc_info=True)

        return get_jenzareviews_sync_status()


async def jenzareviews_sync_worker() -> None:
    """Background task that runs `sync_jenzareviews_from_remote()` on startup and every 5 minutes."""
    interval = max(60, int(getattr(settings, "JENZAREVIEWS_SYNC_INTERVAL_SECONDS", 300) or 300))
    logger.info("Starting jenzareviews periodic background sync worker (interval=%ds).", interval)
    while True:
        try:
            await sync_jenzareviews_from_remote()
        except asyncio.CancelledError:
            logger.info("jenzareviews background sync worker cancelled.")
            raise
        except Exception as exc:
            logger.error("Unhandled error in jenzareviews_sync_worker: %s", exc, exc_info=True)
        await asyncio.sleep(interval)
