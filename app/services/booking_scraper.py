"""AUA Room Booking Scraper (https://booking.aua.am/) for AUA Hub (<Free Rooms> module).

Authenticates using BOOKING_AUA_USER and BOOKING_AUA_PASS from .env (never exposed),
fetches the current day's schedule for both Main Building (area=1) and PAB (area=2),
parses #table_container (08:00 to 22:00 in 10-minute steps), merges contiguous free
cells, and filters out 10-minute passing gaps between classes (showing only blocks > 10 minutes, i.e., >= 20m).
"""

import asyncio
from datetime import datetime
import html
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

BOOKING_BASE_URL = "https://booking.aua.am/index.php"
YEREVAN_TZ = ZoneInfo("Asia/Yerevan")

AREAS = {
    "main": {"id": 1, "name": "Main Building", "short": "MB"},
    "pab": {"id": 2, "name": "Paramaz Avedisian Building (PAB)", "short": "PAB"},
}

# Cache: key = "YYYY-MM-DD" -> (timestamp, payload_dict)
_BOOKING_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_BOOKING_LOCK = asyncio.Lock()
_ACTIVE_REFRESH_TASKS: Dict[str, asyncio.Task] = {}
CACHE_TTL_SECONDS = 900  # 15 minutes fresh window (reduces booking.aua.am traffic by 75%)
STALE_TTL_SECONDS = 7200  # 2 hours stale-while-revalidate window


def _minutes_to_hhmm(total_minutes: int) -> str:
    h = total_minutes // 60
    m = total_minutes % 60
    return f"{h:02d}:{m:02d}"


def _format_duration(minutes: int) -> str:
    h = minutes // 60
    m = minutes % 60
    if h > 0 and m > 0:
        return f"{h}h {m}m"
    if h > 0:
        return f"{h}h"
    return f"{m}m"


def merge_and_filter_free_slots(
    slot_starts_minutes: List[int],
    now_minutes: Optional[int] = None,
) -> Tuple[List[Dict[str, Any]], bool, Optional[str]]:
    """Merge contiguous 10-minute free slots and discard 10-minute passing gaps (duration <= 10m).

    Args:
        slot_starts_minutes: List of start times in minutes from midnight (e.g., 480 for 08:00).
        now_minutes: Current time in minutes from midnight (if checking today's schedule).

    Returns:
        (windows, is_free_now, free_until_hhmm)
    """
    if not slot_starts_minutes:
        return [], False, None

    sorted_starts = sorted(set(s for s in slot_starts_minutes if 480 <= s < 1320))
    if not sorted_starts:
        return [], False, None

    merged: List[Tuple[int, int]] = []
    cur_start = sorted_starts[0]
    cur_end = cur_start + 10

    for s in sorted_starts[1:]:
        if s == cur_end:
            cur_end = s + 10
        else:
            merged.append((cur_start, cur_end))
            cur_start = s
            cur_end = s + 10
    merged.append((cur_start, cur_end))

    windows: List[Dict[str, Any]] = []
    is_free_now = False
    free_until: Optional[str] = None

    for start_m, end_m in merged:
        duration = end_m - start_m
        # Strictly ignore 10-minute gaps between classes! Only keep blocks > 10 minutes (>= 20m).
        if duration <= 10:
            continue

        start_str = _minutes_to_hhmm(start_m)
        end_str = _minutes_to_hhmm(end_m)
        active_now = bool(now_minutes is not None and start_m <= now_minutes < end_m)
        if active_now:
            is_free_now = True
            free_until = end_str

        windows.append(
            {
                "start": start_str,
                "end": end_str,
                "start_minutes": start_m,
                "end_minutes": end_m,
                "duration_minutes": duration,
                "duration_label": _format_duration(duration),
                "label": f"{start_str} – {end_str}",
                "is_now": active_now,
            }
        )

    return windows, is_free_now, free_until


def parse_booking_day_html(
    page_html: str,
    area_key: str,
    now_minutes: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Parse MRBS #table_container HTML into rooms with their >10m free windows."""
    # 1. Extract room headers from the first <thead> tr
    thead_match = re.search(r"<thead[^>]*>(.*?)</thead>", page_html, re.DOTALL | re.IGNORECASE)
    header_source = thead_match.group(1) if thead_match else page_html

    room_headers = re.findall(
        r'<th[^>]*data-room=["\'](\d+)["\'][^>]*>\s*<a[^>]*>(.*?)</a>\s*</th>',
        header_source,
        re.DOTALL | re.IGNORECASE,
    )

    rooms_ordered: List[Dict[str, Any]] = []
    seen_room_ids = set()

    for room_id_str, inner_html in room_headers:
        if room_id_str in seen_room_ids:
            continue
        seen_room_ids.add(room_id_str)

        cap_match = re.search(
            r'<span[^>]*class=["\']capacity["\'][^>]*>(.*?)</span>',
            inner_html,
            re.DOTALL | re.IGNORECASE,
        )
        capacity = int(cap_match.group(1).strip()) if (cap_match and cap_match.group(1).strip().isdigit()) else None

        name_raw = re.sub(r"<span[^>]*>.*?</span>", "", inner_html, flags=re.DOTALL | re.IGNORECASE)
        name_clean = re.sub(r"<[^>]+>", "", name_raw)
        name_clean = html.unescape(name_clean).strip().rstrip(".")

        rooms_ordered.append(
            {
                "room_id": room_id_str,
                "room_name": name_clean or f"Room {room_id_str}",
                "capacity": capacity,
                "building_key": area_key,
                "building_name": AREAS.get(area_key, {}).get("name", area_key.upper()),
                "building_short": AREAS.get(area_key, {}).get("short", area_key.upper()),
            }
        )

    # 2. Extract all unbooked 10-minute slots from <td class="new"> links inside #table_container
    slots_by_room: Dict[str, List[int]] = {r["room_id"]: [] for r in rooms_ordered}

    # Match edit_entry.php links inside td.new cells
    slot_matches = re.findall(
        r'<td[^>]*class=["\'][^"\']*\bnew\b[^"\']*["\'][^>]*>.*?href=["\']edit_entry\.php\?([^"\']+)["\']',
        page_html,
        re.DOTALL | re.IGNORECASE,
    )

    for query_raw in slot_matches:
        query_str = html.unescape(query_raw)
        r_m = re.search(r"(?:^|&)room=(\d+)", query_str)
        h_m = re.search(r"(?:^|&)hour=(\d+)", query_str)
        m_m = re.search(r"(?:^|&)minute=(\d+)", query_str)
        if r_m and h_m and m_m:
            r_id = r_m.group(1)
            hour = int(h_m.group(1))
            minute = int(m_m.group(1))
            total_min = hour * 60 + minute
            if r_id in slots_by_room:
                slots_by_room[r_id].append(total_min)

    # 3. Build room availability payloads
    results: List[Dict[str, Any]] = []
    for room in rooms_ordered:
        r_id = room["room_id"]
        windows, is_free_now, free_until = merge_and_filter_free_slots(
            slots_by_room.get(r_id, []),
            now_minutes=now_minutes,
        )
        total_free_mins = sum(w["duration_minutes"] for w in windows)
        results.append(
            {
                **room,
                "is_free_now": is_free_now,
                "free_until": free_until,
                "free_windows": windows,
                "total_free_minutes": total_free_mins,
                "total_free_label": _format_duration(total_free_mins) if total_free_mins > 0 else "0m",
            }
        )

    return results


async def _fetch_live_booking_data(target_date: datetime) -> Dict[str, Any]:
    """Log into booking.aua.am with .env credentials and scrape Main Building & PAB for target_date."""
    username = settings.BOOKING_AUA_USER
    password = settings.BOOKING_AUA_PASS
    if not username or not password:
        raise RuntimeError("Booking credentials are not configured in .env")

    now_yerevan = datetime.now(YEREVAN_TZ)
    is_today = (
        target_date.year == now_yerevan.year
        and target_date.month == now_yerevan.month
        and target_date.day == now_yerevan.day
    )
    now_minutes = (now_yerevan.hour * 60 + now_yerevan.minute) if is_today else None

    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AUAHubRoomFinder/1.0",
        "Referer": BOOKING_BASE_URL,
    }

    async with httpx.AsyncClient(timeout=25.0, follow_redirects=True, headers=headers) as client:
        # Step 1: Fetch index.php to acquire session cookie and csrf_token
        init_resp = await client.get(BOOKING_BASE_URL)
        init_resp.raise_for_status()
        csrf_match = re.search(
            r'name=["\']csrf_token["\']\s+value=["\']([^"\']+)["\']',
            init_resp.text,
            re.IGNORECASE,
        )
        csrf_token = csrf_match.group(1) if csrf_match else ""

        # Step 2: Authenticate via POST
        login_data = {
            "csrf_token": csrf_token,
            "returl": BOOKING_BASE_URL,
            "target_url": "index.php",
            "action": "SetName",
            "username": username,
            "password": password,
        }
        login_resp = await client.post(BOOKING_BASE_URL, data=login_data)
        login_resp.raise_for_status()

        # Step 3: Fetch both Main Building (area=1) and PAB (area=2) for the requested date
        buildings_data: Dict[str, List[Dict[str, Any]]] = {}
        for area_key, area_meta in AREAS.items():
            params = {
                "view": "day",
                "year": target_date.year,
                "month": target_date.month,
                "day": target_date.day,
                "area": area_meta["id"],
            }
            area_resp = await client.get(BOOKING_BASE_URL, params=params)
            area_resp.raise_for_status()
            buildings_data[area_key] = parse_booking_day_html(
                area_resp.text,
                area_key=area_key,
                now_minutes=now_minutes,
            )

    all_rooms = buildings_data.get("main", []) + buildings_data.get("pab", [])
    return {
        "date": target_date.strftime("%Y-%m-%d"),
        "date_display": target_date.strftime("%A, %b %d, %Y"),
        "current_time_yerevan": now_yerevan.strftime("%H:%M"),
        "is_today": is_today,
        "buildings": {
            "main": {
                "key": "main",
                "id": 1,
                "name": "Main Building",
                "short": "Main",
                "rooms": buildings_data.get("main", []),
            },
            "pab": {
                "key": "pab",
                "id": 2,
                "name": "Paramaz Avedisian Building (PAB)",
                "short": "PAB",
                "rooms": buildings_data.get("pab", []),
            },
        },
        "summary": {
            "total_rooms": len(all_rooms),
            "free_now_count": sum(1 for r in all_rooms if r["is_free_now"]),
            "main_free_now": sum(1 for r in buildings_data.get("main", []) if r["is_free_now"]),
            "pab_free_now": sum(1 for r in buildings_data.get("pab", []) if r["is_free_now"]),
        },
    }


async def _background_refresh_booking_data(target_date: datetime, cache_key: str) -> None:
    """Asynchronously refresh the schedule in background without blocking the client."""
    try:
        async with _BOOKING_LOCK:
            payload = await _fetch_live_booking_data(target_date)
            _BOOKING_CACHE[cache_key] = (time.time(), payload)
            logger.info("Background refresh of Free Rooms schedule completed for %s", cache_key)
    except Exception as e:
        logger.warning("Background refresh of Free Rooms schedule failed for %s: %s", cache_key, e)
    finally:
        _ACTIVE_REFRESH_TASKS.pop(cache_key, None)


async def get_free_rooms_schedule(
    date_str: Optional[str] = None,
    force_refresh: bool = False,
) -> Dict[str, Any]:
    """Return cached or freshly scraped Free Rooms schedule for Main Building and PAB.

    Implements Stale-While-Revalidate:
    - If cache is fresh (< 15 min): returns instantly (< 0.1ms).
    - If cache is stale (< 2 hours): returns stale data instantly (< 0.1ms) and triggers background refresh.
    - If cold or force_refresh: scrapes live from booking.aua.am under mutex lock.
    """
    now_yerevan = datetime.now(YEREVAN_TZ)
    if date_str:
        try:
            parsed_dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=YEREVAN_TZ)
        except ValueError:
            parsed_dt = now_yerevan
    else:
        parsed_dt = now_yerevan

    cache_key = parsed_dt.strftime("%Y-%m-%d")
    now_ts = time.time()

    # 0. Anti-flood guard: if forced refresh was requested, but was already scraped within last 25s, reuse cache
    if force_refresh and cache_key in _BOOKING_CACHE:
        cached_ts, cached_payload = _BOOKING_CACHE[cache_key]
        if now_ts - cached_ts < 25:
            return cached_payload

    # 1. Fast path: check in-memory cache
    if not force_refresh and cache_key in _BOOKING_CACHE:
        cached_ts, cached_payload = _BOOKING_CACHE[cache_key]
        age = now_ts - cached_ts

        # Fully fresh: return instantly
        if age < CACHE_TTL_SECONDS:
            return cached_payload

        # Stale-while-revalidate: return stale data immediately, refresh silently in background
        if age < STALE_TTL_SECONDS:
            if cache_key not in _ACTIVE_REFRESH_TASKS or _ACTIVE_REFRESH_TASKS[cache_key].done():
                _ACTIVE_REFRESH_TASKS[cache_key] = asyncio.create_task(
                    _background_refresh_booking_data(parsed_dt, cache_key)
                )
            return cached_payload

    # 2. Cold fetch under lock (only happens when no cache exists or cache is > 2 hours old)
    async with _BOOKING_LOCK:
        if not force_refresh and cache_key in _BOOKING_CACHE:
            cached_ts, cached_payload = _BOOKING_CACHE[cache_key]
            if time.time() - cached_ts < CACHE_TTL_SECONDS:
                return cached_payload

        payload = await _fetch_live_booking_data(parsed_dt)
        _BOOKING_CACHE[cache_key] = (time.time(), payload)
        return payload


async def free_rooms_prewarm_worker() -> None:
    """Background worker that keeps Free Rooms schedule pre-warmed during campus hours (07:30 to 21:30 Yerevan time)."""
    # Wait 8 seconds after server startup before running the initial warm-up
    await asyncio.sleep(8)
    logger.info("Free Rooms pre-warm worker started.")

    while True:
        try:
            now_yerevan = datetime.now(YEREVAN_TZ)
            hour = now_yerevan.hour
            # Warm up during active university hours (07:00 to 22:00)
            if 7 <= hour <= 21:
                logger.info("Pre-warming Free Rooms schedule for %s...", now_yerevan.strftime("%Y-%m-%d"))
                await get_free_rooms_schedule(force_refresh=True)
            # Sleep 15 minutes before checking again
            await asyncio.sleep(900)
        except asyncio.CancelledError:
            logger.info("Free Rooms pre-warm worker received cancellation.")
            break
        except Exception as e:
            logger.warning("Free Rooms pre-warm worker error: %s", e)
            await asyncio.sleep(60)
