"""APD Hate Crime Incidents — yearly trend, bias breakdown, and offender demographics."""

import io
import json
import logging
import os
from collections import Counter
from datetime import datetime, timezone
from typing import Optional

try:
    from zoneinfo import ZoneInfo
    _CENTRAL = ZoneInfo("America/Chicago")
except ImportError:
    _CENTRAL = None

import requests

logger = logging.getLogger(__name__)

SOCRATA_BASE = "https://data.austintexas.gov/resource"
HATE_CRIME_DATASET = "t99n-5ib4"

_session: Optional[requests.Session] = None


def _format_central_time() -> str:
    if _CENTRAL:
        dt = datetime.now(_CENTRAL)
        return dt.strftime("%Y-%m-%d %I:%M %p ") + dt.strftime("%Z")
    # Fallback without zoneinfo
    utc_now = datetime.now(timezone.utc)
    from datetime import timedelta
    is_dst = 3 <= utc_now.month <= 11
    central_now = utc_now + timedelta(hours=-5 if is_dst else -6)
    return central_now.strftime("%Y-%m-%d %I:%M %p ") + ("CDT" if is_dst else "CST")


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        # AUSTINAPIKEY is the Open311 key (NOT a Socrata token) —
        # sending them causes a 403. Only use a dedicated SOCRATA_APP_TOKEN if set.
        headers = {"Accept": "application/json", "User-Agent": "austin311bot/0.1 (hate crime)"}
        token = os.getenv("SOCRATA_APP_TOKEN", "")
        if token:
            headers["X-App-Token"] = token
        _session.headers.update(headers)
    return _session


# ── data fetching ──────────────────────────────────────────────────────────────

import urllib.parse


def _socrata_get(params: dict) -> list:
    """Make a GET request to Socrata, preserving special chars like (),* in values."""
    base = f"{SOCRATA_BASE}/{HATE_CRIME_DATASET}.json"
    # Build query string manually so () and * aren't encoded
    query_parts = []
    for key, value in params.items():
        encoded_key = urllib.parse.quote(key, safe="")
        encoded_value = urllib.parse.quote(str(value), safe="(),* ")
        # Replace encoded space with + (Socrata prefers this)
        encoded_value = encoded_value.replace("%20", "+")
        query_parts.append(f"{encoded_key}={encoded_value}")
    url = base + "?" + "&".join(query_parts)
    resp = _get_session().get(url, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _fetch_yearly() -> list:
    """Yearly incident counts."""
    return _socrata_get({
        "$select": "date_trunc_y(date_of_incident) as year, count(*) as cnt",
        "$group": "year",
        "$order": "year ASC",
        "$limit": 50,
    })


def _fetch_bias_breakdown() -> list:
    """Counts by bias category."""
    return _socrata_get({
        "$select": "bias, count(*) as cnt",
        "$group": "bias",
        "$order": "cnt DESC",
        "$limit": 50,
    })


def _fetch_offense_breakdown() -> list:
    """Counts by offense type."""
    return _socrata_get({
        "$select": "offense_s, count(*) as cnt",
        "$group": "offense_s",
        "$order": "cnt DESC",
        "$limit": 50,
    })


def _fetch_location_breakdown() -> list:
    """Counts by location type."""
    return _socrata_get({
        "$select": "offense_location, count(*) as cnt",
        "$group": "offense_location",
        "$order": "cnt DESC",
        "$limit": 50,
    })


def _fetch_offender_race() -> list:
    """Counts by offender race/ethnicity."""
    return _socrata_get({
        "$select": "race_ethnicity_of_offenders, count(*) as cnt",
        "$group": "race_ethnicity_of_offenders",
        "$order": "cnt DESC",
        "$limit": 50,
    })


# ── aggregation ────────────────────────────────────────────────────────────────

# Bias grouping for readable labels
# Aliases verified against actual t99n-5ib4 field values (May 2026)
BIAS_GROUPS = {
    "Anti-Black": [
        "Anti-Black or African American", "Anti-Black",
    ],
    "Anti-Gay (Male)": ["Anti-Gay (Male)", "Anti-Gay"],
    "Anti-Jewish": ["Anti-Jewish"],
    "Anti-Hispanic": [
        "Anti-Hispanic or Latino", "Anti-Hispanic", "Anti-Hispanic/Latino",
    ],
    "Anti-White": ["Anti-White"],
    "Anti-Transgender": ["Anti-Transgender"],
    "Anti-LGBTQ+ (Mixed)": [
        "Anti-Lesbian/Gay/Bisexual/Transgender (Mixed Group)",
        "Anti-Lesbian/Bisexual/Transgender (Mixed Group)",
        "Anti-Lesbian/Gay/Transgender",
    ],
    "Anti-Lesbian": ["Anti-Lesbian (Female)", "Anti-Lesbian"],
    "Anti-Asian": ["Anti-Asian"],
    "Anti-Muslim": ["Anti-Islamic (Muslim)", "Anti-Islamic(Muslim)", "Anti-Muslim"],
    "Anti-Arab": ["Anti-Arab"],
    "Anti-Female": ["Anti-Female"],
    "Anti-Bisexual": ["Anti-Bisexual"],
    "Anti-Religion (Other)": [
        "Anti-Other Religion", "Anti-Other Christian", "Anti-Buddhist",
        "Anti-Protestant", "Anti-Religion (Other)",
    ],
    "Anti-Disability": [
        "Anti-Mental Disability", "Anti-Physical Disability", "Anti-Disability",
    ],
    "Anti-Other Race/Ethnicity": [
        "Anti-Other Race/Ethnicity", "Anti-Other Race/Ethnicity/Ancestry",
        "Anti-American Indian/Alaskan Native",
    ],
    "Anti-Multiple Biases": [],  # catch-all for semicolon-joined multi-bias strings
}

BIAS_COLORS = {
    "Anti-Black": "#1a1a2e",
    "Anti-Gay (Male)": "#e91e63",
    "Anti-Jewish": "#1565c0",
    "Anti-Hispanic": "#ff6f00",
    "Anti-White": "#78909c",
    "Anti-Transgender": "#9c27b0",
    "Anti-LGBTQ+ (Mixed)": "#ad1457",
    "Anti-Lesbian": "#f06292",
    "Anti-Asian": "#00838f",
    "Anti-Muslim": "#2e7d32",
    "Anti-Arab": "#558b2f",
    "Anti-Female": "#c62828",
    "Anti-Bisexual": "#ce93d8",
    "Anti-Religion (Other)": "#4a148c",
    "Anti-Disability": "#6a1b9a",
    "Anti-Other Race/Ethnicity": "#546e7a",
    "Anti-Multiple Biases": "#37474f",
}

BIAS_ORDER = [
    "Anti-Black", "Anti-Gay (Male)", "Anti-Jewish", "Anti-Hispanic",
    "Anti-White", "Anti-Transgender", "Anti-LGBTQ+ (Mixed)", "Anti-Lesbian",
    "Anti-Asian", "Anti-Muslim", "Anti-Arab", "Anti-Female", "Anti-Bisexual",
    "Anti-Religion (Other)", "Anti-Disability", "Anti-Other Race/Ethnicity",
    "Anti-Multiple Biases",
]


def _group_bias(raw_bias: str) -> str:
    """Map a raw bias string to its group label. Semicolon-joined multi-bias → Anti-Multiple Biases."""
    if ";" in raw_bias:
        return "Anti-Multiple Biases"
    for group, aliases in BIAS_GROUPS.items():
        if raw_bias in aliases:
            return group
    return raw_bias


def _aggregate(yearly_rows: list, bias_rows: list, offense_rows: list,
               location_rows: list, offender_race_rows: list) -> dict:
    """Aggregate hate crime data into a structured dict."""
    # Yearly trend
    years = []
    yearly_counts = []
    for r in sorted(yearly_rows, key=lambda x: x.get("year", "")):
        year = (r.get("year") or "")[:4]
        if year:
            years.append(year)
            yearly_counts.append(int(r.get("cnt", 0)))

    total = sum(yearly_counts)
    avg_per_year = round(total / max(1, len(years)), 1)
    
    # Growth: compare first 3 years to last 3 years
    if len(yearly_counts) >= 6:
        early_avg = sum(yearly_counts[:3]) / 3
        late_avg = sum(yearly_counts[-3:]) / 3
        growth_pct = round(((late_avg - early_avg) / max(1, early_avg)) * 100)
    else:
        growth_pct = 0

    # Bias breakdown — grouped
    bias_counter = Counter()
    for r in bias_rows:
        raw_bias = r.get("bias", "")
        cnt = int(r.get("cnt", 0))
        group = _group_bias(raw_bias)
        bias_counter[group] += cnt

    bias_data = []
    for group in BIAS_ORDER:
        if group in bias_counter:
            bias_data.append({
                "name": group,
                "count": bias_counter[group],
                "color": BIAS_COLORS.get(group, "#94a3b8"),
            })
    # Any ungrouped biases
    for group, cnt in bias_counter.items():
        if group not in BIAS_ORDER:
            bias_data.append({
                "name": group,
                "count": cnt,
                "color": "#94a3b8",
            })
    bias_data.sort(key=lambda x: -x["count"])
    top_bias = bias_data[0]["name"] if bias_data else "—"

    # Offense breakdown
    offense_data = [
        {"name": (r.get("offense_s") or "").title(), "count": int(r.get("cnt", 0))}
        for r in offense_rows if r.get("offense_s")
    ][:15]

    # Location breakdown
    location_data = [
        {"name": (r.get("offense_location") or "").title(), "count": int(r.get("cnt", 0))}
        for r in location_rows if r.get("offense_location")
    ][:15]

    # Offender race breakdown (pre-grouped by API)
    offender_data = [
        {"name": (r.get("race_ethnicity_of_offenders") or "Unknown"), "count": int(r.get("cnt", 0))}
        for r in offender_race_rows if r.get("race_ethnicity_of_offenders")
    ]

    # Most recent year
    latest_year = years[-1] if years else "—"
    latest_count = yearly_counts[-1] if yearly_counts else 0

    return {
        "total": total,
        "years": years,
        "yearly_counts": yearly_counts,
        "avg_per_year": avg_per_year,
        "growth_pct": growth_pct,
        "latest_year": latest_year,
        "latest_count": latest_count,
        "top_bias": top_bias,
        "bias_data": bias_data,
        "offense_data": offense_data,
        "location_data": location_data,
        "offender_data": offender_data,
    }


# ── HTML ───────────────────────────────────────────────────────────────────────

def _render_html(data: dict, fetched_at: str) -> str:
    total       = data["total"]
    avg_yr      = data["avg_per_year"]
    growth_pct  = data["growth_pct"]
    latest_yr   = data["latest_year"]
    latest_cnt  = data["latest_count"]
    top_bias    = data["top_bias"]
    years       = data["years"]

    payload = json.dumps({
        "years":         years,
        "yearlyCounts":  data["yearly_counts"],
        "biasData":      data["bias_data"],
        "offenseData":   data["offense_data"],
        "locationData":  data["location_data"],
        "offenderData":  data["offender_data"],
    })

    bias_height = max(320, len(data["bias_data"]) * 36)
    offense_height = max(300, len(data["offense_data"]) * 28)
    location_height = max(300, len(data["location_data"]) * 28)

    growth_sign = "+" if growth_pct > 0 else ""
    growth_label = f"{growth_sign}{growth_pct}%"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
  <title>Austin 311 — Hate Crime Incidents</title>
  <script>if(localStorage.getItem("theme")==="dark")document.documentElement.classList.add("dark");</script>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    :root {{
      --bg: #f8fafc; --bg-panel: #f1f5f9; --bg-card: #ffffff;
      --border: #e2e8f0; --text: #1e293b; --text-head: #0f172a;
      --text-sub: #64748b; --text-muted: #94a3b8;
      --btn-bg: #e2e8f0; --btn-border: #cbd5e1; --btn-color: #475569;
      --btn-hover-bg: #d1dae3; --btn-hover-color: #1e293b;
      --chart-title: #374151; --footer-border: #e2e8f0; --footer-color: #94a3b8;
    }}
    html.dark {{
      --bg: #0f1117; --bg-panel: #1e2230; --bg-card: #161a24;
      --border: #2d3348; --text: #e2e8f0; --text-head: #f1f5f9;
      --text-sub: #64748b; --text-muted: #475569;
      --btn-bg: #252b3b; --btn-border: #3d4868; --btn-color: #94a3b8;
      --btn-hover-bg: #2d3453; --btn-hover-color: #e2e8f0;
      --chart-title: #e2e8f0; --footer-border: #1e2230; --footer-color: #475569;
    }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: var(--bg); color: var(--text); min-height: 100vh; display: flex; flex-direction: column; transition: background 0.2s, color 0.2s; }}
    #panel {{ position: sticky; top: 0; z-index: 100; background: var(--bg-panel); border-bottom: 1px solid var(--border); padding: 10px 16px 12px; display: flex; flex-direction: column; align-items: center; gap: 6px; }}
    #panel-title {{ font-size: 15px; font-weight: 700; color: var(--text-head); }}
    #panel-subtitle {{ font-size: 12px; color: var(--text-sub); text-align: center; }}
    #last-ran {{ font-size: 11px; color: var(--text-muted); }}
    .btn-row {{ display: flex; gap: 4px; flex-wrap: wrap; justify-content: center; }}
    .fbtn {{ background: var(--btn-bg); border: 1px solid var(--btn-border); color: var(--btn-color); padding: 5px 13px; border-radius: 4px; font-size: 12px; white-space: nowrap; text-decoration: none; display: inline-block; cursor: pointer; transition: background 0.12s, color 0.12s; }}
    .fbtn:hover {{ background: var(--btn-hover-bg); color: var(--btn-hover-color); }}
    #theme-toggle {{ position: fixed; top: 10px; right: 12px; z-index: 200; background: var(--bg-card); border: 1px solid var(--border); border-radius: 6px; padding: 4px 9px; font-size: 11px; color: var(--text-sub); cursor: pointer; }}
    #stats {{ border-bottom: 1px solid var(--border); }}
    .stats-inner {{ display: flex; justify-content: center; flex-wrap: wrap; }}
    .stat {{ flex: 1; min-width: 100px; max-width: 200px; text-align: center; padding: 10px 8px 9px; border-right: 1px solid var(--border); }}
    .stat:last-child {{ border-right: none; }}
    .stat-value {{ font-size: 1.2rem; font-weight: 700; line-height: 1.1; }}
    .stat-label {{ font-size: 0.67rem; color: var(--text-sub); text-transform: uppercase; letter-spacing: 0.05em; margin-top: 3px; }}
    .stat-sub {{ font-size: 0.67rem; color: var(--text-muted); margin-top: 1px; }}
    #chart-wrap {{ flex: 1; padding: 16px; display: flex; flex-direction: column; gap: 20px; max-width: 1100px; width: 100%; margin: 0 auto; }}
    .chart-block {{ background: var(--bg-card); border: 1px solid var(--border); border-radius: 8px; padding: 14px; }}
    .chart-title {{ font-size: 13px; font-weight: 600; color: var(--chart-title); margin-bottom: 4px; }}
    .chart-sub {{ font-size: 11px; color: var(--text-muted); margin-bottom: 10px; }}
    .chart-container {{ position: relative; }}
    .disclaimer {{ background: #fef3c7; border: 1px solid #f59e0b; border-radius: 8px; padding: 12px 16px; font-size: 12px; color: #92400e; line-height: 1.5; }}
    html.dark .disclaimer {{ background: #2d2a1a; border-color: #a16207; color: #fde68a; }}
    footer {{ text-align: center; padding: 14px 16px; font-size: 0.74rem; color: var(--footer-color); border-top: 1px solid var(--footer-border); }}
    footer a {{ color: var(--text-sub); text-decoration: none; }}
    footer a:hover {{ color: var(--text); }}
    @media (max-width: 520px) {{ .stat-value {{ font-size: 1rem; }} }}
  </style>
<!-- Google tag (gtag.js) -->
<script async src="https://www.googletagmanager.com/gtag/js?id=G-TS158R7XSN"></script>
<script>
  window.dataLayer = window.dataLayer || [];
  function gtag(){{dataLayer.push(arguments);}}
  gtag('js', new Date());
  gtag('config', 'G-TS158R7XSN');
</script>
</head>
<body>

  <button id="theme-toggle" onclick="toggleTheme()">🌙 Dark</button>

  <div id="panel">
    <div id="panel-title">⚠️ APD Hate Crime Incidents</div>
    <div id="panel-subtitle">Austin Police Department — 2017 to present</div>
    <div id="last-ran">Last ran: {fetched_at}</div>
    <div class="btn-row">
      <a class="fbtn" href="../">← Crime Map</a>
      <a class="fbtn" href="../trends/">← Crime Trends</a>
      <a class="fbtn" href="../../">Austin 311 Home</a>
    </div>
  </div>

  <div id="stats">
    <div class="stats-inner">
      <div class="stat">
        <div class="stat-value" style="color:#ef4444;">{total:,}</div>
        <div class="stat-label">Total incidents</div>
        <div class="stat-sub">2017–{latest_yr}</div>
      </div>
      <div class="stat">
        <div class="stat-value" style="color:#f59e0b;">{avg_yr:.0f}</div>
        <div class="stat-label">Avg per year</div>
      </div>
      <div class="stat">
        <div class="stat-value" style="color:#22c55e;">{latest_cnt:,}</div>
        <div class="stat-label">{latest_yr} incidents</div>
      </div>
      <div class="stat">
        <div class="stat-value" style="color:#3b82f6;font-size:0.95rem;">{growth_label}</div>
        <div class="stat-label">3-yr growth</div>
      </div>
      <div class="stat">
        <div class="stat-value" style="color:#8b5cf6;font-size:0.85rem;">{top_bias}</div>
        <div class="stat-label">Most targeted</div>
      </div>
    </div>
  </div>

  <div id="chart-wrap">

    <div class="disclaimer">
      <strong>⚠️ Note:</strong> Hate crimes are underreported. These figures reflect only incidents reported to and classified as hate crimes by APD.
      Actual incidence is likely higher. Data source: <a href="https://data.austintexas.gov/d/t99n-5ib4" target="_blank" rel="noopener" style="color:#92400e;">data.austintexas.gov</a>
    </div>

    <div class="chart-block">
      <div class="chart-title">📈 Yearly trend — hate crimes in Austin</div>
      <div class="chart-sub">2017 to present · {growth_label} growth (3-year avg comparison)</div>
      <div class="chart-container" style="height:300px;"><canvas id="yearlyChart"></canvas></div>
    </div>

    <div class="chart-block">
      <div class="chart-title">🎯 Bias / motivation</div>
      <div class="chart-sub">Which groups are most frequently targeted</div>
      <div class="chart-container" style="height:{bias_height}px;"><canvas id="biasChart"></canvas></div>
    </div>

    <div class="chart-block">
      <div class="chart-title">🔪 Offense type</div>
      <div class="chart-sub">Most common hate crime offenses</div>
      <div class="chart-container" style="height:{offense_height}px;"><canvas id="offenseChart"></canvas></div>
    </div>

    <div class="chart-block">
      <div class="chart-title">📍 Where hate crimes occur</div>
      <div class="chart-sub">Top location types</div>
      <div class="chart-container" style="height:{location_height}px;"><canvas id="locationChart"></canvas></div>
    </div>

    <div class="chart-block">
      <div class="chart-title">👤 Offender demographics</div>
      <div class="chart-sub">Race/ethnicity of offenders as reported by APD</div>
      <div class="chart-container" style="height:280px;"><canvas id="offenderChart"></canvas></div>
    </div>

  </div>

  <footer>
    Data: <a href="https://data.austintexas.gov/d/t99n-5ib4" target="_blank" rel="noopener">APD Hate Crime Incidents (Socrata · t99n-5ib4)</a>
    &nbsp;·&nbsp; <a href="../">← Crime Map</a>
    &nbsp;·&nbsp; <a href="../trends/">← Crime Trends</a>
    &nbsp;·&nbsp; <a href="../../">← Austin 311</a>
  </footer>

  <script>
    const DATA = {payload};

    const isDark   = document.documentElement.classList.contains("dark");
    const grid     = isDark ? "#252b3b" : "#e8ecf0";
    const tick     = isDark ? "#64748b" : "#6b7280";
    const leg      = isDark ? "#94a3b8" : "#4b5563";
    const tipBg    = isDark ? "#1e2230" : "#ffffff";
    const tipBdr   = isDark ? "#3d4868" : "#e2e8f0";
    const tipTitle = isDark ? "#f1f5f9" : "#111827";
    const tipBody  = isDark ? "#e2e8f0" : "#374151";

    const TIP = {{ backgroundColor:tipBg, borderColor:tipBdr, borderWidth:1, titleColor:tipTitle, bodyColor:tipBody }};

    const toggleBtn = document.getElementById("theme-toggle");
    toggleBtn.textContent = isDark ? "☀️ Light" : "🌙 Dark";
    function toggleTheme() {{
      const dark = document.documentElement.classList.toggle("dark");
      localStorage.setItem("theme", dark ? "dark" : "light");
      location.reload();
    }}

    const fmt = n => n >= 1000 ? (n / 1000).toFixed(1) + "k" : n.toLocaleString();
    const pct = (a, b) => b ? ((a / b) * 100).toFixed(1) + "%" : "—";

    // ── yearly bar chart ─────────────────────────────────────────────────────
    new Chart(document.getElementById("yearlyChart"), {{
      type: "bar",
      data: {{
        labels: DATA.years,
        datasets: [{{
          label: "Hate crime incidents",
          data: DATA.yearlyCounts,
          backgroundColor: DATA.yearlyCounts.map((v, i) =>
            i === DATA.yearlyCounts.length - 1 ? "#ef4444" : "#3b82f6"
          ),
          borderRadius: 4,
        }}],
      }},
      options: {{
        responsive: true, maintainAspectRatio: false,
        plugins: {{
          legend: {{ display: false }},
          tooltip: {{
            ...TIP,
            callbacks: {{
              label: ctx => ` ${{fmt(ctx.parsed.y)}} incidents`,
            }},
          }},
        }},
        scales: {{
          x: {{ ticks: {{ color: tick, font: {{ size: 11 }} }}, grid: {{ color: grid }} }},
          y: {{ ticks: {{ color: tick, font: {{ size: 11 }}, callback: v => fmt(v) }}, grid: {{ color: grid }}, beginAtZero: true }},
        }},
      }},
    }});

    // ── bias bar chart ───────────────────────────────────────────────────────
    new Chart(document.getElementById("biasChart"), {{
      type: "bar",
      data: {{
        labels: DATA.biasData.map(b => b.name),
        datasets: [{{
          data: DATA.biasData.map(b => b.count),
          backgroundColor: DATA.biasData.map(b => b.color),
          borderRadius: 4,
        }}],
      }},
      options: {{
        indexAxis: "y",
        responsive: true, maintainAspectRatio: false,
        plugins: {{
          legend: {{ display: false }},
          tooltip: {{
            ...TIP,
            callbacks: {{
              label: ctx => ` ${{fmt(ctx.parsed.x)}} incidents (${{pct(ctx.parsed.x, DATA.biasData.reduce((a,b)=>a+b.count,0))}})`,
            }},
          }},
        }},
        scales: {{
          x: {{ ticks: {{ color: tick, font: {{ size: 11 }}, callback: v => fmt(v) }}, grid: {{ color: grid }}, beginAtZero: true }},
          y: {{ ticks: {{ color: tick, font: {{ size: 11 }} }}, grid: {{ color: grid }} }},
        }},
      }},
    }});

    // ── offense bar chart ────────────────────────────────────────────────────
    new Chart(document.getElementById("offenseChart"), {{
      type: "bar",
      data: {{
        labels: DATA.offenseData.map(o => o.name),
        datasets: [{{
          data: DATA.offenseData.map(o => o.count),
          backgroundColor: "#f97316",
          borderRadius: 3,
        }}],
      }},
      options: {{
        indexAxis: "y",
        responsive: true, maintainAspectRatio: false,
        plugins: {{
          legend: {{ display: false }},
          tooltip: {{
            ...TIP,
            callbacks: {{
              label: ctx => ` ${{fmt(ctx.parsed.x)}} incidents (${{pct(ctx.parsed.x, DATA.offenseData.reduce((a,o)=>a+o.count,0))}})`,
            }},
          }},
        }},
        scales: {{
          x: {{ ticks: {{ color: tick, font: {{ size: 11 }}, callback: v => fmt(v) }}, grid: {{ color: grid }}, beginAtZero: true }},
          y: {{ ticks: {{ color: tick, font: {{ size: 11 }} }}, grid: {{ color: grid }} }},
        }},
      }},
    }});

    // ── location bar chart ───────────────────────────────────────────────────
    new Chart(document.getElementById("locationChart"), {{
      type: "bar",
      data: {{
        labels: DATA.locationData.map(l => l.name),
        datasets: [{{
          data: DATA.locationData.map(l => l.count),
          backgroundColor: "#8b5cf6",
          borderRadius: 3,
        }}],
      }},
      options: {{
        indexAxis: "y",
        responsive: true, maintainAspectRatio: false,
        plugins: {{
          legend: {{ display: false }},
          tooltip: {{
            ...TIP,
            callbacks: {{
              label: ctx => ` ${{fmt(ctx.parsed.x)}} incidents (${{pct(ctx.parsed.x, DATA.locationData.reduce((a,l)=>a+l.count,0))}})`,
            }},
          }},
        }},
        scales: {{
          x: {{ ticks: {{ color: tick, font: {{ size: 11 }}, callback: v => fmt(v) }}, grid: {{ color: grid }}, beginAtZero: true }},
          y: {{ ticks: {{ color: tick, font: {{ size: 11 }} }}, grid: {{ color: grid }} }},
        }},
      }},
    }});

    // ── offender race bar chart ──────────────────────────────────────────────
    const offTotal = DATA.offenderData.reduce((a, o) => a + o.count, 0);
    const offColors = ["#64748b", "#3b82f6", "#ef4444", "#22c55e", "#f59e0b", "#8b5cf6", "#06b6d4"];
    new Chart(document.getElementById("offenderChart"), {{
      type: "bar",
      data: {{
        labels: DATA.offenderData.map(o => o.name),
        datasets: [{{
          data: DATA.offenderData.map(o => o.count),
          backgroundColor: DATA.offenderData.map((_, i) => offColors[i % offColors.length]),
          borderRadius: 3,
        }}],
      }},
      options: {{
        indexAxis: "y",
        responsive: true, maintainAspectRatio: false,
        plugins: {{
          legend: {{ display: false }},
          tooltip: {{
            ...TIP,
            callbacks: {{
              label: ctx => ` ${{fmt(ctx.parsed.x)}} offenders (${{pct(ctx.parsed.x, offTotal)}})`,
            }},
          }},
        }},
        scales: {{
          x: {{ ticks: {{ color: tick, font: {{ size: 11 }}, callback: v => fmt(v) }}, grid: {{ color: grid }}, beginAtZero: true }},
          y: {{ ticks: {{ color: tick, font: {{ size: 11 }} }}, grid: {{ color: grid }} }},
        }},
      }},
    }});
  </script>
</body>
</html>
"""


# ── entry point ────────────────────────────────────────────────────────────────

def generate_hate_crime() -> tuple[Optional[io.BytesIO], str]:
    try:
        yearly_rows       = _fetch_yearly()
        bias_rows         = _fetch_bias_breakdown()
        offense_rows      = _fetch_offense_breakdown()
        location_rows     = _fetch_location_breakdown()
        offender_race_rows = _fetch_offender_race()
    except Exception as e:
        logger.error(f"hate crime fetch: {e}")
        return None, f"⚠️ Error fetching hate crime data: {e}"

    if not yearly_rows:
        return None, "⚠️ No hate crime data found."

    data = _aggregate(yearly_rows, bias_rows, offense_rows, location_rows, offender_race_rows)
    fetched_at = _format_central_time()
    html = _render_html(data, fetched_at)

    buf = io.BytesIO(html.encode("utf-8"))
    buf.seek(0)
    summary = f"⚠️ *Hate Crime Incidents*\n_{data['total']:,} total · {data['top_bias']} most targeted · {data['growth_pct']:+d}% 3-yr growth_"
    return buf, summary