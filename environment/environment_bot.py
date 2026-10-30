"""
Environment page — water conservation, environmental spills, lake water quality.

Combines three independent sources into the single overview page at
https://austin311.com/environment/ :

1. **Lady Bird Lake chlorophyll-a** — Austin Water Quality Sampling (Socrata
   ``5tye-7ray``), filtered to the ``CHLOROPHYLL-A`` parameter and the Lady Bird
   Lake watershed. Lab results are reported in MG/L (and sometimes UG/L or
   MG/M3); everything is normalised to µg/L here, where 1 µg/L = 1,000 MG/L.
   The lab records a QC duplicate ("Field Split") for some sample dates, tagged
   ``qc_type`` — those are dropped so a split isn't counted as a second reading.

2. **TCEQ emergency response spills** — ``xagr-a3x2`` on **data.texas.gov**
   (not data.austintexas.gov, which just redirects). Statewide dataset, so the
   query filters to Travis County. Rows can repeat the same
   ``incid_track_num``; those are de-duplicated before counting.

3. **Water conservation violations** — Open311 ``WWREPORT`` via
   :func:`waterconservation.water_conservation_bot.get_water_conservation_stats`
   (that fetch already falls back to the Open311 SQLite cache when the 311 API
   throttles).

A failed source degrades to a warning box rather than killing the page; the
generator only returns ``None`` when every source came back empty.
"""

import html
import io
import logging
import os
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from open311_client import og_meta_tags

logger = logging.getLogger(__name__)

# ── Sources ──────────────────────────────────────────────────────────────────
WQ_DATASET = "https://data.austintexas.gov/resource/5tye-7ray.json"
SPILLS_DATASET = "https://data.texas.gov/resource/xagr-a3x2.json"

WQ_PARAMETER = "CHLOROPHYLL-A"
WQ_WATERSHED = "Lady Bird Lake"
WQ_LOOKBACK_DAYS = 730  # enough history for the monthly table + a 12-month peak
SPILL_DAYS = 365

# Advisory-trigger levels quoted in the comparison table (µg/L chlorophyll-a).
WHO_THRESHOLD = 10.0
ADVISORY_THRESHOLD = 25.0

# (Socrata site_name, label shown on the page)
WQ_SITES = [
    ("Lady Bird Lake @ Basin [AC]", "Basin (west end)"),
    ("Lady Bird Lake @ 1st St [CC]", "1st St bridge"),
    ("Lady Bird Lake @ Red Bud Isle (EC)", "Red Bud Isle"),
]

MONTHLY_ROWS = 6  # Basin month rows shown in the history table

_STATUS_LABELS = {
    "CLOSED": "Closed",
    "BREFERRED": "Referred for investigation",
}

_session: Optional[requests.Session] = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_local(value: str) -> datetime:
    """Parse a Socrata timestamp, treating bare values as Austin local time.

    Both datasets emit naive timestamps (``2026-09-16T12:10:00.000``) that are
    wall-clock Central time, so attach that zone rather than assuming UTC.
    """
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        return parsed
    try:
        from zoneinfo import ZoneInfo

        return parsed.replace(tzinfo=ZoneInfo("America/Chicago"))
    except Exception:  # pragma: no cover - zoneinfo is stdlib on 3.9+
        return parsed.replace(tzinfo=timezone.utc)


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        headers = {"Accept": "application/json", "User-Agent": "austin311bot/0.1 (environment page)"}
        api_key = os.getenv("AUSTINAPIKEY")
        if api_key:
            headers["X-App-Token"] = api_key
        _session.headers.update(headers)
    return _session


def _socrata_get(url: str, params: dict, label: str) -> list:
    """GET a Socrata dataset, with a couple of retries. Returns [] on failure."""
    session = _get_session()
    last_error = "unknown error"
    for attempt in range(3):
        try:
            resp = session.get(url, params=params, timeout=60)
            if resp.status_code == 200:
                return resp.json()
            last_error = f"HTTP {resp.status_code}"
        except (requests.RequestException, ValueError) as exc:
            last_error = str(exc)
        if attempt < 2:
            time.sleep(2 * (attempt + 1))
    logger.warning(f"{label}: Socrata query failed ({last_error})")
    return []


# ── Water quality ────────────────────────────────────────────────────────────

def _to_ugl(raw: str, unit: str) -> Optional[float]:
    """Normalise a chlorophyll-a result to µg/L."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    unit = (unit or "").strip().upper()
    if unit == "MG/L":          # 0.00239 mg/L → 2.39 µg/L
        return value * 1000.0
    if unit == "MG/M3":         # 1 mg/m³ == 1 µg/L
        return value
    if unit == "UG/L":
        return value
    return None                 # RFU and friends are not concentration


def fetch_water_quality(lookback_days: int = WQ_LOOKBACK_DAYS) -> list:
    """Chlorophyll-a samples for the three Lady Bird Lake index sites."""
    since = (_utc_now() - timedelta(days=lookback_days)).strftime("%Y-%m-%dT00:00:00")
    site_names = ", ".join(f"'{name}'" for name, _ in WQ_SITES)
    rows = _socrata_get(
        WQ_DATASET,
        {
            "$where": (
                f"watershed='{WQ_WATERSHED}' AND parameter='{WQ_PARAMETER}' "
                f"AND site_name in({site_names}) "
                f"AND sample_date > '{since}'"
            ),
            "$select": "sample_date,site_name,result,unit,qc_type,qualifier",
            "$order": "sample_date DESC",
            "$limit": 500,
        },
        "water quality",
    )

    samples = []
    for row in rows:
        # Field splits are QC duplicates of the same bucket of water.
        if (row.get("qc_type") or "").strip():
            continue
        ugl = _to_ugl(row.get("result"), row.get("unit"))
        if ugl is None:
            continue
        try:
            sampled = _parse_local(row["sample_date"])
        except (KeyError, ValueError):
            continue
        samples.append(
            {
                "site": row.get("site_name", ""),
                "ugl": ugl,
                "date": sampled,
                # 'J' means the lab flagged the result as estimated.
                "estimated": "J" in (row.get("qualifier") or ""),
            }
        )
    samples.sort(key=lambda s: s["date"], reverse=True)
    return samples


def summarize_water_quality(samples: list) -> dict:
    """Latest reading per site, plus a monthly Basin history and a 12-month peak."""
    latest_by_site = {}
    for sample in samples:  # already newest first
        latest_by_site.setdefault(sample["site"], sample)

    cutoff = _utc_now() - timedelta(days=365)
    basin = WQ_SITES[0][0]

    monthly = {}
    for sample in samples:
        if sample["site"] != basin or sample["date"] < cutoff:
            continue
        month = sample["date"].strftime("%Y-%m")
        if month not in monthly:  # keep the newest sample within each month
            monthly[month] = sample

    year_samples = [s for s in samples if s["date"] >= cutoff and s["site"] == basin]
    peak = max(year_samples, key=lambda s: s["ugl"]) if year_samples else None

    return {
        "sites": [
            (label, latest_by_site[name])
            for name, label in WQ_SITES
            if name in latest_by_site
        ],
        "latest_date": max((s["date"] for s in samples), default=None),
        "monthly": sorted(monthly.values(), key=lambda s: s["date"], reverse=True)[:MONTHLY_ROWS],
        "peak": peak,
    }


def _rating(ugl: float) -> tuple:
    """(label, colour) for a chlorophyll-a concentration."""
    if ugl > ADVISORY_THRESHOLD:
        return "Advisory level", "#dc2626"
    if ugl >= WHO_THRESHOLD:
        return "Low caution", "#ca8a04"
    return "Clean", "#16a34a"


# ── Spills ───────────────────────────────────────────────────────────────────

def fetch_spills(days: int = SPILL_DAYS) -> list:
    """TCEQ spill incidents in Travis County over the trailing window."""
    since = (_utc_now() - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00")
    rows = _socrata_get(
        SPILLS_DATASET,
        {
            "$where": f"loc_cnty_name='TRAVIS' AND start_dt > '{since}'",
            "$order": "start_dt DESC",
            "$limit": 1000,
        },
        "TCEQ spills",
    )

    seen = set()
    incidents = []
    for row in rows:
        track = row.get("incid_track_num")
        if track in seen:  # the dataset repeats some incidents
            continue
        seen.add(track)
        try:
            started = _parse_local(row["start_dt"])
        except (KeyError, ValueError):
            continue
        incidents.append({**row, "_started": started})
    incidents.sort(key=lambda r: r["_started"], reverse=True)
    return incidents


def summarize_spills(incidents: list, days: int = SPILL_DAYS) -> dict:
    total = len(incidents)
    closed = sum(1 for r in incidents if (r.get("status_cd") or "").upper() == "CLOSED")
    media = Counter((r.get("media_name") or "Unknown").title() for r in incidents)
    materials = Counter((r.get("material_name") or "").strip() or "Unknown" for r in incidents)
    return {
        "days": days,
        "total": total,
        "open": total - closed,
        "closed": closed,
        "per_week": round(total / max(days / 7, 1), 1),
        "media": media.most_common(),
        "materials": materials.most_common(6),
        "recent": incidents[:5],
        "first_date": incidents[-1]["_started"] if incidents else None,
        "last_date": incidents[0]["_started"] if incidents else None,
    }


def _describe_spill(row: dict) -> str:
    """One-line summary: short description, else the structured amount + material."""
    description = re.sub(r"\s+", " ", (row.get("description") or "").strip())
    amount = (row.get("amt_num") or "").strip()
    unit = (row.get("uom_cd") or "").strip().lower()
    material = (row.get("material_name") or "").strip()

    if description and len(description) <= 170:
        return html.escape(description)

    # Long incident narratives (some run to several hundred characters of
    # agency back-and-forth) read badly truncated — prefer amount + material.
    if amount or material:
        parts = " ".join(x for x in (amount, unit) if x)
        text = f"{parts} of {material}" if parts and material else (parts or material)
        return html.escape(f"{text} released")
    if description:
        return html.escape(description[:167].rsplit(" ", 1)[0] + "…")
    return "Details not reported"


# ── Formatting helpers ───────────────────────────────────────────────────────

def _central(value: datetime) -> datetime:
    try:
        from zoneinfo import ZoneInfo

        return value.astimezone(ZoneInfo("America/Chicago"))
    except Exception:  # pragma: no cover - zoneinfo is stdlib on 3.9+
        return value


def _stamp(value: Optional[datetime]) -> str:
    if not value:
        return "—"
    return _central(value).strftime("%b %-d, %Y")


def _stamp_long(value: Optional[datetime]) -> str:
    if not value:
        return "—"
    return _central(value).strftime("%b %-d, %Y %-I:%M %p %Z")


def _num(value: float, decimals: int = 1) -> str:
    return f"{value:,.{decimals}f}"


def _stat(value: str, label: str) -> str:
    return (
        '<div class="stat-card">'
        f'<div class="value">{value}</div>'
        f'<div class="label">{label}</div>'
        "</div>"
    )


def _bullets(items: list, empty: str = "No data reported.") -> str:
    if not items:
        return f"<ul><li>{html.escape(empty)}</li></ul>"
    lis = "".join(f"<li>{item}</li>" for item in items)
    return f"<ul>{lis}</ul>"


def _warn_box(message: str) -> str:
    return f'<div class="info-box warn">⚠️ <strong>Data unavailable:</strong> {html.escape(message)}</div>'


# ── Page ─────────────────────────────────────────────────────────────────────

_STYLE = """
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    :root {
      --bg:           #f8fafc;
      --bg-card:      #ffffff;
      --bg-card-hover:#f1f5f9;
      --border:       #e2e8f0;
      --border-hover: #94a3b8;
      --text:         #1e293b;
      --text-head:    #0f172a;
      --text-tag:     #475569;
      --text-label:   #0f172a;
      --text-desc:    #64748b;
      --text-footer:  #94a3b8;
      --text-footer-a:#64748b;
      --text-footer-hover: #475569;
    }

    html.dark {
      --bg:           #0f1117;
      --bg-card:      #1e2230;
      --bg-card-hover:#252b3b;
      --border:       #2d3348;
      --border-hover: #4a5568;
      --text:         #e2e8f0;
      --text-head:    #f8fafc;
      --text-tag:     #94a3b8;
      --text-label:   #f1f5f9;
      --text-desc:    #64748b;
      --text-footer:  #475569;
      --text-footer-a:#64748b;
      --text-footer-hover: #94a3b8;
    }

    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      align-items: center;
      padding: 2.5rem 1rem 3rem;
      transition: background 0.2s, color 0.2s;
    }

    #theme-toggle {
      position: fixed;
      top: 14px;
      right: 16px;
      z-index: 200;
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 5px 10px;
      font-size: 0.75rem;
      color: var(--text-desc);
      cursor: pointer;
      transition: background 0.15s, border-color 0.15s, color 0.15s;
      user-select: none;
    }
    #theme-toggle:hover { color: var(--text); border-color: var(--border-hover); }

    header {
      text-align: center;
      margin-bottom: 1.5rem;
    }

    header h1 {
      font-size: 2rem;
      font-weight: 700;
      letter-spacing: -0.5px;
      color: var(--text-head);
    }

    header p {
      margin-top: 0.4rem;
      color: var(--text-tag);
      font-size: 0.95rem;
    }

    header .stamp {
      margin-top: 0.7rem;
      font-size: 0.75rem;
      color: var(--text-footer);
    }
    header .stamp b { color: var(--text-desc); font-weight: 600; }

    .back-link {
      display: inline-block;
      margin-bottom: 1rem;
      font-size: 0.85rem;
      color: var(--text-desc);
      text-decoration: none;
    }
    .back-link:hover { color: var(--text-head); text-decoration: underline; }

    .section {
      width: 100%;
      max-width: 860px;
      margin-bottom: 2rem;
    }

    .section h2 {
      font-size: 1.3rem;
      font-weight: 700;
      color: var(--text-head);
      margin-bottom: 0.75rem;
      padding-bottom: 0.4rem;
      border-bottom: 2px solid var(--border);
    }

    .section h3 {
      font-size: 1.05rem;
      font-weight: 600;
      color: var(--text-head);
      margin: 1rem 0 0.5rem;
    }

    .stat-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
      gap: 0.75rem;
      margin-bottom: 1.25rem;
    }

    .stat-card {
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 1rem 1.1rem;
      text-align: center;
    }

    .stat-card .value {
      font-size: 1.6rem;
      font-weight: 700;
      color: var(--text-head);
      letter-spacing: -0.3px;
    }

    .stat-card .label {
      font-size: 0.78rem;
      color: var(--text-desc);
      margin-top: 0.15rem;
    }

    .info-box {
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 1.25rem 1.5rem;
      margin-bottom: 1rem;
      line-height: 1.6;
      font-size: 0.9rem;
    }

    .info-box strong {
      color: var(--text-head);
    }

    .info-box ul {
      margin: 0.5rem 0 0 1.2rem;
    }

    .info-box li {
      margin-bottom: 0.3rem;
    }

    .info-box.warn {
      border-color: #f59e0b;
      background: #fffbeb;
    }
    html.dark .info-box.warn {
      background: #2a2311;
      border-color: #b45309;
    }

    .map-link {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 8px 14px;
      font-size: 0.85rem;
      color: var(--text);
      text-decoration: none;
      transition: background 0.15s, border-color 0.15s;
    }
    .map-link:hover {
      background: var(--bg-card-hover);
      border-color: var(--border-hover);
    }

    .card-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 1rem;
      margin-bottom: 1.5rem;
    }

    .topic-card {
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 1.5rem;
      text-decoration: none;
      color: inherit;
      display: flex;
      flex-direction: column;
      gap: 0.5rem;
      transition: background 0.15s, border-color 0.15s, transform 0.15s;
    }

    .topic-card:hover {
      background: var(--bg-card-hover);
      border-color: var(--border-hover);
      transform: translateY(-2px);
    }

    .topic-card .icon { font-size: 1.8rem; line-height: 1; }
    .topic-card .label { font-size: 1rem; font-weight: 600; color: var(--text-label); }
    .topic-card .desc { font-size: 0.82rem; color: var(--text-desc); line-height: 1.4; }

    .tag {
      display: inline-block;
      font-size: 0.72rem;
      padding: 2px 8px;
      border-radius: 4px;
      background: #dbeafe;
      color: #1e40af;
      font-weight: 500;
    }
    html.dark .tag {
      background: #1e3a5f;
      color: #93c5fd;
    }

    footer {
      margin-top: 2.5rem;
      font-size: 0.75rem;
      color: var(--text-footer);
      text-align: center;
    }
    footer a { color: var(--text-footer-a); text-decoration: none; }
    footer a:hover { color: var(--text-footer-hover); }
"""

_THEME_SCRIPT = """
    const btn = document.getElementById("theme-toggle");
    const html = document.documentElement;
    const dark = localStorage.getItem("theme") === "dark";
    if (dark) { html.classList.add("dark"); btn.textContent = "☀️ Light"; }
    btn.addEventListener("click", () => {
      const isDark = html.classList.toggle("dark");
      btn.textContent = isDark ? "☀️ Light" : "🌙 Dark";
      localStorage.setItem("theme", isDark ? "dark" : "light");
    });
"""

_THRESHOLD_TABLE = """
      <table style="width:100%;border-collapse:collapse;font-size:0.85rem;margin-top:0.5rem;">
        <thead>
          <tr style="border-bottom:1px solid var(--border);color:var(--text-desc);">
            <th style="text-align:left;padding:4px 0;">Standard</th>
            <th style="text-align:left;padding:4px 6px;">Advisory trigger</th>
            <th style="text-align:left;padding:4px 0;">What it means</th>
          </tr>
        </thead>
        <tbody>
          <tr style="border-bottom:1px solid var(--border);">
            <td style="padding:5px 0;font-weight:600;">🇪🇺 EU Bathing Water Directive</td>
            <td style="padding:5px 6px;">~25 µg/L</td>
            <td style="padding:5px 0;color:var(--text-desc);">Post a visible-bloom advisory; test for cyanobacteria toxins before allowing swimming</td>
          </tr>
          <tr style="border-bottom:1px solid var(--border);">
            <td style="padding:5px 0;font-weight:600;">🇺🇸 California (CalEPA)</td>
            <td style="padding:5px 6px;">25 µg/L</td>
            <td style="padding:5px 0;color:var(--text-desc);">State's strictest in the US — triggers sampling for microcystin toxin; beach closure possible at 50 µg/L</td>
          </tr>
          <tr style="border-bottom:1px solid var(--border);">
            <td style="padding:5px 0;font-weight:600;">🌐 WHO Recreational Water</td>
            <td style="padding:5px 6px;">10 µg/L</td>
            <td style="padding:5px 0;color:var(--text-desc);">Low-risk threshold; monitoring recommended above this level</td>
          </tr>
          <tr>
            <td style="padding:5px 0;font-weight:600;">🤠 Austin / Texas</td>
            <td style="padding:5px 6px;">varies</td>
            <td style="padding:5px 0;color:var(--text-desc);">TCEQ issues advisories based on visible bloom + cyanotoxin testing, not a fixed chlorophyll-a number</td>
          </tr>
        </tbody>
      </table>
"""


def _algae_section(wq: dict, errors: list) -> str:
    parts = ["""
  <div class="section">
    <h2>🌿 Harmful Algae &amp; Lake Water Quality</h2>
    <p style="font-size:0.85rem;color:var(--text-desc);margin-bottom:1rem;">
      Scientists measure algae by how much green pigment (chlorophyll) is in the water. The higher the number, the more algae — and the greater the risk of a harmful bloom.
      <a href="https://www.austintexas.gov/department/watershed-protection" target="_blank" rel="noopener" class="tag" style="text-decoration:none;">Source: Austin Watershed Protection ↗</a>
    </p>
"""]

    if errors:
        parts.append(f"    {_warn_box(' '.join(errors))}\n")

    sites = wq.get("sites") or []
    if sites:
        rows = []
        for label, sample in sites:
            rating, color = _rating(sample["ugl"])
            width = min(100.0, sample["ugl"] / ADVISORY_THRESHOLD * 100.0)
            estimate = " (estimated)" if sample["estimated"] else ""
            rows.append(
                '        <div>\n'
                '          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:3px;">\n'
                f"            <span>{html.escape(label)}</span>\n"
                f'            <span style="font-weight:600;color:{color};">{_num(sample["ugl"])} µg/L — {rating}'
                f'{"" if rating == "Clean" else " ⚠️"}</span>\n'
                "          </div>\n"
                '          <div style="background:#e2e8f0;border-radius:4px;height:8px;overflow:hidden;">\n'
                f'            <div style="background:{color};height:100%;width:{width:.1f}%;" '
                f'title="{_num(sample["ugl"])} out of {ADVISORY_THRESHOLD:g} µg/L advisory threshold"></div>\n'
                "          </div>\n"
                f'          <div style="font-size:0.72rem;color:var(--text-desc);margin-top:2px;">sampled {_stamp(sample["date"])}{estimate}</div>\n'
                "        </div>"
            )
        parts.append(
            '    <div class="info-box">\n'
            f'      <strong>🔬 Lady Bird Lake — latest algae levels ({_stamp(wq.get("latest_date"))}):</strong>\n'
            '      <div style="margin-top:0.75rem;display:flex;flex-direction:column;gap:0.6rem;">\n'
            + "\n".join(rows)
            + "\n      </div>\n"
            f'      <p style="margin-top:0.75rem;font-size:0.82em;color:var(--text-desc);">Bar shows % of advisory trigger level ({ADVISORY_THRESHOLD:g} µg/L). '
            'Austin Watershed Protection samples these three lake sites roughly every 6–8 weeks; "estimated" marks lab results flagged as approximate.</p>\n'
            "    </div>\n"
        )

    monthly = wq.get("monthly") or []
    if monthly:
        body = []
        for sample in monthly:
            rating, color = _rating(sample["ugl"])
            body.append(
                '          <tr style="border-bottom:1px solid var(--border);">\n'
                f'            <td style="padding:5px 0;">{sample["date"].strftime("%b %Y")}</td>\n'
                f'            <td style="text-align:right;">{_num(sample["ugl"])} µg/L</td>\n'
                f'            <td style="text-align:right;color:{color};font-weight:600;">{rating}</td>\n'
                "          </tr>"
            )
        parts.append(
            '    <div class="info-box">\n'
            "      <strong>📅 Algae levels over the past year — Lady Bird Lake (basin):</strong>\n"
            '      <table style="width:100%;border-collapse:collapse;margin-top:0.6rem;font-size:0.88rem;">\n'
            '        <thead>\n'
            '          <tr style="border-bottom:1px solid var(--border);color:var(--text-desc);">\n'
            '            <th style="text-align:left;padding:4px 0;">Month</th>\n'
            '            <th style="text-align:right;padding:4px 0;">Level</th>\n'
            '            <th style="text-align:right;padding:4px 0;">Rating</th>\n'
            "          </tr>\n"
            "        </thead>\n"
            "        <tbody>\n"
            + "\n".join(body)
            + "\n        </tbody>\n      </table>\n"
            '      <p style="margin-top:0.6rem;font-size:0.82em;color:var(--text-desc);">One row per sampling month — the latest basin reading in that month. Rows are missing where the lake was not sampled.</p>\n'
            "    </div>\n"
        )

    peak = wq.get("peak")
    compare = ""
    if peak:
        if peak["ugl"] >= ADVISORY_THRESHOLD:
            verdict = "at or above the 25 µg/L level where California and most of Europe issue public advisories."
        elif peak["ugl"] >= WHO_THRESHOLD:
            verdict = "above the WHO 10 µg/L low-risk threshold, but below the 25 µg/L advisory trigger used in California and the EU."
        else:
            verdict = "below both the WHO 10 µg/L low-risk threshold and the 25 µg/L advisory trigger used in California and the EU."
        compare = (
            '<p style="margin:0.5rem 0 0.4rem;">Chlorophyll-a above <strong>25 µg/L</strong> is where regulators around the world start issuing public health advisories. '
            f'The highest basin reading in the past 12 months was <strong>{_num(peak["ugl"])} µg/L</strong> on {_stamp(peak["date"])}, {verdict}</p>'
        )

    parts.append(
        "    <div class=\"info-box\">\n"
        "      <strong>🌍 How does Austin compare to global safety standards?</strong>\n"
        f"      {compare}\n"
        + _THRESHOLD_TABLE
        + "\n    </div>\n"
    )

    parts.append("""
    <div class="info-box">
      <strong>🐕 What to watch for in Austin:</strong> Lady Bird Lake sees its worst algae in late summer and early fall when the water is warm and slow-moving. Blue-green algae (cyanobacteria) looks like a green paint spill or pea soup on the surface. Keep pets out of any water with a visible scum — cyanotoxins can kill a dog within hours of exposure. Austin Parks &amp; Recreation posts advisories at park entrances and on the city website when blooms are detected.
    </div>

    <p style="font-size:0.8rem;color:var(--text-desc);margin-top:0.5rem;">
      Data: <a href="https://data.austintexas.gov/resource/5tye-7ray" target="_blank" rel="noopener" style="color:inherit;text-decoration:underline;">Austin Water Quality Sampling dataset</a> (chlorophyll-a, Lady Bird Lake watershed; lab results reported in mg/L and converted here to µg/L). µg/L = micrograms per liter, a standard unit for algae pigment concentration.
    </p>
  </div>
""")
    return "".join(parts)


def _spills_section(spills: dict, window: tuple) -> str:
    since, until = window
    parts = [f"""
  <div class="section">
    <h2>⚠️ Environmental Spills</h2>
    <p style="font-size:0.85rem;color:var(--text-desc);margin-bottom:1rem;">
      Chemical, fuel, and waste spills reported to the Texas Commission on Environmental Quality (TCEQ) in Austin &amp; Travis County.
      <span class="tag">Source: TCEQ Emergency Response</span>
    </p>
"""]

    if not spills.get("total"):
        parts.append(
            f"    {_warn_box(f'No TCEQ spill records returned for {_stamp(since)} – {_stamp(until)}.')}\n"
        )
    else:
        parts.append(
            '    <div class="stat-grid">\n'
            + _stat(f'{spills["total"]:,}', f'Incidents ({_stamp(since)} – {_stamp(until)})')
            + _stat(f'{spills["open"]:,}', "Open / referred")
            + _stat(f'{spills["closed"]:,}', "Closed / resolved")
            + _stat(f'{spills["per_week"]:g}', "Incidents per week")
            + "    </div>\n"
        )

        media = [
            f"{icon} {html.escape(name)} — {count:,} incident{'s' if count != 1 else ''}"
            for name, count in spills.get("media") or []
            for icon in [{"Waste": "🗑️", "Water": "💧", "Air": "🌫️"}.get(name, "•")]
        ]
        parts.append(
            '    <div class="info-box">\n'
            "      <strong>Impacted media:</strong>\n"
            f"      {_bullets(media, 'No media reported.')}\n"
            "    </div>\n"
        )

        materials = []
        for name, count in spills.get("materials") or []:
            label = "❓ Unknown material" if name == "Unknown" else f"⚗️ {html.escape(name)}"
            materials.append(f"{label} — {count:,} incident{'s' if count != 1 else ''}")
        parts.append(
            '    <div class="info-box">\n'
            "      <strong>Reported materials:</strong>\n"
            f"      {_bullets(materials, 'No materials reported.')}\n"
            "    </div>\n"
        )

        recent = []
        for row in spills.get("recent") or []:
            status = _STATUS_LABELS.get((row.get("status_cd") or "").upper(), (row.get("status_cd") or "Unknown").title())
            city = (row.get("near_city_name") or "").strip().title()
            zip_code = (row.get("loc_zip_txt") or "").strip()
            where = ", ".join(x for x in (f"{city}" if city and city != "Austin" else "", zip_code) if x)
            location = f" ({html.escape(where)})" if where else ""
            recent.append(
                f"<strong>{_stamp(row['_started'])}</strong> — {_describe_spill(row)}{location} "
                f'<em style="color:var(--text-desc)">[{html.escape(status)}]</em>'
            )
        parts.append(
            '    <div class="info-box">\n'
            "      <strong>📋 Recent incidents:</strong>\n"
            f"      {_bullets(recent, 'No incidents in this window.')}\n"
            "    </div>\n"
        )

    parts.append(f"""
    <p style="font-size:0.8rem;color:var(--text-desc);margin-top:0.5rem;">
      Data: <a href="https://data.texas.gov/resource/xagr-a3x2" target="_blank" rel="noopener" style="color:inherit;text-decoration:underline;">TCEQ Emergency Response Spills dataset</a> — statewide data filtered to Travis County incidents filed {_stamp(since)} – {_stamp(until)}. Duplicate incident records are collapsed.
    </p>
  </div>
""")
    return "".join(parts)


def _water_section(stats: Optional[dict], days_back: int, water_error: Optional[str]) -> str:
    parts = [f"""
  <div class="section">
    <h2>💧 Water Conservation Violations</h2>
    <p style="font-size:0.85rem;color:var(--text-desc);margin-bottom:1rem;">
      Reports of sprinkler misuse, leaks, and water waste investigated by Austin Water, over the last {days_back} days.
      <span class="tag">Service code: WWREPORT</span>
    </p>
"""]
    if water_error:
        parts.append(f"    {_warn_box(water_error)}\n")

    if stats and stats.get("total"):
        parts.append(
            '    <div class="stat-grid">\n'
            + _stat(f'{stats["total"]:,}', f'Reports (last {days_back} days)')
            + _stat(f'{stats["open"]:,}', "Open investigations")
            + _stat(f'{stats["closed"]:,}', "Closed / resolved")
            + _stat(f'{round(stats["total"] / max(days_back, 1), 1):g}', "Reports per day")
            + "    </div>\n"
        )
        parts.append(
            '    <div class="info-box">\n'
            "      <strong>📋 Investigation Outcomes:</strong>\n"
            f"      {_bullets([f'{html.escape(label)}: {count:,}' for label, count in stats.get('outcomes') or []], 'No outcomes recorded.')}\n"
            "    </div>\n"
        )
        parts.append(
            '    <div class="info-box">\n'
            "      <strong>🚿 Violation Types:</strong>\n"
            f"      {_bullets([f'{html.escape(label)}: {count:,}' for label, count in stats.get('violation_types') or []], 'No violation types recorded.')}\n"
            "    </div>\n"
        )
        parts.append(
            '    <div class="info-box">\n'
            "      <strong>📍 Most Reported Streets:</strong>\n"
            f"      {_bullets([f'{html.escape(street)}: {count:,}' for street, count in stats.get('top_streets') or []], 'No street hotspots.')}\n"
            "    </div>\n"
        )
    elif not water_error:
        parts.append(
            f"    {_warn_box(f'No water conservation reports returned for the last {days_back} days.')}\n"
        )

    parts.append("""
    <a class="map-link" href="../water/" target="_blank">
      🗺️ Open Water Conservation Map →
    </a>
  </div>
""")
    return "".join(parts)


_RELATED_SECTION = """
  <div class="section">
    <h2>🔗 Related Topics</h2>
    <div class="card-grid">
      <a class="topic-card" href="../water/">
        <span class="icon">💧</span>
        <span class="label">Water Conservation Map</span>
        <span class="desc">Interactive map of water conservation violation reports</span>
      </a>
      <a class="topic-card" href="../storm/">
        <span class="icon">🌧️</span>
        <span class="label">Storm &amp; Drainage</span>
        <span class="desc">Storm debris, drainage &amp; flooding reports</span>
      </a>
      <a class="topic-card" href="../parks/">
        <span class="icon">🏞️</span>
        <span class="label">Parks</span>
        <span class="desc">Park maintenance requests including water features</span>
      </a>
      <a class="topic-card" href="../trees/">
        <span class="icon">🌳</span>
        <span class="label">Urban Trees &amp; Canopy</span>
        <span class="desc">Right-of-way tree issues, hazardous limbs, and park tree reports</span>
      </a>
    </div>
  </div>
"""


def generate_environment_page(days_back: int = 90) -> tuple:
    """Generate the environment overview page.

    Args:
        days_back: window for the Open311 water-conservation section.

    Returns:
        (BytesIO buffer with HTML, summary message). ``None`` buffer when every
        source failed.
    """
    now = _utc_now()
    spill_since = now - timedelta(days=SPILL_DAYS)

    # ── Water quality ──
    wq_errors = []
    samples = fetch_water_quality()
    if not samples:
        wq = {}
        wq_errors.append("Austin water quality samples could not be retrieved.")
    else:
        wq = summarize_water_quality(samples)

    # ── Spills ──
    incidents = fetch_spills()
    spills = summarize_spills(incidents)

    # ── Water conservation (Open311 + cache fallback) ──
    water_stats = None
    water_error = None
    try:
        from waterconservation.water_conservation_bot import get_water_conservation_stats

        water_stats = get_water_conservation_stats(days_back)
    except Exception as exc:  # noqa: BLE001 - one bad source must not kill the page
        logger.warning(f"Water conservation stats failed: {exc}")
        water_error = "Austin 311 water conservation data could not be retrieved."

    if not samples and not incidents and not (water_stats and water_stats.get("total")):
        return None, "❌ Environment page: every data source returned nothing."

    stamp = _stamp_long(now)
    latest_wq = wq.get("latest_date")
    source_note = (
        f'Water quality sampled {_stamp(latest_wq)}'
        if latest_wq
        else "Water quality unavailable"
    )
    header_stamp = (
        f"Last updated <b>{stamp}</b> · {html.escape(source_note)} · "
        f"spills {_stamp(spill_since)} – {_stamp(now)} · "
        f"311 water conservation last {days_back} days"
    )

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Austin 311 — Environment</title>
  {og_meta_tags("environment")}
  <style>{_STYLE}  </style>
</head>
<body>
  <button id="theme-toggle" aria-label="Toggle dark mode">🌙 Dark</button>

  <header>
    <a class="back-link" href="../">← Austin 311</a>
    <h1>🌍 Environment</h1>
    <p>Water conservation, environmental spills, harmful algae &amp; surface water quality</p>
    <p class="stamp">{header_stamp}</p>
  </header>
{_algae_section(wq, wq_errors)}{_spills_section(spills, (spill_since, now))}{_water_section(water_stats, days_back, water_error)}{_RELATED_SECTION}
  <footer>
    Data: <a href="https://311.austintexas.gov" target="_blank" rel="noopener">Austin 311</a>
    &nbsp;·&nbsp;
    <a href="https://data.austintexas.gov" target="_blank" rel="noopener">Open Data Portal</a>
    &nbsp;·&nbsp;
    <a href="https://github.com/seanatwork" target="_blank" rel="noopener">Vibecoded with ❤️</a>
  </footer>

  <script>{_THEME_SCRIPT}  </script>
</body>
</html>
"""

    buffer = io.BytesIO(page.encode("utf-8"))
    buffer.seek(0)

    peak = wq.get("peak")
    peak_note = ""
    if peak:
        peak_note = f"; 12-month basin peak {_num(peak['ugl'])} µg/L on {_stamp(peak['date'])}"
    water_note = (
        f"{water_stats['total']} reports in {days_back}d ({water_stats['open']} open)"
        if water_stats and water_stats.get("total")
        else "water conservation unavailable"
    )
    summary = (
        f"🌍 Environment page — water quality sampled {_stamp(latest_wq)}{peak_note} · "
        f"TCEQ spills {spills['total']} in {SPILL_DAYS}d ({spills['open']} open) · "
        f"water conservation {water_note}"
    )
    return buffer, summary
