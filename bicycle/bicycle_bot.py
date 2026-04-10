"""
Bicycle Complaints — data layer and formatters.

Queries Austin Open311 API for bicycle-related service requests across
multiple service codes including direct bicycle issues and infrastructure
hazards that affect cyclists (obstructions, debris, construction, etc.).

Target service codes:
- PWBICYCL: Bicycle Issues (primary — direct bicycle complaints)
- OBSTMIDB: Obstruction in Right of Way (blocked bike lanes)
- SBDEBROW: Debris in Street (hazards in bike lanes)
- ATCOCIRW: Construction Concerns in Right of Way (detours/closures)
- SBSIDERE: Sidewalk Repair (cyclists using sidewalks)
- TPPECRNE: Pedestrian Crossing New/Modify (crossing safety)
- PWSIDEWL: New Sidewalk/Curb Ramp/ADA Route (access routes)
"""

import re
import time
import logging
import requests
import os
import io
from datetime import datetime, timezone, timedelta
from typing import Optional
from collections import defaultdict

logger = logging.getLogger(__name__)

OPEN311_BASE_URL = "https://311.austintexas.gov/open311/v2"
TIMEOUT = 12
MAX_RETRIES = 3
RETRY_DELAY = 1.0
MAX_PAGES = 10

API_KEY = os.getenv("AUSTIN_APP_TOKEN")

# Service codes to search and their department labels
SERVICE_CODES = {
    "PWBICYCL": "Bicycle Issues",
    "OBSTMIDB": "TPW — Obstruction in ROW",
    "SBDEBROW": "TPW — Debris in Street",
    "ATCOCIRW": "TPW — Construction in ROW",
    "SBSIDERE": "TPW — Sidewalk Repair",
    "TPPECRNE": "TPW — Pedestrian Crossing",
    "PWSIDEWL": "TPW — Sidewalk/Curb Ramp",
}

# Keywords that indicate a bicycle-related report for secondary codes
# (PWBICYCL doesn't need filtering since it's explicitly bicycle)
BICYCLE_KEYWORDS = (
    "bike", "bicycle", "cyclist", "cycling", "bike lane", "bicycle lane",
    "shared path", "trail", "mobility", "scooter", "ebike",
)

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
            "User-Agent": "austin311bot/0.1 (Open311 bicycle queries)",
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


def _is_bicycle_report(record: dict) -> bool:
    """Return True if the record is bicycle-related.

    PWBICYCL records are always included. For other service codes,
    filter by bicycle-related keywords in description/address.
    """
    code = record.get("service_code")
    if code == "PWBICYCL":
        return True

    # For secondary codes, check for bicycle keywords
    citizen_text = " ".join(filter(None, [
        record.get("description") or "",
        record.get("address") or "",
    ])).lower()

    if not citizen_text.strip():
        return False

    for kw in BICYCLE_KEYWORDS:
        if _word_in(kw, citizen_text):
            return True

    return False


def _looks_truncated(text: str | None) -> bool:
    """Return True if a text field appears to have been cut off by the API."""
    if not text:
        return False
    t = text.rstrip()
    if len(t) < 200:
        return False
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
            delay = (10.0 * (2 ** retries)) if e.response.status_code == 429 else RETRY_DELAY * (2 ** retries)
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
            time.sleep(0.25 if API_KEY else 0.5)

    return all_records


def fetch_bicycle_reports(days_back: int = 90) -> dict:
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
            matched = [r for r in records if _is_bicycle_report(r)]
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


def get_recent_complaints(limit: int = 10, days_back: int = 90) -> list:
    """Return most recent bicycle complaints from the past N days."""
    end = _utc_now()
    start = end - timedelta(days=days_back)
    params = {
        "service_code": "PWBICYCL",
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


# =============================================================================
# INTERACTIVE MAP GENERATION
# =============================================================================

def fetch_bicycle_with_coords(days_back: int = 30) -> dict:
    """Fetch all bicycle reports and filter to those with valid coordinates.

    Returns both open AND closed requests with location data for mapping.
    """
    result = fetch_bicycle_reports(days_back)
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


def generate_bicycle_map(days_back: int = 30) -> tuple[Optional[io.BytesIO], str]:
    """Generate an interactive HTML map of bicycle reports.

    Returns:
        tuple: (BytesIO buffer with HTML content, summary message)
    """
    try:
        import folium
        from folium.plugins import MarkerCluster
    except ImportError:
        return None, "❌ Map generation requires 'folium' library. Install with: pip install folium"

    data = fetch_bicycle_with_coords(days_back)
    records = data["records"]
    total = data["total"]

    if not records:
        return None, f"🚴 No bicycle reports with location data found in the last {days_back} days."

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

    # Create map centered on Austin
    m = folium.Map(location=[30.2672, -97.7431], zoom_start=11, tiles="CartoDB positron")

    # Six FeatureGroups: open/closed × 30/60/90-day buckets
    fg_clusters = {}
    fg_objects = {}
    for status_key in ("open", "closed"):
        for bucket in ("30", "60", "90"):
            name = f"{status_key}_{bucket}"
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
        elif age <= 60:
            bucket = "60"
        else:
            bucket = "90"

        cluster_key = f"{status}_{bucket}"
        if cluster_key not in fg_clusters:
            cluster_key = f"closed_{bucket}"
        target_cluster = fg_clusters[cluster_key]

        # Use description if available, fall back to status_notes
        detail_text = description or status_notes
        detail_label = "Description" if description else "Resolution Notes"
        truncate_at = 500
        detail_short = (detail_text[:truncate_at] + "...") if len(detail_text) > truncate_at else detail_text
        detail_short = detail_short.replace("\n", "<br/>")

        address_line = f"<b>Address:</b> {address}<br/>" if address else ""
        updated_line = f"<span style='color: #666;'>Updated: {updated_str}</span><br/>" if updated_str and updated_str != date_str else ""

        ticket_url = f"https://311.austintexas.gov/tickets?filter%5Bsearch%5D={req_id}"
        popup_html = f"""
        <div style="font-family: sans-serif; max-width: 300px;">
            <b><a href="{ticket_url}" target="_blank" style="color: #0066cc;">Report #{req_id}</a></b><br/>
            <span style="color: #666;">Filed: {date_str}</span><br/>
            {updated_line}
            {address_line}
            <br/>
            <b>Status:</b> {'🔴 Open' if status == 'open' else '🟢 Closed'}<br/>
            <b>Category:</b> {service_label}<br/><br/>
            <b>{detail_label}:</b><br/>
            <i>{detail_short if detail_short else '(no details)'}</i>
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
        <b style="font-size: 15px;">🚴 Austin Bicycle Infrastructure 311 Reports</b><br/>
        <span id="map-summary" style="font-size: 12px; color: #555;"></span>
        <div style="display: flex; justify-content: center; gap: 4px; margin-top: 7px;">
            <button id="btn-30" onclick="setDayFilter(30)" class="fbtn active">30d</button>
            <button id="btn-60" onclick="setDayFilter(60)" class="fbtn">60d</button>
            <button id="btn-90" onclick="setDayFilter(90)" class="fbtn">90d</button>
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
        var currentDays = 30;
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
        f"🚴 *Bicycle Infrastructure Report Map*\n"
        f"_Last {days_back} days_\n\n"
        f"📊 *{total:,} reports mapped*\n"
        f"🔴 *{open_count:,} open*  ·  🟢 *{closed_count:,} closed*\n\n"
        f"Tap markers to see details. Use layer control to toggle views."
    )

    return buffer, summary
