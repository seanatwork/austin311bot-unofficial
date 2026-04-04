"""
Bicycle Complaints — data layer and formatters.

Queries Austin Open311 API live for PWBICYCL (Bicycle) service requests.
No local database required — same pattern as restaurants/.
"""

import re
import time
import logging
import requests
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

OPEN311_BASE_URL = "https://311.austintexas.gov/open311/v2"
SERVICE_CODE = "PWBICYCL"
TIMEOUT = 10
MAX_RETRIES = 3
RETRY_DELAY = 1.0

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
            "User-Agent": "austin311bot/0.1 (Open311 bicycle queries)",
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


def get_recent_complaints(limit: int = 10, days_back: int = 90) -> list:
    """Return most recent bicycle complaints from the past N days."""
    end = _utc_now()
    start = end - timedelta(days=days_back)
    params = {
        "service_code": SERVICE_CODE,
        "start_date": _isoformat_z(start),
        "end_date": _isoformat_z(end),
        "per_page": limit,
        "page": 1,
    }
    logger.debug(f"Fetching recent bicycle complaints (last {days_back} days)")
    return _make_request(params)


def lookup_ticket(ticket_id: str) -> Optional[dict]:
    """Look up any 311 service request by ticket ID. Returns the record or None."""
    session = _get_session()
    # Strip leading # if user typed it
    ticket_id = ticket_id.lstrip("#").strip()
    # Normalize: "2600098090" → "26-00098090"
    if re.match(r"^\d{10}$", ticket_id):
        ticket_id = f"{ticket_id[:2]}-{ticket_id[2:]}"
    url = f"{OPEN311_BASE_URL}/requests/{ticket_id}.json"
    try:
        resp = session.get(url, timeout=TIMEOUT)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            return data[0] if data else None
        return data
    except RETRYABLE_ERRORS as e:
        logger.warning(f"Ticket lookup failed ({e})")
        return None


def format_ticket(record: dict) -> str:
    ticket_id = record.get("service_request_id") or "N/A"
    service_name = record.get("service_name") or "Unknown"
    service_code = record.get("service_code") or "N/A"
    status = (record.get("status") or "unknown").upper()
    address = record.get("address") or "Address not available"
    lat = record.get("lat")
    lon = record.get("long")
    requested = record.get("requested_datetime") or ""
    updated = record.get("updated_datetime") or ""
    description = (record.get("description") or "").strip()
    notes = (record.get("status_notes") or "").strip()
    token = record.get("token") or ""

    # Format datetimes readably
    def fmt_dt(s):
        if not s:
            return "N/A"
        return s.replace("T", " ").replace("Z", " UTC")

    status_emoji = "🟢" if status == "CLOSED" else "🔴"

    msg = f"🎫 *311 Ticket Lookup*\n"
    msg += f"━━━━━━━━━━━━━━━━━━━━\n\n"
    msg += f"📋 *Service:* {service_name} `({service_code})`\n"
    msg += f"{status_emoji} *Status:* {status}\n\n"
    msg += f"📍 *Address:* {address}\n"
    if lat and lon:
        msg += f"🗺 *Coordinates:* {lat:.6f}, {lon:.6f}\n"
    msg += f"\n📅 *Filed:* {fmt_dt(requested)}\n"
    if updated:
        msg += f"🔄 *Last updated:* {fmt_dt(updated)}\n"
    if description:
        msg += f"\n💬 *Description:*\n{description}\n"
    if notes:
        msg += f"\n📝 *Status notes:*\n{notes}\n"
    if ticket_id != "N/A":
        msg += f"\n🔗 [View ticket on Austin 311](https://311.austintexas.gov/open311/v2/requests/{ticket_id}.json)"
    return msg


def get_stats(days_back: int = 90) -> dict:
    """Return meaningful statistics for bicycle complaints."""
    complaints = get_recent_complaints(limit=100, days_back=days_back)
    if not complaints:
        return {"total": 0, "days_back": days_back}

    now = _utc_now()
    resolution_days = []
    open_tickets = []
    street_counts: dict = {}

    # Split into two halves to show trend
    half = timedelta(days=days_back // 2)
    cutoff = now - half
    recent_half = 0
    older_half = 0

    for r in complaints:
        status = (r.get("status") or "").lower()
        requested_str = r.get("requested_datetime") or ""
        updated_str = r.get("updated_datetime") or ""

        # Resolution time for closed tickets
        if status == "closed" and requested_str and updated_str:
            try:
                req = datetime.fromisoformat(requested_str.replace("Z", "+00:00"))
                upd = datetime.fromisoformat(updated_str.replace("Z", "+00:00"))
                days = (upd - req).days
                if 0 <= days <= 365:
                    resolution_days.append(days)
            except ValueError:
                pass

        # Open tickets
        if status == "open":
            open_tickets.append(r)

        # Top streets from address
        address = r.get("address") or ""
        # Extract street name (skip house number)
        parts = address.replace(", Austin", "").strip().split()
        if len(parts) >= 2:
            street = " ".join(parts[1:])
            street_counts[street] = street_counts.get(street, 0) + 1

        # Volume trend: recent half vs older half
        if requested_str:
            try:
                req = datetime.fromisoformat(requested_str.replace("Z", "+00:00"))
                if req >= cutoff:
                    recent_half += 1
                else:
                    older_half += 1
            except ValueError:
                pass

    avg_resolution = round(sum(resolution_days) / len(resolution_days), 1) if resolution_days else None
    top_streets = sorted(street_counts.items(), key=lambda x: -x[1])[:5]

    # Oldest unresolved complaint
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
        "total": len(complaints),
        "open": len(open_tickets),
        "closed": len(complaints) - len(open_tickets),
        "avg_resolution_days": avg_resolution,
        "top_streets": top_streets,
        "recent_half": recent_half,
        "older_half": older_half,
        "half_days": days_back // 2,
        "oldest_open": oldest_open,
        "days_back": days_back,
    }


def format_complaints(complaints: list, title: str = "🚴 Bicycle Complaints") -> str:
    if not complaints:
        return "📝 No bicycle complaints found for that search."

    msg = f"{title}\n\n"
    msg += f"Showing {len(complaints)} complaint(s):\n\n"

    for i, r in enumerate(complaints, 1):
        req_id = r.get("service_request_id") or "N/A"
        address = r.get("address") or "Address not available"
        status = (r.get("status") or "unknown").upper()
        requested = r.get("requested_datetime") or ""
        # Trim to date only for readability
        if "T" in requested:
            requested = requested.split("T")[0]
        description = r.get("description") or r.get("service_name") or "Bicycle complaint"

        msg += f"{i}. *{description[:80]}*\n"
        msg += f"📍 {address}\n"
        msg += f"🔖 Status: {status} | 📅 {requested}\n"
        msg += f"🎫 #{req_id}\n\n"

        if i >= 10:
            remaining = len(complaints) - i
            if remaining > 0:
                msg += f"... and {remaining} more.\n"
            break

    return msg


def format_stats(stats: dict) -> str:
    if stats.get("total", 0) == 0:
        return f"📝 No bicycle complaints found in the past {stats.get('days_back', 90)} days."

    total = stats["total"]
    msg = "🚴 *Bicycle Complaints — Last 90 Days*\n\n"

    # Volume trend
    recent = stats.get("recent_half", 0)
    older = stats.get("older_half", 0)
    half = stats.get("half_days", 45)
    if older > 0:
        trend = round(((recent - older) / older) * 100)
        arrow = "📈" if trend > 0 else "📉" if trend < 0 else "➡️"
        trend_str = f"+{trend}%" if trend > 0 else f"{trend}%"
        msg += f"{arrow} *Volume trend:* {trend_str} (last {half} days vs prior {half})\n"
    msg += f"📊 *Total complaints:* {total} ({stats['open']} open · {stats['closed']} closed)\n\n"

    # Resolution time
    if stats.get("avg_resolution_days") is not None:
        msg += f"⏱ *Avg resolution time:* {stats['avg_resolution_days']} days\n\n"

    # Top streets
    top = stats.get("top_streets", [])
    if top:
        msg += "📍 *Most complained streets:*\n"
        for street, count in top:
            msg += f"   {street}: {count} complaint{'s' if count > 1 else ''}\n"
        msg += "\n"

    # Oldest unresolved
    oldest = stats.get("oldest_open")
    if oldest:
        msg += f"🕰 *Oldest open ticket:* #{oldest['id']}\n"
        msg += f"   {oldest['address']} — {oldest['days_ago']} days unresolved\n"

    msg += "\n_Source: [Austin Open311 API](https://311.austintexas.gov/open311/v2)_"
    return msg
