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
# STATS BY COMPLAINT TYPE
# =============================================================================

def get_stats(days_back: int = 90) -> dict:
    records = fetch_all_noise_complaints(days_back)
    if not records:
        return {"total": 0, "days_back": days_back}

    now = _utc_now()
    half = timedelta(days=days_back // 2)
    cutoff = now - half

    type_counts: dict = {}
    recent_total = 0
    older_total = 0

    for r in records:
        label = r.get("_service_label", "Unknown")
        type_counts[label] = type_counts.get(label, 0) + 1

        requested_str = r.get("requested_datetime") or ""
        try:
            req = datetime.fromisoformat(requested_str.replace("Z", "+00:00"))
            if req >= cutoff:
                recent_total += 1
            else:
                older_total += 1
        except ValueError:
            pass

    return {
        "total": len(records),
        "type_counts": type_counts,
        "recent_total": recent_total,
        "older_total": older_total,
        "half_days": days_back // 2,
        "days_back": days_back,
    }


def format_stats(data: dict) -> str:
    if data.get("total", 0) == 0:
        return f"📝 No noise complaints found in the past {data.get('days_back', 90)} days."

    total = data["total"]
    msg = f"🔇 *Noise Complaints — Last {data['days_back']} Days*\n\n"

    recent = data.get("recent_total", 0)
    older = data.get("older_total", 0)
    half = data.get("half_days", 45)
    if older > 0:
        trend = round(((recent - older) / older) * 100)
        arrow = "📈" if trend > 0 else "📉" if trend < 0 else "➡️"
        trend_str = f"+{trend}%" if trend > 0 else f"{trend}%"
        msg += f"{arrow} *Volume trend:* {trend_str} (last {half} days vs prior {half})\n"
    msg += f"📊 *Total complaints:* {total}\n\n"

    msg += "📋 *By complaint type:*\n"
    for label, count in sorted(data["type_counts"].items(), key=lambda x: -x[1]):
        pct = count / total * 100
        bar = "█" * min(10, round(pct / 10))
        msg += f"   *{label}*: {count} ({pct:.1f}%)\n"
        msg += f"   {bar}\n"

    return msg


# =============================================================================
# RESPONSE TIMES
# =============================================================================

def get_response_times(days_back: int = 90) -> dict:
    records = fetch_all_noise_complaints(days_back)
    if not records:
        return {"total": 0, "days_back": days_back}

    type_times: dict = {}

    for r in records:
        if (r.get("status") or "").lower() != "closed":
            continue
        requested_str = r.get("requested_datetime") or ""
        updated_str = r.get("updated_datetime") or ""
        if not requested_str or not updated_str:
            continue
        try:
            req = datetime.fromisoformat(requested_str.replace("Z", "+00:00"))
            upd = datetime.fromisoformat(updated_str.replace("Z", "+00:00"))
            days = (upd - req).days
            if 0 <= days <= 365:
                label = r.get("_service_label", "Unknown")
                type_times.setdefault(label, []).append(days)
        except ValueError:
            pass

    averages = {
        label: round(sum(times) / len(times), 1)
        for label, times in type_times.items()
        if times
    }

    overall_all = [d for times in type_times.values() for d in times]
    overall_avg = round(sum(overall_all) / len(overall_all), 1) if overall_all else None

    return {
        "averages": averages,
        "overall_avg": overall_avg,
        "total_closed": len(overall_all),
        "days_back": days_back,
    }


def format_response_times(data: dict) -> str:
    if not data.get("averages"):
        return "📝 Not enough closed complaints to calculate response times."

    msg = f"⏱ *Noise Complaint Response Times*\n"
    msg += f"_Based on {data['total_closed']} closed complaints (last {data['days_back']} days)_\n\n"

    if data.get("overall_avg") is not None:
        msg += f"📊 *Overall average:* {data['overall_avg']} days\n\n"

    msg += "📋 *By complaint type:*\n"
    for label, avg in sorted(data["averages"].items(), key=lambda x: x[1]):
        if avg <= 1:
            speed = "🟢"
        elif avg <= 5:
            speed = "🟡"
        else:
            speed = "🔴"
        msg += f"   {speed} *{label}:* {avg} days avg\n"

    return msg
