"""
Austin Traffic Cameras — data layer and live map generator.

Socrata dataset `b4k4-adkb`. Each camera has a point location and, when
`camera_status == TURNED_ON`, a `screenshot_address` that serves a *live still
image* (not a video stream — video is not recorded or retained) from
cctv.austinmobility.io.

The map plots every camera, using a distinct icon per status so a user can tell
at a glance which locations are actually live. Clicking a live camera opens a
popup with its current snapshot, which auto-refreshes every few seconds for as
long as the popup is open (so only one image is ever being refreshed at a time).
"""

import io
import os
import time
import tempfile
import logging
import requests
from typing import Optional

from open311_client import og_meta_tags

logger = logging.getLogger(__name__)

SOCRATA_URL = "https://data.austintexas.gov/resource/b4k4-adkb.json"
TIMEOUT = 45
REFRESH_SECONDS = 30  # how often an open camera popup re-fetches its image

# status key -> (label, folium color, font-awesome icon, is_live)
STATUS_META = {
    "live":    ("Live",    "green", "video-camera", True),
    "planned": ("Planned", "blue",  "wrench",       False),
    "removed": ("Removed", "red",   "times-circle", False),
    "void":    ("Void",    "gray",  "ban",          False),
}

# raw camera_status value -> our status key
_RAW_STATUS_MAP = {
    "TURNED_ON": "live",
    "DESIRED": "planned",
    "REMOVED": "removed",
    "VOID": "void",
}

_session: Optional[requests.Session] = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({
            "Accept": "application/json",
            "User-Agent": "austin311bot/0.1 (Socrata traffic-camera queries)",
        })
        token = os.getenv("AUSTINAPIKEY")
        if token:
            _session.headers["X-App-Token"] = token
    return _session


def fetch_cameras() -> list:
    """Fetch all cameras with a usable location from Socrata."""
    session = _get_session()
    params = {
        "$select": "camera_id,location_name,camera_status,screenshot_address,"
                   "location,council_district,landmark",
        "$limit": 5000,
    }
    try:
        resp = session.get(SOCRATA_URL, params=params, timeout=TIMEOUT)
        if resp.status_code == 429:
            time.sleep(int(resp.headers.get("Retry-After", 15)))
            resp = session.get(SOCRATA_URL, params=params, timeout=TIMEOUT)
        resp.raise_for_status()
        rows = resp.json()
    except Exception as e:
        logger.error(f"Traffic cameras fetch failed: {e}")
        return []

    cameras = []
    for row in rows:
        loc = row.get("location") or {}
        coords = loc.get("coordinates") if isinstance(loc, dict) else None
        if not coords or len(coords) != 2:
            continue
        try:
            lon, lat = float(coords[0]), float(coords[1])
        except (TypeError, ValueError):
            continue
        if not (30.0 <= lat <= 30.6 and -98.1 <= lon <= -97.4):
            continue
        raw_status = (row.get("camera_status") or "").strip().upper()
        status = _RAW_STATUS_MAP.get(raw_status, "void")
        cameras.append({
            "id": row.get("camera_id", ""),
            "name": (row.get("location_name") or "").strip(),
            "status": status,
            "image": (row.get("screenshot_address") or "").strip(),
            "district": (row.get("council_district") or "").strip(),
            "landmark": (row.get("landmark") or "").strip(),
            "lat": lat,
            "lon": lon,
        })
    return cameras


def get_camera_stats() -> dict:
    cameras = fetch_cameras()
    counts = {k: 0 for k in STATUS_META}
    for c in cameras:
        counts[c["status"]] = counts.get(c["status"], 0) + 1
    return {"total": len(cameras), "counts": counts}


def generate_cameras_map(days_back: int = 90) -> tuple[Optional[io.BytesIO], str]:
    """Build the live traffic-camera map. `days_back` is accepted for pipeline
    compatibility but unused — cameras are static infrastructure."""
    try:
        import folium
        from folium.plugins import MarkerCluster
    except ImportError:
        return None, "❌ Map generation requires 'folium'. Install: pip install folium"

    cameras = fetch_cameras()
    if not cameras:
        return None, "📷 No traffic cameras with location data found."

    counts = {k: 0 for k in STATUS_META}
    for c in cameras:
        counts[c["status"]] += 1

    m = folium.Map(location=[30.2672, -97.7431], zoom_start=11, tiles="CartoDB positron")
    m.get_root().header.add_child(folium.Element(og_meta_tags("cameras")))

    # one FeatureGroup + cluster per status so toggles can add/remove cleanly
    fg_objects = {}
    clusters = {}
    for status_key in STATUS_META:
        fg = folium.FeatureGroup(name=status_key, show=(status_key == "live"), overlay=True)
        clusters[status_key] = MarkerCluster().add_to(fg)
        fg.add_to(m)
        fg_objects[status_key] = fg

    for c in cameras:
        label, color, icon_name, is_live = STATUS_META[c["status"]]
        name = c["name"] or f"Camera {c['id']}"
        district = f"<br/><span style='color:#666;'>Council District: {c['district']}</span>" if c["district"] else ""
        landmark = f"<br/><span style='color:#666;'>{c['landmark']}</span>" if c["landmark"] else ""

        if is_live and c["image"]:
            # data-src is refreshed (with a cache-buster) by the popupopen handler
            img_block = (
                f'<img class="cam-live" data-src="{c["image"]}" alt="Live view" '
                f'style="width:320px;max-width:100%;border-radius:4px;margin-top:6px;display:block;background:#eee;" '
                f'onerror="this.style.display=\'none\';this.nextElementSibling.style.display=\'block\';"/>'
                f'<div style="display:none;color:#a00;font-size:12px;margin-top:6px;">Image unavailable</div>'
                f'<div style="font-size:11px;color:#888;margin-top:4px;">🔴 Live · refreshes every {REFRESH_SECONDS}s · video is not recorded</div>'
            )
        elif is_live:
            img_block = '<div style="color:#a00;font-size:12px;margin-top:6px;">No image URL published for this camera.</div>'
        else:
            img_block = f'<div style="color:#888;font-size:12px;margin-top:6px;">⚠️ {label} — no live feed at this location.</div>'

        popup_html = f"""
        <div style="font-family:sans-serif;max-width:340px;">
            <b style="font-size:14px;">{name}</b>{district}{landmark}
            <br/><span style="font-size:12px;color:#444;">Status: <b>{label}</b> · ID {c['id']}</span>
            {img_block}
        </div>
        """
        popup = folium.Popup(popup_html, max_width=360)
        icon = folium.Icon(color=color, icon=icon_name, prefix="fa")
        tooltip = f"{label}: {name}"
        folium.Marker(location=[c["lat"], c["lon"]], popup=popup, icon=icon,
                      tooltip=tooltip).add_to(clusters[c["status"]])

    map_var = m.get_name()
    layer_map_js = "{" + ", ".join(f'"{k}": {fg_objects[k].get_name()}' for k in fg_objects) + "}"
    counts_js = str(counts).replace("'", '"')

    status_buttons = ""
    for status_key, (label, color, _icon, _live) in STATUS_META.items():
        active_cls = " active" if status_key == "live" else ""
        status_buttons += (
            f'<button id="btn-{status_key}" onclick="toggleStatus(\'{status_key}\')" '
            f'class="fbtn{active_cls}">{label} (<span id="cnt-{status_key}"></span>)</button>\n            '
        )

    active_js = "{" + ", ".join(f'"{k}": {"true" if k == "live" else "false"}' for k in STATUS_META) + "}"

    panel_html = f"""
    <div id="map-panel" style="position:absolute;top:10px;left:50%;transform:translateX(-50%);
                background:white;padding:10px 16px;border-radius:6px;
                box-shadow:0 2px 6px rgba(0,0,0,0.3);z-index:9999;
                font-family:sans-serif;text-align:center;min-width:360px;max-width:92vw;">
        <b style="font-size:15px;">📷 Austin Traffic Cameras</b><br/>
        <span id="map-summary" style="font-size:12px;color:#555;"></span>
        <div style="display:flex;justify-content:center;flex-wrap:wrap;gap:4px;margin-top:7px;">
            {status_buttons}
        </div>
        <div style="font-size:10px;color:#999;margin-top:5px;">Click a live camera for its current snapshot (auto-refreshes every {REFRESH_SECONDS}s).</div>
    </div>
    <style>
        .fbtn {{ padding:3px 9px;border:1px solid #ccc;border-radius:4px;background:#f5f5f5;cursor:pointer;font-size:12px;color:#444; }}
        .fbtn.active {{ background:#2563eb;color:white;border-color:#2563eb; }}
        .fbtn:hover:not(.active) {{ background:#e0e7ff; }}
    </style>
    <script>
        var activeStatus = {active_js};
        var statusCounts = {counts_js};
        var layerMap = null;
        var leafletMap = null;

        function updateSummary() {{
            var total = 0, live = statusCounts.live || 0;
            Object.keys(activeStatus).forEach(function(k) {{
                if (activeStatus[k]) total += (statusCounts[k] || 0);
            }});
            document.getElementById('map-summary').textContent =
                total + ' shown · ' + live + ' live cameras';
            Object.keys(statusCounts).forEach(function(k) {{
                var el = document.getElementById('cnt-' + k);
                if (el) el.textContent = statusCounts[k] || 0;
            }});
        }}

        function updateLayers() {{
            if (!layerMap || !leafletMap) return;
            Object.keys(layerMap).forEach(function(key) {{
                var layer = layerMap[key];
                if (activeStatus[key]) {{
                    if (!leafletMap.hasLayer(layer)) leafletMap.addLayer(layer);
                }} else {{
                    if (leafletMap.hasLayer(layer)) leafletMap.removeLayer(layer);
                }}
            }});
        }}

        function toggleStatus(key) {{
            activeStatus[key] = !activeStatus[key];
            document.getElementById('btn-' + key).classList.toggle('active');
            updateLayers();
            updateSummary();
        }}

        function initCameras() {{
            layerMap = {layer_map_js};
            leafletMap = {map_var};
            updateLayers();
            updateSummary();

            // Live-refresh only the image inside the currently-open popup.
            leafletMap.on('popupopen', function(e) {{
                var el = e.popup.getElement();
                if (!el) return;
                var img = el.querySelector('img.cam-live');
                if (!img) return;
                var base = img.getAttribute('data-src');
                function refresh() {{ img.src = base + '?t=' + Date.now(); }}
                refresh();
                e.popup._camTimer = setInterval(refresh, {REFRESH_SECONDS} * 1000);
            }});
            leafletMap.on('popupclose', function(e) {{
                if (e.popup && e.popup._camTimer) {{
                    clearInterval(e.popup._camTimer);
                    e.popup._camTimer = null;
                }}
            }});
        }}

        document.addEventListener('DOMContentLoaded', function() {{
            setTimeout(initCameras, 1000);
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
        f"📷 Traffic cameras: {len(cameras):,} total | "
        f"{counts['live']} live · {counts['planned']} planned · "
        f"{counts['removed']} removed · {counts['void']} void"
    )
    return buffer, summary
