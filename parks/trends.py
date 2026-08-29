"""
Parks Maintenance Trends — aggregates park-related 311 reports over time.

Tracks monthly volume across all 9 park service codes, split into
Grounds (outdoor) and Buildings (indoor facility) buckets.
"""

import io
import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional


def _format_central_time() -> str:
    """Return current time formatted in US Central Time (CDT/CST)."""
    utc_now = datetime.now(timezone.utc)
    month = utc_now.month
    is_dst = 3 <= month <= 11
    offset_hours = -5 if is_dst else -6
    central_now = utc_now + timedelta(hours=offset_hours)
    tz_abbr = "CDT" if is_dst else "CST"
    return central_now.strftime(f"%Y-%m-%d %I:%M %p {tz_abbr}")


logger = logging.getLogger(__name__)

LOOKBACK_DAYS = 365
TOP_TYPES = 9

# Grounds codes = outdoor / greenspace; Buildings codes = indoor facility
GROUNDS_LABELS = {
    "Grounds Maintenance",
    "Grounds Plumbing",
    "Grounds Electrical",
    "Commercial Use of Parkland",
    "Park Cemeteries",
}
BUILDINGS_LABELS = {
    "Building Plumbing",
    "Building Issues",
    "Building A/C & Heating",
    "Building Electric",
}


def _aggregate(records: list) -> dict:
    """Bucket records by month and issue type."""
    monthly: dict = defaultdict(int)
    monthly_open: dict = defaultdict(int)
    monthly_grounds: dict = defaultdict(int)
    monthly_buildings: dict = defaultdict(int)
    by_type: dict = defaultdict(int)
    total = 0

    for r in records:
        ts = r.get("requested_datetime") or ""
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            continue

        month_key = dt.strftime("%Y-%m")
        monthly[month_key] += 1
        is_open = (r.get("status") or "").lower() == "open"
        if is_open:
            monthly_open[month_key] += 1

        label = r.get("_service_label") or "Unknown"
        by_type[label] += 1

        if label in GROUNDS_LABELS:
            monthly_grounds[month_key] += 1
        elif label in BUILDINGS_LABELS:
            monthly_buildings[month_key] += 1

        total += 1

    months_sorted = sorted(monthly.keys())
    counts = [monthly[m] for m in months_sorted]

    # 3-month rolling average
    window = 3
    rolling: list = []
    for i in range(len(counts)):
        if i < window - 1:
            rolling.append(None)
        else:
            rolling.append(round(sum(counts[i - window + 1 : i + 1]) / window, 1))

    top_types = sorted(by_type.items(), key=lambda x: -x[1])[:TOP_TYPES]

    return {
        "total": total,
        "months": months_sorted,
        "monthly_counts": counts,
        "monthly_open_counts": [monthly_open[m] for m in months_sorted],
        "monthly_grounds": [monthly_grounds[m] for m in months_sorted],
        "monthly_buildings": [monthly_buildings[m] for m in months_sorted],
        "rolling_avg": rolling,
        "top_types": top_types,
    }


def _render_html(data: dict, fetched_at: str) -> str:
    total = data["total"]
    months = data["months"]
    monthly_counts = data["monthly_counts"]
    monthly_open_counts = data["monthly_open_counts"]
    top_types = data["top_types"]

    avg_per_month = round(total / max(1, len(months)), 0) if months else 0
    total_open = sum(monthly_open_counts)
    pct_open = round(total_open / max(1, total) * 100)

    month_labels = [datetime.strptime(m, "%Y-%m").strftime("%b %Y") for m in months]

    payload = {
        "months": month_labels,
        "monthlyCounts": monthly_counts,
        "monthlyOpenCounts": monthly_open_counts,
        "monthlyGrounds": data["monthly_grounds"],
        "monthlyBuildings": data["monthly_buildings"],
        "rollingAvg": data["rolling_avg"],
        "types": [{"name": t, "count": c} for t, c in top_types],
    }
    payload_json = json.dumps(payload)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
  <meta name="google" content="notranslate" />
  <title>Austin 311 — Parks Maintenance Trends</title>
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
      --btn-active-bg: #3b82f6; --btn-active-color: #fff;
      --chart-title: #374151; --footer-border: #e2e8f0; --footer-color: #94a3b8;
    }}
    html.dark {{
      --bg: #0f1117; --bg-panel: #1e2230; --bg-card: #161a24;
      --border: #2d3348; --text: #e2e8f0; --text-head: #f1f5f9;
      --text-sub: #64748b; --text-muted: #475569;
      --btn-bg: #252b3b; --btn-border: #3d4868; --btn-color: #94a3b8;
      --btn-hover-bg: #2d3453; --btn-hover-color: #e2e8f0;
      --btn-active-bg: #3b82f6; --btn-active-color: #fff;
      --chart-title: #e2e8f0; --footer-border: #1e2230; --footer-color: #475569;
    }}

    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg); color: var(--text);
      min-height: 100vh; display: flex; flex-direction: column; transition: background 0.2s, color 0.2s;
    }}
    #panel {{
      position: sticky; top: 0; z-index: 100;
      background: var(--bg-panel); border-bottom: 1px solid var(--border);
      padding: 10px 16px 12px;
      display: flex; flex-direction: column; align-items: center; gap: 6px;
    }}
    #panel-title {{ font-size: 15px; font-weight: 700; color: var(--text-head); }}
    #panel-subtitle {{ font-size: 12px; color: var(--text-sub); text-align: center; }}
    #last-ran {{ font-size: 11px; color: var(--text-muted); }}
    .btn-row {{ display: flex; gap: 4px; flex-wrap: wrap; justify-content: center; }}
    .fbtn {{
      background: var(--btn-bg); border: 1px solid var(--btn-border); color: var(--btn-color);
      padding: 5px 13px; border-radius: 4px; font-size: 12px; cursor: pointer;
      transition: background 0.12s, color 0.12s;
      white-space: nowrap; text-decoration: none; display: inline-block;
    }}
    .fbtn:hover {{ background: var(--btn-hover-bg); color: var(--btn-hover-color); }}
    .fbtn.active {{ background: var(--btn-active-bg); border-color: var(--btn-active-bg); color: var(--btn-active-color); font-weight: 600; }}
    #theme-toggle {{
      position: fixed; top: 10px; right: 12px; z-index: 200;
      background: var(--bg-card); border: 1px solid var(--border);
      border-radius: 6px; padding: 4px 9px; font-size: 11px; color: var(--text-sub); cursor: pointer;
    }}

    #stats {{ border-bottom: 1px solid var(--border); }}
    .stats-inner {{ display: flex; justify-content: center; }}
    .stat {{
      flex: 1; max-width: 170px; text-align: center;
      padding: 10px 8px 9px; border-right: 1px solid var(--border);
    }}
    .stat:last-child {{ border-right: none; }}
    .stat-value {{ font-size: 1.25rem; font-weight: 700; line-height: 1.1; }}
    .stat-label {{ font-size: 0.67rem; color: var(--text-sub); text-transform: uppercase; letter-spacing: 0.05em; margin-top: 3px; }}
    .stat-sub   {{ font-size: 0.67rem; color: var(--text-muted); margin-top: 1px; }}

    #chart-wrap {{ flex: 1; padding: 16px; display: flex; flex-direction: column; gap: 20px; max-width: 1100px; width: 100%; margin: 0 auto; }}
    .chart-block {{ background: var(--bg-card); border: 1px solid var(--border); border-radius: 8px; padding: 14px; }}
    .chart-title {{ font-size: 13px; font-weight: 600; color: var(--chart-title); margin-bottom: 10px; }}
    .chart-container {{ position: relative; height: 320px; }}

    footer {{
      text-align: center; padding: 14px 16px;
      font-size: 0.74rem; color: var(--footer-color); border-top: 1px solid var(--footer-border);
    }}
    footer a {{ color: var(--text-sub); text-decoration: none; }}
    footer a:hover {{ color: var(--text); }}
    @media (max-width: 520px) {{ .stat-value {{ font-size: 1rem; }} .chart-container {{ height: 260px; }} }}
  </style>
</head>
<body>

  <button id="theme-toggle" onclick="toggleTheme()">🌙 Dark</button>

  <div id="panel">
    <div id="panel-title">🏞️ Austin Parks Maintenance Trends</div>
    <div id="panel-subtitle">Park maintenance 311 requests — last 365 days</div>
    <div id="last-ran">Last ran: {fetched_at}</div>
    <div class="btn-row">
      <a class="fbtn" href="../">← Parks Map</a>
      <a class="fbtn" href="../../">Austin 311 Home</a>
    </div>
  </div>

  <div id="stats">
    <div class="stats-inner">
      <div class="stat">
        <div class="stat-value" style="color:#3b82f6;">{total:,}</div>
        <div class="stat-label">Total reports</div>
        <div class="stat-sub">last 365 days</div>
      </div>
      <div class="stat">
        <div class="stat-value" style="color:#22c55e;">{int(avg_per_month):,}</div>
        <div class="stat-label">Avg / month</div>
      </div>
      <div class="stat">
        <div class="stat-value" style="color:#ef4444;">{pct_open}%</div>
        <div class="stat-label">Still open</div>
        <div class="stat-sub">{total_open:,} unresolved</div>
      </div>
    </div>
  </div>

  <div id="chart-wrap">
    <div class="chart-block">
      <div class="chart-title">Reports per month — total, grounds, buildings &amp; 3-month avg</div>
      <div class="chart-container"><canvas id="monthlyChart"></canvas></div>
    </div>

    <div class="chart-block">
      <div class="chart-title">Top {TOP_TYPES} issue types</div>
      <div class="chart-container" style="height: {max(280, len(top_types) * 34)}px;"><canvas id="typesChart"></canvas></div>
    </div>
  </div>

  <footer>
    Data: <a href="https://311.austintexas.gov/open311/v2" target="_blank" rel="noopener">Austin Open311</a>
    &nbsp;·&nbsp;
    <a href="../">← Parks Map</a>
    &nbsp;·&nbsp;
    <a href="../../">← Austin 311</a>
  </footer>

  <script>
    const DATA = {payload_json};

    const isDark = document.documentElement.classList.contains("dark");
    const gridColor  = isDark ? "#252b3b" : "#e8ecf0";
    const tickColor  = isDark ? "#64748b" : "#6b7280";
    const legColor   = isDark ? "#94a3b8" : "#4b5563";
    const TOOLTIP = {{
      backgroundColor: isDark ? "#1e2230" : "#ffffff",
      borderColor:     isDark ? "#3d4868" : "#e2e8f0",
      borderWidth: 1,
      titleColor: isDark ? "#f1f5f9" : "#111827",
      bodyColor:  isDark ? "#e2e8f0"  : "#374151",
    }};
    const TICK_X = {{ color: tickColor, font: {{ size: 11 }} }};
    const TICK_Y = {{ color: tickColor, font: {{ size: 11 }} }};
    const GRID = {{ color: gridColor }};

    const lineOpts = {{
      plugins: {{
        legend: {{ labels: {{ color: legColor, font: {{ size: 11 }} }} }},
        tooltip: TOOLTIP,
      }},
      scales: {{
        x: {{ ticks: TICK_X, grid: GRID }},
        y: {{ ticks: TICK_Y, grid: GRID, beginAtZero: true }},
      }},
      responsive: true,
      maintainAspectRatio: false,
    }};

    const hBarOpts = {{
      indexAxis: "y",
      plugins: {{
        legend: {{ display: false }},
        tooltip: TOOLTIP,
      }},
      scales: {{
        x: {{ ticks: TICK_X, grid: GRID, beginAtZero: true }},
        y: {{ ticks: TICK_Y, grid: GRID }},
      }},
      responsive: true,
      maintainAspectRatio: false,
    }};

    const toggleBtn = document.getElementById("theme-toggle");
    toggleBtn.textContent = isDark ? "☀️ Light" : "🌙 Dark";
    function toggleTheme() {{
      const dark = document.documentElement.classList.toggle("dark");
      localStorage.setItem("theme", dark ? "dark" : "light");
      location.reload();
    }}

    // Monthly trend — total, grounds, buildings, 3-month avg
    new Chart(document.getElementById("monthlyChart"), {{
      type: "line",
      data: {{
        labels: DATA.months,
        datasets: [
          {{
            label: "Total reports",
            data: DATA.monthlyCounts,
            borderColor: "#3b82f6",
            backgroundColor: "rgba(59,130,246,0.08)",
            fill: true,
            tension: 0.3,
            pointRadius: 3,
            pointHoverRadius: 5,
          }},
          {{
            label: "Grounds",
            data: DATA.monthlyGrounds,
            borderColor: "#22c55e",
            backgroundColor: "rgba(34,197,94,0.06)",
            fill: false,
            tension: 0.3,
            pointRadius: 2,
          }},
          {{
            label: "Buildings",
            data: DATA.monthlyBuildings,
            borderColor: "#f59e0b",
            backgroundColor: "rgba(245,158,11,0.06)",
            fill: false,
            tension: 0.3,
            pointRadius: 2,
          }},
          {{
            label: "3-month avg",
            data: DATA.rollingAvg,
            borderColor: "#8b5cf6",
            borderWidth: 2,
            borderDash: [5, 3],
            pointRadius: 0,
            tension: 0.4,
            fill: false,
            spanGaps: true,
          }},
        ],
      }},
      options: lineOpts,
    }});

    // Top issue types — horizontal bar
    new Chart(document.getElementById("typesChart"), {{
      type: "bar",
      data: {{
        labels: DATA.types.map(t => t.name),
        datasets: [{{
          label: "Reports",
          data: DATA.types.map(t => t.count),
          backgroundColor: "#22c55e",
          borderRadius: 4,
        }}],
      }},
      options: hBarOpts,
    }});
  </script>
</body>
</html>
"""


def generate_parks_trends(days_back: int = LOOKBACK_DAYS) -> tuple[Optional[io.BytesIO], str]:
    """Generate the parks maintenance trends HTML page.

    Returns (BytesIO buffer, summary string) — matches the signature used by
    scripts/generate_map.py for consistency.
    """
    from parks.parks_bot import fetch_parks_monthly

    months_back = max(1, days_back // 30) + 1
    # Bypass the SQLite cache here: it is incremental and can be missing entire
    # months (this page undercounted Nov 2025–Jan 2026 by ~5x). Always fetch the
    # full window so the chart reflects complete data.
    records = fetch_parks_monthly(months_back, use_cache=False)
    if not records:
        return None, f"🏞️ No park maintenance data found for last {days_back} days."

    data = _aggregate(records)
    fetched_at = _format_central_time()
    html = _render_html(data, fetched_at)

    import os
    out_path = os.path.join(os.path.dirname(__file__), "..", "docs", "parks", "trends", "index.html")
    out_path = os.path.normpath(out_path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    buf = io.BytesIO(html.encode("utf-8"))
    buf.seek(0)

    summary = (
        f"🏞️ *Parks Maintenance Trends*\n"
        f"_Last {days_back} days · {data['total']:,} reports across "
        f"{len(data['months'])} months_"
    )
    return buf, summary
