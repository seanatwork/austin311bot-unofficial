"""
Bicycle & Cycling Infrastructure — data layer and formatters.

Queries Austin Open311 API across 5 cycling-relevant service codes.
No keyword filtering — all records from these codes are included since
any obstruction, debris, or construction in the ROW is a hazard to cyclists
regardless of whether the reporter mentioned "bike" in their description.

Service codes:
- PWBICYCL: Bicycle Issues (primary — explicit bicycle complaints)
- OBSTMIDB: Obstruction in Right of Way (blocked lanes, illegally parked)
- SBDEBROW: Debris in Street (road hazards)
- ATCOCIRW: Construction Concerns in Right of Way (detours/closures)
- ZZARSTSW: Street Sweeping (bike lane cleaning requests)
"""

import re
import time
import logging
import requests
import os
import io
from open311_client import open311_get, subscribe_popup_html, og_meta_tags
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

OPEN311_BASE_URL = "https://311.austintexas.gov/open311/v2"
TIMEOUT = 45
MAX_RETRIES = 8
RETRY_DELAY = 1.0
MAX_PAGES = 10

API_KEY = os.getenv("AUSTINAPIKEY")

SERVICE_CODES = {
    "PWBICYCL": "Bicycle Issue",
    "OBSTMIDB": "Obstruction in ROW",
    "SBDEBROW": "Debris in Street",
    "ATCOCIRW": "Construction in ROW",
    "ZZARSTSW": "Street Sweeping",
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
            "User-Agent": "austin311bot/0.1 (Open311 bicycle queries)",
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


def _make_request(params: dict) -> list:
    return open311_get(_get_session(), f"{OPEN311_BASE_URL}/requests.json", params)


def _fetch_code(service_code: str, days_back: int) -> list:
    """Fetch all requests for one service code with pagination."""
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

    return all_records


def fetch_bicycle_reports(days_back: int = 90, use_cache: bool = True) -> dict:
    """Fetch 311 reports across all cycling-relevant service codes with optional caching.

    All records from these codes are included — no keyword filtering.

    Args:
        days_back: Number of days to fetch
        use_cache: Whether to use SQLite caching (default True)
    """
    from open311_cache import init_cache, get_cached_records, cache_records, get_last_fetch_date

    CATEGORY = "bicycle"

    # Initialize cache if using
    if use_cache:
        init_cache()
        cached_records = get_cached_records(CATEGORY, service_codes=list(SERVICE_CODES.keys()))
        cached_ids = {r.get("service_request_id") for r in cached_records}
        logger.info(f"Loaded {len(cached_records)} cached bicycle records")

        # Check if cache is fresh (less than 6 days old)
        last_fetch = get_last_fetch_date(CATEGORY)
        if last_fetch:
            cache_age = _utc_now() - last_fetch
            if cache_age < timedelta(days=6) and len(cached_records) > 0:
                logger.info(f"Cache is fresh ({cache_age.days} days old), using cached data")
                return {
                    "records": cached_records,
                    "total_fetched": len(cached_records),
                    "days_back": days_back,
                    "by_code": {},  # Not tracked for cached data
                    "fetched_at": _utc_now().strftime("%Y-%m-%d %H:%M UTC"),
                    "cached": True,
                }
    else:
        cached_records = []
        cached_ids = set()

    all_records: list = []
    seen_ids: set = cached_ids.copy()
    new_records: list = []
    by_code: dict = {}

    for code, label in SERVICE_CODES.items():
        try:
            records = _fetch_code(code, days_back)
            # Filter out already-cached records
            unique_records = [r for r in records if r.get("service_request_id") not in seen_ids]
            for r in unique_records:
                seen_ids.add(r.get("service_request_id"))
                new_records.append(r)
            by_code[code] = {"label": label, "fetched": len(unique_records), "total": len(records)}
            all_records.extend(unique_records)
            logger.debug(f"{code}: {len(unique_records)} new records")
        except Exception as e:
            logger.warning(f"Failed to fetch {code}: {e}")
            by_code[code] = {"label": label, "fetched": 0, "error": str(e)}
        time.sleep(3.0 if not API_KEY else 1.0)

    # Combine with cached records
    if use_cache and cached_records:
        all_records = cached_records + [r for r in all_records if r.get("service_request_id") not in cached_ids]

    # Cache new records
    if use_cache and new_records:
        cache_records(CATEGORY, new_records)
        logger.info(f"Cached {len(new_records)} new bicycle records")

    return {
        "records": all_records,
        "total_fetched": len(all_records),
        "days_back": days_back,
        "by_code": by_code,
        "fetched_at": _utc_now().strftime("%Y-%m-%d %H:%M UTC"),
        "cached": False,
    }


# ── Telegram-facing functions (unchanged interface) ────────────────────────────

def get_recent_complaints(limit: int = 10, days_back: int = 90) -> list:
    """Return most recent bicycle complaints (PWBICYCL only)."""
    end = _utc_now()
    start = end - timedelta(days=days_back)
    params = {
        "service_code": "PWBICYCL",
        "start_date": _isoformat_z(start),
        "end_date": _isoformat_z(end),
        "per_page": limit,
        "page": 1,
    }
    return _make_request(params)


def lookup_ticket(ticket_id: str) -> Optional[dict]:
    """Look up any 311 service request by ticket ID."""
    session = _get_session()
    ticket_id = ticket_id.lstrip("#").strip()
    if re.match(r"^\d{10}$", ticket_id):
        ticket_id = f"{ticket_id[:2]}-{ticket_id[2:]}"
    url = f"{OPEN311_BASE_URL}/requests/{ticket_id}.json"
    try:
        resp = session.get(url, timeout=TIMEOUT)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            return data[0] if data else None
        return data
    except RETRYABLE_ERRORS as e:
        logger.warning(f"Ticket lookup failed ({e})")
        return None


def format_ticket(record: dict) -> str:
    ticket_id = record.get("service_request_id") or "N/A"
    service_name = record.get("service_name") or "Unknown"
    service_code = record.get("service_code") or "N/A"
    status = (record.get("status") or "unknown").upper()
    address = record.get("address") or "Address not available"
    lat = record.get("lat")
    lon = record.get("long")
    requested = record.get("requested_datetime") or ""
    updated = record.get("updated_datetime") or ""
    description = (record.get("description") or "").strip()
    notes = (record.get("status_notes") or "").strip()

    def fmt_dt(s):
        if not s:
            return "N/A"
        return s.replace("T", " ").replace("Z", " UTC")

    status_emoji = "🟢" if status == "CLOSED" else "🔴"
    msg = f"🎫 *311 Ticket Lookup*\n━━━━━━━━━━━━━━━━━━━━\n\n"
    msg += f"📋 *Service:* {service_name} `({service_code})`\n"
    msg += f"{status_emoji} *Status:* {status}\n\n"
    msg += f"📍 *Address:* {address}\n"
    if lat and lon:
        msg += f"🗺 *Coordinates:* {lat:.6f}, {lon:.6f}\n"
    msg += f"\n📅 *Filed:* {fmt_dt(requested)}\n"
    if updated:
        msg += f"🔄 *Last updated:* {fmt_dt(updated)}\n"
    if description:
        msg += f"\n💬 *Description:*\n{description}\n"
    if notes:
        msg += f"\n📝 *Status notes:*\n{notes}\n"
    if ticket_id != "N/A":
        msg += f"\n🔗 [View ticket on Austin 311](https://311.austintexas.gov/open311/v2/requests/{ticket_id}.json)"
    return msg


def format_complaints(complaints: list, title: str = "🚴 Bicycle Complaints") -> str:
    if not complaints:
        return "📝 No bicycle complaints found for that search."
    msg = f"{title}\n\nShowing {len(complaints)} complaint(s):\n\n"
    for i, r in enumerate(complaints, 1):
        req_id = r.get("service_request_id") or "N/A"
        address = r.get("address") or "Address not available"
        status = (r.get("status") or "unknown").upper()
        requested = (r.get("requested_datetime") or "").split("T")[0]
        description = r.get("description") or r.get("service_name") or "Bicycle complaint"
        msg += f"{i}. *{description[:80]}*\n"
        msg += f"📍 {address}\n"
        msg += f"🔖 Status: {status} | 📅 {requested}\n"
        msg += f"🎫 #{req_id}\n\n"
        if i >= 10:
            remaining = len(complaints) - i
            if remaining > 0:
                msg += f"... and {remaining} more.\n"
            break
    return msg


def get_stats(days_back: int = 90) -> dict:
    complaints = get_recent_complaints(limit=100, days_back=days_back)
    if not complaints:
        return {"total": 0, "days_back": days_back}
    now = _utc_now()
    resolution_days = []
    open_tickets = []
    street_counts: dict = {}
    for r in complaints:
        status = (r.get("status") or "").lower()
        requested_str = r.get("requested_datetime") or ""
        updated_str = r.get("updated_datetime") or ""
        if status == "closed" and requested_str and updated_str:
            try:
                req = datetime.fromisoformat(requested_str.replace("Z", "+00:00"))
                upd = datetime.fromisoformat(updated_str.replace("Z", "+00:00"))
                days = (upd - req).days
                if 0 <= days <= 365:
                    resolution_days.append(days)
            except ValueError:
                pass
        if status == "open":
            open_tickets.append(r)
        address = r.get("address") or ""
        parts = address.replace(", Austin", "").strip().split()
        if len(parts) >= 2:
            street = " ".join(parts[1:])
            street_counts[street] = street_counts.get(street, 0) + 1
    avg_resolution = round(sum(resolution_days) / len(resolution_days), 1) if resolution_days else None
    top_streets = sorted(street_counts.items(), key=lambda x: -x[1])[:5]
    oldest_open = None
    if open_tickets:
        def req_date(r):
            try:
                return datetime.fromisoformat((r.get("requested_datetime") or "").replace("Z", "+00:00"))
            except ValueError:
                return now
        oldest = min(open_tickets, key=req_date)
        oldest_dt = req_date(oldest)
        oldest_open = {
            "id": oldest.get("service_request_id"),
            "address": oldest.get("address"),
            "days_ago": (now - oldest_dt).days,
        }
    return {
        "total": len(complaints),
        "open": len(open_tickets),
        "closed": len(complaints) - len(open_tickets),
        "avg_resolution_days": avg_resolution,
        "top_streets": top_streets,
        "oldest_open": oldest_open,
        "days_back": days_back,
    }


def format_stats(stats: dict) -> str:
    if stats.get("total", 0) == 0:
        return f"📝 No bicycle complaints found in the past {stats.get('days_back', 90)} days."
    total = stats["total"]
    msg = "🚴 *Bicycle Complaints — Last 90 Days*\n\n"
    msg += f"📊 *Total complaints:* {total} ({stats['open']} open · {stats['closed']} closed)\n\n"
    if stats.get("avg_resolution_days") is not None:
        msg += f"⏱ *Avg resolution time:* {stats['avg_resolution_days']} days\n\n"
    top = stats.get("top_streets", [])
    if top:
        msg += "📍 *Most complained streets:*\n"
        for street, count in top:
            msg += f"   {street}: {count} complaint{'s' if count > 1 else ''}\n"
        msg += "\n"
    oldest = stats.get("oldest_open")
    if oldest:
        msg += f"🕰 *Oldest open ticket:* #{oldest['id']}\n"
        msg += f"   {oldest['address']} — {oldest['days_ago']} days unresolved\n"
    msg += "\n_Source: [Austin Open311 API](https://311.austintexas.gov/open311/v2)_"
    return msg


# =============================================================================
# MAP GENERATOR
# =============================================================================

# Open marker color/icon by service code
_CODE_STYLE = {
    "PWBICYCL": ("blue",      "info-sign"),
    "OBSTMIDB": ("red",       "exclamation-sign"),
    "SBDEBROW": ("orange",    "warning-sign"),
    "ATCOCIRW": ("purple",    "wrench"),
    "ZZARSTSW": ("cadetblue", "refresh"),
}
_DEFAULT_STYLE = ("gray", "info-sign")

# camelCase slugs — no underscores so split('_') on the layer key is safe
_CODE_SLUGS = {
    "PWBICYCL": "bicycleIssue",
    "OBSTMIDB": "obstruction",
    "SBDEBROW": "debris",
    "ATCOCIRW": "construction",
    "ZZARSTSW": "sweeping",
}
_DEFAULT_SLUG = "other"

# Tab definitions: each tab is a category with its own service codes and defaults
_TABS = {
    "bicycle": {
        "label": "🚴 Bicycle Issues",
        "codes": ["PWBICYCL"],
        "default_days": 30,
        "description": "Explicit bicycle complaints (hazards, damaged lanes, etc.)"
    },
    "infrastructure": {
        "label": "🏗️ Infrastructure",
        "codes": ["OBSTMIDB", "SBDEBROW", "ATCOCIRW"],
        "default_days": 30,
        "description": "Obstructions, debris, and construction in right-of-way"
    },
    "sweeping": {
        "label": "🧹 Street Sweeping",
        "codes": ["ZZARSTSW"],
        "default_days": 30,
        "description": "Bike lane cleaning requests"
    }
}


def generate_bicycle_map(days_back: int = 90) -> tuple:
    """Generate an interactive HTML map of cycling-relevant 311 reports.

    Three tabs:
    1. 🚴 Bicycle Issues - PWBICYCL only (explicit bike complaints)
    2. 🏗️ Infrastructure - OBSTMIDB, SBDEBROW, ATCOCIRW (road hazards/obstructions)
    3. 🧹 Street Sweeping - ZZARSTSW only (bike lane cleaning)

    Returns:
        tuple: (BytesIO buffer with HTML content, summary message)
    """
    try:
        import folium
        from folium.plugins import MarkerCluster
    except ImportError:
        return None, "❌ Map generation requires 'folium'. Install: pip install folium"

    import tempfile

    result = fetch_bicycle_reports(days_back)
    records_raw = result["records"]

    now_dt = _utc_now()
    
    # Parse and validate records, group by tab
    tab_records = {tab_id: [] for tab_id in _TABS}
    
    for r in records_raw:
        try:
            lat = float(r.get("lat") or 0)
            lon = float(r.get("long") or 0)
            if 30.0 <= lat <= 30.5 and -98.0 <= lon <= -97.5:
                r["_lat"] = lat
                r["_lon"] = lon
                code = r.get("_service_code", "")
                # Assign to correct tab
                assigned = False
                for tab_id, tab_info in _TABS.items():
                    if code in tab_info["codes"]:
                        tab_records[tab_id].append(r)
                        assigned = True
                        break
                if not assigned:
                    tab_records["bicycle"].append(r)
        except (TypeError, ValueError):
            pass

    # Check if any tab has records
    total_mapped = sum(len(recs) for recs in tab_records.values())
    if total_mapped == 0:
        return None, "🚴 No cycling reports with location data found."

    def _age_days(r):
        try:
            dt = datetime.fromisoformat(r.get("requested_datetime", "").replace("Z", "+00:00"))
            return (now_dt - dt).days
        except Exception:
            return days_back

    # Build counts per tab × bucket × status for the JS layer filter
    all_slugs = list(_CODE_SLUGS.values()) + [_DEFAULT_SLUG]
    tab_type_bucket_counts = {}
    
    for tab_id, tab_info in _TABS.items():
        tab_slugs = list(set([_CODE_SLUGS.get(c, _DEFAULT_SLUG) for c in tab_info["codes"]]))
        tab_slugs.append("all")
        counts = {}
        for slug in tab_slugs:
            counts[slug] = {"30": {"open": 0, "closed": 0}, "60": {"open": 0, "closed": 0}, "90": {"open": 0, "closed": 0}}
        tab_type_bucket_counts[tab_id] = counts

    # Populate counts
    for tab_id, recs in tab_records.items():
        for r in recs:
            age = _age_days(r)
            status = (r.get("status") or "").lower()
            s = status if status in ("open", "closed") else "closed"
            slug = _CODE_SLUGS.get(r.get("_service_code", ""), _DEFAULT_SLUG)
            for bucket_days in (30, 60, 90):
                if age <= bucket_days:
                    b = str(bucket_days)
                    if slug in tab_type_bucket_counts[tab_id]:
                        tab_type_bucket_counts[tab_id][slug][b][s] += 1
                    tab_type_bucket_counts[tab_id]["all"][b][s] += 1

    counts_js = str(tab_type_bucket_counts).replace("'", '"')

    m = folium.Map(location=[30.2672, -97.7431], zoom_start=11, tiles="CartoDB positron")
    m.get_root().header.add_child(folium.Element(og_meta_tags("bicycle")))

    # Build all possible layer keys
    # Layer key: {tabId}_{status}_{bucket}_{typeSlug}
    fg_clusters = {}
    fg_objects = {}
    
    for tab_id in list(_TABS.keys()):
        tab_slugs = list(set([_CODE_SLUGS.get(c, _DEFAULT_SLUG) for c in _TABS[tab_id]["codes"]]))
        tab_slugs.append("all")
        for status_key in ("open", "closed"):
            for bucket in ("30", "60", "90"):
                for slug in tab_slugs:
                    name = f"{tab_id}_{status_key}_{bucket}_{slug}"
                    # Default: show only bicycle tab, 30 days, all types
                    show = (tab_id == "bicycle" and bucket == "30" and slug == "all")
                    fg = folium.FeatureGroup(name=name, show=show, overlay=True)
                    cluster = MarkerCluster().add_to(fg)
                    fg.add_to(m)
                    fg_clusters[name] = cluster
                    fg_objects[name] = fg

    # Add markers to appropriate layers
    for tab_id, recs in tab_records.items():
        for r in recs:
            lat = r["_lat"]
            lon = r["_lon"]
            status = (r.get("status") or "").lower()
            code = r.get("_service_code", "")
            label = r.get("_service_label", "Cycling Report")
            desc = (r.get("description") or "").strip()
            notes = (r.get("status_notes") or "").strip()
            date_str = (r.get("requested_datetime") or "").split("T")[0]
            updated_str = (r.get("updated_datetime") or "").split("T")[0]
            address = (r.get("address") or "").strip()
            req_id = r.get("service_request_id", "N/A")

            age = _age_days(r)
            bucket = "30" if age <= 30 else ("60" if age <= 60 else "90")
            slug = _CODE_SLUGS.get(code, _DEFAULT_SLUG)

            # Add to tab-specific layers - try specific slug first, fall back to "all"
            for target_slug in [slug, "all"]:
                cluster_key = f"{tab_id}_{status}_{bucket}_{target_slug}"
                if cluster_key in fg_clusters:
                    break

            # Fallback to closed if open cluster doesn't exist
            if cluster_key not in fg_clusters:
                cluster_key = f"{tab_id}_closed_{bucket}_all"
            if cluster_key not in fg_clusters:
                cluster_key = f"{tab_id}_closed_90_all"
            if cluster_key not in fg_clusters:
                continue  # Skip if no matching cluster

            address_line = (
                f'<b>Address:</b> <a href="https://www.google.com/maps/search/?api=1&query={lat},{lon}"'
                f' target="_blank">{address}</a><br/>'
            ) if address else ""
            updated_line = (
                f"<span style='color:#666;'>Updated: {updated_str}</span><br/>"
            ) if updated_str and updated_str != date_str else ""

            desc_block = ""
            if desc:
                short = (desc[:400] + "…") if len(desc) > 400 else desc
                desc_block = f"<b>Description:</b><br/><i>{short.replace(chr(10), '<br/>')}</i><br/>"
            elif notes:
                short = (notes[:300] + "…") if len(notes) > 300 else notes
                note_label = "Resolution" if status == "closed" else "Notes"
                desc_block = f"<b>{note_label}:</b><br/><i>{short}</i><br/>"

            ticket_url = f"https://311.austintexas.gov/tickets/{req_id}"
            sub_link = subscribe_popup_html(lat, lon)
            popup_html = f"""
            <div style="font-family:sans-serif;max-width:300px;font-size:13px;">
                <b><a href="{ticket_url}" target="_blank" style="color:#0066cc;">Report #{req_id}</a></b><br/>
                <span style="color:#666;">Filed: {date_str}</span><br/>
                {updated_line}
                {address_line}
                <br/>
                <b>Status:</b> {'🔴 Open' if status == 'open' else '🟢 Closed'}<br/>
                <b>Type:</b> {label}<br/><br/>
                {desc_block}
                {sub_link}
            </div>
            """
            popup = folium.Popup(popup_html, max_width=300)

            if status == "open":
                color, icon_name = _CODE_STYLE.get(code, _DEFAULT_STYLE)
            else:
                color, icon_name = "green", "ok-sign"

            folium.Marker(
                location=[lat, lon],
                popup=popup,
                icon=folium.Icon(color=color, icon=icon_name, prefix="glyphicon"),
                tooltip=f"{'Open' if status == 'open' else 'Closed'}: {label}",
            ).add_to(fg_clusters[cluster_key])

    map_var = m.get_name()
    
    # Build layer map JS
    layer_map_entries = []
    for tab_id in list(_TABS.keys()):
        tab_slugs = list(set([_CODE_SLUGS.get(c, _DEFAULT_SLUG) for c in _TABS[tab_id]["codes"]]))
        tab_slugs.append("all")
        for status_key in ("open", "closed"):
            for bucket in ("30", "60", "90"):
                for slug in tab_slugs:
                    name = f"{tab_id}_{status_key}_{bucket}_{slug}"
                    if name in fg_objects:
                        layer_map_entries.append(f'"{name}": {fg_objects[name].get_name()}')
    layer_map_js = "{" + ", ".join(layer_map_entries) + "}"

    # Build tab navigation HTML
    tab_buttons_html = ""
    tab_panels_html = ""
    first_tab = True
    for tab_id, tab_info in _TABS.items():
        active = "active" if first_tab else ""
        tab_buttons_html += f'<button class="tab-btn {active}" onclick="switchTab(\'{tab_id}\')">{tab_info["label"]}</button>\n'
        tab_panels_html += f'''
        <div id="panel-{tab_id}" class="tab-panel" style="display:{'block' if first_tab else 'none'}">
            <span style="font-size:11px;color:#666;">{tab_info["description"]}</span>
            <div style="display:flex;justify-content:center;gap:4px;margin-top:5px;">
                <button id="btn-{tab_id}-30" onclick="setDayFilter(\'{tab_id}\', 30)" class="fbtn active">30d</button>
                <button id="btn-{tab_id}-60" onclick="setDayFilter(\'{tab_id}\', 60)" class="fbtn">60d</button>
                <button id="btn-{tab_id}-90" onclick="setDayFilter(\'{tab_id}\', 90)" class="fbtn">90d</button>
                <span style="margin:0 4px;color:#ccc;">|</span>
                <button id="btn-{tab_id}-open"   onclick="toggleStatus(\'{tab_id}\', \'open\')"   class="fbtn active">🔴 Open</button>
                <button id="btn-{tab_id}-closed" onclick="toggleStatus(\'{tab_id}\', \'closed\')" class="fbtn active">🟢 Closed</button>
            </div>
        </div>'''
        first_tab = False

    # Type filter dropdown per tab
    type_filter_html = ""
    first_tab = True
    for tab_id, tab_info in _TABS.items():
        options = '<option value="all">All Types</option>\n'
        for code in tab_info["codes"]:
            slug = _CODE_SLUGS.get(code, _DEFAULT_SLUG)
            label = SERVICE_CODES.get(code, code)
            options += f'<option value="{slug}">{label}</option>\n'
        type_filter_html += f'''
        <div id="type-filter-{tab_id}" class="type-filter" style="display:{'block' if first_tab else 'none'}">
            <label style="font-size:11px;font-weight:bold;color:#444;display:block;margin-bottom:4px;">Filter by Type</label>
            <select id="select-{tab_id}" onchange="setTypeFilter(\'{tab_id}\', this.value)"
                    style="font-size:12px;padding:3px 6px;border:1px solid #ccc;border-radius:4px;cursor:pointer;width:100%;">
                {options}
            </select>
        </div>'''
        first_tab = False

    panel_html = f"""
    <div id="map-panel" style="position:absolute;top:10px;left:50%;transform:translateX(-50%);
                background:white;padding:10px 16px;border-radius:6px;
                box-shadow:0 2px 6px rgba(0,0,0,0.3);z-index:9999;
                font-family:sans-serif;text-align:center;max-width:700px;width:90%;">
        <b style="font-size:15px;">🚴 Austin Bicycle Issues 311 Map</b><br/>
        <span id="map-summary" style="font-size:12px;color:#555;"></span>
        <div id="tab-nav" style="display:flex;justify-content:center;gap:2px;margin-top:7px;border-bottom:2px solid #e5e7eb;padding-bottom:4px;">
            {tab_buttons_html}
        </div>
        {tab_panels_html}
        <div id="type-filter-container" style="margin-top:6px;">
            {type_filter_html}
        </div>
    </div>
    <style>
        .tab-btn {{ padding:5px 12px;border:1px solid transparent;border-radius:4px 4px 0 0;background:transparent;cursor:pointer;font-size:13px;color:#666;transition:all 0.2s; }}
        .tab-btn.active {{ background:white;color:#2563eb;border-color:#e5e7eb #e5e7eb white;font-weight:bold; }}
        .tab-btn:hover:not(.active) {{ background:#f3f4f6;color:#374151; }}
        .tab-panel {{ padding:6px 0; }}
        .type-filter {{ padding:4px 0; }}
        .fbtn {{ padding:3px 9px;border:1px solid #ccc;border-radius:4px;background:#f5f5f5;cursor:pointer;font-size:12px;color:#444; }}
        .fbtn.active {{ background:#2563eb;color:white;border-color:#2563eb; }}
        .fbtn:hover:not(.active) {{ background:#e0e7ff; }}
    </style>
    <script>
        var layerMap = {layer_map_js};
        var leafletMap = {map_var};
        var currentTab = 'bicycle';
        var currentDays = {{'bicycle': 30, 'infrastructure': 30, 'sweeping': 30}};
        var showOpen = {{'bicycle': true, 'infrastructure': true, 'sweeping': true}};
        var showClosed = {{'bicycle': true, 'infrastructure': true, 'sweeping': true}};
        var currentType = {{'bicycle': 'all', 'infrastructure': 'all', 'sweeping': 'all'}};
        var typeBucketCounts = {counts_js};

        function updateSummary() {{
            var tab = currentTab;
            var d = String(currentDays[tab]);
            var catData = typeBucketCounts[tab] || {{}};
            var typeSlug = currentType[tab];
            var counts = (catData[typeSlug] && catData[typeSlug][d]) ? catData[typeSlug][d] : {{open: 0, closed: 0}};
            var o = showOpen[tab] ? (counts.open || 0) : 0;
            var c = showClosed[tab] ? (counts.closed || 0) : 0;
            document.getElementById('map-summary').textContent =
                'Last ' + d + ' days · ' + (o + c) + ' total · ' + o + ' open · ' + c + ' closed';
        }}

        function switchTab(tabId) {{
            currentTab = tabId;
            // Update tab buttons
            document.querySelectorAll('.tab-btn').forEach(function(btn) {{
                btn.classList.remove('active');
            }});
            document.querySelectorAll('.tab-btn').forEach(function(btn) {{
                if (btn.getAttribute('onclick') && btn.getAttribute('onclick').includes(tabId)) {{
                    btn.classList.add('active');
                }}
            }});
            // Show/hide panels
            document.querySelectorAll('.tab-panel').forEach(function(p) {{
                p.style.display = 'none';
            }});
            var panel = document.getElementById('panel-' + tabId);
            if (panel) panel.style.display = 'block';
            // Show/hide type filters
            document.querySelectorAll('.type-filter').forEach(function(f) {{
                f.style.display = 'none';
            }});
            var filter = document.getElementById('type-filter-' + tabId);
            if (filter) filter.style.display = 'block';
            updateLayers();
            updateSummary();
        }}

        function updateLayers() {{
            if (!layerMap || !leafletMap) return;
            Object.keys(layerMap).forEach(function(key) {{
                var parts = key.split('_');
                var tab = parts[0];
                var status = parts[1];
                var bucket = parseInt(parts[2]);
                var typeSlug = parts.slice(3).join('_');
                var timeOk = bucket <= currentDays[tab];
                var statusOk = (status === 'open' && showOpen[tab]) || (status === 'closed' && showClosed[tab]);
                var typeOk = (currentType[tab] === 'all') || (typeSlug === currentType[tab]);
                var layer = layerMap[key];
                if (tab === currentTab && timeOk && statusOk && typeOk) {{
                    if (!leafletMap.hasLayer(layer)) leafletMap.addLayer(layer);
                }} else {{
                    if (leafletMap.hasLayer(layer)) leafletMap.removeLayer(layer);
                }}
            }});
        }}

        function setDayFilter(tab, days) {{
            currentDays[tab] = days;
            [30, 60, 90].forEach(function(d) {{
                var btn = document.getElementById('btn-' + tab + '-' + d);
                if (btn) btn.classList.toggle('active', d === days);
            }});
            if (tab === currentTab) {{
                updateLayers();
                updateSummary();
            }}
        }}

        function toggleStatus(tab, status) {{
            if (status === 'open') showOpen[tab] = !showOpen[tab];
            else showClosed[tab] = !showClosed[tab];
            var btn = document.getElementById('btn-' + tab + '-' + status);
            if (btn) btn.classList.toggle('active');
            if (tab === currentTab) {{
                updateLayers();
                updateSummary();
            }}
        }}

        function setTypeFilter(tab, type) {{
            currentType[tab] = type;
            if (tab === currentTab) {{
                updateLayers();
                updateSummary();
            }}
        }}

        document.addEventListener('DOMContentLoaded', function() {{
            setTimeout(function() {{
                switchTab('bicycle');
            }}, 1000);
        }});
    </script>
    """
    m.get_root().html.add_child(folium.Element(panel_html))

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
            import os as _os
            _os.unlink(tmp_path)
        except Exception:
            pass

    # Build summary
    bicycle_count = len(tab_records["bicycle"])
    infra_count = len(tab_records["infrastructure"])
    sweeping_count = len(tab_records["sweeping"])
    
    bicycle_open = sum(1 for r in tab_records["bicycle"] if (r.get("status") or "").lower() == "open")
    infra_open = sum(1 for r in tab_records["infrastructure"] if (r.get("status") or "").lower() == "open")
    sweeping_open = sum(1 for r in tab_records["sweeping"] if (r.get("status") or "").lower() == "open")

    summary = (
        f"🚴 *Cycling 311 Reports Map*\n"
        f"_Last {days_back} days · 3 categories_\n\n"
        f"📊 *{total_mapped:,} reports mapped*\n\n"
        f"🚴 **Bicycle Issues:** {bicycle_count:,} ({bicycle_open:,} open)\n"
        f"🏗️ **Infrastructure:** {infra_count:,} ({infra_open:,} open)\n"
        f"🧹 **Street Sweeping:** {sweeping_count:,} ({sweeping_open:,} open)\n\n"
        f"Use the tabs at the top to switch between categories. "
        f"Each tab has its own time range and type filter."
    )
    return buffer, summary
