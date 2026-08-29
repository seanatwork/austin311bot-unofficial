"""
Generate a static HTML trends page for homeless-related 311 reports.

Primary story: total matched complaint volume over time — how many 311 tickets
are filed each month that match homeless/encampment keywords across all relevant
service codes. Since Austin 311 has no homeless-specific category, keyword
matching across multiple codes is the only way to track this.

Secondary layer: how many of those tickets are closed with the HSO boilerplate.
"""

import io
import math
from datetime import datetime, timezone, timedelta
from collections import defaultdict


def _format_central_time() -> str:
    """Return current time formatted in US Central Time (CDT/CST)."""
    utc_now = datetime.now(timezone.utc)
    
    # Check if DST is in effect (simplified: March to November)
    month = utc_now.month
    is_dst = 3 <= month <= 11
    offset_hours = -5 if is_dst else -6
    
    central_now = utc_now + timedelta(hours=offset_hours)
    tz_abbr = "CDT" if is_dst else "CST"
    return central_now.strftime(f"%Y-%m-%d %I:%M %p {tz_abbr}")


GA_SNIPPET = """<!-- Google tag (gtag.js) -->
<script async src="https://www.googletagmanager.com/gtag/js?id=G-TS158R7XSN"></script>
<script>
  window.dataLayer = window.dataLayer || [];
  function gtag(){dataLayer.push(arguments);}
  gtag('js', new Date());
  gtag('config', 'G-TS158R7XSN');
</script>
"""


def _is_hso_deflected(record: dict) -> bool:
    """True if this ticket was closed with the HSO deflection boilerplate."""
    notes = (record.get("status_notes") or "").lower()
    if "homeless strategy" in notes:
        return True
    # Handles asterisk-formatted variant: "Homeless* *Strategy* *Office"
    if "homeless" in notes and "strategy" in notes:
        return True
    return False


def generate_homeless_trends(days_back: int = 365) -> tuple:
    """Fetch homeless 311 data and generate a static HTML trends page.

    Returns:
        tuple: (BytesIO buffer with HTML, summary string)
    """
    from homeless.homeless_bot import fetch_encampment_reports_monthly

    # Fetch month by month — the Open311 API returns records oldest-first, so a
    # single 365-day request only returns the oldest ~90 days before hitting the
    # pagination cap. Month-by-month ensures every period is fully covered.
    months_back = max(1, days_back // 30) + 1
    # Bypass the SQLite cache here: the cache is incremental and can be missing
    # entire months (this page previously undercounted early months ~8x for that
    # reason). The trends page must always reflect a complete, current fetch.
    records = fetch_encampment_reports_monthly(months_back=months_back, use_cache=False)

    if not records:
        buf = io.BytesIO(b"<p>No data found.</p>")
        buf.seek(0)
        return buf, "No homeless-related 311 records found."

    MONTH_NAMES = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]

    monthly = defaultdict(lambda: {"total": 0, "hso": 0, "other_closed": 0, "open": 0})

    total_hso = 0

    for r in records:
        dt_str = r.get("requested_datetime") or ""
        if not dt_str:
            continue
        try:
            dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        except ValueError:
            continue

        key = f"{dt.year}-{dt.month:02d}"
        status = (r.get("status") or "").lower()
        is_hso = _is_hso_deflected(r) and status == "closed"

        monthly[key]["total"] += 1
        if is_hso:
            monthly[key]["hso"] += 1
            total_hso += 1
        elif status == "closed":
            monthly[key]["other_closed"] += 1
        else:
            monthly[key]["open"] += 1

    sorted_months = sorted(monthly.keys())
    total_reports = len(records)
    total_open    = sum(1 for r in records if (r.get("status") or "").lower() == "open")
    deflection_rate = round(total_hso / total_reports * 100) if total_reports else 0

    # Volume trend: first half vs second half total count
    half = len(sorted_months) // 2
    first_half  = sorted_months[:half]
    second_half = sorted_months[half:]
    vol_first  = sum(monthly[m]["total"] for m in first_half)
    vol_second = sum(monthly[m]["total"] for m in second_half)
    avg_first  = round(vol_first  / len(first_half))  if first_half  else 0
    avg_second = round(vol_second / len(second_half)) if second_half else 0
    vol_arrow  = "📈 rising"  if avg_second > avg_first  else \
                 ("📉 falling" if avg_second < avg_first else "➡️ stable")

    # SVG line chart
    PLOT_X0, PLOT_X1 = 44, 548
    PLOT_Y_BOT, PLOT_Y_TOP = 130, 12
    n = len(sorted_months)
    totals = [monthly[m]["total"] for m in sorted_months]
    data_max = max(totals) if totals else 1

    y_step = 50
    y_axis_max = math.ceil(data_max / y_step) * y_step + y_step
    y_axis_min = 0

    def to_y(val):
        frac = (val - y_axis_min) / (y_axis_max - y_axis_min)
        return round(PLOT_Y_BOT - frac * (PLOT_Y_BOT - PLOT_Y_TOP), 1)

    def to_x(i):
        if n <= 1:
            return (PLOT_X0 + PLOT_X1) / 2
        return round(PLOT_X0 + i * (PLOT_X1 - PLOT_X0) / (n - 1), 1)

    pts = [(to_x(i), to_y(totals[i])) for i in range(n)]

    # Gridlines and y-axis labels
    grid_vals = list(range(y_axis_min, y_axis_max + 1, y_step))
    grid_html = ""
    for gv in grid_vals:
        gy = to_y(gv)
        is_base = gv == y_axis_min
        sw = "1" if is_base else "0.7"
        dash = "" if is_base else ' stroke-dasharray="3,2"'
        grid_html += f'<line x1="{PLOT_X0}" y1="{gy}" x2="{PLOT_X1}" y2="{gy}" stroke="var(--border)" stroke-width="{sw}"{dash}/>\n'
        grid_html += f'<text x="{PLOT_X0 - 4}" y="{gy + 3}" text-anchor="end" font-size="9" fill="var(--text-muted)">{gv}</text>\n'

    # Area fill polygon
    area_pts = f"{PLOT_X0},{PLOT_Y_BOT} " + " ".join(f"{x},{y}" for x, y in pts) + f" {PLOT_X1},{PLOT_Y_BOT}"
    # Line polyline
    line_pts = " ".join(f"{x},{y}" for x, y in pts)

    # Peak point
    peak_i = totals.index(max(totals))
    peak_x, peak_y = pts[peak_i]
    peak_val = totals[peak_i]

    # Data point circles
    circles_html = ""
    for i, (mx, my) in enumerate(pts):
        mo_key = sorted_months[i]
        yr, mo = mo_key.split("-")
        lbl = f"{MONTH_NAMES[int(mo)-1]} {yr}"
        r = "4.5" if i == peak_i else "3.5"
        peak_tag = " (peak)" if i == peak_i else ""
        circles_html += f'<circle cx="{mx}" cy="{my}" r="{r}" fill="#3b82f6" stroke="var(--bg-card)" stroke-width="1.5"><title>{lbl}: {totals[i]}{peak_tag}</title></circle>\n'

    # X-axis labels (month only)
    xlabels_html = ""
    for i, mo_key in enumerate(sorted_months):
        yr, mo = mo_key.split("-")
        lbl = MONTH_NAMES[int(mo) - 1]
        xlabels_html += f'<text x="{pts[i][0]}" y="147" text-anchor="middle" font-size="9" fill="var(--text-muted)">{lbl}</text>\n'

    svg_html = f"""<svg viewBox="0 0 560 155" width="100%" style="display:block;overflow:visible;margin-top:4px">
        <defs>
          <linearGradient id="areaGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="#3b82f6" stop-opacity="0.18"/>
            <stop offset="100%" stop-color="#3b82f6" stop-opacity="0.02"/>
          </linearGradient>
        </defs>
        {grid_html}
        <polygon points="{area_pts}" fill="url(#areaGrad)"/>
        <polyline points="{line_pts}" fill="none" stroke="#3b82f6" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
        <text x="{peak_x}" y="{peak_y - 7}" text-anchor="middle" font-size="9" font-weight="700" fill="#3b82f6">{peak_val}</text>
        {circles_html}
        {xlabels_html}
      </svg>"""

    now_str = _format_central_time()

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
  <meta name="google" content="notranslate" />
  <title>Austin 311 — Homeless Report Trends</title>
  {GA_SNIPPET}
  <script>if(localStorage.getItem("theme")==="dark")document.documentElement.classList.add("dark");</script>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    :root {{
      --bg: #f8fafc; --bg-panel: #f1f5f9; --bg-card: #ffffff;
      --border: #e2e8f0; --text: #1e293b; --text-head: #0f172a;
      --text-sub: #64748b; --text-muted: #94a3b8;
      --btn-bg: #e2e8f0; --btn-border: #cbd5e1; --btn-color: #475569;
      --btn-hover-bg: #dbeafe; --btn-hover-color: #1e293b;
      --stat-border: #e2e8f0;
    }}
    html.dark {{
      --bg: #0f1117; --bg-panel: #1e2230; --bg-card: #161a24;
      --border: #2d3348; --text: #e2e8f0; --text-head: #f1f5f9;
      --text-sub: #64748b; --text-muted: #475569;
      --btn-bg: #252b3b; --btn-border: #3d4868; --btn-color: #94a3b8;
      --btn-hover-bg: #2d3453; --btn-hover-color: #e2e8f0;
      --stat-border: #2d3348;
    }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg); color: var(--text);
      min-height: 100vh; display: flex; flex-direction: column; align-items: center;
      padding: 0 0 3rem; transition: background 0.2s, color 0.2s;
    }}
    #theme-toggle {{
      position: fixed; top: 10px; right: 12px; z-index: 200;
      background: var(--bg-card); border: 1px solid var(--border);
      border-radius: 6px; padding: 4px 9px; font-size: 11px; color: var(--text-sub); cursor: pointer;
    }}
    #panel {{
      width: 100%; background: var(--bg-panel); border-bottom: 1px solid var(--border);
      padding: 10px 16px 12px; display: flex; flex-direction: column; align-items: center; gap: 5px;
      position: sticky; top: 0; z-index: 100;
    }}
    #panel-title {{ font-size: 15px; font-weight: 700; color: var(--text-head); }}
    #panel-sub   {{ font-size: 12px; color: var(--text-sub); text-align: center; }}
    #last-ran    {{ font-size: 11px; color: var(--text-muted); }}
    .btn-row {{ display: flex; gap: 4px; flex-wrap: wrap; justify-content: center; }}
    .fbtn {{
      background: var(--btn-bg); border: 1px solid var(--btn-border); color: var(--btn-color);
      padding: 4px 11px; border-radius: 4px; font-size: 12px; cursor: pointer;
      text-decoration: none; display: inline-block;
      transition: background 0.12s, color 0.12s; white-space: nowrap;
    }}
    .fbtn:hover {{ background: var(--btn-hover-bg); color: var(--btn-hover-color); }}
    #main {{ width: 100%; max-width: 900px; padding: 20px 16px; display: flex; flex-direction: column; gap: 20px; }}
    .stats-row {{
      display: flex; flex-wrap: wrap; gap: 0;
      background: var(--bg-card); border: 1px solid var(--border); border-radius: 10px; overflow: hidden;
    }}
    .stat {{
      flex: 1; min-width: 130px; text-align: center;
      padding: 14px 10px 12px; border-right: 1px solid var(--stat-border);
    }}
    .stat:last-child {{ border-right: none; }}
    .stat-val   {{ font-size: 1.4rem; font-weight: 700; line-height: 1.1; }}
    .stat-label {{ font-size: 0.65rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.06em; margin-top: 3px; }}
    .chart-card {{
      background: var(--bg-card); border: 1px solid var(--border); border-radius: 10px; padding: 16px 18px;
    }}
    .chart-title {{ font-size: 13px; font-weight: 700; color: var(--text-head); margin-bottom: 3px; }}
    .chart-sub   {{ font-size: 11px; color: var(--text-sub); margin-bottom: 14px; }}
    .trend-badges {{ display: flex; gap: 10px; flex-wrap: wrap; margin-top: 10px; }}
    .trend-badge {{
      display: inline-block; padding: 4px 12px; border-radius: 4px; font-size: 11px; font-weight: 600;
      background: var(--bg-panel); border: 1px solid var(--border); color: var(--text-sub);
    }}
    footer {{ font-size: 0.72rem; color: var(--text-muted); text-align: center; margin-top: 8px; }}
    footer a {{ color: var(--text-sub); text-decoration: none; }}
  </style>
</head>
<body>

  <button id="theme-toggle" onclick="toggleTheme()">🌙 Dark</button>

  <div id="panel">
    <div id="panel-title">🏕️ Homeless 311 — Complaint Trends</div>
    <div id="panel-sub">Keyword-matched reports across all relevant service codes · last {days_back} days<br/>
    Austin 311 has no homeless-specific category — complaints are filed under parks, ROW, debris &amp; noise codes</div>
    <div id="last-ran">Last ran: {now_str}</div>
    <div class="btn-row">
      <a class="fbtn" href="../">← Homeless Map</a>
      <a class="fbtn" href="../../">🏠 Home</a>
    </div>
  </div>

  <div id="main">

    <div class="stats-row">
      <div class="stat">
        <div class="stat-val" style="color:#3b82f6;">{total_reports:,}</div>
        <div class="stat-label">Total matched ({days_back}d)</div>
      </div>
      <div class="stat">
        <div class="stat-val" style="color:#f59e0b;">{total_open:,}</div>
        <div class="stat-label">Currently open</div>
      </div>
      <div class="stat">
        <div class="stat-val" style="color:#dc2626;">{total_hso:,}</div>
        <div class="stat-label">Closed → HSO</div>
      </div>
    </div>

    <div class="chart-card">
      <div class="chart-title">📈 Monthly Complaint Volume</div>
      <div class="chart-sub">Total keyword-matched 311 tickets filed per month · hover dots for exact counts</div>
      {svg_html}
      <div class="trend-badges">
        <span class="trend-badge">avg {avg_first}/mo → {avg_second}/mo &nbsp;{vol_arrow}</span>
      </div>
    </div>

    <footer>
      Data: <a href="https://311.austintexas.gov/open311/v2" target="_blank" rel="noopener">Austin Open311 API</a>
      &nbsp;·&nbsp; Keyword-matched across PRGRDISS, ATCOCIRW, OBSTMIDB, SBDEBROW, DRCHANEL
      &nbsp;·&nbsp; Generated {now_str}
    </footer>

  </div>

  <script>
    const isDark = document.documentElement.classList.contains("dark");
    document.getElementById("theme-toggle").textContent = isDark ? "☀️ Light" : "🌙 Dark";
    function toggleTheme() {{
      const dark = document.documentElement.classList.toggle("dark");
      localStorage.setItem("theme", dark ? "dark" : "light");
      location.reload();
    }}
  </script>

</body>
</html>"""

    buf = io.BytesIO(html.encode("utf-8"))
    buf.seek(0)

    summary = (
        f"🏕️ *Homeless 311 Trends*\n"
        f"_Last {days_back} days_\n\n"
        f"📊 *{total_reports:,} matched reports*\n"
        f"🔴 *{total_hso:,} closed → HSO* ({deflection_rate}%)\n"
        f"Volume: avg {avg_first}/mo → {avg_second}/mo {vol_arrow}\n\n"
        f"_Source: Austin Open311 API_"
    )
    return buf, summary
