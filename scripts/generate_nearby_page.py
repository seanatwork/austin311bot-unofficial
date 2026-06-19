#!/usr/bin/env python3
"""
Generate a self-contained "311 Near You" page at docs/nearby/index.html.

Fetches ALL 311 requests (no service_code filter) for the last 180 days,
writes a compact JSON data file, and generates a Leaflet map page with
address search and dynamic radius based on distance from downtown Austin.

Run:
    AUSTINAPIKEY=sk... python scripts/generate_nearby_page.py
"""
import json
import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Any

import requests

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(message)s")

OPEN311_URL = "https://311.austintexas.gov/open311/v2/requests.json"
API_KEY = os.getenv("AUSTINAPIKEY")
DAYS_BACK = 180

# Austin city center (roughly Congress & 6th)
AUSTIN_LAT = 30.2672
AUSTIN_LON = -97.7431

# Bounding box for valid Austin-area coordinates
LAT_MIN, LAT_MAX = 30.0, 30.6
LON_MIN, LON_MAX = -98.0, -97.4

_session: Optional[requests.Session] = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        headers = {
            "Accept": "application/json",
            "User-Agent": "austin311bot/nearby (Open311 bulk fetch)",
        }
        if API_KEY:
            headers["X-Api-Key"] = API_KEY
        _session.headers.update(headers)
    return _session


def _fetch_all_180days() -> list[dict]:
    """Fetch ALL 311 requests from the last 180 days, paginating all pages.

    Returns a list of raw Open311 records (no service_code filter).
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=DAYS_BACK)
    start_str = start.strftime("%Y-%m-%dT00:00:00Z")

    all_records: list[dict] = []
    seen_ids: set[str] = set()
    page = 1
    session = _get_session()
    total_fetched = 0

    while True:
        params: dict[str, Any] = {
            "start_date": start_str,
            "status": "open,closed",
            "page": page,
            "page_size": 1000,
        }

        records: list[dict] = []
        for attempt in range(6):
            try:
                resp = session.get(OPEN311_URL, params=params, timeout=60)
                resp.raise_for_status()
                records = resp.json()
                if not isinstance(records, list):
                    records = []
                break
            except Exception as e:
                if attempt == 5:
                    logger.warning(f"  Page {page} failed after 6 retries: {e}")
                    records = []
                    break
                delay = 2.0 * (2 ** attempt)
                logger.info(f"  Page {page} attempt {attempt + 1} failed ({e}), retry in {delay:.0f}s")
                time.sleep(delay)

        if not records:
            break

        for r in records:
            sid = r.get("service_request_id")
            if sid and sid not in seen_ids:
                seen_ids.add(sid)
                all_records.append(r)

        total_fetched += len(records)
        logger.info(f"  Page {page}: {len(records)} records ({total_fetched:,} total so far)")

        if len(records) < 1000:
            break

        page += 1
        # Respectful delay between pages
        time.sleep(1.5 if API_KEY else 3.0)

    logger.info(f"Fetched {len(all_records):,} unique records from {total_fetched:,} total")
    return all_records


def _compact_record(r: dict) -> dict | None:
    """Convert a raw Open311 record to a compact dict for the JSON data file."""
    lat = r.get("lat")
    lon = r.get("long")
    if lat is not None and lon is not None:
        try:
            lat_f = float(lat)
            lon_f = float(lon)
            if not (LAT_MIN <= lat_f <= LAT_MAX and LON_MIN <= lon_f <= LON_MAX):
                lat_f = lon_f = None
        except (ValueError, TypeError):
            lat_f = lon_f = None
    else:
        lat_f = lon_f = None

    if lat_f is None or lon_f is None:
        return None

    desc = (r.get("description") or "")[:240]
    notes = (r.get("status_notes") or "")[:160]
    address = (r.get("address") or "")[:120]

    return {
        "i": r.get("service_request_id", ""),
        "sc": r.get("service_code", ""),
        "d": desc,
        "s": (r.get("status") or "").lower(),
        "dt": (r.get("requested_datetime") or "")[:19],
        "l": [lat_f, lon_f],
        "a": address,
        "sn": notes,
    }


def _guess_category_label(service_code: str) -> str:
    """Map service_code prefix to a human-readable category label."""
    code_upper = (service_code or "").upper()
    if not code_upper:
        return "Other"

    prefixes = {
        "PRGR": "Parks", "PARK": "Parks",
        "ATCO": "Right of Way", "OBST": "Right of Way",
        "SBDE": "Streets", "SBPO": "Streets", "SBST": "Streets",
        "DRCH": "Drainage", "DRAI": "Drainage",
        "HHSG": "Graffiti",
        "APDN": "Noise", "APDP": "Parking", "APDC": "Crime",
        "WWRE": "Water", "WATR": "Water",
        "ANML": "Animal", "BICY": "Bicycle",
        "SIGN": "Signals", "TRAF": "Traffic", "LIGH": "Lighting",
        "CODE": "Code Enforcement", "ZONI": "Code Enforcement",
        "HEAL": "Health",
        "SOLI": "Solid Waste", "RECY": "Recycling", "TRSH": "Trash",
        "STRE": "Streets", "SIDE": "Sidewalks",
    }
    for prefix, label in prefixes.items():
        if code_upper.startswith(prefix):
            return label
    return "Other"


def _category_color(label: str) -> str:
    """Assign a hex color for each category for marker icon tinting."""
    palette = {
        "Parks": "#22c55e", "Right of Way": "#f59e0b", "Streets": "#64748b",
        "Drainage": "#06b6d4", "Graffiti": "#a855f7", "Noise": "#ef4444",
        "Parking": "#3b82f6", "Crime": "#dc2626", "Water": "#0ea5e9",
        "Animal": "#ec4899", "Bicycle": "#10b981", "Signals": "#eab308",
        "Traffic": "#f97316", "Lighting": "#fef08a", "Code Enforcement": "#78716c",
        "Health": "#14b8a6", "Solid Waste": "#4b5563", "Recycling": "#65a30d",
        "Trash": "#6b7280", "Sidewalks": "#a1a1aa", "Other": "#94a3b8",
    }
    return palette.get(label, "#94a3b8")


def main() -> None:
    logger.info("=" * 50)
    logger.info("Fetching ALL 311 requests (last 180 days, all service codes)...")
    logger.info("=" * 50)

    raw_records = _fetch_all_180days()

    compact = []
    category_counts: dict[str, int] = defaultdict(int)
    for r in raw_records:
        c = _compact_record(r)
        if c is not None:
            c["ca"] = _guess_category_label(c["sc"])
            category_counts[c["ca"]] += 1
            compact.append(c)

    logger.info(f"Records with valid coords: {len(compact):,}")

    logger.info("\nCategory breakdown:")
    for label, count in sorted(category_counts.items(), key=lambda x: -x[1]):
        logger.info(f"  {label}: {count:,}")

    categories = {}
    for label in sorted(category_counts.keys()):
        categories[label] = {
            "color": _category_color(label),
            "count": category_counts[label],
        }

    payload = {
        "fetched": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "daysBack": DAYS_BACK,
        "center": [AUSTIN_LAT, AUSTIN_LON],
        "categories": categories,
        "records": compact,
    }

    docs_dir = Path(__file__).resolve().parent.parent / "docs" / "nearby"
    docs_dir.mkdir(parents=True, exist_ok=True)

    data_path = docs_dir / "data.json"
    data_json = json.dumps(payload, separators=(",", ":"))
    data_path.write_text(data_json, encoding="utf-8")
    data_size_mb = data_path.stat().st_size / (1024 * 1024)
    logger.info(f"\nWrote {data_size_mb:.1f} MB to {data_path} ({len(compact):,} records)")

    _generate_html(docs_dir, payload)
    _cache_results(raw_records)
    logger.info("\n✅ Nearby page generation complete!")


def _cache_results(raw_records: list[dict]) -> None:
    """Store raw records in the shared Open311 cache."""
    try:
        from open311_cache import init_cache, cache_records

        init_cache()
        cache_records("nearby", raw_records)
        logger.info(f"Cached {len(raw_records)} records for 'nearby'")
    except Exception as e:
        logger.warning(f"Could not cache to SQLite: {e}")


def _generate_html(docs_dir: Path, payload: dict) -> None:
    """Generate docs/nearby/index.html — the self-contained Leaflet map page."""
    total = len(payload["records"])
    center_lat = payload["center"][0]
    center_lon = payload["center"][1]

    html = _HTML_TEMPLATE % (
        json.dumps(payload, separators=(",", ":")),
        center_lat,
        center_lon,
    )

    out_path = docs_dir / "index.html"
    out_path.write_text(html, encoding="utf-8")
    html_size_kb = out_path.stat().st_size / 1024
    logger.info(f"Wrote {html_size_kb:.0f} KB to {out_path}")


_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1.0" />
  <title>311 Near You — Austin</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
  <link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.css" />
  <link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.Default.css" />
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    :root {
      --bg: #f8fafc; --text: #1e293b; --text-head: #0f172a; --text-desc: #64748b;
      --bg-card: #ffffff; --border: #e2e8f0; --input-bg: #ffffff; --shadow: 0 2px 8px rgba(0,0,0,0.12);
    }
    .dark {
      --bg: #0f1117; --text: #e2e8f0; --text-head: #f8fafc; --text-desc: #94a3b8;
      --bg-card: #1e2230; --border: #2d3348; --input-bg: #1a1f2e; --shadow: 0 2px 8px rgba(0,0,0,0.4);
    }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg); color: var(--text); height: 100vh; display: flex; flex-direction: column;
      transition: background 0.2s, color 0.2s;
    }
    #map { flex: 1; width: 100%; }
    #search-bar {
      display: flex; gap: 8px; padding: 10px 16px; align-items: center;
      background: var(--bg-card); border-bottom: 1px solid var(--border);
      flex-wrap: wrap; transition: background 0.2s;
    }
    #search-input {
      flex: 1; min-width: 180px; padding: 8px 12px; border: 1px solid var(--border);
      border-radius: 6px; font-size: 14px; background: var(--input-bg); color: var(--text);
      outline: none; transition: border-color 0.15s;
    }
    #search-input:focus { border-color: #3b82f6; }
    #search-btn, #geolocate-btn {
      padding: 8px 14px; border: 1px solid var(--border); border-radius: 6px;
      font-size: 13px; font-weight: 500; cursor: pointer; transition: all 0.15s;
    }
    #search-btn { background: #3b82f6; color: white; border-color: #3b82f6; }
    #search-btn:hover { background: #2563eb; }
    #geolocate-btn { background: var(--bg-card); color: var(--text); }
    #geolocate-btn:hover { background: var(--border); }
    #radius-badge {
      font-size: 12px; color: var(--text-desc); white-space: nowrap;
      padding: 4px 8px; background: var(--bg); border-radius: 4px;
    }
    #stats-badge {
      font-size: 12px; color: var(--text-desc); white-space: nowrap;
    }
    #theme-toggle {
      padding: 6px 10px; border: 1px solid var(--border); border-radius: 6px;
      font-size: 12px; background: var(--bg-card); color: var(--text-desc);
      cursor: pointer; transition: background 0.15s;
    }
    #theme-toggle:hover { background: var(--border); }
    .leaflet-popup-content-wrapper {
      border-radius: 8px; font-size: 13px; max-width: 320px;
    }
    .leaflet-popup-content { margin: 10px 14px; line-height: 1.5; }
    .popup-category { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; color: white; }
    .popup-addr { color: var(--text-desc); font-size: 12px; margin-top: 4px; }
    .popup-desc { margin-top: 6px; font-size: 12px; color: var(--text); }
    .popup-status { font-size: 12px; margin-top: 4px; }
    .popup-link { display: block; margin-top: 6px; font-size: 12px; color: #3b82f6; text-decoration: none; }
    .leaflet-control-zoom a { background: var(--bg-card) !important; color: var(--text) !important; border-color: var(--border) !important; }
    @media (max-width: 600px) {
      #search-bar { padding: 8px 10px; }
      #search-btn, #geolocate-btn { padding: 6px 10px; font-size: 12px; }
      #stats-badge { display: none; }
    }
  </style>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script src="https://unpkg.com/leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js"></script>
  <!-- Google tag (gtag.js) -->
  <script async src="https://www.googletagmanager.com/gtag/js?id=G-TS158R7XSN"></script>
  <script>window.dataLayer=window.dataLayer||[];function gtag(){dataLayer.push(arguments);}gtag('js',new Date());gtag('config','G-TS158R7XSN');</script>
</head>
<body>
  <div id="search-bar">
    <input id="search-input" type="text" placeholder="Enter address, neighborhood, or ZIP code\u2026" />
    <button id="search-btn">\ud83d\udd0d Search</button>
    <button id="geolocate-btn">\ud83d\udccd My Location</button>
    <span id="radius-badge">\ud83d\udccf Radius: \u2014</span>
    <span id="stats-badge">\ud83d\udcca \u2014</span>
    <button id="theme-toggle">\ud83c\udf19 Dark</button>
  </div>
  <div id="map"></div>

  <script>
    // \u2500\u2500 Data \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    var PAYLOAD = %s;
    var records = PAYLOAD.records;
    var categories = PAYLOAD.categories;
    var centerLat = %s;
    var centerLon = %s;

    // \u2500\u2500 Dynamic radius \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    function haversineKm(lat1, lon1, lat2, lon2) {
      var R = 6371;
      var dLat = (lat2 - lat1) * Math.PI / 180;
      var dLon = (lon2 - lon1) * Math.PI / 180;
      var a = Math.sin(dLat/2)*Math.sin(dLat/2) +
              Math.cos(lat1*Math.PI/180)*Math.cos(lat2*Math.PI/180)*
              Math.sin(dLon/2)*Math.sin(dLon/2);
      return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1-a));
    }

    function calcRadiusKm(distFromCenterKm) {
      if (distFromCenterKm <= 5) return 1.2;    // downtown core
      if (distFromCenterKm <= 13) return 3.2;   // inner neighborhoods
      if (distFromCenterKm <= 24) return 5.6;   // outer Austin
      return 8;                                  // suburbs / far out
    }

    // \u2500\u2500 Build map \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    var map = L.map('map', { zoomControl: true, attributionControl: false })
      .setView([centerLat, centerLon], 11);

    L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png', {
      maxZoom: 19, attribution: '&copy; <a href="https://carto.com/">CARTO</a>'
    }).addTo(map);

    // \u2500\u2500 Marker clusters \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    var allMarkers = L.markerClusterGroup({
      chunkedLoading: true,
      maxClusterRadius: 50,
      spiderfyOnMaxZoom: true,
      showCoverageOnHover: false,
      iconCreateFunction: function(cluster) {
        var count = cluster.getChildCount();
        var size = count < 10 ? 'small' : count < 100 ? 'medium' : 'large';
        return L.divIcon({
          html: '<div style="background:#3b82f6;color:white;border-radius:50%%;width:' +
                (size==='small'?32:size==='medium'?40:48)+'px;height:' +
                (size==='small'?32:size==='medium'?40:48)+'px;display:flex;align-items:center;justify-content:center;' +
                'font-weight:700;font-size:'+(size==='small'?11:size==='medium'?13:15)+'px;box-shadow:0 2px 6px rgba(0,0,0,0.3);">' + count + '</div>',
          className: '',
          iconSize: size==='small'?[32,32]:size==='medium'?[40,40]:[48,48],
        });
      }
    });
    var markerData = [];

    records.forEach(function(r) {
      var lat = r.l[0], lon = r.l[1];
      var cat = r.ca || 'Other';
      var catInfo = categories[cat] || { color: '#94a3b8' };
      var color = catInfo.color;
      var isOpen = r.s === 'open';
      var statusIcon = isOpen ? '\ud83d\udd34' : '\ud83d\udfe2';
      var desc = (r.d || '').substring(0, 200);

      var popupHtml = '<div>' +
        '<span class="popup-category" style="background:' + color + '">' + cat + '</span><br/>' +
        (r.a ? '<div class="popup-addr">\ud83d\udccd ' + r.a + '</div>' : '') +
        (desc ? '<div class="popup-desc">' + escHtml(desc) + '</div>' : '') +
        '<div class="popup-status">' + statusIcon + ' ' + (isOpen ? 'Open' : 'Closed') +
        (r.dt ? ' \u00b7 ' + r.dt.split('T')[0] : '') + '</div>' +
        (r.sn ? '<div class="popup-desc" style="color:#888;font-size:11px;">\ud83d\udcdd ' + escHtml(r.sn.substring(0,200)) + '</div>' : '') +
        '<a class="popup-link" href="https://311.austintexas.gov/tickets/' + r.i + '" target="_blank">View ticket \u2192</a>' +
        '</div>';

      var iconSize = isOpen ? 10 : 8;
      var marker = L.circleMarker([lat, lon], {
        radius: iconSize,
        fillColor: color,
        color: '#ffffff',
        weight: 1.5,
        opacity: 0.9,
        fillOpacity: 0.8,
      }).bindPopup(popupHtml, { maxWidth: 320, className: '' });

      if (desc) {
        marker.bindTooltip(desc.substring(0, 80), { direction: 'top', offset: [0, -8] });
      }

      allMarkers.addLayer(marker);
      markerData.push({ marker: marker, record: r });
    });

    map.addLayer(allMarkers);

    function escHtml(s) {
      var d = document.createElement('div');
      d.textContent = s;
      return d.innerHTML;
    }

    // \u2500\u2500 Filter by location & radius \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    var currentCenter = null;
    var currentRadiusKm = null;
    var activeCircle = null;

    function filterByLocation(lat, lon) {
      currentCenter = [lat, lon];
      var distFromCenter = haversineKm(lat, lon, centerLat, centerLon);
      currentRadiusKm = calcRadiusKm(distFromCenter);

      document.getElementById('radius-badge').textContent =
        '\ud83d\udccf Radius: ' + currentRadiusKm.toFixed(1) + ' km';

      if (activeCircle) map.removeLayer(activeCircle);
      activeCircle = L.circle([lat, lon], {
        radius: currentRadiusKm * 1000,
        color: '#3b82f6',
        fillColor: '#3b82f6',
        fillOpacity: 0.06,
        weight: 2,
        dashArray: '6 4',
      }).addTo(map);

      var inRange = 0, openCount = 0;
      allMarkers.clearLayers();

      markerData.forEach(function(item) {
        var mlat = item.record.l[0], mlon = item.record.l[1];
        var dist = haversineKm(lat, lon, mlat, mlon);
        if (dist <= currentRadiusKm) {
          allMarkers.addLayer(item.marker);
          inRange++;
          if (item.record.s === 'open') openCount++;
        }
      });

      document.getElementById('stats-badge').textContent =
        '\ud83d\udcca ' + inRange + ' requests \u00b7 ' + openCount + ' open';

      map.setView([lat, lon], map.getZoom() < 13 ? 13 : map.getZoom());
    }

    // \u2500\u2500 Geocoding \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    function geocode(query) {
      var url = 'https://nominatim.openstreetmap.org/search?format=json&q=' +
                encodeURIComponent(query + ', Austin, TX') +
                '&limit=5&addressdetails=0';

      document.getElementById('search-btn').textContent = '\u23f3 Searching\u2026';

      fetch(url, { headers: { 'User-Agent': 'austin311bot/nearby' } })
        .then(function(r) { return r.json(); })
        .then(function(data) {
          document.getElementById('search-btn').textContent = '\ud83d\udd0d Search';
          if (!data || !data.length) {
            alert('Location not found. Try a different address or neighborhood name.');
            return;
          }
          var lat = parseFloat(data[0].lat);
          var lon = parseFloat(data[0].lon);
          filterByLocation(lat, lon);
          var q = encodeURIComponent(query);
          history.replaceState(null, '', '?q=' + q);
        })
        .catch(function(err) {
          document.getElementById('search-btn').textContent = '\ud83d\udd0d Search';
          alert('Geocoding failed: ' + err.message);
        });
    }

    // \u2500\u2500 Geolocation \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    function geolocate() {
      if (!navigator.geolocation) {
        alert('Geolocation is not supported by your browser.');
        return;
      }
      document.getElementById('geolocate-btn').textContent = '\u23f3 Locating\u2026';
      navigator.geolocation.getCurrentPosition(
        function(pos) {
          document.getElementById('geolocate-btn').textContent = '\ud83d\udccd My Location';
          filterByLocation(pos.coords.latitude, pos.coords.longitude);
          history.replaceState(null, '', '?q=my-location');
        },
        function() {
          document.getElementById('geolocate-btn').textContent = '\ud83d\udccd My Location';
          alert('Could not get your location. Please make sure location access is enabled.');
        },
        { enableHighAccuracy: true, timeout: 10000 }
      );
    }

    document.getElementById('search-btn').addEventListener('click', function() {
      var q = document.getElementById('search-input').value.trim();
      if (q) geocode(q);
    });
    document.getElementById('search-input').addEventListener('keydown', function(e) {
      if (e.key === 'Enter') {
        var q = e.target.value.trim();
        if (q) geocode(q);
      }
    });
    document.getElementById('geolocate-btn').addEventListener('click', geolocate);

    // \u2500\u2500 Theme toggle \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    var htmlEl = document.documentElement;
    var themeBtn = document.getElementById('theme-toggle');
    var savedTheme = localStorage.getItem('theme');
    if (savedTheme === 'dark') {
      htmlEl.classList.add('dark');
      themeBtn.textContent = '\u2600\ufe0f Light';
      map.eachLayer(function(l) {
        if (l instanceof L.TileLayer) {
          map.removeLayer(l);
          L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
            maxZoom: 19, attribution: '&copy; <a href="https://carto.com/">CARTO</a>'
          }).addTo(map);
        }
      });
    }
    themeBtn.addEventListener('click', function() {
      var isDark = htmlEl.classList.toggle('dark');
      themeBtn.textContent = isDark ? '\u2600\ufe0f Light' : '\ud83c\udf19 Dark';
      localStorage.setItem('theme', isDark ? 'dark' : 'light');
      if (activeCircle) {
        map.removeLayer(activeCircle);
        activeCircle.setStyle({ color: '#60a5fa', fillColor: '#60a5fa' });
        map.addLayer(activeCircle);
      }
      map.eachLayer(function(l) {
        if (l instanceof L.TileLayer) {
          map.removeLayer(l);
          var tileUrl = isDark
            ? 'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png'
            : 'https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png';
          L.tileLayer(tileUrl, { maxZoom: 19 }).addTo(map);
        }
      });
    });

    // \u2500\u2500 Load from URL param \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    (function() {
      var params = new URLSearchParams(window.location.search);
      var q = params.get('q');
      if (q) {
        document.getElementById('search-input').value = decodeURIComponent(q);
        geocode(q);
      }
    })();
  </script>
</body>
</html>"""


if __name__ == "__main__":
    main()