"""
Dead Animal Pickup Trends — aggregates ZZARDEAC 311 reports over time.

Primary story: volume over time (monthly counts + open/closed split).
Secondary story: resolution speed — dead animal pickup is one of Austin's
fastest-resolved 311 request types; avg turnaround is typically < 24 hours.
"""

import io
import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

LOOKBACK_DAYS = 365
RESOLUTION_CAP_HOURS = 720  # 30 days — exclude stale outliers


def _format_central_time() -> str:
    """Return current time formatted in US Central Time (CDT/CST)."""
    utc_now = datetime.now(timezone.utc)
    month = utc_now.month
    is_dst = 3 <= month <= 11
    offset_hours = -5 if is_dst else -6
    central_now = utc_now + timedelta(hours=offset_hours)
    tz_abbr = "CDT" if is_dst else "CST"
    return central_now.strftime(f"%Y-%m-%d %I:%M %p {tz_abbr}")


def _format_resolution(avg_hours: Optional[float]) -> str:
    """Format avg resolution time as a human-readable string."""
    if avg_hours is None:
        return "N/A"
    if avg_hours < 48:
        return f"{avg_hours:.1f} hrs"
    return f"{avg_hours / 24:.1f} days"


def _aggregate(records: list) -> dict:
    """Bucket records by month and compute resolution statistics."""
    monthly: dict = defaultdict(int)
    monthly_open: dict = defaultdict(int)
    monthly_resolution: dict = defaultdict(list)  # list of resolution_hours per month
    total = 0
    all_resolution_hours: list = []

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
        total += 1

        is_open = (r.get("status") or "").lower() == "open"
        if is_open:
            monthly_open[month_key] += 1
        else:
            # Compute resolution time for closed records
            upd_str = r.get("updated_datetime") or ""
            if upd_str:
                try:
                    upd = datetime.fromisoformat(upd_str.replace("Z", "+00:00"))
                    resolution_hours = (upd - dt).total_seconds() / 3600
                    if 0 < resolution_hours < RESOLUTION_CAP_HOURS:
                        monthly_resolution[month_key].append(resolution_hours)
                        all_resolution_hours.append(resolution_hours)
                except ValueError:
                    pass

    months_sorted = sorted(monthly.keys())
    counts = [monthly[m] for m in months_sorted]

    # 3-month rolling average on totals
    rolling: list = []
    window = 3
    for i in range(len(counts)):
        if i < window - 1:
            rolling.append(None)
        else:
            rolling.append(round(sum(counts[i - window + 1: i + 1]) / window, 1))

    # Avg resolution hours per month (None if no closed records that month)
    avg_resolution_by_month: list = []
    for m in months_sorted:
        hrs = monthly_resolution.get(m, [])
        if hrs:
            avg_resolution_by_month.append(round(sum(hrs) / len(hrs), 1))
        else:
            avg_resolution_by_month.append(None)

    overall_avg_hours = (
        round(sum(all_resolution_hours) / len(all_resolution_hours), 1)
        if all_resolution_hours else None
    )

    open_total = sum(monthly_open.values())
    pct_open = round(open_total / total * 100) if total else 0

    return {
        "total": total,
        "months": months_sorted,
        "monthly_counts": counts,
        "monthly_open_counts": [monthly_open[m] for m in months_sorted],
        "rolling_avg": rolling,
        "avg_resolution_by_month": avg_resolution_by_month,
        "overall_avg_hours": overall_avg_hours,
        "pct_open": pct_open,
        "open_total": open_total,
    }


def _render_html(data: dict, fetched_at: str) -> str:
    total = data["total"]
    months = data["months"]
    monthly_counts = data["monthly_counts"]
    monthly_open_counts = data["monthly_open_counts"]
    rolling_avg = data["rolling_avg"]
    avg_resolution_by_month = data["avg_resolution_by_month"]
    overall_avg_hours = data["overall_avg_hours"]
    pct_open = data["pct_open"]

    avg_per_month = round(total / max(1, len(months)), 0) if months else 0
    avg_res_str = _format_resolution(overall_avg_hours)
    month_labels = [datetime.strptime(m, "%Y-%m").strftime("%b %Y") for m in months]

    payload = {
        "months": month_labels,
        "monthlyCounts": monthly_counts,
        "monthlyOpenCounts": monthly_open_counts,
        "rollingAvg": rolling_avg,
        "avgResolutionByMonth": avg_resolution_by_month,
    }
    payload_json = json.dumps(payload)

    # Resolution stat color: green if fast (< 24h), amber if moderate, red if slow
    res_color = "#22c55e"
    if overall_avg_hours is not None and overall_avg_hours >= 48:
        res_color = "#f59e0b"
    elif overall_avg_hours is not None and overall_avg_hours >= 96:
        res_color = "#ef4444"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
  <meta name="google" content="notranslate" />
  <title>Austin 311 — Dead Animal Pickup Trends</title>
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
      --note-bg: #f0fdf4; --note-border: #bbf7d0; --note-text: #166534; --note-strong: #14532d;
    }}
    html.dark {{
      --bg: #0f1117; --bg-panel: #1e2230; --bg-card: #161a24;
      --border: #2d3348; --text: #e2e8f0; --text-head: #f1f5f9;
      --text-sub: #64748b; --text-muted: #475569;
      --btn-bg: #252b3b; --btn-border: #3d4868; --btn-color: #94a3b8;
      --btn-hover-bg: #2d3453; --btn-hover-color: #e2e8f0;
      --btn-active-bg: #3b82f6; --btn-active-color: #fff;
      --chart-title: #e2e8f0; --footer-border: #1e2230; --footer-color: #475569;
      --note-bg: #1e2230; --note-border: #2d3348; --note-text: #86efac; --note-strong: #f1f5f9;
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
      flex: 1; max-width: 200px; text-align: center;
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

    #data-note {{
      padding: 12px 14px; background: var(--note-bg);
      border: 1px solid var(--note-border); border-left: 3px solid #22c55e;
      border-radius: 6px; font-size: 0.8rem; color: var(--note-text); line-height: 1.6;
    }}
    #data-note strong {{ color: var(--note-strong); }}

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
    <div id="panel-title">🐿️ Austin Dead Animal Pickup Trends</div>
    <div id="panel-subtitle">Dead animal collection reports — last 12 months</div>
    <div id="last-ran">Last ran: {fetched_at}</div>
    <div class="btn-row">
      <a class="fbtn" href="../">← Dead Animal Map</a>
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
        <div class="stat-value" style="color:#8b5cf6;">{int(avg_per_month):,}</div>
        <div class="stat-label">Avg / month</div>
      </div>
      <div class="stat">
        <div class="stat-value" style="color:{res_color};">{avg_res_str}</div>
        <div class="stat-label">Avg resolution</div>
        <div class="stat-sub">closed tickets</div>
      </div>
      <div class="stat">
        <div class="stat-value" style="color:#ef4444;">{pct_open}%</div>
        <div class="stat-label">Still open</div>
      </div>
    </div>
  </div>

  <div id="chart-wrap">
    <div class="chart-block">
      <div class="chart-title">Monthly reports — total, open, and 3-month average</div>
      <div class="chart-container"><canvas id="monthlyChart"></canvas></div>
    </div>

    <div class="chart-block">
      <div class="chart-title">Avg resolution time per month (hours, closed tickets only)</div>
      <div class="chart-container"><canvas id="resolutionChart"></canvas></div>
    </div>

    <div id="data-note">
      <strong>Dead animal pickup is one of Austin's fastest-resolved 311 request types.</strong>
      Austin Resource Recovery typically picks up within 24–48 hours of a report being filed.
      <br /><br />
      Resolution time is calculated from the time the ticket was filed to when it was marked closed
      in the Open311 system. Records closed more than 30 days after filing are excluded as outliers
      (these are usually stale tickets closed in batch, not real service times).
      <br /><br />
      <strong>Service code:</strong> ZZARDEAC (Dead Animal Collection — Austin Resource Recovery)
    </div>
  </div>

  <footer>
    Data: <a href="https://311.austintexas.gov/open311/v2" target="_blank" rel="noopener">Austin Open311 (ZZARDEAC)</a>
    &nbsp;·&nbsp;
    <a href="../">← Dead Animal Map</a>
    &nbsp;·&nbsp;
    <a href="../../">← Austin 311</a>
  </footer>

  <script>
    const DATA = {payload_json};

    const isDark = document.documentElement.classList.contains("dark");
    const gridColor = isDark ? "#252b3b" : "#e8ecf0";
    const tickColor = isDark ? "#64748b" : "#6b7280";
    const legColor  = isDark ? "#94a3b8" : "#4b5563";
    const TOOLTIP = {{
      backgroundColor: isDark ? "#1e2230" : "#ffffff",
      borderColor:     isDark ? "#3d4868" : "#e2e8f0",
      borderWidth: 1,
      titleColor: isDark ? "#f1f5f9" : "#111827",
      bodyColor:  isDark ? "#e2e8f0"  : "#374151",
    }};
    const TICK_X = {{ color: tickColor, font: {{ size: 11 }} }};
    const TICK_Y = {{ color: tickColor, font: {{ size: 11 }} }};
    const GRID   = {{ color: gridColor }};

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

    document.getElementById("theme-toggle").textContent = isDark ? "☀️ Light" : "🌙 Dark";
    function toggleTheme() {{
      const dark = document.documentElement.classList.toggle("dark");
      localStorage.setItem("theme", dark ? "dark" : "light");
      location.reload();
    }}

    // Monthly volume chart
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

    // Resolution time chart
    new Chart(document.getElementById("resolutionChart"), {{
      type: "line",
      data: {{
        labels: DATA.months,
        datasets: [
          {{
            label: "Avg resolution (hours)",
            data: DATA.avgResolutionByMonth,
            borderColor: "#22c55e",
            backgroundColor: "rgba(34,197,94,0.08)",
            fill: true,
            tension: 0.3,
            pointRadius: 3,
            pointHoverRadius: 5,
            spanGaps: true,
          }},
        ],
      }},
      options: {{
        ...lineOpts,
        plugins: {{
          ...lineOpts.plugins,
          tooltip: {{
            ...TOOLTIP,
            callbacks: {{
              label: ctx => `${{ctx.dataset.label}}: ${{ctx.parsed.y !== null ? ctx.parsed.y.toFixed(1) + " hrs" : "N/A"}}`,
            }},
          }},
        }},
        scales: {{
          x: {{ ticks: TICK_X, grid: GRID }},
          y: {{
            ticks: {{ ...TICK_Y, callback: v => v + " h" }},
            grid: GRID,
            beginAtZero: true,
            title: {{ display: true, text: "hours", color: tickColor, font: {{ size: 11 }} }},
          }},
        }},
      }},
    }});
  </script>
</body>
</html>
"""


def generate_dead_animal_trends(days_back: int = LOOKBACK_DAYS) -> tuple[Optional[io.BytesIO], str]:
    """Generate the dead animal pickup trends HTML page.

    Returns (BytesIO buffer, summary string) — matches the signature used by
    scripts/generate_map.py for consistency.
    """
    from animalsvc.dead_animal_bot import fetch_dead_animal_monthly

    months_back = max(1, days_back // 30) + 1
    records = fetch_dead_animal_monthly(months_back, use_cache=False)
    if not records:
        return None, f"🐿️ No dead animal collection data found for last {days_back} days."

    data = _aggregate(records)
    fetched_at = _format_central_time()
    html = _render_html(data, fetched_at)

    out_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "docs", "animal", "dead", "trends",
    )
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "index.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    logger.info(f"Wrote dead animal trends to {out_path}")

    buf = io.BytesIO(html.encode("utf-8"))
    buf.seek(0)

    avg_res = data["overall_avg_hours"]
    avg_res_str = _format_resolution(avg_res)
    summary = (
        f"🐿️ *Dead Animal Pickup Trends*\n"
        f"_Last {days_back} days · {data['total']:,} reports across "
        f"{len(data['months'])} months_\n"
        f"Avg resolution: {avg_res_str}"
    )
    return buf, summary
