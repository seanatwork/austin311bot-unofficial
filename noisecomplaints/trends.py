"""Noise complaint trends — monthly counts per complaint type over 12 months."""

import io
import json
import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from noisecomplaints.noise_bot import SERVICE_CODES, fetch_noise_monthly


def _format_central_time() -> str:
    """Return current time formatted in US Central Time (CDT/CST)."""
    utc_now = datetime.now(timezone.utc)
    month = utc_now.month
    is_dst = 3 <= month <= 11  # Simplified DST check
    offset_hours = -5 if is_dst else -6
    central_now = utc_now + timedelta(hours=offset_hours)
    tz_abbr = "CDT" if is_dst else "CST"
    return central_now.strftime(f"%Y-%m-%d %I:%M %p {tz_abbr}")

logger = logging.getLogger(__name__)

LOOKBACK_DAYS = 365
OPEN311_URL = "https://311.austintexas.gov/open311/v2/requests.json"

CODE_LABELS = {
    "APDNONNO": "Non-Emergency Noise",
    "DSOUCVMC": "Outdoor Venue / Music",
    "AFDFIREW": "Fireworks",
}
TYPE_COLORS = ["#3b82f6", "#22c55e", "#f59e0b"]


def _fetch_code_paginated(service_code: str, days_back: int) -> list:
    session = _get_session()
    end = _utc_now()
    start = end - timedelta(days=days_back)
    params = {
        "service_code": service_code,
        "start_date": _isoformat_z(start),
        "end_date": _isoformat_z(end),
        "per_page": 100,
        "page": 1,
    }
    records = []
    while True:
        try:
            resp = session.get(OPEN311_URL, params=params, timeout=45)
            resp.raise_for_status()
            batch = resp.json()
        except Exception as e:
            logger.warning(f"fetch {service_code} p{params['page']}: {e}")
            break
        if not isinstance(batch, list) or not batch:
            break
        records.extend(batch)
        if len(batch) < 100:
            break
        params["page"] += 1
        time.sleep(1.0)
    return records


def _rolling_avg(counts: list, window: int = 3) -> list:
    result = []
    for i in range(len(counts)):
        if i < window - 1:
            result.append(None)
        else:
            result.append(round(sum(counts[i - window + 1 : i + 1]) / window, 1))
    return result


def _aggregate(records_by_code: dict) -> dict:
    all_months: set = set()
    code_monthly: dict = {}

    for code, records in records_by_code.items():
        monthly: dict = defaultdict(int)
        for r in records:
            ts = r.get("requested_datetime") or ""
            if not ts:
                continue
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                continue
            monthly[dt.strftime("%Y-%m")] += 1
            all_months.add(dt.strftime("%Y-%m"))
        code_monthly[code] = monthly

    months = sorted(all_months)
    total_by_month = [
        sum(code_monthly.get(c, {}).get(m, 0) for c in SERVICE_CODES) for m in months
    ]
    total = sum(total_by_month)
    peak_month = months[total_by_month.index(max(total_by_month))] if months else "—"
    peak_count = max(total_by_month) if total_by_month else 0
    avg_per_month = round(total / max(1, len(months)), 1)

    type_datasets = []
    type_totals = []
    for i, code in enumerate(SERVICE_CODES):
        monthly = code_monthly.get(code, {})
        type_total = sum(monthly.values())
        type_totals.append(type_total)
        type_datasets.append({
            "label": CODE_LABELS.get(code, code),
            "data": [monthly.get(m, 0) for m in months],
            "color": TYPE_COLORS[i % len(TYPE_COLORS)],
            "total": type_total,
        })

    return {
        "total": total,
        "months": months,
        "total_by_month": total_by_month,
        "rolling_avg": _rolling_avg(total_by_month),
        "peak_month": peak_month,
        "peak_count": peak_count,
        "avg_per_month": avg_per_month,
        "type_datasets": type_datasets,
        "type_totals": type_totals,
    }


def _render_html(data: dict, fetched_at: str) -> str:
    total = data["total"]
    avg_per_month = data["avg_per_month"]
    peak_month = data["peak_month"]
    peak_count = data["peak_count"]
    months = data["months"]
    peak_label = (
        datetime.strptime(peak_month, "%Y-%m").strftime("%b %Y")
        if peak_month not in ("—", "")
        else "—"
    )
    month_labels = [datetime.strptime(m, "%Y-%m").strftime("%b %Y") for m in months]

    payload = json.dumps({
        "months": month_labels,
        "totalByMonth": data["total_by_month"],
        "rollingAvg": data["rolling_avg"],
        "typeLabels": [ds["label"] for ds in data["type_datasets"]],
        "typeTotals": data["type_totals"],
        "typeColors": [ds["color"] for ds in data["type_datasets"]],
    })

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
  <meta name="google" content="notranslate" />
  <title>Austin 311 — Noise Complaint Trends</title>
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
    .stats-inner {{ display: flex; justify-content: center; }}
    .stat {{ flex: 1; max-width: 170px; text-align: center; padding: 10px 8px 9px; border-right: 1px solid var(--border); }}
    .stat:last-child {{ border-right: none; }}
    .stat-value {{ font-size: 1.25rem; font-weight: 700; line-height: 1.1; }}
    .stat-label {{ font-size: 0.67rem; color: var(--text-sub); text-transform: uppercase; letter-spacing: 0.05em; margin-top: 3px; }}
    .stat-sub {{ font-size: 0.67rem; color: var(--text-muted); margin-top: 1px; }}
    #chart-wrap {{ flex: 1; padding: 16px; display: flex; flex-direction: column; gap: 20px; max-width: 1100px; width: 100%; margin: 0 auto; }}
    .chart-block {{ background: var(--bg-card); border: 1px solid var(--border); border-radius: 8px; padding: 14px; }}
    .chart-title {{ font-size: 13px; font-weight: 600; color: var(--chart-title); margin-bottom: 10px; }}
    .chart-container {{ position: relative; height: 320px; }}
    .two-col {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
    @media (max-width: 640px) {{ .two-col {{ grid-template-columns: 1fr; }} .stat-value {{ font-size: 1rem; }} .chart-container {{ height: 260px; }} }}
    footer {{ text-align: center; padding: 14px 16px; font-size: 0.74rem; color: var(--footer-color); border-top: 1px solid var(--footer-border); }}
    footer a {{ color: var(--text-sub); text-decoration: none; }}
    footer a:hover {{ color: var(--text); }}
  </style>
</head>
<body>

  <button id="theme-toggle" onclick="toggleTheme()">🌙 Dark</button>

  <div id="panel">
    <div id="panel-title">🔊 Austin Noise Complaint Trends</div>
    <div id="panel-subtitle">Monthly complaints — last 12 months</div>
    <div id="last-ran">Last ran: {fetched_at}</div>
    <div class="btn-row">
      <a class="fbtn" href="../">← Noise Map</a>
      <a class="fbtn" href="../../">Austin 311 Home</a>
    </div>
  </div>

  <div id="stats">
    <div class="stats-inner">
      <div class="stat">
        <div class="stat-value" style="color:#3b82f6;">{total:,}</div>
        <div class="stat-label">Total complaints</div>
        <div class="stat-sub">last 12 months</div>
      </div>
      <div class="stat">
        <div class="stat-value" style="color:#22c55e;">{avg_per_month:.0f}</div>
        <div class="stat-label">Avg / month</div>
      </div>
      <div class="stat">
        <div class="stat-value" style="color:#f59e0b;">{peak_count:,}</div>
        <div class="stat-label">Peak month</div>
        <div class="stat-sub">{peak_label}</div>
      </div>
    </div>
  </div>

  <div id="chart-wrap">
    <div class="chart-block">
      <div class="chart-title">Monthly complaints (all types)</div>
      <div class="chart-container"><canvas id="monthlyChart"></canvas></div>
    </div>

    <div class="two-col">
      <div class="chart-block">
        <div class="chart-title">Complaint type breakdown</div>
        <div class="chart-container" style="height:280px;"><canvas id="typeChart"></canvas></div>
      </div>
      <div class="chart-block" style="display:flex;flex-direction:column;justify-content:center;">
        <div class="chart-title">By type</div>
        <div id="type-legend" style="display:flex;flex-direction:column;gap:10px;padding:8px 4px;"></div>
      </div>
    </div>
  </div>

  <footer>
    Data: <a href="https://311.austintexas.gov/open311/v2" target="_blank" rel="noopener">Austin Open311 (APDNONNO · DSOUCVMC · AFDFIREW)</a>
    &nbsp;·&nbsp; <a href="../">← Noise Map</a>
    &nbsp;·&nbsp; <a href="../../">← Austin 311</a>
  </footer>

  <script>
    const DATA = {payload};

    const isDark = () => document.documentElement.classList.contains("dark");

    function themeColors() {{
      return {{
        grid:    isDark() ? "#252b3b" : "#e8ecf0",
        tick:    isDark() ? "#64748b" : "#6b7280",
        legend:  isDark() ? "#94a3b8" : "#4b5563",
        tipBg:   isDark() ? "#1e2230" : "#ffffff",
        tipBorder: isDark() ? "#3d4868" : "#e2e8f0",
        tipTitle: isDark() ? "#f1f5f9" : "#111827",
        tipBody:  isDark() ? "#e2e8f0" : "#374151",
      }};
    }}

    function makeTooltip(c) {{
      return {{ backgroundColor: c.tipBg, borderColor: c.tipBorder, borderWidth: 1, titleColor: c.tipTitle, bodyColor: c.tipBody }};
    }}

    function makeScales(c) {{
      return {{
        x: {{ ticks: {{ color: c.tick, font: {{ size: 11 }} }}, grid: {{ color: c.grid }} }},
        y: {{ ticks: {{ color: c.tick, font: {{ size: 11 }} }}, grid: {{ color: c.grid }}, beginAtZero: true }},
      }};
    }}

    const c = themeColors();

    // Monthly bar chart with rolling avg line
    new Chart(document.getElementById("monthlyChart"), {{
      type: "bar",
      data: {{
        labels: DATA.months,
        datasets: [
          {{
            label: "Complaints",
            data: DATA.totalByMonth,
            backgroundColor: "#3b82f6",
            borderRadius: 4,
            order: 2,
          }},
          {{
            label: "3-mo avg",
            data: DATA.rollingAvg,
            type: "line",
            borderColor: "#8b5cf6",
            borderWidth: 2,
            borderDash: [5, 3],
            pointRadius: 0,
            tension: 0.4,
            fill: false,
            spanGaps: true,
            order: 1,
          }},
        ],
      }},
      options: {{
        responsive: true,
        maintainAspectRatio: false,
        plugins: {{
          legend: {{ labels: {{ color: c.legend, font: {{ size: 11 }} }} }},
          tooltip: makeTooltip(c),
        }},
        scales: makeScales(c),
      }},
    }});

    // Doughnut by type
    const typeTotal = DATA.typeTotals.reduce((a, b) => a + b, 0);
    new Chart(document.getElementById("typeChart"), {{
      type: "doughnut",
      data: {{
        labels: DATA.typeLabels,
        datasets: [{{
          data: DATA.typeTotals,
          backgroundColor: DATA.typeColors,
          borderWidth: 2,
          borderColor: isDark() ? "#161a24" : "#ffffff",
          hoverOffset: 6,
        }}],
      }},
      options: {{
        responsive: true,
        maintainAspectRatio: false,
        cutout: "62%",
        plugins: {{
          legend: {{ display: false }},
          tooltip: {{
            ...makeTooltip(c),
            callbacks: {{
              label: ctx => {{
                const pct = typeTotal ? ((ctx.parsed / typeTotal) * 100).toFixed(1) : "0.0";
                return ` ${{ctx.parsed.toLocaleString()}} complaints (${{pct}}%)`;
              }},
            }},
          }},
        }},
      }},
    }});

    // Build the legend sidebar
    const legend = document.getElementById("type-legend");
    DATA.typeLabels.forEach((label, i) => {{
      const pct = typeTotal ? ((DATA.typeTotals[i] / typeTotal) * 100).toFixed(1) : "0.0";
      const row = document.createElement("div");
      row.style.cssText = "display:flex;align-items:center;gap:10px;";
      row.innerHTML = `
        <span style="width:12px;height:12px;border-radius:3px;background:${{DATA.typeColors[i]}};flex-shrink:0;"></span>
        <div>
          <div style="font-size:12px;font-weight:600;">${{label}}</div>
          <div style="font-size:11px;color:var(--text-sub);">${{DATA.typeTotals[i].toLocaleString()}} (${{pct}}%)</div>
        </div>`;
      legend.appendChild(row);
    }});

    // Theme toggle
    const btn = document.getElementById("theme-toggle");
    btn.textContent = isDark() ? "☀️ Light" : "🌙 Dark";
    function toggleTheme() {{
      const dark = document.documentElement.classList.toggle("dark");
      localStorage.setItem("theme", dark ? "dark" : "light");
      location.reload();
    }}
  </script>
</body>
</html>
"""


def generate_noise_trends(
    days_back: int = LOOKBACK_DAYS,
) -> tuple[Optional[io.BytesIO], str]:
    # Fetch month by month — the Open311 API returns records oldest-first, so a
    # single 365-day request only returns the oldest ~90 days before hitting the
    # pagination cap. Month-by-month ensures every period is fully covered.
    months_back = max(1, days_back // 30) + 1
    all_records = fetch_noise_monthly(months_back, use_cache=False)

    if not all_records:
        return None, f"🔊 No noise data found for last {days_back} days."

    # Group records by service code for aggregation
    records_by_code = {code: [] for code in SERVICE_CODES}
    for r in all_records:
        code = r.get("_service_code")
        if code in records_by_code:
            records_by_code[code].append(r)

    logger.info(f"noise trends: {len(all_records)} total records across {len([v for v in records_by_code.values() if v])} codes")

    data = _aggregate(records_by_code)
    fetched_at = _format_central_time()
    html = _render_html(data, fetched_at)

    buf = io.BytesIO(html.encode("utf-8"))
    buf.seek(0)
    summary = f"🔊 *Noise Trends*\n_{data['total']:,} complaints over {len(data['months'])} months_"
    return buf, summary
