"""
Parking Enforcement — data layer and formatters.

Queries Austin Open311 API live for PARKINGV (Parking Violation Enforcement) service requests.
"""

import time
import logging
import os
import io
import requests
from datetime import datetime, timezone, timedelta
from open311_client import open311_get, og_meta_tags
from typing import Optional
from collections import defaultdict

logger = logging.getLogger(__name__)

OPEN311_BASE_URL = "https://311.austintexas.gov/open311/v2"
SERVICE_CODE = "PARKINGV"
TIMEOUT = 45
MAX_RETRIES = 8
RETRY_DELAY = 1.0
MAX_PAGES = 100  # 90 days can exceed 2,000 records; cap at 10,000

# API key from environment
API_KEY = os.getenv("AUSTINAPIKEY")

# Austin local time approximation (CDT = UTC-5, CST = UTC-6; use -6 as conservative default)
_AUSTIN_OFFSET = timedelta(hours=-6)

RETRYABLE_HTTP_CODES = {423, 429, 500, 502, 503, 504}

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


def _looks_truncated(text: str | None) -> bool:
    """Return True if a text field appears to have been cut off by the API."""
    if not text:
        return False
    t = text.rstrip()
    if len(t) < 200:
        return False
    return t[-1] not in ".!?,;: \t\n"


def _fetch_detail(service_request_id: str) -> dict:
    """Fetch a single ticket by ID to get untruncated field values and attributes."""
    session = _get_session()
    url = f"{OPEN311_BASE_URL}/requests/{service_request_id}.json"
    try:
        resp = session.get(url, params={"extensions": "true"}, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list) and data:
            return data[0]
        if isinstance(data, dict):
            return data
    except Exception as e:
        logger.debug(f"Detail fetch failed for {service_request_id}: {e}")
    return {}


def _make_request(params: dict) -> list:
    return open311_get(_get_session(), f"{OPEN311_BASE_URL}/requests.json", params)


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
            "extensions": "true",
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
        time.sleep(1.0 if API_KEY else 2.0)

    # Re-fetch individual records whose text fields look truncated
    for i, r in enumerate(all_records):
        if _looks_truncated(r.get("description")) or _looks_truncated(r.get("status_notes")):
            sid = r.get("service_request_id")
            if not sid:
                continue
            detail = _fetch_detail(sid)
            if detail:
                for field in ("description", "status_notes"):
                    if detail.get(field):
                        r[field] = detail[field]

    return all_records


def fetch_parking_monthly(months_back: int = 12, use_cache: bool = True) -> list:
    """Fetch parking complaint records month-by-month with optional caching.

    With caching enabled (default), this will:
    1. Load cached records from SQLite
    2. Only fetch new records from Open311 API
    3. Cache new records for future runs

    The Open311 API returns records in chronological order (oldest first), so a
    single 365-day request only returns the oldest ~90 days before hitting the
    per-page cap. Fetching month by month ensures every period is fully covered.

    Args:
        months_back: Number of months to fetch
        use_cache: Whether to use SQLite caching (default True)

    Returns:
        A flat list of parking complaint records across all months.
    """
    from open311_cache import init_cache, get_cached_records, cache_records, get_last_fetch_date

    CATEGORY = "parking"

    # Initialize cache if using
    if use_cache:
        init_cache()
        cached_records = get_cached_records(CATEGORY, service_codes=[SERVICE_CODE])
        cached_ids = {r.get("service_request_id") for r in cached_records}
        logger.info(f"Loaded {len(cached_records)} cached records")

        # Check if we have recent cache
        last_fetch = get_last_fetch_date(CATEGORY)
        if last_fetch:
            logger.info(f"Last fetch was at {last_fetch}")
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

    # Calculate how far back we need to fetch
    if use_cache and cached_records:
        last_fetch = get_last_fetch_date(CATEGORY)
        if last_fetch:
            fetch_start = last_fetch - timedelta(days=1)
        else:
            fetch_start = now - timedelta(days=30 * months_back)
    else:
        fetch_start = now - timedelta(days=30 * months_back)

    logger.info(f"Fetching records from {fetch_start} to {now}")

    # Calculate months to fetch
    current_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    start_month = fetch_start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    months_to_fetch = []
    while start_month <= current_month:
        months_to_fetch.append(start_month)
        if start_month.month == 12:
            start_month = start_month.replace(year=start_month.year + 1, month=1)
        else:
            start_month = start_month.replace(month=start_month.month + 1)

    logger.info(f"Will fetch {len(months_to_fetch)} months of data")

    for month_start in reversed(months_to_fetch):  # Newest first
        # Determine month end
        if month_start.year == now.year and month_start.month == now.month:
            month_end = now
        else:
            if month_start.month == 12:
                month_end = month_start.replace(year=month_start.year + 1, month=1)
            else:
                month_end = month_start.replace(month=month_start.month + 1)

        page = 1
        monthly_records = 0
        while page <= MAX_PAGES:
            params = {
                "service_code": SERVICE_CODE,
                "start_date": _isoformat_z(month_start),
                "end_date": _isoformat_z(month_end),
                "per_page": 100,
                "page": page,
                "extensions": "true",
            }

            try:
                records = _make_request(params)
            except Exception as e:
                logger.warning(f"API error for {month_start}: {e}")
                break

            if not records:
                break

            for r in records:
                sid = r.get("service_request_id")
                if sid and sid not in seen_ids:
                    seen_ids.add(sid)
                    r["_service_label"] = "Parking Violation"
                    r["_service_code"] = SERVICE_CODE
                    all_records.append(r)
                    new_records.append(r)
                    monthly_records += 1

            if len(records) < 100:
                break

            page += 1
            time.sleep(0.5 if API_KEY else 1.0)

        if monthly_records > 0:
            logger.info(f"  {month_start.strftime('%Y-%m')}: {monthly_records} new records")

    # Cache new records
    if use_cache and new_records:
        cache_records(CATEGORY, new_records)
        logger.info(f"Cached {len(new_records)} new records")

    # Return combined cached + new
    if use_cache and cached_records:
        combined = {r.get("service_request_id"): r for r in cached_records}
        for r in all_records:
            combined[r.get("service_request_id")] = r
        result = list(combined.values())
        logger.info(f"Returning {len(result)} total records ({len(cached_records)} cached + {len(new_records)} new)")
        return result

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


# =============================================================================
# INTERACTIVE MAP GENERATION
# =============================================================================

def fetch_parking_with_coords(days_back: int = 30) -> dict:
    """Fetch all parking reports and filter to those with valid coordinates.

    Returns both open AND closed requests with location data for mapping.
    Uses the cached month-by-month fetcher to avoid hitting MAX_PAGES=100.
    """
    months_back = max(1, days_back // 30) + 1
    records = fetch_parking_monthly(months_back)

    # Filter to records with valid coordinates
    located = []
    for r in records:
        lat = r.get("lat")
        lon = r.get("long")
        if lat and lon:
            try:
                lat_f = float(lat)
                lon_f = float(lon)
                # Basic validation: should be in Austin area
                if 30.0 <= lat_f <= 30.5 and -98.0 <= lon_f <= -97.5:
                    r["_lat"] = lat_f
                    r["_lon"] = lon_f
                    located.append(r)
            except (ValueError, TypeError):
                pass

    return {
        "records": located,
        "total": len(located),
        "days_back": days_back,
        "fetched_at": _utc_now().strftime("%Y-%m-%d %H:%M UTC"),
    }


def _extract_violation_type(description: str) -> str:
    """Extract the violation type from description text.
    
    Examples:
        "Parked in bike lane" → "Bike Lane"
        "Car blocking sidewalk" → "Blocking Sidewalk"
        "Black SUV parked blocking driveway" → "Blocking Driveway"
    """
    if not description:
        return ""
    
    desc_lower = description.lower()
    
    # Define patterns to match common violation types
    violation_patterns = [
        ("bike lane", "Bike Lane"),
        ("bicycle lane", "Bike Lane"),
        ("blocking sidewalk", "Blocking Sidewalk"),
        ("on sidewalk", "On Sidewalk"),
        ("parked on sidewalk", "On Sidewalk"),
        ("blocking driveway", "Blocking Driveway"),
        ("no parking zone", "No Parking Zone"),
        ("commercial parking", "Commercial Zone"),
        ("parked in commercial", "Commercial Zone"),
        ("abandoned vehicle", "Abandoned Vehicle"),
        ("lift abandoned", "Abandoned Vehicle"),
        ("illegal parking", "Illegal Parking"),
        ("living in van", "Overnight Camping"),
        ("overnight parking", "Overnight Parking"),
        ("fire hydrant", "Fire Hydrant"),
        ("handicap space", "Handicap Space"),
        ("ada space", "Handicap Space"),
        ("accessible space", "Handicap Space"),
        ("bus stop", "Bus Stop"),
        ("crosswalk", "Crosswalk"),
        ("sidewalk ramp", "Sidewalk Ramp"),
        ("construction zone", "Construction Zone"),
        ("loading zone", "Loading Zone"),
        ("tow zone", "Tow Zone"),
        ("street sweeping", "Street Sweeping"),
    ]
    
    for pattern, violation_type in violation_patterns:
        if pattern in desc_lower:
            return violation_type
    
    # If no pattern matches, return a shortened version of the description
    if len(description) <= 50:
        return description
    return description[:47] + "..."


def _violation_type_slug(violation_type: str) -> str:
    """Map a violation type label to a URL-safe camelCase slug (no underscores).
    
    Used as a layer-key segment so the map can filter by type via ?type= param.
    """
    _TYPE_SLUGS = {
        "Bike Lane": "bikeLane",
        "Blocking Sidewalk": "blockingSidewalk",
        "On Sidewalk": "onSidewalk",
        "Blocking Driveway": "blockingDriveway",
        "No Parking Zone": "noParkingZone",
        "Commercial Zone": "commercialZone",
        "Abandoned Vehicle": "abandonedVehicle",
        "Illegal Parking": "illegalParking",
        "Overnight Camping": "overnightCamping",
        "Overnight Parking": "overnightParking",
        "Fire Hydrant": "fireHydrant",
        "Handicap Space": "handicapSpace",
        "Bus Stop": "busStop",
        "Crosswalk": "crosswalk",
        "Sidewalk Ramp": "sidewalkRamp",
        "Construction Zone": "constructionZone",
        "Loading Zone": "loadingZone",
        "Tow Zone": "towZone",
        "Street Sweeping": "streetSweeping",
    }
    if violation_type in _TYPE_SLUGS:
        return _TYPE_SLUGS[violation_type]
    # Fallback: slugify the type label (lowercase, no spaces, no special chars)
    import re
    slug = re.sub(r'[^a-zA-Z0-9]', '', violation_type) if violation_type else "other"
    return slug[:30] if slug else "other"


def generate_parking_map(days_back: int = 30) -> tuple[Optional[io.BytesIO], str]:
    """Generate an interactive HTML map of parking reports.

    Returns:
        tuple: (BytesIO buffer with HTML content, summary message)
    """
    try:
        import folium
        from folium.plugins import MarkerCluster
    except ImportError:
        return None, "❌ Map generation requires 'folium' library. Install with: pip install folium"

    data = fetch_parking_with_coords(days_back)
    records = data["records"]
    total = data["total"]

    if not records:
        return None, f"🅿️ No parking reports with location data found in the last {days_back} days."

    # Count by status
    open_count = sum(1 for r in records if (r.get("status") or "").lower() == "open")
    closed_count = sum(1 for r in records if (r.get("status") or "").lower() == "closed")

    # Bucket each record by age (days since filed)
    now_dt = datetime.now(timezone.utc)

    def _age_days(r):
        try:
            dt = datetime.fromisoformat(r.get("requested_datetime", "").replace("Z", "+00:00"))
            return (now_dt - dt).days
        except Exception:
            return days_back

    # Pre-compute counts per bucket + violation type for dynamic title updates
    type_to_slug_map = {}
    all_slugs = set()
    bucket_counts = {"30": {"open": 0, "closed": 0}, "60": {"open": 0, "closed": 0}, "90": {"open": 0, "closed": 0}}
    type_bucket_counts = {}
    for r in records:
        age = _age_days(r)
        status = (r.get("status") or "").lower()
        s = status if status in ("open", "closed") else "closed"
        vt = _extract_violation_type(r.get("description") or "")
        slug = _violation_type_slug(vt)
        type_to_slug_map[vt] = slug
        all_slugs.add(slug)
        if slug not in type_bucket_counts:
            type_bucket_counts[slug] = {"30": {"open": 0, "closed": 0}, "60": {"open": 0, "closed": 0}, "90": {"open": 0, "closed": 0}}
        if age <= 30:
            bucket_counts["30"][s] += 1
            type_bucket_counts[slug]["30"][s] += 1
        if age <= 60:
            bucket_counts["60"][s] += 1
            type_bucket_counts[slug]["60"][s] += 1
        if age <= 90:
            bucket_counts["90"][s] += 1
            type_bucket_counts[slug]["90"][s] += 1
    counts_js = str(type_bucket_counts).replace("'", '"')

    # Create map centered on Austin
    m = folium.Map(location=[30.2672, -97.7431], zoom_start=11, tiles="CartoDB positron")
    m.get_root().header.add_child(folium.Element(og_meta_tags("parking")))

    # FeatureGroups: open/closed × 30/60/90-day × violation type slug
    fg_clusters = {}
    fg_objects = {}
    for status_key in ("open", "closed"):
        for bucket in ("30", "60", "90"):
            for slug in all_slugs:
                name = f"{status_key}_{bucket}_{slug}"
                show = (bucket == "30")
                fg = folium.FeatureGroup(name=name, show=show, overlay=True)
                cluster = MarkerCluster().add_to(fg)
                fg.add_to(m)
                fg_clusters[name] = cluster
                fg_objects[name] = fg

    # Add markers to the appropriate bucket
    for r in records:
        lat = r["_lat"]
        lon = r["_lon"]
        status = (r.get("status") or "").lower()
        service_label = "Parking Violation Enforcement"
        description = (r.get("description") or "").strip()
        status_notes = (r.get("status_notes") or "").strip()
        date_str = (r.get("requested_datetime") or "").split("T")[0]
        updated_str = (r.get("updated_datetime") or "").split("T")[0]
        address = (r.get("address") or "").strip()
        req_id = r.get("service_request_id", "N/A")

        # Determine time bucket
        age = _age_days(r)
        if age <= 30:
            bucket = "30"
        elif age <= 60:
            bucket = "60"
        else:
            bucket = "90"

        vt = _extract_violation_type(description)
        slug = type_to_slug_map.get(vt, _violation_type_slug(vt))
        if slug not in all_slugs:
            slug = _violation_type_slug("")
        cluster_key = f"{status}_{bucket}_{slug}"
        target_cluster = fg_clusters[cluster_key]

        address_line = f'<b>Address:</b> <a href="https://www.google.com/maps/search/?api=1&query={lat},{lon}" target="_blank">{address}</a><br/>' if address else ""
        updated_line = f"<span style='color: #666;'>Updated: {updated_str}</span><br/>" if updated_str and updated_str != date_str else ""

        # Build structured attributes block (includes violation type + any other attrs)
        attrs = r.get("attributes") or []
        attrs_html = "".join(f"<b>{a['label']}:</b> {a['value']}<br/>" for a in attrs if a.get("label") and a.get("value"))
        attrs_block = f"<b>Additional Details:</b><br/>{attrs_html}" if attrs_html else ""

        desc_short = (description[:500] + "...") if len(description) > 500 else description
        desc_short = desc_short.replace("\n", "<br/>")
        desc_block = f"<b>Description:</b><br/><i>{desc_short}</i><br/>" if desc_short else ""

        notes_short = (status_notes[:500] + "...") if len(status_notes) > 500 else status_notes
        notes_short = notes_short.replace("\n", "<br/>")
        notes_block = f"<b>Resolution Notes:</b><br/><i>{notes_short}</i><br/>" if notes_short else ""

        ticket_url = f"https://311.austintexas.gov/tickets/{req_id}"
        popup_html = f"""
        <div style="font-family: sans-serif; max-width: 300px;">
            <b><a href="{ticket_url}" target="_blank" style="color: #0066cc;">Report #{req_id}</a></b><br/>
            <span style="color: #666;">Filed: {date_str}</span><br/>
            {updated_line}
            {address_line}
            <br/>
            <b>Status:</b> {'🔴 Open' if status == 'open' else '🟢 Closed'}<br/>
            <b>Category:</b> {service_label}<br/><br/>
            {attrs_block}
            {desc_block}
            {notes_block}
        </div>
        """

        popup = folium.Popup(popup_html, max_width=300)

        if status == "open":
            icon = folium.Icon(color="red", icon="exclamation-sign", prefix="glyphicon")
            tooltip = f"Open: {service_label}"
        else:
            icon = folium.Icon(color="green", icon="ok-sign", prefix="glyphicon")
            tooltip = f"Closed: {service_label}"

        folium.Marker(location=[lat, lon], popup=popup, icon=icon, tooltip=tooltip).add_to(target_cluster)

    # Single centered control panel: title + summary + filters
    map_var = m.get_name()
    layer_map_js = "{" + ", ".join(
        f'"{k}": {fg_objects[k].get_name()}' for k in fg_objects
    ) + "}"
    # JS map: violation type label -> slug for ?type= URL param lookup
    type_slug_js = "{" + ", ".join(
        f'"{vt}": "{slug}"' for vt, slug in type_to_slug_map.items()
    ) + "}"
    panel_html = f"""
    <div id="map-panel" style="position: absolute; top: 10px; left: 50%; transform: translateX(-50%);
                background: white; padding: 10px 16px; border-radius: 6px;
                box-shadow: 0 2px 6px rgba(0,0,0,0.3); z-index: 9999;
                font-family: sans-serif; text-align: center;">
        <b style="font-size: 15px;">🅿️ Austin Parking Enforcement 311 Reports</b><br/>
        <span id="map-summary" style="font-size: 12px; color: #555;"></span>
        <div style="display: flex; justify-content: center; gap: 4px; margin-top: 7px;">
            <button id="btn-30" onclick="setDayFilter(30)" class="fbtn active">30d</button>
            <button id="btn-60" onclick="setDayFilter(60)" class="fbtn">60d</button>
            <button id="btn-90" onclick="setDayFilter(90)" class="fbtn">90d</button>
            <span style="margin: 0 4px; color: #ccc;">|</span>
            <button id="btn-open" onclick="toggleStatus('open')" class="fbtn active">🔴 Open</button>
            <button id="btn-closed" onclick="toggleStatus('closed')" class="fbtn active">🟢 Closed</button>
            <span style="margin: 0 4px; color: #ccc;">|</span>
            <a href="trends/" class="fbtn" style="text-decoration: none; display: inline-block;">📈 Trends</a>
        </div>
    </div>
    <style>
        .fbtn {{
            padding: 3px 9px; border: 1px solid #ccc; border-radius: 4px;
            background: #f5f5f5; cursor: pointer; font-size: 12px; color: #444;
        }}
        .fbtn.active {{ background: #2563eb; color: white; border-color: #2563eb; }}
        .fbtn:hover:not(.active) {{ background: #e0e7ff; }}
    </style>
    <script>
        var currentDays = 30;
        var showOpen = true;
        var showClosed = true;
        var currentType = 'all';
        var layerMap = null;
        var leafletMap = null;
        var typeSlugMap = {type_slug_js};
        var typeBucketCounts = {counts_js};

        function updateSummary() {{
            var d = String(currentDays);
            var o = 0, c = 0;
            if (currentType === 'all') {{
                Object.keys(typeBucketCounts).forEach(function(slug) {{
                    var counts = (typeBucketCounts[slug] || {{}})[d] || {{}};
                    if (showOpen) o += (counts.open || 0);
                    if (showClosed) c += (counts.closed || 0);
                }});
            }} else {{
                var catData = typeBucketCounts[currentType];
                if (catData) {{
                    var counts = catData[d] || {{}};
                    o = showOpen ? (counts.open || 0) : 0;
                    c = showClosed ? (counts.closed || 0) : 0;
                }}
            }}
            document.getElementById('map-summary').textContent =
                'Last ' + d + ' days · ' + (o + c) + ' total · ' + o + ' open · ' + c + ' closed';
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
                var typeSlug = parts.slice(2).join('_');
                var timeOk = bucket <= currentDays;
                var statusOk = (status === 'open' && showOpen) || (status === 'closed' && showClosed);
                var typeOk = (currentType === 'all') || (typeSlug === currentType);
                var layer = layerMap[key];
                if (timeOk && statusOk && typeOk) {{
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

        function setTypeFilter(slug) {{
            currentType = slug;
            updateLayers();
            updateSummary();
        }}

        document.addEventListener('DOMContentLoaded', function() {{
            setTimeout(initLayers, 1000);
            var typeFilter = new URLSearchParams(window.location.search).get('type');
            if (typeFilter) {{
                var slug = typeSlugMap[typeFilter];
                if (slug) {{
                    setTypeFilter(slug);
                    var banner = document.createElement('div');
                    banner.id = 'type-filter-banner';
                    banner.style.cssText = 'position:absolute;bottom:24px;left:50%;transform:translateX(-50%);' +
                        'background:#1d4ed8;color:#fff;padding:7px 14px;border-radius:6px;' +
                        'font-family:sans-serif;font-size:12px;z-index:9999;white-space:nowrap;' +
                        'box-shadow:0 2px 8px rgba(0,0,0,0.3);display:flex;align-items:center;gap:10px;';
                    banner.innerHTML = '🔍 Filtering: <strong>' + typeFilter + '</strong>' +
                        ' &nbsp;<a href="." style="color:#93c5fd;font-size:11px;">show all</a>' +
                        ' &nbsp;<a href="trends/" style="color:#93c5fd;font-size:11px;">← back to trends</a>' +
                        ' &nbsp;<span onclick="setTypeFilter(\'all\');document.getElementById(\'type-filter-banner\').remove()" ' +
                        'style="cursor:pointer;opacity:0.7;font-size:14px;">✕</span>';
                    document.body.appendChild(banner);
                }}
            }}
        }});
    </script>
    """
    m.get_root().html.add_child(folium.Element(panel_html))

    # Save to buffer
    import tempfile
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
        except:
            pass

    summary = (
        f"🅿️ *Parking Enforcement Report Map*\n"
        f"_Last {days_back} days_\n\n"
        f"📊 *{total:,} reports mapped*\n"
        f"🔴 *{open_count:,} open*  ·  🟢 *{closed_count:,} closed*\n\n"
        f"Tap markers to see details. Use layer control to toggle views."
    )

    return buffer, summary
