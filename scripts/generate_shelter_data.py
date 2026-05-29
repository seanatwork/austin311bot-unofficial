#!/usr/bin/env python3
"""
Pre-fetch Austin Animal Center shelter data and write docs/shelter/data.json.

Queries Socrata datasets:
  wter-evkm  — Animal Center Intakes (2013–present, ~174K records)
  9t4d-g238  — Animal Center Outcomes (2013–present, ~174K records)

Aggregations are done server-side with SoQL $select+$group to avoid
pulling 174K raw rows.  The frontend loads this JSON on startup for
instant charts, then makes small live queries for Lost & Found search
and Adoptable Pets.

Run:  python scripts/generate_shelter_data.py
Output: docs/shelter/data.json
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE = "https://data.austintexas.gov/resource"
OUT  = Path("docs/shelter/data.json")

INTAKES_DATASET  = "wter-evkm"
OUTCOMES_DATASET = "9t4d-g238"

SESSION = requests.Session()
SESSION.headers.update({
    "Accept": "application/json",
    "User-Agent": "austin311bot/shelter-cache",
})

MAX_RETRIES = 3
RETRY_DELAY = 5.0


def get(dataset_id: str, params: dict, retries: int = 0) -> list:
    """Fetch from a Socrata dataset with retry+backoff."""
    url = f"{BASE}/{dataset_id}.json"
    app_token = os.getenv("AUSTINAPIKEY")
    if app_token:
        params = params.copy()
        params["$$app_token"] = app_token

    try:
        print(f"  GET {dataset_id} (attempt={retries+1})...")
        resp = SESSION.get(url, params=params, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            return data
        print(f"    Unexpected response type: {type(data)}")
        return []
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
        if retries < MAX_RETRIES:
            print(f"    Timeout: {e}. Retrying in {RETRY_DELAY}s...")
            time.sleep(RETRY_DELAY)
            return get(dataset_id, params, retries + 1)
        raise
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code >= 500 and retries < MAX_RETRIES:
            print(f"    Server error {e.response.status_code}. Retrying in {RETRY_DELAY}s...")
            time.sleep(RETRY_DELAY)
            return get(dataset_id, params, retries + 1)
        raise


def fetch_monthly_intakes() -> list:
    """Monthly intake counts by animal_type (Dog/Cat/Other/Bird/Livestock).
    datetime is calendar_date; date_trunc_ym returns 'YYYY-MM-01T00:00:00.000'.
    We clean the month prefix in post-processing.
    """
    raw = get(INTAKES_DATASET, {
        "$select": "date_trunc_ym(datetime) as month, animal_type, count(*) as cnt",
        "$group": "month, animal_type",
        "$order": "month",
        "$limit": 5000,
    })
    # Clean month: "2013-10-01T00:00:00.000" → "2013-10"
    for row in raw:
        m = row.get("month", "")
        if len(m) >= 7:
            row["month"] = m[:7]
    return raw


def fetch_monthly_outcomes() -> list:
    """Monthly outcome counts by outcome_type (Adoption/Transfer/RTO/Euthanasia/...).
    datetime is stored as text (ISO8601 with timezone), so use substring.
    """
    return get(OUTCOMES_DATASET, {
        "$select": "substring(datetime, 1, 7) as month, outcome_type, count(*) as cnt",
        "$group": "month, outcome_type",
        "$order": "month",
        "$limit": 5000,
    })


def fetch_top_breeds(limit: int = 25) -> list:
    """Top breeds by total intake count."""
    return get(INTAKES_DATASET, {
        "$select": "breed, count(*) as cnt",
        "$group": "breed",
        "$order": "cnt DESC",
        "$limit": limit,
    })


def fetch_intake_types() -> list:
    """Breakdown of intake types (Stray, Owner Surrender, Public Assist, etc.)."""
    return get(INTAKES_DATASET, {
        "$select": "intake_type, count(*) as cnt",
        "$group": "intake_type",
        "$order": "cnt DESC",
    })


def fetch_intake_conditions() -> list:
    """Breakdown of intake conditions (Normal, Injured, Sick, Nursing, etc.)."""
    return get(INTAKES_DATASET, {
        "$select": "intake_condition, count(*) as cnt",
        "$group": "intake_condition",
        "$order": "cnt DESC",
    })


def fetch_euthanasia_reasons() -> list:
    """Outcome subtypes for euthanasia cases (Suffering, Rabies Risk, etc.)."""
    return get(OUTCOMES_DATASET, {
        "$select": "outcome_subtype, count(*) as cnt",
        "$where": "outcome_type = 'Euthanasia' AND outcome_subtype IS NOT NULL",
        "$group": "outcome_subtype",
        "$order": "cnt DESC",
        "$limit": 20,
    })


def fetch_sex_breakdown() -> list:
    """Sex/reproductive status at intake."""
    return get(INTAKES_DATASET, {
        "$select": "sex_upon_intake, count(*) as cnt",
        "$group": "sex_upon_intake",
        "$order": "cnt DESC",
    })


def fetch_latest_date(dataset_id: str) -> str:
    """Get the most recent datetime from a dataset."""
    rows = get(dataset_id, {
        "$select": "datetime",
        "$order": "datetime DESC",
        "$limit": 1,
    })
    if rows:
        return rows[0].get("datetime", "")
    return ""


def fetch_recent_intakes(limit: int = 500) -> list:
    """Fetch the most recent intake records for 'currently in shelter' estimate.
    Orders by datetime DESC.  The frontend cross-references these with recent
    outcomes to approximate which animals are still at the shelter.
    """
    return get(INTAKES_DATASET, {
        "$select": "animal_id, name, datetime, animal_type, breed, color, "
                   "sex_upon_intake, age_upon_intake, intake_type, intake_condition, found_location",
        "$order": "datetime DESC",
        "$limit": limit,
    })


def fetch_recent_outcomes(limit: int = 500) -> list:
    """Fetch the most recent outcome records (ordered DESC)."""
    return get(OUTCOMES_DATASET, {
        "$select": "animal_id, outcome_type, outcome_subtype, datetime",
        "$order": "datetime DESC",
        "$limit": limit,
    })


def fetch_overall_totals() -> dict:
    """Grand totals for summary stats."""
    intakes_total = get(INTAKES_DATASET, {
        "$select": "count(*) as cnt",
    })
    outcomes_total = get(OUTCOMES_DATASET, {
        "$select": "count(*) as cnt",
    })
    # Count live outcomes (adoption + RTO + transfer)
    live_outcomes = get(OUTCOMES_DATASET, {
        "$select": "count(*) as cnt",
        "$where": "outcome_type in ('Adoption', 'Return to Owner', 'Transfer')",
    })
    return {
        "total_intakes": int(intakes_total[0]["cnt"]) if intakes_total else 0,
        "total_outcomes": int(outcomes_total[0]["cnt"]) if outcomes_total else 0,
        "total_live_outcomes": int(live_outcomes[0]["cnt"]) if live_outcomes else 0,
    }


def main():
    print("🐾 Generating shelter data cache...")

    cache = {}

    print("\nFetching overall totals...")
    cache["totals"] = fetch_overall_totals()

    print("\nFetching monthly intakes by animal type...")
    cache["monthlyIntakes"] = fetch_monthly_intakes()

    print("\nFetching monthly outcomes by outcome type...")
    cache["monthlyOutcomes"] = fetch_monthly_outcomes()

    print("\nFetching top breeds...")
    cache["topBreeds"] = fetch_top_breeds()

    print("\nFetching intake types...")
    cache["intakeTypes"] = fetch_intake_types()

    print("\nFetching intake conditions...")
    cache["intakeConditions"] = fetch_intake_conditions()

    print("\nFetching euthanasia reasons...")
    cache["euthanasiaReasons"] = fetch_euthanasia_reasons()

    print("\nFetching sex breakdown...")
    cache["sexBreakdown"] = fetch_sex_breakdown()

    print("\nFetching latest data dates...")
    cache["latestIntakeDate"] = fetch_latest_date(INTAKES_DATASET)
    cache["latestOutcomeDate"] = fetch_latest_date(OUTCOMES_DATASET)

    print("\nFetching recent intakes (most recent 500)...")
    cache["recentIntakes"] = fetch_recent_intakes()

    print("\nFetching recent outcomes (most recent 500)...")
    cache["recentOutcomes"] = fetch_recent_outcomes()

    payload = {
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cache": cache,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    size = OUT.stat().st_size
    print(f"\n✅ Written {size:,} bytes to {OUT}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n❌ Error: {e}", file=sys.stderr)
        sys.exit(1)
