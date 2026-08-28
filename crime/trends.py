"""Crime trends — monthly counts, category breakdown, and location analysis."""

import io
import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests


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

SOCRATA_BASE = "https://data.austintexas.gov/resource"
CRIME_DATASET = "fdj4-gpfu"
LOOKBACK_DAYS = 365

_session: Optional[requests.Session] = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        token = os.getenv("AUSTINAPIKEY", "")
        headers = {"Accept": "application/json", "User-Agent": "austin311bot/0.1 (crime trends)"}
        if token:
            headers["X-App-Token"] = token
        _session.headers.update(headers)
    return _session


# ── UCR category mapping ───────────────────────────────────────────────────────

CRIME_CATEGORIES = {
    "Domestic & Family":     {"icon": "👪", "color": "#ef4444"},
    "Assault":               {"icon": "🥊", "color": "#dc2626"},
    "Vehicle Crime":         {"icon": "🚗", "color": "#f97316"},
    "Theft":                 {"icon": "💰", "color": "#f59e0b"},
    "Harassment & Disorder": {"icon": "🗣️", "color": "#a78bfa"},
    "Burglary":              {"icon": "🥷", "color": "#6366f1"},
    "Drugs & DWI":           {"icon": "💊", "color": "#22c55e"},
    "Criminal Mischief":     {"icon": "🔨", "color": "#64748b"},
    "Fraud & Financial":     {"icon": "💳", "color": "#06b6d4"},
    "Other":                 {"icon": "⚠️", "color": "#94a3b8"},
}

CATEGORY_ORDER = list(CRIME_CATEGORIES.keys())

# UCR codes that are vehicle-related theft (pulled out of the 600-699 range)
_VEHICLE_THEFT_CODES = {601, 604, 613, 614, 620}


def _ucr_to_category(ucr_str: str) -> str:
    try:
        ucr = int(ucr_str)
    except (ValueError, TypeError):
        return "Other"
    if ucr in _VEHICLE_THEFT_CODES or 700 <= ucr <= 799:
        return "Vehicle Crime"
    if 600 <= ucr <= 699 or ucr == 8503:   # 8503 = mail theft
        return "Theft"
    if 500 <= ucr <= 599:
        return "Burglary"
    if 400 <= ucr <= 499 or 900 <= ucr <= 999:
        return "Assault"
    if (3400 <= ucr <= 3499) or (2400 <= ucr <= 2499) or (2000 <= ucr <= 2099) or (3000 <= ucr <= 3099):
        return "Domestic & Family"
    if 2700 <= ucr <= 2799:
        return "Harassment & Disorder"
    if 1800 <= ucr <= 1899 or 2100 <= ucr <= 2199:
        return "Drugs & DWI"
    if 1000 <= ucr <= 1299 or ucr == 4022:  # 4022 = identity theft
        return "Fraud & Financial"
    if 1400 <= ucr <= 1499:
        return "Criminal Mischief"
    return "Other"


# ── data fetching ──────────────────────────────────────────────────────────────

def _cutoff(days_back: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%dT00:00:00")


def _fetch_monthly(days_back: int) -> list:
    """Monthly totals for the trend chart."""
    params = {
        "$select": "date_trunc_ym(occ_date) as month, count(*) as cnt",
        "$where": f"occ_date >= '{_cutoff(days_back)}'",
        "$group": "month",
        "$order": "month ASC",
        "$limit": 100000,
    }
    resp = _get_session().get(f"{SOCRATA_BASE}/{CRIME_DATASET}.json", params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _fetch_by_type(days_back: int) -> list:
    """Per crime_type + UCR code counts — for category bar and drill-down."""
    params = {
        "$select": "ucr_code, crime_type, count(*) as cnt",
        "$where": f"occ_date >= '{_cutoff(days_back)}' AND crime_type IS NOT NULL AND ucr_code IS NOT NULL",
        "$group": "ucr_code, crime_type",
        "$order": "cnt DESC",
        "$limit": 10000,
    }
    resp = _get_session().get(f"{SOCRATA_BASE}/{CRIME_DATASET}.json", params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _fetch_locations(days_back: int) -> list:
    """Crime counts by location type."""
    params = {
        "$select": "location_type, count(*) as cnt",
        "$where": f"occ_date >= '{_cutoff(days_back)}' AND location_type IS NOT NULL",
        "$group": "location_type",
        "$order": "cnt DESC",
        "$limit": 50,
    }
    resp = _get_session().get(f"{SOCRATA_BASE}/{CRIME_DATASET}.json", params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _fetch_yearly() -> list:
    """Per-year + UCR code counts over the FULL dataset (2003-present)."""
    params = {
        "$select": "date_extract_y(occ_date) as yr, ucr_code, count(*) as cnt",
        "$where": "ucr_code IS NOT NULL",
        "$group": "yr, ucr_code",
        "$order": "yr ASC",
        "$limit": 100000,
    }
    resp = _get_session().get(f"{SOCRATA_BASE}/{CRIME_DATASET}.json", params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


# ── aggregation ────────────────────────────────────────────────────────────────

def _rolling_avg(counts: list, window: int = 3) -> list:
    result = []
    for i in range(len(counts)):
        if i < window - 1:
            result.append(None)
        else:
            result.append(round(sum(counts[i - window + 1 : i + 1]) / window, 1))
    return result


def _aggregate(monthly_rows: list, type_rows: list, location_rows: list, days_back: int = LOOKBACK_DAYS) -> dict:
    now = datetime.now(timezone.utc)
    cutoff_month = (now - timedelta(days=days_back)).strftime("%Y-%m")
    current_month = now.strftime("%Y-%m")

    # Monthly trend
    months, monthly_totals = [], []
    for r in sorted(monthly_rows, key=lambda x: x.get("month", "")):
        month = (r.get("month") or "")[:7]
        if month:
            months.append(month)
            monthly_totals.append(int(r.get("cnt", 0)))

    # Drop the first month if the lookback cutoff falls mid-month (partial data)
    if months and months[0] == cutoff_month:
        months = months[1:]
        monthly_totals = monthly_totals[1:]

    # Drop the current month — it's always incomplete until the month ends
    if months and months[-1] == current_month:
        months = months[:-1]
        monthly_totals = monthly_totals[:-1]

    total = sum(monthly_totals)
    avg_per_month = round(total / max(1, len(months)), 1)
    peak_idx = monthly_totals.index(max(monthly_totals)) if monthly_totals else -1
    peak_month = months[peak_idx] if peak_idx >= 0 else "—"
    peak_count = monthly_totals[peak_idx] if peak_idx >= 0 else 0

    # Category breakdown + drill-down crimes
    cat_counts: dict = defaultdict(int)
    cat_crimes: dict = defaultdict(lambda: defaultdict(int))

    for r in type_rows:
        ucr = r.get("ucr_code", "")
        crime_type = (r.get("crime_type") or "").title()
        cnt = int(r.get("cnt", 0))
        cat = _ucr_to_category(ucr)
        cat_counts[cat] += cnt
        cat_crimes[cat][crime_type] += cnt

    categories = []
    for cat in CATEGORY_ORDER:
        if cat not in cat_counts:
            continue
        crimes_sorted = sorted(cat_crimes[cat].items(), key=lambda x: -x[1])
        categories.append({
            "name":   cat,
            "count":  cat_counts[cat],
            "icon":   CRIME_CATEGORIES[cat]["icon"],
            "color":  CRIME_CATEGORIES[cat]["color"],
            "crimes": [{"name": n, "count": c} for n, c in crimes_sorted[:25]],
        })
    categories.sort(key=lambda c: -c["count"])

    top_category = categories[0]["name"] if categories else "—"

    # Location breakdown (top 15)
    locations = [
        {"name": r["location_type"].title(), "count": int(r["cnt"])}
        for r in location_rows
        if r.get("location_type")
    ][:15]

    return {
        "total": total,
        "months": months,
        "monthly_totals": monthly_totals,
        "rolling_avg": _rolling_avg(monthly_totals),
        "avg_per_month": avg_per_month,
        "peak_month": peak_month,
        "peak_count": peak_count,
        "top_category": top_category,
        "categories": categories,
        "locations": locations,
    }


def _aggregate_yearly(yearly_rows: list) -> dict:
    """Yearly totals and per-category series over the full dataset."""
    now = datetime.now(timezone.utc)
    current_year = now.year

    year_cat: dict = {}
    for r in yearly_rows:
        try:
            yr = int(r.get("yr"))
        except (TypeError, ValueError):
            continue
        cnt = int(r.get("cnt", 0))
        cat = _ucr_to_category(r.get("ucr_code", ""))
        year_cat.setdefault(yr, defaultdict(int))[cat] += cnt

    years = sorted(year_cat)
    totals = [sum(year_cat[y].values()) for y in years]
    cats = [c for c in CATEGORY_ORDER if any(year_cat[y].get(c, 0) for y in years)]
    cat_series = {c: [year_cat[y].get(c, 0) for y in years] for c in cats}

    return {
        "years": years,
        "totals": totals,
        "cat_series": cat_series,
        "current_year": current_year,
    }


# ── HTML ───────────────────────────────────────────────────────────────────────

def _render_html(data: dict, fetched_at: str) -> str:
    total       = data["total"]
    avg_pm      = data["avg_per_month"]
    peak_count  = data["peak_count"]
    peak_month  = data["peak_month"]
    top_cat     = data["top_category"]
    months      = data["months"]

    yearly       = data.get("yearly") or {}
    yyears       = yearly.get("years", [])
    ytotals      = yearly.get("totals", [])
    ycurrent     = yearly.get("current_year", datetime.now(timezone.utc).year)
    ycat_series  = yearly.get("cat_series", {})
    ycats        = [
        {"name": c, "color": CRIME_CATEGORIES[c]["color"], "data": ycat_series[c]}
        for c in ycat_series
    ]
    yearly_first = yyears[0] if yyears else "—"

    peak_label = (
        datetime.strptime(peak_month, "%Y-%m").strftime("%b %Y")
        if peak_month not in ("—", "") else "—"
    )
    month_labels = [datetime.strptime(m, "%Y-%m").strftime("%b %Y") for m in months]

    payload = json.dumps({
        "months":        month_labels,
        "monthlyTotals": data["monthly_totals"],
        "rollingAvg":    data["rolling_avg"],
        "categories":    data["categories"],
        "locations":     data["locations"],
        "yearlyYears":       yyears,
        "yearlyTotals":      ytotals,
        "yearlyCategories":  ycats,
        "yearlyCurrentYear": ycurrent,
    })

    cat_height = max(320, len(data["categories"]) * 36)
    loc_height = max(300, len(data["locations"]) * 28)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
  <meta name="google" content="notranslate" />
  <title>Austin 311 — Crime Trends</title>
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
    .chart-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
    @media (max-width: 580px) {{ .chart-grid {{ grid-template-columns: 1fr; }} }}

    /* drill-down panel */
    #drill-panel {{
      display: none; background: var(--bg-card);
      border: 1px solid var(--border); border-left: 3px solid #3b82f6;
      border-radius: 8px; padding: 16px 16px 18px;
      animation: slideIn 0.16s ease;
    }}
    @keyframes slideIn {{ from {{ opacity:0; transform:translateY(-5px); }} to {{ opacity:1; transform:translateY(0); }} }}
    #drill-header {{ display: flex; align-items: baseline; justify-content: space-between; margin-bottom: 14px; flex-wrap: wrap; gap: 6px; }}
    #drill-title {{ font-size: 15px; font-weight: 700; color: var(--text-head); }}
    #drill-meta {{ font-size: 11px; color: var(--text-sub); }}
    #drill-close {{ background: none; border: 1px solid var(--border); color: var(--text-sub); border-radius: 4px; padding: 3px 9px; font-size: 11px; cursor: pointer; }}
    #drill-close:hover {{ border-color: var(--text-sub); color: var(--text); }}
    #drill-body {{ display: grid; grid-template-columns: 220px 1fr; gap: 20px; align-items: start; }}
    @media (max-width: 580px) {{ #drill-body {{ grid-template-columns: 1fr; }} #drill-donut-wrap {{ height: 200px; }} }}
    #drill-donut-wrap {{ position: relative; height: 240px; }}
    #drill-list {{ display: flex; flex-direction: column; gap: 7px; max-height: 300px; overflow-y: auto; padding-right: 4px; }}
    .drill-row {{ display: flex; align-items: center; gap: 8px; }}
    .drill-swatch {{ width: 10px; height: 10px; border-radius: 2px; flex-shrink: 0; }}
    .drill-name {{ font-size: 12px; color: var(--text); flex: 1; }}
    .drill-count {{ font-size: 11px; color: var(--text-sub); white-space: nowrap; }}
    .drill-pct {{ font-size: 11px; font-weight: 700; color: var(--text-muted); min-width: 34px; text-align: right; }}
    #drill-hint {{ font-size: 11px; color: var(--text-muted); text-align: center; margin-top: 8px; }}

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
    <div id="panel-title">🚔 Austin Crime Trends</div>
    <div id="panel-subtitle">APD incidents — last 12 months</div>
    <div id="last-ran">Last ran: {fetched_at}</div>
    <div class="btn-row">
      <a class="fbtn" href="../">← Crime Map</a>
      <a class="fbtn" href="../../">Austin 311 Home</a>
    </div>
  </div>

  <div id="stats">
    <div class="stats-inner">
      <div class="stat">
        <div class="stat-value" style="color:#3b82f6;">{total:,}</div>
        <div class="stat-label">Total incidents</div>
        <div class="stat-sub">last 12 months</div>
      </div>
      <div class="stat">
        <div class="stat-value" style="color:#22c55e;">{avg_pm:.0f}</div>
        <div class="stat-label">Avg / month</div>
      </div>
      <div class="stat">
        <div class="stat-value" style="color:#ef4444;">{peak_count:,}</div>
        <div class="stat-label">Peak month</div>
        <div class="stat-sub">{peak_label}</div>
      </div>
      <div class="stat">
        <div class="stat-value" style="color:#f59e0b;font-size:0.95rem;">{top_cat}</div>
        <div class="stat-label">Top category</div>
      </div>
    </div>
  </div>

  <div id="chart-wrap">

    <div class="chart-block">
      <div class="chart-title">📅 Yearly Trends</div>
      <div class="chart-sub">Full APD dataset · {yearly_first} – {ycurrent} · * = partial year</div>
      <div class="chart-grid">
        <div>
          <div class="chart-sub">Total incidents per year</div>
          <div class="chart-container" style="height:260px;"><canvas id="yearlyChart"></canvas></div>
        </div>
        <div>
          <div class="chart-sub">Incidents by category — all-time share</div>
          <div class="chart-container" style="height:260px;"><canvas id="yearDonutChart"></canvas></div>
        </div>
      </div>
      <div class="chart-sub" style="margin-top:14px;">Incidents by category per year</div>
      <div class="chart-container" style="height:300px;"><canvas id="yearCatChart"></canvas></div>
    </div>

    <div class="chart-block">
      <div class="chart-title">Monthly incident totals</div>
      <div class="chart-container" style="height:280px;"><canvas id="monthlyChart"></canvas></div>
    </div>

    <div class="chart-block">
      <div class="chart-title">Incidents by crime category</div>
      <div class="chart-sub">Last 12 months · <strong style="color:#3b82f6;">Click a bar to see the specific crime types</strong></div>
      <div class="chart-container" style="height:{cat_height}px;"><canvas id="catChart"></canvas></div>
      <div id="drill-hint">👆 Click any category to drill down into specific crime types</div>
    </div>

    <div id="drill-panel">
      <div id="drill-header">
        <div>
          <div id="drill-title">Category</div>
          <div id="drill-meta"></div>
        </div>
        <button id="drill-close" onclick="closeDrill()">✕ Close</button>
      </div>
      <div id="drill-body">
        <div id="drill-donut-wrap"><canvas id="drillChart"></canvas></div>
        <div id="drill-list"></div>
      </div>
    </div>

    <div class="chart-block">
      <div class="chart-title">Where crimes happen</div>
      <div class="chart-sub">Top 15 location types — last 12 months</div>
      <div class="chart-container" style="height:{loc_height}px;"><canvas id="locChart"></canvas></div>
    </div>

  </div>

  <footer>
    Data: <a href="https://data.austintexas.gov/d/fdj4-gpfu" target="_blank" rel="noopener">APD Crime Reports (Socrata · fdj4-gpfu)</a>
    &nbsp;·&nbsp; <a href="../">← Crime Map</a>
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

    // ── monthly bar chart ────────────────────────────────────────────────────
    new Chart(document.getElementById("monthlyChart"), {{
      type: "bar",
      data: {{
        labels: DATA.months,
        datasets: [
          {{ label: "Incidents", data: DATA.monthlyTotals, backgroundColor: "#3b82f6", borderRadius: 3, order: 2 }},
          {{ label: "3-mo avg",  data: DATA.rollingAvg, type: "line", borderColor: "#ef4444", borderWidth: 2,
             borderDash: [5, 3], pointRadius: 0, tension: 0.4, fill: false, spanGaps: true, order: 1 }},
        ],
      }},
      options: {{
        responsive: true, maintainAspectRatio: false,
        plugins: {{ legend: {{ labels: {{ color: leg, font: {{ size: 11 }} }} }}, tooltip: TIP }},
        scales: {{
          x: {{ ticks: {{ color: tick, font: {{ size: 11 }} }}, grid: {{ color: grid }} }},
          y: {{ ticks: {{ color: tick, font: {{ size: 11 }} }}, grid: {{ color: grid }}, beginAtZero: true }},
        }},
      }},
    }});

    // ── category bar chart ───────────────────────────────────────────────────
    const catChart = new Chart(document.getElementById("catChart"), {{
      type: "bar",
      data: {{
        labels: DATA.categories.map(c => c.icon + " " + c.name),
        datasets: [{{
          data:            DATA.categories.map(c => c.count),
          backgroundColor: DATA.categories.map(c => c.color),
          borderRadius: 4,
        }}],
      }},
      options: {{
        indexAxis: "y",
        responsive: true, maintainAspectRatio: false,
        onClick(evt, elements) {{
          if (elements.length) openDrill(DATA.categories[elements[0].index]);
        }},
        plugins: {{
          legend: {{ display: false }},
          tooltip: {{
            ...TIP,
            callbacks: {{
              label: ctx => ` ${{fmt(ctx.parsed.x)}} incidents (${{pct(ctx.parsed.x, DATA.categories.reduce((a,c)=>a+c.count,0))}})`,
              afterBody: () => ["  Click to see breakdown →"],
            }},
          }},
        }},
        scales: {{
          x: {{ ticks: {{ color: tick, font: {{ size: 11 }}, callback: v => fmt(v) }}, grid: {{ color: grid }}, beginAtZero: true }},
          y: {{ ticks: {{ color: tick, font: {{ size: 11 }} }}, grid: {{ color: grid }} }},
        }},
      }},
    }});

    // ── drill-down ────────────────────────────────────────────────────────────
    let drillChart = null;

    function openDrill(cat) {{
      const total = cat.crimes.reduce((a, c) => a + c.count, 0);
      document.getElementById("drill-title").textContent = cat.icon + " " + cat.name;
      document.getElementById("drill-meta").textContent =
        `${{fmt(cat.count)}} incidents · top ${{cat.crimes.length}} crime types`;

      const top = cat.crimes.slice(0, 12);
      const topTotal = top.reduce((a, c) => a + c.count, 0);
      const otherCount = total - topTotal;

      const labels  = top.map(c => c.name);
      const counts  = top.map(c => c.count);
      const palette = [
        "#3b82f6","#ef4444","#22c55e","#f59e0b","#8b5cf6","#06b6d4",
        "#f97316","#ec4899","#10b981","#6366f1","#a78bfa","#fbbf24",
      ];
      if (otherCount > 0) {{ labels.push("Other types"); counts.push(otherCount); palette.push("#94a3b8"); }}

      if (drillChart) {{ drillChart.destroy(); drillChart = null; }}
      drillChart = new Chart(document.getElementById("drillChart"), {{
        type: "doughnut",
        data: {{
          labels,
          datasets: [{{ data: counts, backgroundColor: palette, borderWidth: 2,
                        borderColor: isDark ? "#161a24" : "#ffffff", hoverOffset: 6 }}],
        }},
        options: {{
          responsive: true, maintainAspectRatio: false, cutout: "58%",
          plugins: {{
            legend: {{ display: false }},
            tooltip: {{
              ...TIP,
              callbacks: {{
                label: ctx => ` ${{fmt(ctx.parsed)}} (${{pct(ctx.parsed, total)}})`,
              }},
            }},
          }},
        }},
      }});

      const list = document.getElementById("drill-list");
      list.innerHTML = "";
      labels.forEach((name, i) => {{
        const row = document.createElement("div");
        row.className = "drill-row";
        row.innerHTML = `
          <span class="drill-swatch" style="background:${{palette[i]}}"></span>
          <span class="drill-name">${{name}}</span>
          <span class="drill-count">${{fmt(counts[i])}}</span>
          <span class="drill-pct">${{pct(counts[i], total)}}</span>`;
        list.appendChild(row);
      }});

      document.getElementById("drill-panel").style.display = "block";
      document.getElementById("drill-hint").style.display = "none";
      document.getElementById("drill-panel").scrollIntoView({{ behavior: "smooth", block: "nearest" }});
    }}

    function closeDrill() {{
      document.getElementById("drill-panel").style.display = "none";
      document.getElementById("drill-hint").style.display = "block";
      if (drillChart) {{ drillChart.destroy(); drillChart = null; }}
    }}

    // ── location bar chart ───────────────────────────────────────────────────
    const locTotal = DATA.locations.reduce((a, l) => a + l.count, 0);
    new Chart(document.getElementById("locChart"), {{
      type: "bar",
      data: {{
        labels: DATA.locations.map(l => l.name),
        datasets: [{{
          data: DATA.locations.map(l => l.count),
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
              label: ctx => ` ${{fmt(ctx.parsed.x)}} incidents (${{pct(ctx.parsed.x, locTotal)}})`,
            }},
          }},
        }},
        scales: {{
          x: {{ ticks: {{ color: tick, font: {{ size: 11 }}, callback: v => fmt(v) }}, grid: {{ color: grid }}, beginAtZero: true }},
          y: {{ ticks: {{ color: tick, font: {{ size: 11 }} }}, grid: {{ color: grid }} }},
        }},
      }},
    }});
    // ── yearly trends (full dataset) ─────────────────────────────────────────
    const YY = DATA.yearlyYears || [];
    const YT = DATA.yearlyTotals || [];
    const YCATS = DATA.yearlyCategories || [];
    const YCUR = DATA.yearlyCurrentYear;
    const YLABELS = YY.map(y => y === YCUR ? y + "*" : String(y));

    new Chart(document.getElementById("yearlyChart"), {{
      type: "line",
      data: {{
        labels: YLABELS,
        datasets: [{{
          label: "Incidents",
          data: YT,
          borderColor: "#3b82f6",
          backgroundColor: "rgba(59,130,246,0.12)",
          fill: true,
          tension: 0.25,
          pointRadius: 2,
          borderWidth: 2,
        }}],
      }},
      options: {{
        responsive: true, maintainAspectRatio: false,
        plugins: {{ legend: {{ display: false }}, tooltip: TIP }},
        scales: {{
          x: {{ ticks: {{ color: tick, font: {{ size: 10 }} }}, grid: {{ color: grid }} }},
          y: {{ ticks: {{ color: tick, font: {{ size: 10 }}, callback: v => fmt(v) }}, grid: {{ color: grid }}, beginAtZero: true }},
        }},
      }},
    }});

    new Chart(document.getElementById("yearCatChart"), {{
      type: "line",
      data: {{
        labels: YLABELS,
        datasets: YCATS.map(c => ({{
          label: c.name,
          data: c.data,
          borderColor: c.color,
          backgroundColor: c.color,
          tension: 0.25,
          pointRadius: 0,
          hoverRadius: 3,
          borderWidth: 2,
        }})),
      }},
      options: {{
        responsive: true, maintainAspectRatio: false,
        interaction: {{ mode: "index", intersect: false }},
        plugins: {{ legend: {{ labels: {{ color: leg, font: {{ size: 10 }} }} }}, tooltip: TIP }},
        scales: {{
          x: {{ ticks: {{ color: tick, font: {{ size: 10 }} }}, grid: {{ color: grid }} }},
          y: {{ ticks: {{ color: tick, font: {{ size: 10 }}, callback: v => fmt(v) }}, grid: {{ color: grid }}, beginAtZero: true }},
        }},
      }},
    }});

    const fullIdx = YY.map((y, i) => (y !== YCUR ? i : -1)).filter(i => i >= 0);
    const donutData = YCATS.map(c => fullIdx.reduce((a, i) => a + (c.data[i] || 0), 0));
    const donutTotal = donutData.reduce((a, b) => a + b, 0);
    new Chart(document.getElementById("yearDonutChart"), {{
      type: "doughnut",
      data: {{
        labels: YCATS.map(c => c.name),
        datasets: [{{
          data: donutData,
          backgroundColor: YCATS.map(c => c.color),
          borderColor: isDark ? "#161a24" : "#ffffff",
          borderWidth: 2,
        }}],
      }},
      options: {{
        responsive: true, maintainAspectRatio: false, cutout: "58%",
        plugins: {{
          legend: {{ position: "right", labels: {{ color: leg, font: {{ size: 10 }} }} }},
          tooltip: {{
            ...TIP,
            callbacks: {{
              label: ctx => ` ${{ctx.label}}: ${{fmt(ctx.parsed)}} (${{pct(ctx.parsed, donutTotal)}})`,
            }},
          }},
        }},
      }},
    }});
  </script>
</body>
</html>
"""


# ── entry point ────────────────────────────────────────────────────────────────

def generate_crime_trends(
    days_back: int = LOOKBACK_DAYS,
) -> tuple[Optional[io.BytesIO], str]:
    try:
        monthly_rows   = _fetch_monthly(days_back)
        type_rows      = _fetch_by_type(days_back)
        location_rows  = _fetch_locations(days_back)
        yearly_rows    = _fetch_yearly()
    except Exception as e:
        logger.error(f"crime trends fetch: {e}")
        return None, f"🚔 Error fetching crime data: {e}"

    if not monthly_rows:
        return None, f"🚔 No crime data found for last {days_back} days."

    data = _aggregate(monthly_rows, type_rows, location_rows, days_back)
    data["yearly"] = _aggregate_yearly(yearly_rows)
    fetched_at = _format_central_time()
    html = _render_html(data, fetched_at)

    buf = io.BytesIO(html.encode("utf-8"))
    buf.seek(0)
    summary = f"🚔 *Crime Trends*\n_{data['total']:,} incidents · top category: {data['top_category']}_"
    return buf, summary
