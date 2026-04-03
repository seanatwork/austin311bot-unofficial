"""
Infrastructure & Transportation — data layer and formatters.

Queries Austin Open311 API live across road, signal, and infrastructure service codes.
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
    "SBPOTREP": "Pothole Repair",
    "TRASIGMA": "Traffic Signal - Maintenance",
    "STREETL2": "Street Light Issue",
    "SBDEBROW": "Debris in Street",
    "ATTRSIMO": "Traffic Signal - Modification",
    "SIGNSTRE": "Street Name Sign Maintenance",
    "OBSINTTR": "Obstruction at Intersection",
    "SBSIDERE": "Sidewalk Repair",
    "SBSTRES":  "Street Resurfacing",
    "OBSTMIDB": "Obstruction in Right of Way",
    "ZZARSTSW": "Street Sweeping",
    "DRCHANEL": "Drainage/Creek Issues",
    "ATCOCIRW": "Construction in Right of Way",
    "PWTRISRW": "Tree Issue - Right of Way",
    "SBGENRL":  "Street & Bridge Miscellaneous",
    "SIGNNEWT": "Traffic Sign - New",
    "TRASIGNE": "Traffic Signal - New",
    "TPPECRNE": "Pedestrian Crossing - New/Modify",
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
            "User-Agent": "austin311bot/0.1 (Open311 traffic queries)",
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


def fetch_all_traffic_complaints(days_back: int = 90, limit_per_code: int = 100) -> list:
    all_records = []
    for code in SERVICE_CODES:
        try:
            records = _fetch_code(code, days_back, limit_per_code)
            all_records.extend(records)
            logger.debug(f"{code}: {len(records)} records")
        except Exception as e:
            logger.warning(f"Failed to fetch {code}: {e}")
    return all_records


# High-volume codes only — keeps API calls to 4 and results meaningful
BACKLOG_CODES = {
    "SBPOTREP": "Pothole Repair",
    "TRASIGMA": "Traffic Signal",
    "STREETL2": "Street Light",
    "SBDEBROW": "Debris in Street",
}


# =============================================================================
# INFRA BACKLOG
# =============================================================================

def get_infra_backlog() -> dict:
    """Fetch open infrastructure complaints across the 4 highest-volume codes."""
    now = _utc_now()
    start = now - timedelta(days=90)
    type_counts: dict = {}
    oldest: list = []  # (days_open, label, addr, ticket_id)

    for code, label in BACKLOG_CODES.items():
        try:
            params = {
                "service_code": code,
                "status": "open",
                "start_date": _isoformat_z(start),
                "end_date": _isoformat_z(now),
                "per_page": 100,
                "page": 1,
            }
            records = _make_request(params)
            type_counts[label] = len(records)
            for r in records:
                dt_str = r.get("requested_datetime") or ""
                addr = (r.get("address") or "Unknown").replace(", Austin", "").strip()
                ticket_id = r.get("service_request_id") or ""
                try:
                    req = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                    oldest.append(((now - req).days, label, addr, ticket_id))
                except (ValueError, TypeError):
                    pass
        except Exception as e:
            logger.warning(f"backlog fetch {code}: {e}")

    return {
        "total_open": sum(type_counts.values()),
        "type_counts": type_counts,
        "oldest_10": sorted(oldest, key=lambda x: -x[0])[:10],
    }


def format_infra_backlog(data: dict) -> str:
    """Returns the summary text. Oldest tickets are rendered as buttons by the handler."""
    total_open = data.get("total_open", 0)
    type_counts = data.get("type_counts", {})

    if not total_open:
        return "✅ No open infrastructure complaints in the last 90 days."

    msg = "📋 *Infrastructure Backlog*\n"
    msg += f"_{total_open} open complaints · last 90 days_\n\n"

    msg += "*Open by type:*\n"
    max_count = max(type_counts.values()) if type_counts else 1
    for label, count in sorted(type_counts.items(), key=lambda x: -x[1]):
        bar = "█" * min(10, round(count / max_count * 10))
        msg += f"  {bar} *{label}*: {count}\n"

    msg += "\n*Oldest unresolved — tap to look up:*"
    return msg


def build_backlog_keyboard(data: dict):
    """Returns a list of button rows for the oldest unresolved tickets."""
    from telegram import InlineKeyboardButton
    oldest_10 = data.get("oldest_10", [])
    rows = []
    for days_open, label, addr, ticket_id in oldest_10:
        age_emoji = "🔴" if days_open >= 30 else "🟡" if days_open >= 14 else "🟢"
        short_addr = addr[:28] + "…" if len(addr) > 30 else addr
        btn_label = f"{age_emoji} {days_open}d · {short_addr}"
        rows.append([InlineKeyboardButton(btn_label, callback_data=f"tlookup_{ticket_id}")])
    return rows


# =============================================================================
# BROKEN TRAFFIC SIGNALS
# =============================================================================

def get_signal_maintenance(days_back: int = 90) -> dict:
    """Fetch TRASIGMA records and surface broken signals still waiting for repair."""
    now = _utc_now()
    start = now - timedelta(days=days_back)
    params = {
        "service_code": "TRASIGMA",
        "start_date": _isoformat_z(start),
        "end_date": _isoformat_z(now),
        "per_page": 100,
        "page": 1,
    }
    records = _make_request(params)

    open_records = []
    closed_count = 0
    closed_days: list = []

    for r in records:
        status = (r.get("status") or "").lower()
        requested_str = r.get("requested_datetime") or ""
        updated_str = r.get("updated_datetime") or ""
        if not requested_str:
            continue

        if status == "closed":
            closed_count += 1
            try:
                req = datetime.fromisoformat(requested_str.replace("Z", "+00:00"))
                upd = datetime.fromisoformat(updated_str.replace("Z", "+00:00"))
                d = (upd - req).days
                if 0 <= d <= 365:
                    closed_days.append(d)
            except (ValueError, TypeError):
                pass
        else:
            try:
                req = datetime.fromisoformat(requested_str.replace("Z", "+00:00"))
                days_waiting = (now - req).days
                addr = (r.get("address") or "Unknown").replace(", Austin, TX", "").replace(", Austin", "").strip()
                ticket_id = r.get("service_request_id") or ""
                open_records.append((days_waiting, addr, ticket_id))
            except (ValueError, TypeError):
                pass

    open_records.sort(key=lambda x: -x[0])
    avg_wait = round(sum(x[0] for x in open_records) / len(open_records), 1) if open_records else None
    avg_fix = round(sum(closed_days) / len(closed_days), 1) if closed_days else None

    return {
        "total": len(records),
        "open_records": open_records,
        "closed_count": closed_count,
        "avg_wait_days": avg_wait,
        "avg_fix_days": avg_fix,
        "days_back": days_back,
    }


def format_signal_maintenance(data: dict) -> str:
    total = data.get("total", 0)
    open_records = data.get("open_records", [])
    closed_count = data.get("closed_count", 0)
    avg_wait = data.get("avg_wait_days")
    avg_fix = data.get("avg_fix_days")
    days_back = data.get("days_back", 90)

    if not total:
        return "✅ No traffic signal maintenance requests in the last 90 days."

    open_count = len(open_records)
    msg = "🚦 *Broken Traffic Signals*\n"
    msg += f"_Last {days_back} days · {total} reported · {open_count} still broken · {closed_count} fixed_\n\n"

    if avg_fix is not None:
        if avg_fix <= 7:
            verdict = "🟢 City is fixing signals quickly"
        elif avg_fix <= 21:
            verdict = "🟡 Repair times are moderate"
        else:
            verdict = "🔴 Signals are waiting a long time"
        msg += f"{verdict}\n"
        msg += f"⏱ *Avg fix time:* {avg_fix} days\n"

    if avg_wait is not None:
        msg += f"⏳ *Avg current wait:* {avg_wait} days\n"

    if open_records:
        msg += "\n*Still broken — longest waiting:*\n"
        for days_waiting, addr, ticket_id in open_records[:8]:
            age_emoji = "🔴" if days_waiting >= 30 else "🟡" if days_waiting >= 14 else "🟢"
            msg += f"{age_emoji} *{days_waiting}d* — {addr}\n"

    return msg
