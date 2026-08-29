#!/usr/bin/env python3
"""
Generate docs/homepage/card-stats.json — open/closed counts per category
for the homepage map cards.

Queries Open311 for each category's service codes and counts open vs.
total requests, using the same per-category windows and filters as the map
pages so the homepage cards match the maps they link to.

Per-category windows mirror scripts/generate_map.py (traffic=30d,
homeless=180d, everything else=90d). The homeless category additionally
applies the encampment keyword filter (homeless.homeless_bot.is_encampment_report)
so it matches the homeless map instead of counting every ROW/grounds report.

Run:  python scripts/generate_card_stats.py
Output: docs/homepage/card-stats.json
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

import requests

from open311_client import open311_get
from categories import CATEGORY_CODES, CATEGORY_NAMES

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(message)s")

OPEN311_URL = "https://311.austintexas.gov/open311/v2/requests.json"

# Category → service codes come from categories.py (shared with
# generate_query_data.py). Overlap is intentional — the same 311 report may
# be relevant to multiple categories (e.g., OBSTMIDB is both homeless and
# traffic). Note the bicycle map page shows extra cycling-relevant codes,
# but the Bicycle card counts PWBICYCL only, matching the reporting taxonomy.

# ── Output keys used by the homepage JS (camelCase where historical) ────────
OUTPUT_KEYS = {
    "dead_animal": "deadAnimal",
}

DAYS_BACK = 90
MAX_PAGES = 10
PER_PAGE = 100

# Per-category windows must match scripts/generate_map.py so each card's
# counts line up with the map page it links to.
CATEGORY_DAYS_BACK = {
    "traffic": 30,
    "homeless": 180,
}

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


def _fetch_window_counts(
    service_code: str,
    start: datetime,
    end: datetime,
    filter_fn: Optional[Callable[[dict], bool]] = None,
) -> dict:
    """Fetch open and total counts for a service code within [start, end).

    The Open311 API returns oldest records first, so a high-volume code can
    exceed MAX_PAGES * PER_PAGE records in a window and silently truncate —
    recent (typically open) tickets are never seen. When a window is
    truncated, split it in half recursively until it fits under the cap.

    filter_fn (if given) is applied per record before counting — e.g. the
    homeless category only counts encampment-keyword matches, matching the
    homeless map.
    """
    start_str = _isoformat_z(start)
    end_str = _isoformat_z(end)

    open_count = 0
    total_count = 0
    truncated = False

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
            if filter_fn and not filter_fn(r):
                continue
            total_count += 1
            status = (r.get("status") or "").lower()
            if status == "open":
                open_count += 1

        if len(records) < PER_PAGE:
            break
        if page == MAX_PAGES:
            truncated = True
        time.sleep(0.5)

    if truncated and (end - start) > timedelta(days=1):
        mid = start + (end - start) / 2
        first = _fetch_window_counts(service_code, start, mid, filter_fn)
        second = _fetch_window_counts(service_code, mid, end, filter_fn)
        return {
            "open": first["open"] + second["open"],
            "total": first["total"] + second["total"],
        }

    return {"open": open_count, "total": total_count}


def _fetch_code_counts(
    service_code: str,
    days_back: int = DAYS_BACK,
    filter_fn: Optional[Callable[[dict], bool]] = None,
) -> dict:
    """Fetch open and total counts for a single service code over a window."""
    now = _utc_now()
    return _fetch_window_counts(service_code, now - timedelta(days=days_back), now, filter_fn)


def main() -> None:
    now = _utc_now()
    logger.info(f"=== Card stats — {now.isoformat()} ===")

    # The homeless map only counts encampment-keyword matches (not every
    # ROW/grounds/debris report under those service codes), so apply the same
    # filter here or the card wildly overcounts vs. the map.
    from homeless.homeless_bot import is_encampment_report

    stats = {}

    for category, codes in CATEGORY_CODES.items():
        out_key = OUTPUT_KEYS.get(category, category)
        days_back = CATEGORY_DAYS_BACK.get(category, DAYS_BACK)
        filter_fn = is_encampment_report if category == "homeless" else None
        cat_open = 0
        cat_total = 0
        logger.info(f"{CATEGORY_NAMES.get(category, category)} ({len(codes)} codes, {days_back}d):")

        for code in codes:
            counts = _fetch_code_counts(code, days_back, filter_fn)
            cat_open += counts["open"]
            cat_total += counts["total"]
            logger.info(f"  {code}: {counts['open']} open / {counts['total']} total")

        stats[out_key] = {
            "name": CATEGORY_NAMES.get(category, category),
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
