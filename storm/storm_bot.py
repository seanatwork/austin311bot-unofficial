"""
Storm Debris, Drainage & Flooding — data layer and map generator.

Queries Austin Open311 API across 8 watershed/drainage service codes.
"""

import io
import os
import time
import tempfile
import logging
import requests
from datetime import datetime, timezone, timedelta
from typing import Optional

from open311_client import og_meta_tags

logger = logging.getLogger(__name__)

OPEN311_BASE_URL = "https://311.austintexas.gov/open311/v2"
TIMEOUT = 45
MAX_RETRIES = 8
RETRY_DELAY = 1.0
MAX_PAGES = 10

API_KEY = os.getenv("AUSTINAPIKEY")

SERVICE_CODES = {
    "SWSSTORM": "Storm Debris Collection",
    "DRCHANEL": "Channels/Creeks/Drainage",
    "DRILID":   "Storm Drain Services",
    "DRFLOODG": "Flooding — Current",
    "DRSSPIPE": "Standing Water",
    "DRFLOODR": "Flooding — Past",
    "ZZEROSIO": "Erosion",
    "DRDITCH":  "Ditch/Driveway Pipe",
}

CATEGORY_GROUPS = {
    "debris": {
        "label": "Storm Debris",
        "codes": {"SWSSTORM"},
    },
    "drainage": {
        "label": "Drainage & Pipes",
        "codes": {"DRCHANEL", "DRILID", "DRDITCH"},
    },
    "flooding": {
        "label": "Flooding & Water",
        "codes": {"DRFLOODG", "DRFLOODR", "DRSSPIPE"},
    },
    "erosion": {
        "label": "Erosion",
        "codes": {"ZZEROSIO"},
    },
}

_CODE_TO_CATEGORY = {
    code: cat_key
    for cat_key, cat in CATEGORY_GROUPS.items()
    for code in cat["codes"]
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
            "User-Agent": "austin311bot/0.1 (Open311 storm/drainage queries)",
        }
        if API_KEY:
            headers["X-Api-Key"] = API_KEY
        _session.headers.update(headers)
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
        if resp.status_code == 429 and retries < MAX_RETRIES:
            retry_after = int(resp.headers.get("Retry-After", 0))
            delay = min(max(retry_after, 15 * (2 ** retries)), 60)
            logger.warning(f"Rate limited (429), retrying in {delay:.0f}s ({retries+1}/{MAX_RETRIES})")
            time.sleep(delay)
            return _make_request(params, retries + 1)
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


def _fetch_code(service_code: str, days_back: int) -> list:
    end = _utc_now()
    start = end - timedelta(days=days_back)
    all_records = []
    seen_ids: set = set()
    for page in range(1, MAX_PAGES + 1):
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
        time.sleep(1.0 if API_KEY else 2.0)
    return all_records


def fetch_storm_monthly(months_back: int = 13, use_cache: bool = True) -> list:
    """Fetch storm/drainage records month-by-month with optional caching.

    The Open311 API returns records oldest-first, so a single 365-day request
    only returns the oldest ~90 days before hitting the pagination cap.
    Fetching month by month ensures every period is fully covered.

    Args:
        months_back: Number of months to fetch (default 13 for a full trailing year)
        use_cache: Whether to use SQLite caching (default True)

    Returns:
        A flat list of storm/drainage records across all months and all codes.
    """
    from open311_cache import init_cache, get_cached_records, cache_records, get_last_fetch_date

    CATEGORY = "storm"

    if use_cache:
        init_cache()
        cached_records = get_cached_records(service_codes=list(SERVICE_CODES.keys()))
        cached_ids = {r.get("service_request_id") for r in cached_records}
        logger.info(f"Loaded {len(cached_records)} cached storm records")

        last_fetch = get_last_fetch_date(service_codes=list(SERVICE_CODES.keys()))
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

    if use_cache and cached_records:
        last_fetch = get_last_fetch_date(service_codes=list(SERVICE_CODES.keys()))
        if last_fetch:
            fetch_start = last_fetch - timedelta(days=1)
        else:
            fetch_start = now - timedelta(days=30 * months_back)
    else:
        fetch_start = now - timedelta(days=30 * months_back)

    logger.info(f"Fetching storm records from {fetch_start} to {now}")

    current_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    start_month = fetch_start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    months_to_fetch = []
    while start_month <= current_month:
        months_to_fetch.append(start_month)
        if start_month.month == 12:
            start_month = start_month.replace(year=start_month.year + 1, month=1)
        else:
            start_month = start_month.replace(month=start_month.month + 1)

    logger.info(f"Will fetch {len(months_to_fetch)} months of storm data")

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
                    params = {
                        "service_code": code,
                        "start_date": _isoformat_z(month_start),
                        "end_date": _isoformat_z(month_end),
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
                            r["_service_label"] = SERVICE_CODES.get(code, code)
                            r["_service_code"] = code
                            all_records.append(r)
                            new_records.append(r)
                    if len(records) < 100:
                        break
                    page += 1
                    time.sleep(1.0 if API_KEY else 2.0)
            except Exception as e:
                logger.warning(f"Monthly fetch failed {code} {month_start.strftime('%Y-%m')}: {e}")
        time.sleep(2.0 if API_KEY else 4.0)

    if use_cache and new_records:
        cache_records(CATEGORY, new_records)
        logger.info(f"Cached {len(new_records)} new storm records")

    if use_cache and cached_records:
        combined = {r.get("service_request_id"): r for r in cached_records}
        for r in all_records:
            combined[r.get("service_request_id")] = r
        result = list(combined.values())
        logger.info(f"Returning {len(result)} total storm records")
        return result

    return all_records


def fetch_all_storm_reports(days_back: int = 90, use_cache: bool = True) -> list:
    """Fetch reports across all storm/drainage service codes with optional caching."""
    from open311_cache import init_cache, get_cached_records, cache_records, get_last_fetch_date

    CATEGORY = "storm"

    if use_cache:
        init_cache()
        cached_records = get_cached_records(service_codes=list(SERVICE_CODES.keys()))
        cached_ids = {r.get("service_request_id") for r in cached_records}
        logger.info(f"Loaded {len(cached_records)} cached storm records")

        last_fetch = get_last_fetch_date(service_codes=list(SERVICE_CODES.keys()))
        if last_fetch:
            cache_age = _utc_now() - last_fetch
            if cache_age < timedelta(days=6) and len(cached_records) > 0:
                logger.info(f"Cache is fresh ({cache_age.days} days old), using cached data")
                return cached_records
    else:
        cached_records = []
        cached_ids = set()

    all_records = []
    seen_ids = cached_ids.copy()
    new_records = []

    for code in SERVICE_CODES:
        try:
            records = _fetch_code(code, days_back)
            unique_records = [r for r in records if r.get("service_request_id") not in seen_ids]
            for r in unique_records:
                seen_ids.add(r.get("service_request_id"))
                new_records.append(r)
            all_records.extend(unique_records)
            logger.debug(f"{code}: {len(unique_records)} new records")
        except Exception as e:
            logger.warning(f"Failed to fetch {code}: {e}")

    if use_cache and cached_records:
        all_records = cached_records + [r for r in all_records if r.get("service_request_id") not in cached_ids]

    if use_cache and new_records:
        cache_records(CATEGORY, new_records)
        logger.info(f"Cached {len(new_records)} new storm records")

    return all_records


def _get_category(service_code: str) -> str:
    return _CODE_TO_CATEGORY.get(service_code, "debris")


# =============================================================================
# MAP GENERATOR
# =============================================================================

def generate_storm_map(days_back: int = 90) -> tuple[Optional[io.BytesIO], str]:
    try:
        import folium
        from folium.plugins import MarkerCluster
    except ImportError:
        return None, "❌ Map generation requires 'folium'. Install: pip install folium"

    records_raw = fetch_all_storm_reports(days_back)

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
        return None, "🌧️ No storm/drainage reports with location data found."

    open_count = sum(1 for r in records if (r.get("status") or "").lower() == "open")
    closed_count = len(records) - open_count

    def _age_days(r):
        try:
            dt = datetime.fromisoformat(r.get("requested_datetime", "").replace("Z", "+00:00"))
            return (now_dt - dt).days
        except Exception:
            return days_back

    cat_keys = list(CATEGORY_GROUPS.keys())
    cat_bucket_counts = {
        cat: {"30": {"open": 0, "closed": 0}, "60": {"open": 0, "closed": 0}, "90": {"open": 0, "closed": 0}}
        for cat in cat_keys + ["all"]
    }
    for r in records:
        age = _age_days(r)
        status = (r.get("status") or "").lower()
        s = status if status in ("open", "closed") else "closed"
        cat = _get_category(r.get("_service_code", ""))
        for bucket_days in (30, 60, 90):
            if age <= bucket_days:
                b = str(bucket_days)
                cat_bucket_counts["all"][b][s] += 1
                cat_bucket_counts[cat][b][s] += 1
    counts_js = str(cat_bucket_counts).replace("'", '"')

    m = folium.Map(location=[30.2672, -97.7431], zoom_start=11, tiles="CartoDB positron")
    m.get_root().header.add_child(folium.Element(og_meta_tags("storm")))

    # 24 FeatureGroups: open/closed × 30/60/90 × 4 categories
    fg_clusters = {}
    fg_objects = {}
    for status_key in ("open", "closed"):
        for bucket in ("30", "60", "90"):
            for cat_key in cat_keys:
                name = f"{status_key}_{bucket}_{cat_key}"
                show = (bucket == "90")
                fg = folium.FeatureGroup(name=name, show=show, overlay=True)
                cluster = MarkerCluster().add_to(fg)
                fg.add_to(m)
                fg_clusters[name] = cluster
                fg_objects[name] = fg

    _CAT_COLORS = {
        "debris":   ("orange",  "trash"),
        "drainage": ("blue",    "tint"),
        "flooding": ("darkblue","warning-sign"),
        "erosion":  ("beige",   "leaf"),
    }

    for r in records:
        lat = r["_lat"]
        lon = r["_lon"]
        status = (r.get("status") or "").lower()
        service_label = r.get("_service_label", "Storm Report")
        cat = _get_category(r.get("_service_code", ""))
        description = (r.get("description") or "").strip()
        status_notes = (r.get("status_notes") or "").strip()
        date_str = (r.get("requested_datetime") or "").split("T")[0]
        updated_str = (r.get("updated_datetime") or "").split("T")[0]
        address = (r.get("address") or "").strip()
        req_id = r.get("service_request_id", "N/A")

        age = _age_days(r)
        bucket = "30" if age <= 30 else ("60" if age <= 60 else "90")
        cluster_key = f"{status}_{bucket}_{cat}"
        if cluster_key not in fg_clusters:
            cluster_key = f"closed_{bucket}_{cat}"

        address_line = f'<b>Address:</b> <a href="https://www.google.com/maps/search/?api=1&query={lat},{lon}" target="_blank">{address}</a><br/>' if address else ""
        updated_line = f"<span style='color:#666;'>Updated: {updated_str}</span><br/>" if updated_str and updated_str != date_str else ""
        desc_text = description or status_notes
        desc_short = (desc_text[:500] + "...") if len(desc_text) > 500 else desc_text
        desc_block = f"<b>Description:</b><br/><i>{desc_short.replace(chr(10), '<br/>')}</i><br/>" if desc_short else ""
        ticket_url = f"https://311.austintexas.gov/tickets/{req_id}"
        popup_html = f"""
        <div style="font-family:sans-serif;max-width:310px;">
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
        popup = folium.Popup(popup_html, max_width=310)

        color, icon_name = _CAT_COLORS.get(cat, ("gray", "info-sign"))
        if status == "open":
            icon = folium.Icon(color="red" if cat == "flooding" else color, icon="exclamation-sign", prefix="glyphicon")
            tooltip = f"Open: {service_label}"
        else:
            icon = folium.Icon(color="green", icon="ok-sign", prefix="glyphicon")
            tooltip = f"Closed: {service_label}"

        folium.Marker(location=[lat, lon], popup=popup, icon=icon, tooltip=tooltip).add_to(fg_clusters[cluster_key])

    map_var = m.get_name()
    layer_map_js = "{" + ", ".join(f'"{k}": {fg_objects[k].get_name()}' for k in fg_objects) + "}"

    cat_buttons_html = ""
    for cat_key, cat_info in CATEGORY_GROUPS.items():
        cat_buttons_html += f'<button id="btn-cat-{cat_key}" onclick="toggleCat(\'{cat_key}\')" class="fbtn active">{cat_info["label"]}</button>\n            '

    panel_html = f"""
    <div id="map-panel" style="position:absolute;top:10px;left:50%;transform:translateX(-50%);
                background:white;padding:10px 16px;border-radius:6px;
                box-shadow:0 2px 6px rgba(0,0,0,0.3);z-index:9999;
                font-family:sans-serif;text-align:center;min-width:360px;">
        <b style="font-size:15px;">🌧️ Austin Storm, Drainage &amp; Flooding</b><br/>
        <span id="map-summary" style="font-size:12px;color:#555;"></span>
        <div style="display:flex;justify-content:center;flex-wrap:wrap;gap:4px;margin-top:7px;">
            <button id="btn-30" onclick="setDayFilter(30)" class="fbtn">30d</button>
            <button id="btn-60" onclick="setDayFilter(60)" class="fbtn">60d</button>
            <button id="btn-90" onclick="setDayFilter(90)" class="fbtn active">90d</button>
            <span style="margin:0 4px;color:#ccc;">|</span>
            <button id="btn-open" onclick="toggleStatus('open')" class="fbtn active">🔴 Open</button>
            <button id="btn-closed" onclick="toggleStatus('closed')" class="fbtn active">🟢 Closed</button>
        </div>
        <div style="display:flex;justify-content:center;flex-wrap:wrap;gap:4px;margin-top:5px;">
            {cat_buttons_html}
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
        var activeCats = {{"debris": true, "drainage": true, "flooding": true, "erosion": true}};
        var layerMap = null;
        var leafletMap = null;
        var bucketCounts = {counts_js};

        function updateSummary() {{
            var d = String(currentDays);
            var counts = bucketCounts["all"][d] || {{}};
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
                var cat = parts[2];
                var timeOk = bucket <= currentDays;
                var statusOk = (status === 'open' && showOpen) || (status === 'closed' && showClosed);
                var catOk = activeCats[cat] !== false;
                var layer = layerMap[key];
                if (timeOk && statusOk && catOk) {{
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

        function toggleCat(cat) {{
            activeCats[cat] = !activeCats[cat];
            document.getElementById('btn-cat-' + cat).classList.toggle('active');
            updateLayers();
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
        f"🌧️ *Storm, Drainage & Flooding Map*\n"
        f"_Last {days_back} days_\n\n"
        f"📊 *{len(records):,} reports mapped*\n"
        f"🔴 *{open_count:,} open*  ·  🟢 *{closed_count:,} closed*\n\n"
        f"Tap markers to see details. Filter by time window and category."
    )
    return buffer, summary
