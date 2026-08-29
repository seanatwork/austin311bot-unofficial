"""Graffiti abatement trends — monthly ticket counts over 365 days."""

import io
import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from graffiti.graffiti_bot import fetch_graffiti_monthly


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


def _rolling_avg(counts: list, window: int = 3) -> list:
    result = []
    for i in range(len(counts)):
        if i < window - 1:
            result.append(None)
        else:
            result.append(round(sum(counts[i - window + 1 : i + 1]) / window, 1))
    return result


def _aggregate(records: list) -> dict:
    monthly: dict = defaultdict(int)
    monthly_open: dict = defaultdict(int)
    res_days: dict = defaultdict(list)

    for r in records:
        ts = r.get("requested_datetime") or ""
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            continue

        mk = dt.strftime("%Y-%m")
        monthly[mk] += 1

        if (r.get("status") or "").lower() == "open":
            monthly_open[mk] += 1
        else:
            upd = r.get("updated_datetime") or ""
            if upd:
                try:
                    upd_dt = datetime.fromisoformat(upd.replace("Z", "+00:00"))
                    days = (upd_dt - dt).days
                    if 0 <= days <= 365:
                        res_days[mk].append(days)
                except ValueError:
                    pass

    months = sorted(monthly.keys())
    counts = [monthly[m] for m in months]
    avg_res = [
        round(sum(res_days[m]) / len(res_days[m]), 1) if res_days.get(m) else None
        for m in months
    ]
    return {
        "total": sum(counts),
        "months": months,
        "monthly_counts": counts,
        "monthly_open": [monthly_open[m] for m in months],
        "rolling_avg": _rolling_avg(counts),
        "avg_resolution_days": avg_res,
    }


def _render_html(data: dict, fetched_at: str) -> str:
    total = data["total"]
    months = data["months"]
    counts = data["monthly_counts"]

    avg_per_month = round(total / max(1, len(months)), 1)
    peak_idx = counts.index(max(counts)) if counts else -1
    peak_month = months[peak_idx] if peak_idx >= 0 else "—"
    peak_count = counts[peak_idx] if peak_idx >= 0 else 0
    peak_label = (
        datetime.strptime(peak_month, "%Y-%m").strftime("%b %Y")
        if peak_month != "—"
        else "—"
    )
    month_labels = [datetime.strptime(m, "%Y-%m").strftime("%b %Y") for m in months]

    payload = json.dumps(
        {
            "months": month_labels,
            "monthlyCounts": counts,
            "rollingAvg": data["rolling_avg"],
            "avgResolutionDays": data["avg_resolution_days"],
        }
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
  <meta name="google" content="notranslate" />
  <title>Austin 311 — Graffiti Trends</title>
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
    .chart-container {{ position: relative; height: 300px; }}
    footer {{ text-align: center; padding: 14px 16px; font-size: 0.74rem; color: var(--footer-color); border-top: 1px solid var(--footer-border); }}
    footer a {{ color: var(--text-sub); text-decoration: none; }}
    footer a:hover {{ color: var(--text); }}
    @media (max-width: 520px) {{ .stat-value {{ font-size: 1rem; }} .chart-container {{ height: 240px; }} }}
  </style>
</head>
<body>
  <button id="theme-toggle" onclick="toggleTheme()">🌙 Dark</button>
  <div id="panel">
    <div id="panel-title">🎨 Austin Graffiti Abatement Trends</div>
    <div id="panel-subtitle">New reports per month — last 365 days</div>
    <div id="last-ran">Last ran: {fetched_at}</div>
    <div class="btn-row">
      <a class="fbtn" href="../">← Graffiti Map</a>
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
      <div class="chart-title">New reports per month</div>
      <div class="chart-container"><canvas id="monthlyChart"></canvas></div>
    </div>
    <div class="chart-block">
      <div class="chart-title">Avg days to resolve (closed tickets)</div>
      <div class="chart-container"><canvas id="resolutionChart"></canvas></div>
    </div>
  </div>

  <footer>
    Data: <a href="https://311.austintexas.gov/open311/v2" target="_blank" rel="noopener">Austin Open311 (HHSGRAFF)</a>
    &nbsp;·&nbsp; <a href="../">← Graffiti Map</a>
    &nbsp;·&nbsp; <a href="../../">← Austin 311</a>
  </footer>

  <script>
    const DATA = {payload};

    const isDark = document.documentElement.classList.contains("dark");
    const gridColor = isDark ? "#252b3b" : "#e8ecf0";
    const tickColor = isDark ? "#64748b" : "#6b7280";
    const legColor  = isDark ? "#94a3b8" : "#4b5563";
    const TOOLTIP = {{ backgroundColor: isDark ? "#1e2230" : "#ffffff", borderColor: isDark ? "#3d4868" : "#e2e8f0", borderWidth: 1, titleColor: isDark ? "#f1f5f9" : "#111827", bodyColor: isDark ? "#e2e8f0" : "#374151" }};
    const BASE_OPTS = {{
      plugins: {{
        legend: {{ labels: {{ color: legColor, font: {{ size: 11 }} }} }},
        tooltip: TOOLTIP,
      }},
      scales: {{
        x: {{ ticks: {{ color: tickColor, font: {{ size: 11 }} }}, grid: {{ color: gridColor }} }},
        y: {{ ticks: {{ color: tickColor, font: {{ size: 11 }} }}, grid: {{ color: gridColor }}, beginAtZero: true }},
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

    new Chart(document.getElementById("monthlyChart"), {{
      type: "line",
      data: {{
        labels: DATA.months,
        datasets: [
          {{ label: "Monthly reports", data: DATA.monthlyCounts, borderColor: "#3b82f6", backgroundColor: "rgba(59,130,246,0.08)", fill: true, tension: 0.3, pointRadius: 3, pointHoverRadius: 5 }},
          {{ label: "3-month avg", data: DATA.rollingAvg, borderColor: "#f59e0b", borderWidth: 2, borderDash: [5, 3], pointRadius: 0, tension: 0.4, fill: false, spanGaps: true }},
        ],
      }},
      options: BASE_OPTS,
    }});

    new Chart(document.getElementById("resolutionChart"), {{
      type: "line",
      data: {{
        labels: DATA.months,
        datasets: [
          {{ label: "Avg days to resolve", data: DATA.avgResolutionDays, borderColor: "#22c55e", backgroundColor: "rgba(34,197,94,0.08)", fill: true, tension: 0.3, pointRadius: 3, spanGaps: true }},
        ],
      }},
      options: BASE_OPTS,
    }});
  </script>
</body>
</html>
"""


def generate_graffiti_trends(
    days_back: int = LOOKBACK_DAYS,
) -> tuple[Optional[io.BytesIO], str]:
    # Fetch month by month — the Open311 API returns records oldest-first, so a
    # single 365-day request only returns the oldest ~90 days before hitting the
    # pagination cap. Month-by-month ensures every period is fully covered.
    months_back = max(1, days_back // 30) + 1
    # Bypass the SQLite cache here: the cache is incremental and can be missing
    # records (the homeless trends page had this exact undercount bug). It also
    # accumulates history beyond the requested window, which made this page show
    # 16 months while claiming "last 12 months". Always fetch the full window.
    records = fetch_graffiti_monthly(months_back, use_cache=False)
    if not records:
        return None, f"🎨 No graffiti data found for last {days_back} days."

    data = _aggregate(records)
    fetched_at = _format_central_time()
    html = _render_html(data, fetched_at)

    buf = io.BytesIO(html.encode("utf-8"))
    buf.seek(0)
    summary = f"🎨 *Graffiti Trends*\n_{data['total']:,} reports over {len(data['months'])} months_"
    return buf, summary
