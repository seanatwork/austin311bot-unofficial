#!/usr/bin/env python3
"""
Generate docs/querystore/address_counts.json — troublespot ranking data.

Answers "where are the troublespots?" — the top reported addresses and
intersections for all 311 activity, overall and broken out by problem type
(graffiti, abandoned vehicles, parking, trash, code violations, noise, ...),
over the trailing 90 and 365 days.

Data source: Socrata `xwdj-i9he` ("Austin 311 Public Data"), NOT Open311.
The Open311 v2 endpoint only exposes ~40% of 311 volume — it has no ARR
(trash), DSD (code violations) or APD (vehicle abatement) codes at all — so
a troublespot ranking built from it would miss 60% of the city's reports.
Socrata also does the `GROUP BY sr_location` server-side, so the whole
ranking costs ~20 sub-second queries instead of a full-year cache backfill.

The one exception is `homeless`: the encampment filter needs `description`
and `status_notes`, which Socrata does not publish. That category is built
from the local Open311 SQLite cache instead (a local read, zero extra API
calls) using the exact same `is_encampment_report()` the map and the
reporting categories use.

Ranking is by *reported location*, not by offender: the top addresses are
large apartment complexes and shelters.

Run:  AUSTINAPIKEY=sk... python scripts/generate_address_data.py
Output: docs/querystore/address_counts.json

This runs in CI only (the local cache has no homeless rows), so the script
narrates its own correctness into the log — one line per (window, category)
with raw -> merged -> blank -> top counts. Read the log to validate a run.
"""

import json
import logging
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Add project root to path so we can import repo modules
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import requests  # noqa: E402

from open311_cache import get_cached_records  # noqa: E402
from homeless.homeless_bot import is_encampment_report  # noqa: E402

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(message)s")

SOCRATA_URL = "https://data.austintexas.gov/resource/xwdj-i9he.json"
OUTPUT_DIR = REPO_ROOT / "docs" / "querystore"
OUTPUT_FILE = OUTPUT_DIR / "address_counts.json"

# Trailing windows, keyed by the label used in the JSON.
WINDOWS = {"90d": 90, "365d": 365}

# Pull more rows than we display so canonicalization can merge reversed
# intersection pairs ("A & B" + "B & A") that both rank below the cut.
TOP_RAW = 5000          # overall: distinct sr_location rows requested
TOP_CATEGORY_RAW = 1000  # per category
TOP_OVERALL = 200       # entries kept in `all`
TOP_CATEGORY = 25       # entries kept in each by_category list

API_KEY = os.getenv("AUSTINAPIKEY", "")

# ── Troublespot taxonomy ────────────────────────────────────────────────────
# Separate from categories.py on purpose: the site's reporting categories
# are built from Open311 service codes and exclude trash, code violations and
# abandoned vehicles — exactly the volume this feature exists to surface.
# Entries are matched against Socrata `sr_type_desc` by exact type and/or
# prefix. Categories are not a partition (TPW - Parking Violation Enforcement
# is in both `parking` and `traffic`), same as the existing taxonomy.
TROUBLESPOT_TYPES: Dict[str, Dict[str, Any]] = {
    "graffiti": {
        "label": "Graffiti",
        "types": [
            "APH - Graffiti Abatement - Public Property",
            "DSD - Graffiti Abatement - Private Property",
        ],
    },
    "abandoned_vehicles": {
        "label": "Abandoned Vehicles",
        "types": [
            "APD - Vehicle Abatement Report",
            "APD - Vehicle Abatement Callback Request",
        ],
    },
    "parking": {
        "label": "Parking",
        "types": ["TPW - Parking Violation Enforcement"],
    },
    "trash": {
        "label": "Trash & Dumping",
        "types": [
            "ARR - Garbage",
            "ARR - Bulk",
            "ARR - Compost",
            "ARR - Recycling",
            "ARR - General",
            "ARR - Brush",
            "ARR - Household Hazardous Waste",
        ],
    },
    "code_violations": {
        "label": "Code Violations",
        "types": [
            "DSD - Request Code Officer",
            "DSD - Follow-Up",
            "DSD - Short Term Rental Complaint",
            "DSD - Billboard Complaint",
        ],
    },
    "noise": {
        "label": "Noise",
        "types": [
            "DSD - Outdoor Commercial Venue Music Complaint",
            "AFD - Fireworks Complaint",
            "ZZ - AFD - Fireworks Complaint",
        ],
    },
    "animals": {
        "label": "Animals",
        # Prefixes cover every Animal Protection / Pet Resource Center variant
        # (including the "ZZ - " duplicates the city emits for some types).
        "types": ["ARR - Dead Animal Collection", "ARR - Pet Search"],
        "prefixes": [
            "Animal Protection - ",
            "Animal Services - ",
            "Pet Resource Center - ",
            "ZZ - Pet Resource Center - ",
        ],
    },
    "traffic": {
        "label": "Traffic & Streets",
        "prefixes": ["TPW - "],
    },
    # Austin Water and Watershed Protection are separate reporting categories
    # (`water` = AWUBEEMT/WWREPORT, `storm` = the WPD drainage codes), so the
    # ranking splits them too: a "most-reported water locations" answer should
    # not be dominated by storm drains, and vice versa.
    "water": {
        "label": "Water",
        "prefixes": ["AW - "],
    },
    "storm": {
        "label": "Storm & Drainage",
        "prefixes": ["WPD - "],
    },
    "parks": {
        "label": "Parks",
        "prefixes": ["Park Maintenance - ", "PARD - ", "APR (PARD) - "],
    },
}

# Homeless is the one category not derived from Socrata — see module docstring.
# These are the 5 generic codes the site treats as homeless (PRGRDISS etc.),
# keyword-filtered at read time. Do NOT filter the cache read by `category`:
# that column is only a provenance tag and these codes are shared with
# parks/traffic/bicycle.
HOMELESS_CODES = ["PRGRDISS", "OBSTMIDB", "SBDEBROW", "DRCHANEL", "ATCOCIRW"]

# ", AUSTIN, TX 78754" / ", AUSTIN, TX" suffixes
CITY_SUFFIX = re.compile(r",\s*AUSTIN\s*,\s*TX\b\s*(?:\d{5})?\s*$", re.I)
# Locations that are *only* a city/state ("AUSTIN, TX 78745")
CITY_ONLY = re.compile(r"^AUSTIN\s*,?\s*TX\b.*$", re.I)
# "45TH" -> "45th" after str.title() mangles it into "45Th"
ORDINAL = re.compile(r"\b(\d+)(St|Nd|Rd|Th)\b", re.I)
# Road-designator tokens that must stay uppercase ("N FM 620 RD" not "Fm")
ROAD_ABBREV = {"Fm", "Rm", "Ih", "Us", "Sh", "Hwy", "Lp"}

_session: Optional[requests.Session] = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({
            "Accept": "application/json",
            "User-Agent": "austin311bot/address-data",
        })
    return _session


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _socrata_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


# ── Canonicalization ────────────────────────────────────────────────────────

def canon(loc: str) -> Tuple[str, str]:
    """Return (canonical_key, kind) where kind is 'address' or 'intersection'.

    The city stores the same corner both ways ("AIRPORT BLVD & E 45TH ST" and
    "E 45TH ST & AIRPORT BLVD"), so intersections are sorted — without this the
    #1 and #2 troublespots are the same corner counted twice.

    Returns an empty key for blank/city-only locations, which callers drop.
    """
    s = (loc or "").strip()
    if not s:
        return "", "address"

    s = CITY_SUFFIX.sub("", s)
    s = s.upper()
    s = re.sub(r"\s*&\s*", " & ", s)
    s = re.sub(r"\s+", " ", s).strip(" ,")

    if not s or CITY_ONLY.match(s):
        return "", "address"

    if " & " in s:
        parts = sorted(p.strip() for p in s.split(" & ") if p.strip())
        if len(parts) >= 2:
            return " & ".join(parts), "intersection"

    return s, "address"


def _display(key: str) -> str:
    """Title-case a canonical key for display."""
    s = ORDINAL.sub(lambda m: m.group(1) + m.group(2).lower(), key.title())
    return " ".join(
        w.upper() if w in ROAD_ABBREV else w for w in s.split(" ")
    )


# ── Socrata ─────────────────────────────────────────────────────────────────

def _socrata_get(params: Dict[str, str]) -> List[dict]:
    """Run one Socrata query, retrying transient failures."""
    session = _get_session()
    if API_KEY:
        params = {**params, "$$app_token": API_KEY}

    last_err: Optional[Exception] = None
    for attempt in range(3):
        try:
            resp = session.get(SOCRATA_URL, params=params, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list):
                raise ValueError(f"unexpected Socrata response: {data}")
            return data
        except Exception as e:  # noqa: BLE001 - retried below, raised at the end
            last_err = e
            if attempt < 2:
                time.sleep(2 ** attempt * 2)
    raise RuntimeError(f"Socrata query failed: {last_err}")


def _type_filter(spec: Dict[str, Any]) -> str:
    """Build a SoQL predicate matching one troublespot category's types."""
    clauses: List[str] = []
    types = spec.get("types") or []
    if types:
        quoted = ",".join("'" + t.replace("'", "''") + "'" for t in types)
        clauses.append(f"sr_type_desc in({quoted})")
    for prefix in spec.get("prefixes") or []:
        clauses.append("starts_with(sr_type_desc,'" + prefix.replace("'", "''") + "')")
    if not clauses:
        raise ValueError("troublespot category has no types")
    return "(" + " OR ".join(clauses) + ")"


def _fetch_location_counts(base_where: str, type_predicate: str = "", limit: int = TOP_RAW) -> List[dict]:
    """Grouped sr_location counts, largest first."""
    where = base_where if not type_predicate else f"{base_where} AND {type_predicate}"
    return _socrata_get({
        "$select": "sr_location, count(*) as n",
        "$where": where,
        "$group": "sr_location",
        "$order": "n DESC",
        "$limit": str(limit),
    })


def _fetch_total(base_where: str) -> int:
    """Total requests matching `base_where`, for the log's context column."""
    rows = _socrata_get({
        "$select": "count(*) as n",
        "$where": base_where,
        "$limit": "1",
    })
    return int(rows[0]["n"]) if rows else 0


# ── Aggregation ─────────────────────────────────────────────────────────────

def _aggregate(rows: List[dict]) -> Tuple[Counter, int, int, int]:
    """Merge raw sr_location rows into canonical counts.

    Returns (counts keyed by canonical key, rows seen, blank rows dropped,
    requests dropped with those blank rows).
    """
    counts: Counter = Counter()
    seen = 0
    blank = 0
    blank_volume = 0
    for row in rows:
        seen += 1
        key, _kind = canon(row.get("sr_location") or "")
        if not key:
            blank += 1
            blank_volume += int(row.get("n") or 0)
            continue
        counts[key] += int(row.get("n") or 0)
    return counts, seen, blank, blank_volume


def _top_list(counts: Counter, limit: int) -> List[dict]:
    """Sorted, capped display list. Re-sorted after merging: a merged
    intersection pair can outrank both of its halves."""
    items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    return [
        {
            "address": _display(key),
            "count": count,
            "kind": "intersection" if " & " in key else "address",
        }
        for key, count in items
    ]


def _homeless_records() -> List[dict]:
    """Encampment-filtered records from the local Open311 cache.

    Deliberately no `init_cache()`: creating an empty cache DB here could get
    saved back over the real one by the workflow's cache/save step.
    """
    try:
        cached = get_cached_records(service_codes=HOMELESS_CODES)
    except Exception as e:  # noqa: BLE001 - fail soft, the category is optional
        logger.warning(f"  homeless: cache read failed — {e}")
        return []
    return [r for r in cached if is_encampment_report(r)]


def _homeless_counts(records: List[dict], cutoff: datetime) -> Tuple[Counter, int, int, int]:
    """Count encampment reports per canonical address inside the window.

    Returns (counts, records in window, blank addresses, records with no text).
    """
    counts: Counter = Counter()
    in_window = 0
    blank = 0
    no_text = 0
    for r in records:
        ts = r.get("requested_datetime") or ""
        if not ts:
            continue
        try:
            if datetime.fromisoformat(ts.replace("Z", "+00:00")) < cutoff:
                continue
        except ValueError:
            continue
        in_window += 1
        if not (r.get("description") or r.get("status_notes")):
            no_text += 1
        key, _kind = canon(r.get("address") or "")
        if not key:
            blank += 1
            continue
        counts[key] += 1
    return counts, in_window, blank, no_text


def _log_line(
    window: str,
    name: str,
    counts: Counter,
    raw: int,
    blank: int,
    blank_volume: int = 0,
    truncated: bool = False,
    extra: str = "",
) -> None:
    """One self-check line per (window, category) — the log is the test."""
    top = _top_list(counts, 1)
    top_txt = f"{top[0]['address']} ({top[0]['count']:,})" if top else "(none)"
    raw_txt = f"{raw:,}{'+' if truncated else ''}"
    blank_txt = f"{blank:,}" + (f"/{blank_volume:,}req" if blank_volume else "")
    logger.info(
        f"[{window}] {name:<18} raw={raw_txt:>7} merged={len(counts):>6,} "
        f"blank={blank_txt:<13} {extra}top={top_txt}"
    )


# ── Main ────────────────────────────────────────────────────────────────────

def main() -> int:
    now = _utc_now()
    logger.info(f"=== Troublespot address ranking — {now.isoformat()} ===")
    logger.info(f"Source: socrata:xwdj-i9he · categories: {len(TROUBLESPOT_TYPES) + 1}")

    # Read the homeless cache once and slice it per window below.
    homeless = _homeless_records()
    logger.info(f"Homeless cache: {len(homeless)} encampment-filtered records")

    result: Dict[str, Any] = {
        "generated": now.isoformat(),
        "source": "socrata:xwdj-i9he",
        "windows": {},
    }
    failures: List[str] = []

    for window, days in WINDOWS.items():
        cutoff = now - timedelta(days=days)
        base_where = (
            f"sr_created_date > '{_socrata_iso(cutoff)}' "
            "AND sr_location IS NOT NULL"
        )
        logger.info(f"\n--- {window} (since {cutoff.date()}) ---")

        try:
            total = _fetch_total(base_where)
        except RuntimeError as e:
            failures.append(f"{window} all-total: {e}")
            total = -1

        try:
            rows = _fetch_location_counts(base_where, limit=TOP_RAW)
        except RuntimeError as e:
            failures.append(f"{window} all: {e}")
            logger.error(f"  [{window}] all: {e}")
            continue

        counts, seen, blank, blank_volume = _aggregate(rows)
        block: Dict[str, Any] = {"all": _top_list(counts, TOP_OVERALL), "by_category": {}}
        _log_line(
            window, "all", counts, seen, blank, blank_volume,
            truncated=len(rows) >= TOP_RAW,
            extra=f"total={total:,} " if total >= 0 else "",
        )

        for name, spec in TROUBLESPOT_TYPES.items():
            try:
                cat_rows = _fetch_location_counts(base_where, _type_filter(spec), TOP_CATEGORY_RAW)
            except RuntimeError as e:
                failures.append(f"{window} {name}: {e}")
                logger.error(f"  [{window}] {name}: {e}")
                continue
            cat_counts, cat_seen, cat_blank, cat_blank_volume = _aggregate(cat_rows)
            block["by_category"][name] = _top_list(cat_counts, TOP_CATEGORY)
            _log_line(
                window, name, cat_counts, cat_seen, cat_blank, cat_blank_volume,
                truncated=len(cat_rows) >= TOP_CATEGORY_RAW,
            )

        # Homeless: from the cache, not Socrata.
        if homeless:
            h_counts, h_seen, h_blank, h_no_text = _homeless_counts(homeless, cutoff)
            block["by_category"]["homeless"] = _top_list(h_counts, TOP_CATEGORY)
            _log_line(
                window, "homeless", h_counts, h_seen, h_blank,
                extra=f"no_text={h_no_text:,} ",
            )
        else:
            logger.warning(
                f"  [{window}] homeless: no cached encampment records — "
                "omitting the category (is the Open311 cache warm?)"
            )

        result["windows"][window] = block

    if failures:
        logger.error("\nSocrata failures — not writing (previous file stays valid):")
        for f in failures:
            logger.error(f"  {f}")
        return 1

    if not any(w.get("all") for w in result["windows"].values()):
        logger.error("No window produced any addresses — refusing to write an empty file.")
        return 1

    # Atomic write so a crash mid-dump can't truncate the published file.
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = OUTPUT_FILE.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(result, f, indent=2)
    os.replace(tmp, OUTPUT_FILE)

    size_kb = OUTPUT_FILE.stat().st_size / 1024
    logger.info(f"\nWrote {OUTPUT_FILE} ({size_kb:.1f} KB)")
    for window, block in result["windows"].items():
        n_all = len(block.get("all") or [])
        n_cats = len(block.get("by_category") or {})
        logger.info(f"  {window}: {n_all} addresses overall, {n_cats} categories")
    return 0


if __name__ == "__main__":
    sys.exit(main())
