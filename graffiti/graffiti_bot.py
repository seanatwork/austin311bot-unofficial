"""
Graffiti Analysis — live Open311 API queries.
Replaces the old SQLite-backed implementation.
"""

import time
import logging
import requests
import os
import io
from datetime import datetime, timezone, timedelta
from collections import Counter
from typing import Optional

from .config import Config

logger = logging.getLogger(__name__)

OPEN311_BASE_URL = "https://311.austintexas.gov/open311/v2"
TIMEOUT = 15
MAX_RETRIES = 3
MAX_PAGES = 10

API_KEY = os.getenv("AUSTIN_APP_TOKEN")

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
            "User-Agent": "austin311bot/0.1 (graffiti queries)",
        }
        if API_KEY:
            headers["X-Api-Key"] = API_KEY
        _session.headers.update(headers)
    return _session


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _isoformat_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


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


def _fetch_graffiti(days_back: int = 90) -> list:
    """Fetch graffiti records from Open311 API with pagination."""
    end = _utc_now()
    start = end - timedelta(days=days_back)
    url = f"{OPEN311_BASE_URL}/requests.json"
    params = {
        "service_code": Config.SERVICE_CODE,
        "start_date": _isoformat_z(start),
        "end_date": _isoformat_z(end),
        "per_page": 100,
        "page": 1,
    }

    all_records = []
    seen_ids: set = set()
    page = 1
    session = _get_session()

    while page <= MAX_PAGES:
        retries = 0
        while True:
            try:
                resp = session.get(url, params=params, timeout=TIMEOUT)
                resp.raise_for_status()
                data = resp.json()
                break
            except requests.exceptions.HTTPError as e:
                if e.response.status_code in {429, 500, 502, 503, 504} and retries < MAX_RETRIES:
                    delay = (10.0 * (2 ** retries)) if e.response.status_code == 429 else 2.0 * (2 ** retries)
                    logger.warning(f"HTTP {e.response.status_code}, retrying in {delay:.1f}s ({retries+1}/{MAX_RETRIES})")
                    time.sleep(delay)
                    retries += 1
                else:
                    raise
            except RETRYABLE_ERRORS as e:
                retries += 1
                if retries >= MAX_RETRIES:
                    raise
                time.sleep(2.0 * (2 ** retries))

        if not isinstance(data, list) or not data:
            break

        for r in data:
            sid = r.get("service_request_id")
            if sid and sid not in seen_ids:
                seen_ids.add(sid)
                r["_service_label"] = "Graffiti Abatement"
                r["_service_code"] = Config.SERVICE_CODE
                all_records.append(r)

        if len(data) < 100:
            break

        params["page"] += 1
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
    week_ago = now - timedelta(days=7)

    open_count = 0
    closed_count = 0
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
                if dt >= week_ago:
                    last_7_days += 1
            except (ValueError, TypeError):
                pass

    msg = f"🎨 *Graffiti Analysis — Last {days_back} Days*\n\n"
    msg += f"📊 *Total reports:* {total}\n"
    msg += f"✅ *Closed:* {closed_count}  🔴 *Open:* {open_count}\n"
    msg += f"🗓 *Last 7 days:* {last_7_days} new reports\n"

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


# =============================================================================
# INTERACTIVE MAP GENERATION
# =============================================================================

def fetch_graffiti_with_coords(days_back: int = 30) -> dict:
    """Fetch all graffiti reports and filter to those with valid coordinates.

    Returns both open AND closed requests with location data for mapping.
    """
    records = _fetch_graffiti(days_back)

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


def generate_graffiti_map(days_back: int = 30) -> tuple[Optional[io.BytesIO], str]:
    """Generate an interactive HTML map of graffiti reports.

    Returns:
        tuple: (BytesIO buffer with HTML content, summary message)
    """
    try:
        import folium
        from folium.plugins import MarkerCluster
    except ImportError:
        return None, "❌ Map generation requires 'folium' library. Install with: pip install folium"

    data = fetch_graffiti_with_coords(days_back)
    records = data["records"]
    total = data["total"]

    if not records:
        return None, f"🎨 No graffiti reports with location data found in the last {days_back} days."

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
    panel_html = f"""
    <div id="map-panel" style="position: absolute; top: 10px; left: 50%; transform: translateX(-50%);
                background: white; padding: 10px 16px; border-radius: 6px;
                box-shadow: 0 2px 6px rgba(0,0,0,0.3); z-index: 9999;
                font-family: sans-serif; text-align: center;">
        <b style="font-size: 15px;">🎨 Austin Graffiti Abatement 311 Reports</b><br/>
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
        f"🎨 *Graffiti Abatement Report Map*\n"
        f"_Last {days_back} days_\n\n"
        f"📊 *{total:,} reports mapped*\n"
        f"🔴 *{open_count:,} open*  ·  🟢 *{closed_count:,} closed*\n\n"
        f"Tap markers to see details. Use layer control to toggle views."
    )

    return buffer, summary
