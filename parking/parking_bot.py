"""
Parking Enforcement — data layer and formatters.

Queries Austin Open311 API live for PARKINGV (Parking Violation Enforcement) service requests.
"""

import time
import logging
import os
import requests
from datetime import datetime, timezone, timedelta
from typing import Optional
from collections import defaultdict

logger = logging.getLogger(__name__)

OPEN311_BASE_URL = "https://311.austintexas.gov/open311/v2"
SERVICE_CODE = "PARKINGV"
TIMEOUT = 10
MAX_RETRIES = 3
RETRY_DELAY = 1.0
MAX_PAGES = 20  # cap at 2,000 records — enough for all stats/hotspot analysis

# API key from environment
API_KEY = os.getenv("AUSTIN_APP_TOKEN")

# Austin local time approximation (CDT = UTC-5, CST = UTC-6; use -6 as conservative default)
_AUSTIN_OFFSET = timedelta(hours=-6)

RETRYABLE_HTTP_CODES = {429, 500, 502, 503, 504}

RETRYABLE_ERRORS = (
    requests.exceptions.Timeout,
    requests.exceptions.ConnectionError,
)

_session: Optional[requests.Session] = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        headers = {
            "Accept": "application/json",
            "User-Agent": "austin311bot/0.1 (Open311 parking queries)",
        }
        if API_KEY:
            headers["X-Api-Key"] = API_KEY
        _session.headers.update(headers)
    return _session


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _isoformat_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _extract_street(address: str) -> str:
    """Extract street name from '1234 Some St, Austin' → 'Some St'."""
    addr = address.replace(", Austin", "").strip()
    parts = addr.split(" ", 1)
    if len(parts) == 2 and parts[0].isdigit():
        return parts[1].strip()
    return addr


def _fmt_hour(h: int) -> str:
    if h == 0:
        return "12am"
    if h < 12:
        return f"{h}am"
    if h == 12:
        return "12pm"
    return f"{h - 12}pm"


def _make_request(params: dict, retries: int = 0) -> list:
    session = _get_session()
    url = f"{OPEN311_BASE_URL}/requests.json"
    try:
        resp = session.get(url, params=params, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []
    except requests.exceptions.HTTPError as e:
        if e.response.status_code in RETRYABLE_HTTP_CODES and retries < MAX_RETRIES:
            delay = RETRY_DELAY * (2 ** retries)
            logger.warning(f"HTTP {e.response.status_code}, retrying in {delay:.1f}s ({retries+1}/{MAX_RETRIES})")
            time.sleep(delay)
            return _make_request(params, retries + 1)
        raise
    except RETRYABLE_ERRORS as e:
        if retries < MAX_RETRIES:
            delay = RETRY_DELAY * (2 ** retries)
            logger.warning(f"Request failed ({e}), retrying in {delay:.1f}s ({retries+1}/{MAX_RETRIES})")
            time.sleep(delay)
            return _make_request(params, retries + 1)
        raise


def get_all_citations(days_back: int = 90) -> list:
    """Fetch all parking citations with pagination."""
    end = _utc_now()
    start = end - timedelta(days=days_back)

    all_records = []
    page = 1
    seen_ids = set()

    while True:
        params = {
            "service_code": SERVICE_CODE,
            "start_date": _isoformat_z(start),
            "end_date": _isoformat_z(end),
            "per_page": 100,
            "page": page,
        }

        records = _make_request(params)
        if not records:
            break

        new_records = [
            r for r in records
            if (sid := r.get("service_request_id")) and sid not in seen_ids and not seen_ids.add(sid)
        ]

        if not new_records:
            break

        all_records.extend(new_records)

        if len(records) < 100:
            break

        page += 1

        if page > MAX_PAGES:
            logger.warning(f"Reached MAX_PAGES ({MAX_PAGES}), stopping pagination early")
            break

        # Rate-limit regardless of API key; shorter delay when authenticated
        time.sleep(0.5 if API_KEY else 1.0)

    return all_records


def get_stats(days_back: int = 90) -> dict:
    """Return meaningful statistics for parking citations."""
    citations = get_all_citations(days_back=days_back)
    if not citations:
        return {"total": 0, "days_back": days_back}

    now = _utc_now()
    resolution_days = []
    open_tickets = []
    street_counts: dict = {}
    hourly_counts: dict = defaultdict(int)


    for r in citations:
        status = (r.get("status") or "").lower()
        requested_str = r.get("requested_datetime") or ""
        updated_str = r.get("updated_datetime") or ""

        if status == "closed" and requested_str and updated_str:
            try:
                req = datetime.fromisoformat(requested_str.replace("Z", "+00:00"))
                upd = datetime.fromisoformat(updated_str.replace("Z", "+00:00"))
                days = (upd - req).days
                if 0 <= days <= 365:
                    resolution_days.append(days)
            except ValueError:
                pass

        if status == "open":
            open_tickets.append(r)

        address = r.get("address") or ""
        if address:
            street = _extract_street(address)
            street_counts[street] = street_counts.get(street, 0) + 1

        if requested_str:
            try:
                req_utc = datetime.fromisoformat(requested_str.replace("Z", "+00:00"))
                req_local = req_utc + _AUSTIN_OFFSET
                hourly_counts[req_local.hour] += 1
            except ValueError:
                pass

    avg_resolution = round(sum(resolution_days) / len(resolution_days), 1) if resolution_days else None
    top_streets = sorted(street_counts.items(), key=lambda x: -x[1])[:5]
    peak_hour = max(hourly_counts.items(), key=lambda x: x[1])[0] if hourly_counts else None

    oldest_open = None
    if open_tickets:
        def req_date(r):
            try:
                return datetime.fromisoformat((r.get("requested_datetime") or "").replace("Z", "+00:00"))
            except ValueError:
                return now
        oldest = min(open_tickets, key=req_date)
        oldest_dt = req_date(oldest)
        oldest_open = {
            "id": oldest.get("service_request_id"),
            "address": oldest.get("address"),
            "days_ago": (now - oldest_dt).days,
        }

    return {
        "total": len(citations),
        "open": len(open_tickets),
        "closed": len(citations) - len(open_tickets),
        "avg_resolution_days": avg_resolution,
        "top_streets": top_streets,
        "peak_hour": peak_hour,
        "hourly_counts": dict(hourly_counts),
        "oldest_open": oldest_open,
        "days_back": days_back,
    }


def get_hotspots(days_back: int = 30) -> dict:
    """Return citation counts grouped by street for hot zone analysis.

    Uses a short window and page cap so the response stays fast — 300 records
    is more than enough to identify concentrated enforcement patterns.
    """
    end = _utc_now()
    start = end - timedelta(days=days_back)
    seen_ids: set = set()
    citations: list = []

    for page in range(1, 4):  # max 3 pages = 300 records, no sleep
        params = {
            "service_code": SERVICE_CODE,
            "start_date": _isoformat_z(start),
            "end_date": _isoformat_z(end),
            "per_page": 100,
            "page": page,
        }
        records = _make_request(params)
        if not records:
            break
        for r in records:
            sid = r.get("service_request_id")
            if sid and sid not in seen_ids:
                seen_ids.add(sid)
                citations.append(r)
        if len(records) < 100:
            break

    if not citations:
        return {"hotspots": [], "total": 0, "days_back": days_back}

    street_counts: dict = {}
    street_locations: dict = {}

    for r in citations:
        address = r.get("address") or ""
        lat = r.get("lat")
        lon = r.get("long")

        street = _extract_street(address) if address else "Unknown"
        street_counts[street] = street_counts.get(street, 0) + 1

        if street not in street_locations and lat and lon:
            street_locations[street] = (lat, lon)

    hotspots = sorted(street_counts.items(), key=lambda x: -x[1])

    return {
        "hotspots": hotspots,
        "locations": street_locations,
        "total": len(citations),
        "days_back": days_back,
    }


def format_stats(stats: dict) -> str:
    if stats.get("total", 0) == 0:
        return f"📝 No parking citations found in the past {stats.get('days_back', 90)} days."

    total = stats["total"]
    days_back = stats.get("days_back", 90)
    msg = f"🅿️ *Parking Enforcement — Last {days_back} Days*\n\n"

    msg += f"📊 *Total citations:* {total} ({stats['open']} open · {stats['closed']} closed)\n\n"

    if stats.get("avg_resolution_days") is not None:
        msg += f"⏱ *Avg resolution time:* {stats['avg_resolution_days']} days\n\n"

    peak = stats.get("peak_hour")
    if peak is not None:
        msg += f"🕐 *Peak reporting:* {_fmt_hour(peak)} (Austin local time)\n\n"

    top = stats.get("top_streets", [])
    if top:
        msg += "🔥 *Hot zones (top streets):*\n"
        for street, count in top:
            msg += f"   {street}: {count} citation{'s' if count > 1 else ''}\n"
        msg += "\n"

    oldest = stats.get("oldest_open")
    if oldest:
        msg += f"🕰 *Oldest open ticket:* #{oldest['id']}\n"
        msg += f"   {oldest['address']} — {oldest['days_ago']} days unresolved\n"

    msg += "\n_Source: [Austin Open311 API](https://311.austintexas.gov/open311/v2)_"
    return msg


def format_hotspots(data: dict) -> str:
    hotspots = data.get("hotspots", [])
    locations = data.get("locations", {})
    total = data.get("total", 0)
    days_back = data.get("days_back", 90)

    if not hotspots:
        return "📝 No parking enforcement data found."

    msg = f"🅿️ *Parking Enforcement Hot Zones*\n"
    msg += f"_Last {days_back} days · {total} citations sampled_\n\n"

    top = hotspots[:8]
    max_count = top[0][1]

    for i, (street, count) in enumerate(top, 1):
        bar = "█" * min(10, round(count / max_count * 10))
        msg += f"{i}. *{street}*\n"
        msg += f"   {bar} {count} citation{'s' if count > 1 else ''}\n"
        if street in locations:
            lat, lon = locations[street]
            msg += f"   📍 {float(lat):.4f}, {float(lon):.4f}\n"
        msg += "\n"

    msg += "_Source: [Austin Open311 API](https://311.austintexas.gov/open311/v2)_"
    return msg
