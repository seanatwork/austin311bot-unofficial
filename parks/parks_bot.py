"""
Parks Maintenance — data layer and formatters.

Queries Austin Open311 API live for all park-related service requests.
Provides hotspot analysis (parks with most unresolved complaints), complaint type stats,
and response time analysis.

Service codes (from 311categories.txt):
- PRGRDISS: Park Maintenance - Grounds (36,598)
- PRGRDPLB: Park Maintenance - Grounds Plumbing Issues (7,324)
- PRGRDELC: Park Maintenance - Grounds Electrical Issues (4,514)
- PATRISPA: Park - Tree Issues (4,409)
- PRBLDPLB: Park Maintenance - Building Plumbing Issues (2,767)
- PRBLDISS: Park Building Issues (1,478)
- PRBLDACH: Parks - Building A/C & Heating Issues (958)
- PRBLDELE: Parks - Building Electric Issues (532)
- COMPARLN: Commercial Use of Parkland (356)
- PRCEMET1: Park Cemeteries (343)
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

logger = logging.getLogger(__name__)

OPEN311_BASE_URL = "https://311.austintexas.gov/open311/v2"
TIMEOUT = 10
MAX_RETRIES = 3
RETRY_DELAY = 1.0
MAX_PAGES = 15  # cap at 1,500 records per code for performance

# API key from environment
API_KEY = os.getenv("AUSTIN_APP_TOKEN")

# Park service codes and human-readable labels
SERVICE_CODES = {
    "PRGRDISS": "Grounds Maintenance",
    "PRGRDPLB": "Grounds Plumbing",
    "PRGRDELC": "Grounds Electrical",
    "PATRISPA": "Tree Issues",
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


def fetch_all_park_complaints(days_back: int = 90) -> list:
    """Fetch complaints across all park service codes."""
    all_records = []
    for code in SERVICE_CODES:
        try:
            records = _fetch_code(code, days_back, limit=100)
            all_records.extend(records)
            logger.debug(f"{code}: {len(records)} records")
        except Exception as e:
            logger.warning(f"Failed to fetch {code}: {e}")
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


def build_park_name_keyboard(hotspots_data: dict, days: int):
    """Build keyboard with actual park names for top parks."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    
    hotspots = hotspots_data.get("hotspots", [])
    if not hotspots:
        return InlineKeyboardMarkup([[]])
    
    # Show top 7 parks with their actual names
    top_parks = hotspots[:7]
    keyboard = []
    
    for park, counts in top_parks:
        open_count = counts["open"]
        if open_count > 0:
            label = f"{park} ({open_count} open)"
        else:
            label = f"{park} ({counts['total']} total)"
        
        # Use park name directly in callback data
        callback_data = f"parks_detail_{park.replace(' ', '_')}_{days}"
        keyboard.append([InlineKeyboardButton(label, callback_data=callback_data)])
    
    # Add "See more" option if there are more parks
    if len(hotspots) > 7:
        keyboard.append([InlineKeyboardButton(f"See more parks ({len(hotspots) - 7} remaining)", callback_data=f"parks_more_{days}")])
    
    return InlineKeyboardMarkup(keyboard)


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

        address_line = f"<b>Address:</b> {address}<br/>" if address else ""
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
    m.get_root().html.add_child(folium.Element(panel_html))

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
