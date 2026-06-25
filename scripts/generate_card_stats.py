#!/usr/bin/env python3
"""
Generate docs/homepage/card-stats.json — open/closed counts per category
for the homepage map cards.

Queries Open311 for each category's service codes (90-day window) and
counts open vs. total requests.

Run:  python scripts/generate_card_stats.py
Output: docs/homepage/card-stats.json
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

from open311_client import open311_get

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(message)s")

OPEN311_URL = "https://311.austintexas.gov/open311/v2/requests.json"

# ── Category → service codes mapping ────────────────────────────────────────
# Mirrors the service code groupings used by each package's map generator.
# Overlap is intentional — the same 311 report may be relevant to multiple
# analysis categories (e.g., OBSTMIDB is both bicycle and homeless).

CATEGORY_CODES = {
    "homeless": ["PRGRDISS", "ATCOCIRW", "OBSTMIDB", "SBDEBROW", "DRCHANEL"],
    "parking":  ["PARKINGV"],
    "noise":    ["APDNONNO", "DSOUCVMC", "AFDFIREW"],
    "animal":   ["ACLONAG", "ACLOANIM", "ACBITE2", "COAACDD", "ACPROPER", "WILDEXPO", "ACINFORM"],
    "graffiti": ["HHSGRAFF"],
    "parks":    ["PRGRDISS", "PRGRDPLB", "PRGRDELC", "PRBLDPLB", "PRBLDISS", "PRBLDACH", "PRBLDELE", "COMPARLN", "PRCEMET1"],
    "storm":    ["SWSSTORM", "DRCHANEL", "DRILID", "DRFLOODG", "DRSSPIPE", "DRFLOODR", "ZZEROSIO", "DRDITCH"],
    "traffic":  ["SBPOTREP", "TRASIGMA", "STREETL2", "SBDEBROW", "ATTRSIMO", "SIGNSTRE", "OBSINTTR", "SBSIDERE", "SBSTRES", "OBSTMIDB", "ZZARSTSW", "DRCHANEL", "ATCOCIRW", "PWTRISRW", "SBGENRL", "SIGNNEWT", "TRASIGNE", "TPPECRNE"],
    "bicycle":  ["PWBICYCL", "OBSTMIDB", "SBDEBROW", "ATCOCIRW", "ZZARSTSW"],
    "deadAnimal": ["ZZARDEAC"],
}

# ── Card display names (matches homepage card labels) ──────────────────────
CARD_NAMES = {
    "homeless":   "Homeless",
    "parking":    "Parking",
    "noise":      "Noise",
    "animal":     "Animal Services",
    "graffiti":   "Graffiti",
    "parks":      "Parks",
    "storm":      "Storm & Drainage",
    "traffic":    "Traffic",
    "bicycle":    "Bicycle",
    "deadAnimal": "Dead Animal",
}

DAYS_BACK = 90
MAX_PAGES = 10
PER_PAGE = 100

_session: Optional[requests.Session] = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({
            "Accept": "application/json",
            "User-Agent": "austin311bot/card-stats",
        })
    return _session


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _isoformat_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _fetch_code_counts(service_code: str) -> dict:
    """Fetch open and total counts for a single service code (last 90 days)."""
    now = _utc_now()
    start = now - timedelta(days=DAYS_BACK)
    start_str = _isoformat_z(start)
    end_str = _isoformat_z(now)

    open_count = 0
    total_count = 0

    session = _get_session()
    token = os.getenv("AUSTINAPIKEY", "")

    for page in range(1, MAX_PAGES + 1):
        params = {
            "service_code": service_code,
            "start_date": start_str,
            "end_date": end_str,
            "per_page": PER_PAGE,
            "page": page,
        }
        if token:
            params["$$app_token"] = token

        try:
            records = open311_get(session, OPEN311_URL, params)
        except Exception as e:
            logger.warning(f"  {service_code}: fetch failed — {e}")
            break

        if not records:
            break

        for r in records:
            total_count += 1
            status = (r.get("status") or "").lower()
            if status == "open":
                open_count += 1

        if len(records) < PER_PAGE:
            break
        time.sleep(0.5)

    return {"open": open_count, "total": total_count}


def main() -> None:
    now = _utc_now()
    logger.info(f"=== Card stats — {now.isoformat()} ===")
    logger.info(f"Window: last {DAYS_BACK} days")

    stats = {}

    for category, codes in CATEGORY_CODES.items():
        cat_open = 0
        cat_total = 0
        logger.info(f"{CARD_NAMES.get(category, category)} ({len(codes)} codes):")

        for code in codes:
            counts = _fetch_code_counts(code)
            cat_open += counts["open"]
            cat_total += counts["total"]
            logger.info(f"  {code}: {counts['open']} open / {counts['total']} total")

        stats[category] = {
            "name": CARD_NAMES.get(category, category),
            "open": cat_open,
            "total": cat_total,
        }
        logger.info(f"  → {cat_open} open / {cat_total} total")

        time.sleep(0.5)

    output = {
        "generated": now.isoformat(),
        "daysBack": DAYS_BACK,
        "categories": stats,
    }

    out_path = (
        Path(__file__).resolve().parent.parent / "docs" / "homepage" / "card-stats.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, indent=2), encoding="utf-8")
    logger.info(f"Wrote {out_path.stat().st_size:,} bytes to {out_path}")


if __name__ == "__main__":
    main()
