"""
Water Conservation Violations — data layer and formatters.

Queries Austin Open311 API for service code WWREPORT (Water Conservation Violation).
Residents report sprinkler misuse, leaks, water waste, and irrigation violations.
Austin Water investigates and sends postcards or confirms violations.
"""

import io
import os
import re
import time
import tempfile
import logging
import requests
from collections import Counter
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

OPEN311_BASE_URL = "https://311.austintexas.gov/open311/v2"
SERVICE_CODE     = "WWREPORT"
TIMEOUT          = 10
MAX_RETRIES      = 3
RETRY_DELAY      = 1.0

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
            "User-Agent": "austin311bot/0.1 (Open311 water conservation queries)",
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
            logger.warning(f"Water conservation request failed ({e}), retrying in {delay:.1f}s")
            time.sleep(delay)
            return _make_request(params, retries + 1)
        raise


def _fetch_violations(days_back: int, limit: int = 100) -> list:
    end   = _utc_now()
    start = end - timedelta(days=days_back)
    results = []
    page = 1

    while True:
        batch = _make_request({
            "service_code": SERVICE_CODE,
            "start_date":   _isoformat_z(start),
            "end_date":     _isoformat_z(end),
            "per_page":     limit,
            "page":         page,
        })
        if not batch:
            break
        results.extend(batch)
        if len(batch) < limit:
            break
        page += 1

    return results


# Map status_notes prefixes → readable outcome labels
_OUTCOME_PATTERNS = [
    (r"violation confirmed|confirmed violation",  "✅ Violation confirmed"),
    (r"2nd post",                                 "📬 2nd warning issued"),
    (r"postcard sent",                            "📬 Warning postcard sent"),
    (r"action already taken",                     "✔️ Action already taken"),
    (r"no problem found",                         "🔍 No problem found"),
    (r"insufficient information",                 "❓ Insufficient info"),
    (r"invalid complaint",                        "🚫 Invalid complaint"),
    (r"under investigation",                      "🔎 Under investigation"),
    (r"internal procedures",                      "✅ Violation confirmed"),
]

def _classify_outcome(status_notes: str) -> str:
    lower = (status_notes or "").lower()
    for pattern, label in _OUTCOME_PATTERNS:
        if re.search(pattern, lower):
            return label
    if not status_notes or not status_notes.strip():
        return "🔎 Under investigation"
    return "📋 Other"


# Bucket description text into violation types
_VIOLATION_TYPE_PATTERNS = [
    (r"rain|raining|rainy",               "🌧️ Watering during rain"),
    (r"wrong day|off day|not.*day|day.*not", "📅 Wrong watering day"),
    (r"leak|leaking|broken.*pipe|pipe.*broken", "🔧 Leak / broken pipe"),
    (r"flow.*street|street.*flow|gutter|runoff|overflow|drain", "🌊 Runoff into street"),
    (r"sprinkler|irrigation|spraying",    "💦 Sprinkler / irrigation"),
    (r"hose|washing|car wash|pressure wash", "🪣 Hose / washing"),
    (r"pool|fountain",                    "🏊 Pool / fountain"),
]

def _classify_violation_type(description: str) -> str:
    lower = (description or "").lower()
    for pattern, label in _VIOLATION_TYPE_PATTERNS:
        if re.search(pattern, lower):
            return label
    return "💧 Other water waste"


def _extract_street(address: str) -> str:
    """Pull street name from a full address like '1234 Main St, Austin'."""
    if not address:
        return ""
    # Strip house number and city suffix
    parts = address.split(",")
    street = parts[0].strip()
    street = re.sub(r"^\d+\s+", "", street)  # remove leading house number
    return street.title()


def get_water_conservation_stats(days_back: int = 90) -> dict:
    """Fetch and summarise water conservation violations."""
    records = _fetch_violations(days_back)

    total  = len(records)
    open_  = sum(1 for r in records if (r.get("status") or "").lower() == "open")
    closed = total - open_

    # Outcome breakdown
    outcome_counts: Counter = Counter()
    for r in records:
        outcome_counts[_classify_outcome(r.get("status_notes", ""))] += 1

    # Violation type from description
    type_counts: Counter = Counter()
    for r in records:
        type_counts[_classify_violation_type(r.get("description", ""))] += 1

    # Hotspot streets
    street_counts: Counter = Counter()
    for r in records:
        street = _extract_street(r.get("address", ""))
        if street:
            street_counts[street] += 1

    confirmed = sum(
        v for k, v in outcome_counts.items()
        if "confirmed" in k.lower()
    )

    return {
        "days_back":     days_back,
        "total":         total,
        "open":          open_,
        "closed":        closed,
        "confirmed":     confirmed,
        "outcomes":      outcome_counts.most_common(6),
        "violation_types": type_counts.most_common(6),
        "top_streets":   street_counts.most_common(6),
    }


def format_water_conservation(stats: dict) -> str:
    total    = stats.get("total", 0)
    days     = stats["days_back"]

    if total == 0:
        return f"💧 *Water Conservation Violations*\n\nNo reports found in the last {days} days."

    open_    = stats["open"]
    closed   = stats["closed"]
    confirmed = stats["confirmed"]
    per_day  = round(total / days, 1)

    msg  = f"💧 *Austin Water Conservation Violations — Last {days} Days*\n"
    msg += f"_Reports of sprinkler misuse, leaks, and water waste_\n\n"

    msg += f"📊 *Overview:*\n"
    msg += f"• Reports: {total:,} (~{per_day}/day)\n"
    msg += f"• Open: {open_}  ·  Closed: {closed}\n"
    if confirmed:
        confirm_pct = round(confirmed / total * 100)
        msg += f"• Confirmed violations: {confirmed} ({confirm_pct}%)\n"
    msg += "\n"

    outcomes = stats.get("outcomes", [])
    if outcomes:
        msg += "📋 *Investigation Outcomes:*\n"
        for label, cnt in outcomes:
            msg += f"  {label}: {cnt}\n"
        msg += "\n"

    vtypes = stats.get("violation_types", [])
    if vtypes:
        msg += "🚿 *Violation Types:*\n"
        for label, cnt in vtypes:
            msg += f"  {label}: {cnt}\n"
        msg += "\n"

    streets = stats.get("top_streets", [])
    if streets:
        msg += "📍 *Most Reports by Street:*\n"
        for street, cnt in streets:
            msg += f"  {street}: {cnt}\n"
        msg += "\n"

    msg += "_Source: [Austin 311 — Water Conservation](https://311.austintexas.gov)_"
    return msg


# =============================================================================
# MAP GENERATOR
# =============================================================================

def generate_water_map(days_back: int = 90) -> tuple:
    """Generate an interactive HTML map of water conservation violation reports.

    Returns:
        tuple: (BytesIO buffer with HTML content, summary message)
    """
    try:
        import folium
        from folium.plugins import MarkerCluster
    except ImportError:
        return None, "❌ Map generation requires 'folium'. Install: pip install folium"

    records_raw = _fetch_violations(days_back)

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
        return None, "💧 No water conservation reports with location data found."

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
        description = (r.get("description") or "").strip()
        status_notes = (r.get("status_notes") or "").strip()
        date_str = (r.get("requested_datetime") or "").split("T")[0]
        updated_str = (r.get("updated_datetime") or "").split("T")[0]
        address = (r.get("address") or "").strip()
        req_id = r.get("service_request_id", "N/A")
        violation_type = _classify_violation_type(description)
        outcome = _classify_outcome(status_notes)

        age = _age_days(r)
        bucket = "30" if age <= 30 else ("60" if age <= 60 else "90")
        cluster_key = f"{status}_{bucket}"
        if cluster_key not in fg_clusters:
            cluster_key = f"closed_{bucket}"

        address_line = f"<b>Address:</b> {address}<br/>" if address else ""
        updated_line = f"<span style='color:#666;'>Updated: {updated_str}</span><br/>" if updated_str and updated_str != date_str else ""
        desc_short = (description[:500] + "...") if len(description) > 500 else description
        desc_block = f"<b>Description:</b><br/><i>{desc_short.replace(chr(10), '<br/>')}</i><br/>" if desc_short else ""
        outcome_block = f"<b>Outcome:</b> {outcome}<br/>" if status_notes else ""

        ticket_url = f"https://311.austintexas.gov/tickets/{req_id}"
        popup_html = f"""
        <div style="font-family:sans-serif;max-width:300px;">
            <b><a href="{ticket_url}" target="_blank" style="color:#0066cc;">Report #{req_id}</a></b><br/>
            <span style="color:#666;">Filed: {date_str}</span><br/>
            {updated_line}
            {address_line}
            <br/>
            <b>Status:</b> {'🔴 Open' if status == 'open' else '🟢 Closed'}<br/>
            <b>Type:</b> {violation_type}<br/>
            {outcome_block}<br/>
            {desc_block}
        </div>
        """
        popup = folium.Popup(popup_html, max_width=300)
        if status == "open":
            icon = folium.Icon(color="blue", icon="tint", prefix="glyphicon")
            tooltip = f"Open: {violation_type}"
        else:
            icon = folium.Icon(color="green", icon="ok-sign", prefix="glyphicon")
            tooltip = f"Closed: {violation_type}"

        folium.Marker(location=[lat, lon], popup=popup, icon=icon, tooltip=tooltip).add_to(fg_clusters[cluster_key])

    map_var = m.get_name()
    layer_map_js = "{" + ", ".join(f'"{k}": {fg_objects[k].get_name()}' for k in fg_objects) + "}"
    panel_html = f"""
    <div id="map-panel" style="position:absolute;top:10px;left:50%;transform:translateX(-50%);
                background:white;padding:10px 16px;border-radius:6px;
                box-shadow:0 2px 6px rgba(0,0,0,0.3);z-index:9999;
                font-family:sans-serif;text-align:center;">
        <b style="font-size:15px;">💧 Austin Water Conservation Violation Reports</b><br/>
        <span id="map-summary" style="font-size:12px;color:#555;"></span>
        <div style="display:flex;justify-content:center;gap:4px;margin-top:7px;">
            <button id="btn-30" onclick="setDayFilter(30)" class="fbtn">30d</button>
            <button id="btn-60" onclick="setDayFilter(60)" class="fbtn">60d</button>
            <button id="btn-90" onclick="setDayFilter(90)" class="fbtn active">90d</button>
            <span style="margin:0 4px;color:#ccc;">|</span>
            <button id="btn-open" onclick="toggleStatus('open')" class="fbtn active">🔵 Open</button>
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
        f"💧 *Water Conservation Violation Map*\n"
        f"_Last {days_back} days_\n\n"
        f"📊 *{len(records):,} reports mapped*\n"
        f"🔵 *{open_count:,} open*  ·  🟢 *{closed_count:,} closed*\n\n"
        f"Tap markers to see violation type and outcome. Use buttons to filter."
    )
    return buffer, summary
