"""
Animal Services — data layer and formatters.

Queries Austin Open311 API live across all animal service codes.
Provides hotspot (by zipcode), complaint type stats, and response time analysis.
"""

import io
import os
import re
import time
import tempfile
import logging
import requests
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

OPEN311_BASE_URL = "https://311.austintexas.gov/open311/v2"
TIMEOUT = 10
MAX_RETRIES = 3
RETRY_DELAY = 1.0

# Service codes and human-readable labels
SERVICE_CODES = {
    "ACLONAG":  "Loose Dog",
    "ACLOANIM": "Loose Animal (Not Dog)",
    "ACBITE2":  "Animal Bite",
    "COAACDD":  "Vicious Dog",
    "ACPROPER": "Animal Care Concern",
    "WILDEXPO": "Wildlife Exposure",
    "ACINFORM": "Animal Protection Request",
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
        _session.headers.update({
            "Accept": "application/json",
            "User-Agent": "austin311bot/0.1 (Open311 animal queries)",
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


def _fetch_code(service_code: str, days_back: int, limit: int = 100) -> list:
    end = _utc_now()
    start = end - timedelta(days=days_back)
    params = {
        "service_code": service_code,
        "start_date": _isoformat_z(start),
        "end_date": _isoformat_z(end),
        "per_page": limit,
        "page": 1,
    }
    records = _make_request(params)
    # Tag each record with its service code label
    for r in records:
        r["_service_label"] = SERVICE_CODES.get(service_code, service_code)
    return records


def fetch_all_animal_complaints(days_back: int = 90, limit_per_code: int = 100) -> list:
    """Fetch complaints across all animal service codes."""
    all_records = []
    for code in SERVICE_CODES:
        try:
            records = _fetch_code(code, days_back, limit_per_code)
            all_records.extend(records)
            logger.debug(f"{code}: {len(records)} records")
        except Exception as e:
            logger.warning(f"Failed to fetch {code}: {e}")
    return all_records


# =============================================================================
# HOTSPOTS BY ZIPCODE
# =============================================================================

def _extract_street(address: str) -> str:
    """Extract street name from '1234 Some St, Austin' → 'Some St'."""
    addr = address.replace(", Austin", "").strip()
    # Remove leading house number
    parts = addr.split(" ", 1)
    if len(parts) == 2 and parts[0].isdigit():
        return parts[1].strip()
    return addr


def get_hotspots(days_back: int = 90) -> dict:
    """Return complaint counts grouped by street name, sorted by volume."""
    records = fetch_all_animal_complaints(days_back)
    if not records:
        return {"hotspots": [], "total": 0, "days_back": days_back}

    street_counts: dict = {}
    street_types: dict = {}

    for r in records:
        address = (r.get("address") or "").strip()
        street = _extract_street(address) if address else "Unknown"
        label = r.get("_service_label", "Unknown")

        street_counts[street] = street_counts.get(street, 0) + 1
        street_types.setdefault(street, {})
        street_types[street][label] = street_types[street].get(label, 0) + 1

    hotspots = sorted(street_counts.items(), key=lambda x: -x[1])

    return {
        "hotspots": hotspots,
        "street_types": street_types,
        "total": len(records),
        "days_back": days_back,
    }


def format_hotspots(data: dict) -> str:
    hotspots = data.get("hotspots", [])
    street_types = data.get("street_types", {})
    total = data.get("total", 0)
    days_back = data.get("days_back", 90)

    if not hotspots:
        return "📝 No animal complaints found."

    msg = f"🐕 *Top Animal Complaint Streets*\n"
    msg += f"_Last {days_back} days · {total} total complaints_\n\n"

    top = hotspots[:10]
    max_count = top[0][1]

    for i, (street, count) in enumerate(top, 1):
        bar = "█" * min(10, round(count / max_count * 10))
        msg += f"{i}. *{street}*\n"
        msg += f"   {bar} {count} complaint{'s' if count > 1 else ''}\n"
        types = street_types.get(street, {})
        top_types = sorted(types.items(), key=lambda x: -x[1])[:2]
        if top_types:
            type_str = " · ".join(f"{t} ({c})" for t, c in top_types)
            msg += f"   _{type_str}_\n"
        msg += "\n"

    msg += "_Source: [Austin Open311 API](https://311.austintexas.gov/open311/v2)_"
    return msg


# =============================================================================
# STATS BY COMPLAINT TYPE
# =============================================================================

def get_stats(days_back: int = 90) -> dict:
    """Return complaint counts by service type."""
    records = fetch_all_animal_complaints(days_back)
    if not records:
        return {"total": 0, "days_back": days_back}

    type_counts: dict = {}
    for r in records:
        label = r.get("_service_label", "Unknown")
        type_counts[label] = type_counts.get(label, 0) + 1

    return {
        "total": len(records),
        "type_counts": type_counts,
        "days_back": days_back,
    }


def format_stats(data: dict) -> str:
    if data.get("total", 0) == 0:
        return f"📝 No animal complaints found in the past {data.get('days_back', 90)} days."

    total = data["total"]
    msg = f"🐾 *Animal Complaints — Last {data['days_back']} Days*\n\n"
    msg += f"📊 *Total complaints:* {total}\n\n"

    msg += "📋 *By complaint type:*\n"
    for label, count in sorted(data["type_counts"].items(), key=lambda x: -x[1]):
        pct = count / total * 100
        bar = "█" * min(10, round(pct / 10))
        msg += f"   *{label}*: {count} ({pct:.1f}%)\n"
        msg += f"   {bar}\n"

    msg += "\n_Source: [Austin Open311 API](https://311.austintexas.gov/open311/v2)_"
    return msg


# =============================================================================
# RESPONSE TIME
# =============================================================================

def get_response_times(days_back: int = 90) -> dict:
    """Calculate average response time per complaint type for closed tickets."""
    records = fetch_all_animal_complaints(days_back)
    if not records:
        return {"total": 0, "days_back": days_back}

    type_times: dict = {}  # label → list of days to close

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
        except ValueError:
            pass

    averages = {
        label: round(sum(times) / len(times), 1)
        for label, times in type_times.items()
        if times
    }

    overall_all = [d for times in type_times.values() for d in times]
    overall_avg = round(sum(overall_all) / len(overall_all), 1) if overall_all else None

    return {
        "averages": averages,
        "overall_avg": overall_avg,
        "total_closed": len(overall_all),
        "days_back": days_back,
    }


def format_response_times(data: dict) -> str:
    if not data.get("averages"):
        return "📝 Not enough closed complaints to calculate response times."

    msg = f"⏱ *Animal Services Response Times*\n"
    msg += f"_Based on {data['total_closed']} closed complaints (last {data['days_back']} days)_\n\n"

    if data.get("overall_avg") is not None:
        msg += f"📊 *Overall average:* {data['overall_avg']} days\n\n"

    msg += "📋 *By complaint type:*\n"
    for label, avg in sorted(data["averages"].items(), key=lambda x: x[1]):
        # Emoji based on speed
        if avg <= 1:
            speed = "🟢"
        elif avg <= 5:
            speed = "🟡"
        else:
            speed = "🔴"
        msg += f"   {speed} *{label}:* {avg} days avg\n"

    msg += "\n_Source: [Austin Open311 API](https://311.austintexas.gov/open311/v2)_"
    return msg


# =============================================================================
# MAP GENERATOR
# =============================================================================

# Open markers colored by severity; closed always green
_LABEL_COLOR = {
    "Animal Bite":               ("red",       "exclamation-sign"),
    "Vicious Dog":               ("red",       "exclamation-sign"),
    "Wildlife Exposure":         ("orange",    "warning-sign"),
    "Loose Dog":                 ("blue",      "info-sign"),
    "Loose Animal (Not Dog)":    ("blue",      "info-sign"),
    "Animal Care Concern":       ("purple",    "heart"),
    "Animal Protection Request": ("purple",    "heart"),
}
_DEFAULT_COLOR = ("cadetblue", "info-sign")

# camelCase slugs used as layer-key segment (no underscores — split-safe)
_TYPE_SLUGS = {
    "Animal Bite":               "animalBite",
    "Vicious Dog":               "viciousDog",
    "Wildlife Exposure":         "wildlife",
    "Loose Dog":                 "looseDog",
    "Loose Animal (Not Dog)":    "looseAnimal",
    "Animal Care Concern":       "animalCare",
    "Animal Protection Request": "animalProtection",
}
_DEFAULT_SLUG = "other"

# Ordered list for the dropdown (severity first)
_TYPE_OPTIONS = [
    ("animalBite",       "🔴 Animal Bite"),
    ("viciousDog",       "🔴 Vicious Dog"),
    ("wildlife",         "🟠 Wildlife Exposure"),
    ("looseDog",         "🔵 Loose Dog"),
    ("looseAnimal",      "🔵 Loose Animal (Not Dog)"),
    ("animalCare",       "🟣 Animal Care Concern"),
    ("animalProtection", "🟣 Animal Protection Request"),
]


def generate_animal_map(days_back: int = 90) -> tuple:
    """Generate an interactive HTML map of animal services complaints.

    Returns:
        tuple: (BytesIO buffer with HTML content, summary message)
    """
    try:
        import folium
        from folium.plugins import MarkerCluster
    except ImportError:
        return None, "❌ Map generation requires 'folium'. Install: pip install folium"

    records_raw = fetch_all_animal_complaints(days_back, limit_per_code=200)

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
        return None, "🐾 No animal services reports with location data found."

    open_count = sum(1 for r in records if (r.get("status") or "").lower() == "open")
    closed_count = len(records) - open_count

    def _age_days(r):
        try:
            dt = datetime.fromisoformat(r.get("requested_datetime", "").replace("Z", "+00:00"))
            return (now_dt - dt).days
        except Exception:
            return days_back

    # Count per type × bucket for the summary bar (matches traffic map pattern)
    all_slugs = list(_TYPE_SLUGS.values()) + [_DEFAULT_SLUG]
    type_bucket_counts = {
        slug: {"30": {"open": 0, "closed": 0}, "60": {"open": 0, "closed": 0}, "90": {"open": 0, "closed": 0}}
        for slug in all_slugs + ["all"]
    }
    for r in records:
        age = _age_days(r)
        status = (r.get("status") or "").lower()
        s = status if status in ("open", "closed") else "closed"
        slug = _TYPE_SLUGS.get(r.get("_service_label", ""), _DEFAULT_SLUG)
        for bucket_days in (30, 60, 90):
            if age <= bucket_days:
                b = str(bucket_days)
                type_bucket_counts["all"][b][s] += 1
                type_bucket_counts[slug][b][s] += 1
    counts_js = str(type_bucket_counts).replace("'", '"')

    m = folium.Map(location=[30.2672, -97.7431], zoom_start=11, tiles="CartoDB positron")

    # Layer key: {status}_{bucket}_{typeSlug}  — no underscores in slug so split('_') is safe
    fg_clusters = {}
    fg_objects = {}
    for status_key in ("open", "closed"):
        for bucket in ("30", "60", "90"):
            for slug in all_slugs:
                name = f"{status_key}_{bucket}_{slug}"
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
        service_label = r.get("_service_label", "Animal Complaint")
        description = (r.get("description") or "").strip()
        status_notes = (r.get("status_notes") or "").strip()
        date_str = (r.get("requested_datetime") or "").split("T")[0]
        updated_str = (r.get("updated_datetime") or "").split("T")[0]
        address = (r.get("address") or "").strip()
        req_id = r.get("service_request_id", "N/A")

        age = _age_days(r)
        bucket = "30" if age <= 30 else ("60" if age <= 60 else "90")
        slug = _TYPE_SLUGS.get(service_label, _DEFAULT_SLUG)
        cluster_key = f"{status}_{bucket}_{slug}"
        if cluster_key not in fg_clusters:
            cluster_key = f"closed_{bucket}_{slug}"

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
            color, icon_name = _LABEL_COLOR.get(service_label, _DEFAULT_COLOR)
        else:
            color, icon_name = "green", "ok-sign"
        tooltip = f"{'Open' if status == 'open' else 'Closed'}: {service_label}"

        folium.Marker(
            location=[lat, lon],
            popup=popup,
            icon=folium.Icon(color=color, icon=icon_name, prefix="glyphicon"),
            tooltip=tooltip,
        ).add_to(fg_clusters[cluster_key])

    type_options_html = '<option value="all">All Types</option>\n'
    for slug, label in _TYPE_OPTIONS:
        type_options_html += f'<option value="{slug}">{label}</option>\n'

    map_var = m.get_name()
    layer_map_js = "{" + ", ".join(f'"{k}": {fg_objects[k].get_name()}' for k in fg_objects) + "}"
    panel_html = f"""
    <div id="map-panel" style="position:absolute;top:10px;left:50%;transform:translateX(-50%);
                background:white;padding:10px 16px;border-radius:6px;
                box-shadow:0 2px 6px rgba(0,0,0,0.3);z-index:9999;
                font-family:sans-serif;text-align:center;">
        <b style="font-size:15px;">🐾 Austin Animal Services 311 Reports</b><br/>
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
    <div id="type-panel" style="position:absolute;top:10px;right:10px;
                background:white;padding:8px 12px;border-radius:6px;
                box-shadow:0 2px 6px rgba(0,0,0,0.3);z-index:9999;
                font-family:sans-serif;">
        <label for="type-select" style="font-size:11px;font-weight:bold;color:#444;display:block;margin-bottom:4px;">Filter by Type</label>
        <select id="type-select" onchange="setTypeFilter(this.value)"
                style="font-size:12px;padding:3px 6px;border:1px solid #ccc;border-radius:4px;cursor:pointer;width:100%;">
            {type_options_html}
        </select>
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
        var currentType = 'all';
        var layerMap = null;
        var leafletMap = null;
        var typeBucketCounts = {counts_js};

        function updateSummary() {{
            var d = String(currentDays);
            var catData = typeBucketCounts[currentType] || typeBucketCounts['all'];
            var counts = catData[d] || {{}};
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
                var typeSlug = parts[2];
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

        function setTypeFilter(type) {{
            currentType = type;
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
        f"🐾 *Animal Services Map*\n"
        f"_Last {days_back} days_\n\n"
        f"📊 *{len(records):,} reports mapped*\n"
        f"🔴 *{open_count:,} open*  ·  🟢 *{closed_count:,} closed*\n\n"
        f"Open markers colored by severity. Use the type filter to focus on bites, loose dogs, etc."
    )
    return buffer, summary
