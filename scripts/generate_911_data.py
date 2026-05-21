#!/usr/bin/env python3
"""
Fetch and aggregate Austin 911 dispatch data for the response-time dashboard.

Pulls ALL historical records from Socrata dataset 22de-7rzg, computes
per-record response times, aggregates into monthly × priority × district
buckets, and writes docs/911/data.json.

Run quarterly via GitHub Actions, or manually:
    python scripts/generate_911_data.py
"""

import json
import os
import statistics
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests

DATASET_ID = "22de-7rzg"
BASE_URL = f"https://data.austintexas.gov/resource/{DATASET_ID}.json"
COUNT_URL = f"https://data.austintexas.gov/api/id/{DATASET_ID}.json?$select=COUNT(*)"
OUT = Path("docs/911/data.json")

FIELDS = ",".join([
    "response_datetime",
    "response_time",
    "response_day_of_week",
    "priority_level",
    "council_district",
    "sector",
    "mental_health_flag",
    "final_problem_category",
])

CHUNK_SIZE = 5000
MAX_RETRIES = 8
RETRYABLE_CODES = {423, 429, 500, 502, 503, 504}
INTER_CHUNK_DELAY = 1.0

# Distribution bins match the dashboard's current histogram buckets (minutes)
DOW_ORDER = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

DIST_BINS = [0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 25, 30, 40, 60, 120]
DIST_LABELS = [
    "0–2", "2–4", "4–6", "6–8", "8–10", "10–12", "12–14", "14–16",
    "16–18", "18–20", "20–25", "25–30", "30–40", "40–60", "60–120", "120+",
]

_session: requests.Session | None = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({
            "Accept": "application/json",
            "User-Agent": "austin311bot/911-cache",
        })
        token = os.getenv("AUSTINAPIKEY")
        if token:
            _session.headers["X-App-Token"] = token
    return _session


def _get(url: str, params: dict | None = None, retries: int = 0):
    """GET with retry/backoff matching open311_client.py.

    429/423: exponential backoff starting at 15s, respects Retry-After header.
    5xx/network: exponential backoff starting at 2s.
    Up to MAX_RETRIES attempts before re-raising.
    """
    session = _get_session()
    try:
        resp = session.get(url, params=params, timeout=60)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code
        if status in RETRYABLE_CODES and retries < MAX_RETRIES:
            retry_after = e.response.headers.get("Retry-After")
            if retry_after:
                try:
                    delay = float(retry_after)
                except ValueError:
                    delay = 15.0 * (2 ** retries)
            elif status in {423, 429}:
                delay = 15.0 * (2 ** retries)
            else:
                delay = 2.0 * (2 ** retries)
            print(f"  HTTP {status}, retrying in {delay:.1f}s ({retries + 1}/{MAX_RETRIES})...", flush=True)
            time.sleep(delay)
            return _get(url, params, retries + 1)
        raise
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
        if retries < MAX_RETRIES:
            delay = 2.0 * (2 ** retries)
            print(f"  Connection error ({e}), retrying in {delay:.1f}s ({retries + 1}/{MAX_RETRIES})...", flush=True)
            time.sleep(delay)
            return _get(url, params, retries + 1)
        raise


def fetch_total_count() -> int:
    result = _get(COUNT_URL)
    return int(result[0].get("COUNT", 0))


def fetch_all_records() -> list[dict]:
    total = fetch_total_count()
    print(f"Total records in dataset: {total:,}", flush=True)

    all_rows: list[dict] = []
    offset = 0

    while True:
        print(f"  Fetching {offset:,}–{offset + CHUNK_SIZE:,}...", end=" ", flush=True)
        rows = _get(BASE_URL, {
            "$select": FIELDS,
            "$order": "response_datetime ASC",
            "$limit": CHUNK_SIZE,
            "$offset": offset,
        })

        if not rows:
            print("done (empty response)", flush=True)
            break

        all_rows.extend(rows)
        print(f"→ {len(all_rows):,} loaded", flush=True)

        if len(rows) < CHUNK_SIZE:
            break

        offset += CHUNK_SIZE
        time.sleep(INTER_CHUNK_DELAY)

    return all_rows


def _parse_dt(s: str) -> datetime | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:26], fmt)
        except ValueError:
            continue
    return None


def _response_minutes(row: dict) -> float | None:
    rt = row.get("response_time")
    if not rt:
        return None
    try:
        minutes = float(rt) / 60.0
    except (ValueError, TypeError):
        return None
    if minutes <= 0 or minutes > 1440:
        return None
    return minutes


def _dist_bucket(minutes: float) -> int:
    for i, threshold in enumerate(DIST_BINS[1:]):
        if minutes < threshold:
            return i
    return len(DIST_BINS) - 1  # "120+" bucket


def aggregate(rows: list[dict]) -> dict:
    # monthly_data key: (month_str, priority, district)
    # value: {tc: total calls, n: calls with response time, s: sum minutes, mh: MH calls}
    monthly: dict[tuple, dict] = defaultdict(lambda: {"tc": 0, "n": 0, "s": 0.0, "mh": 0})

    by_hour: dict[int, dict] = defaultdict(lambda: {"n": 0, "s": 0.0})
    by_dow: dict[str, dict] = defaultdict(lambda: {"n": 0, "s": 0.0})
    by_sector: dict[str, dict] = defaultdict(lambda: {"n": 0, "s": 0.0})

    # distribution per priority and overall
    dist: dict[str, list] = defaultdict(lambda: [0] * len(DIST_LABELS))
    dist_all: list[int] = [0] * len(DIST_LABELS)

    inc_types: dict[str, dict] = defaultdict(lambda: {"n": 0, "s": 0.0})

    # raw lists for percentile computation (sampled to bound memory)
    all_minutes: list[float] = []
    per_priority_minutes: dict[str, list[float]] = defaultdict(list)

    earliest: datetime | None = None
    latest: datetime | None = None

    for row in rows:
        dispatched = _parse_dt(row.get("response_datetime", ""))
        if not dispatched:
            continue

        month = dispatched.strftime("%Y-%m")
        hour = dispatched.hour
        dow = (row.get("response_day_of_week") or "").strip()[:3]
        priority = (row.get("priority_level") or "Unknown").strip()
        district = (row.get("council_district") or "Unknown").strip()
        sector = (row.get("sector") or "Unknown").strip()
        is_mh = (row.get("mental_health_flag") or "").strip() == "Mental Health Incident"

        inc_type = (row.get("final_problem_category") or "Unknown").strip()

        if earliest is None or dispatched < earliest:
            earliest = dispatched
        if latest is None or dispatched > latest:
            latest = dispatched

        key = (month, priority, district)
        monthly[key]["tc"] += 1
        if is_mh:
            monthly[key]["mh"] += 1

        by_hour[hour]["n"] += 1
        if dow:
            by_dow[dow]["n"] += 1
        if sector != "Unknown":
            by_sector[sector]["n"] += 1
        inc_types[inc_type]["n"] += 1

        minutes = _response_minutes(row)
        if minutes is not None:
            monthly[key]["n"] += 1
            monthly[key]["s"] += minutes
            by_hour[hour]["s"] += minutes
            if dow:
                by_dow[dow]["s"] += minutes
            if sector != "Unknown":
                by_sector[sector]["s"] += minutes
            inc_types[inc_type]["s"] += minutes
            dist[priority][_dist_bucket(minutes)] += 1
            dist_all[_dist_bucket(minutes)] += 1
            all_minutes.append(minutes)
            per_priority_minutes[priority].append(minutes)

    # Build monthly_data list (compact keys to reduce JSON size)
    monthly_data = []
    for (month, priority, district), d in sorted(monthly.items()):
        entry: dict = {"m": month, "p": priority, "d": district, "tc": d["tc"]}
        if d["n"] > 0:
            entry["n"] = d["n"]
            entry["s"] = round(d["s"], 2)
        if d["mh"] > 0:
            entry["mh"] = d["mh"]
        monthly_data.append(entry)

    # by_hour (all 24 hours)
    hour_data = []
    for h in range(24):
        d = by_hour.get(h, {"n": 0, "s": 0.0})
        hour_data.append({"h": h, "n": d["n"], "s": round(d["s"], 2)})

    # by_dow (Mon–Sun order)
    dow_data = [
        {"dow": d, "n": by_dow[d]["n"], "s": round(by_dow[d]["s"], 2)}
        for d in DOW_ORDER if d in by_dow
    ]

    # by_sector (APD patrol sectors, sorted alphabetically)
    sector_data = sorted(
        [{"sector": s, "n": d["n"], "s": round(d["s"], 2)} for s, d in by_sector.items()],
        key=lambda x: x["sector"],
    )

    # distribution per priority + overall
    dist_out: dict[str, list] = {
        "all": [{"label": DIST_LABELS[i], "n": dist_all[i]} for i in range(len(DIST_LABELS))]
    }
    for p, buckets in sorted(dist.items()):
        dist_out[p] = [{"label": DIST_LABELS[i], "n": buckets[i]} for i in range(len(DIST_LABELS))]

    # Top 15 incident types by call count
    top_types = sorted(
        [{"type": t, "n": d["n"], "s": round(d["s"], 2)} for t, d in inc_types.items()],
        key=lambda x: x["n"],
        reverse=True,
    )[:15]

    # Overall percentile stats
    all_minutes_sorted = sorted(all_minutes)
    n_total = len(all_minutes_sorted)
    overall_avg = round(sum(all_minutes_sorted) / n_total, 2) if n_total else None
    overall_median = round(statistics.median(all_minutes_sorted), 2) if n_total else None
    overall_p95 = round(all_minutes_sorted[int(n_total * 0.95)], 2) if n_total else None

    # Per-priority percentile stats
    priority_stats: dict[str, dict] = {}
    for p, mins in sorted(per_priority_minutes.items()):
        mins_sorted = sorted(mins)
        np_ = len(mins_sorted)
        priority_stats[p] = {
            "avg": round(sum(mins_sorted) / np_, 2) if np_ else None,
            "median": round(statistics.median(mins_sorted), 2) if np_ else None,
            "p95": round(mins_sorted[int(np_ * 0.95)], 2) if np_ else None,
            "n": np_,
        }

    return {
        "meta": {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "total_records": len(rows),
            "total_with_response_time": n_total,
            "date_range": {
                "earliest": earliest.strftime("%Y-%m") if earliest else None,
                "latest": latest.strftime("%Y-%m") if latest else None,
            },
            "overall_avg_minutes": overall_avg,
            "overall_median_minutes": overall_median,
            "overall_p95_minutes": overall_p95,
            "priority_stats": priority_stats,
        },
        "monthly_data": monthly_data,
        "by_hour": hour_data,
        "by_dow": dow_data,
        "by_sector": sector_data,
        "distribution": dist_out,
        "top_incident_types": top_types,
    }


def main() -> None:
    print("Fetching Austin 911 dispatch data (full history)...", flush=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)

    rows = fetch_all_records()
    print(f"\nFetched {len(rows):,} total records. Aggregating...", flush=True)

    data = aggregate(rows)
    OUT.write_text(json.dumps(data, separators=(",", ":")))

    meta = data["meta"]
    size_kb = OUT.stat().st_size // 1024
    print(f"Wrote {OUT} ({size_kb} KB)", flush=True)
    print(f"Date range: {meta['date_range']['earliest']} → {meta['date_range']['latest']}", flush=True)
    print(
        f"Records: {meta['total_records']:,} total, "
        f"{meta['total_with_response_time']:,} with response time",
        flush=True,
    )
    print(
        f"Overall avg: {meta['overall_avg_minutes']} min | "
        f"median: {meta['overall_median_minutes']} min | "
        f"p95: {meta['overall_p95_minutes']} min",
        flush=True,
    )


if __name__ == "__main__":
    main()
