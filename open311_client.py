"""
Shared Open311 API retry logic used by all map-generating modules.

Improvements over per-module implementations:
- Respects the Retry-After response header when the server sends one
- 6 retries instead of 3 for rate-limit errors
- Exponential backoff starts at 15s for 429s (vs 10s before)
"""
import time
import logging
import requests

logger = logging.getLogger(__name__)

MAX_RETRIES = 8
RETRY_DELAY = 2.0
RETRYABLE_HTTP_CODES = {423, 429, 500, 502, 503, 504}
RETRYABLE_ERRORS = (
    requests.exceptions.Timeout,
    requests.exceptions.ConnectionError,
)


def open311_get(
    session: requests.Session,
    url: str,
    params: dict,
    retries: int = 0,
) -> list:
    """GET request to the Open311 API with retry/backoff logic.

    On a 429, checks the Retry-After response header first; falls back to
    exponential backoff (15s, 30s, 60s, 120s, 240s, 480s) if not present.
    Retries up to MAX_RETRIES times before re-raising.
    """
    try:
        resp = session.get(url, params=params, timeout=45)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code
        if status in RETRYABLE_HTTP_CODES and retries < MAX_RETRIES:
            retry_after = e.response.headers.get("Retry-After")
            if retry_after:
                try:
                    delay = float(retry_after)
                except ValueError:
                    delay = 15.0 * (2 ** retries)
            elif status in {423, 429}:
                delay = 15.0 * (2 ** retries)
            else:
                delay = RETRY_DELAY * (2 ** retries)
            logger.warning(
                f"HTTP {status}, retrying in {delay:.1f}s ({retries + 1}/{MAX_RETRIES})"
            )
            time.sleep(delay)
            return open311_get(session, url, params, retries + 1)
        raise
    except RETRYABLE_ERRORS as e:
        if retries < MAX_RETRIES:
            delay = RETRY_DELAY * (2 ** retries)
            logger.warning(
                f"Request failed ({e}), retrying in {delay:.1f}s ({retries + 1}/{MAX_RETRIES})"
            )
            time.sleep(delay)
            return open311_get(session, url, params, retries + 1)
        raise


# ── Telegram deep-link helper for map popups ──────────────────────────────────

TELEGRAM_BOT_USERNAME = "austin311bot"


def telegram_subscribe_link(lat: float, lon: float, alert_code: str = "311") -> str:
    """Build a t.me deep link that pre-fills the alerts subscription flow.

    alert_code is "311" for nearby_311 alerts or "animal" for animal_nearby.
    Lat/lon are encoded as signed integer microdegrees so the payload only
    contains characters Telegram permits ([A-Za-z0-9_-]).
    """
    lat_int = int(round(lat * 1_000_000))
    lon_int = int(round(lon * 1_000_000))
    return f"https://t.me/{TELEGRAM_BOT_USERNAME}?start=sub_{alert_code}_{lat_int}_{lon_int}"


def subscribe_popup_html(lat: float, lon: float, alert_code: str = "311") -> str:
    """HTML snippet to embed at the bottom of a Folium map popup."""
    href = telegram_subscribe_link(lat, lon, alert_code)
    return (
        '<div style="margin-top:6px;padding-top:6px;border-top:1px solid #e5e7eb;">'
        f'<a href="{href}" target="_blank" '
        'style="color:#0088cc;font-size:12px;text-decoration:none;font-weight:600;">'
        '🔔 Alert me near here →</a></div>'
    )


# ── Open Graph / Twitter card metadata for each docs/<slug>/ page ─────────────

SITE_BASE_URL = "https://austin311.com"

# (slug → (title, description)). slug "" is the landing page.
_OG_PAGES = {
    "":            ("Austin 311 — Real data on what your city is doing",
                    "Live maps and trends across 311 reports, APD crime, crashes, parking, courts, and more — refreshed daily. Subscribe to alerts on Telegram."),
    "animal":      ("Austin 311 — Animal Services Map",
                    "Loose dogs, bites, and wildlife reports across Austin — last 30/60/90 days, refreshed daily."),
    "bicycle":     ("Austin 311 — Bicycle Infrastructure Map",
                    "Bike lane issues, debris hazards, and obstructions across Austin's bike network — last 30/60/90 days."),
    "budget":      ("Austin 311 — General Fund Budget FY2026",
                    "Where Austin's $1.6B General Fund actually goes — by department, with line-item drill-down."),
    "childcare":   ("Austin 311 — Child Care Compliance",
                    "Austin childcare facility inspections — search by name and live compliance data."),
    "court":       ("Austin 311 — Municipal & Community Court Caseloads",
                    "Austin court cases by charge type, demographics, and FY2022–2026 trends. Live data."),
    "crashes":     ("Austin 311 — Live Crash Map",
                    "Live APD crash reports — fatal, injury, and minor crashes across Austin updated continuously."),
    "911":         ("Austin 311 — 911 Response Time Trends",
                    "APD dispatch response times across all years, priorities, and council districts. Updated quarterly."),
    "crime":       ("Austin 311 — APD Crime by District",
                    "30/60/90-day APD incident counts by Austin council district."),
    "crime/hatecrime": ("Austin 311 — Hate Crime Incidents",
                    "APD hate crime incidents 2017–present. Yearly trends, bias breakdown, and offender demographics."),
    "fun":         ("Austin 311 — Fun Data",
                    "Bar of the month, coyote sightings, and other live Austin curiosities."),
    "graffiti":    ("Austin 311 — Graffiti Abatement Map",
                    "Graffiti abatement requests across Austin, with response-time trends and hotspot detection."),
    "homeless":    ("Austin 311 — Encampment & Homeless 311 Reports",
                    "311 reports mentioning encampments, tents, and homelessness across Parks, ROW, Debris, and Drainage."),
    "noise":       ("Austin 311 — Noise Complaints Map",
                    "Noise complaints, outdoor venue/music issues, and fireworks reports across Austin."),
    "parking":     ("Austin 311 — Parking Enforcement Map",
                    "Citywide 311 parking enforcement reports — last 30/60/90 days, refreshed daily."),
    "parks":       ("Austin 311 — Parks Maintenance Map",
                    "Park maintenance requests across Austin — grounds, plumbing, electrical, and building issues."),
    "restaurants": ("Austin 311 — Restaurant Inspection Search",
                    "Austin Public Health restaurant inspection scores — search by name with live data."),
    "traffic":     ("Austin 311 — Traffic & Infrastructure Map",
                    "Potholes, signal issues, and sidewalk requests citywide — last 30/60/90 days."),
    "water":       ("Austin 311 — Water Conservation Violations",
                    "Water conservation violation reports across Austin."),
}


def og_meta_tags(slug: str = "") -> str:
    """Return Open Graph + Twitter-card meta tags for a docs/<slug>/ page.

    Pass "" (default) for the landing page. Tags assume a shared OG image at
    /og-default.png; per-page overrides can be added by extending _OG_PAGES.
    """
    title, desc = _OG_PAGES.get(slug, _OG_PAGES[""])
    page_path = f"/{slug}/" if slug else "/"
    page_url = f"{SITE_BASE_URL}{page_path}"
    image_url = f"{SITE_BASE_URL}/og-default.png"
    return (
        '<meta property="og:type" content="website" />\n'
        f'<meta property="og:title" content="{title}" />\n'
        f'<meta property="og:description" content="{desc}" />\n'
        f'<meta property="og:url" content="{page_url}" />\n'
        f'<meta property="og:image" content="{image_url}" />\n'
        '<meta property="og:image:width" content="1200" />\n'
        '<meta property="og:image:height" content="630" />\n'
        '<meta property="og:site_name" content="Austin 311" />\n'
        '<meta name="twitter:card" content="summary_large_image" />\n'
        f'<meta name="twitter:title" content="{title}" />\n'
        f'<meta name="twitter:description" content="{desc}" />\n'
        f'<meta name="twitter:image" content="{image_url}" />\n'
        f'<meta name="description" content="{desc}" />'
    )
