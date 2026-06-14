#!/usr/bin/env python3
"""
Generate static JSON snapshots for the fire section of the site.

The fire pages fetch live data directly from Socrata (wpu4-x69d and v5hh-nyr8)
in the browser. This works in normal operation, but when the Socrata platform
goes down (Tyler Tech outages), the entire fire section breaks because no
endpoints respond.

This script pre-fetches the same data the JS needs and writes it to
`docs/fire/data.json` (used as a fallback by the live map) and
`docs/fire/trends/data.json` (pre-aggregated stats for the trends page).
The browser code tries the live Socrata feed first, then falls back to
these snapshots if Socrata is unreachable.

Outage guard:
    If Socrata is completely unreachable (platform-wide 503 or DNS/network
    error), the script exits with code 2 and DOES NOT overwrite the
    existing snapshot files. This way the GitHub Action can be configured
    to only commit when the script exits cleanly.

Run locally:
    python scripts/generate_fire_data.py

Run in CI (GitHub Actions) with AUSTINAPIKEY set for higher rate limits.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

# Socrata endpoints
LIVE_DATASET = "wpu4-x69d"   # Real-Time Fire Incidents (CTECC)
HIST_DATASET = "v5hh-nyr8"   # AFD Fire Incidents 2023–2025
SOCRATA_BASE = "https://data.austintexas.gov/resource"

# Output files
LIVE_OUT = Path("docs/fire/data.json")
TRENDS_OUT = Path("docs/fire/trends/data.json")

# How many days of live incidents to snapshot
LIVE_DAYS = 90

# Canonical AFD fire-type codes (matches docs/fire/trends/index.html)
FIRE_CODES = frozenset([
    "TRASH", "GRASS", "AUTO", "ELEC", "BOX", "BOXS", "DUMP", "BOXL",
    "BBQ", "BRSHL", "BOXMID", "BRUSH", "BOXHI", "PKG", "FIRE", "BOXMAR",
])

# Months for trends labels
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# Status check fields we want to keep from the live feed
LIVE_FIELDS = ",".join([
    "traffic_report_id", "issue_reported", "address",
    "latitude", "longitude", "published_date", "agency",
    "traffic_report_status",
])

# History (AFD 2023-2025) fields
HIST_FIELDS = ",".join([
    "incdate", "problem", "prioritydescription", "council_district",
])

MAX_RETRIES = 3
RETRYABLE_CODES = {423, 429, 500, 502, 503, 504}


def _get_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "Accept": "application/json",
        "User-Agent": "austin311bot/fire-cache",
    })
    token = os.getenv("AUSTINAPIKEY")
    if token:
        session.headers["X-App-Token"] = token
    return session


def _socrata_get(session: requests.Session, url: str, params: dict,
                 retries: int = 0) -> list:
    """GET a Socrata resource. Returns [] on outage instead of raising,
    so callers can decide what to do. Raises on partial success or
    non-retryable HTTP errors.
    """
    try:
        resp = session.get(url, params=params, timeout=60)
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
        # Outage signal — let the caller treat this as a platform-down event.
        raise SocrataOutage(f"connection error: {e}") from e

    if resp.status_code == 503:
        # Tyler Tech "Site Currently Unavailable" HTML page — definitely platform-wide.
        raise SocrataOutage("Socrata returned 503 (platform unavailable)")

    if resp.status_code in RETRYABLE_CODES and retries < MAX_RETRIES:
        delay = 2.0 * (2 ** retries)
        print(f"  HTTP {resp.status_code}, retrying in {delay:.1f}s "
              f"({retries + 1}/{MAX_RETRIES})...", flush=True)
        time.sleep(delay)
        return _socrata_get(session, url, params, retries + 1)

    resp.raise_for_status()
    return resp.json()


class SocrataOutage(Exception):
    """Raised when the Socrata platform itself is unreachable (not a per-query error)."""


def _detect_outage() -> bool:
    """Quick probe of the Socrata platform. Returns True if the platform is down.

    The first request to a Socrata dataset during an outage is the most
    expensive (it can take ~60s to time out and return 503). We probe
    `data.austintexas.gov/` directly with a short timeout to detect
    outages cheaply.
    """
    try:
        resp = requests.get(
            "https://data.austintexas.gov/",
            timeout=10,
            allow_redirects=False,
        )
        if resp.status_code == 503:
            print("  ⚠ Socrata platform returned 503 on root probe.", flush=True)
            return True
        # Some 5xx errors indicate the platform is also down.
        if resp.status_code >= 500:
            print(f"  ⚠ Socrata platform returned {resp.status_code} on root probe.",
                  flush=True)
            return True
        return False
    except requests.exceptions.RequestException as e:
        print(f"  ⚠ Socrata platform probe failed: {e}", flush=True)
        return True


def fetch_live_records(session: requests.Session) -> list[dict]:
    """Fetch the last LIVE_DAYS days of real-time fire incidents."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=LIVE_DAYS)
    cutoff_str = cutoff.strftime("%Y-%m-%dT00:00:00")

    url = f"{SOCRATA_BASE}/{LIVE_DATASET}.json"
    params = {
        "$where": f"published_date >= '{cutoff_str}'",
        "$order": "published_date DESC",
        "$limit": 10000,
        "$select": LIVE_FIELDS,
    }
    print(f"  Fetching live incidents (last {LIVE_DAYS} days)...", flush=True)
    rows = _socrata_get(session, url, params)
    print(f"  ✓ {len(rows):,} live records", flush=True)
    return rows


def fetch_history_aggregates(session: requests.Session) -> dict:
    """Fetch pre-aggregated stats from the AFD 2023-2025 historical dataset.

    Returns a dict with:
      - monthly: list of {ym, n} sorted by ym
      - by_type: list of {problem, n}
      - by_district: list of {council_district, n}
      - by_priority: list of {prioritydescription, n}
    """
    base = f"{SOCRATA_BASE}/{HIST_DATASET}.json"

    print("  Fetching historical AFD monthly counts...", flush=True)
    monthly = _socrata_get(session, base, {
        "$select": "date_trunc_ym(incdate) as ym, count(*) as n",
        "$group": "ym",
        "$order": "ym",
        "$limit": 1000,
    })
    print(f"    ✓ {len(monthly):,} monthly buckets", flush=True)

    print("  Fetching historical AFD type breakdown...", flush=True)
    by_type = _socrata_get(session, base, {
        "$select": "problem, count(*) as n",
        "$group": "problem",
        "$order": "n DESC",
        "$limit": 30,
    })
    print(f"    ✓ {len(by_type):,} types", flush=True)

    print("  Fetching historical AFD council-district breakdown...", flush=True)
    by_district = _socrata_get(session, base, {
        "$select": "council_district, count(*) as n",
        "$group": "council_district",
        "$order": "n DESC",
        "$limit": 30,
    })
    print(f"    ✓ {len(by_district):,} districts", flush=True)

    print("  Fetching historical AFD priority breakdown...", flush=True)
    by_priority = _socrata_get(session, base, {
        "$select": "prioritydescription, count(*) as n",
        "$group": "prioritydescription",
        "$order": "n DESC",
        "$limit": 20,
    })
    print(f"    ✓ {len(by_priority):,} priority levels", flush=True)

    return {
        "monthly": monthly,
        "by_type": by_type,
        "by_district": by_district,
        "by_priority": by_priority,
    }


def fetch_live_monthly_aggregates(session: requests.Session) -> list[dict]:
    """Fetch 2026+ monthly aggregates from the live feed (fire calls only)."""
    url = f"{SOCRATA_BASE}/{LIVE_DATASET}.json"
    params = {
        "$where": "published_date >= '2026-01-01'",
        "$select": ("date_trunc_ym(to_floating_timestamp(published_date, "
                    "'America/Chicago')) as ym, issue_reported, count(*) as n"),
        "$group": "ym,issue_reported",
        "$limit": 10000,
    }
    print("  Fetching live 2026+ monthly fire-call aggregates...", flush=True)
    rows = _socrata_get(session, url, params)
    # Filter down to fire-codes only on the server side so the JS stays small.
    filtered = []
    for r in rows:
        code_match = (r.get("issue_reported") or "").split(" ", 1)[0]
        if code_match in FIRE_CODES:
            filtered.append(r)
    print(f"  ✓ {len(filtered):,} live monthly fire buckets (of {len(rows):,} total)",
          flush=True)
    return filtered


def parse_ym(s: str) -> tuple[int, int]:
    return int(s[:4]), int(s[5:7]) - 1


def build_live_snapshot(records: list[dict]) -> dict:
    return {
        "meta": {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": f"https://data.austintexas.gov/resource/{LIVE_DATASET}",
            "days": LIVE_DAYS,
            "record_count": len(records),
        },
        "records": records,
    }


def build_trends_snapshot(historical: dict, live_monthly: list[dict]) -> dict:
    """Stitch historical AFD + live 2026+ into a single trends payload the
    trends page can render without any further Socrata calls.
    """
    # ── monthly: combine AFD (≤2025) and live (≥2026) ──
    monthly = {}  # y*12+m -> {y, m, afd, live}
    for r in historical["monthly"]:
        try:
            y, m = parse_ym(r["ym"])
        except (TypeError, ValueError):
            continue
        if y > 2025:
            continue
        key = y * 12 + m
        monthly.setdefault(key, {"y": y, "m": m, "afd": 0, "live": 0})["afd"] += int(r.get("n") or 0)
    for r in live_monthly:
        try:
            y, m = parse_ym(r["ym"])
        except (TypeError, ValueError):
            continue
        if y < 2026:
            continue
        key = y * 12 + m
        monthly.setdefault(key, {"y": y, "m": m, "afd": 0, "live": 0})["live"] += int(r.get("n") or 0)

    keys = sorted(monthly.keys())
    monthly_out = [
        {
            "y": monthly[k]["y"],
            "m": monthly[k]["m"],
            "ym": f"{monthly[k]['y']:04d}-{monthly[k]['m'] + 1:02d}",
            "label": f"{MONTHS[monthly[k]['m']]} {monthly[k]['y']}",
            "afd": monthly[k]["afd"],
            "live": monthly[k]["live"],
            "total": monthly[k]["afd"] + monthly[k]["live"],
        }
        for k in keys
    ]

    # ── by year (for seasonal chart) ──
    by_year = {}
    for entry in monthly_out:
        by_year.setdefault(entry["y"], [None] * 12)
        by_year[entry["y"]][entry["m"]] = entry["total"]

    # ── type / district / priority (2023-2025 official) ──
    by_type = [
        {"problem": r.get("problem"), "n": int(r.get("n") or 0)}
        for r in historical["by_type"]
    ]
    by_district = [
        {"council_district": r.get("council_district"), "n": int(r.get("n") or 0)}
        for r in historical["by_district"]
    ]
    by_priority = [
        {"prioritydescription": r.get("prioritydescription"),
         "n": int(r.get("n") or 0)}
        for r in historical["by_priority"]
    ]

    return {
        "meta": {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "sources": [
                f"https://data.austintexas.gov/resource/{HIST_DATASET}",
                f"https://data.austintexas.gov/resource/{LIVE_DATASET}",
            ],
        },
        "monthly": monthly_out,
        "by_year": by_year,
        "by_type": by_type,
        "by_district": by_district,
        "by_priority": by_priority,
        "fire_codes": sorted(FIRE_CODES),
    }


def _write_if_changed(path: Path, payload: dict) -> bool:
    """Write JSON to disk only if the content actually changed. Returns
    True if a write happened.
    """
    new_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if path.exists() and path.read_bytes() == new_bytes:
        print(f"  ✓ {path} unchanged ({len(new_bytes):,} bytes)", flush=True)
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(new_bytes)
    print(f"  ✓ Wrote {path} ({len(new_bytes):,} bytes)", flush=True)
    return True


def main() -> int:
    now = datetime.now(timezone.utc)
    print(f"=== Fire data snapshot — {now.isoformat()} ===", flush=True)

    # Cheap outage probe first — saves us paying 60s timeouts per endpoint.
    if _detect_outage():
        print("  ✗ Socrata platform is down — keeping existing snapshots.", flush=True)
        return 2

    session = _get_session()

    # ── Live feed snapshot ──
    try:
        live_records = fetch_live_records(session)
    except SocrataOutage as e:
        print(f"  ✗ Live feed fetch failed ({e}) — keeping existing snapshot.",
              flush=True)
        return 2
    except Exception as e:
        print(f"  ✗ Live feed fetch error: {e}", flush=True)
        return 2

    live_payload = build_live_snapshot(live_records)
    live_written = _write_if_changed(LIVE_OUT, live_payload)

    # ── Trends aggregates (historical + live 2026+) ──
    try:
        historical = fetch_history_aggregates(session)
        live_monthly = fetch_live_monthly_aggregates(session)
    except SocrataOutage as e:
        print(f"  ✗ Trends fetch failed ({e}) — keeping existing snapshot.",
              flush=True)
        return 2
    except Exception as e:
        print(f"  ✗ Trends fetch error: {e}", flush=True)
        return 2

    trends_payload = build_trends_snapshot(historical, live_monthly)
    trends_written = _write_if_changed(TRENDS_OUT, trends_payload)

    print()
    print(f"  Live snapshot:    {'updated' if live_written else 'unchanged'}", flush=True)
    print(f"  Trends snapshot:  {'updated' if trends_written else 'unchanged'}", flush=True)
    print("✓ Fire data snapshots complete.", flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
