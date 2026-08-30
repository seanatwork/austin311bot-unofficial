"""
Dead Animal Collection — data layer and map generator.

Service code:
  ZZARDEAC  Dead Animal Collection  (~1,600 reports/90 days, ~26K all-time)

SWSDEADA (ARR Dead Animal Collection) returned 0 records in 90-day checks — retired.
"""

import io
import os
import time
import logging
import tempfile
import requests
from datetime import datetime, timezone, timedelta
from typing import Optional

from open311_client import open311_get, og_meta_tags

logger = logging.getLogger(__name__)

OPEN311_BASE_URL = "https://311.austintexas.gov/open311/v2"

SERVICE_CODES = {
    "ZZARDEAC": "Dead Animal Collection",
}

API_KEY = os.getenv("AUSTINAPIKEY")

MAX_PAGES = 20    # cap at 2,000 records per window
PAGE_DELAY = 0.4  # seconds between page requests to avoid 429s

_session: Optional[requests.Session] = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        headers = {
            "Accept": "application/json",
            "User-Agent": "austin311bot/0.1 (Open311 dead-animal queries)",
        }
        if API_KEY:
            headers["X-Api-Key"] = API_KEY
        _session.headers.update(headers)
    return _session


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _isoformat_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _fetch_code(service_code: str, days_back: int) -> list:
    end = _utc_now()
    start = end - timedelta(days=days_back)
    label = SERVICE_CODES.get(service_code, service_code)
    all_records: list = []
    for page in range(1, MAX_PAGES + 1):
        if page > 1:
            time.sleep(PAGE_DELAY)
        params = {
            "service_code": service_code,
            "start_date": _isoformat_z(start),
            "end_date": _isoformat_z(end),
            "per_page": 100,
            "page": page,
        }
        batch = open311_get(_get_session(), f"{OPEN311_BASE_URL}/requests.json", params)
        if not batch:
            break
        for r in batch:
            r["_service_label"] = label
        all_records.extend(batch)
        if len(batch) < 100:
            break
    return all_records


def fetch_dead_animal_monthly(months_back: int = 13, use_cache: bool = True) -> list:
    """Fetch dead animal collection records month-by-month with optional caching.

    The Open311 API returns records oldest-first, so a single 365-day request
    only returns the oldest ~90 days before hitting the pagination cap.
    Fetching month by month ensures every period is fully covered.

    Args:
        months_back: Number of months to fetch (default 13 for a full trailing year)
        use_cache: Whether to use SQLite caching (default True)

    Returns:
        A flat list of dead animal collection records across all months.
    """
    from open311_cache import init_cache, get_cached_records, cache_records, get_last_fetch_date, attach_service_labels

    CATEGORY = "dead_animal"

    if use_cache:
        init_cache()
        cached_records = get_cached_records(service_codes=list(SERVICE_CODES.keys()))
        cached_ids = {r.get("service_request_id") for r in cached_records}
        logger.info(f"Loaded {len(cached_records)} cached dead-animal records")

        last_fetch = get_last_fetch_date(service_codes=list(SERVICE_CODES.keys()))
        if last_fetch:
            logger.info(f"Last fetch was at {last_fetch}")
            cache_age = _utc_now() - last_fetch
            if cache_age < timedelta(days=6) and len(cached_records) > 0:
                logger.info(f"Cache is fresh ({cache_age.days} days old), returning cached data")
                return attach_service_labels(cached_records, SERVICE_CODES)
    else:
        cached_records = []
        cached_ids = set()

    now = _utc_now()
    all_records: list = []
    seen_ids: set = cached_ids.copy()
    new_records: list = []

    if use_cache and cached_records:
        last_fetch = get_last_fetch_date(service_codes=list(SERVICE_CODES.keys()))
        if last_fetch:
            fetch_start = last_fetch - timedelta(days=1)
        else:
            fetch_start = now - timedelta(days=30 * months_back)
    else:
        fetch_start = now - timedelta(days=30 * months_back)

    logger.info(f"Fetching dead-animal records from {fetch_start} to {now}")

    current_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    start_month = fetch_start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    months_to_fetch = []
    while start_month <= current_month:
        months_to_fetch.append(start_month)
        if start_month.month == 12:
            start_month = start_month.replace(year=start_month.year + 1, month=1)
        else:
            start_month = start_month.replace(month=start_month.month + 1)

    logger.info(f"Will fetch {len(months_to_fetch)} months of dead-animal data")

    for month_start in reversed(months_to_fetch):
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
                while page <= MAX_PAGES:
                    if page > 1:
                        time.sleep(PAGE_DELAY)
                    params = {
                        "service_code": code,
                        "start_date": _isoformat_z(month_start),
                        "end_date": _isoformat_z(month_end),
                        "per_page": 100,
                        "page": page,
                    }
                    batch = open311_get(_get_session(), f"{OPEN311_BASE_URL}/requests.json", params)
                    if not batch:
                        break
                    for r in batch:
                        sid = r.get("service_request_id")
                        if sid and sid not in seen_ids:
                            seen_ids.add(sid)
                            r["_service_label"] = SERVICE_CODES.get(code, code)
                            all_records.append(r)
                            new_records.append(r)
                    if len(batch) < 100:
                        break
                    page += 1
            except Exception as e:
                logger.warning(f"Monthly dead-animal fetch failed {code} {month_start.strftime('%Y-%m')}: {e}")
        time.sleep(2.0 if API_KEY else 4.0)

    if use_cache and new_records:
        cache_records(CATEGORY, new_records)
        logger.info(f"Cached {len(new_records)} new dead-animal records")

    if use_cache and cached_records:
        combined = {r.get("service_request_id"): r for r in cached_records}
        for r in all_records:
            combined[r.get("service_request_id")] = r
        result = list(combined.values())
        logger.info(f"Returning {len(result)} total dead-animal records")
        return result

    return all_records


def fetch_dead_animal_reports(days_back: int = 90, use_cache: bool = True) -> list:
    from open311_cache import init_cache, get_cached_records, cache_records, get_last_fetch_date, attach_service_labels

    CATEGORY = "dead_animal"

    if use_cache:
        init_cache()
        cached = get_cached_records(service_codes=list(SERVICE_CODES.keys()))
        last_fetch = get_last_fetch_date(service_codes=list(SERVICE_CODES.keys()))
        if last_fetch and ((_utc_now() - last_fetch) < timedelta(days=6)) and cached:
            logger.info(f"Using {len(cached)} cached dead-animal records")
            return attach_service_labels(cached, SERVICE_CODES)
        cached_ids = {r.get("service_request_id") for r in cached}
    else:
        cached = []
        cached_ids = set()

    all_records = []
    new_records = []
    seen_ids = cached_ids.copy()

    for code in SERVICE_CODES:
        try:
            records = _fetch_code(code, days_back)
            unique = [r for r in records if r.get("service_request_id") not in seen_ids]
            for r in unique:
                seen_ids.add(r.get("service_request_id"))
                new_records.append(r)
            all_records.extend(unique)
        except Exception as e:
            logger.warning(f"Failed to fetch {code}: {e}")

    if use_cache and cached:
        all_records = cached + [r for r in all_records if r.get("service_request_id") not in cached_ids]

    if use_cache and new_records:
        cache_records(CATEGORY, new_records)

    return all_records


# =============================================================================
# RESOLUTION TIME STATS
# =============================================================================

def _resolution_stats(records: list) -> Optional[float]:
    """Return overall avg resolution days from closed records, or None."""
    times = []
    for r in records:
        if (r.get("status") or "").lower() != "closed":
            continue
        req_str = r.get("requested_datetime") or ""
        upd_str = r.get("updated_datetime") or ""
        if not req_str or not upd_str:
            continue
        try:
            req = datetime.fromisoformat(req_str.replace("Z", "+00:00"))
            upd = datetime.fromisoformat(upd_str.replace("Z", "+00:00"))
            days = (upd - req).total_seconds() / 86400
            if 0 <= days <= 90:
                times.append(days)
        except ValueError:
            pass
    return round(sum(times) / len(times), 1) if times else None


# =============================================================================
# MAP GENERATOR
# =============================================================================

_OPEN_COLOR   = ("orange", "remove")
_CLOSED_COLOR = ("green",  "ok-sign")


def generate_dead_animal_map(days_back: int = 90) -> tuple:
    """Generate interactive HTML map of dead animal collection reports.

    Returns (BytesIO, summary_str).
    """
    try:
        import folium
        from folium.plugins import MarkerCluster
    except ImportError:
        return None, "❌ Map generation requires 'folium'."

    records_raw = fetch_dead_animal_reports(days_back)

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
        return None, "🐾 No dead animal collection reports with location data found."

    open_count   = sum(1 for r in records if (r.get("status") or "").lower() == "open")
    closed_count = len(records) - open_count
    overall_avg  = _resolution_stats(records_raw)

    def _age_days(r):
        try:
            dt = datetime.fromisoformat(r.get("requested_datetime", "").replace("Z", "+00:00"))
            return (now_dt - dt).days
        except Exception:
            return days_back

    # Pre-compute counts per status × bucket for the JS summary bar
    bucket_counts: dict = {
        "all": {
            "30": {"open": 0, "closed": 0},
            "60": {"open": 0, "closed": 0},
            "90": {"open": 0, "closed": 0},
        }
    }
    for r in records:
        age = _age_days(r)
        s = (r.get("status") or "closed").lower()
        s = s if s in ("open", "closed") else "closed"
        for bd in (30, 60, 90):
            if age <= bd:
                bucket_counts["all"][str(bd)][s] += 1
    counts_js = str(bucket_counts).replace("'", '"')

    m = folium.Map(
        location=[30.2672, -97.7431],
        zoom_start=11,
        tiles="https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png?key=cb1_2du8_1_a49e4774820276874a1a5b33",
        attr='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',
    )
    m.get_root().header.add_child(folium.Element(og_meta_tags("animal")))

    # Layer key: {status}_{bucket}
    fg_clusters: dict = {}
    fg_objects:  dict = {}
    for status_key in ("open", "closed"):
        for bucket in ("30", "60", "90"):
            key = f"{status_key}_{bucket}"
            fg = folium.FeatureGroup(name=key, show=(bucket == "90"), overlay=True)
            cluster = MarkerCluster().add_to(fg)
            fg.add_to(m)
            fg_clusters[key] = cluster
            fg_objects[key]  = fg

    for r in records:
        lat  = r["_lat"]
        lon  = r["_lon"]
        status = (r.get("status") or "").lower()
        description  = (r.get("description")  or "").strip()
        status_notes = (r.get("status_notes") or "").strip()
        date_str     = (r.get("requested_datetime") or "").split("T")[0]
        updated_str  = (r.get("updated_datetime")   or "").split("T")[0]
        address      = (r.get("address") or "").strip()
        req_id       = r.get("service_request_id", "N/A")

        age    = _age_days(r)
        bucket = "30" if age <= 30 else ("60" if age <= 60 else "90")
        ckey   = f"{status}_{bucket}" if f"{status}_{bucket}" in fg_clusters else f"closed_{bucket}"

        address_line = (
            f'<b>Address:</b> <a href="https://www.google.com/maps/search/?api=1&query={lat},{lon}"'
            f' target="_blank">{address}</a><br/>'
        ) if address else ""
        updated_line = (
            f"<span style='color:#666;'>Updated: {updated_str}</span><br/>"
        ) if updated_str and updated_str != date_str else ""

        desc_block = ""
        if description:
            desc_short = (description[:400] + "...") if len(description) > 400 else description
            desc_block = f"<b>Description:</b><br/><i>{desc_short.replace(chr(10), '<br/>')}</i><br/>"
        elif status_notes:
            notes_short = (status_notes[:300] + "...") if len(status_notes) > 300 else status_notes
            note_label  = "Resolution" if status == "closed" else "Notes"
            desc_block  = f"<b>{note_label}:</b><br/><i>{notes_short}</i><br/>"

        ticket_url = f"https://311.austintexas.gov/tickets/{req_id}"
        popup_html = f"""
        <div style="font-family:sans-serif;max-width:300px;font-size:13px;">
            <b><a href="{ticket_url}" target="_blank" style="color:#0066cc;">Report #{req_id}</a></b><br/>
            <span style="color:#666;">Filed: {date_str}</span><br/>
            {updated_line}
            {address_line}
            <br/>
            <b>Status:</b> {'🔴 Open' if status == 'open' else '🟢 Closed'}<br/><br/>
            {desc_block}
        </div>
        """
        popup = folium.Popup(popup_html, max_width=300)
        color, icon_name = _OPEN_COLOR if status == "open" else _CLOSED_COLOR

        folium.Marker(
            location=[lat, lon],
            popup=popup,
            icon=folium.Icon(color=color, icon=icon_name, prefix="glyphicon"),
            tooltip=f"{'Open' if status == 'open' else 'Closed'}: Dead Animal Collection",
        ).add_to(fg_clusters[ckey])

    avg_line = f"Avg resolution: {overall_avg}d" if overall_avg is not None else ""

    map_var      = m.get_name()
    layer_map_js = "{" + ", ".join(f'"{k}": {fg_objects[k].get_name()}' for k in fg_objects) + "}"
    panel_html   = f"""
    <div id="map-panel" style="position:absolute;top:10px;left:50%;transform:translateX(-50%);
                background:white;padding:10px 16px;border-radius:6px;
                box-shadow:0 2px 6px rgba(0,0,0,0.3);z-index:9999;
                font-family:sans-serif;text-align:center;">
        <b style="font-size:15px;">🐿️ Dead Animal Collection — Austin 311</b><br/>
        <span id="map-summary" style="font-size:12px;color:#555;"></span>
        <div style="display:flex;justify-content:center;gap:4px;margin-top:7px;">
            <button id="btn-30" onclick="setDayFilter(30)" class="fbtn">30d</button>
            <button id="btn-60" onclick="setDayFilter(60)" class="fbtn">60d</button>
            <button id="btn-90" onclick="setDayFilter(90)" class="fbtn active">90d</button>
            <span style="margin:0 4px;color:#ccc;">|</span>
            <button id="btn-open" onclick="toggleStatus('open')" class="fbtn active">🔴 Open</button>
            <button id="btn-closed" onclick="toggleStatus('closed')" class="fbtn active">🟢 Closed</button>
        </div>
        {f'<div style="font-size:11px;color:#888;margin-top:4px;">{avg_line}</div>' if avg_line else ''}
    </div>
    <div id="back-panel" style="position:absolute;top:10px;right:10px;
                background:white;padding:8px 12px;border-radius:6px;
                box-shadow:0 2px 6px rgba(0,0,0,0.3);z-index:9999;
                font-family:sans-serif;font-size:12px;">
        <a href="../" style="color:#0066cc;text-decoration:none;">← Animal Services</a>
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
            var counts = (bucketCounts['all'] || {{}})[d] || {{}};
            var o = showOpen  ? (counts.open   || 0) : 0;
            var c = showClosed ? (counts.closed || 0) : 0;
            document.getElementById('map-summary').textContent =
                'Last ' + d + ' days · ' + (o + c) + ' total · ' + o + ' open · ' + c + ' closed';
        }}

        function initLayers() {{
            layerMap  = {layer_map_js};
            leafletMap = {map_var};
            updateLayers();
            updateSummary();
        }}

        function updateLayers() {{
            if (!layerMap || !leafletMap) return;
            Object.keys(layerMap).forEach(function(key) {{
                var parts  = key.split('_');
                var status = parts[0];
                var bucket = parseInt(parts[1]);
                var timeOk   = bucket <= currentDays;
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
        buf = io.BytesIO(html_content)
        buf.seek(0)
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass

    avg_str = f"  ·  avg resolution {overall_avg}d" if overall_avg is not None else ""
    summary = (
        f"🐿️ Dead Animal Collection Map — last {days_back} days\n"
        f"{len(records):,} reports mapped · {open_count:,} open · {closed_count:,} closed{avg_str}"
    )
    return buf, summary
