"""
Infrastructure & Transportation — data layer and formatters.

Queries Austin Open311 API live across road, signal, and infrastructure service codes.
Provides hotspot (by street), complaint type stats, and response time analysis.
"""

import json
import time
import logging
import requests
import os
import io
from datetime import datetime, timezone, timedelta
from open311_client import open311_get, og_meta_tags
from typing import Optional

logger = logging.getLogger(__name__)

OPEN311_BASE_URL = "https://311.austintexas.gov/open311/v2"
TIMEOUT = 45
MAX_RETRIES = 8
RETRY_DELAY = 1.0
MAX_PAGES = 10

API_KEY = os.getenv("AUSTINAPIKEY")

SERVICE_CODES = {
    "SBPOTREP": "Pothole Repair",
    "TRASIGMA": "Traffic Signal - Maintenance",
    "STREETL2": "Street Light Issue",
    "SBDEBROW": "Debris in Street",
    "ATTRSIMO": "Traffic Signal - Modification",
    "SIGNSTRE": "Street Name Sign Maintenance",
    "OBSINTTR": "Obstruction at Intersection",
    "SBSIDERE": "Sidewalk Repair",
    "SBSTRES":  "Street Resurfacing",
    "OBSTMIDB": "Obstruction in Right of Way",
    "ZZARSTSW": "Street Sweeping",
    "DRCHANEL": "Drainage/Creek Issues",
    "ATCOCIRW": "Construction in Right of Way",
    "PWTRISRW": "Tree Issue - Right of Way",
    "SBGENRL":  "Street & Bridge Miscellaneous",
    "SIGNNEWT": "Traffic Sign - New",
    "TRASIGNE": "Traffic Signal - New",
    "TPPECRNE": "Pedestrian Crossing - New/Modify",
}

RETRYABLE_ERRORS = (
    requests.exceptions.Timeout,
    requests.exceptions.ConnectionError,
)

_session: Optional[requests.Session] = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        headers = {
            "Accept": "application/json",
            "User-Agent": "austin311bot/0.1 (Open311 traffic queries)",
        }
        if API_KEY:
            headers["X-Api-Key"] = API_KEY
        _session.headers.update(headers)
    return _session


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_central_time() -> str:
    """Return current time formatted in US Central Time (CDT/CST)."""
    utc_now = datetime.now(timezone.utc)
    month = utc_now.month
    is_dst = 3 <= month <= 11
    offset_hours = -5 if is_dst else -6
    central_now = utc_now + timedelta(hours=offset_hours)
    tz_abbr = "CDT" if is_dst else "CST"
    return central_now.strftime(f"%Y-%m-%d %I:%M %p {tz_abbr}")


def _isoformat_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _looks_truncated(text: str | None) -> bool:
    """Return True if a text field appears to have been cut off by the API."""
    if not text:
        return False
    t = text.rstrip()
    if len(t) < 200:
        return False
    return t[-1] not in ".!?,;: \t\n"


def _fetch_detail(service_request_id: str) -> dict:
    """Fetch a single ticket by ID to get untruncated field values."""
    session = _get_session()
    url = f"{OPEN311_BASE_URL}/requests/{service_request_id}.json"
    try:
        resp = session.get(url, params={"extensions": "true"}, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list) and data:
            return data[0]
        if isinstance(data, dict):
            return data
    except Exception as e:
        logger.debug(f"Detail fetch failed for {service_request_id}: {e}")
    return {}


def _make_request(params: dict) -> list:
    return open311_get(_get_session(), f"{OPEN311_BASE_URL}/requests.json", params)


def _fetch_code(service_code: str, days_back: int) -> list:
    """Fetch all requests for one service code with pagination.

    After the bulk fetch, any record with a truncated description or
    status_notes is re-fetched individually using the single-ticket endpoint.
    """
    end = _utc_now()
    start = end - timedelta(days=days_back)
    all_records: list = []
    seen_ids: set = set()
    page = 1

    while page <= MAX_PAGES:
        params = {
            "service_code": service_code,
            "start_date": _isoformat_z(start),
            "end_date": _isoformat_z(end),
            "per_page": 100,
            "page": page,
            "extensions": "true",
        }
        records = _make_request(params)
        if not records:
            break

        for r in records:
            sid = r.get("service_request_id")
            if sid and sid not in seen_ids:
                seen_ids.add(sid)
                r["_service_label"] = SERVICE_CODES.get(service_code, service_code)
                r["_service_code"] = service_code
                all_records.append(r)

        if len(records) < 100:
            break

        page += 1
        time.sleep(1.0 if API_KEY else 2.0)

    # Re-fetch individual records whose text fields look truncated
    for i, r in enumerate(all_records):
        if _looks_truncated(r.get("description")) or _looks_truncated(r.get("status_notes")):
            sid = r.get("service_request_id")
            if not sid:
                continue
            detail = _fetch_detail(sid)
            if detail:
                for field in ("description", "status_notes"):
                    if detail.get(field):
                        r[field] = detail[field]
                if detail.get("attributes"):
                    r["attributes"] = detail["attributes"]
            time.sleep(0.25 if API_KEY else 0.5)

    return all_records


def fetch_all_traffic_complaints(days_back: int = 90, use_cache: bool = True) -> list:
    """Fetch complaints across all traffic/infrastructure service codes with optional caching."""
    from open311_cache import init_cache, get_cached_records, cache_records, get_last_fetch_date

    CATEGORY = "traffic"

    # Initialize cache if using
    if use_cache:
        init_cache()
        cached_records = get_cached_records(service_codes=list(SERVICE_CODES.keys()))
        cached_ids = {r.get("service_request_id") for r in cached_records}
        logger.info(f"Loaded {len(cached_records)} cached traffic records")

        # Check if cache is fresh (less than 6 days old)
        last_fetch = get_last_fetch_date(service_codes=list(SERVICE_CODES.keys()))
        if last_fetch:
            cache_age = _utc_now() - last_fetch
            if cache_age < timedelta(days=6) and len(cached_records) > 0:
                logger.info(f"Cache is fresh ({cache_age.days} days old), using cached data")
                return cached_records
    else:
        cached_records = []
        cached_ids = set()

    all_records = []
    seen_ids = cached_ids.copy()
    new_records = []

    for code in SERVICE_CODES:
        try:
            records = _fetch_code(code, days_back)
            # Filter out already-cached records
            unique_records = [r for r in records if r.get("service_request_id") not in seen_ids]
            for r in unique_records:
                seen_ids.add(r.get("service_request_id"))
                new_records.append(r)
            all_records.extend(unique_records)
            logger.debug(f"{code}: {len(unique_records)} new records")
        except Exception as e:
            logger.warning(f"Failed to fetch {code}: {e}")
        time.sleep(1.0 if API_KEY else 2.0)

    # Combine with cached records
    if use_cache and cached_records:
        all_records = cached_records + [r for r in all_records if r.get("service_request_id") not in cached_ids]

    # Cache new records
    if use_cache and new_records:
        cache_records(CATEGORY, new_records)
        logger.info(f"Cached {len(new_records)} new traffic records")

    return all_records


# High-volume codes only — keeps API calls to 4 and results meaningful
BACKLOG_CODES = {
    "SBPOTREP": "Pothole Repair",
    "TRASIGMA": "Traffic Signal",
    "STREETL2": "Street Light",
    "SBDEBROW": "Debris in Street",
}


# =============================================================================
# INFRA BACKLOG
# =============================================================================

def get_infra_backlog() -> dict:
    """Fetch open infrastructure complaints across the 4 highest-volume codes."""
    now = _utc_now()
    start = now - timedelta(days=90)
    type_counts: dict = {}
    oldest: list = []  # (days_open, label, addr, ticket_id)

    for code, label in BACKLOG_CODES.items():
        try:
            params = {
                "service_code": code,
                "status": "open",
                "start_date": _isoformat_z(start),
                "end_date": _isoformat_z(now),
                "per_page": 100,
                "page": 1,
            }
            records = _make_request(params)
            type_counts[label] = len(records)
            for r in records:
                dt_str = r.get("requested_datetime") or ""
                addr = (r.get("address") or "Unknown").replace(", Austin", "").strip()
                ticket_id = r.get("service_request_id") or ""
                try:
                    req = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                    oldest.append(((now - req).days, label, addr, ticket_id))
                except (ValueError, TypeError):
                    pass
        except Exception as e:
            logger.warning(f"backlog fetch {code}: {e}")

    return {
        "total_open": sum(type_counts.values()),
        "type_counts": type_counts,
        "oldest_10": sorted(oldest, key=lambda x: -x[0])[:10],
    }


def format_infra_backlog(data: dict) -> str:
    """Returns the summary text. Oldest tickets are rendered as buttons by the handler."""
    total_open = data.get("total_open", 0)
    type_counts = data.get("type_counts", {})

    if not total_open:
        return "✅ No open infrastructure complaints in the last 90 days."

    msg = "📋 *Infrastructure Backlog*\n"
    msg += f"_{total_open} open complaints · last 90 days_\n\n"

    msg += "*Open by type:*\n"
    max_count = max(type_counts.values()) if type_counts else 1
    for label, count in sorted(type_counts.items(), key=lambda x: -x[1]):
        bar = "█" * min(10, round(count / max_count * 10))
        msg += f"  {bar} *{label}*: {count}\n"

    msg += "\n\n_Source: [Austin Open311 API](https://311.austintexas.gov/open311/v2)_"
    return msg


# =============================================================================
# BROKEN TRAFFIC SIGNALS
# =============================================================================

def get_signal_maintenance(days_back: int = 90) -> dict:
    """Fetch TRASIGMA records and surface broken signals still waiting for repair."""
    now = _utc_now()
    start = now - timedelta(days=days_back)
    params = {
        "service_code": "TRASIGMA",
        "start_date": _isoformat_z(start),
        "end_date": _isoformat_z(now),
        "per_page": 100,
        "page": 1,
    }
    records = _make_request(params)

    open_records = []
    closed_count = 0
    closed_days: list = []

    for r in records:
        status = (r.get("status") or "").lower()
        requested_str = r.get("requested_datetime") or ""
        updated_str = r.get("updated_datetime") or ""
        if not requested_str:
            continue

        if status == "closed":
            closed_count += 1
            try:
                req = datetime.fromisoformat(requested_str.replace("Z", "+00:00"))
                upd = datetime.fromisoformat(updated_str.replace("Z", "+00:00"))
                d = (upd - req).days
                if 0 <= d <= 365:
                    closed_days.append(d)
            except (ValueError, TypeError):
                pass
        else:
            try:
                req = datetime.fromisoformat(requested_str.replace("Z", "+00:00"))
                days_waiting = (now - req).days
                addr = (r.get("address") or "Unknown").replace(", Austin, TX", "").replace(", Austin", "").strip()
                ticket_id = r.get("service_request_id") or ""
                open_records.append((days_waiting, addr, ticket_id))
            except (ValueError, TypeError):
                pass

    open_records.sort(key=lambda x: -x[0])
    avg_wait = round(sum(x[0] for x in open_records) / len(open_records), 1) if open_records else None
    avg_fix = round(sum(closed_days) / len(closed_days), 1) if closed_days else None

    return {
        "total": len(records),
        "open_records": open_records,
        "closed_count": closed_count,
        "avg_wait_days": avg_wait,
        "avg_fix_days": avg_fix,
        "days_back": days_back,
    }


def format_signal_maintenance(data: dict) -> str:
    total = data.get("total", 0)
    open_records = data.get("open_records", [])
    closed_count = data.get("closed_count", 0)
    avg_wait = data.get("avg_wait_days")
    avg_fix = data.get("avg_fix_days")
    days_back = data.get("days_back", 90)

    if not total:
        return "✅ No traffic signal maintenance requests in the last 90 days."

    open_count = len(open_records)
    msg = "🚦 *Broken Traffic Signals*\n"
    msg += f"_Last {days_back} days · {total} reported · {open_count} still broken · {closed_count} fixed_\n\n"

    if avg_fix is not None:
        if avg_fix <= 7:
            verdict = "🟢 City is fixing signals quickly"
        elif avg_fix <= 21:
            verdict = "🟡 Repair times are moderate"
        else:
            verdict = "🔴 Signals are waiting a long time"
        msg += f"{verdict}\n"
        msg += f"⏱ *Avg fix time:* {avg_fix} days\n"

    if avg_wait is not None:
        msg += f"⏳ *Avg current wait:* {avg_wait} days\n"

    if open_records:
        msg += "\n*Still broken — longest waiting:*\n"
        for days_waiting, addr, ticket_id in open_records[:8]:
            age_emoji = "🔴" if days_waiting >= 30 else "🟡" if days_waiting >= 14 else "🟢"
            msg += f"{age_emoji} *{days_waiting}d* — {addr}\n"

    msg += "\n_Source: [Austin Open311 API](https://311.austintexas.gov/open311/v2)_"
    return msg


# =============================================================================
# INTERACTIVE MAP GENERATION
# =============================================================================

# Groups the 18 service codes into 5 user-facing categories for the map filter
CATEGORY_GROUPS = {
    "roads": {
        "label": "Roads & Pavement",
        "codes": {"SBPOTREP", "SBSTRES", "SBGENRL", "ZZARSTSW"},
    },
    "signals": {
        "label": "Traffic Signals & Signs",
        "codes": {"TRASIGMA", "ATTRSIMO", "TRASIGNE", "SIGNSTRE", "SIGNNEWT"},
    },
    "pedestrians": {
        "label": "Pedestrians & Bikes",
        "codes": {"SBSIDERE", "TPPECRNE", "OBSINTTR"},
    },
    "obstructions": {
        "label": "Obstructions & Debris",
        "codes": {"SBDEBROW", "OBSTMIDB", "ATCOCIRW"},
    },
    "utilities": {
        "label": "Utilities & Environment",
        "codes": {"STREETL2", "DRCHANEL", "PWTRISRW"},
    },
}

# Reverse map: service_code → category key
_CODE_TO_CATEGORY = {
    code: cat_key
    for cat_key, cat in CATEGORY_GROUPS.items()
    for code in cat["codes"]
}


def _get_category(service_code: str) -> str:
    return _CODE_TO_CATEGORY.get(service_code, "roads")  # default to roads for unknowns


def fetch_traffic_with_coords(days_back: int = 30) -> dict:
    """Fetch all traffic/infrastructure reports and filter to those with valid coordinates.

    Returns both open AND closed requests with location data for mapping.
    """
    records = fetch_all_traffic_complaints(days_back)

    # Filter to records with valid coordinates
    located = []
    for r in records:
        lat = r.get("lat")
        lon = r.get("long")
        if lat and lon:
            try:
                lat_f = float(lat)
                lon_f = float(lon)
                # Basic validation: should be in Austin area
                if 30.0 <= lat_f <= 30.5 and -98.0 <= lon_f <= -97.5:
                    r["_lat"] = lat_f
                    r["_lon"] = lon_f
                    located.append(r)
            except (ValueError, TypeError):
                pass

    return {
        "records": located,
        "total": len(located),
        "days_back": days_back,
        "fetched_at": _utc_now().strftime("%Y-%m-%d %H:%M UTC"),
    }


def generate_traffic_map(days_back: int = 30) -> tuple[Optional[io.BytesIO], str]:
    """Generate an interactive HTML map of traffic/infrastructure reports.

    Returns:
        tuple: (BytesIO buffer with HTML content, summary message)
    """
    try:
        import folium
        from folium.plugins import MarkerCluster
    except ImportError:
        return None, "❌ Map generation requires 'folium' library. Install with: pip install folium"

    data = fetch_traffic_with_coords(days_back)
    records = data["records"]
    total = data["total"]

    if not records:
        return None, f"🚦 No traffic/infrastructure reports with location data found in the last {days_back} days."

    # Count by status
    open_count = sum(1 for r in records if (r.get("status") or "").lower() == "open")
    closed_count = sum(1 for r in records if (r.get("status") or "").lower() == "closed")

    # Bucket each record by age (days since filed)
    now_dt = datetime.now(timezone.utc)

    def _age_days(r):
        try:
            dt = datetime.fromisoformat(r.get("requested_datetime", "").replace("Z", "+00:00"))
            return (now_dt - dt).days
        except Exception:
            return days_back

    # Pre-compute counts per category × bucket for summary updates
    cat_keys = list(CATEGORY_GROUPS.keys())
    cat_bucket_counts = {
        cat: {"30": {"open": 0, "closed": 0}, "60": {"open": 0, "closed": 0}, "90": {"open": 0, "closed": 0}}
        for cat in cat_keys + ["all"]
    }
    for r in records:
        age = _age_days(r)
        status = (r.get("status") or "").lower()
        s = status if status in ("open", "closed") else "closed"
        cat = _get_category(r.get("_service_code", ""))
        for bucket_days in (30, 60, 90):
            if age <= bucket_days:
                b = str(bucket_days)
                cat_bucket_counts["all"][b][s] += 1
                cat_bucket_counts[cat][b][s] += 1
    counts_js = json.dumps(cat_bucket_counts)

    # Create map centered on Austin
    m = folium.Map(
        location=[30.2672, -97.7431],
        zoom_start=11,
        tiles="https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png?key=cb1_2du8_1_a49e4774820276874a1a5b33",
        attr='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',
    )
    m.get_root().header.add_child(folium.Element(og_meta_tags("traffic")))

    # Single MarkerCluster — markers are built client-side from a compact JSON
    # blob on demand (Option C) instead of pre-rendering every record into every
    # 30/60/90-day × open/closed × category bucket (previously ~24k markers / 57 MB).
    marker_cluster = MarkerCluster().add_to(m)

    # Serialize records to a compact JSON blob for on-demand client rendering.
    # Descriptions/notes are trimmed to keep the page lean; the full ticket text
    # is one click away on the 311 site.
    records_js_list = []
    for r in records:
        age = _age_days(r)
        # The panel only ever shows the days_back window (30d for traffic); the
        # cache can return far older records, so skip them to keep the page lean.
        if age > days_back:
            continue
        status = (r.get("status") or "").lower()
        s = "open" if status == "open" else "closed"
        description = (r.get("description") or "").strip()
        status_notes = (r.get("status_notes") or "").strip()
        attrs = r.get("attributes") or []
        attrs_slim = [
            {"l": (a.get("label") or "").strip(), "v": (a.get("value") or "").strip()}
            for a in attrs
            if a.get("label") and a.get("value")
        ]
        records_js_list.append({
            "lat": r["_lat"],
            "lon": r["_lon"],
            "st": s,
            "age": age,
            "cat": _get_category(r.get("_service_code", "")),
            "label": r.get("_service_label", "Unknown"),
            "id": r.get("service_request_id", "N/A"),
            "addr": (r.get("address") or "").strip(),
            "date": (r.get("requested_datetime") or "").split("T")[0],
            "upd": (r.get("updated_datetime") or "").split("T")[0],
            "desc": (description[:200] + "...") if len(description) > 200 else description,
            "notes": (status_notes[:200] + "...") if len(status_notes) > 200 else status_notes,
            "attrs": attrs_slim,
        })
    records_js = json.dumps(records_js_list, ensure_ascii=False).replace("</", "<\\/")

    # Build category options HTML for the dropdown
    cat_options_html = '<option value="all">All Categories</option>\n'
    for cat_key, cat in CATEGORY_GROUPS.items():
        cat_options_html += f'<option value="{cat_key}">{cat["label"]}</option>\n'

    # Control panel + client-side renderer. Markers are built only for the
    # currently visible filter combo (Option C), keeping the page tiny and
    # responsive instead of embedding ~24k pre-built markers.
    map_var = m.get_name()
    cluster_var = marker_cluster.get_name()
    panel_html = """
    <div id="map-panel" style="position: absolute; top: 10px; left: 50%; transform: translateX(-50%);
                background: white; padding: 10px 16px; border-radius: 6px;
                box-shadow: 0 2px 6px rgba(0,0,0,0.3); z-index: 9999;
                font-family: sans-serif; text-align: center;">
        <b style="font-size: 15px;">🚦 Austin Traffic & Infrastructure 311 Reports</b><br/>
        <span id="map-summary" style="font-size: 12px; color: #555;"></span>
        <div style="display: flex; justify-content: center; gap: 4px; margin-top: 7px;">
            <span style="margin: 0 4px; color: #ccc;">|</span>
            <button id="btn-open" onclick="toggleStatus('open')" class="fbtn active">🔴 Open</button>
            <button id="btn-closed" onclick="toggleStatus('closed')" class="fbtn active">🟢 Closed</button>
        </div>
    </div>
    <div id="cat-panel" style="position: absolute; top: 10px; right: 10px;
                background: white; padding: 8px 12px; border-radius: 6px;
                box-shadow: 0 2px 6px rgba(0,0,0,0.3); z-index: 9999;
                font-family: sans-serif;">
        <label for="cat-select" style="font-size: 11px; font-weight: bold; color: #444; display: block; margin-bottom: 4px;">Filter by Category</label>
        <select id="cat-select" onchange="setCategoryFilter(this.value)"
                style="font-size: 12px; padding: 3px 6px; border: 1px solid #ccc; border-radius: 4px; cursor: pointer;">
            __CAT_OPTIONS__
        </select>
    </div>
    <style>
        .fbtn {
            padding: 3px 9px; border: 1px solid #ccc; border-radius: 4px;
            background: #f5f5f5; cursor: pointer; font-size: 12px; color: #444;
        }
        .fbtn.active { background: #2563eb; color: white; border-color: #2563eb; }
        .fbtn:hover:not(.active) { background: #e0e7ff; }
    </style>
    <script>
        var currentDays = 30;
        var showOpen = true;
        var showClosed = true;
        var currentCategory = 'all';
        var records = __RECORDS_JS__;
        var markerCluster = null;
        var catBucketCounts = __COUNTS_JS__;

        function esc(s) {
            return String(s == null ? '' : s)
                .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;');
        }

        function makeIcon(status) {
            var color = (status === 'open') ? '#dc2626' : '#16a34a';
            return L.divIcon({
                className: '',
                html: '<div style="width:14px;height:14px;border-radius:50%;background:' + color +
                      ';border:2px solid #fff;box-shadow:0 0 4px rgba(0,0,0,0.45);"></div>',
                iconSize: [14, 14],
                iconAnchor: [7, 7]
            });
        }

        function buildPopup(r) {
            var addrLine = r.addr ? '<b>Address:</b> <a href="https://www.google.com/maps/search/?api=1&query=' +
                r.lat + ',' + r.lon + '" target="_blank">' + esc(r.addr) + '</a><br/>' : '';
            var updLine = (r.upd && r.upd !== r.date) ? '<span style="color: #666;">Updated: ' + esc(r.upd) + '</span><br/>' : '';
            var attrsHtml = (r.attrs || []).map(function(a) {
                return '<b>' + esc(a.l) + ':</b> ' + esc(a.v) + '<br/>';
            }).join('');
            var attrsBlock = attrsHtml ? '<b>Additional Details:</b><br/>' + attrsHtml : '';
            var descBlock = r.desc ? '<b>Description:</b><br/><i>' + esc(r.desc).replace(/\\n/g, '<br/>') + '</i><br/>' : '';
            var notesBlock = r.notes ? '<b>Resolution Notes:</b><br/><i>' + esc(r.notes).replace(/\\n/g, '<br/>') + '</i><br/>' : '';
            var statusText = (r.st === 'open') ? '🔴 Open' : '🟢 Closed';
            var ticketUrl = 'https://311.austintexas.gov/tickets/' + encodeURIComponent(r.id);
            return '<div style="font-family: sans-serif; max-width: 300px;">' +
                '<b><a href="' + ticketUrl + '" target="_blank" style="color: #0066cc;">Report #' + esc(r.id) + '</a></b><br/>' +
                '<span style="color: #666;">Filed: ' + esc(r.date) + '</span><br/>' + updLine + addrLine +
                '<br/><b>Status:</b> ' + statusText + '<br/>' +
                '<b>Category:</b> ' + esc(r.label) + '<br/><br/>' +
                attrsBlock + descBlock + notesBlock + '</div>';
        }

        function rebuildMarkers() {
            if (!markerCluster) return;
            markerCluster.clearLayers();
            for (var i = 0; i < records.length; i++) {
                var r = records[i];
                if (r.age > currentDays) continue;
                if (currentCategory !== 'all' && r.cat !== currentCategory) continue;
                var isOpen = (r.st === 'open');
                if (isOpen && !showOpen) continue;
                if (!isOpen && !showClosed) continue;
                var m = L.marker([r.lat, r.lon], { icon: makeIcon(r.st) });
                m.bindPopup(buildPopup(r), { maxWidth: 300 });
                m.bindTooltip((isOpen ? 'Open' : 'Closed') + ': ' + r.label);
                markerCluster.addLayer(m);
            }
        }

        function updateSummary() {
            var d = String(currentDays);
            var catData = catBucketCounts[currentCategory] || catBucketCounts['all'];
            var counts = catData[d] || {};
            var o = showOpen ? (counts.open || 0) : 0;
            var c = showClosed ? (counts.closed || 0) : 0;
            document.getElementById('map-summary').textContent =
                'Last ' + d + ' days · ' + (o + c) + ' total · ' + o + ' open · ' + c + ' closed';
        }

        function updateLayers() {
            rebuildMarkers();
            updateSummary();
        }

        function initLayers() {
            markerCluster = __CLUSTER_VAR__;
            updateLayers();
        }

        function toggleStatus(status) {
            if (status === 'open') showOpen = !showOpen;
            else showClosed = !showClosed;
            document.getElementById('btn-' + status).classList.toggle('active');
            updateLayers();
        }

        function setCategoryFilter(cat) {
            currentCategory = cat;
            updateLayers();
        }

        document.addEventListener('DOMContentLoaded', function() {
            setTimeout(initLayers, 1000);
        });
    </script>
    """
    panel_html = (
        panel_html.replace("__MAP_VAR__", map_var)
        .replace("__CLUSTER_VAR__", cluster_var)
        .replace("__COUNTS_JS__", counts_js)
        .replace("__CAT_OPTIONS__", cat_options_html)
        .replace("__RECORDS_JS__", records_js)
    )
    m.get_root().html.add_child(folium.Element(panel_html))

    # Save to buffer
    import tempfile
    with tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False) as tmp:
        tmp_path = tmp.name

    try:
        m.save(tmp_path)
        with open(tmp_path, 'rb') as f:
            html_content = f.read()

        buffer = io.BytesIO(html_content)
        buffer.seek(0)
    finally:
        try:
            os.unlink(tmp_path)
        except:
            pass

    summary = (
        f"🚦 *Traffic & Infrastructure Report Map*\n"
        f"_Last {days_back} days_\n\n"
        f"📊 *{total:,} reports mapped*\n"
        f"🔴 *{open_count:,} open*  ·  🟢 *{closed_count:,} closed*\n\n"
        f"Tap markers to see details. Use layer control to toggle views."
    )

    return buffer, summary
