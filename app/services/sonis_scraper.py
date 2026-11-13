"""Jenzabar Sonis Course Scraper for AUA Hub (<Have-Need> module).

Fetches course schedules from https://auasonis.jenzabarcloud.com/GENSRsC.cfm
and implements the Last-Month Semester Switch Rule:
- Keeps the current/upcoming semester during the semester.
- Automatically switches to the next semester on the last month of the semester
  (e.g., December switches Fall -> Spring; May switches Spring -> Fall).
"""

from datetime import datetime, timezone
import html
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Course

logger = logging.getLogger(__name__)

SONIS_URL = "https://auasonis.jenzabarcloud.com/GENSRsC.cfm"
YEREVAN_TZ = ZoneInfo("Asia/Yerevan")


def get_semester_candidates(now: Optional[datetime] = None) -> List[Tuple[str, str, str]]:
    """Return ordered list of (chkschyear, chkschsem, semester_label) candidates.

    Implements the Last-Month Semester Switch Rule:
    - Fall semester (Sep, Oct, Nov): stays on Fall YYYY.
    - December (last month of Fall): switches to upcoming Spring YYYY+1 (fallback: Fall YYYY).
    - Spring semester (Jan, Feb, Mar, Apr): stays on Spring YYYY.
    - May (last month of Spring): switches to upcoming Fall YYYY (fallback: Spring YYYY).
    - Summer / Pre-Fall (Jun, Jul, Aug): targets upcoming Fall YYYY (fallback: Summer YYYY).
    """
    if now is None:
        now = datetime.now(YEREVAN_TZ)
    y = now.year
    m = now.month

    def acad_code(start_year: int) -> str:
        return f"{start_year}{str(start_year + 1)[-2:]}"

    if m in (9, 10, 11):
        # Fall in progress
        return [
            (acad_code(y), "1 ", f"Fall {y}"),
            (acad_code(y), "3 ", f"Spring {y + 1}"),
        ]
    elif m == 12:
        # Last month of Fall -> switch to upcoming Spring!
        return [
            (acad_code(y), "3 ", f"Spring {y + 1}"),
            (acad_code(y), "1 ", f"Fall {y}"),
        ]
    elif m in (1, 2, 3, 4):
        # Spring in progress
        return [
            (acad_code(y - 1), "3 ", f"Spring {y}"),
            (acad_code(y), "1 ", f"Fall {y}"),
            (acad_code(y - 1), "1 ", f"Fall {y - 1}"),
        ]
    elif m == 5:
        # Last month of Spring -> switch to upcoming Fall!
        return [
            (acad_code(y), "1 ", f"Fall {y}"),
            (acad_code(y - 1), "4 ", f"Summer {y}"),
            (acad_code(y - 1), "3 ", f"Spring {y}"),
        ]
    else:
        # June, July, August -> upcoming Fall
        return [
            (acad_code(y), "1 ", f"Fall {y}"),
            (acad_code(y - 1), "4 ", f"Summer {y}"),
            (acad_code(y - 1), "3 ", f"Spring {y}"),
        ]


def _clean_cell_html(raw_html: str, preserve_breaks: bool = False) -> str:
    """Strip HTML tags and normalize whitespace inside a table cell."""
    if not raw_html:
        return ""
    text = raw_html
    if preserve_breaks:
        text = re.sub(r"<br\s*/?>", " | ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if preserve_breaks:
        text = re.sub(r"(\s*\|\s*)+", " | ", text).strip(" |")
    return text


def parse_sonis_courses_html(page_html: str) -> List[Dict[str, str]]:
    """Parse <table id="crsbysemester"> from Jenzabar Sonis HTML into structured dicts."""
    table_match = re.search(
        r'<table[^>]*id=["\']crsbysemester["\'][^>]*>(.*?)</table>',
        page_html,
        re.DOTALL | re.IGNORECASE,
    )
    if not table_match:
        return []

    table_body = table_match.group(1)
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table_body, re.DOTALL | re.IGNORECASE)

    parsed_courses: List[Dict[str, str]] = []
    seen_keys = set()

    for row_html in rows:
        cols = re.findall(r"<td[^>]*>(.*?)</td>", row_html, re.DOTALL | re.IGNORECASE)
        if len(cols) < 8:
            continue

        col0 = cols[0]
        title_match = re.search(
            r'<span[^>]*class=["\']title["\'][^>]*>(.*?)</span>',
            col0,
            re.DOTALL | re.IGNORECASE,
        )
        code_match = re.search(
            r'<span[^>]*class=["\']crsbysemester_course_id["\'][^>]*>(.*?)</span>',
            col0,
            re.DOTALL | re.IGNORECASE,
        )

        course_name = _clean_cell_html(title_match.group(1)) if title_match else _clean_cell_html(col0)
        raw_code = _clean_cell_html(code_match.group(1)) if code_match else ""
        course_code = raw_code.strip("() ").strip()

        if not course_name and not course_code:
            continue
        if not course_code:
            course_code = course_name[:32]

        section = _clean_cell_html(cols[1]) if len(cols) > 1 else "A"
        if not section:
            section = "A"
        credits = _clean_cell_html(cols[3]) if len(cols) > 3 else ""
        instructor = _clean_cell_html(cols[5]) if len(cols) > 5 else "TBA"
        times = _clean_cell_html(cols[6], preserve_breaks=True) if len(cols) > 6 else "TBA"
        location = _clean_cell_html(cols[11], preserve_breaks=True) if len(cols) > 11 else ""

        if not instructor:
            instructor = "TBA"
        if not times:
            times = "TBA"

        dedup_key = (course_code.upper(), section.upper())
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)

        parsed_courses.append(
            {
                "course_code": course_code,
                "course_name": course_name,
                "section": section,
                "credits": credits,
                "instructor": instructor,
                "times": times,
                "location": location,
            }
        )

    return parsed_courses


async def fetch_sonis_semester_courses(
    chkschyear: str,
    chkschsem: str,
) -> List[Dict[str, str]]:
    """Perform HTTP POST to Sonis GENSRsC.cfm and return parsed courses."""
    form_data = {
        "chkschyear": chkschyear,
        "chkschsem": chkschsem,
        "search": "",
        "Submit": "Search",
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AUAHubCourseSync/1.0",
        "Referer": SONIS_URL,
    }
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        resp = await client.post(SONIS_URL, data=form_data, headers=headers)
        resp.raise_for_status()
        return parse_sonis_courses_html(resp.text)


async def sync_courses_from_sonis(
    session: AsyncSession,
    now: Optional[datetime] = None,
) -> Tuple[str, int]:
    """Fetch courses from Jenzabar Sonis for the active/upcoming semester and upsert into PostgreSQL.

    Returns (semester_label, upserted_count).
    """
    candidates = get_semester_candidates(now=now)
    chosen_year_code = candidates[0][0]
    chosen_sem_code = candidates[0][1].strip()
    chosen_label = candidates[0][2]
    scraped_rows: List[Dict[str, str]] = []

    for chkschyear, chkschsem, sem_label in candidates:
        try:
            rows = await fetch_sonis_semester_courses(chkschyear, chkschsem)
            if len(rows) >= 10:
                chosen_year_code = chkschyear
                chosen_sem_code = chkschsem.strip()
                chosen_label = sem_label
                scraped_rows = rows
                logger.info(
                    "Sonis scraper fetched %d courses for %s (%s_%s).",
                    len(rows),
                    sem_label,
                    chosen_year_code,
                    chosen_sem_code,
                )
                break
            else:
                logger.info(
                    "Sonis candidate %s (%s) returned only %d courses; trying next fallback.",
                    sem_label,
                    chkschyear,
                    len(rows),
                )
        except Exception as e:
            logger.warning("Sonis scrape failed for %s (%s): %s", sem_label, chkschyear, e)

    if not scraped_rows:
        # Check if we already have courses in DB
        existing_res = await session.execute(select(Course).limit(1))
        first_existing = existing_res.scalar_one_or_none()
        if first_existing:
            return first_existing.semester_label, 0
        return chosen_label, 0

    # Upsert courses for chosen semester so existing Course IDs referenced by HaveNeedPost stay intact
    existing_stmt = select(Course).where(
        Course.school_year_code == chosen_year_code,
        Course.semester_code == chosen_sem_code,
    )
    existing_result = await session.execute(existing_stmt)
    existing_map: Dict[Tuple[str, str], Course] = {
        (c.course_code.upper(), c.section.upper()): c
        for c in existing_result.scalars().all()
    }

    now_utc = datetime.now(timezone.utc)
    count = 0
    for item in scraped_rows:
        key = (item["course_code"].upper(), item["section"].upper())
        existing_course = existing_map.get(key)
        if existing_course:
            existing_course.course_title = item["course_name"]
            existing_course.instructor = item["instructor"]
            existing_course.schedule_times = item["times"]
            existing_course.credits = item.get("credits")
            existing_course.location = item.get("location")
            existing_course.semester_label = chosen_label
            existing_course.updated_at = now_utc
        else:
            new_course = Course(
                semester_label=chosen_label,
                school_year_code=chosen_year_code,
                semester_code=chosen_sem_code,
                course_code=item["course_code"],
                course_title=item["course_name"],
                section=item["section"],
                credits=item.get("credits"),
                instructor=item["instructor"],
                schedule_times=item["times"],
                location=item.get("location"),
                updated_at=now_utc,
            )
            session.add(new_course)
        count += 1

    await session.commit()
    return chosen_label, count
