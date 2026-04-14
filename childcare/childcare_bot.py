"""
Child Care Licensing — data layer and formatters.

Queries HHSC Child Care Licensing datasets on data.texas.gov:
  bc5r-88dy  — facility master (operation_name, type, capacity, flags, deficiency counts)
  tqgd-mf4x  — non-compliance detail (violations, risk level, narrative)

Joins on operation_id to show Austin-specific summaries and top violators.
"""

import io
import os
import json
import tempfile
import logging
import requests
from typing import Optional
from urllib.parse import quote

logger = logging.getLogger(__name__)

FACILITIES_URL   = "https://data.texas.gov/resource/bc5r-88dy.json"
VIOLATIONS_URL   = "https://data.texas.gov/resource/tqgd-mf4x.json"
TIMEOUT          = 20

_session: Optional[requests.Session] = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({
            "Accept": "application/json",
            "User-Agent": "austin311bot/0.1 (childcare queries)",
        })
    return _session


def _fetch_austin_facilities() -> list:
    """Fetch all Austin child care facilities from bc5r-88dy."""
    session = _get_session()
    results = []
    offset = 0
    limit = 1000

    while True:
        resp = session.get(FACILITIES_URL, params={
            "$where": "city='AUSTIN'",
            "$limit": limit,
            "$offset": offset,
        }, timeout=TIMEOUT)
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        results.extend(batch)
        if len(batch) < limit:
            break
        offset += limit

    return results


def _fetch_violations_for_ids(operation_ids: list[str]) -> list:
    """Fetch non-compliance records for a list of operation_ids."""
    if not operation_ids:
        return []

    session = _get_session()
    ids_str = ", ".join(operation_ids)
    results = []
    offset = 0
    limit = 1000

    while True:
        resp = session.get(VIOLATIONS_URL, params={
            "$where": f"operation_id in ({ids_str})",
            "$limit": limit,
            "$offset": offset,
        }, timeout=TIMEOUT)
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        results.extend(batch)
        if len(batch) < limit:
            break
        offset += limit

    return results


def get_childcare_stats() -> dict:
    """Fetch and summarize Austin child care licensing data."""
    facilities = _fetch_austin_facilities()
    if not facilities:
        return {"total": 0}

    total = len(facilities)
    active = sum(1 for f in facilities if (f.get("operation_status") or "").upper() == "Y")
    total_capacity = sum(int(f.get("total_capacity") or 0) for f in facilities)

    # Facilities with any compliance flag
    flagged = [
        f for f in facilities
        if any(
            (f.get(field) or "").upper() == "YES"
            for field in ("adverse_action", "corrective_action", "conditions_on_permit", "temporarily_closed")
        )
    ]

    adverse_action    = sum(1 for f in facilities if (f.get("adverse_action") or "").upper() == "YES")
    corrective_action = sum(1 for f in facilities if (f.get("corrective_action") or "").upper() == "YES")
    temp_closed       = sum(1 for f in facilities if (f.get("temporarily_closed") or "").upper() == "YES")

    # Top violators: sort by deficiency_high desc, then deficiency_medium_high desc
    def deficiency_key(f):
        high    = int(f.get("deficiency_high") or 0)
        med_hi  = int(f.get("deficiency_medium_high") or 0)
        med     = int(f.get("deficiency_medium") or 0)
        return (high, med_hi, med)

    # Only include facilities that have at least one high or medium-high deficiency
    with_deficiencies = [
        f for f in facilities
        if int(f.get("deficiency_high") or 0) > 0 or int(f.get("deficiency_medium_high") or 0) > 0
    ]
    top_violators = sorted(with_deficiencies, key=deficiency_key, reverse=True)[:8]

    # Fetch violations detail for top violators (for narratives/risk levels)
    top_ids = [str(int(f["operation_id"])) for f in top_violators if f.get("operation_id")]
    violations = _fetch_violations_for_ids(top_ids) if top_ids else []

    # Group violations by operation_id, collect open (uncorrected) ones
    open_violations: dict[str, list] = {}
    for v in violations:
        oid = str(int(v.get("operation_id", 0)))
        if not v.get("corrected_date"):  # uncorrected = still open
            open_violations.setdefault(oid, []).append(v)

    # Build top violator records
    top_records = []
    for f in top_violators:
        oid   = str(int(f.get("operation_id", 0)))
        high  = int(f.get("deficiency_high") or 0)
        medhi = int(f.get("deficiency_medium_high") or 0)
        med   = int(f.get("deficiency_medium") or 0)
        inspections = int(f.get("total_inspections") or 0)
        open_v = open_violations.get(oid, [])

        # Pick the most severe open violation narrative (High > Medium High > others)
        def risk_rank(v):
            rl = (v.get("standard_risk_level") or "").lower()
            if rl == "high":             return 0
            if rl == "medium high":      return 1
            if rl == "medium":           return 2
            return 3

        open_v_sorted = sorted(open_v, key=risk_rank)
        top_narrative = ""
        if open_v_sorted:
            top_narrative = (open_v_sorted[0].get("narrative") or "").strip()
            if len(top_narrative) > 150:
                top_narrative = top_narrative[:147] + "…"

        top_records.append({
            "name":          (f.get("operation_name") or "Unknown").strip(),
            "type":          f.get("operation_type") or "",
            "address":       (f.get("location_address") or "").replace(f.get("city",""),"").strip().rstrip(","),
            "high":          high,
            "medium_high":   medhi,
            "medium":        med,
            "inspections":   inspections,
            "open_violations": len(open_v),
            "top_narrative": top_narrative,
            "adverse":       (f.get("adverse_action") or "").upper() == "YES",
            "temp_closed":   (f.get("temporarily_closed") or "").upper() == "YES",
        })

    return {
        "total":             total,
        "active":            active,
        "total_capacity":    total_capacity,
        "flagged":           len(flagged),
        "adverse_action":    adverse_action,
        "corrective_action": corrective_action,
        "temp_closed":       temp_closed,
        "top_violators":     top_records,
    }


def format_childcare(stats: dict) -> str:
    if stats.get("total", 0) == 0:
        return "📝 No Austin child care facility data available."

    total    = stats["total"]
    active   = stats["active"]
    capacity = stats["total_capacity"]
    flagged  = stats["flagged"]

    msg  = "🧒 *Austin Child Care Licensing*\n"
    msg += "_HHSC Child Care Licensing — facility compliance summary_\n\n"

    msg += f"🏫 *Facilities:* {active} active ({total} total)\n"
    msg += f"👶 *Licensed capacity:* {capacity:,} children\n\n"

    if stats.get("adverse_action") or stats.get("corrective_action") or stats.get("temp_closed"):
        msg += "⚠️ *Compliance flags:*\n"
        if stats["adverse_action"]:
            msg += f"  🚨 Adverse action: {stats['adverse_action']}\n"
        if stats["corrective_action"]:
            msg += f"  📋 Corrective action: {stats['corrective_action']}\n"
        if stats["temp_closed"]:
            msg += f"  🔒 Temporarily closed: {stats['temp_closed']}\n"
        msg += "\n"

    top = stats.get("top_violators", [])
    if top:
        msg += "🔴 *Most Deficiencies (High + Medium High):*\n\n"
        for r in top:
            flags = []
            if r["adverse"]:    flags.append("🚨 adverse action")
            if r["temp_closed"]: flags.append("🔒 closed")
            flag_str = f" · {', '.join(flags)}" if flags else ""

            # Build Google Maps link from name + address
            maps_query = f"{r['name']} {r['address']} Austin TX".strip()
            maps_url = f"https://www.google.com/maps/search/?api=1&query={quote(maps_query)}"
            name_link = f"[{r['name']}]({maps_url})"

            msg += f"{name_link}{flag_str}\n"
            msg += f"_{r['type']}_\n"
            msg += (
                f"🔴 {r['high']} high  "
                f"🟠 {r['medium_high']} med-high  "
                f"🟡 {r['medium']} medium"
            )
            if r["inspections"]:
                msg += f"  ·  {r['inspections']} inspections"
            msg += "\n"
            if r["open_violations"]:
                msg += f"  ⚠️ {r['open_violations']} uncorrected violation(s)\n"
            if r["top_narrative"]:
                msg += f'  _"{r["top_narrative"]}"_\n'
            msg += "\n"

    msg += "_Source: [HHSC Child Care Licensing](https://data.texas.gov/d/bc5r-88dy)_"
    return msg


# =============================================================================
# MAP GENERATOR
# =============================================================================

def _fetch_facilities_with_coords() -> list:
    """Fetch Austin childcare facilities that have geocoded coordinates."""
    session = _get_session()
    results = []
    offset = 0
    limit = 1000

    while True:
        resp = session.get(FACILITIES_URL, params={
            "$select": (
                "operation_id,operation_name,operation_type,location_address,"
                "location_address_geo,total_capacity,operation_status,"
                "deficiency_high,deficiency_medium_high,deficiency_medium,"
                "adverse_action,corrective_action,temporarily_closed"
            ),
            "$where": "city='AUSTIN'",
            "$limit": limit,
            "$offset": offset,
        }, timeout=TIMEOUT)
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        for f in batch:
            geo = f.get("location_address_geo") or {}
            try:
                lat = float(geo.get("latitude") or 0)
                lon = float(geo.get("longitude") or 0)
                if 30.0 <= lat <= 30.5 and -98.0 <= lon <= -97.5:
                    f["_lat"] = lat
                    f["_lon"] = lon
                    results.append(f)
            except (TypeError, ValueError):
                pass
        if len(batch) < limit:
            break
        offset += limit

    return results


def generate_childcare_map(days_back: int = 90) -> tuple:
    """Generate an interactive HTML map of Austin childcare facility compliance.

    Markers are colored by deficiency level. days_back is accepted for API
    compatibility but not used (facility data is a current compliance snapshot).

    Returns:
        tuple: (BytesIO buffer with HTML content, summary message)
    """
    try:
        import folium
        from folium.plugins import MarkerCluster
    except ImportError:
        return None, "❌ Map generation requires 'folium'. Install: pip install folium"

    facilities = _fetch_facilities_with_coords()
    if not facilities:
        return None, "🧒 No Austin childcare facility location data found."

    total = len(facilities)
    flagged = sum(
        1 for f in facilities
        if int(f.get("deficiency_high") or 0) > 0
        or int(f.get("deficiency_medium_high") or 0) > 0
    )
    high_risk = sum(1 for f in facilities if int(f.get("deficiency_high") or 0) > 0)

    def _deficiency_tier(f):
        if int(f.get("deficiency_high") or 0) > 0:
            return "high"
        if int(f.get("deficiency_medium_high") or 0) > 0:
            return "medium_high"
        if int(f.get("deficiency_medium") or 0) > 0:
            return "medium"
        return "clean"

    tier_colors = {
        "high":        ("red",    "remove-sign"),
        "medium_high": ("orange", "warning-sign"),
        "medium":      ("beige",  "exclamation-sign"),
        "clean":       ("green",  "ok-sign"),
    }

    counts_by_tier = {"high": 0, "medium_high": 0, "medium": 0, "clean": 0}

    m = folium.Map(location=[30.2672, -97.7431], zoom_start=11, tiles="CartoDB positron")

    fg_objects = {}
    fg_clusters = {}
    for tier in ("high", "medium_high", "medium", "clean"):
        fg = folium.FeatureGroup(name=tier, show=True, overlay=True)
        cluster = MarkerCluster().add_to(fg)
        fg.add_to(m)
        fg_objects[tier] = fg
        fg_clusters[tier] = cluster

    for f in facilities:
        lat = f["_lat"]
        lon = f["_lon"]
        tier = _deficiency_tier(f)
        counts_by_tier[tier] += 1

        name = (f.get("operation_name") or "Unknown").strip().title()
        op_type = (f.get("operation_type") or "").strip()
        address = (f.get("location_address") or "").strip()
        capacity = f.get("total_capacity") or ""
        high = int(f.get("deficiency_high") or 0)
        med_hi = int(f.get("deficiency_medium_high") or 0)
        med = int(f.get("deficiency_medium") or 0)
        adverse = (f.get("adverse_action") or "").upper() == "YES"
        temp_closed = (f.get("temporarily_closed") or "").upper() == "YES"
        status = (f.get("operation_status") or "").upper()

        flags = []
        if adverse:
            flags.append("🚨 Adverse action")
        if temp_closed:
            flags.append("🔒 Temp closed")
        flags_html = "<br/>".join(f"<b>{fl}</b>" for fl in flags)
        flags_block = f"{flags_html}<br/>" if flags_html else ""

        deficiency_line = ""
        if high or med_hi or med:
            deficiency_line = (
                f"<b>Deficiencies:</b> "
                f"🔴 {high} high &nbsp; 🟠 {med_hi} med-high &nbsp; 🟡 {med} medium<br/>"
            )

        capacity_line = f"<b>Capacity:</b> {capacity} children<br/>" if capacity else ""

        popup_html = f"""
        <div style="font-family:sans-serif;max-width:300px;">
            <b style="font-size:13px;">{name}</b><br/>
            <span style="color:#666;font-size:11px;">{op_type}</span><br/>
            <span style="color:#888;font-size:11px;">{address}</span><br/><br/>
            {flags_block}
            {deficiency_line}
            {capacity_line}
            <span style="color:#666;font-size:11px;">Status: {'Active' if status == 'Y' else status}</span>
        </div>
        """
        popup = folium.Popup(popup_html, max_width=300)
        color, icon_name = tier_colors[tier]
        tooltip = f"{name} — {'⚠️ ' + str(high+med_hi) + ' deficiencies' if (high or med_hi) else 'Clean'}"
        folium.Marker(
            location=[lat, lon],
            popup=popup,
            icon=folium.Icon(color=color, icon=icon_name, prefix="glyphicon"),
            tooltip=tooltip,
        ).add_to(fg_clusters[tier])

    counts_js = json.dumps(counts_by_tier)
    map_var = m.get_name()
    layer_map_js = "{" + ", ".join(f'"{k}": {fg_objects[k].get_name()}' for k in fg_objects) + "}"

    panel_html = f"""
    <div id="map-panel" style="position:absolute;top:10px;left:50%;transform:translateX(-50%);
                background:white;padding:10px 16px;border-radius:6px;
                box-shadow:0 2px 6px rgba(0,0,0,0.3);z-index:9999;
                font-family:sans-serif;text-align:center;min-width:340px;">
        <b style="font-size:15px;">🧒 Austin Child Care Licensing Compliance</b><br/>
        <span id="map-summary" style="font-size:12px;color:#555;"></span>
        <div style="display:flex;justify-content:center;gap:4px;margin-top:7px;flex-wrap:wrap;">
            <button id="btn-high" onclick="toggleTier('high')" class="fbtn active">🔴 High Risk</button>
            <button id="btn-medium_high" onclick="toggleTier('medium_high')" class="fbtn active">🟠 Med-High</button>
            <button id="btn-medium" onclick="toggleTier('medium')" class="fbtn active">🟡 Medium</button>
            <button id="btn-clean" onclick="toggleTier('clean')" class="fbtn active">🟢 Clean</button>
        </div>
    </div>
    <style>
        .fbtn {{ padding:3px 9px;border:1px solid #ccc;border-radius:4px;background:#f5f5f5;cursor:pointer;font-size:12px;color:#444; }}
        .fbtn.active {{ background:#2563eb;color:white;border-color:#2563eb; }}
        .fbtn:hover:not(.active) {{ background:#e0e7ff; }}
    </style>
    <script>
        var tierVisible = {{high: true, medium_high: true, medium: true, clean: true}};
        var layerMap = null;
        var leafletMap = null;
        var tierCounts = {counts_js};

        function updateSummary() {{
            var shown = Object.keys(tierVisible).filter(function(t) {{ return tierVisible[t]; }});
            var total = shown.reduce(function(s, t) {{ return s + (tierCounts[t] || 0); }}, 0);
            document.getElementById('map-summary').textContent =
                total + ' facilities shown \u00b7 click a marker for details';
        }}

        function initLayers() {{
            layerMap = {layer_map_js};
            leafletMap = {map_var};
            updateSummary();
        }}

        function toggleTier(tier) {{
            tierVisible[tier] = !tierVisible[tier];
            document.getElementById('btn-' + tier).classList.toggle('active');
            var layer = layerMap[tier];
            if (tierVisible[tier]) {{
                if (!leafletMap.hasLayer(layer)) leafletMap.addLayer(layer);
            }} else {{
                if (leafletMap.hasLayer(layer)) leafletMap.removeLayer(layer);
            }}
            updateSummary();
        }}

        document.addEventListener('DOMContentLoaded', function() {{
            setTimeout(initLayers, 1000);
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
            os.unlink(tmp_path)
        except Exception:
            pass

    summary = (
        f"🧒 *Child Care Licensing Compliance Map*\n"
        f"_Current facility compliance snapshot_\n\n"
        f"📊 *{total:,} facilities mapped*\n"
        f"🔴 *{high_risk:,} high-risk*  ·  ⚠️ *{flagged:,} with deficiencies*\n\n"
        f"Markers colored by deficiency level. Tap to see details."
    )
    return buffer, summary
