"""
Noise Complaints — data layer and formatters.

Queries Austin Open311 API live across noise and quality-of-life service codes.
Provides hotspot (by street), complaint type stats, and response time analysis.
"""

import time
import logging
import requests
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

OPEN311_BASE_URL = "https://311.austintexas.gov/open311/v2"
TIMEOUT = 10
MAX_RETRIES = 3
RETRY_DELAY = 1.0

SERVICE_CODES = {
    "APDNONNO": "Non-Emergency Noise Complaint",
    "DSOUCVMC": "Outdoor Venue / Music Complaint",
    "AFDFIREW": "Fireworks Complaint",
}

RETRYABLE_ERRORS = (
    requests.exceptions.Timeout,
    requests.exceptions.ConnectionError,
)

_session: Optional[requests.Session] = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({
            "Accept": "application/json",
            "User-Agent": "austin311bot/0.1 (Open311 noise queries)",
        })
    return _session


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _isoformat_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _make_request(params: dict, retries: int = 0) -> list:
    session = _get_session()
    url = f"{OPEN311_BASE_URL}/requests.json"
    try:
        resp = session.get(url, params=params, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []
    except RETRYABLE_ERRORS as e:
        if retries < MAX_RETRIES:
            delay = RETRY_DELAY * (2 ** retries)
            logger.warning(f"Request failed ({e}), retrying in {delay:.1f}s ({retries+1}/{MAX_RETRIES})")
            time.sleep(delay)
            return _make_request(params, retries + 1)
        raise


def _fetch_code(service_code: str, days_back: int, limit: int = 100) -> list:
    end = _utc_now()
    start = end - timedelta(days=days_back)
    params = {
        "service_code": service_code,
        "start_date": _isoformat_z(start),
        "end_date": _isoformat_z(end),
        "per_page": limit,
        "page": 1,
    }
    records = _make_request(params)
    for r in records:
        r["_service_label"] = SERVICE_CODES.get(service_code, service_code)
    return records


def fetch_all_noise_complaints(days_back: int = 90, limit_per_code: int = 100) -> list:
    all_records = []
    for code in SERVICE_CODES:
        try:
            records = _fetch_code(code, days_back, limit_per_code)
            all_records.extend(records)
            logger.debug(f"{code}: {len(records)} records")
        except Exception as e:
            logger.warning(f"Failed to fetch {code}: {e}")
    return all_records


# =============================================================================
# HOTSPOTS BY STREET
# =============================================================================

def _extract_street(address: str) -> str:
    addr = address.replace(", Austin", "").strip()
    parts = addr.split(" ", 1)
    if len(parts) == 2 and parts[0].isdigit():
        return parts[1].strip()
    return addr


def get_hotspots(days_back: int = 90) -> dict:
    records = fetch_all_noise_complaints(days_back)
    if not records:
        return {"hotspots": [], "total": 0, "days_back": days_back}

    street_counts: dict = {}
    street_types: dict = {}

    for r in records:
        address = (r.get("address") or "").strip()
        street = _extract_street(address) if address else "Unknown"
        label = r.get("_service_label", "Unknown")

        street_counts[street] = street_counts.get(street, 0) + 1
        street_types.setdefault(street, {})
        street_types[street][label] = street_types[street].get(label, 0) + 1

    hotspots = sorted(street_counts.items(), key=lambda x: -x[1])
    return {
        "hotspots": hotspots,
        "street_types": street_types,
        "total": len(records),
        "days_back": days_back,
    }


def format_hotspots(data: dict) -> str:
    hotspots = data.get("hotspots", [])
    street_types = data.get("street_types", {})
    total = data.get("total", 0)
    days_back = data.get("days_back", 90)

    if not hotspots:
        return "📝 No noise complaints found."

    msg = f"🔊 *Top Noise Complaint Streets*\n"
    msg += f"_Last {days_back} days · {total} total complaints_\n\n"

    top = hotspots[:10]
    max_count = top[0][1]

    for i, (street, count) in enumerate(top, 1):
        bar = "█" * min(10, round(count / max_count * 10))
        msg += f"{i}. *{street}*\n"
        msg += f"   {bar} {count} complaint{'s' if count > 1 else ''}\n"
        types = street_types.get(street, {})
        top_types = sorted(types.items(), key=lambda x: -x[1])[:2]
        if top_types:
            type_str = " · ".join(f"{t} ({c})" for t, c in top_types)
            msg += f"   _{type_str}_\n"
        msg += "\n"

    return msg


# =============================================================================
# PEAK TIMES & WEEKLY TREND
# =============================================================================

# Austin local time approximation (CDT = UTC-5, CST = UTC-6; we use -6 as default)
_AUSTIN_OFFSET = timedelta(hours=-6)

_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_DAY_SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _fmt_hour(h: int) -> str:
    if h == 0:
        return "12am"
    if h < 12:
        return f"{h}am"
    if h == 12:
        return "12pm"
    return f"{h - 12}pm"


def get_peak_times(days_back: int = 56) -> dict:
    records = fetch_all_noise_complaints(days_back, limit_per_code=100)
    if not records:
        return {"total": 0, "days_back": days_back}

    now = _utc_now()
    # day_hour[weekday][hour] = count
    day_hour: list = [[0] * 24 for _ in range(7)]
    weekly: list = [0] * 8  # index 7 = most recent week, 0 = oldest

    for r in records:
        dt_str = r.get("requested_datetime") or ""
        if not dt_str:
            continue
        try:
            dt_utc = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            dt_local = dt_utc + _AUSTIN_OFFSET
            day_hour[dt_local.weekday()][dt_local.hour] += 1

            days_ago = (now - dt_utc).days
            week_idx = min(7, days_ago // 7)
            weekly[7 - week_idx] += 1
        except (ValueError, TypeError):
            pass

    # Overall peak (day + hour)
    peak_day = peak_hour = peak_count = 0
    for d in range(7):
        for h in range(24):
            if day_hour[d][h] > peak_count:
                peak_count = day_hour[d][h]
                peak_day, peak_hour = d, h

    # Per-day peak hour
    day_peaks = [
        (_DAYS[d], _DAY_SHORT[d], day_hour[d].index(max(day_hour[d])), max(day_hour[d]))
        for d in range(7)
    ]

    # Week-start labels (Mon of each week, oldest first)
    week_labels = []
    for i in range(8):
        week_start = now - timedelta(days=(7 - i) * 7)
        week_labels.append(week_start.strftime("%-m/%-d"))

    return {
        "total": sum(weekly),
        "days_back": days_back,
        "peak_day": _DAYS[peak_day],
        "peak_hour": peak_hour,
        "peak_count": peak_count,
        "day_peaks": day_peaks,
        "weekly": weekly,
        "week_labels": week_labels,
    }


def format_peak_times(data: dict) -> str:
    if not data.get("total"):
        return "📝 Not enough data to analyze peak times."

    peak_day = data["peak_day"]
    peak_hour = data["peak_hour"]
    peak_count = data["peak_count"]
    day_peaks = data["day_peaks"]
    weekly = data["weekly"]
    week_labels = data["week_labels"]
    days_back = data["days_back"]

    # Headline
    msg = f"🕐 *Noise Complaints — When They Happen*\n"
    msg += f"_Last {days_back} days · {data['total']} total complaints_\n\n"
    msg += f"📍 *Peak:* {peak_day}s around *{_fmt_hour(peak_hour)}* ({peak_count} complaints)\n\n"

    # Peak hour per day of week
    msg += "*Peak hour by day:*\n"
    for _, short, peak_h, count in day_peaks:
        bar = "█" * min(8, round(count / max(1, peak_count) * 8))
        msg += f"  `{short}` {_fmt_hour(peak_h):>5}  {bar} {count}\n"

    msg += "\n"

    # 8-week trend
    msg += "*Weekly volume — last 8 weeks:*\n"
    max_week = max(weekly) if weekly else 1
    for i, (label, count) in enumerate(zip(week_labels, weekly)):
        bar = "█" * min(10, round(count / max(1, max_week) * 10))
        recency = " ◀ this wk" if i == 7 else ""
        msg += f"  `{label}` {bar} {count}{recency}\n"

    return msg

