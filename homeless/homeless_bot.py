"""
Homeless Encampment & Trash Reports — data layer and formatters.

Queries Austin Open311 API for service requests that mention encampments,
tents, homeless camps, or related keywords across parks, right-of-way,
debris, and drainage service codes.

These reports reflect voluntary public reporting only — they capture the
burden placed on city departments by constituent complaints, not a census
of all encampments in Austin.

Target service codes (from 311categories.txt):
- PRGRDISS: Park Maintenance - Grounds (primary — most reports filed here)
- ATCOCIRW: Construction Concerns in Right of Way (underpasses, sidewalks)
- OBSTMIDB: Obstruction in Right of Way
- SBDEBROW: Debris in Street
- DRCHANEL: Channels / Creeks / Drainage Issues (watershed dept)

Keywords (per Austin 311 research guide):
  encampment · homeless · camp · tent · transient · trash + homeless
"""

import os
import re
import time
import logging
import requests
from datetime import datetime, timezone, timedelta
from open311_client import open311_get, og_meta_tags
from typing import Optional
from collections import defaultdict
import io

logger = logging.getLogger(__name__)

OPEN311_BASE_URL = "https://311.austintexas.gov/open311/v2"
CLEANUP_MAP_URL = "https://www.google.com/maps/d/viewer?mid=152l2HDPGqXzchnKl8V_dWPSb82ciYQs&ll=30.193194959961946%2C-97.75715792298466&z=14"
TIMEOUT = 45
MAX_RETRIES = 3
RETRY_DELAY = 1.0
MAX_PAGES = 10  # up to 1,000 records per code

API_KEY = os.getenv("AUSTINAPIKEY")

# Service codes to search and their department labels
# 
# DISCOVERY NOTES (2026-04-29):
# Analysis of recent requests found homeless-related keywords across these codes:
# - PRGRDISS: Park Maintenance (tent, homeless) ✓ included
# - OBSTMIDB: Obstruction in ROW (homeless, tent) ✓ included
# - SBDEBROW: Debris in Street (homeless) ✓ included
# - DRCHANEL: Drainage/Creek (homeless, tent) ✓ included
# - APDNONNO: Non Emergency Noise Complaint (homeless) ← NOT INCLUDED (may add noise)
# - HHSGRAFF: Graffiti Abatement (homeless, tent) ← NOT INCLUDED (graffiti-focused)
# - SBPOTREP: Pothole Repair (tent) ← NOT INCLUDED (minimal matches, off-topic)
#
# The "Homeless - Violet Kiosk and Storage Carts" and "Homelessness Matters" categories
# from the 311 dataset appear to use the same service codes as Park Maintenance
# and other general codes, NOT unique homeless-specific codes.
#
# Recommendation: Keep current 5 codes. Adding APDNONNO may increase false positives
# from general noise complaints not related to encampments.
SERVICE_CODES = {
    "PRGRDISS": "Parks — Grounds Maintenance",
    "ATCOCIRW": "TPW — Right of Way",
    "OBSTMIDB": "TPW — Obstruction in ROW",
    "SBDEBROW": "TPW — Debris in Street",
    "DRCHANEL": "Watershed — Drainage/Creek",
}

# Keywords that indicate an encampment / homeless-related report.
# All comparisons are lower-case.
ENCAMPMENT_KEYWORDS = ("encampment", "homelessness", "homeless camp", "homeless", "camp", "tent", "transient", "vagrant")

# Keywords that appear in city status/closure notes when a report is routed
# to the Homeless Strategy Office (HSO). These fire on status_notes, not
# the citizen's original description.
HSO_KEYWORDS = ("homeless strategy", "hso")

# "trash" or "debris" alone is too noisy; only flag when paired with
# a co-occurring homeless keyword in the same description.
TRASH_KEYWORDS = ("trash", "debris", "garbage")

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
            "User-Agent": "austin311bot/0.1 (Open311 encampment queries)",
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


def _word_in(keyword: str, text: str) -> bool:
    """Return True if keyword appears as a whole word (or phrase) in text."""
    return bool(re.search(r"\b" + re.escape(keyword) + r"\b", text))


def _is_encampment_report(record: dict) -> bool:
    """Return True if the record is encampment / homeless-related.

    Checks three text sources:
      - description: the citizen's original complaint text
      - address: sometimes contains location notes with keywords
      - status_notes: city closure/routing note — often says "referred to
        Homeless Strategy Office (HSO)", which is the strongest signal that
        a report was administratively handled as an encampment issue

    All keyword matches use whole-word boundaries to avoid false positives
    (e.g. "tent" inside "intention", "camp" inside "campaign").
    """
    citizen_text = " ".join(filter(None, [
        record.get("description") or "",
        record.get("address") or "",
    ])).lower()

    status_text = (record.get("status_notes") or "").lower()
    full_text = f"{citizen_text} {status_text}"

    if not full_text.strip():
        return False

    # Direct encampment / homeless keywords — whole-word match only
    for kw in ENCAMPMENT_KEYWORDS:
        if _word_in(kw, full_text):
            return True

    # HSO routing keywords — primarily appear in status_notes
    for kw in HSO_KEYWORDS:
        if _word_in(kw, status_text):
            return True

    # Fallback for asterisk-formatted HSO mentions like "Homeless* *Strategy* *Office"
    # The city sometimes writes status_notes with asterisks between words, which
    # breaks the word-boundary regex in _word_in(). Strip asterisks and recheck.
    normalized_status = status_text.replace("*", " ")
    if "homeless strategy" in normalized_status:
        return True

    # Trash/debris only counts when "homeless" also appears as a whole word
    has_trash = any(_word_in(kw, full_text) for kw in TRASH_KEYWORDS)
    has_homeless = _word_in("homeless", full_text)
    if has_trash and has_homeless:
        return True

    return False


# Public alias so aggregation scripts (e.g. scripts/generate_query_data.py)
# can apply the exact same filter the map uses.
def is_encampment_report(record: dict) -> bool:
    """Public wrapper for the encampment keyword filter."""
    return _is_encampment_report(record)


def _make_request(params: dict) -> list:
    return open311_get(_get_session(), f"{OPEN311_BASE_URL}/requests.json", params)


def _looks_truncated(text: str | None) -> bool:
    """Return True if a text field appears to have been cut off by the API.

    The bulk list endpoint truncates long text fields at ~255 characters
    without a trailing punctuation mark or space boundary.
    """
    if not text:
        return False
    t = text.rstrip()
    if len(t) < 200:
        return False
    # Truncated strings end mid-word (no sentence-ending punctuation or space)
    return t[-1] not in ".!?,;: \t\n"


def _fetch_detail(service_request_id: str) -> dict:
    """Fetch a single ticket by ID to get untruncated field values."""
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


def _fetch_code(service_code: str, days_back: int, max_pages: int = MAX_PAGES) -> list:
    """Fetch all requests for one service code with pagination.

    After the bulk fetch, any record with a truncated description or
    status_notes is re-fetched individually using the single-ticket endpoint
    so keyword matching sees the full text.
    """
    end = _utc_now()
    start = end - timedelta(days=days_back)
    all_records: list = []
    seen_ids: set = set()
    page = 1

    while page <= max_pages:
        params = {
            "service_code": service_code,
            "start_date": _isoformat_z(start),
            "end_date": _isoformat_z(end),
            "per_page": 100,
            "page": page,
            "extensions": "true",
        }
        records = _make_request(params)
        if not records:
            break

        for r in records:
            sid = r.get("service_request_id")
            if sid and sid not in seen_ids:
                seen_ids.add(sid)
                r["_service_label"] = SERVICE_CODES.get(service_code, service_code)
                r["_service_code"] = service_code
                all_records.append(r)

        if len(records) < 100:
            break

        page += 1
        time.sleep(1.0 if API_KEY else 2.0)

    # Re-fetch individual records whose text fields look truncated
    for i, r in enumerate(all_records):
        if _looks_truncated(r.get("description")) or _looks_truncated(r.get("status_notes")):
            sid = r.get("service_request_id")
            if not sid:
                continue
            detail = _fetch_detail(sid)
            if detail:
                # Merge full-text fields back; preserve our private labels
                for field in ("description", "status_notes"):
                    if detail.get(field):
                        r[field] = detail[field]
                if detail.get("attributes"):
                    r["attributes"] = detail["attributes"]
            time.sleep(0.25 if API_KEY else 0.5)

    return all_records


def fetch_encampment_reports_monthly(months_back: int = 12, use_cache: bool = True) -> list:
    """Fetch keyword-matched encampment reports with optional caching.

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
        A flat list of matched records across all months and all codes.
    """
    from open311_cache import init_cache, get_cached_records, cache_records, get_last_fetch_date, attach_service_labels

    CATEGORY = "homeless"

    # Initialize cache if using
    if use_cache:
        init_cache()
        # Read raw cached records by service code (any category may have
        # cached them), then apply the keyword filter at read time.
        raw_cached = get_cached_records(service_codes=list(SERVICE_CODES.keys()))
        cached_ids = {r.get("service_request_id") for r in raw_cached}
        cached_records = [r for r in raw_cached if _is_encampment_report(r)]
        logger.info(f"Loaded {len(raw_cached)} cached records ({len(cached_records)} keyword matches)")

        # Check if we have recent cache
        last_fetch = get_last_fetch_date(service_codes=list(SERVICE_CODES.keys()))
        if last_fetch:
            logger.info(f"Last fetch was at {last_fetch}")
            # If cache is less than 6 days old and we have data, use it
            cache_age = _utc_now() - last_fetch
            if cache_age < timedelta(days=6) and len(cached_records) > 0:
                logger.info(f"Cache is fresh ({cache_age.days} days old), returning cached data")
                return attach_service_labels(cached_records, SERVICE_CODES)
    else:
        cached_records = []
        cached_ids = set()

    now = _utc_now()
    all_matched: list = []
    seen_ids: set = cached_ids.copy()  # Start with cached IDs to avoid duplicates
    new_records: list = []

    # Calculate how far back we need to fetch
    # If we have cache, only fetch from last fetch date
    if use_cache and cached_records:
        last_fetch = get_last_fetch_date(service_codes=list(SERVICE_CODES.keys()))
        if last_fetch:
            # Fetch from 1 day before last fetch to catch any missed records
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

        for code in SERVICE_CODES:
            try:
                page = 1
                monthly_records = 0
                while page <= MAX_PAGES:
                    params = {
                        "service_code": code,
                        "start_date": _isoformat_z(month_start),
                        "end_date":   _isoformat_z(month_end),
                        "per_page":   100,
                        "page":       page,
                    }
                    records = _make_request(params)
                    if not records:
                        break
                    for r in records:
                        sid = r.get("service_request_id")
                        if sid and sid not in seen_ids:
                            seen_ids.add(sid)
                            r["_service_label"] = SERVICE_CODES.get(code, code)
                            r["_service_code"]  = code
                            new_records.append(r)  # cache raw; filter at read time
                            if _is_encampment_report(r):
                                all_matched.append(r)
                                monthly_records += 1
                    if len(records) < 100:
                        break
                    page += 1
                    time.sleep(1.0 if API_KEY else 2.0)
                if monthly_records > 0:
                    logger.info(f"  {code} {month_start.strftime('%Y-%m')}: {monthly_records} new matches")
            except Exception as e:
                logger.warning(f"Monthly fetch failed {code} {month_start.strftime('%Y-%m')}: {e}")
        time.sleep(2.0 if API_KEY else 4.0)

    # Cache new records
    if use_cache and new_records:
        cache_records(CATEGORY, new_records)
        logger.info(f"Cached {len(new_records)} new records")

    # Return combined cached + new (if we had partial cache)
    if use_cache and cached_records:
        # Combine and remove duplicates
        combined = {r.get("service_request_id"): r for r in cached_records}
        for r in all_matched:
            combined[r.get("service_request_id")] = r
        result = list(combined.values())
        logger.info(f"Returning {len(result)} total records ({len(cached_records)} cached + {len(new_records)} new)")
        return result

    return all_matched


def fetch_encampment_reports(days_back: int = 90, max_pages: int = MAX_PAGES) -> dict:
    """Fetch and keyword-filter 311 reports across all target service codes.

    Returns a dict with:
        records      — list of matched Open311 records
        total_fetched — total records pulled before keyword filtering
        days_back     — time window used
        by_code       — {service_code: {"fetched": N, "matched": N}}
    """
    all_records: list = []
    matched_records: list = []
    by_code: dict = {}

    for code, label in SERVICE_CODES.items():
        try:
            records = _fetch_code(code, days_back, max_pages=max_pages)
            matched = [r for r in records if _is_encampment_report(r)]
            by_code[code] = {"label": label, "fetched": len(records), "matched": len(matched)}
            all_records.extend(records)
            matched_records.extend(matched)
            logger.debug(f"{code}: {len(records)} fetched, {len(matched)} matched")
        except Exception as e:
            logger.warning(f"Failed to fetch {code}: {e}")
            by_code[code] = {"label": label, "fetched": 0, "matched": 0, "error": str(e)}
        time.sleep(3.0 if not API_KEY else 1.0)

    return {
        "records": matched_records,
        "total_fetched": len(all_records),
        "days_back": days_back,
        "by_code": by_code,
        "fetched_at": _utc_now().strftime("%Y-%m-%d %H:%M UTC"),
    }


# =============================================================================
# AGGREGATE STATS
# =============================================================================

def get_encampment_stats(days_back: int = 90) -> dict:
    """Return summary stats for encampment/homeless-related 311 reports."""
    result = fetch_encampment_reports(days_back)
    records = result["records"]

    status_counts: dict = {"open": 0, "closed": 0, "other": 0}
    by_dept: dict = {}     # service_label → {total, open, closed}
    monthly: dict = {}     # "YYYY-MM" → count
    locations: list = []   # (lat, lon, label, status) for top open reports

    for r in records:
        label = r.get("_service_label", "Unknown")
        status = (r.get("status") or "").lower()
        dt_str = r.get("requested_datetime") or ""

        # Status
        if status == "open":
            status_counts["open"] += 1
        elif status == "closed":
            status_counts["closed"] += 1
        else:
            status_counts["other"] += 1

        # By department
        if label not in by_dept:
            by_dept[label] = {"total": 0, "open": 0, "closed": 0}
        by_dept[label]["total"] += 1
        if status == "open":
            by_dept[label]["open"] += 1
        elif status == "closed":
            by_dept[label]["closed"] += 1

        # Monthly trend
        if dt_str:
            try:
                dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                month_key = dt.strftime("%Y-%m")
                monthly[month_key] = monthly.get(month_key, 0) + 1
            except ValueError:
                pass

        # Collect coordinates for open reports
        lat = r.get("lat")
        lon = r.get("long")
        if lat and lon and status == "open":
            locations.append((float(lat), float(lon), label))

    return {
        "total": len(records),
        "total_fetched": result["total_fetched"],
        "status_counts": status_counts,
        "by_dept": by_dept,
        "monthly": monthly,
        "locations": locations[:15],  # cap for formatting
        "by_code": result["by_code"],
        "days_back": days_back,
        "fetched_at": result["fetched_at"],
    }


# =============================================================================
# INTERACTIVE MAP GENERATION
# =============================================================================

def fetch_encampment_with_coords(days_back: int = 90) -> dict:
    """Fetch encampment reports with valid coordinates, using the cache.

    Pulls from fetch_encampment_reports_monthly (cache-backed) and filters
    to the requested time window in Python, so the map generator never hits
    the Open311 API directly when the cache is warm.
    """
    # months_back=6 ensures coverage on a cold cache; warm cache returns everything
    all_matched = fetch_encampment_reports_monthly(months_back=6)

    cutoff = _utc_now() - timedelta(days=days_back)
    located = []
    in_window = 0
    for r in all_matched:
        try:
            dt = datetime.fromisoformat(r.get("requested_datetime", "").replace("Z", "+00:00"))
            if dt < cutoff:
                continue
        except Exception:
            pass
        in_window += 1
        lat = r.get("lat")
        lon = r.get("long")
        if lat and lon:
            try:
                lat_f = float(lat)
                lon_f = float(lon)
                if 30.0 <= lat_f <= 30.5 and -98.0 <= lon_f <= -97.5:
                    r["_lat"] = lat_f
                    r["_lon"] = lon_f
                    located.append(r)
            except (ValueError, TypeError):
                pass

    return {
        "records": located,
        "total": len(located),
        "total_matched": in_window,
        "days_back": days_back,
        "fetched_at": _utc_now().isoformat(),
    }


def generate_encampment_map(days_back: int = 180) -> tuple[Optional[io.BytesIO], str]:
    """Generate an interactive HTML map of encampment reports.
    
    Returns:
        tuple: (BytesIO buffer with HTML content, summary message)
    """
    try:
        import folium
        from folium.plugins import MarkerCluster
    except ImportError:
        return None, "❌ Map generation requires 'folium' library. Install with: pip install folium"
    
    data = fetch_encampment_with_coords(days_back)
    records = data["records"]
    total         = data["total"]
    total_matched = data.get("total_matched", total)

    if not records:
        return None, f"🏕️ No encampment reports with location data found in the last {days_back} days."

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

    # Pre-compute counts per bucket for dynamic title updates
    bucket_counts = {"30": {"open": 0, "closed": 0}, "90": {"open": 0, "closed": 0}, "180": {"open": 0, "closed": 0}}
    for r in records:
        age = _age_days(r)
        status = (r.get("status") or "").lower()
        s = status if status in ("open", "closed") else "closed"
        if age <= 30:
            bucket_counts["30"][s] += 1
        if age <= 90:
            bucket_counts["90"][s] += 1
        if age <= 180:
            bucket_counts["180"][s] += 1
    counts_js = str(bucket_counts).replace("'", '"')

    # Create map centered on Austin
    m = folium.Map(
        location=[30.2672, -97.7431],
        zoom_start=11,
        tiles="https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png?key=cb1_2du8_1_a49e4774820276874a1a5b33",
        attr='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',
    )
    m.get_root().header.add_child(folium.Element(og_meta_tags("homeless")))

    # Six FeatureGroups: open/closed × 1-month/3-month/6-month buckets
    fg_clusters = {}
    fg_objects = {}  # name → FeatureGroup (to get JS var names later)
    for status_key in ("open", "closed"):
        for bucket in ("30", "90", "180"):
            name = f"{status_key}_{bucket}"
            show = (bucket == "180")
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
        service_label = r.get("_service_label", "Unknown")
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
        elif age <= 90:
            bucket = "90"
        else:
            bucket = "180"

        cluster_key = f"{status}_{bucket}"
        if cluster_key not in fg_clusters:
            cluster_key = f"closed_{bucket}"
        target_cluster = fg_clusters[cluster_key]

        address_line = f'<b>Address:</b> <a href="https://www.google.com/maps/search/?api=1&query={lat},{lon}" target="_blank">{address}</a><br/>' if address else ""
        updated_line = f"<span style='color: #666;'>Updated: {updated_str}</span><br/>" if updated_str and updated_str != date_str else ""

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
            tooltip = f"Open: {service_label.split('—')[-1].strip()}"
        else:
            icon = folium.Icon(color="green", icon="ok-sign", prefix="glyphicon")
            tooltip = f"Closed: {service_label.split('—')[-1].strip()}"

        folium.Marker(location=[lat, lon], popup=popup, icon=icon, tooltip=tooltip).add_to(target_cluster)

    # Single centered control panel: title + summary + filters
    map_var = m.get_name()
    layer_map_js = "{" + ", ".join(
        f'"{k}": {fg_objects[k].get_name()}' for k in fg_objects
    ) + "}"
    panel_html = f"""
    <div id="map-panel" style="position: absolute; top: 10px; left: 50%; transform: translateX(-50%);
                background: white; padding: 10px 16px; border-radius: 6px;
                box-shadow: 0 2px 6px rgba(0,0,0,0.3); z-index: 9999;
                font-family: sans-serif; text-align: center;">
        <b style="font-size: 15px;">🏕️ Homeless-Related 311 Complaints</b><br/>
        <a href="{CLEANUP_MAP_URL}" target="_blank" style="font-size: 11px; color: #7c3aed; text-decoration: none;">🧹 View city cleanup sites map →</a><br/>
        <span id="map-summary" style="font-size: 12px; color: #555;"></span>
        {"" if total_matched == total else f'<span style="font-size: 11px; color: #888;">{total_matched - total:,} matched reports excluded — no coordinates provided by filer</span>'}
        <div style="display: flex; justify-content: center; gap: 4px; margin-top: 7px;">
            <button id="btn-30" onclick="setDayFilter(30)" class="fbtn">1 mo</button>
            <button id="btn-90" onclick="setDayFilter(90)" class="fbtn">3 mo</button>
            <button id="btn-180" onclick="setDayFilter(180)" class="fbtn active">6 mo</button>
            <span style="margin: 0 4px; color: #ccc;">|</span>
            <button id="btn-open" onclick="toggleStatus('open')" class="fbtn active">🔴 Open</button>
            <button id="btn-closed" onclick="toggleStatus('closed')" class="fbtn active">🟢 Closed</button>
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
        var currentDays = 180;
        var showOpen = true;
        var showClosed = true;
        var layerMap = null;
        var leafletMap = null;
        var bucketCounts = {counts_js};

        var dayLabels = {{"30": "1 month", "90": "3 months", "180": "6 months"}};

        function updateSummary() {{
            var d = String(currentDays);
            var counts = bucketCounts[d] || {{}};
            var o = showOpen ? (counts.open || 0) : 0;
            var c = showClosed ? (counts.closed || 0) : 0;
            document.getElementById('map-summary').textContent =
                'Last ' + (dayLabels[d] || d + ' days') + ' · ' + (o + c) + ' total · ' + o + ' open · ' + c + ' closed';
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
            [30, 90, 180].forEach(function(d) {{
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
        # Clean up temp file
        try:
            os.unlink(tmp_path)
        except:
            pass
    
    no_coords = total_matched - total
    summary = (
        f"🏕️ *Encampment Report Map*\n"
        f"_Last {days_back} days_\n\n"
        f"📊 *{total:,} reports mapped* ({total_matched:,} total matched)\n"
        f"🔴 *{open_count:,} open*  ·  🟢 *{closed_count:,} closed*\n"
        + (f"⚠️ *{no_coords:,} matched reports excluded* — no coordinates provided\n\n" if no_coords else "\n")
        + f"Tap markers to see details. Use layer control to toggle views."
    )
    
    return buffer, summary
