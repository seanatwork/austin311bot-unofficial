"""
Child Care Licensing — data layer and formatters.

Queries HHSC Child Care Licensing datasets on data.texas.gov:
  bc5r-88dy  — facility master (operation_name, type, capacity, flags, deficiency counts)
  tqgd-mf4x  — non-compliance detail (violations, risk level, narrative)

Joins on operation_id to show Austin-specific summaries and top violators.
"""

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
