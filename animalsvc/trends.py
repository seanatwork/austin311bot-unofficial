"""
Animal Services Trends — aggregates animal-related 311 reports over time.

Tracks monthly complaint volume across all 7 animal service codes, with
breakdown by incident type and 3-month rolling average.
"""

import io
import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from animalsvc.animal_bot import SERVICE_CODES, _extract_street


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
TOP_STREETS = 5


def _aggregate(records: list) -> dict:
    """Bucket records by month and incident type."""
    monthly: dict = defaultdict(int)
    monthly_open: dict = defaultdict(int)
    by_type: dict = defaultdict(int)
    monthly_by_type: dict = defaultdict(lambda: defaultdict(int))
    type_streets: dict = defaultdict(lambda: defaultdict(int))
    type_open: dict = defaultdict(int)
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

        label = (
            r.get("_service_label")
            or SERVICE_CODES.get(r.get("service_code"))
            or r.get("service_name")
            or "Unknown"
        )
        by_type[label] += 1
        monthly_by_type[label][month_key] += 1
        if is_open:
            type_open[label] += 1

        street = _extract_street((r.get("address") or "").strip())
        if street:
            type_streets[label][street] += 1

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

    # Sort all incident types by count descending
    top_types = sorted(by_type.items(), key=lambda x: -x[1])

    # Per-type monthly series aligned to months_sorted (same order as top_types)
    type_monthly = [
        {"name": label, "data": [monthly_by_type[label].get(m, 0) for m in months_sorted]}
        for label, _ in top_types
    ]

    # Per-type drill-down detail: top streets, monthly series, open/closed split
    type_details: dict = {}
    for label, count in top_types:
        streets = sorted(type_streets[label].items(), key=lambda x: -x[1])[:TOP_STREETS]
        type_details[label] = {
            "streets": [{"name": s, "count": c} for s, c in streets],
            "monthly": [monthly_by_type[label].get(m, 0) for m in months_sorted],
            "open": type_open[label],
            "closed": count - type_open[label],
        }

    return {
        "total": total,
        "months": months_sorted,
        "monthly_counts": counts,
        "monthly_open_counts": [monthly_open[m] for m in months_sorted],
        "rolling_avg": rolling,
        "top_types": top_types,
        "type_monthly": type_monthly,
        "type_details": type_details,
    }


def _render_html(data: dict, fetched_at: str) -> str:
    total = data["total"]
    months = data["months"]
    monthly_counts = data["monthly_counts"]
    monthly_open_counts = data["monthly_open_counts"]
    top_types = data["top_types"]
    type_monthly = data.get("type_monthly", [])

    top_type_name = top_types[0][0] if top_types else "—"
    top_type_count = top_types[0][1] if top_types else 0

    avg_per_month = round(total / max(1, len(months)), 0) if months else 0
    total_open = sum(monthly_open_counts)
    pct_open = round(total_open / max(1, total) * 100)

    month_labels = [datetime.strptime(m, "%Y-%m").strftime("%b %Y") for m in months]

    payload = {
        "months": month_labels,
        "monthlyCounts": monthly_counts,
        "monthlyOpenCounts": monthly_open_counts,
        "rollingAvg": data["rolling_avg"],
        "types": [{"name": t, "count": c} for t, c in top_types],
        "typeMonthly": type_monthly,
        "typeDetails": data.get("type_details", {}),
    }
    payload_json = json.dumps(payload)

    type_count = len(top_types)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
  <meta name="google" content="notranslate" />
  <title>Austin 311 — Animal Services Trends</title>
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

    .chart-hint {{ font-size: 11px; color: var(--text-muted); margin-bottom: 8px; font-style: italic; }}

    #type-detail {{
      display: none;
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-top: 3px solid #f59e0b;
      border-radius: 0 0 8px 8px;
      padding: 16px;
      margin-top: -9px;
    }}
    #td-header {{
      display: flex; align-items: center; justify-content: space-between;
      margin-bottom: 12px;
    }}
    #td-title {{ font-size: 15px; font-weight: 700; color: var(--text-head); }}
    #td-close {{
      background: none; border: 1px solid var(--border); border-radius: 4px;
      color: var(--text-muted); cursor: pointer; font-size: 13px; padding: 2px 8px;
      line-height: 1.4;
    }}
    #td-close:hover {{ color: var(--text); border-color: #94a3b8; }}
    .vd-section {{ margin-bottom: 14px; }}
    .vd-section-title {{ font-size: 11px; font-weight: 600; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 8px; }}
    .vd-stat-row {{ display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 14px; }}
    .vd-stat {{ background: var(--bg-panel, #f1f5f9); border-radius: 6px; padding: 8px 12px; text-align: center; min-width: 80px; }}
    .vd-stat .v {{ font-size: 1.2rem; font-weight: 700; color: var(--text-head); }}
    .vd-stat .l {{ font-size: 0.67rem; color: var(--text-sub); text-transform: uppercase; letter-spacing: 0.04em; margin-top: 2px; }}
    .mini-bar-row {{ display: flex; align-items: center; gap: 8px; margin-bottom: 5px; font-size: 11px; }}
    .mini-bar-label {{ width: 160px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; color: var(--text); }}
    .mini-bar-track {{ flex: 1; background: var(--border); border-radius: 2px; height: 9px; }}
    .mini-bar-fill {{ height: 100%; border-radius: 2px; }}
    .mini-bar-count {{ width: 34px; text-align: right; color: var(--text-sub); }}
    .month-bar-row {{ display: flex; align-items: center; gap: 6px; margin-bottom: 4px; font-size: 10px; }}
    .month-bar-label {{ width: 32px; color: var(--text-muted); }}
    .month-bar-track {{ flex: 1; background: var(--border); border-radius: 2px; height: 8px; }}
    .month-bar-fill {{ height: 100%; border-radius: 2px; background: #f59e0b; }}
    .month-bar-count {{ width: 30px; text-align: right; color: var(--text-sub); }}
    #td-map-btn {{
      display: inline-flex; align-items: center; gap: 6px;
      background: #2563eb; color: #fff; border: none; border-radius: 5px;
      padding: 7px 14px; font-size: 12px; font-weight: 600; cursor: pointer;
      text-decoration: none; margin-top: 4px;
    }}
    #td-map-btn:hover {{ background: #1d4ed8; }}
  </style>
</head>
<body>

  <button id="theme-toggle" onclick="toggleTheme()">🌙 Dark</button>

  <div id="panel">
    <div id="panel-title">🐾 Austin Animal Services Trends</div>
    <div id="panel-subtitle">Animal complaint &amp; incident reports — last 12 months</div>
    <div id="last-ran">Last ran: {fetched_at}</div>
    <div class="btn-row">
      <a class="fbtn" href="../">← Animal Map</a>
      <a class="fbtn" href="../../">Austin 311 Home</a>
    </div>
  </div>

  <div id="stats">
    <div class="stats-inner">
      <div class="stat">
        <div class="stat-value" style="color:#3b82f6;">{total:,}</div>
        <div class="stat-label">Total reports</div>
        <div class="stat-sub">last 12 months</div>
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
      <div class="stat">
        <div class="stat-value" style="color:#f59e0b;">{top_type_count:,}</div>
        <div class="stat-label">Top type</div>
        <div class="stat-sub">{top_type_name}</div>
      </div>
    </div>
  </div>

  <div id="chart-wrap">
    <div class="chart-block">
      <div class="chart-title">Reports per month — total, open &amp; 3-month avg</div>
      <div class="chart-container"><canvas id="monthlyChart"></canvas></div>
    </div>

    <div class="chart-block">
      <div class="chart-title">Reports by incident type per month — loose dogs, bites, wildlife, etc.</div>
      <div class="chart-container" style="height: 400px;"><canvas id="typeMonthlyChart"></canvas></div>
    </div>

    <div class="chart-block" id="types-block">
      <div class="chart-title">Breakdown by incident type</div>
      <div class="chart-hint">Click a bar to explore that incident type ↓</div>
      <div class="chart-container" style="height: {max(280, type_count * 38)}px;"><canvas id="typesChart"></canvas></div>
    </div>

    <div id="type-detail">
      <div id="td-header">
        <span id="td-title"></span>
        <button id="td-close" onclick="closeTypeDetail()">✕ Close</button>
      </div>
      <div class="vd-stat-row" id="td-stats"></div>
      <div class="vd-section">
        <div class="vd-section-title">Top streets</div>
        <div id="td-streets"></div>
      </div>
      <div class="vd-section">
        <div class="vd-section-title">Monthly breakdown</div>
        <div id="td-trend"></div>
      </div>
      <a id="td-map-btn" href="../" target="_blank">🗺️ View on animal map →</a>
    </div>
  </div>

  <footer>
    Data: <a href="https://311.austintexas.gov/open311/v2" target="_blank" rel="noopener">Austin Open311</a>
    (ACLONAG, ACLOANIM, ACBITE2, COAACDD, ACPROPER, WILDEXPO, ACINFORM)
    &nbsp;·&nbsp;
    <a href="../">← Animal Map</a>
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

    // Monthly trend — total, open, 3-month avg
    new Chart(document.getElementById("monthlyChart"), {{
      type: "line",
      data: {{
        labels: DATA.months,
        datasets: [
          {{
            label: "Monthly reports",
            data: DATA.monthlyCounts,
            borderColor: "#3b82f6",
            backgroundColor: "rgba(59,130,246,0.08)",
            fill: true,
            tension: 0.3,
            pointRadius: 3,
            pointHoverRadius: 5,
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
          {{
            label: "Still open",
            data: DATA.monthlyOpenCounts,
            borderColor: "#ef4444",
            backgroundColor: "rgba(239,68,68,0.05)",
            fill: true,
            tension: 0.3,
            pointRadius: 2,
          }},
        ],
      }},
      options: lineOpts,
    }});

    // Per-type monthly trends — one line per incident type (click legend to isolate)
    const TYPE_COLORS = ["#dc2626", "#9333ea", "#f97316", "#3b82f6", "#0ea5e9", "#84cc16", "#64748b"];
    new Chart(document.getElementById("typeMonthlyChart"), {{
      type: "line",
      data: {{
        labels: DATA.months,
        datasets: DATA.typeMonthly.map((t, i) => ({{
          label: t.name,
          data: t.data,
          borderColor: TYPE_COLORS[i % TYPE_COLORS.length],
          backgroundColor: TYPE_COLORS[i % TYPE_COLORS.length],
          borderWidth: 2,
          pointRadius: 2,
          pointHoverRadius: 4,
          tension: 0.3,
          fill: false,
        }})),
      }},
      options: {{
        ...lineOpts,
        plugins: {{
          legend: {{ labels: {{ color: legColor, font: {{ size: 10 }} }} }},
          tooltip: TOOLTIP,
        }},
      }},
    }});

    // Incident type breakdown — horizontal bar, click to drill down
    let selectedTypeIdx = null;
    const typesChart = new Chart(document.getElementById("typesChart"), {{
      type: "bar",
      data: {{
        labels: DATA.types.map(t => t.name),
        datasets: [{{
          label: "Reports",
          data: DATA.types.map(t => t.count),
          backgroundColor: DATA.types.map(() => "#f59e0b"),
          borderRadius: 4,
        }}],
      }},
      options: {{
        ...hBarOpts,
        onClick(evt, elements) {{
          if (!elements.length) return;
          const idx = elements[0].index;
          if (idx === selectedTypeIdx) {{
            closeTypeDetail();
          }} else {{
            openTypeDetail(idx);
          }}
        }},
        onHover(evt, elements) {{
          evt.native.target.style.cursor = elements.length ? "pointer" : "default";
        }},
      }},
    }});

    function openTypeDetail(idx) {{
      selectedTypeIdx = idx;
      const t = DATA.types[idx];
      const detail = DATA.typeDetails[t.name] || {{}};
      const total = DATA.types.reduce((s, x) => s + x.count, 0);
      const pct = total ? Math.round(t.count / total * 100) : 0;

      // Highlight selected bar, dim others
      typesChart.data.datasets[0].backgroundColor =
        DATA.types.map((_, i) => i === idx ? "#f59e0b" : "rgba(245,158,11,0.3)");
      typesChart.update("none");

      document.getElementById("td-title").textContent = t.name + " reports";

      const openCount = detail.open || 0;
      const closedCount = detail.closed || 0;
      document.getElementById("td-stats").innerHTML = `
        <div class="vd-stat"><div class="v">${{t.count.toLocaleString()}}</div><div class="l">Reports</div></div>
        <div class="vd-stat"><div class="v">${{pct}}%</div><div class="l">of all reports</div></div>
        <div class="vd-stat"><div class="v" style="color:#ef4444">${{openCount}}</div><div class="l">Still open</div></div>
        <div class="vd-stat"><div class="v" style="color:#22c55e">${{closedCount}}</div><div class="l">Resolved</div></div>
      `;

      // Top streets
      const streets = detail.streets || [];
      const maxStreet = streets.length ? streets[0].count : 1;
      document.getElementById("td-streets").innerHTML = streets.length
        ? streets.map(s => `
            <div class="mini-bar-row">
              <span class="mini-bar-label" title="${{s.name}}">${{s.name}}</span>
              <div class="mini-bar-track"><div class="mini-bar-fill" style="width:${{Math.round(s.count/maxStreet*100)}}%;background:#8b5cf6;"></div></div>
              <span class="mini-bar-count">${{s.count}}</span>
            </div>`).join("")
        : '<span style="font-size:12px;color:var(--text-muted);">No street data available</span>';

      // Monthly trend
      const monthly = detail.monthly || [];
      const maxMonth = Math.max(...monthly, 1);
      document.getElementById("td-trend").innerHTML = monthly.map((cnt, i) => `
        <div class="month-bar-row">
          <span class="month-bar-label">${{DATA.months[i].split(" ")[0]}}</span>
          <div class="month-bar-track"><div class="month-bar-fill" style="width:${{Math.round(cnt/maxMonth*100)}}%;"></div></div>
          <span class="month-bar-count">${{cnt}}</span>
        </div>`).join("");

      // Map link — passes the type as a URL param (map reads ?type=)
      document.getElementById("td-map-btn").href = "../?type=" + encodeURIComponent(t.name);

      document.getElementById("type-detail").style.display = "block";
      document.getElementById("type-detail").scrollIntoView({{ behavior: "smooth", block: "nearest" }});
    }}

    function closeTypeDetail() {{
      selectedTypeIdx = null;
      typesChart.data.datasets[0].backgroundColor = DATA.types.map(() => "#f59e0b");
      typesChart.update("none");
      document.getElementById("type-detail").style.display = "none";
    }}
  </script>
</body>
</html>
"""


def generate_animal_trends(days_back: int = LOOKBACK_DAYS) -> tuple[Optional[io.BytesIO], str]:
    """Generate the animal services trends HTML page.

    Returns (BytesIO buffer, summary string) — matches the signature used by
    scripts/generate_map.py for consistency.
    """
    from animalsvc.animal_bot import fetch_animals_monthly

    months_back = max(1, days_back // 30) + 1
    # Cache-aware fetch: always refreshes the current month and backfills past
    # months not already cached end-to-end (see open311_cache.fetch_monthly_with_cache).
    records = fetch_animals_monthly(months_back, use_cache=True)
    if not records:
        return None, f"🐾 No animal services data found for last {days_back} days."

    data = _aggregate(records)
    fetched_at = _format_central_time()
    html = _render_html(data, fetched_at)

    import os
    out_path = os.path.join(os.path.dirname(__file__), "..", "docs", "animal", "trends", "index.html")
    out_path = os.path.normpath(out_path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    buf = io.BytesIO(html.encode("utf-8"))
    buf.seek(0)

    summary = (
        f"🐾 *Animal Services Trends*\n"
        f"_Last {days_back} days · {data['total']:,} reports across "
        f"{len(data['months'])} months_"
    )
    return buf, summary
