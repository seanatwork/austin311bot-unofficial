#!/usr/bin/env python3
"""
Generate static data files for the CapMetro / MetroBike web page.

Produces:
  docs/capmetro/data.json  — All aggregated data for the map + charts
  docs/capmetro/index.html — Interactive map page

Run:  python scripts/generate_capmetro_data.py
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from capmetro.metrobike import (
    get_total_trips,
    get_electric_vs_classic,
    get_kiosk_flow,
    get_kiosk_evolution,
    get_membership_breakdown,
    get_yearly_membership,
    KIOSK_LOCATIONS,
    resolve_kiosk_name_to_id,
)

GA_SNIPPET = '''<!-- Google tag (gtag.js) -->
<script async src="https://www.googletagmanager.com/gtag/js?id=G-TS158R7XSN"></script>
<script>
  window.dataLayer = window.dataLayer || [];
  function gtag(){dataLayer.push(arguments);}
  gtag('js', new Date());
  gtag('config', 'G-TS158R7XSN');
</script>
'''

OG_META = '''    <meta property="og:type" content="website" />
    <meta property="og:title" content="Austin 311 — CapMetro MetroBike" />
    <meta property="og:description" content="MetroBike trip data: electric vs classic usage, kiosk flows, station evolution, and membership trends — 2013 to 2024." />
    <meta property="og:url" content="https://austin311.com/capmetro/" />
    <meta property="og:image" content="https://austin311.com/og-default.png" />
    <meta property="og:image:width" content="1200" />
    <meta property="og:image:height" content="630" />
    <meta property="og:site_name" content="Austin 311" />
    <meta name="twitter:card" content="summary_large_image" />
    <meta name="twitter:title" content="Austin 311 — CapMetro MetroBike" />
    <meta name="twitter:description" content="MetroBike trip data: electric vs classic usage, kiosk flows, station evolution, and membership trends — 2013 to 2024." />
    <meta name="twitter:image" content="https://austin311.com/og-default.png" />
    <meta name="description" content="MetroBike trip data: electric vs classic usage, kiosk flows, station evolution, and membership trends — 2013 to 2024." />'''


def format_central_time() -> str:
    utc_now = datetime.now(timezone.utc)
    month = utc_now.month
    is_dst = 3 <= month <= 11
    offset_hours = -5 if is_dst else -6
    central_now = utc_now + timedelta(hours=offset_hours)
    tz_abbr = "CDT" if is_dst else "CST"
    return central_now.strftime(f"%Y-%m-%d %I:%M %p {tz_abbr}")


def collect_data() -> dict:
    logger.info("Fetching total trips...")
    totals = get_total_trips()

    logger.info("Fetching electric vs classic...")
    bike_types = get_electric_vs_classic()

    logger.info("Fetching kiosk flow (top 25)...")
    flows = get_kiosk_flow(top_n=25)

    logger.info("Fetching kiosk evolution...")
    evolution = get_kiosk_evolution()

    logger.info("Fetching membership breakdown...")
    memberships = get_membership_breakdown()

    logger.info("Fetching yearly membership...")
    yearly_membership = get_yearly_membership()

    # Build kiosk locations lookup
    kiosk_locations = []
    kiosk_by_id: dict[str, dict] = {}
    for kid, (lat, lng, name) in KIOSK_LOCATIONS.items():
        entry = {
            "id": kid,
            "name": name,
            "lat": round(lat, 5),
            "lng": round(lng, 5),
        }
        kiosk_locations.append(entry)
        kiosk_by_id[kid] = entry

    # Enrich kiosk evolution with locations
    kiosk_evolution_enriched = []
    for k in evolution["kiosks"]:
        loc = KIOSK_LOCATIONS.get(k["kiosk_id"])
        if loc:
            k["lat"] = round(loc[0], 5)
            k["lng"] = round(loc[1], 5)
            k["name"] = loc[2]
        kiosk_evolution_enriched.append(k)

    # Resolve flow origin/destination to kiosk IDs server-side
    resolved_flows = []
    for f in flows["flows"]:
        origin_id = resolve_kiosk_name_to_id(f["origin"])
        dest_id = resolve_kiosk_name_to_id(f["destination"])
        if origin_id and dest_id:
            resolved_flows.append({
                "origin": f["origin"],
                "destination": f["destination"],
                "origin_id": origin_id,
                "dest_id": dest_id,
                "trips": f["trips"],
            })

    logger.info(f"Resolved {len(resolved_flows)} / {len(flows['flows'])} flows to kiosk IDs")

    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_note": "Data ends June 30, 2024. The city has not received updated trip data from the vendor since then.",
        "totals": {
            "total_trips": totals["total_trips"],
            "by_year": {str(k): v for k, v in totals["by_year"].items()},
            "first_year": totals["first_year"],
            "last_year": totals["last_year"],
        },
        "bike_types": {
            "by_year": {str(k): v for k, v in bike_types["by_year"].items()},
            "totals": bike_types["totals"],
            "electric_pct": bike_types["electric_pct"],
        },
        "flows": resolved_flows,
        "kiosk_evolution": kiosk_evolution_enriched,
        "kiosk_locations": kiosk_locations,
        "memberships": {
            "types": memberships["membership_types"],
            "total_trips": memberships["total_trips"],
        },
        "yearly_membership": {str(k): v for k, v in yearly_membership["by_year"].items()},
    }
    return data


def generate_html(data: dict) -> str:
    now_str = format_central_time()
    data_json = json.dumps(data)

    total_trips = data["totals"]["total_trips"]
    first_year = data["totals"]["first_year"]
    last_year = data["totals"]["last_year"]
    electric_pct = data["bike_types"]["electric_pct"]
    classic_count = data["bike_types"]["totals"]["classic"]
    electric_count = data["bike_types"]["totals"]["electric"]
    active_kiosks = sum(1 for k in data["kiosk_evolution"] if k["active"])
    removed_kiosks = sum(1 for k in data["kiosk_evolution"] if not k["active"] and k["last_year"] < 2024)

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>CapMetro MetroBike — Austin 311</title>
  {OG_META}
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    :root {{
      --bg: #f8fafc; --bg-card: #ffffff; --text: #1e293b; --text-head: #0f172a;
      --text-desc: #64748b; --border: #e2e8f0; --border-hover: #94a3b8;
      --blue: #2563eb; --green: #16a34a; --orange: #ea580c; --purple: #9333ea;
    }}
    html.dark {{
      --bg: #0f1117; --bg-card: #1e2230; --text: #e2e8f0; --text-head: #f8fafc;
      --text-desc: #64748b; --border: #2d3348; --border-hover: #4a5568;
    }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg); color: var(--text); min-height: 100vh;
      display: flex; flex-direction: column; align-items: center;
      padding: 1.5rem 1rem 2rem; transition: background 0.2s, color 0.2s;
    }}
    #theme-toggle {{
      position: fixed; top: 14px; right: 16px; z-index: 200;
      background: var(--bg-card); border: 1px solid var(--border);
      border-radius: 6px; padding: 5px 10px; font-size: 0.75rem;
      color: var(--text-desc); cursor: pointer; user-select: none;
    }}
    #theme-toggle:hover {{ color: var(--text); border-color: var(--border-hover); }}
    header {{ text-align: center; margin-bottom: 1rem; width: 100%; max-width: 1100px; }}
    header h1 {{ font-size: 1.8rem; font-weight: 700; color: var(--text-head); }}
    header p {{ color: var(--text-desc); font-size: 0.9rem; margin-top: 0.3rem; }}
    header .data-note {{ color: #ea580c; font-size: 0.82rem; margin-top: 0.5rem; }}
    .stats-row {{
      display: flex; gap: 0.75rem; width: 100%; max-width: 1100px;
      margin-bottom: 1rem; flex-wrap: wrap; justify-content: center;
    }}
    .stat-card {{
      flex: 1; min-width: 140px; max-width: 200px;
      background: var(--bg-card); border: 1px solid var(--border);
      border-radius: 10px; padding: 0.8rem 1rem; text-align: center;
    }}
    .stat-value {{ font-size: 1.5rem; font-weight: 700; color: var(--text-head); }}
    .stat-label {{ font-size: 0.72rem; color: var(--text-desc); }}

    .charts-row {{
      display: flex; gap: 1rem; width: 100%; max-width: 1100px;
      margin-bottom: 1rem; flex-wrap: wrap;
    }}
    .chart-card {{
      flex: 1; min-width: 300px; background: var(--bg-card);
      border: 1px solid var(--border); border-radius: 10px;
      padding: 1rem; position: relative;
    }}
    .chart-card h3 {{ font-size: 0.9rem; margin-bottom: 0.5rem; color: var(--text-head); }}
    .chart-card canvas {{ width: 100% !important; height: 250px !important; }}
    .map-row {{
      display: flex; gap: 0.75rem; width: 100%; max-width: 1100px;
      margin-bottom: 1rem;
    }}
    #map {{
      flex: 1; height: 520px;
      border-radius: 12px; border: 1px solid var(--border);
      z-index: 1;
    }}
    .flow-panel {{
      width: 280px; min-width: 220px;
      background: var(--bg-card); border: 1px solid var(--border);
      border-radius: 10px; padding: 0.6rem;
      overflow-y: auto; max-height: 520px;
      font-size: 0.78rem;
    }}
    .flow-panel h3 {{ font-size: 0.82rem; margin-bottom: 0.4rem; color: var(--text-head); }}
    .flow-item {{
      padding: 4px 6px; border-radius: 5px; cursor: pointer;
      display: flex; justify-content: space-between; align-items: center;
      transition: background 0.1s;
      border-left: 3px solid transparent;
      margin-bottom: 2px;
    }}
    .flow-item:hover {{ background: var(--bg-card-hover, #f1f5f9); }}
    .flow-item.active {{ border-left-color: #9333ea; background: rgba(147, 51, 234, 0.08); }}
    .flow-item .route {{ flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .flow-item .count {{ font-weight: 600; color: var(--text-head); margin-left: 6px; font-size: 0.75rem; }}
    .flow-item .arrow {{ color: var(--text-desc); margin: 0 2px; }}
    .controls {{
      display: flex; gap: 0.5rem; width: 100%; max-width: 1100px;
      margin-bottom: 0.75rem; flex-wrap: wrap; align-items: center;
      justify-content: center;
    }}
    .controls label {{ font-size: 0.82rem; color: var(--text-desc); }}
    .controls select, .controls button {{
      padding: 4px 10px; border: 1px solid var(--border);
      border-radius: 6px; background: var(--bg-card); color: var(--text);
      font-size: 0.82rem; cursor: pointer;
    }}
    .controls select:hover, .controls button:hover {{
      border-color: var(--border-hover);
    }}
    .controls button.active {{
      background: var(--blue); color: white; border-color: var(--blue);
    }}
    @media (max-width: 768px) {{
      .map-row {{ flex-direction: column; }}
      .flow-panel {{ width: 100%; max-height: 200px; }}
      #map {{ height: 350px; }}
    }}
    @media (max-width: 600px) {{
      .stat-card {{ min-width: 100px; }}
      .chart-card {{ min-width: 100%; }}
      header h1 {{ font-size: 1.4rem; }}
    }}
    footer {{
      margin-top: 1.5rem; font-size: 0.75rem; color: var(--text-desc);
      text-align: center; max-width: 600px;
    }}
    footer a {{ color: var(--text-desc); }}
    .kiosk-popup {{ font-family: sans-serif; font-size: 13px; }}
    .kiosk-popup b {{ color: var(--text-head); }}
  </style>
  {GA_SNIPPET}
</head>
<body>

<button id="theme-toggle" aria-label="Toggle dark mode">🌙 Dark</button>

<header>
  <h1>🚲 CapMetro MetroBike</h1>
  <p>Trip data from Austin's bikeshare system — {first_year} to {last_year}</p>
  <p class="data-note">⚠️ Data ends June 30, 2024. The city has not received updated trip data from the vendor since then.</p>
</header>

<div class="stats-row">
  <div class="stat-card">
    <div class="stat-value">{total_trips:,}</div>
    <div class="stat-label">Total Trips</div>
  </div>
  <div class="stat-card">
    <div class="stat-value">{classic_count:,}</div>
    <div class="stat-label">Classic Bike Trips</div>
  </div>
  <div class="stat-card">
    <div class="stat-value">{electric_count:,}</div>
    <div class="stat-label">Electric Bike Trips</div>
  </div>
  <div class="stat-card">
    <div class="stat-value">{electric_pct}%</div>
    <div class="stat-label">Electric Share</div>
  </div>
  <div class="stat-card">
    <div class="stat-value">{active_kiosks}</div>
    <div class="stat-label">Active Stations</div>
  </div>
  <div class="stat-card">
    <div class="stat-value">{removed_kiosks}</div>
    <div class="stat-label">Removed Stations</div>
  </div>
</div>

<div class="controls">
  <label>Year:</label>
  <select id="year-filter" onchange="updateMap()">
    <option value="all">All Years</option>
  </select>
  <label>Show:</label>
  <button id="btn-stations" class="active" onclick="toggleLayer('stations')">📍 Stations</button>
  <button id="btn-flows" onclick="toggleLayer('flows')">↔️ Flows</button>
</div>

<div class="map-row">
  <div id="map"></div>
  <div class="flow-panel" id="flow-panel">
    <h3>↔️ Top Routes</h3>
    <div id="flow-list"></div>
  </div>
</div>

<div class="charts-row">
  <div class="chart-card">
    <h3>Trips per Year</h3>
    <canvas id="chart-yearly"></canvas>
  </div>
  <div class="chart-card">
    <h3>Electric vs Classic</h3>
    <canvas id="chart-bike-type"></canvas>
  </div>
</div>

<div class="charts-row">
  <div class="chart-card">
    <h3>Top 10 Stations by Volume</h3>
    <canvas id="chart-top-stations"></canvas>
  </div>
  <div class="chart-card">
    <h3>Membership Types</h3>
    <canvas id="chart-membership"></canvas>
  </div>
</div>

<footer>
  Data: <a href="https://data.austintexas.gov/dataset/Austin-MetroBike-Trip-Data/tyfh-5r8s" target="_blank" rel="noopener">Austin MetroBike Trip Data</a>
  &nbsp;·&nbsp; Last updated: {now_str}
  &nbsp;·&nbsp; <a href="https://github.com/seanatwork" target="_blank" rel="noopener">Vibecoded with ❤️</a>
</footer>

<script>
const DATA = {data_json};

const btn = document.getElementById("theme-toggle");
const html = document.documentElement;
if (localStorage.getItem("theme") === "dark") {{
  html.classList.add("dark"); btn.textContent = "☀️ Light";
}}
btn.addEventListener("click", () => {{
  const isDark = html.classList.toggle("dark");
  btn.textContent = isDark ? "☀️ Light" : "🌙 Dark";
  localStorage.setItem("theme", isDark ? "dark" : "light");
}});

const yearFilter = document.getElementById("year-filter");
const years = Object.keys(DATA.totals.by_year).sort();
years.forEach(y => {{
  const opt = document.createElement("option");
  opt.value = y; opt.textContent = y;
  yearFilter.appendChild(opt);
}});

const map = L.map("map", {{ zoomControl: true }}).setView([30.27, -97.74], 12);
L.tileLayer("https://{{s}}.basemaps.cartocdn.com/light_all/{{z}}/{{x}}/{{y}}{{r}}.png?key=cb1_2du8_1_a49e4774820276874a1a5b33", {{
  attribution: "&copy; <a href='https://carto.com/'>CARTO</a>",
  subdomains: "abcd", maxZoom: 19,
}}).addTo(map);

const stationLayer = L.layerGroup().addTo(map);
const flowLayer = L.layerGroup();

function getKioskTrips(kioskId) {{
  const k = DATA.kiosk_evolution.find(k => k.kiosk_id === kioskId);
  return k ? k.total_trips : 0;
}}

function updateMap() {{
  const year = document.getElementById("year-filter").value;

  stationLayer.clearLayers();
  flowLayer.clearLayers();

  const maxTrips = Math.max(...DATA.kiosk_evolution.map(k => k.total_trips), 1);

  DATA.kiosk_locations.forEach(k => {{
    if (!k.lat || !k.lng) return;
    const trips = getKioskTrips(k.id);
    const evo = DATA.kiosk_evolution.find(ke => ke.kiosk_id === k.id);
    if (!evo) return;

    // Apply year filter
    if (year !== "all" && (parseInt(year) < evo.first_year || parseInt(year) > evo.last_year)) {{
      return;
    }}

    const radius = Math.max(5, Math.sqrt(trips / maxTrips) * 30);
    const isRemoved = !evo.active;
    const color = isRemoved ? "#ea580c" : "#2563eb";
    const opacity = isRemoved ? 0.5 : 0.8;

    const marker = L.circleMarker([k.lat, k.lng], {{
      radius, color, opacity, fillColor: color, fillOpacity: 0.3, weight: 2,
    }}).addTo(stationLayer);

    const yearsActive = `${{evo.first_year}}-${{evo.last_year}}`;
    const statusText = isRemoved ? "🔴 Removed" : "🟢 Active";

    marker.bindPopup(`
      <div class="kiosk-popup">
        <b>${{k.name}}</b><br/>
        ${{statusText}} · ${{yearsActive}}<br/>
        <b>${{trips.toLocaleString()}}</b> trips
      </div>
    `);
  }});

  // Always populate flowLayer with data; visibility is controlled separately
  flowPolylines = [];
  DATA.flows.slice(0, 15).forEach((flow, fi) => {{
      const origin = DATA.kiosk_locations.find(k => k.id === flow.origin_id);
      const dest = DATA.kiosk_locations.find(k => k.id === flow.dest_id);
      if (!origin || !dest) return;

      const weight = Math.max(1, Math.log10(flow.trips) * 2);
      const opacity = Math.min(0.6, flow.trips / DATA.flows[0].trips);

      const latlngs = [
        [origin.lat, origin.lng],
        [(origin.lat + dest.lat) / 2 + 0.005, (origin.lng + dest.lng) / 2],
        [dest.lat, dest.lng],
      ];

      const polyline = L.polyline(latlngs, {{
        color: "#9333ea", weight, opacity,
      }}).addTo(flowLayer);
      flowPolylines.push(polyline);

      polyline.on('click', () => selectFlow(fi));

      polyline.bindPopup(`
        <div class="kiosk-popup">
          <b>${{flow.origin}}</b> → <b>${{flow.destination}}</b><br/>
          <b>${{flow.trips.toLocaleString()}}</b> trips
        </div>
      `);
    }});

  // Ensure layer visibility after redraw
  if (showStations) map.addLayer(stationLayer);
  else map.removeLayer(stationLayer);
  if (showFlows) map.addLayer(flowLayer);
  else map.removeLayer(flowLayer);
}}

let showStations = true;
let showFlows = false;
let selectedFlow = null;
let flowPolylines = [];

function toggleLayer(layer) {{
  if (layer === "stations") {{
    showStations = !showStations;
    document.getElementById("btn-stations").classList.toggle("active");
    if (showStations) map.addLayer(stationLayer);
    else map.removeLayer(stationLayer);
  }} else {{
    showFlows = !showFlows;
    document.getElementById("btn-flows").classList.toggle("active");
    if (showFlows) {{
      map.addLayer(flowLayer);
      // Also scroll to first flow if none selected
      if (!selectedFlow) {{
        const first = document.querySelector('.flow-item');
        if (first) first.scrollIntoView({{ behavior: 'smooth', block: 'nearest' }});
      }}
    }} else {{
      map.removeLayer(flowLayer);
    }}
  }}
}}

function selectFlow(index) {{
  selectedFlow = index;
  // Update active state in list
  document.querySelectorAll('.flow-item').forEach((el, i) => {{
    el.classList.toggle('active', i === index);
  }});
  // Highlight the selected flow on the map
  // Reset all polylines to default style
  flowPolylines.forEach((p, i) => {{
    if (i === index) {{
      p.setStyle({{ color: '#dc2626', weight: Math.max(3, Math.log10(DATA.flows[i].trips) * 2.5), opacity: 0.9 }});
    }} else {{
      const flow = DATA.flows[i];
      const weight = Math.max(1, Math.log10(flow.trips) * 2);
      const opacity = Math.min(0.6, flow.trips / DATA.flows[0].trips);
      p.setStyle({{ color: '#9333ea', weight, opacity }});
    }}
  }});
  // Pan map to show the flow
  const flow = DATA.flows[index];
  const origin = DATA.kiosk_locations.find(k => k.id === flow.origin_id);
  const dest = DATA.kiosk_locations.find(k => k.id === flow.dest_id);
  if (origin && dest) {{
    const bounds = L.latLngBounds([origin.lat, origin.lng], [dest.lat, dest.lng]);
    map.fitBounds(bounds, {{ padding: [50, 50], maxZoom: 15 }});
  }}
  // Ensure flows are visible
  if (!showFlows) {{
    showFlows = true;
    document.getElementById("btn-flows").classList.add("active");
    map.addLayer(flowLayer);
  }}
}}

// Build flow list
function buildFlowList() {{
  const list = document.getElementById('flow-list');
  DATA.flows.forEach((flow, i) => {{
    const item = document.createElement('div');
    item.className = 'flow-item';
    item.onclick = () => selectFlow(i);
    const originShort = flow.origin.length > 18 ? flow.origin.slice(0, 16) + '…' : flow.origin;
    const destShort = flow.destination.length > 18 ? flow.destination.slice(0, 16) + '…' : flow.destination;
    item.innerHTML = `
      <span class="route">${{originShort}}<span class="arrow"> → </span>${{destShort}}</span>
      <span class="count">${{flow.trips.toLocaleString()}}</span>
    `;
    list.appendChild(item);
  }});
}}

buildFlowList();
updateMap();

Chart.defaults.color = getComputedStyle(document.documentElement).getPropertyValue("--text").trim();
Chart.defaults.borderColor = getComputedStyle(document.documentElement).getPropertyValue("--border").trim();

new Chart(document.getElementById("chart-yearly"), {{
  type: "bar",
  data: {{
    labels: Object.keys(DATA.totals.by_year),
    datasets: [{{
      label: "Trips",
      data: Object.values(DATA.totals.by_year),
      backgroundColor: "#2563eb80",
      borderColor: "#2563eb",
      borderWidth: 1,
    }}],
  }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    plugins: {{ legend: {{ display: false }} }},
    scales: {{ y: {{ beginAtZero: true, ticks: {{ callback: v => v.toLocaleString() }} }} }},
  }},
}});

const bikeYears = Object.keys(DATA.bike_types.by_year);
new Chart(document.getElementById("chart-bike-type"), {{
  type: "bar",
  data: {{
    labels: bikeYears,
    datasets: [
      {{
        label: "Classic",
        data: bikeYears.map(y => DATA.bike_types.by_year[y].classic || 0),
        backgroundColor: "#16a34a80",
        borderColor: "#16a34a",
        borderWidth: 1,
      }},
      {{
        label: "Electric",
        data: bikeYears.map(y => DATA.bike_types.by_year[y].electric || 0),
        backgroundColor: "#2563eb80",
        borderColor: "#2563eb",
        borderWidth: 1,
      }},
    ],
  }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    scales: {{
      x: {{ stacked: true }},
      y: {{ stacked: true, beginAtZero: true, ticks: {{ callback: v => v.toLocaleString() }} }},
    }},
  }},
}});

const topStations = [...DATA.kiosk_evolution].sort((a, b) => b.total_trips - a.total_trips).slice(0, 10);
new Chart(document.getElementById("chart-top-stations"), {{
  type: "bar",
  data: {{
    labels: topStations.map(k => k.name.length > 20 ? k.name.slice(0, 18) + "…" : k.name),
    datasets: [{{
      label: "Trips",
      data: topStations.map(k => k.total_trips),
      backgroundColor: topStations.map(k => k.active ? "#2563eb80" : "#ea580c80"),
      borderColor: topStations.map(k => k.active ? "#2563eb" : "#ea580c"),
      borderWidth: 1,
    }}],
  }},
  options: {{
    indexAxis: "y", responsive: true, maintainAspectRatio: false,
    plugins: {{ legend: {{ display: false }} }},
    scales: {{ x: {{ beginAtZero: true, ticks: {{ callback: v => v.toLocaleString() }} }} }},
  }},
}});

const topMemberships = DATA.memberships.types.slice(0, 8);
const colors = ["#2563eb","#16a34a","#ea580c","#9333ea","#0891b2","#d97706","#dc2626","#84cc16"];
new Chart(document.getElementById("chart-membership"), {{
  type: "doughnut",
  data: {{
    labels: topMemberships.map(m => m.type.length > 25 ? m.type.slice(0, 23) + "…" : m.type),
    datasets: [{{
      data: topMemberships.map(m => m.trips),
      backgroundColor: colors.slice(0, topMemberships.length),
    }}],
  }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    plugins: {{ legend: {{ position: "right", labels: {{ font: {{ size: 10 }} }} }} }},
  }},
}});
</script>
</body>
</html>'''


def main():
    logger.info("=" * 50)
    logger.info("Generating CapMetro / MetroBike data")
    logger.info("=" * 50)

    out_dir = Path(__file__).resolve().parent.parent / "docs" / "capmetro"
    out_dir.mkdir(parents=True, exist_ok=True)

    data = collect_data()

    data_path = out_dir / "data.json"
    data_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    logger.info(f"Wrote {data_path.stat().st_size:,} bytes to {data_path}")

    html = generate_html(data)
    html_path = out_dir / "index.html"
    html_path.write_text(html, encoding="utf-8")
    logger.info(f"Wrote {html_path.stat().st_size:,} bytes to {html_path}")

    logger.info("Done!")


if __name__ == "__main__":
    main()