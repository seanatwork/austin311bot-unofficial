#!/usr/bin/env python3
"""
Generate docs/fun/data.json — pre-generated Fun Data cards.

Replaces live browser API calls for:
  1. Bar of the Month        (TABC Mixed Beverage Sales)
  2. Coyote Sightings        (Open311 ACCOYTE)
  3. Graffiti Hall of Fame   (Open311 HHSGRAFF)
  4. Parking Shenanigans     (Open311 PARKINGV)
  5. Graffiti Speedrun Stats (Open311 HHSGRAFF resolution times)

Run:  python scripts/generate_fun_data.py
Output: docs/fun/data.json
"""
import json
import logging
import os
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

from open311_client import open311_get

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(message)s")

OPEN311_URL = "https://311.austintexas.gov/open311/v2/requests.json"
SOCRATA_BASE = "https://data.texas.gov/resource"
TABC_DATASET = "g5bj-yb6k"

_session: Optional[requests.Session] = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({
            "Accept": "application/json",
            "User-Agent": "austin311bot/fun-data (pre-generator)",
        })
    return _session


def _app_token() -> str:
    return os.getenv("AUSTINAPIKEY", "")


def _fetch_open311(params: dict) -> Optional[list]:
    """Fetch from Open311 using the shared retry-aware client (respects Retry-After, 8 retries)."""
    session = _get_session()
    token = _app_token()
    if token:
        params = {**params, "$$app_token": token}
    try:
        return open311_get(session, OPEN311_URL, params)
    except Exception as e:
        logger.warning(f"Open311 fetch failed: {e}")
        return None


def _fetch_json(url: str, params: dict, max_retries: int = 3) -> Optional[list]:
    """Fetch JSON from a non-Open311 API (Socrata/TABC) with basic retries."""
    session = _get_session()
    token = _app_token()
    if token:
        params["$$app_token"] = token

    for attempt in range(max_retries):
        try:
            resp = session.get(url, params=params, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):
                return data
            if isinstance(data, dict) and "count" in data:
                return [data]
            logger.warning(f"Unexpected response format: {type(data)}")
            return None
        except Exception as e:
            if attempt == max_retries - 1:
                logger.warning(f"Fetch failed after {max_retries} attempts: {e}")
                return None
            time.sleep(2 ** attempt * 2)
    return None


# ── Helpers ──────────────────────────────────────────────────────────────────

MONTH_NAMES = ["January", "February", "March", "April", "May", "June",
               "July", "August", "September", "October", "November", "December"]


def month_label(date_str: str) -> str:
    """Convert '2026-04-01' to 'April 2026'."""
    parts = date_str[:7].split("-")
    if len(parts) == 2:
        y, m = parts
        return f"{MONTH_NAMES[int(m) - 1]} {y}"
    return date_str


def title_case(s: str) -> str:
    return re.sub(r"\w\S*", lambda m: m.group(0).capitalize(), (s or "").strip())


def fmt_dollars(val: float) -> str:
    if val >= 1_000_000:
        return f"${val / 1_000_000:.1f}M"
    if val >= 1_000:
        return f"${int(val / 1_000)}K"
    return f"${val:,.0f}"


def fmt_hour(h: int) -> str:
    if h == 0:
        return "12am"
    if h < 12:
        return f"{h}am"
    if h == 12:
        return "12pm"
    return f"{h - 12}pm"


def fmt_days(days: float) -> str:
    """Format a number of days into a human-readable string."""
    if days < 1:
        hours = int(days * 24)
        return f"{hours}h" if hours > 0 else "<1h"
    if days < 30:
        return f"{days:.1f}d"
    if days < 365:
        months = int(days / 30)
        return f"{months}mo"
    return f"{days / 365:.1f}y"


# ── 1. Bar of the Month ──────────────────────────────────────────────────────

def _load_bars() -> Optional[dict]:
    """Fetch TABC mixed beverage sales data for Austin."""
    logger.info("Loading Bar of the Month…")
    tabc_url = f"{SOCRATA_BASE}/{TABC_DATASET}.json"

    # Get the 4 most recent months
    months = _fetch_json(tabc_url, {
        "$select": "obligation_end_date,count(*) as cnt",
        "$where": "upper(location_city)='AUSTIN'",
        "$group": "obligation_end_date",
        "$order": "obligation_end_date DESC",
        "$limit": 4,
    })
    if not months or len(months) < 2:
        logger.warning("Not enough monthly data for bars")
        return None

    months.sort(key=lambda m: m["obligation_end_date"], reverse=True)
    counts = [int(m["cnt"]) for m in months]
    # Skip most recent if incomplete
    start_idx = 1 if counts[0] < counts[1] * 0.5 else 0
    cur_date = months[start_idx]["obligation_end_date"][:10]
    prev_date = months[start_idx + 1]["obligation_end_date"][:10]

    def build_where(date: str) -> str:
        return f"upper(location_city)='AUSTIN' AND obligation_end_date='{date}T00:00:00.000'"

    cur_rows = _fetch_json(tabc_url, {
        "$select": "tabc_permit_number,location_name,location_address,total_sales_receipts",
        "$where": build_where(cur_date),
        "$order": "total_sales_receipts DESC",
        "$limit": 5000,
    })
    prev_rows = _fetch_json(tabc_url, {
        "$select": "tabc_permit_number,location_name,location_address,total_sales_receipts",
        "$where": build_where(prev_date),
        "$order": "total_sales_receipts DESC",
        "$limit": 5000,
    })
    if not cur_rows or not prev_rows:
        return None

    def dedup(rows):
        seen = set()
        result = {}
        for r in rows:
            addr = (r.get("location_address") or "").strip().upper()
            sales = float(r.get("total_sales_receipts") or 0)
            key = f"{addr}|{sales}"
            if key in seen:
                continue
            seen.add(key)
            result[r["tabc_permit_number"]] = {
                "name": title_case(r.get("location_name", "Unknown")),
                "address": title_case(r.get("location_address", "")),
                "sales": sales,
            }
        return result

    cur = dedup(cur_rows)
    prev = dedup(prev_rows)

    top10 = sorted(cur.values(), key=lambda x: -x["sales"])[:10]

    movers = []
    for permit, c in cur.items():
        if permit in prev and prev[permit]["sales"] > 0:
            delta = c["sales"] - prev[permit]["sales"]
            if delta > 0:
                movers.append({
                    "name": c["name"],
                    "address": c["address"],
                    "delta": delta,
                    "pct": round(delta / prev[permit]["sales"] * 100, 1),
                })
    movers.sort(key=lambda x: -x["delta"])
    top5_movers = movers[:5]

    result = {
        "top10": top10,
        "top5movers": top5_movers,
        "curDate": cur_date,
        "prevDate": prev_date,
        "curLabel": month_label(cur_date),
        "prevLabel": month_label(prev_date),
    }
    logger.info(f"  ✓ Bar of the Month: {len(top10)} locations, {len(top5_movers)} movers")
    return result


# ── 2. Coyote Sightings ──────────────────────────────────────────────────────

def _load_coyotes() -> Optional[dict]:
    """Fetch coyote complaints from Open311."""
    logger.info("Loading Coyote Sightings…")
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=365)
    start_str = start.strftime("%Y-%m-%dT%H:%M:%SZ")

    records = _fetch_open311({
        "service_code": "ACCOYTE",
        "start_date": start_str,
        "per_page": 500,
        "page": 1,
    })
    if not records:
        return None

    # Monthly counts
    monthly = Counter()
    pupping = 0
    for r in records:
        ts = r.get("requested_datetime", "")
        if not ts:
            continue
        try:
            d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            key = f"{d.year}-{d.month:02d}"
            monthly[key] += 1
            if d.month in (3, 4, 5):
                pupping += 1
        except (ValueError, TypeError):
            continue

    sorted_months = sorted(monthly.items(), key=lambda x: x[0], reverse=True)[:12]
    max_monthly = max((c for _, c in sorted_months), default=1)
    total = sum(monthly.values())
    pupping_pct = round(pupping / total * 100) if total else 0
    is_peak = any(k.endswith("-03") or k.endswith("-04") or k.endswith("-05") for k, _ in sorted_months[:3])

    month_data = []
    for key, cnt in sorted_months:
        y, m = key.split("-")
        label = f"{MONTH_NAMES[int(m) - 1][:3]} {y}"
        is_pupping = key.endswith("-03") or key.endswith("-04") or key.endswith("-05")
        pct = round(cnt / max_monthly * 100)
        month_data.append({
            "label": label,
            "count": cnt,
            "pct": pct,
            "isPupping": is_pupping,
        })

    result = {
        "total": total,
        "pupping": pupping,
        "puppingPct": pupping_pct,
        "isPeak": is_peak,
        "months": month_data,
    }
    logger.info(f"  ✓ Coyote Sightings: {total} total, {pupping} pupping season")
    return result


# ── 3. Graffiti Hall of Fame ─────────────────────────────────────────────────

def _load_graffiti() -> Optional[dict]:
    """Fetch graffiti complaints from Open311."""
    logger.info("Loading Graffiti Hall of Fame…")
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=365)
    start_str = start.strftime("%Y-%m-%dT%H:%M:%SZ")

    records = _fetch_open311({
        "service_code": "HHSGRAFF",
        "start_date": start_str,
        "per_page": 500,
        "page": 1,
    })
    if not records:
        return None

    addr_count = Counter()
    addr_details = {}
    total = 0

    for r in records:
        addr = (r.get("address") or "").replace(", Austin", "").replace(", TX", "").strip().upper()
        if not addr:
            continue
        total += 1
        addr_count[addr] += 1
        if addr not in addr_details or (r.get("requested_datetime") or "") > (addr_details[addr].get("lastDate") or ""):
            addr_details[addr] = {
                "address": title_case(r.get("address", "")).replace(", Austin", "").replace(", Tx", ""),
                "status": "✅ Cleaned" if (r.get("status") or "").lower() == "closed" else "🔴 Tagged",
                "lastDate": r.get("requested_datetime", ""),
                "lat": r.get("lat"),
                "lon": r.get("long"),
            }

    sorted_locations = sorted(
        ((addr, addr_count[addr], addr_details[addr]) for addr in addr_count),
        key=lambda x: -x[1]
    )
    top15 = [{"address": d["address"], "count": c, "status": d["status"],
              "lat": d["lat"], "lon": d["lon"]} for addr, c, d in sorted_locations[:15]]

    winner = top15[0] if top15 else None
    unique_locations = len(addr_count)
    repeat_rate = round((total - unique_locations) / total * 100) if total else 0

    result = {
        "total": total,
        "uniqueLocations": unique_locations,
        "repeatRate": repeat_rate,
        "winner": winner,
        "top15": top15,
    }
    logger.info(f"  ✓ Graffiti Hall of Fame: {total} reports, {unique_locations} unique locations")
    return result


# ── 4. Parking Shenanigans ───────────────────────────────────────────────────

def _extract_violation_type(desc: str) -> str:
    if not desc:
        return "Other"
    d = desc.lower()
    patterns = [
        ("Bike Lane", ["bike lane", "bicycle"]),
        ("Sidewalk", ["sidewalk"]),
        ("Fire Hydrant", ["fire hydrant", "hydrant"]),
        ("Handicap", ["handicap", "ada", "accessible"]),
        ("Driveway", ["driveway"]),
        ("Crosswalk", ["crosswalk"]),
        ("Bus Stop", ["bus stop"]),
        ("Alley", ["alley"]),
        ("No Parking Zone", ["no parking"]),
        ("Abandoned", ["abandoned"]),
        ("Overnight/Camping", ["overnight", "camping", "living in"]),
        ("Loading Zone", ["loading"]),
        ("Construction Zone", ["construction"]),
        ("Street Sweeping", ["street sweeping"]),
        ("Tow Zone", ["tow zone"]),
        ("Double Parking", ["double park"]),
        ("Fire Lane", ["fire lane"]),
        ("Oversized Vehicle", ["truck", "rv", "trailer", "boat"]),
        ("On Grass/Yard", ["grass", "yard"]),
        ("Dumpster", ["dumpster"]),
        ("Blocking", ["blocking"]),
    ]
    for vtype, keywords in patterns:
        if any(k in d for k in keywords):
            return vtype
    return "Other"


def _load_parking() -> Optional[dict]:
    """Fetch parking complaints from Open311."""
    logger.info("Loading Parking Shenanigans…")
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=90)
    start_str = start.strftime("%Y-%m-%dT%H:%M:%SZ")

    records = _fetch_open311({
        "service_code": "PARKINGV",
        "start_date": start_str,
        "per_page": 500,
        "page": 1,
    })
    if not records:
        return None

    hourly_counts = Counter()
    violation_types = Counter()
    total = 0

    for r in records:
        ts = r.get("requested_datetime", "")
        if not ts:
            continue
        total += 1

        try:
            d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            # Approximate CDT (UTC-5)
            local_hour = (d.hour - 5) % 24
            hourly_counts[local_hour] += 1
        except (ValueError, TypeError):
            pass

        desc = r.get("description") or ""
        if desc:
            vtype = _extract_violation_type(desc)
            violation_types[vtype] += 1

    sorted_hours = sorted(hourly_counts.items())
    max_hourly = max((c for _, c in sorted_hours), default=1)
    peak_hour = max(hourly_counts, key=hourly_counts.get) if hourly_counts else None
    peak_count = hourly_counts.get(peak_hour, 0) if peak_hour else 0

    hour_data = []
    for h, cnt in sorted_hours:
        pct = round(cnt / max_hourly * 100)
        hour_data.append({
            "hour": h,
            "label": fmt_hour(h),
            "count": cnt,
            "pct": pct,
            "isPeak": h == peak_hour,
        })

    sorted_types = sorted(violation_types.items(), key=lambda x: -x[1])
    max_type = sorted_types[0][1] if sorted_types else 1
    type_data = []
    for vtype, cnt in sorted_types[:8]:
        pct = round(cnt / max_type * 100)
        type_data.append({"type": vtype, "count": cnt, "pct": pct})

    result = {
        "total": total,
        "peakHour": fmt_hour(peak_hour) if peak_hour is not None else None,
        "peakCount": peak_count,
        "hours": hour_data,
        "violationTypes": type_data,
    }
    logger.info(f"  ✓ Parking Shenanigans: {total} citations, peak at {result['peakHour']}")
    return result


# ── 5. Graffiti Speedrun Stats ───────────────────────────────────────────────

def _load_graffiti_speedrun() -> Optional[dict]:
    """Calculate graffiti resolution time stats from Open311."""
    logger.info("Loading Graffiti Speedrun Stats…")
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=365)
    start_str = start.strftime("%Y-%m-%dT%H:%M:%SZ")

    records = _fetch_open311({
        "service_code": "HHSGRAFF",
        "start_date": start_str,
        "per_page": 500,
        "page": 1,
    })
    if not records:
        return None

    resolution_times = []  # in days
    for r in records:
        status = (r.get("status") or "").lower()
        if status != "closed":
            continue
        req_ts = r.get("requested_datetime")
        upd_ts = r.get("updated_datetime")
        if not req_ts or not upd_ts:
            continue
        try:
            req = datetime.fromisoformat(req_ts.replace("Z", "+00:00"))
            upd = datetime.fromisoformat(upd_ts.replace("Z", "+00:00"))
            delta = (upd - req).total_seconds() / 86400
            if delta >= 0:
                resolution_times.append(delta)
        except (ValueError, TypeError):
            continue

    if not resolution_times:
        return None

    resolution_times.sort()
    n = len(resolution_times)
    avg = sum(resolution_times) / n
    median = resolution_times[n // 2] if n % 2 else (resolution_times[n // 2 - 1] + resolution_times[n // 2]) / 2

    # Fastest 5
    fastest = [round(t, 1) for t in resolution_times[:5]]

    # Distribution buckets
    buckets = {
        "under1h": sum(1 for t in resolution_times if t < 1 / 24),
        "1hTo6h": sum(1 for t in resolution_times if 1 / 24 <= t < 6 / 24),
        "6hTo24h": sum(1 for t in resolution_times if 6 / 24 <= t < 1),
        "1dTo3d": sum(1 for t in resolution_times if 1 <= t < 3),
        "3dTo7d": sum(1 for t in resolution_times if 3 <= t < 7),
        "7dTo30d": sum(1 for t in resolution_times if 7 <= t < 30),
        "over30d": sum(1 for t in resolution_times if t >= 30),
    }

    # Longest open
    longest_open = None
    for r in records:
        status = (r.get("status") or "").lower()
        if status != "open":
            continue
        req_ts = r.get("requested_datetime")
        if not req_ts:
            continue
        try:
            req = datetime.fromisoformat(req_ts.replace("Z", "+00:00"))
            days_open = (now - req).total_seconds() / 86400
            if longest_open is None or days_open > longest_open["days"]:
                longest_open = {
                    "days": round(days_open, 1),
                    "address": title_case(r.get("address", "Unknown")),
                }
        except (ValueError, TypeError):
            continue

    result = {
        "totalClosed": n,
        "avgDays": round(avg, 1),
        "medianDays": round(median, 1),
        "fastestDays": fastest,
        "buckets": {k: {"count": v, "label": _bucket_label(k)} for k, v in buckets.items()},
        "longestOpen": longest_open,
    }
    logger.info(f"  ✓ Graffiti Speedrun: {n} closed tickets, avg {avg:.1f}d")
    return result


def _bucket_label(key: str) -> str:
    labels = {
        "under1h": "Under 1 hour",
        "1hTo6h": "1–6 hours",
        "6hTo24h": "6–24 hours",
        "1dTo3d": "1–3 days",
        "3dTo7d": "3–7 days",
        "7dTo30d": "7–30 days",
        "over30d": "Over 30 days",
    }
    return labels.get(key, key)


# ── 6. Funniest Descriptions Ticker ──────────────────────────────────────────

# Profanity censor map — bleep out offensive words while keeping the shock value
_PROFANITY_CENSOR = {
    "fucking": "f***ing",
    "fuck": "f***",
    "fucker": "f***er",
    "fucked": "f***ed",
    "shit": "sh**",
    "bitch": "b****",
    "bitches": "b****es",
    "damn": "d***",
    "ass": "a**",
    "bastard": "b*****d",
}


def _censor(text: str) -> str:
    """Bleep out profanity while keeping the shock value."""
    for word, replacement in _PROFANITY_CENSOR.items():
        # Case-insensitive replace, preserving original casing where possible
        import re as _re
        text = _re.sub(_re.escape(word), replacement, text, flags=_re.IGNORECASE)
        text = _re.sub(_re.escape(word.capitalize()), replacement.capitalize(), text)
        text = _re.sub(_re.escape(word.upper()), replacement.upper(), text)
    return text


# Keywords that indicate shock-value / entertaining descriptions
_FUNNY_KEYWORDS = [
    # Anger/frustration
    "fucking", "fuck", "shit", "useless", "pathetic", "begging", "squash",
    "unbearable", "insane", "blast", "blasting", "booming", "BOOM",
    "sick of", "do something", "please help", "please send",
    # Absurd situations
    "cow", "rooster", "loose chicken", "parakeet", "turkey", "raccoon",
    "python", "snake", "possum", "roadrunner",
    "shack", "bums", "tents", "vagrants",
    "sex acts", "underage", "camping in", "playscape",
    # Specificity / color
    "stared me down", "foaming", "rabid", "mange", "skin and bone",
    "malnourished", "abuse", "attacking",
    # Venue complaints
    "citation issued", "permit", "decibel", "cutoff", "curfew",
    "after hours", "past midnight", "2am", "3am", "4am",
    "apartment shaking", "walls shake", "vibrating",
    # Desperation
    "can't sleep", "cannot sleep", "go to bed", "ruining",
    "make it stop", "make this stop", "send help",
    "nobody does", "nothing done", "no one came",
]

# Codes most likely to yield entertaining descriptions (ranked by prior research)
_FUNNY_CODES = [
    ("DSOUCVMC", "Outdoor Music Venue"),
    ("APDNONNO", "Noise Complaint"),
    ("ACLONAG", "Loose Dog"),
    ("ACPROPER", "Animal Care"),
    ("WILDEXPO", "Wildlife"),
    ("ACLOANIM", "Loose Animal"),
    ("PRGRDISS", "Homeless/Grounds"),
    ("OBSTMIDB", "Obstruction"),
    ("PARKINGV", "Parking"),
    ("SBDEBROW", "Debris"),
    ("DSDENVCO", "Tree/Environmental"),
    ("ACCOYTE", "Coyote"),
    ("SIGNSTRE", "Street Sign"),
    ("ZZARSTSW", "Street Sweeping"),
    ("SBGENRL", "Street Misc"),
    ("AFDFIREW", "Fireworks"),
    ("SBPOTREP", "Pothole"),
    ("DRFLOODG", "Flooding"),
    ("SWSSTORM", "Storm Debris"),
    ("STREETL2", "Street Light"),
    ("HHSGRAFF", "Graffiti"),
    ("TRASIGMA", "Traffic Signal"),
    ("DRCHANEL", "Drainage"),
    ("ACBITE2", "Animal Bite"),
    ("COAACDD", "Vicious Dog"),
]


def _is_funny_description(text: str) -> bool:
    """Check if a description has shock/entertainment value."""
    if not text or len(text.strip()) < 25:
        return False
    t = text.lower()
    # Skip procedural/auto-generated responses
    skip_patterns = [
        "unable to make contact", "contact made", "general broadcast",
        "sent text message", "close sr", "job#", "replaced head",
        "sweep - completed", "deferred to", "no problem found",
        "cite vehicle", "cleaned graffiti", "private property",
        "referred to 311", "inspection performed", "no action needed",
        "future work scheduled", "vegetation maintenance",
        "no work planned", "citation issued", "investigated",
        "work order", "duplicate", "resolved",
    ]
    for pat in skip_patterns:
        if pat in t:
            return False
    # Check for shock-value keywords
    for kw in _FUNNY_KEYWORDS:
        if kw.lower() in t:
            return True
    return False


def _load_funny_descriptions() -> Optional[dict]:
    """Fetch ~2500 records across 25 service codes, find the funniest descriptions."""
    logger.info("Loading Funniest Descriptions…")

    all_candidates = []
    total_fetched = 0
    PAGES_PER_CODE = 5  # 500 records per code

    for code, category in _FUNNY_CODES:
        code_records = 0
        code_hits = 0
        for page in range(1, PAGES_PER_CODE + 1):
            records = _fetch_open311({
                "service_code": code,
                "per_page": 100,
                "page": page,
            })
            if not records:
                break
            code_records += len(records)

            for r in records:
                desc = r.get("description", "") or ""
                notes = r.get("status_notes", "") or ""
                text = desc.strip() if len(desc.strip()) > len(notes.strip()) else notes.strip()

                if _is_funny_description(text):
                    all_candidates.append({
                        "text": _censor(text)[:300],
                        "category": category,
                        "code": code,
                        "address": (r.get("address") or "").strip(),
                        "date": (r.get("requested_datetime") or "")[:10],
                        "id": r.get("service_request_id", ""),
                    })
                    code_hits += 1

            if len(records) < 100:
                break
            time.sleep(0.5)

        total_fetched += code_records
        logger.info(f"  {code} ({category}): {code_records} rec, {code_hits} funny")
        time.sleep(0.5)

    if not all_candidates:
        return None

    # Deduplicate by first 60 chars
    seen_texts = set()
    unique = []
    for d in sorted(all_candidates, key=lambda x: len(x["text"])):
        key = d["text"][:60].lower()
        if key not in seen_texts:
            seen_texts.add(key)
            unique.append(d)

    # Pick a smaller, varied set for the front-page ticker (cap 40),
    # but keep the full deduped pool for the hub-and-spoke /funny page.
    import random
    full = list(unique)
    random.shuffle(full)
    ticker = full[:40]
    ticker.sort(key=lambda x: x["category"])

    # All items, sorted by category then date desc — used by docs/funny/.
    all_sorted = sorted(
        unique,
        key=lambda x: (x["category"], x.get("date", "")),
        reverse=False,
    )
    all_sorted.sort(key=lambda x: x.get("date", ""), reverse=True)
    all_sorted.sort(key=lambda x: x["category"])

    result = {
        "totalFetched": total_fetched,
        "totalCandidates": len(all_candidates),
        "selected": len(ticker),
        "items": ticker,
        "allItems": all_sorted,
    }
    logger.info(f"  ✓ Funniest Descriptions: {total_fetched} fetched, {len(all_candidates)} candidates, {len(ticker)} in ticker, {len(all_sorted)} total unique")
    return result


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    now = datetime.now(timezone.utc)
    logger.info(f"Generating fun/data.json at {now.isoformat()}")

    data = {
        "updated": now.isoformat(),
        "bars": _load_bars(),
        "coyotes": _load_coyotes(),
        "graffiti": _load_graffiti(),
        "parking": _load_parking(),
        "graffitiSpeedrun": _load_graffiti_speedrun(),
        "funnyDescriptions": _load_funny_descriptions(),
    }

    out_path = Path(__file__).resolve().parent.parent / "docs" / "fun" / "data.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    logger.info(f"Wrote {out_path.stat().st_size:,} bytes to {out_path}")


if __name__ == "__main__":
    main()