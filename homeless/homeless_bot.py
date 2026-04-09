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
- NOISECMP: Non-Emergency Noise Complaint (catch-all quality-of-life)

Keywords (per Austin 311 research guide):
  encampment · homeless · camp · tent · transient · trash + homeless
"""

import os
import re
import time
import logging
import requests
from datetime import datetime, timezone, timedelta
from typing import Optional
from collections import defaultdict
import io

logger = logging.getLogger(__name__)

OPEN311_BASE_URL = "https://311.austintexas.gov/open311/v2"
TIMEOUT = 12
MAX_RETRIES = 3
RETRY_DELAY = 1.0
MAX_PAGES = 10  # up to 1,000 records per code

API_KEY = os.getenv("AUSTIN_APP_TOKEN")

# Service codes to search and their department labels
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

    # Trash/debris only counts when "homeless" also appears as a whole word
    has_trash = any(_word_in(kw, full_text) for kw in TRASH_KEYWORDS)
    has_homeless = _word_in("homeless", full_text)
    if has_trash and has_homeless:
        return True

    return False


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
        resp = session.get(url, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list) and data:
            return data[0]
        if isinstance(data, dict):
            return data
    except Exception as e:
        logger.debug(f"Detail fetch failed for {service_request_id}: {e}")
    return {}


def _fetch_code(service_code: str, days_back: int) -> list:
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

    while page <= MAX_PAGES:
        params = {
            "service_code": service_code,
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
                r["_service_label"] = SERVICE_CODES.get(service_code, service_code)
                r["_service_code"] = service_code
                all_records.append(r)

        if len(records) < 100:
            break

        page += 1
        time.sleep(0.5 if API_KEY else 1.0)

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
            time.sleep(0.25 if API_KEY else 0.5)

    return all_records


def fetch_encampment_reports(days_back: int = 90) -> dict:
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
            records = _fetch_code(code, days_back)
            matched = [r for r in records if _is_encampment_report(r)]
            by_code[code] = {"label": label, "fetched": len(records), "matched": len(matched)}
            all_records.extend(records)
            matched_records.extend(matched)
            logger.debug(f"{code}: {len(records)} fetched, {len(matched)} matched")
        except Exception as e:
            logger.warning(f"Failed to fetch {code}: {e}")
            by_code[code] = {"label": label, "fetched": 0, "matched": 0, "error": str(e)}

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
# FORMATTERS
# =============================================================================

def format_encampment_stats(data: dict) -> str:
    """Format the encampment/homeless 311 report summary for Telegram."""
    total = data.get("total", 0)
    days_back = data.get("days_back", 90)
    status = data.get("status_counts", {})
    by_dept = data.get("by_dept", {})
    monthly = data.get("monthly", {})
    fetched_at = data.get("fetched_at", "")
    total_fetched = data.get("total_fetched", 0)

    disclaimer = (
        "_Reports are based on voluntary 311 complaints only — not a full "
        "count of encampments in Austin._"
    )

    if total == 0:
        msg = f"🏕️ *Encampment & Homeless-Related 311 Reports*\n"
        msg += f"_Last {days_back} days_\n\n"
        msg += "No encampment-related keywords found in the fetched records.\n\n"
        msg += disclaimer
        return msg

    open_count = status.get("open", 0)
    closed_count = status.get("closed", 0)
    resolution_pct = round(closed_count / total * 100) if total else 0

    msg = f"🏕️ *Encampment & Homeless-Related 311 Reports*\n"
    msg += f"_Last {days_back} days · {total:,} matched from {total_fetched:,} fetched_\n\n"

    msg += f"🔴 *Open:* {open_count}   🟢 *Resolved:* {closed_count} ({resolution_pct}%)\n\n"

    # Department breakdown
    if by_dept:
        msg += "*By Department / Category:*\n"
        for label, counts in sorted(by_dept.items(), key=lambda x: -x[1]["total"]):
            dep_total = counts["total"]
            dep_open = counts["open"]
            dep_closed = counts["closed"]
            bar = "█" * min(8, round(dep_total / total * 8)) if total > 0 else ""
            msg += f"  *{label}*: {dep_total} total"
            msg += f" · {dep_open} open · {dep_closed} resolved\n"
            msg += f"  {bar}\n"
        msg += "\n"

    # Monthly trend (last 6 months)
    if monthly:
        sorted_months = sorted(monthly.keys())[-6:]
        msg += "*Monthly Trend (last 6 months):*\n"
        max_month_count = max(monthly[m] for m in sorted_months) or 1
        for m in sorted_months:
            count = monthly[m]
            bar = "▓" * min(10, round(count / max_month_count * 10))
            # Format "2024-11" → "Nov 24"
            try:
                dt = datetime.strptime(m, "%Y-%m")
                label = dt.strftime("%b %y")
            except ValueError:
                label = m
            msg += f"  {label}: {bar} {count}\n"
        msg += "\n"

    msg += disclaimer + "\n"
    msg += f"\n_Source: [Austin Open311 API](https://311.austintexas.gov/open311/v2) · {fetched_at}_"
    return msg


def format_encampment_locations(data: dict) -> str:
    """Format open encampment reports with map links."""
    locations = data.get("locations", [])
    total = data.get("total", 0)
    days_back = data.get("days_back", 90)
    open_count = data.get("status_counts", {}).get("open", 0)

    if not locations:
        return "🏕️ No open encampment reports with location data found."

    msg = f"🏕️ *Open Encampment Reports — Locations*\n"
    msg += f"_Last {days_back} days · {open_count} open of {total} matched_\n\n"

    for i, (lat, lon, label) in enumerate(locations, 1):
        dept_short = label.split("—")[-1].strip() if "—" in label else label
        msg += f"{i}. {dept_short}\n"
        msg += f"   [📍 View on map](https://maps.google.com/?q={lat:.5f},{lon:.5f})\n"

    msg += f"\n_Source: [Austin Open311 API](https://311.austintexas.gov/open311/v2)_"
    return msg


# =============================================================================
# INTERACTIVE MAP GENERATION
# =============================================================================

def fetch_encampment_with_coords(days_back: int = 30) -> dict:
    """Fetch all encampment reports and filter to those with valid coordinates.
    
    Returns both open AND closed requests with location data for mapping.
    """
    result = fetch_encampment_reports(days_back)
    records = result["records"]
    
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
        "fetched_at": result["fetched_at"],
    }


def generate_encampment_map(days_back: int = 30) -> tuple[Optional[io.BytesIO], str]:
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
    total = data["total"]
    
    if not records:
        return None, f"🏕️ No encampment reports with location data found in the last {days_back} days."
    
    # Count by status
    open_count = sum(1 for r in records if (r.get("status") or "").lower() == "open")
    closed_count = sum(1 for r in records if (r.get("status") or "").lower() == "closed")
    
    # Create map centered on Austin
    m = folium.Map(location=[30.2672, -97.7431], zoom_start=11, tiles="CartoDB positron")
    
    # Add marker clusters for open and closed
    cluster_open = MarkerCluster(name="🔴 Open Reports").add_to(m)
    cluster_closed = MarkerCluster(name="🟢 Closed Reports").add_to(m)
    
    # Add markers
    for r in records:
        lat = r["_lat"]
        lon = r["_lon"]
        status = (r.get("status") or "").lower()
        service_label = r.get("_service_label", "Unknown")
        description = (r.get("description") or "")[:150]
        date_str = (r.get("requested_datetime") or "").split("T")[0]
        req_id = r.get("service_request_id", "N/A")
        
        # Truncate description for popup
        desc_short = description[:100] + "..." if len(description) > 100 else description
        
        ticket_url = f"https://311.austintexas.gov/issues/{req_id}"
        popup_html = f"""
        <div style="font-family: sans-serif; max-width: 300px;">
            <b><a href="{ticket_url}" target="_blank" style="color: #0066cc;">Report #{req_id}</a></b><br/>
            <span style="color: #666;">{date_str}</span><br/><br/>
            <b>Status:</b> {'🔴 Open' if status == 'open' else '🟢 Closed'}<br/>
            <b>Category:</b> {service_label}<br/><br/>
            <b>Description:</b><br/>
            <i>{desc_short}</i>
        </div>
        """
        
        popup = folium.Popup(popup_html, max_width=300)
        
        # Color based on status
        if status == "open":
            icon = folium.Icon(color="red", icon="exclamation-sign", prefix="glyphicon")
            marker = folium.Marker(
                location=[lat, lon],
                popup=popup,
                icon=icon,
                tooltip=f"Open: {service_label.split('—')[-1].strip()}"
            )
            marker.add_to(cluster_open)
        else:
            icon = folium.Icon(color="green", icon="ok-sign", prefix="glyphicon")
            marker = folium.Marker(
                location=[lat, lon],
                popup=popup,
                icon=icon,
                tooltip=f"Closed: {service_label.split('—')[-1].strip()}"
            )
            marker.add_to(cluster_closed)
    
    # Add layer control
    folium.LayerControl().add_to(m)
    
    # Add title
    title_html = f"""
    <div style="position: absolute; top: 10px; left: 50%; transform: translateX(-50%);
                background: white; padding: 10px 20px; border-radius: 5px; 
                box-shadow: 0 2px 6px rgba(0,0,0,0.3); z-index: 9999;
                font-family: sans-serif; text-align: center;">
        <b style="font-size: 16px;">🏕️ Austin Homeless Encampment 311 Reports</b><br/>
        <span style="font-size: 13px; color: #555;">
            Last {days_back} days · {total:,} total · {open_count:,} open · {closed_count:,} closed
        </span>
    </div>
    """
    m.get_root().html.add_child(folium.Element(title_html))
    
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
    
    summary = (
        f"🏕️ *Encampment Report Map*\n"
        f"_Last {days_back} days_\n\n"
        f"📊 *{total:,} reports mapped*\n"
        f"🔴 *{open_count:,} open*  ·  🟢 *{closed_count:,} closed*\n\n"
        f"Tap markers to see details. Use layer control to toggle views."
    )
    
    return buffer, summary
