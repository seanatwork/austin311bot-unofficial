"""
Parks Maintenance — data layer and formatters.

Queries Austin Open311 API live for all park-related service requests.
Provides hotspot analysis (parks with most unresolved complaints), complaint type stats,
and response time analysis.

Service codes (from 311categories.txt):
- PRGRDISS: Park Maintenance - Grounds (36,598)
- PRGRDPLB: Park Maintenance - Grounds Plumbing Issues (7,324)
- PRGRDELC: Park Maintenance - Grounds Electrical Issues (4,514)
- PRBLDPLB: Park Maintenance - Building Plumbing Issues (2,767)
- PRBLDISS: Park Building Issues (1,478)
- PRBLDACH: Parks - Building A/C & Heating Issues (958)
- PRBLDELE: Parks - Building Electric Issues (532)
- COMPARLN: Commercial Use of Parkland (356)
- PRCEMET1: Park Cemeteries (343)

Note: PATRISPA (Park - Tree Issues) moved to trees/ module under Environment.
"""

import io
import os
import time
import tempfile
import logging
import requests
from datetime import datetime, timezone, timedelta
from typing import Optional
from collections import defaultdict

from open311_client import og_meta_tags

logger = logging.getLogger(__name__)

OPEN311_BASE_URL = "https://311.austintexas.gov/open311/v2"
TIMEOUT = 45
MAX_RETRIES = 8
RETRY_DELAY = 1.0
MAX_PAGES = 15  # cap at 1,500 records per code for performance

# API key from environment
API_KEY = os.getenv("AUSTINAPIKEY")

# Park service codes and human-readable labels
SERVICE_CODES = {
    "PRGRDISS": "Grounds Maintenance",
    "PRGRDPLB": "Grounds Plumbing",
    "PRGRDELC": "Grounds Electrical",
    "PRBLDPLB": "Building Plumbing",
    "PRBLDISS": "Building Issues",
    "PRBLDACH": "Building A/C & Heating",
    "PRBLDELE": "Building Electric",
    "COMPARLN": "Commercial Use of Parkland",
    "PRCEMET1": "Park Cemeteries",
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
        headers = {
            "Accept": "application/json",
            "User-Agent": "austin311bot/0.1 (Open311 parks queries)",
        }
        if API_KEY:
            headers["X-Api-Key"] = API_KEY
        _session.headers.update(headers)
    return _session


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_central_time() -> str:
    """Return current time formatted in US Central Time (CDT/CST)."""
    utc_now = datetime.now(timezone.utc)
    month = utc_now.month
    is_dst = 3 <= month <= 11
    offset_hours = -5 if is_dst else -6
    central_now = utc_now + timedelta(hours=offset_hours)
    tz_abbr = "CDT" if is_dst else "CST"
    return central_now.strftime(f"%Y-%m-%d %I:%M %p {tz_abbr}")


def _isoformat_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


_PARK_KEYWORDS = (
    "Park", "Pool", "Recreation Center", "Rec Center", "Greenbelt",
    "Trail", "Field", "Cemetery", "Cemetary", "Garden", "Plaza",
    "Preserve", "Reserve", "Lake", "Springs", "Barton",
)


def _extract_park_name(address: str) -> str:
    """Extract and normalize a park name from an Open311 address field.

    Handles patterns like:
    - 'Zilker Park, 2100 Barton Springs Rd'
    - 'Pease Park, 1100 Kingsbury St'
    - 'Barton Springs Pool, Austin'
    - '1234 Some St, Austin'

    Returns a title-cased park/street name for consistent bucketing.
    """
    addr = address.strip()
    if not addr or addr.lower() == "unknown":
        return "Unknown"

    # Strip city/state suffixes
    for suffix in (", Austin, TX", ", Austin TX", ", Austin"):
        addr = addr.replace(suffix, "")
    addr = addr.strip()

    # If a known park-type keyword appears before the first comma, use that segment
    first_segment = addr.split(",", 1)[0].strip()
    for kw in _PARK_KEYWORDS:
        if kw.lower() in first_segment.lower():
            return first_segment.title()

    # Fall back to street name (strip leading house number)
    parts = first_segment.split(" ", 1)
    if len(parts) == 2 and parts[0].isdigit():
        return parts[1].strip().title()

    return first_segment.title()


def _make_request(params: dict, retries: int = 0) -> list:
    session = _get_session()
    url = f"{OPEN311_BASE_URL}/requests.json"
    try:
        resp = session.get(url, params=params, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []
    except requests.exceptions.HTTPError as e:
        if e.response.status_code in {429, 500, 502, 503, 504} and retries < MAX_RETRIES:
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


def _fetch_code(service_code: str, days_back: int, limit: int = 100) -> list:
    """Fetch requests for a single service code with pagination."""
    end = _utc_now()
    start = end - timedelta(days=days_back)
    all_records = []
    seen_ids = set()
    page = 1

    while page <= MAX_PAGES:
        params = {
            "service_code": service_code,
            "start_date": _isoformat_z(start),
            "end_date": _isoformat_z(end),
            "per_page": min(limit, 100),
            "page": page,
        }
        records = _make_request(params)
        if not records:
            break
        
        new_records = []
        for r in records:
            sid = r.get("service_request_id")
            if sid and sid not in seen_ids:
                seen_ids.add(sid)
                r["_service_label"] = SERVICE_CODES.get(service_code, service_code)
                new_records.append(r)
        
        all_records.extend(new_records)
        
        if len(records) < 100:
            break
        
        page += 1
        # Rate limit
        time.sleep(0.5 if API_KEY else 1.0)

    return all_records


def fetch_all_park_complaints(days_back: int = 90, use_cache: bool = True) -> list:
    """Fetch complaints across all park service codes with optional caching."""
    from open311_cache import init_cache, get_cached_records, cache_records, get_last_fetch_date

    CATEGORY = "parks"

    # Initialize cache if using
    if use_cache:
        init_cache()
        cached_records = get_cached_records(service_codes=list(SERVICE_CODES.keys()))
        cached_ids = {r.get("service_request_id") for r in cached_records}
        logger.info(f"Loaded {len(cached_records)} cached parks records")

        # Check if cache is fresh (less than 6 days old)
        last_fetch = get_last_fetch_date(service_codes=list(SERVICE_CODES.keys()))
        if last_fetch:
            cache_age = _utc_now() - last_fetch
            if cache_age < timedelta(days=6) and len(cached_records) > 0:
                logger.info(f"Cache is fresh ({cache_age.days} days old), using cached data")
                return cached_records
    else:
        cached_records = []
        cached_ids = set()

    all_records = []
    seen_ids = cached_ids.copy()
    new_records = []

    for code in SERVICE_CODES:
        try:
            records = _fetch_code(code, days_back, limit=100)
            # Filter out already-cached records
            unique_records = [r for r in records if r.get("service_request_id") not in seen_ids]
            for r in unique_records:
                seen_ids.add(r.get("service_request_id"))
                new_records.append(r)
            all_records.extend(unique_records)
            logger.debug(f"{code}: {len(unique_records)} new records")
        except Exception as e:
            logger.warning(f"Failed to fetch {code}: {e}")

    # Combine with cached records
    if use_cache and cached_records:
        all_records = cached_records + [r for r in all_records if r.get("service_request_id") not in cached_ids]

    # Cache new records
    if use_cache and new_records:
        cache_records(CATEGORY, new_records)
        logger.info(f"Cached {len(new_records)} new parks records")

    return all_records


def fetch_parks_monthly(months_back: int = 13, use_cache: bool = True) -> list:
    """Fetch park complaint records month-by-month with optional caching.

    With caching enabled (default), this will:
    1. Load cached records from SQLite
    2. Only fetch new records from Open311 API
    3. Cache new records for future runs

    The Open311 API returns records in chronological order (oldest first), so a
    single 365-day request only returns the oldest ~90 days before hitting the
    per-page cap. Fetching month by month ensures every period is fully covered.

    Args:
        months_back: Number of months to fetch (default 13 for a full rolling year)
        use_cache: Whether to use SQLite caching (default True)

    Returns:
        A flat list of park complaint records across all months and service codes.
    """
    from open311_cache import init_cache, get_cached_records, cache_records, get_last_fetch_date

    CATEGORY = "parks"

    if use_cache:
        init_cache()
        cached_records = get_cached_records(service_codes=list(SERVICE_CODES.keys()))
        cached_ids = {r.get("service_request_id") for r in cached_records}
        logger.info(f"Loaded {len(cached_records)} cached parks records")

        last_fetch = get_last_fetch_date(service_codes=list(SERVICE_CODES.keys()))
        if last_fetch:
            cache_age = _utc_now() - last_fetch
            if cache_age < timedelta(days=6) and len(cached_records) > 0:
                logger.info(f"Cache is fresh ({cache_age.days} days old), returning cached data")
                return cached_records
    else:
        cached_records = []
        cached_ids = set()

    now = _utc_now()
    all_records: list = []
    seen_ids: set = cached_ids.copy()
    new_records: list = []

    # Determine fetch window
    if use_cache and cached_records:
        last_fetch = get_last_fetch_date(service_codes=list(SERVICE_CODES.keys()))
        if last_fetch:
            fetch_start = last_fetch - timedelta(days=1)
        else:
            fetch_start = now - timedelta(days=30 * months_back)
    else:
        fetch_start = now - timedelta(days=30 * months_back)

    logger.info(f"Fetching parks records from {fetch_start} to {now}")

    # Build month list
    current_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    start_month = fetch_start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    months_to_fetch = []
    while start_month <= current_month:
        months_to_fetch.append(start_month)
        if start_month.month == 12:
            start_month = start_month.replace(year=start_month.year + 1, month=1)
        else:
            start_month = start_month.replace(month=start_month.month + 1)

    logger.info(f"Will fetch {len(months_to_fetch)} months of parks data")

    for month_start in reversed(months_to_fetch):  # Newest first
        if month_start.year == now.year and month_start.month == now.month:
            month_end = now
        else:
            if month_start.month == 12:
                month_end = month_start.replace(year=month_start.year + 1, month=1)
            else:
                month_end = month_start.replace(month=month_start.month + 1)

        for code in SERVICE_CODES:
            try:
                page = 1
                monthly_count = 0
                while page <= MAX_PAGES:
                    params = {
                        "service_code": code,
                        "start_date": _isoformat_z(month_start),
                        "end_date": _isoformat_z(month_end),
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
                            r["_service_label"] = SERVICE_CODES.get(code, code)
                            r["_service_code"] = code
                            all_records.append(r)
                            new_records.append(r)
                            monthly_count += 1
                    if len(records) < 100:
                        break
                    page += 1
                    time.sleep(0.5 if API_KEY else 1.0)
                if monthly_count > 0:
                    logger.info(f"  {code} {month_start.strftime('%Y-%m')}: {monthly_count} new records")
            except Exception as e:
                logger.warning(f"Monthly parks fetch failed {code} {month_start.strftime('%Y-%m')}: {e}")
        time.sleep(1.0 if API_KEY else 2.0)

    # Cache new records
    if use_cache and new_records:
        cache_records(CATEGORY, new_records)
        logger.info(f"Cached {len(new_records)} new parks records")

    # Merge with existing cache
    if use_cache and cached_records:
        combined = {r.get("service_request_id"): r for r in cached_records}
        for r in all_records:
            combined[r.get("service_request_id")] = r
        result = list(combined.values())
        logger.info(f"Returning {len(result)} total parks records ({len(cached_records)} cached + {len(new_records)} new)")
        return result

    return all_records


# =============================================================================
# PARK HOTSPOTS — Parks with most unresolved complaints
# =============================================================================

def get_park_hotspots(days_back: int = 90) -> dict:
    """Return park complaint counts grouped by park, with open/closed breakdown."""
    records = fetch_all_park_complaints(days_back)
    if not records:
        return {"hotspots": [], "total": 0, "days_back": days_back}

    park_counts: dict = {}  # park_name → {"total": N, "open": N, "closed": N}
    park_types: dict = {}   # park_name → {service_label: count}
    park_coords: dict = {}  # park_name → (lat, lon)

    for r in records:
        address = (r.get("address") or "").strip()
        park = _extract_park_name(address) if address else "Unknown"
        label = r.get("_service_label", "Unknown")
        status = (r.get("status") or "").lower()
        lat = r.get("lat")
        lon = r.get("long")

        if park not in park_counts:
            park_counts[park] = {"total": 0, "open": 0, "closed": 0}
        park_counts[park]["total"] += 1
        if status == "open":
            park_counts[park]["open"] += 1
        elif status == "closed":
            park_counts[park]["closed"] += 1

        park_types.setdefault(park, {})
        park_types[park][label] = park_types[park].get(label, 0) + 1

        # Store first coordinates seen for this park
        if park not in park_coords and lat and lon:
            park_coords[park] = (lat, lon)

    # Sort by open complaints (unresolved issues most useful for users)
    hotspots = sorted(park_counts.items(), key=lambda x: -x[1]["open"])

    # Ranked list of park names (index = rank-1, used for drill-down callbacks)
    ranked_parks = [park for park, _ in hotspots]

    return {
        "hotspots": hotspots,
        "park_types": park_types,
        "park_coords": park_coords,
        "ranked_parks": ranked_parks,
        "total": len(records),
        "days_back": days_back,
    }


def format_hotspots(data: dict, page: int = 1) -> str:
    """Format park hotspots.

    Args:
        data: result dict from get_park_hotspots()
        page: 1 = ranks 1-10, 2 = ranks 11-25
    """
    hotspots = data.get("hotspots", [])
    park_types = data.get("park_types", {})
    park_coords = data.get("park_coords", {})
    total = data.get("total", 0)
    days_back = data.get("days_back", 90)

    if not hotspots:
        return "📝 No park maintenance complaints found."

    if page == 1:
        slice_start, slice_end = 0, 10
        title_suffix = ""
    else:
        slice_start, slice_end = 10, 25
        title_suffix = " (11–25)"

    top = hotspots[slice_start:slice_end]
    if not top:
        return "📝 Not enough parks for a second page."

    msg = f"🏞️ *Park Maintenance Hotspots{title_suffix}*\n"
    msg += f"_Last {days_back} days · {total} total complaints_\n\n"
    msg += f"_Sorted by unresolved (open) complaints_\n\n"

    max_open = max((c["open"] for _, c in hotspots[:10]), default=1) or 1

    for i, (park, counts) in enumerate(top, slice_start + 1):
        open_count = counts["open"]
        closed_count = counts["closed"]
        total_count = counts["total"]

        # Progress bar relative to page-1 max so bars are comparable
        bar_open = "🔴" * min(5, round(open_count / max_open * 5)) if open_count > 0 else "⚫"
        bar_closed = "🟢" * min(5, round(closed_count / max_open * 5)) if closed_count > 0 else ""

        msg += f"{i}. *{park}*\n"
        msg += f"   {bar_open}{bar_closed} {open_count} open · {closed_count} resolved ({total_count} total)\n"

        # Top complaint types
        types = park_types.get(park, {})
        top_types = sorted(types.items(), key=lambda x: -x[1])[:2]
        if top_types:
            type_str = " · ".join(f"{t} ({c})" for t, c in top_types)
            msg += f"   _{type_str}_\n"

        # Clickable map link
        if park in park_coords:
            lat, lon = park_coords[park]
            msg += f"   [📍 View on map](https://maps.google.com/?q={float(lat):.5f},{float(lon):.5f})\n"

        msg += "\n"

    msg += "_Source: [Austin Open311 API](https://311.austintexas.gov/open311/v2)_"
    return msg


# =============================================================================
# PARK DRILL-DOWN — Individual complaints for a specific park
# =============================================================================

def get_park_detail(park_name: str, days_back: int = 90) -> dict:
    """Return individual complaint records for a single park."""
    records = fetch_all_park_complaints(days_back)
    park_records = [
        r for r in records
        if _extract_park_name((r.get("address") or "").strip()) == park_name
    ]
    # Sort: open first, then by most recently requested
    def _sort_key(r):
        status = (r.get("status") or "").lower()
        dt_str = r.get("requested_datetime") or ""
        return (0 if status == "open" else 1, dt_str)

    park_records.sort(key=_sort_key)
    return {
        "park_name": park_name,
        "records": park_records,
        "days_back": days_back,
    }


def format_park_detail(data: dict) -> str:
    park_name = data.get("park_name", "Unknown")
    records = data.get("records", [])
    days_back = data.get("days_back", 90)

    if not records:
        return f"📝 No complaints found for *{park_name}* in the last {days_back} days."

    open_recs = [r for r in records if (r.get("status") or "").lower() == "open"]
    closed_recs = [r for r in records if (r.get("status") or "").lower() == "closed"]

    msg = f"🏞️ *{park_name}*\n"
    msg += f"_Last {days_back} days · {len(records)} complaints · {len(open_recs)} open · {len(closed_recs)} resolved_\n\n"

    # Show up to 15 records (open first, then most recent closed)
    shown = (records[:15])
    for r in shown:
        status = (r.get("status") or "").lower()
        status_icon = "🔴" if status == "open" else "🟢"
        label = r.get("_service_label", "Unknown")
        req_id = r.get("service_request_id", "")
        dt_str = r.get("requested_datetime") or ""
        desc = (r.get("description") or "").strip()
        lat = r.get("lat")
        lon = r.get("long")

        # Parse date
        date_fmt = ""
        if dt_str:
            try:
                dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                date_fmt = dt.strftime("%b %-d")
            except ValueError:
                date_fmt = dt_str[:10]

        msg += f"{status_icon} *{label}*"
        if date_fmt:
            msg += f" · {date_fmt}"
        if req_id:
            msg += f" · `#{req_id}`"
        msg += "\n"

        if desc:
            # Trim long descriptions
            snippet = desc if len(desc) <= 120 else desc[:117] + "…"
            msg += f"   _{snippet}_\n"

        if lat and lon:
            msg += f"   [📍 Map](https://maps.google.com/?q={float(lat):.5f},{float(lon):.5f})\n"

        msg += "\n"

    if len(records) > 15:
        msg += f"_…and {len(records) - 15} more complaints not shown_\n\n"

    msg += "_Source: [Austin Open311 API](https://311.austintexas.gov/open311/v2)_"
    return msg


# =============================================================================
# STATS BY COMPLAINT TYPE
# =============================================================================

def get_park_stats(days_back: int = 90) -> dict:
    """Return complaint counts by service type with open/closed breakdown."""
    records = fetch_all_park_complaints(days_back)
    if not records:
        return {"total": 0, "days_back": days_back}

    type_counts: dict = {}  # label → {"total": N, "open": N, "closed": N}
    status_counts = {"open": 0, "closed": 0, "other": 0}

    for r in records:
        label = r.get("_service_label", "Unknown")
        status = (r.get("status") or "").lower()
        
        if label not in type_counts:
            type_counts[label] = {"total": 0, "open": 0, "closed": 0}
        type_counts[label]["total"] += 1
        
        if status == "open":
            type_counts[label]["open"] += 1
            status_counts["open"] += 1
        elif status == "closed":
            type_counts[label]["closed"] += 1
            status_counts["closed"] += 1
        else:
            status_counts["other"] += 1

    return {
        "total": len(records),
        "type_counts": type_counts,
        "status_counts": status_counts,
        "days_back": days_back,
    }


def format_stats(data: dict) -> str:
    if data.get("total", 0) == 0:
        return f"📝 No park complaints found in the past {data.get('days_back', 90)} days."

    total = data["total"]
    days_back = data["days_back"]
    status = data.get("status_counts", {})
    
    msg = f"🏞️ *Park Maintenance — Last {days_back} Days*\n\n"
    msg += f"📊 *Total complaints:* {total}\n"
    msg += f"🔴 *Open:* {status.get('open', 0)}\n"
    msg += f"🟢 *Closed:* {status.get('closed', 0)}\n\n"

    msg += "📋 *By complaint type:*\n"
    for label, counts in sorted(data["type_counts"].items(), key=lambda x: -x[1]["total"]):
        count = counts["total"]
        pct = count / total * 100
        bar = "█" * min(10, round(pct / 10))
        open_count = counts.get("open", 0)
        msg += f"   *{label}*: {count} ({pct:.1f}%)\n"
        msg += f"   {bar} {open_count} open\n"

    msg += "\n_Source: [Austin Open311 API](https://311.austintexas.gov/open311/v2)_"
    return msg


# =============================================================================
# RESPONSE TIME — How long to resolve park issues
# =============================================================================

def get_park_resolution(days_back: int = 90) -> dict:
    """Calculate average response time per complaint type for closed tickets."""
    records = fetch_all_park_complaints(days_back)
    if not records:
        return {"total": 0, "days_back": days_back}

    type_times: dict = {}  # label → list of days to close
    overall_times = []

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
                overall_times.append(days)
        except ValueError:
            pass

    averages = {
        label: round(sum(times) / len(times), 1)
        for label, times in type_times.items()
        if times
    }

    overall_avg = round(sum(overall_times) / len(overall_times), 1) if overall_times else None

    return {
        "averages": averages,
        "overall_avg": overall_avg,
        "total_closed": len(overall_times),
        "days_back": days_back,
    }


def format_unified_overview(hotspots_data: dict, stats_data: dict) -> str:
    """Format a unified overview combining hotspots and stats."""
    hotspots = hotspots_data.get("hotspots", [])
    park_types = hotspots_data.get("park_types", {})
    park_coords = hotspots_data.get("park_coords", {})
    total_hotspots = hotspots_data.get("total", 0)
    days_back = hotspots_data.get("days_back", 90)
    
    stats_total = stats_data.get("total", 0)
    status_counts = stats_data.get("status_counts", {})
    type_counts = stats_data.get("type_counts", {})

    if not hotspots and stats_total == 0:
        return "No park maintenance complaints found."

    msg = f"Park Maintenance Overview\n"
    msg += f"Last {days_back} days\n\n"
    
    # Summary stats
    msg += f"Total complaints: {stats_total}\n"
    msg += f"Open: {status_counts.get('open', 0)} · Closed: {status_counts.get('closed', 0)}\n\n"

    # Top hotspots with direct park names
    if hotspots:
        msg += f"Top Parks by Unresolved Issues:\n"
        top_parks = hotspots[:7]  # Show top 7 directly
        
        for i, (park, counts) in enumerate(top_parks, 1):
            open_count = counts["open"]
            closed_count = counts["closed"]
            total_count = counts["total"]
            
            if open_count > 0:
                msg += f"{i}. {park} - {open_count} open"
            else:
                msg += f"{i}. {park} - {total_count} total"
            
            # Show top complaint type
            types = park_types.get(park, {})
            if types:
                top_type = max(types.items(), key=lambda x: x[1])
                msg += f" ({top_type[0]})"
            
            msg += "\n"
        
        if len(hotspots) > 7:
            msg += f"... and {len(hotspots) - 7} more parks\n"
        
        msg += "\n"

    # Top complaint types
    if type_counts:
        msg += f"Top Complaint Types:\n"
        for label, counts in sorted(type_counts.items(), key=lambda x: -x[1]["total"])[:4]:
            count = counts["total"]
            open_count = counts.get("open", 0)
            msg += f" {label}: {count} ({open_count} open)\n"

    msg += "\nSource: Austin Open311 API"
    return msg


def format_resolution(data: dict) -> str:
    if not data.get("averages"):
        return "Not enough closed complaints to calculate response times."

    msg = f"Park Maintenance Resolution Times\n"
    msg += f"Based on {data['total_closed']} closed complaints (last {data['days_back']} days)\n\n"

    if data.get("overall_avg") is not None:
        msg += f"Overall average: {data['overall_avg']} days\n\n"

    msg += "By complaint type:\n"
    for label, avg in sorted(data["averages"].items(), key=lambda x: x[1]):
        # Emoji based on speed
        if avg <= 7:
            speed = ""
        elif avg <= 30:
            speed = ""
        else:
            speed = ""
        msg += f" {speed} {label}: {avg} days avg\n"

    msg += "\nSource: Austin Open311 API"
    return msg


# =============================================================================
# MAP GENERATOR
# =============================================================================

# Type-filter sidebar injected into every generated parks map.
# The JS scans marker tooltips at runtime (format: "Open: Grounds Maintenance")
# and builds a per-type index so markers can be added/removed from clusters.
_TYPE_FILTER_HTML = r"""
    <div id="type-sidebar" style="position:absolute;right:10px;top:50%;transform:translateY(-50%);background:white;padding:10px 12px;border-radius:6px;box-shadow:0 2px 6px rgba(0,0,0,0.3);z-index:9999;font-family:sans-serif;max-height:80vh;overflow-y:auto;min-width:168px;display:none;">
      <div style="font-size:12px;font-weight:700;color:#333;margin-bottom:5px;border-bottom:1px solid #eee;padding-bottom:4px;">Filter by Type</div>
      <div id="type-btn-list"></div>
      <div style="display:flex;gap:4px;margin-top:6px;">
        <button onclick="selectAllTypes()" class="fbtn" style="flex:1;font-size:10px;padding:2px 6px;">All</button>
        <button onclick="selectNoTypes()" class="fbtn" style="flex:1;font-size:10px;padding:2px 6px;">None</button>
      </div>
    </div>
    <style>
      .type-filter-btn {display:flex;align-items:center;justify-content:space-between;width:100%;margin-bottom:3px;padding:3px 7px;text-align:left;}
    </style>
    <script>
    (function(){
    var _tm={},_tt={},_at=new Set(),_built=false;
    var _groups=[
      {g:'Grounds',   types:['Grounds Maintenance','Grounds Plumbing','Grounds Electrical']},
      {g:'Buildings', types:['Building Plumbing','Building Issues','Building A/C & Heating','Building Electric']},
      {g:'Other',     types:['Commercial Use of Parkland','Park Cemeteries']}
    ];
    var _icons={'Grounds Maintenance':'\u{1F33F}','Grounds Plumbing':'\u{1F4A7}','Grounds Electrical':'⚡',
      'Building Plumbing':'\u{1F527}','Building Issues':'\u{1F3D7}',
      'Building A/C & Heating':'❄','Building Electric':'\u{1F4A1}',
      'Commercial Use of Parkland':'\u{1F3EA}','Park Cemeteries':'⚰'};
    var _short={'Grounds Maintenance':'Grounds Maint.','Grounds Plumbing':'Grnds Plumbing',
      'Grounds Electrical':'Grnds Electric',
      'Building Plumbing':'Bldg Plumbing','Building Issues':'Bldg Issues',
      'Building A/C & Heating':'Bldg A/C & Heat','Building Electric':'Bldg Electric',
      'Commercial Use of Parkland':'Commercial Use','Park Cemeteries':'Cemeteries'};

    function _buildIdx(){
      if(!window.layerMap||!window.leafletMap){setTimeout(_buildIdx,500);return;}
      _tm={};_tt={};
      Object.keys(window.layerMap).forEach(function(fk){
        var p=fk.split('_'),fs=p[0],fb=p[1];
        window.layerMap[fk].eachLayer(function(child){
          if(!child.eachLayer)return;
          child.eachLayer(function(mk){
            var tip=mk.getTooltip?mk.getTooltip():null;
            var raw=tip?(tip.getContent()+''):'';
            var text=raw.replace(/<[^>]+>/g,'').trim().replace(/\s+/g,' ');
            var m=text.match(/^(?:Open|Closed): (.+)$/);
            var lbl=m?m[1]:'Other';
            if(!_tm[lbl])_tm[lbl]=[];
            _tm[lbl].push({cluster:child,marker:mk,fs:fs,fb:fb});
            if(!_tt[lbl])_tt[lbl]={'30':{open:0,closed:0},'60':{open:0,closed:0},'90':{open:0,closed:0}};
            _tt[lbl][fb][fs]=(_tt[lbl][fb][fs]||0)+1;
          });
        });
      });
      _at=new Set(Object.keys(_tm));
      _built=true;
      _buildPanel();
      window.updateSummary();
    }

    function _cnt(lbl){
      var b=_tt[lbl];if(!b)return 0;
      var n=0;
      ['30','60','90'].forEach(function(bk){
        if(parseInt(bk)<=window.currentDays&&b[bk]){
          if(window.showOpen)n+=b[bk].open||0;
          if(window.showClosed)n+=b[bk].closed||0;
        }
      });
      return n;
    }

    function _buildPanel(){
      var c=document.getElementById('type-btn-list');if(!c)return;
      c.innerHTML='';
      _groups.forEach(function(grp){
        if(!grp.types.some(function(t){return _tm[t];}))return;
        var hdr=document.createElement('div');
        hdr.style.cssText='font-size:10px;font-weight:700;color:#888;text-transform:uppercase;letter-spacing:.04em;margin:5px 0 2px;';
        hdr.textContent=grp.g;c.appendChild(hdr);
        grp.types.forEach(function(lbl){
          if(!_tm[lbl])return;
          var btn=document.createElement('button');
          btn.id='tbtn-'+lbl.replace(/[^a-zA-Z0-9]/g,'_');
          btn.className='fbtn type-filter-btn active';
          btn.innerHTML=(_icons[lbl]||'•')+' '+(_short[lbl]||lbl)+
            ' <span class="tc" style="font-size:10px;opacity:.7;margin-left:4px;">'+_cnt(lbl)+'</span>';
          btn.onclick=(function(l){return function(){window.toggleType(l);};})(lbl);
          c.appendChild(btn);
        });
      });
      document.getElementById('type-sidebar').style.display='';
    }

    function _updBtns(){
      Object.keys(_tm).forEach(function(lbl){
        var btn=document.getElementById('tbtn-'+lbl.replace(/[^a-zA-Z0-9]/g,'_'));
        if(!btn)return;
        btn.classList.toggle('active',_at.has(lbl));
        var tc=btn.querySelector('.tc');if(tc)tc.textContent=_cnt(lbl);
      });
    }

    window.toggleType=function(lbl){
      if(_at.has(lbl)){
        _at.delete(lbl);
        (_tm[lbl]||[]).forEach(function(e){try{e.cluster.removeLayer(e.marker);}catch(x){}});
      }else{
        _at.add(lbl);
        (_tm[lbl]||[]).forEach(function(e){try{e.cluster.addLayer(e.marker);}catch(x){}});
      }
      _updBtns();window.updateSummary();
    };
    window.selectAllTypes=function(){Object.keys(_tm).forEach(function(l){if(!_at.has(l))window.toggleType(l);});};
    window.selectNoTypes =function(){Object.keys(_tm).forEach(function(l){if(_at.has(l)) window.toggleType(l);});};

    window.updateSummary=function(){
      if(!_built){
        var d=String(window.currentDays),ct=window.bucketCounts[d]||{};
        var o=window.showOpen?(ct.open||0):0,c=window.showClosed?(ct.closed||0):0;
        document.getElementById('map-summary').textContent='Last '+d+' days · '+(o+c)+' total · '+o+' open · '+c+' closed';
        return;
      }
      var tot=0,opn=0,cls=0;
      _at.forEach(function(t){
        var b=_tt[t];if(!b)return;
        ['30','60','90'].forEach(function(bk){
          if(parseInt(bk)<=window.currentDays&&b[bk]){
            if(window.showOpen)  {opn+=b[bk].open||0;  tot+=b[bk].open||0;}
            if(window.showClosed){cls+=b[bk].closed||0;tot+=b[bk].closed||0;}
          }
        });
      });
      document.getElementById('map-summary').textContent='Last '+window.currentDays+'d · '+tot+' shown · '+opn+' open · '+cls+' closed';
    };

    var _osd=window.setDayFilter,_ots=window.toggleStatus;
    window.setDayFilter=function(days){_osd(days);if(_built)_updBtns();};
    window.toggleStatus=function(s){_ots(s);if(_built)_updBtns();};

    document.addEventListener('DOMContentLoaded',function(){setTimeout(_buildIdx,1500);});
    })();
    </script>
"""

def generate_parks_map(days_back: int = 90) -> tuple:
    """Generate an interactive HTML map of park maintenance complaints.

    Returns:
        tuple: (BytesIO buffer with HTML content, summary message)
    """
    try:
        import folium
        from folium.plugins import MarkerCluster
    except ImportError:
        return None, "❌ Map generation requires 'folium'. Install: pip install folium"

    records_raw = fetch_all_park_complaints(days_back)

    now_dt = _utc_now()
    records = []
    for r in records_raw:
        try:
            lat = float(r.get("lat") or 0)
            lon = float(r.get("long") or 0)
            if 30.0 <= lat <= 30.5 and -98.0 <= lon <= -97.5:
                r["_lat"] = lat
                r["_lon"] = lon
                records.append(r)
        except (TypeError, ValueError):
            pass

    if not records:
        return None, "🏞️ No park maintenance reports with location data found."

    open_count = sum(1 for r in records if (r.get("status") or "").lower() == "open")
    closed_count = len(records) - open_count

    def _age_days(r):
        try:
            dt = datetime.fromisoformat(r.get("requested_datetime", "").replace("Z", "+00:00"))
            return (now_dt - dt).days
        except Exception:
            return days_back

    bucket_counts = {"30": {"open": 0, "closed": 0}, "60": {"open": 0, "closed": 0}, "90": {"open": 0, "closed": 0}}
    for r in records:
        age = _age_days(r)
        status = (r.get("status") or "").lower()
        s = status if status in ("open", "closed") else "closed"
        if age <= 30:
            bucket_counts["30"][s] += 1
        if age <= 60:
            bucket_counts["60"][s] += 1
        if age <= 90:
            bucket_counts["90"][s] += 1
    counts_js = str(bucket_counts).replace("'", '"')

    m = folium.Map(location=[30.2672, -97.7431], zoom_start=11, tiles="CartoDB positron")
    m.get_root().header.add_child(folium.Element(og_meta_tags("parks")))

    fg_clusters = {}
    fg_objects = {}
    for status_key in ("open", "closed"):
        for bucket in ("30", "60", "90"):
            name = f"{status_key}_{bucket}"
            show = (bucket == "90")
            fg = folium.FeatureGroup(name=name, show=show, overlay=True)
            cluster = MarkerCluster().add_to(fg)
            fg.add_to(m)
            fg_clusters[name] = cluster
            fg_objects[name] = fg

    for r in records:
        lat = r["_lat"]
        lon = r["_lon"]
        status = (r.get("status") or "").lower()
        service_label = r.get("_service_label", "Park Maintenance")
        description = (r.get("description") or "").strip()
        status_notes = (r.get("status_notes") or "").strip()
        date_str = (r.get("requested_datetime") or "").split("T")[0]
        updated_str = (r.get("updated_datetime") or "").split("T")[0]
        address = (r.get("address") or "").strip()
        req_id = r.get("service_request_id", "N/A")

        age = _age_days(r)
        bucket = "30" if age <= 30 else ("60" if age <= 60 else "90")
        cluster_key = f"{status}_{bucket}"
        if cluster_key not in fg_clusters:
            cluster_key = f"closed_{bucket}"

        address_line = f'<b>Address:</b> <a href="https://www.google.com/maps/search/?api=1&query={lat},{lon}" target="_blank">{address}</a><br/>' if address else ""
        updated_line = f"<span style='color:#666;'>Updated: {updated_str}</span><br/>" if updated_str and updated_str != date_str else ""
        desc_text = description or status_notes
        desc_short = (desc_text[:500] + "...") if len(desc_text) > 500 else desc_text
        desc_block = f"<b>Description:</b><br/><i>{desc_short.replace(chr(10), '<br/>')}</i><br/>" if desc_short else ""

        ticket_url = f"https://311.austintexas.gov/tickets/{req_id}"
        popup_html = f"""
        <div style="font-family:sans-serif;max-width:300px;">
            <b><a href="{ticket_url}" target="_blank" style="color:#0066cc;">Report #{req_id}</a></b><br/>
            <span style="color:#666;">Filed: {date_str}</span><br/>
            {updated_line}
            {address_line}
            <br/>
            <b>Status:</b> {'🔴 Open' if status == 'open' else '🟢 Closed'}<br/>
            <b>Type:</b> {service_label}<br/><br/>
            {desc_block}
        </div>
        """
        popup = folium.Popup(popup_html, max_width=300)
        if status == "open":
            icon = folium.Icon(color="red", icon="exclamation-sign", prefix="glyphicon")
            tooltip = f"Open: {service_label}"
        else:
            icon = folium.Icon(color="green", icon="ok-sign", prefix="glyphicon")
            tooltip = f"Closed: {service_label}"

        folium.Marker(location=[lat, lon], popup=popup, icon=icon, tooltip=tooltip).add_to(fg_clusters[cluster_key])

    map_var = m.get_name()
    layer_map_js = "{" + ", ".join(f'"{k}": {fg_objects[k].get_name()}' for k in fg_objects) + "}"
    panel_html = f"""
    <div id="map-panel" style="position:absolute;top:10px;left:50%;transform:translateX(-50%);
                background:white;padding:10px 16px;border-radius:6px;
                box-shadow:0 2px 6px rgba(0,0,0,0.3);z-index:9999;
                font-family:sans-serif;text-align:center;">
        <b style="font-size:15px;">🏞️ Austin Park Maintenance 311 Reports</b><br/>
        <span id="map-summary" style="font-size:12px;color:#555;"></span>
        <div style="display:flex;justify-content:center;gap:4px;margin-top:7px;">
            <button id="btn-30" onclick="setDayFilter(30)" class="fbtn">30d</button>
            <button id="btn-60" onclick="setDayFilter(60)" class="fbtn">60d</button>
            <button id="btn-90" onclick="setDayFilter(90)" class="fbtn active">90d</button>
            <span style="margin:0 4px;color:#ccc;">|</span>
            <button id="btn-open" onclick="toggleStatus('open')" class="fbtn active">🔴 Open</button>
            <button id="btn-closed" onclick="toggleStatus('closed')" class="fbtn active">🟢 Closed</button>
        </div>
    </div>
    <style>
        .fbtn {{ padding:3px 9px;border:1px solid #ccc;border-radius:4px;background:#f5f5f5;cursor:pointer;font-size:12px;color:#444; }}
        .fbtn.active {{ background:#2563eb;color:white;border-color:#2563eb; }}
        .fbtn:hover:not(.active) {{ background:#e0e7ff; }}
    </style>
    <script>
        var currentDays = 90;
        var showOpen = true;
        var showClosed = true;
        var layerMap = null;
        var leafletMap = null;
        var bucketCounts = {counts_js};

        function updateSummary() {{
            var d = String(currentDays);
            var counts = bucketCounts[d] || {{}};
            var o = showOpen ? (counts.open || 0) : 0;
            var c = showClosed ? (counts.closed || 0) : 0;
            document.getElementById('map-summary').textContent =
                'Last ' + d + ' days \u00b7 ' + (o + c) + ' total \u00b7 ' + o + ' open \u00b7 ' + c + ' closed';
        }}

        function initLayers() {{
            layerMap = {layer_map_js};
            leafletMap = {map_var};
            updateLayers();
            updateSummary();
            var params = new URLSearchParams(window.location.search);
            var lat = parseFloat(params.get('lat'));
            var lon = parseFloat(params.get('lon'));
            if (lat && lon) {{ leafletMap.setView([lat, lon], 17); }}
        }}

        function updateLayers() {{
            if (!layerMap || !leafletMap) return;
            Object.keys(layerMap).forEach(function(key) {{
                var parts = key.split('_');
                var status = parts[0];
                var bucket = parseInt(parts[1]);
                var timeOk = bucket <= currentDays;
                var statusOk = (status === 'open' && showOpen) || (status === 'closed' && showClosed);
                var layer = layerMap[key];
                if (timeOk && statusOk) {{
                    if (!leafletMap.hasLayer(layer)) leafletMap.addLayer(layer);
                }} else {{
                    if (leafletMap.hasLayer(layer)) leafletMap.removeLayer(layer);
                }}
            }});
        }}

        function setDayFilter(days) {{
            currentDays = days;
            [30, 60, 90].forEach(function(d) {{
                var btn = document.getElementById('btn-' + d);
                if (btn) btn.classList.toggle('active', d === days);
            }});
            updateLayers();
            updateSummary();
        }}

        function toggleStatus(status) {{
            if (status === 'open') showOpen = !showOpen;
            else showClosed = !showClosed;
            document.getElementById('btn-' + status).classList.toggle('active');
            updateLayers();
            updateSummary();
        }}

        document.addEventListener('DOMContentLoaded', function() {{
            setTimeout(initLayers, 1000);
        }});
    </script>
    """
    m.get_root().html.add_child(folium.Element(panel_html + _TYPE_FILTER_HTML))

    with tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False) as tmp:
        tmp_path = tmp.name
    try:
        m.save(tmp_path)
        with open(tmp_path, 'rb') as f:
            html_content = f.read()
        buffer = io.BytesIO(html_content)
        buffer.seek(0)
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass

    summary = (
        f"🏞️ *Park Maintenance Map*\n"
        f"_Last {days_back} days_\n\n"
        f"📊 *{len(records):,} reports mapped*\n"
        f"🔴 *{open_count:,} open*  ·  🟢 *{closed_count:,} closed*\n\n"
        f"Tap markers to see details. Use buttons to filter by time window."
    )
    return buffer, summary


# =============================================================================
# PARKS HUB PAGE — searchable landing page at docs/parks/index.html
# =============================================================================

def generate_parks_hub(days_back: int = 90) -> tuple[Optional[io.BytesIO], str]:
    """Generate a searchable hub page listing all parks with recent 311 activity."""
    data = get_park_hotspots(days_back)
    hotspots = data.get("hotspots", [])
    park_types = data.get("park_types", {})
    park_coords = data.get("park_coords", {})
    total_reports = data.get("total", 0)
    total_open = sum(c["open"] for _, c in hotspots)
    park_count = len(hotspots)

    if not hotspots:
        return None, "No park data available."

    import json as _json

    parks_json = []
    for park_name, counts in hotspots:
        types_dict = park_types.get(park_name, {})
        top_type = max(types_dict.items(), key=lambda x: x[1])[0] if types_dict else ""
        coords = park_coords.get(park_name)
        entry = {
            "name": park_name,
            "open": counts["open"],
            "closed": counts["closed"],
            "total": counts["total"],
            "top_type": top_type,
            "types": types_dict,
        }
        if coords:
            entry["lat"] = round(float(coords[0]), 6)
            entry["lon"] = round(float(coords[1]), 6)
        parks_json.append(entry)

    parks_data_js = _json.dumps(parks_json, ensure_ascii=False)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Austin Parks — 311 Maintenance Reports</title>
  <meta property="og:type" content="website" />
  <meta property="og:title" content="Austin Parks — 311 Maintenance Reports" />
  <meta property="og:description" content="Search {park_count} Austin parks to see open maintenance issues, complaint history, and resolution trends." />
  <meta property="og:url" content="https://austin311.com/parks/" />
  <meta property="og:image" content="https://austin311.com/og-default.png" />
  <meta property="og:site_name" content="Austin 311" />
  <meta name="twitter:card" content="summary_large_image" />
  <meta name="description" content="Search Austin parks to see open maintenance issues reported via 311." />
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    :root {{
      --bg:           #f8fafc;
      --bg-card:      #ffffff;
      --bg-card-hover:#f1f5f9;
      --bg-search:    #ffffff;
      --border:       #e2e8f0;
      --border-hover: #94a3b8;
      --text:         #1e293b;
      --text-head:    #0f172a;
      --text-muted:   #64748b;
      --text-label:   #0f172a;
      --text-footer:  #94a3b8;
      --accent:       #2563eb;
      --open-bg:      #fee2e2;
      --open-text:    #dc2626;
      --bar-bg:       #e2e8f0;
      --bar-fill:     #2563eb;
    }}
    html.dark {{
      --bg:           #0f1117;
      --bg-card:      #1e2230;
      --bg-card-hover:#252b3b;
      --bg-search:    #1e2230;
      --border:       #2d3348;
      --border-hover: #4a5568;
      --text:         #e2e8f0;
      --text-head:    #f8fafc;
      --text-muted:   #64748b;
      --text-label:   #f1f5f9;
      --text-footer:  #475569;
      --open-bg:      #3b1f1f;
      --open-text:    #f87171;
      --bar-bg:       #2d3348;
      --bar-fill:     #3b82f6;
    }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg); color: var(--text);
      min-height: 100vh; display: flex; flex-direction: column; align-items: center;
      padding: 2.5rem 1rem 3rem; transition: background 0.2s, color 0.2s;
    }}
    #theme-toggle {{
      position: fixed; top: 14px; right: 16px; z-index: 200;
      background: var(--bg-card); border: 1px solid var(--border); border-radius: 6px;
      padding: 5px 10px; font-size: 0.75rem; color: var(--text-muted);
      cursor: pointer; transition: background 0.15s; user-select: none;
    }}
    .page-wrap {{ width: 100%; max-width: 860px; }}
    header {{ text-align: center; margin-bottom: 2rem; }}
    header h1 {{ font-size: 2rem; font-weight: 800; color: var(--text-head); margin-bottom: 0.4rem; }}
    header p {{ color: var(--text-muted); font-size: 0.95rem; }}
    .stat-row {{
      display: flex; justify-content: center; gap: 2rem;
      margin: 1.2rem 0; flex-wrap: wrap;
    }}
    .stat {{ text-align: center; }}
    .stat .val {{ font-size: 1.6rem; font-weight: 700; color: var(--text-head); line-height: 1; }}
    .stat .lbl {{ font-size: 0.78rem; color: var(--text-muted); margin-top: 2px; }}
    .controls {{
      display: flex; align-items: center; gap: 0.75rem;
      margin-bottom: 1.5rem; flex-wrap: wrap;
    }}
    #park-search {{
      flex: 1; min-width: 200px; padding: 0.55rem 0.9rem;
      border: 1px solid var(--border); border-radius: 8px;
      background: var(--bg-search); color: var(--text);
      font-size: 0.95rem; outline: none; transition: border-color 0.15s;
    }}
    #park-search:focus {{ border-color: var(--accent); }}
    #match-count {{ font-size: 0.82rem; color: var(--text-muted); white-space: nowrap; }}
    .map-btn {{
      display: inline-block; padding: 0.5rem 1rem;
      background: var(--accent); color: white; border-radius: 8px;
      font-size: 0.88rem; font-weight: 600; text-decoration: none;
      white-space: nowrap; transition: opacity 0.15s;
    }}
    .map-btn:hover {{ opacity: 0.85; }}
    #park-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
      gap: 0.85rem;
    }}
    .park-card {{
      background: var(--bg-card); border: 1px solid var(--border);
      border-radius: 10px; padding: 0.9rem 1rem;
      cursor: pointer; transition: border-color 0.15s, background 0.15s;
    }}
    .park-card:hover {{ border-color: var(--border-hover); background: var(--bg-card-hover); }}
    .park-card.expanded {{ border-color: var(--accent); }}
    .card-header {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 0.5rem; }}
    .park-name {{ font-weight: 700; font-size: 0.97rem; color: var(--text-label); line-height: 1.25; }}
    .open-badge {{
      flex-shrink: 0; font-size: 0.72rem; font-weight: 700;
      background: var(--open-bg); color: var(--open-text);
      padding: 2px 7px; border-radius: 99px;
    }}
    .card-meta {{ margin-top: 0.35rem; font-size: 0.8rem; color: var(--text-muted); }}
    .top-type {{
      margin-top: 0.4rem; font-size: 0.78rem; color: var(--text-muted);
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }}
    .type-breakdown {{
      display: none; margin-top: 0.75rem;
      border-top: 1px solid var(--border); padding-top: 0.65rem;
    }}
    .park-card.expanded .type-breakdown {{ display: block; }}
    .type-row {{ margin-bottom: 0.45rem; }}
    .type-row-label {{ font-size: 0.78rem; color: var(--text-muted); margin-bottom: 2px;
      display: flex; justify-content: space-between; }}
    .type-bar-track {{
      height: 5px; background: var(--bar-bg); border-radius: 99px; overflow: hidden;
    }}
    .type-bar-fill {{
      height: 100%; background: var(--bar-fill); border-radius: 99px; transition: width 0.3s;
    }}
    .map-link-btn {{
      display: inline-block; margin-top: 0.65rem;
      padding: 0.35rem 0.8rem; background: var(--accent); color: white;
      border-radius: 6px; font-size: 0.78rem; font-weight: 600;
      text-decoration: none; transition: opacity 0.15s;
    }}
    .map-link-btn:hover {{ opacity: 0.82; }}
    .no-results {{
      text-align: center; color: var(--text-muted); padding: 3rem 0; grid-column: 1 / -1;
    }}
    footer {{
      margin-top: 3rem; font-size: 0.78rem; color: var(--text-footer); text-align: center;
    }}
    footer a {{ color: inherit; text-decoration: underline; }}
    footer a:hover {{ color: var(--border-hover); }}
  </style>
</head>
<body>
<button id="theme-toggle">🌙 Dark</button>
<div class="page-wrap">
  <header>
    <h1>🏞️ Austin Parks</h1>
    <p>311 maintenance reports · last {days_back} days</p>
    <div class="stat-row">
      <div class="stat"><div class="val">{total_reports:,}</div><div class="lbl">Total reports</div></div>
      <div class="stat"><div class="val">{total_open:,}</div><div class="lbl">Open issues</div></div>
      <div class="stat"><div class="val">{park_count:,}</div><div class="lbl">Parks with activity</div></div>
    </div>
  </header>

  <div class="controls">
    <input type="search" id="park-search" placeholder="Search parks…" autocomplete="off" />
    <span id="match-count">{park_count} parks</span>
    <a class="map-btn" href="./map/">View all on map →</a>
  </div>

  <div id="park-grid"></div>
</div>

<footer>
  Data: <a href="https://311.austintexas.gov" target="_blank" rel="noopener">Austin 311</a>
  &nbsp;·&nbsp;
  <a href="https://austin311.com" target="_blank" rel="noopener">austin311.com</a>
</footer>

<script>
  var PARKS = {parks_data_js};

  function renderCards(list) {{
    var grid = document.getElementById('park-grid');
    document.getElementById('match-count').textContent = list.length + ' park' + (list.length === 1 ? '' : 's');
    if (!list.length) {{
      grid.innerHTML = '<div class="no-results">No parks match your search.</div>';
      return;
    }}
    grid.innerHTML = list.map(function(p, i) {{
      var openBadge = p.open > 0
        ? '<span class="open-badge">' + p.open + ' open</span>'
        : '';
      var topType = p.top_type ? '<div class="top-type">🔧 ' + p.top_type + '</div>' : '';
      var breakdown = buildBreakdown(p.types, p.lat, p.lon);
      return '<div class="park-card" id="card-' + i + '" onclick="toggleCard(this)">' +
        '<div class="card-header">' +
          '<div class="park-name">' + p.name + '</div>' +
          openBadge +
        '</div>' +
        '<div class="card-meta">' + p.total + ' report' + (p.total === 1 ? '' : 's') +
          (p.open > 0 ? ' · ' + p.closed + ' resolved' : ' · all resolved') + '</div>' +
        topType +
        '<div class="type-breakdown">' + breakdown + '</div>' +
      '</div>';
    }}).join('');
  }}

  function buildBreakdown(types, lat, lon) {{
    if (!types || !Object.keys(types).length) return '';
    var entries = Object.entries(types).sort(function(a,b){{return b[1]-a[1];}});
    var max = entries[0][1];
    var bars = entries.map(function(e) {{
      var pct = Math.round(e[1] / max * 100);
      return '<div class="type-row">' +
        '<div class="type-row-label"><span>' + e[0] + '</span><span>' + e[1] + '</span></div>' +
        '<div class="type-bar-track"><div class="type-bar-fill" style="width:' + pct + '%"></div></div>' +
      '</div>';
    }}).join('');
    var mapBtn = (lat && lon)
      ? '<a class="map-link-btn" href="./map/?lat=' + lat + '&lon=' + lon + '" onclick="event.stopPropagation()">📍 View on map</a>'
      : '';
    return bars + mapBtn;
  }}

  function toggleCard(el) {{
    el.classList.toggle('expanded');
  }}

  var _current = PARKS;
  document.getElementById('park-search').addEventListener('input', function() {{
    var q = this.value.trim().toLowerCase();
    _current = q ? PARKS.filter(function(p) {{ return p.name.toLowerCase().indexOf(q) !== -1; }}) : PARKS;
    renderCards(_current);
  }});

  renderCards(PARKS);

  var btn = document.getElementById('theme-toggle');
  var html = document.documentElement;
  if (localStorage.getItem('theme') === 'dark') {{ html.classList.add('dark'); btn.textContent = '☀️ Light'; }}
  btn.addEventListener('click', function() {{
    var d = html.classList.toggle('dark');
    btn.textContent = d ? '☀️ Light' : '🌙 Dark';
    localStorage.setItem('theme', d ? 'dark' : 'light');
  }});
</script>
</body>
</html>"""

    buffer = io.BytesIO(html.encode("utf-8"))
    buffer.seek(0)
    return buffer, f"🏞️ Parks hub: {park_count} parks · {total_reports:,} reports · {total_open} open"
