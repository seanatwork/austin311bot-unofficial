#!/usr/bin/env python3
"""
Generate pre-computed query datasets for the austin311.com NL query system.

Produces JSON files in docs/querystore/:
  - daily_counts.json     — counts by category × date × status (365 days)
  - district_counts.json  — counts by category × council district × status (90d/365d)
  - resolution_stats.json — avg/median/p90 resolution times by category (90d)
  - monthly_aggregates.json — monthly counts with day-of-week breakdowns
  - service_codes.json    — code → name mapping for all service codes

Run:  AUSTINAPIKEY=sk... python scripts/generate_query_data.py

The script fetches Open311 data month-by-month for all 10 categories,
caches results via open311_cache.py, and writes output to docs/querystore/.
"""

import json
import logging
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Optional, Dict, List, Any

# Add project root to path so we can import geolocation and open311_client
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from open311_client import open311_get
from categories import CATEGORY_CODES, CATEGORY_NAMES

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(message)s")

OPEN311_URL = "https://311.austintexas.gov/open311/v2/requests.json"
OUTPUT_DIR = Path("docs/querystore")
LOOKBACK_DAYS = 365
MAX_PAGES = 10
PER_PAGE = 100
API_KEY = os.getenv("AUSTINAPIKEY", "")

# ── Per-category record filters ─────────────────────────────────────────────
# Applied at aggregation time (the cache mirrors raw Open311 records).
# The homeless category counts keyword-matched encampment reports only —
# the same filter homeless/homeless_bot.py applies to the map — so the
# chart and the map report the same definition of "homeless complaint".
from homeless.homeless_bot import is_encampment_report

CATEGORY_RECORD_FILTERS = {
    "homeless": is_encampment_report,
}

# ── Service code → human-readable name ──────────────────────────────────────
SERVICE_CODE_NAMES: Dict[str, str] = {
    # Homeless
    "PRGRDISS": "Park Maintenance - Grounds",
    "ATCOCIRW": "Construction in Right of Way",
    "OBSTMIDB": "Obstruction in Right of Way",
    "SBDEBROW": "Debris in Street",
    "DRCHANEL": "Channels/Creeks/Drainage",
    # Parking
    "PARKINGV": "Parking Violation",
    # Noise
    "APDNONNO": "Noise Complaint",
    "DSOUCVMC": "Outdoor Music Venue",
    "AFDFIREW": "Fireworks",
    # Animal
    "ACLONAG": "Loose Dog",
    "ACLOANIM": "Loose Animal",
    "ACBITE2": "Animal Bite",
    "COAACDD": "Vicious Dog",
    "ACPROPER": "Animal Care",
    "WILDEXPO": "Wildlife",
    "ACINFORM": "Animal Protection",
    "ACCOYTE": "Coyote Sighting",
    # Graffiti
    "HHSGRAFF": "Graffiti",
    # Parks
    "PRGRDPLB": "Park Playgrounds",
    "PRGRDELC": "Park Electrical",
    "PRBLDPLB": "Park Building Plumbing",
    "PRBLDISS": "Park Building Issue",
    "PRBLDACH": "Park Building ADA",
    "PRBLDELE": "Park Building Electric",
    "COMPARLN": "Park Comparable",
    "PRCEMET1": "Cemetery",
    # Storm
    "SWSSTORM": "Storm Debris",
    "DRILID": "Drainage Inlet",
    "DRFLOODG": "Flooding",
    "DRSSPIPE": "Storm Pipe",
    "DRFLOODR": "Flood Risk",
    "ZZEROSIO": "Erosion",
    "DRDITCH": "Ditch",
    # Traffic
    "SBPOTREP": "Pothole",
    "TRASIGMA": "Traffic Signal",
    "STREETL2": "Street Light",
    "ATTRSIMO": "Traffic Sign Maintenance",
    "SIGNSTRE": "Street Sign",
    "OBSINTTR": "Traffic Obstruction",
    "SBSIDERE": "Sidewalk Repair",
    "SBSTRES": "Street Repair",
    "ZZARSTSW": "Street Sweeping",
    "PWTRISRW": "Tree in ROW",
    "SBGENRL": "Street Misc",
    "SIGNNEWT": "New Sign Request",
    "TRASIGNE": "Traffic Signal New",
    "TPPECRNE": "Traffic Calming",
    # Bicycle
    "PWBICYCL": "Bicycle Issue",
    # Dead Animal
    "ZZARDEAC": "Dead Animal",
    # Water
    "WWREPORT": "Water Waste",
    # Tree
    "DSDENVCO": "Tree/Environmental",
    "PATRISPA": "Tree/Parks",
}

_session: Optional[requests.Session] = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({
            "Accept": "application/json",
            "User-Agent": "austin311bot/query-data",
        })
    return _session


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _isoformat_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# ── Open311 fetching ────────────────────────────────────────────────────────

def _fetch_code_month(service_code: str, month_start: datetime) -> List[dict]:
    """Fetch all records for a single service code within one calendar month.

    Uses pagination to get all records for the month window.
    Returns a list of raw Open311 records.
    """
    month_end = month_start.replace(day=28) + timedelta(days=4)
    month_end = month_end - timedelta(days=month_end.day)
    month_end = month_end.replace(hour=23, minute=59, second=59)

    all_records: List[dict] = []
    session = _get_session()

    for page in range(1, MAX_PAGES + 1):
        params: Dict[str, Any] = {
            "service_code": service_code,
            "start_date": _isoformat_z(month_start),
            "end_date": _isoformat_z(month_end),
            "per_page": PER_PAGE,
            "page": page,
            "status": "open,closed",
        }
        if API_KEY:
            params["$$app_token"] = API_KEY

        try:
            records = open311_get(session, OPEN311_URL, params)
        except Exception as e:
            logger.warning(f"  {service_code} page {page}: fetch failed — {e}")
            break

        if not records:
            break

        all_records.extend(records)

        if len(records) < PER_PAGE:
            break

        time.sleep(1.0 if API_KEY else 2.0)

    return all_records


def _fetch_category_monthly(
    category: str,
    codes: List[str],
    months_back: int = 12,
    use_cache: bool = True,
) -> List[dict]:
    """Fetch records for a category, month by month, with optional caching.

    The Open311 API returns records oldest-first, so a single long date-range
    request only returns the oldest records before hitting the page cap.
    Month-by-month ensures every period is fully covered.

    Returns a flat list of records across all codes and months.
    """
    from open311_cache import (
        init_cache,
        get_cached_records,
        cache_records,
        get_last_fetch_date,
    )

    # Initialize cache
    if use_cache:
        init_cache()
        # Look up by service codes only (not category) — the cache mirrors raw
        # Open311 records and codes overlap across categories (e.g. PRGRDISS
        # is both homeless and parks), so rows may carry any category tag.
        cached = get_cached_records(service_codes=codes)
        cached_ids = {r.get("service_request_id") for r in cached}
        logger.info(f"  Cache: {len(cached)} existing records for {category}")

        last_fetch = get_last_fetch_date(service_codes=codes)
        if last_fetch and len(cached) > 0:
            cache_age = _utc_now() - last_fetch
            if cache_age < timedelta(days=6):
                logger.info(f"  Cache is fresh ({cache_age.days}d), returning cached data")
                return cached
    else:
        cached = []
        cached_ids = set()

    now = _utc_now()
    # Derive freshness from cached records even if metadata for this category
    # is missing (common for overlapping codes cached under another category).
    last_fetch = get_last_fetch_date(service_codes=codes)
    if not last_fetch and cached:
        latest_dates = [r.get("requested_datetime", "") for r in cached if r.get("requested_datetime")]
        if latest_dates:
            latest = max(latest_dates)
            try:
                last_fetch = datetime.fromisoformat(latest.replace("Z", "+00:00"))
            except ValueError:
                pass

    if use_cache and cached:
        if last_fetch and len(cached) > 0:
            cache_age = _utc_now() - last_fetch
            if cache_age < timedelta(days=6):
                logger.info(f"  Cache is fresh ({cache_age.days}d, latest: {last_fetch.date()}), returning cached data")
                return cached
        fetch_start = last_fetch - timedelta(days=1) if last_fetch else now - timedelta(days=30 * months_back)
    else:
        fetch_start = now - timedelta(days=30 * months_back)

    logger.info(f"  Fetching {category} from {fetch_start.date()} to {now.date()}")

    # Build monthly windows
    current = fetch_start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end_month = now.replace(day=1)

    all_records: List[dict] = list(cached)
    seen_ids: set = cached_ids.copy()
    new_records: List[dict] = []

    while current <= end_month:
        month_label = current.strftime("%Y-%m")
        month_new = 0
        for code in codes:
            records = _fetch_code_month(code, current)
            for r in records:
                sid = r.get("service_request_id")
                if sid and sid not in seen_ids:
                    seen_ids.add(sid)
                    new_records.append(r)
                    all_records.append(r)
                    month_new += 1
        logger.info(f"    {month_label}: {month_new} new records")
        time.sleep(0.5)

        if current.month == 12:
            current = current.replace(year=current.year + 1, month=1)
        else:
            current = current.replace(month=current.month + 1)

    # Cache new records
    if use_cache and new_records:
        cache_records(category, new_records)
        logger.info(f"  Cached {len(new_records)} new records for {category}")

    return all_records


# ── Aggregation functions ───────────────────────────────────────────────────

def _build_daily_counts(all_records: Dict[str, List[dict]]) -> Dict[str, Any]:
    """Build daily_counts.json from fetched records.

    Returns {days: {date: {category: {open, closed, total}}}, totals_90d: {...}, totals_365d: {...}}
    """
    # daily_counts: day → category → {open, closed, total}
    daily: Dict[str, Dict[str, Dict[str, int]]] = defaultdict(
        lambda: defaultdict(lambda: {"open": 0, "closed": 0, "total": 0})
    )
    totals_90d: Dict[str, Dict[str, int]] = defaultdict(
        lambda: {"open": 0, "closed": 0, "total": 0}
    )
    totals_365d: Dict[str, Dict[str, int]] = defaultdict(
        lambda: {"open": 0, "closed": 0, "total": 0}
    )

    now = _utc_now()
    cutoff_90d = now - timedelta(days=90)
    cutoff_365d = now - timedelta(days=365)

    for category, records in all_records.items():
        for r in records:
            dt_str = r.get("requested_datetime") or ""
            if not dt_str:
                continue
            day = dt_str[:10]  # YYYY-MM-DD

            status = (r.get("status") or "").lower()
            is_open = status == "open"

            daily[day][category]["total"] += 1
            if is_open:
                daily[day][category]["open"] += 1
            else:
                daily[day][category]["closed"] += 1

            # Rollup windows
            try:
                dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            except ValueError:
                continue

            if dt >= cutoff_90d:
                totals_90d[category]["total"] += 1
                if is_open:
                    totals_90d[category]["open"] += 1
                else:
                    totals_90d[category]["closed"] += 1

            if dt >= cutoff_365d:
                totals_365d[category]["total"] += 1
                if is_open:
                    totals_365d[category]["open"] += 1
                else:
                    totals_365d[category]["closed"] += 1

    # Convert defaultdicts to regular dicts for JSON
    days_sorted = dict(sorted(daily.items()))
    result: Dict[str, Any] = {
        "generated": _utc_now().isoformat(),
        "days": days_sorted,
        "totals_90d": dict(totals_90d),
        "totals_365d": dict(totals_365d),
    }
    return result


def _build_district_counts(all_records: Dict[str, List[dict]]) -> Dict[str, Any]:
    """Build district_counts.json with council district aggregation."""
    from geolocation import point_in_district

    now = _utc_now()
    cutoff_90d = now - timedelta(days=90)
    cutoff_365d = now - timedelta(days=365)

    windows: Dict[str, Dict[str, Dict[str, Dict[str, int]]]] = {
        "90d": defaultdict(lambda: defaultdict(lambda: {"open": 0, "closed": 0, "total": 0})),
        "365d": defaultdict(lambda: defaultdict(lambda: {"open": 0, "closed": 0, "total": 0})),
    }

    for category, records in all_records.items():
        for r in records:
            lat = r.get("lat")
            lon = r.get("long")

            # Skip records without coordinates
            if lat is None or lon is None:
                continue
            try:
                lat_f = float(lat)
                lon_f = float(lon)
            except (TypeError, ValueError):
                continue

            district = point_in_district(lat_f, lon_f)
            if district is None:
                continue
            dist_key = str(district)

            dt_str = r.get("requested_datetime") or ""
            try:
                dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            except ValueError:
                continue

            status = (r.get("status") or "").lower()
            is_open = status == "open"

            if dt >= cutoff_90d:
                windows["90d"][category][dist_key]["total"] += 1
                if is_open:
                    windows["90d"][category][dist_key]["open"] += 1
                else:
                    windows["90d"][category][dist_key]["closed"] += 1

            if dt >= cutoff_365d:
                windows["365d"][category][dist_key]["total"] += 1
                if is_open:
                    windows["365d"][category][dist_key]["open"] += 1
                else:
                    windows["365d"][category][dist_key]["closed"] += 1

    return {
        "generated": _utc_now().isoformat(),
        "windows": {
            "90d": {cat: dict(districts) for cat, districts in windows["90d"].items()},
            "365d": {cat: dict(districts) for cat, districts in windows["365d"].items()},
        },
    }


def _build_resolution_stats(all_records: Dict[str, List[dict]]) -> Dict[str, Any]:
    """Build resolution_stats.json — avg/median/p90 days to close by category."""
    now = _utc_now()
    cutoff_90d = now - timedelta(days=90)

    stats: Dict[str, Dict[str, Any]] = {}

    for category, records in all_records.items():
        resolution_days: List[int] = []

        for r in records:
            status = (r.get("status") or "").lower()
            if status == "open":
                continue  # Only count closed records

            req_str = r.get("requested_datetime") or ""
            upd_str = r.get("updated_datetime") or ""
            if not req_str or not upd_str:
                continue

            try:
                req_dt = datetime.fromisoformat(req_str.replace("Z", "+00:00"))
                upd_dt = datetime.fromisoformat(upd_str.replace("Z", "+00:00"))
            except ValueError:
                continue

            if req_dt < cutoff_90d:
                continue

            days = (upd_dt - req_dt).days
            if 0 <= days <= 730:  # Reasonable range, cap at 2 years
                resolution_days.append(days)

        if resolution_days:
            sorted_days = sorted(resolution_days)
            p90_idx = int(len(sorted_days) * 0.9)
            stats[category] = {
                "avg_days": round(sum(resolution_days) / len(resolution_days), 1),
                "median_days": median(sorted_days),
                "p90_days": sorted_days[min(p90_idx, len(sorted_days) - 1)],
                "count": len(resolution_days),
            }
        else:
            stats[category] = {
                "avg_days": None,
                "median_days": None,
                "p90_days": None,
                "count": 0,
            }

    return {
        "generated": _utc_now().isoformat(),
        "90d": stats,
    }


def _build_monthly_aggregates(all_records: Dict[str, List[dict]]) -> Dict[str, Any]:
    """Build monthly_aggregates.json with monthly counts and day-of-week breakdowns."""
    DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    monthly: Dict[str, Dict[str, Dict[str, int]]] = defaultdict(
        lambda: defaultdict(lambda: {"total": 0, "open": 0, "closed": 0})
    )
    dow: Dict[str, Dict[str, Dict[str, int]]] = defaultdict(
        lambda: defaultdict(lambda: {"total": 0, "open": 0, "closed": 0})
    )

    for category, records in all_records.items():
        for r in records:
            dt_str = r.get("requested_datetime") or ""
            if not dt_str:
                continue
            try:
                dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            except ValueError:
                continue

            month_key = dt.strftime("%Y-%m")
            day_key = DAY_NAMES[dt.weekday()]
            status = (r.get("status") or "").lower()
            is_open = status == "open"

            monthly[month_key][category]["total"] += 1
            if is_open:
                monthly[month_key][category]["open"] += 1
            else:
                monthly[month_key][category]["closed"] += 1

            dow[category][day_key]["total"] += 1
            if is_open:
                dow[category][day_key]["open"] += 1
            else:
                dow[category][day_key]["closed"] += 1

    # Sort months
    months_sorted = dict(sorted(monthly.items()))

    return {
        "generated": _utc_now().isoformat(),
        "months": {month: dict(categories) for month, categories in months_sorted.items()},
        "day_of_week": {cat: dict(days) for cat, days in dow.items()},
    }


def _build_service_code_index() -> Dict[str, Any]:
    """Build service_codes.json — code → {name, category} mapping."""
    index: Dict[str, Dict[str, str]] = {}

    for category, codes in CATEGORY_CODES.items():
        for code in codes:
            index[code] = {
                "name": SERVICE_CODE_NAMES.get(code, code),
                "category": category,
                "category_name": CATEGORY_NAMES.get(category, category),
            }

    return {
        "generated": _utc_now().isoformat(),
        "codes": index,
    }


# ── Main ────────────────────────────────────────────────────────────────────

def main() -> None:
    now = _utc_now()
    logger.info(f"=== Query data generation — {now.isoformat()} ===")
    logger.info(f"Categories: {len(CATEGORY_CODES)}")
    logger.info(f"Lookback: {LOOKBACK_DAYS} days")

    # Ensure output directory exists
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # ── Phase 1: Fetch all records ──────────────────────────────────────
    months_back = max(1, LOOKBACK_DAYS // 30) + 2  # +2 for padding

    all_records: Dict[str, List[dict]] = {}

    for category, codes in CATEGORY_CODES.items():
        name = CATEGORY_NAMES.get(category, category)
        logger.info(f"\n--- {name} ({len(codes)} codes) ---")

        records = _fetch_category_monthly(
            category=category,
            codes=codes,
            months_back=months_back,
            use_cache=True,
        )
        record_filter = CATEGORY_RECORD_FILTERS.get(category)
        if record_filter:
            before = len(records)
            records = [r for r in records if record_filter(r)]
            logger.info(f"  Filtered: {before} -> {len(records)} records")
        all_records[category] = records
        logger.info(f"  Total: {len(records)} records")

    # ── Phase 2: Build datasets ─────────────────────────────────────────
    logger.info("\n=== Building datasets ===")

    # Daily counts
    logger.info("Building daily_counts.json...")
    daily_counts = _build_daily_counts(all_records)
    total_days = len(daily_counts.get("days", {}))
    logger.info(f"  {total_days} days of data")

    # District counts
    logger.info("Building district_counts.json...")
    district_counts = _build_district_counts(all_records)
    logger.info("  District aggregation complete")

    # Resolution stats
    logger.info("Building resolution_stats.json...")
    resolution_stats = _build_resolution_stats(all_records)
    for cat, s in resolution_stats.get("90d", {}).items():
        if s["count"] > 0:
            logger.info(f"  {cat}: avg={s['avg_days']}d, median={s['median_days']}d (n={s['count']})")

    # Monthly aggregates
    logger.info("Building monthly_aggregates.json...")
    monthly_aggregates = _build_monthly_aggregates(all_records)
    logger.info(f"  {len(monthly_aggregates.get('months', {}))} months of data")

    # Service code index
    logger.info("Building service_codes.json...")
    service_codes = _build_service_code_index()
    logger.info(f"  {len(service_codes.get('codes', {}))} service codes indexed")

    # ── Phase 3: Write output ───────────────────────────────────────────
    logger.info("\n=== Writing output files ===")

    files = {
        "daily_counts.json": daily_counts,
        "district_counts.json": district_counts,
        "resolution_stats.json": resolution_stats,
        "monthly_aggregates.json": monthly_aggregates,
        "service_codes.json": service_codes,
    }

    for filename, data in files.items():
        filepath = OUTPUT_DIR / filename
        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)
        size_kb = filepath.stat().st_size / 1024
        logger.info(f"  {filename}: {size_kb:.1f} KB")

    logger.info(f"\nDone. Files written to {OUTPUT_DIR}/")


if __name__ == "__main__":
    main()
