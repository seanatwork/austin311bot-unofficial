"""
Graffiti Analysis — live Open311 API queries.
Replaces the old SQLite-backed implementation.
"""

import time
import logging
import requests
from datetime import datetime, timezone, timedelta
from collections import Counter
from typing import Optional

from .config import Config

logger = logging.getLogger(__name__)

OPEN311_BASE_URL = "https://311.austintexas.gov/open311/v2"
TIMEOUT = 15
MAX_RETRIES = 3

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
            "User-Agent": "austin311bot/0.1 (graffiti queries)",
        })
    return _session


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _isoformat_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _fetch_graffiti(days_back: int = 90, limit: int = 100) -> list:
    """Fetch graffiti records from Open311 API."""
    end = _utc_now()
    start = end - timedelta(days=days_back)
    url = f"{OPEN311_BASE_URL}/requests.json"
    params = {
        "service_code": Config.SERVICE_CODE,
        "start_date": _isoformat_z(start),
        "end_date": _isoformat_z(end),
        "per_page": limit,
        "page": 1,
    }

    all_records = []
    session = _get_session()

    while True:
        retries = 0
        while True:
            try:
                resp = session.get(url, params=params, timeout=TIMEOUT)
                resp.raise_for_status()
                data = resp.json()
                break
            except RETRYABLE_ERRORS as e:
                retries += 1
                if retries >= MAX_RETRIES:
                    raise
                time.sleep(2 ** retries)

        if not isinstance(data, list) or not data:
            break
        all_records.extend(data)
        if len(data) < limit:
            break
        params["page"] += 1
        if params["page"] > 20:
            break

    return all_records


# =============================================================================
# ANALYZE COMMAND
# =============================================================================

def analyze_graffiti_command(days_back: int = 90) -> str:
    try:
        records = _fetch_graffiti(days_back)
    except Exception as e:
        logger.error(f"graffiti fetch: {e}")
        return f"❌ Could not fetch graffiti data: {e}"

    if not records:
        return f"📝 No graffiti reports found in the last {days_back} days."

    total = len(records)
    now = _utc_now()
    half = timedelta(days=days_back // 2)
    cutoff = now - half
    week_ago = now - timedelta(days=7)

    open_count = 0
    closed_count = 0
    recent_half = 0
    older_half = 0
    last_7_days = 0
    open_waiting = []  # (days_waiting, address, ticket_id)

    for r in records:
        status = (r.get("status") or "").lower()
        dt_str = r.get("requested_datetime") or ""
        addr = (r.get("address") or "Unknown").replace(", Austin", "").strip()
        ticket_id = r.get("service_request_id") or ""

        if status == "closed":
            closed_count += 1
        else:
            open_count += 1
            if ticket_id and dt_str:
                try:
                    req = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                    days_waiting = (now - req).days
                    if 0 <= days_waiting <= 365:
                        open_waiting.append((days_waiting, addr, ticket_id))
                except (ValueError, TypeError):
                    pass

        if dt_str:
            try:
                dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                if dt >= cutoff:
                    recent_half += 1
                else:
                    older_half += 1
                if dt >= week_ago:
                    last_7_days += 1
            except (ValueError, TypeError):
                pass

    if older_half > 0:
        trend = round(((recent_half - older_half) / older_half) * 100)
        arrow = "📈" if trend > 0 else "📉" if trend < 0 else "➡️"
        trend_str = f"{arrow} {'+' if trend > 0 else ''}{trend}% vs prior {days_back // 2} days"
    else:
        trend_str = None

    msg = f"🎨 *Graffiti Analysis — Last {days_back} Days*\n\n"
    msg += f"📊 *Total reports:* {total}\n"
    msg += f"✅ *Closed:* {closed_count}  🔴 *Open:* {open_count}\n"
    msg += f"🗓 *Last 7 days:* {last_7_days} new reports\n"
    if trend_str:
        msg += f"{trend_str}\n"

    if open_waiting:
        top_waiting = sorted(open_waiting, key=lambda x: -x[0])[:5]
        msg += "\n*Longest open — still unresolved:*\n"
        for days_waiting, addr, ticket_id in top_waiting:
            url = f"https://311.austintexas.gov/open311/v2/requests/{ticket_id}.json"
            msg += f"  🕐 {days_waiting}d — {addr} [🔗]({url})\n"

    msg += "\n_Source: [Austin Open311 API](https://311.austintexas.gov/open311/v2)_"
    return msg


# =============================================================================
# PATTERNS COMMAND (kept for import compatibility)
# =============================================================================

def patterns_command(days_back: int = 30) -> str:
    return analyze_graffiti_command(days_back)
