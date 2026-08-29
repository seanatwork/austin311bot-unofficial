#!/usr/bin/env python3
"""
Generate docs/wrapped/data.json — per-ZIP "311 Wrapped" year-in-review data.

For each Austin ZIP (ZCTA5) this computes, over the last 365 days:
  - complaint volume + citywide rank/percentile
  - top issue categories (service-code level, with % share)
  - response-time stats (median/avg/p90/same-day %)
  - "what sets this ZIP apart" — location quotient vs citywide share
  - "lowlights" — slowest category, worst single day
  - activity facts — peak hour/day, weekend %, late-night count
  - a heuristic archetype + shareable punchline (no LLM)

Also emits a citywide block: totals, response-time leaderboard (fastest/slowest
service categories), worst day, and fastest/slowest ZIPs by median response.

Fetch strategy: read the Open311 SQLite cache (full 365-day window, all codes),
then gap-fill any code x month that is empty while the code has nonzero volume
in adjacent months (the documented cache mid-window-gap failure mode) with a
fresh month-by-month Open311 fetch. This keeps the run cheap and correct.

Run:  AUSTINAPIKEY=sk... python scripts/generate_wrapped_data.py
Output: docs/wrapped/data.json
"""
import json
import logging
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any, Dict, List, Optional, Tuple

# Add project root to path so we can import repo modules
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from categories import CATEGORY_CODES, CATEGORY_NAMES  # noqa: E402
from geolocation import zip_for_point, get_zip_label  # noqa: E402
from open311_cache import (  # noqa: E402
    init_cache,
    get_cached_records,
    cache_records,
    get_last_fetch_date,
)
from scripts.generate_query_data import (  # noqa: E402
    _fetch_code_month,
    SERVICE_CODE_NAMES,
)

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(message)s")

WINDOW_DAYS = 365
MONTHS_BACK = 13  # 12 full months + current partial
OUT_PATH = REPO_ROOT / "docs" / "wrapped" / "data.json"

# Coyotes are a notable Austin thing (fun page) — add to the code set.
EXTRA_CODES = ["ACCOYTE"]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_ts(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _parse_dt(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _all_codes() -> List[str]:
    codes: List[str] = []
    for cat_codes in CATEGORY_CODES.values():
        for c in cat_codes:
            if c not in codes:
                codes.append(c)
    for c in EXTRA_CODES:
        if c not in codes:
            codes.append(c)
    return codes


# ── Fetching ────────────────────────────────────────────────────────────────

def _month_windows(months_back: int = MONTHS_BACK) -> List[Tuple[str, datetime]]:
    """Return [(YYYY-MM, month_start_datetime)] for the last N months."""
    now = _utc_now()
    windows: List[Tuple[str, datetime]] = []
    current = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    for _ in range(months_back):
        windows.append((current.strftime("%Y-%m"), current))
        if current.month == 1:
            current = current.replace(year=current.year - 1, month=12)
        else:
            current = current.replace(month=current.month - 1)
    windows.reverse()
    return windows


def _fetch_wrapped_records(codes: List[str]) -> Dict[str, List[dict]]:
    """Fetch a year of Open311 records, cache-first with gap-fill.

    Strategy:
      * Cache is FRESH (< 7 days since last fetch — the CI case): read the
        cached 365-day window, then gap-fill any code x month that is empty
        while the code has nonzero volume elsewhere, plus the current month.
        This mitigates the documented cache mid-window-gap failure mode.
      * Cache is STALE (>= 7 days — e.g. a fresh local checkout): do a full
        authoritative month-by-month refetch for every code, mirroring the
        trends modules' use_cache=False pattern. Set WRAPPED_FORCE_CACHE=1 to
        skip this and use the cache only (for quick local logic checks).

    Returns {service_code: [records]}.
    """
    init_cache()
    since = _utc_now() - timedelta(days=WINDOW_DAYS)
    cached = get_cached_records(service_codes=codes, since=since)
    logger.info(f"Cache: {len(cached)} records in {WINDOW_DAYS}d window for {len(codes)} codes")

    by_code: Dict[str, List[dict]] = {c: [] for c in codes}
    seen: Dict[str, set] = {c: set() for c in codes}
    for r in cached:
        code = r.get("service_code")
        if code in by_code:
            sid = r.get("service_request_id")
            if sid and sid not in seen[code]:
                seen[code].add(sid)
                by_code[code].append(r)

    last_fetch = get_last_fetch_date(service_codes=codes)
    stale = last_fetch is None or (_utc_now() - last_fetch) > timedelta(days=7)
    force_cache = os.getenv("WRAPPED_FORCE_CACHE") == "1"
    full_refetch = stale and not force_cache
    logger.info(
        f"Last fetch: {last_fetch} | stale={stale} | force_cache={force_cache} "
        f"| full_refetch={full_refetch}"
    )

    windows = _month_windows()

    # Per-code monthly coverage from cache
    coverage: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for code, records in by_code.items():
        for r in records:
            dt = _parse_dt(r.get("requested_datetime"))
            if dt:
                coverage[code][dt.strftime("%Y-%m")] += 1

    current_month = _utc_now().strftime("%Y-%m")
    gap_filled = 0
    fetched_records: List[dict] = []

    def _ingest(code: str, records: List[dict], month: str) -> int:
        """Add newly-seen records for a code; returns how many were new."""
        nonlocal gap_filled
        new = 0
        for r in records:
            sid = r.get("service_request_id")
            if sid and sid not in seen[code]:
                seen[code].add(sid)
                by_code[code].append(r)
                fetched_records.append(r)
                new += 1
        gap_filled += new
        if new and month != current_month:
            time.sleep(0.5)
        return new

    for code in codes:
        if full_refetch:
            # Authoritative: fetch every month fresh
            for month, month_start in windows:
                try:
                    records = _fetch_code_month(code, month_start)
                except Exception as e:
                    logger.warning(f"  {code} {month} fetch failed: {e}")
                    continue
                _ingest(code, records, month)
        else:
            monthly = coverage[code]
            nonzero = [v for v in monthly.values() if v > 0]
            med = median(nonzero) if nonzero else 0
            for month, month_start in windows:
                count = monthly.get(month, 0)
                # Gap-fill empty months for a code that otherwise has volume,
                # and always refresh the current (partial) month. With
                # WRAPPED_FORCE_CACHE=1, no network at all (pure cache read).
                if not force_cache and (count == 0 and med > 0 or month == current_month):
                    try:
                        records = _fetch_code_month(code, month_start)
                    except Exception as e:
                        logger.warning(f"  {code} {month} fetch failed: {e}")
                        continue
                    _ingest(code, records, month)
        logger.info(f"  {code}: {len(by_code[code])} records")

    if fetched_records:
        # Backfill the cache with only what we newly fetched; cache_records
        # dedupes by service_request_id so this is safe.
        cache_records("wrapped", fetched_records)

    logger.info(f"Fetched {gap_filled} new records")
    return by_code


# ── Resolution helpers ──────────────────────────────────────────────────────

def _resolution_hours(r: dict, now: datetime) -> Optional[float]:
    """Return resolution time in hours for a closed record, else None."""
    if (r.get("status") or "").lower() != "closed":
        return None
    req_dt = _parse_dt(r.get("requested_datetime"))
    upd_dt = _parse_dt(r.get("updated_datetime"))
    if not req_dt or not upd_dt:
        return None
    hours = (upd_dt - req_dt).total_seconds() / 3600.0
    if hours < 0 or hours > 730 * 24:
        return None
    return hours


def _response_stats(hours_list: List[float]) -> Optional[Dict[str, Any]]:
    if not hours_list:
        return None
    days = [h / 24.0 for h in hours_list]
    days_sorted = sorted(days)
    n = len(days_sorted)
    p90 = days_sorted[min(int(n * 0.9), n - 1)]
    same_day = sum(1 for h in hours_list if h < 24) / n
    return {
        "count": n,
        "medianDays": round(median(days_sorted), 1),
        "avgDays": round(sum(days_sorted) / n, 1),
        "p90Days": round(p90, 1),
        "sameDayPct": round(same_day * 100),
    }


def _fmt_hour(h: int) -> str:
    if h == 0:
        return "12am"
    if h < 12:
        return f"{h}am"
    if h == 12:
        return "12pm"
    return f"{h - 12}pm"


# ── Main aggregation ────────────────────────────────────────────────────────

def _service_name(code: str, record: dict) -> str:
    name = SERVICE_CODE_NAMES.get(code)
    if name:
        return name
    raw = (record.get("service_name") or "").strip()
    return raw or code


def _build_data(by_code: Dict[str, List[dict]]) -> Dict[str, Any]:
    now = _utc_now()
    window_start = now - timedelta(days=WINDOW_DAYS)

    # ── Pass 1: collect per-record facts ────────────────────────────────────
    # name -> [zip] ; zip -> Counter(name) ; name -> Counter(day) ; etc.
    city_name_counts: Counter = Counter()
    zip_name_counts: Dict[str, Counter] = defaultdict(Counter)
    zip_resolution: Dict[str, List[float]] = defaultdict(list)
    zip_name_resolution: Dict[str, Dict[str, List[float]]] = defaultdict(lambda: defaultdict(list))
    name_resolution: Dict[str, List[float]] = defaultdict(list)
    zip_days: Dict[str, Counter] = defaultdict(Counter)  # zip -> date -> count
    zip_hours: Dict[str, Counter] = defaultdict(Counter)
    zip_weekdays: Dict[str, Counter] = defaultdict(Counter)
    zip_late_night: Counter = Counter()
    zip_total: Counter = Counter()

    total = 0
    mapped = 0

    for code, records in by_code.items():
        for r in records:
            req_dt = _parse_dt(r.get("requested_datetime"))
            if not req_dt or req_dt < window_start:
                continue
            total += 1

            name = _service_name(code, r)
            city_name_counts[name] += 1

            lat = r.get("lat")
            lon = r.get("long", r.get("lng"))
            zip_code = None
            if lat is not None and lon is not None:
                try:
                    zip_code = zip_for_point(float(lat), float(lon))
                except (TypeError, ValueError):
                    zip_code = None
            if not zip_code:
                continue  # outside Austin ZIP coverage — citywide only

            mapped += 1
            zip_total[zip_code] += 1
            zip_name_counts[zip_code][name] += 1
            zip_days[zip_code][req_dt.strftime("%Y-%m-%d")] += 1
            zip_hours[zip_code][req_dt.hour] += 1
            zip_weekdays[zip_code][req_dt.strftime("%a")] += 1
            if req_dt.hour >= 22 or req_dt.hour <= 3:
                zip_late_night[zip_code] += 1

            hours = _resolution_hours(r, now)
            if hours is not None:
                zip_resolution[zip_code].append(hours)
                zip_name_resolution[zip_code][name].append(hours)
                name_resolution[name].append(hours)

    city_total = sum(city_name_counts.values()) or 1

    # ── Citywide ────────────────────────────────────────────────────────────
    city_top = city_name_counts.most_common(8)
    city_top_issues = [
        {"name": n, "count": c, "share": round(c / city_total * 100, 1)}
        for n, c in city_top
    ]

    all_res = [h for lst in name_resolution.values() for h in lst]
    city_response = _response_stats(all_res)

    # Response-time leaderboard (fastest / slowest service categories)
    lb = []
    for name, hrs in name_resolution.items():
        if len(hrs) < 5:
            continue
        st = _response_stats(hrs)
        if st:
            lb.append({"name": name, **st})
    lb.sort(key=lambda x: x["medianDays"])
    fastest = lb[:5]
    slowest = list(reversed(lb[-5:]))

    # Worst citywide day
    city_days: Counter = Counter()
    for zc in zip_days.values():
        city_days.update(zc)
    city_worst_day = None
    if city_days:
        date, count = city_days.most_common(1)[0]
        daily_avg = total / WINDOW_DAYS
        city_worst_day = {
            "date": date,
            "count": count,
            "dailyAvg": round(daily_avg, 1),
            "xDaily": round(count / daily_avg, 1),
        }

    # Fastest / slowest ZIPs by median response
    zip_res_stats: Dict[str, Dict[str, Any]] = {}
    for zc, hrs in zip_resolution.items():
        if len(hrs) >= 10:
            st = _response_stats(hrs)
            if st:
                zip_res_stats[zc] = st
    zip_medians = [(zc, st["medianDays"]) for zc, st in zip_res_stats.items()]
    zip_medians.sort(key=lambda x: x[1])
    citywide_zips = None
    if zip_medians:
        citywide_zips = {
            "fastest": {"zip": zip_medians[0][0], "medianDays": zip_medians[0][1]},
            "slowest": {"zip": zip_medians[-1][0], "medianDays": zip_medians[-1][1]},
        }

    # ── Per-ZIP ─────────────────────────────────────────────────────────────
    ranked = sorted(zip_total.items(), key=lambda kv: kv[1], reverse=True)
    rank_of = {zc: i + 1 for i, (zc, _) in enumerate(ranked)}
    zip_count = len(ranked)

    zips: Dict[str, Any] = {}
    for zc, vol in ranked:
        name_counts = zip_name_counts[zc]
        z_total = vol
        z_top = name_counts.most_common(3)
        top_issues = [
            {"name": n, "count": c, "share": round(c / z_total * 100, 1)}
            for n, c in z_top
        ]

        rank = rank_of[zc]
        percentile = round(rank / zip_count, 2)
        if percentile <= 0.10:
            top_label = "Top 10%"
        elif percentile <= 0.30:
            top_label = "Top 30%"
        elif percentile <= 0.70:
            top_label = "Middle"
        else:
            top_label = "Bottom"

        resp = zip_res_stats.get(zc) or _response_stats(zip_resolution[zc])

        # Lowlight: slowest category in this ZIP (median days, n>=3)
        lowlight = None
        slow_items = []
        for name, hrs in zip_name_resolution[zc].items():
            if len(hrs) >= 3:
                st = _response_stats(hrs)
                if st:
                    slow_items.append({"name": name, **st})
        if slow_items:
            slow_items.sort(key=lambda x: x["medianDays"], reverse=True)
            lowlight = {"name": slow_items[0]["name"], "medianDays": slow_items[0]["medianDays"]}

        # Worst day for this ZIP
        worst_day = None
        if zip_days[zc]:
            date, count = zip_days[zc].most_common(1)[0]
            daily_avg = vol / WINDOW_DAYS
            worst_day = {
                "date": date,
                "count": count,
                "dailyAvg": round(daily_avg, 1),
                "xDaily": round(count / daily_avg, 1),
            }

        # What sets this ZIP apart — location quotient
        sets_apart = []
        for name, count in name_counts.items():
            if count < 5:
                continue
            city_share = city_name_counts[name] / city_total
            if city_share < 0.002 or city_name_counts[name] < 20:
                continue
            zip_share = count / z_total
            quotient = zip_share / city_share
            if quotient >= 1.5:
                sets_apart.append({
                    "name": name,
                    "quotient": round(quotient, 1),
                    "shareZip": round(zip_share * 100, 1),
                    "shareCity": round(city_share * 100, 1),
                    "count": count,
                })
        sets_apart.sort(key=lambda x: x["quotient"], reverse=True)
        sets_apart = sets_apart[:4]

        # Activity
        peak_hour = zip_hours[zc].most_common(1)[0][0] if zip_hours[zc] else 0
        peak_day = zip_weekdays[zc].most_common(1)[0][0] if zip_weekdays[zc] else ""
        weekend = sum(v for d, v in zip_weekdays[zc].items() if d in ("Sat", "Sun"))
        weekend_pct = round(weekend / z_total * 100) if z_total else 0

        archetype, punchline = _archetype(top_label, top_issues, percentile)
        area = get_zip_label(zc)

        share_text = (
            f"{area} ({zc}): {share_pct(top_issues)}% of 311 complaints were about "
            f"{top_issues[0]['name'] if top_issues else '…'} — {punchline} "
            f"Ranked #{rank} of {zip_count} Austin areas by volume. austin311.com/wrapped/"
        )

        zips[zc] = {
            "area": area,
            "volume": vol,
            "rank": rank,
            "zipCount": zip_count,
            "percentile": percentile,
            "topLabel": top_label,
            "topIssues": top_issues,
            "response": resp,
            "lowlight": lowlight,
            "worstDay": worst_day,
            "setsApart": sets_apart,
            "activity": {
                "peakHour": peak_hour,
                "peakHourLabel": _fmt_hour(peak_hour),
                "peakDay": peak_day,
                "weekendPct": weekend_pct,
                "lateNightCount": zip_late_night[zc],
            },
            "archetype": archetype,
            "punchline": punchline,
            "shareText": share_text,
        }

    data = {
        "generated": _iso_ts(now),
        "windowDays": WINDOW_DAYS,
        "totalRecords": total,
        "mappedToZip": mapped,
        "zipCount": zip_count,
        "citywide": {
            "total": total,
            "topIssues": city_top_issues,
            "response": city_response,
            "leaderboard": {"fastest": fastest, "slowest": slowest},
            "worstDay": city_worst_day,
            "zips": citywide_zips,
        },
        "zips": zips,
    }
    return data


def share_pct(top_issues: List[dict]) -> int:
    return int(round(top_issues[0]["share"])) if top_issues else 0


def _archetype(top_label: str, top_issues: List[dict], percentile: float) -> Tuple[str, str]:
    if percentile <= 0.10:
        archetype = "OVERACHIEVER"
    elif percentile <= 0.30:
        archetype = "KEEN EYE"
    elif percentile <= 0.70:
        archetype = "SELECTIVE REPORTER"
    elif percentile <= 0.90:
        archetype = "OBSERVER"
    else:
        archetype = "CHILL"

    top_name = (top_issues[0]["name"] if top_issues else "").lower()
    if any(k in top_name for k in ("parking", "violation", "boot")):
        punchline = "finding parking is a blood sport"
    elif any(k in top_name for k in ("music", "noise", "alarm", "firework")):
        punchline = "the party never really stops"
    elif "graffiti" in top_name:
        punchline = "every wall is a canvas"
    elif any(k in top_name for k in ("dead animal", "coyote", "loose")):
        punchline = "the wildlife is undefeated"
    elif any(k in top_name for k in ("trash", "debris", "sweep", "litter")):
        punchline = "the curbs are a daily negotiation"
    elif any(k in top_name for k in ("signal", "sign", "street light", "pothole")):
        punchline = "the streets are a choose-your-own-adventure"
    else:
        punchline = "the city's small print gets written daily"
    return archetype, punchline


def main() -> None:
    codes = _all_codes()
    logger.info(f"Fetching {len(codes)} service codes for {WINDOW_DAYS}-day window…")
    by_code = _fetch_wrapped_records(codes)
    data = _build_data(by_code)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(data, indent=1), encoding="utf-8")
    logger.info(f"Wrote {OUT_PATH.stat().st_size:,} bytes to {OUT_PATH}")
    logger.info(f"ZIPs: {data['zipCount']} | citywide total: {data['citywide']['total']} | "
                f"mapped: {data['mappedToZip']} ({data['mappedToZip'] / max(data['totalRecords'], 1) * 100:.0f}%)")


if __name__ == "__main__":
    main()
