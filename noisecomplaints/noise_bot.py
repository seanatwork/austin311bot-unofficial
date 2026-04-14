"""
Noise Complaints — data layer and formatters.

Queries Austin Open311 API live across noise and quality-of-life service codes.
Provides hotspot (by street), complaint type stats, and response time analysis.
"""

import io
import os
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

SERVICE_CODES = {
    "APDNONNO": "Non-Emergency Noise Complaint",
    "DSOUCVMC": "Outdoor Venue / Music Complaint",
    "AFDFIREW": "Fireworks Complaint",
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
            "User-Agent": "austin311bot/0.1 (Open311 noise queries)",
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
    for r in records:
        r["_service_label"] = SERVICE_CODES.get(service_code, service_code)
    return records


def fetch_all_noise_complaints(days_back: int = 90, limit_per_code: int = 100) -> list:
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
# HOTSPOTS BY STREET
# =============================================================================

def _extract_street(address: str) -> str:
    addr = address.replace(", Austin", "").strip()
    parts = addr.split(" ", 1)
    if len(parts) == 2 and parts[0].isdigit():
        return parts[1].strip()
    return addr


def get_hotspots(days_back: int = 90) -> dict:
    records = fetch_all_noise_complaints(days_back)
    if not records:
        return {"hotspots": [], "total": 0, "days_back": days_back}

    half = days_back // 2
    cutoff = _utc_now() - timedelta(days=half)

    recent_counts: dict = {}   # last half of window
    older_counts: dict = {}    # prior half of window
    street_types: dict = {}

    for r in records:
        address = (r.get("address") or "").strip()
        street = _extract_street(address) if address else "Unknown"
        label = r.get("_service_label", "Unknown")

        dt_str = r.get("requested_datetime") or ""
        try:
            dt_utc = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            in_recent = dt_utc >= cutoff
        except (ValueError, TypeError):
            in_recent = True

        if in_recent:
            recent_counts[street] = recent_counts.get(street, 0) + 1
        else:
            older_counts[street] = older_counts.get(street, 0) + 1

        street_types.setdefault(street, {})
        street_types[street][label] = street_types[street].get(label, 0) + 1

    # Combined count for ranking
    all_streets = set(recent_counts) | set(older_counts)
    street_counts = {s: recent_counts.get(s, 0) + older_counts.get(s, 0) for s in all_streets}
    # Chronic = appeared in both halves
    chronic = {s for s in all_streets if s in recent_counts and s in older_counts}

    hotspots = sorted(street_counts.items(), key=lambda x: -x[1])
    return {
        "hotspots": hotspots,
        "street_types": street_types,
        "chronic": chronic,
        "total": len(records),
        "days_back": days_back,
    }


def format_hotspots(data: dict) -> str:
    hotspots = data.get("hotspots", [])
    street_types = data.get("street_types", {})
    chronic = data.get("chronic", set())
    total = data.get("total", 0)
    days_back = data.get("days_back", 90)

    if not hotspots:
        return "📝 No noise complaints found."

    msg = f"🔊 *Top Noise Complaint Streets*\n"
    msg += f"_Last {days_back} days · {total} total complaints_\n"
    msg += f"_🔴 Chronic = problem both halves of window · 🟡 New = recent only_\n\n"

    top = hotspots[:10]
    max_count = top[0][1]

    for i, (street, count) in enumerate(top, 1):
        tag = "🔴" if street in chronic else "🟡"
        bar = "█" * min(10, round(count / max_count * 10))
        msg += f"{i}. {tag} *{street}*\n"
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
# PEAK TIMES & WEEKLY TREND
# =============================================================================

# Austin local time approximation (CDT = UTC-5, CST = UTC-6; we use -6 as default)
_AUSTIN_OFFSET = timedelta(hours=-6)

_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_DAY_SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _fmt_hour(h: int) -> str:
    if h == 0:
        return "12am"
    if h < 12:
        return f"{h}am"
    if h == 12:
        return "12pm"
    return f"{h - 12}pm"


def get_peak_times(days_back: int = 56) -> dict:
    records = fetch_all_noise_complaints(days_back, limit_per_code=100)
    if not records:
        return {"total": 0, "days_back": days_back}

    now = _utc_now()
    # day_hour[weekday][hour] = count
    day_hour: list = [[0] * 24 for _ in range(7)]
    weekly: list = [0] * 8  # index 7 = most recent week, 0 = oldest

    for r in records:
        dt_str = r.get("requested_datetime") or ""
        if not dt_str:
            continue
        try:
            dt_utc = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            dt_local = dt_utc + _AUSTIN_OFFSET
            day_hour[dt_local.weekday()][dt_local.hour] += 1

            days_ago = (now - dt_utc).days
            week_idx = min(7, days_ago // 7)
            weekly[7 - week_idx] += 1
        except (ValueError, TypeError):
            pass

    # Overall peak (day + hour)
    peak_day = peak_hour = peak_count = 0
    for d in range(7):
        for h in range(24):
            if day_hour[d][h] > peak_count:
                peak_count = day_hour[d][h]
                peak_day, peak_hour = d, h

    # Per-day peak hour
    day_peaks = [
        (_DAYS[d], _DAY_SHORT[d], day_hour[d].index(max(day_hour[d])), max(day_hour[d]))
        for d in range(7)
    ]

    # Week-start labels (Mon of each week, oldest first)
    week_labels = []
    for i in range(8):
        week_start = now - timedelta(days=(7 - i) * 7)
        week_labels.append(week_start.strftime("%-m/%-d"))

    return {
        "total": sum(weekly),
        "days_back": days_back,
        "peak_day": _DAYS[peak_day],
        "peak_hour": peak_hour,
        "peak_count": peak_count,
        "day_peaks": day_peaks,
        "weekly": weekly,
        "week_labels": week_labels,
    }


def format_peak_times(data: dict) -> str:
    if not data.get("total"):
        return "📝 Not enough data to analyze peak times."

    peak_day = data["peak_day"]
    peak_hour = data["peak_hour"]
    peak_count = data["peak_count"]
    day_peaks = data["day_peaks"]
    weekly = data["weekly"]
    week_labels = data["week_labels"]
    days_back = data["days_back"]

    # Headline
    msg = f"🕐 *Noise Complaints — When They Happen*\n"
    msg += f"_Last {days_back} days · {data['total']} total complaints_\n\n"
    msg += f"📍 *Peak:* {peak_day}s around *{_fmt_hour(peak_hour)}* ({peak_count} complaints)\n\n"

    # Peak hour per day of week
    msg += "*Peak hour by day:*\n"
    for _, short, peak_h, count in day_peaks:
        bar = "█" * min(8, round(count / max(1, peak_count) * 8))
        msg += f"  `{short}` {_fmt_hour(peak_h):>5}  {bar} {count}\n"

    msg += "\n"

    # 8-week trend
    msg += "*Weekly volume — last 8 weeks:*\n"
    max_week = max(weekly) if weekly else 1
    for i, (label, count) in enumerate(zip(week_labels, weekly)):
        bar = "█" * min(10, round(count / max(1, max_week) * 10))
        recency = " ◀ this wk" if i == 7 else ""
        msg += f"  `{label}` {bar} {count}{recency}\n"

    msg += "\n_Source: [Austin Open311 API](https://311.austintexas.gov/open311/v2)_"
    return msg


# =============================================================================
# RESOLUTION RATE BY COMPLAINT TYPE
# =============================================================================

def get_resolution_by_type(days_back: int = 90) -> dict:
    """Return open/closed counts and resolution rate per complaint type."""
    records = fetch_all_noise_complaints(days_back)
    if not records:
        return {"types": {}, "total": 0, "days_back": days_back}

    types: dict = {}
    for r in records:
        label = r.get("_service_label", "Unknown")
        status = (r.get("status") or "").lower()
        if label not in types:
            types[label] = {"open": 0, "closed": 0}
        if status == "closed":
            types[label]["closed"] += 1
        else:
            types[label]["open"] += 1

    return {"types": types, "total": len(records), "days_back": days_back}


_TYPE_EMOJI = {
    "Non-Emergency Noise Complaint": "🔊",
    "Outdoor Venue / Music Complaint": "🎵",
    "Fireworks Complaint": "🎆",
}


def format_resolution_by_type(data: dict) -> str:
    types = data.get("types", {})
    total = data.get("total", 0)
    days_back = data.get("days_back", 90)

    if not types:
        return "📝 No noise complaint data available."

    msg = "🔊 *Noise Complaints — Resolution by Type*\n"
    msg += f"_Last {days_back} days · {total} total complaints_\n\n"

    for label, counts in sorted(types.items(), key=lambda x: -(x[1]["open"] + x[1]["closed"])):
        subtotal = counts["open"] + counts["closed"]
        resolved_pct = round(counts["closed"] / subtotal * 100) if subtotal else 0
        open_pct = 100 - resolved_pct
        bar_filled = round(resolved_pct / 10)
        bar = "█" * bar_filled + "░" * (10 - bar_filled)
        emoji = _TYPE_EMOJI.get(label, "📋")
        msg += f"{emoji} *{label}*\n"
        msg += f"   {bar} {resolved_pct}% resolved\n"
        msg += f"   {subtotal} total · {counts['closed']} closed · {counts['open']} still open\n\n"

    msg += "_Source: [Austin Open311 API](https://311.austintexas.gov/open311/v2)_"
    return msg


# =============================================================================
# LATE-NIGHT WINDOW BREAKDOWN
# =============================================================================

def get_night_breakdown(days_back: int = 90) -> dict:
    """Bucket complaints into Evening / Late Night / Early Morning windows."""
    records = fetch_all_noise_complaints(days_back)
    if not records:
        return {"buckets": {}, "total": 0, "days_back": days_back}

    # Windows in Austin local time (approx UTC-6)
    buckets: dict = {
        "evening":   {"label": "Evening",    "hours": "8pm–midnight", "count": 0},
        "late_night":{"label": "Late Night", "hours": "Midnight–3am", "count": 0},
        "early_am":  {"label": "Early AM",   "hours": "3am–7am",      "count": 0},
        "daytime":   {"label": "Daytime",    "hours": "7am–8pm",      "count": 0},
    }
    type_buckets: dict = {}  # bucket_key -> {label: count}

    for r in records:
        dt_str = r.get("requested_datetime") or ""
        label = r.get("_service_label", "Unknown")
        try:
            dt_utc = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            h = (dt_utc + _AUSTIN_OFFSET).hour
        except (ValueError, TypeError):
            continue

        if h >= 20:
            key = "evening"
        elif h < 3:
            key = "late_night"
        elif h < 7:
            key = "early_am"
        else:
            key = "daytime"

        buckets[key]["count"] += 1
        type_buckets.setdefault(key, {})
        type_buckets[key][label] = type_buckets[key].get(label, 0) + 1

    return {
        "buckets": buckets,
        "type_buckets": type_buckets,
        "total": len(records),
        "days_back": days_back,
    }


def format_night_breakdown(data: dict) -> str:
    buckets = data.get("buckets", {})
    type_buckets = data.get("type_buckets", {})
    total = data.get("total", 0)
    days_back = data.get("days_back", 90)

    if not total:
        return "📝 Not enough data."

    msg = "🌙 *Noise Complaints — Time of Night*\n"
    msg += f"_Last {days_back} days · {total} total complaints_\n\n"

    order = [
        ("evening",    "🌆"),
        ("late_night", "🌙"),
        ("early_am",   "🌅"),
        ("daytime",    "☀️"),
    ]
    max_count = max(b["count"] for b in buckets.values()) or 1

    for key, emoji in order:
        b = buckets[key]
        count = b["count"]
        pct = round(count / total * 100) if total else 0
        bar = "█" * min(10, round(count / max_count * 10))
        msg += f"{emoji} *{b['label']}* _{b['hours']}_\n"
        msg += f"   {bar} {count} complaints ({pct}%)\n"
        # Top complaint type for this window
        tb = type_buckets.get(key, {})
        if tb:
            top_type, top_count = max(tb.items(), key=lambda x: x[1])
            msg += f"   _Most common: {top_type} ({top_count})_\n"
        msg += "\n"

    msg += "_Source: [Austin Open311 API](https://311.austintexas.gov/open311/v2)_"
    return msg


# =============================================================================
# MAP GENERATOR
# =============================================================================

def generate_noise_map(days_back: int = 90) -> tuple:
    """Generate an interactive HTML map of noise complaints.

    Returns:
        tuple: (BytesIO buffer with HTML content, summary message)
    """
    try:
        import folium
        from folium.plugins import MarkerCluster
    except ImportError:
        return None, "❌ Map generation requires 'folium'. Install: pip install folium"

    records_raw = fetch_all_noise_complaints(days_back, limit_per_code=200)

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
        return None, "🔊 No noise complaints with location data found."

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
        service_label = r.get("_service_label", "Noise Complaint")
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
        <b style="font-size:15px;">🔊 Austin Noise Complaints 311 Reports</b><br/>
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
        f"🔊 *Noise Complaint Map*\n"
        f"_Last {days_back} days_\n\n"
        f"📊 *{len(records):,} complaints mapped*\n"
        f"🔴 *{open_count:,} open*  ·  🟢 *{closed_count:,} closed*\n\n"
        f"Tap markers to see details. Use buttons to filter by time window."
    )
    return buffer, summary
