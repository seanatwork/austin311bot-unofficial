"""
Crime choropleth map — APD incident counts by Austin council district.

Fetches from Socrata APD Crime Reports (fdj4-gpfu) and renders a
Folium choropleth colored by incident count per district, with
30/60/90-day time filter buttons.
"""

import io
import os
import json
import time
import logging
import tempfile
import requests
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

SOCRATA_BASE = "https://data.austintexas.gov/resource"
CRIME_DATASET = "fdj4-gpfu"

# Austin council district polygons from City of Austin ArcGIS
DISTRICTS_GEOJSON_URL = (
    "https://services.arcgis.com/0L95CJ0VTaxqcmED/ArcGIS/rest/services/"
    "Council_Districts/FeatureServer/0/query"
    "?where=1%3D1&outFields=COUNCIL_DI&f=geojson"
)

_session: Optional[requests.Session] = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({
            "Accept": "application/json",
            "User-Agent": "austin311bot/0.1 (crime map)",
        })
    return _session


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _fetch_crime_counts(days_back: int) -> dict:
    """Fetch APD crime incident counts grouped by council district."""
    session = _get_session()
    cutoff = (_utc_now() - timedelta(days=days_back)).strftime("%Y-%m-%dT00:00:00")
    params = {
        "$select": "council_district, count(*) as cnt",
        "$where": f"occ_date >= '{cutoff}' AND council_district IS NOT NULL",
        "$group": "council_district",
        "$limit": 20,
    }
    app_token = os.getenv("AUSTIN_APP_TOKEN", "")
    if app_token:
        params["$$app_token"] = app_token

    url = f"{SOCRATA_BASE}/{CRIME_DATASET}.json"
    for attempt in range(3):
        try:
            resp = session.get(url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            return {
                str(int(float(row["council_district"]))): int(row["cnt"])
                for row in data
                if row.get("council_district") and row.get("cnt")
            }
        except Exception as e:
            if attempt < 2:
                time.sleep(2 ** attempt * 2)
            else:
                logger.error(f"crime counts ({days_back}d): {e}")
    return {}


def _fetch_districts_geojson() -> dict:
    """Fetch Austin council district boundary GeoJSON from ArcGIS."""
    session = _get_session()
    for attempt in range(3):
        try:
            resp = session.get(DISTRICTS_GEOJSON_URL, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            if attempt < 2:
                time.sleep(2 ** attempt * 2)
            else:
                logger.error(f"districts geojson: {e}")
    return {}


def generate_crime_map(days_back: int = 90) -> tuple[Optional[io.BytesIO], str]:
    """Generate an interactive choropleth map of APD crime by council district.

    Returns:
        tuple: (BytesIO buffer with HTML content, summary message)
    """
    try:
        import folium
    except ImportError:
        return None, "❌ Map generation requires 'folium'. Install: pip install folium"

    print("Fetching 30-day crime counts by district...")
    counts_30 = _fetch_crime_counts(30)
    print("Fetching 60-day crime counts by district...")
    counts_60 = _fetch_crime_counts(60)
    print("Fetching 90-day crime counts by district...")
    counts_90 = _fetch_crime_counts(days_back)

    if not counts_90:
        return None, "❌ No crime data returned from APD API"

    print("Fetching council district boundaries...")
    geojson = _fetch_districts_geojson()
    if not geojson or not geojson.get("features"):
        return None, "❌ Failed to fetch council district boundary GeoJSON"

    total_90 = sum(counts_90.values())
    total_30 = sum(counts_30.values())

    m = folium.Map(location=[30.2672, -97.7431], zoom_start=11, tiles="CartoDB positron")
    map_var = m.get_name()

    counts_js = json.dumps({
        "30": counts_30,
        "60": counts_60,
        "90": counts_90,
    })
    geojson_js = json.dumps(geojson)

    panel_html = f"""
    <div id="map-panel" style="position: absolute; top: 10px; left: 50%;
                transform: translateX(-50%); background: white; padding: 10px 16px;
                border-radius: 6px; box-shadow: 0 2px 6px rgba(0,0,0,0.3);
                z-index: 9999; font-family: sans-serif; text-align: center;
                min-width: 320px;">
        <b style="font-size: 15px;">🚔 Austin APD Crime by Council District</b><br/>
        <span id="map-summary" style="font-size: 12px; color: #555;"></span>
        <div style="display: flex; justify-content: center; gap: 4px; margin-top: 7px;">
            <button id="btn-30" onclick="setDayFilter(30)" class="fbtn">30d</button>
            <button id="btn-60" onclick="setDayFilter(60)" class="fbtn">60d</button>
            <button id="btn-90" onclick="setDayFilter(90)" class="fbtn active">90d</button>
        </div>
    </div>

    <div id="map-legend" style="position: absolute; bottom: 30px; right: 10px;
                background: white; padding: 8px 12px; border-radius: 6px;
                box-shadow: 0 2px 6px rgba(0,0,0,0.25); z-index: 9999;
                font-family: sans-serif; font-size: 11px;">
        <b style="font-size: 12px;">Incidents</b><br/>
        <div style="display:flex; align-items:center; gap:4px; margin-top:4px;">
            <div style="width:14px;height:14px;background:#ffffb2;border:1px solid #ccc;"></div> Fewer
        </div>
        <div style="display:flex; align-items:center; gap:4px; margin-top:2px;">
            <div style="width:14px;height:14px;background:#fecc5c;border:1px solid #ccc;"></div>
        </div>
        <div style="display:flex; align-items:center; gap:4px; margin-top:2px;">
            <div style="width:14px;height:14px;background:#fd8d3c;border:1px solid #ccc;"></div>
        </div>
        <div style="display:flex; align-items:center; gap:4px; margin-top:2px;">
            <div style="width:14px;height:14px;background:#f03b20;border:1px solid #ccc;"></div>
        </div>
        <div style="display:flex; align-items:center; gap:4px; margin-top:2px;">
            <div style="width:14px;height:14px;background:#bd0026;border:1px solid #ccc;"></div> More
        </div>
        <div style="margin-top:4px; color:#888; font-size:10px;">APD Crime Reports</div>
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
        var districtCounts = {counts_js};
        var districtGeojson = {geojson_js};
        var currentDays = 90;
        var geoLayer = null;

        var COLOR_STEPS = ['#ffffb2', '#fecc5c', '#fd8d3c', '#f03b20', '#bd0026'];

        function getColor(count, maxCount) {{
            if (!maxCount || count === 0) return '#ffffb2';
            var ratio = Math.min(count / maxCount, 1);
            var idx = Math.min(
                Math.floor(ratio * COLOR_STEPS.length),
                COLOR_STEPS.length - 1
            );
            return COLOR_STEPS[idx];
        }}

        function getMaxCount() {{
            var counts = districtCounts[String(currentDays)] || {{}};
            var vals = Object.values(counts);
            return vals.length > 0 ? Math.max.apply(null, vals) : 1;
        }}

        function styleFeature(feature) {{
            var dist = String(feature.properties.COUNCIL_DI);
            var counts = districtCounts[String(currentDays)] || {{}};
            var count = counts[dist] || 0;
            return {{
                fillColor: getColor(count, getMaxCount()),
                fillOpacity: 0.72,
                color: '#ffffff',
                weight: 2
            }};
        }}

        function makePopupHtml(dist) {{
            var counts = districtCounts[String(currentDays)] || {{}};
            var count = counts[dist] || 0;
            var total = Object.values(counts).reduce(function(a, b) {{ return a + b; }}, 0);
            var pct = total > 0 ? ((count / total) * 100).toFixed(1) : '0.0';
            return '<div style="font-family:sans-serif;min-width:180px;padding:4px;">' +
                '<b style="font-size:14px;">District ' + dist + '</b><br/>' +
                '<span style="color:#555;font-size:12px;">Last ' + currentDays + ' days</span>' +
                '<br/><br/>' +
                '<b style="font-size:16px;">' + count.toLocaleString() + '</b> incidents<br/>' +
                '<span style="color:#777;font-size:11px;">' + pct + '% of citywide total</span>' +
                '<br/><br/>' +
                '<a href="https://t.me/austin311bot" target="_blank" ' +
                'style="font-size:11px;color:#0066cc;">📲 More stats: @austin311bot</a>' +
                '</div>';
        }}

        function updateSummary() {{
            var counts = districtCounts[String(currentDays)] || {{}};
            var total = Object.values(counts).reduce(function(a, b) {{ return a + b; }}, 0);
            document.getElementById('map-summary').textContent =
                'Last ' + currentDays + ' days · ' + total.toLocaleString() +
                ' incidents across ' + Object.keys(counts).length + ' districts';
        }}

        function setDayFilter(days) {{
            currentDays = days;
            [30, 60, 90].forEach(function(d) {{
                var btn = document.getElementById('btn-' + d);
                if (btn) btn.classList.toggle('active', d === days);
            }});
            if (geoLayer) {{
                geoLayer.setStyle(styleFeature);
                geoLayer.eachLayer(function(layer) {{
                    var dist = String(layer.feature.properties.COUNCIL_DI);
                    if (layer.getPopup()) {{
                        layer.getPopup().setContent(makePopupHtml(dist));
                    }}
                    var counts = districtCounts[String(currentDays)] || {{}};
                    var count = counts[dist] || 0;
                    layer.setTooltipContent('<b>District ' + dist + '</b><br/>' +
                        count.toLocaleString() + ' incidents (last ' + currentDays + 'd)');
                }});
            }}
            updateSummary();
        }}

        function initMap() {{
            var leafletMap = {map_var};
            geoLayer = L.geoJSON(districtGeojson, {{
                style: styleFeature,
                onEachFeature: function(feature, layer) {{
                    var dist = String(feature.properties.COUNCIL_DI);
                    var counts = districtCounts[String(currentDays)] || {{}};
                    var count = counts[dist] || 0;
                    layer.bindTooltip(
                        '<b>District ' + dist + '</b><br/>' +
                        count.toLocaleString() + ' incidents (last ' + currentDays + 'd)',
                        {{sticky: true}}
                    );
                    layer.bindPopup(makePopupHtml(dist));
                    layer.on('mouseover', function() {{
                        layer.setStyle({{fillOpacity: 0.9, weight: 3, color: '#333'}});
                    }});
                    layer.on('mouseout', function() {{
                        geoLayer.resetStyle(layer);
                    }});
                    layer.on('click', function() {{
                        layer.openPopup();
                    }});
                }}
            }}).addTo(leafletMap);
            updateSummary();
        }}

        document.addEventListener('DOMContentLoaded', function() {{
            setTimeout(initMap, 500);
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
        f"🚔 *APD Crime Map by Council District*\n"
        f"_Last {days_back} days_\n\n"
        f"📊 *{total_90:,} incidents mapped* across 10 districts\n"
        f"_(Last 30 days: {total_30:,} incidents)_\n\n"
        f"Click a district to see stats. Use buttons to switch time windows.\n"
        f"_Source: [APD Crime Reports](https://data.austintexas.gov/d/fdj4-gpfu)_"
    )

    return buffer, summary
