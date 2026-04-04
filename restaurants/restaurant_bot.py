"""
Restaurant Inspections — data layer and formatters.

Port of restchk/api-client.js and formatting logic from restchk/bot.js.
Queries Austin Restaurant Inspections dataset (ecmv-9xxi) via Socrata API.
"""

import os
import re
import time
import logging
import requests
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

SOCRATA_BASE_URL = "https://data.austintexas.gov/resource/ecmv-9xxi.json"
TIMEOUT = 10
MAX_RETRIES = 3
RETRY_DELAY = 1.0  # seconds, doubled on each retry
CACHE_TTL_DAYS = 14  # biweekly refresh
PAGE_SIZE = 50_000   # Socrata max per request

# In-memory grade distribution cache
_grade_cache: Optional[dict] = None
_grade_cache_ts: Optional[datetime] = None

RETRYABLE_ERRORS = (
    requests.exceptions.Timeout,
    requests.exceptions.ConnectionError,
)

_session: Optional[requests.Session] = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({"Accept": "application/json"})
    return _session


def _build_params(where: str, order: str, limit: int) -> dict:
    params = {
        "$where": where,
        "$order": order,
        "$limit": limit,
    }
    app_token = os.getenv("AUSTIN_APP_TOKEN")
    if app_token and not app_token.startswith("your_"):
        params["$$app_token"] = app_token
    return params


def _make_request(params: dict, retries: int = 0) -> list:
    session = _get_session()
    try:
        resp = session.get(SOCRATA_BASE_URL, params=params, timeout=TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except RETRYABLE_ERRORS as e:
        if retries < MAX_RETRIES:
            delay = RETRY_DELAY * (2 ** retries)
            logger.warning(f"Request failed ({e}), retrying in {delay:.1f}s ({retries+1}/{MAX_RETRIES})")
            time.sleep(delay)
            return _make_request(params, retries + 1)
        raise


def _is_address(search_term: str) -> bool:
    has_numbers = bool(re.search(r"\d", search_term))
    has_street = bool(re.search(
        r"\b(st|ave|avenue|rd|road|dr|drive|blvd|boulevard|ln|lane|way|court|ct|pl|place|sq|square)\b",
        search_term, re.IGNORECASE
    ))
    return has_numbers and has_street


def search_restaurants(search_term: str, limit: int = 10) -> list:
    """Search restaurants by name or address. Returns list of inspection records."""
    clean = re.sub(r"['\"]", "", search_term).strip()

    if _is_address(clean):
        street = clean.split(",")[0].strip()
        where = f"upper(address) like upper('%{street}%')"
    else:
        name = re.sub(r"[^\w\s]", "", clean).strip()
        where = f"upper(restaurant_name) like upper('%{name}%')"

    params = _build_params(where, "inspection_date DESC", limit)
    logger.debug(f"Searching restaurants: {search_term!r}")
    return _make_request(params)


def get_lowest_scoring(limit: int = 10) -> list:
    """Return unique restaurants with the lowest scores, sorted by most recent inspection date."""
    params = _build_params("score is not null", "score ASC", limit * 20)
    logger.debug("Fetching lowest scoring restaurants")
    records = _make_request(params)

    # Deduplicate: keep only the most recent inspection per restaurant
    seen: dict = {}
    for r in records:
        key = (
            (r.get("restaurant_name") or "").lower(),
            (r.get("address") or "").lower(),
        )
        date = r.get("inspection_date") or ""
        if key not in seen or date > seen[key].get("inspection_date", ""):
            seen[key] = r

    # Take the `limit` worst unique restaurants, then sort by most recent inspection first
    worst = sorted(seen.values(), key=lambda r: float(r.get("score") or 999))[:limit]
    worst.sort(key=lambda r: r.get("inspection_date") or "", reverse=True)
    return worst


def format_search_results(restaurants: list, search_term: str) -> str:
    if not restaurants:
        is_addr = _is_address(search_term)
        base = "No restaurants found at that address." if is_addr else "No restaurants found with that name."
        return (
            f"{base}\n\n"
            "💡 *Tips:*\n"
            "1. Try a more general search term\n"
            "2. Check spelling\n"
            + ("3. Try searching by address instead\n" if not is_addr else "")
        )

    # Group by restaurant name, most recent first per group
    grouped: dict = {}
    for r in restaurants:
        name = r.get("restaurant_name") or "Unknown"
        grouped.setdefault(name, []).append(r)

    for name in grouped:
        grouped[name].sort(
            key=lambda r: r.get("inspection_date") or "1900-01-01",
            reverse=True,
        )

    total_inspections = len(restaurants)
    total_restaurants = len(grouped)
    msg = f"Found {total_inspections} inspection(s) for {total_restaurants} restaurant(s):\n\n"

    for i, (name, inspections) in enumerate(grouped.items()):
        latest = inspections[0]
        score = latest.get("score")
        score_str = str(round(float(score))) if score else "N/A"
        msg += f"🏪 *{name}*\n"
        msg += f"📍 {latest.get('address') or 'Address not available'}\n"
        msg += f"📅 Most recent: {latest.get('inspection_date') or 'N/A'}\n"
        msg += f"⭐ Latest score: {score_str}\n"
        msg += f"📋 {latest.get('process_description') or 'N/A'}\n"
        if len(inspections) > 1:
            msg += f"📊 {len(inspections)} total inspections recorded\n"
        msg += "\n"

        if i >= 8 and total_restaurants > 10:
            msg += f"... and {total_restaurants - i - 1} more. Try a more specific search.\n"
            break

    msg += "\n_Source: [Austin Restaurant Inspections](https://data.austintexas.gov/d/ecmv-9xxi)_"
    return msg


GRADE_SCALE = [
    ("A", 95, 100),
    ("B", 90,  94),
    ("C", 85,  89),
    ("D", 75,  84),
    ("F",  0,  74),
]


def score_to_grade(score) -> str:
    try:
        s = float(score)
    except (TypeError, ValueError):
        return "?"
    for grade, low, high in GRADE_SCALE:
        if low <= s <= high:
            return grade
    return "F"


def _fetch_full_year() -> list:
    """Fetch all scored inspection records from the past year, paginated.

    Uses date-only format (YYYY-MM-DD) which is valid SoQL without a token.
    Paginates at PAGE_SIZE with a delay to stay within unauthenticated rate limits.
    """
    one_year_ago = (datetime.now(timezone.utc) - timedelta(days=365)).strftime("%Y-%m-%d")
    where = f"score is not null AND inspection_date > '{one_year_ago}'"
    all_records = []
    offset = 0

    while True:
        params = _build_params(where, "inspection_date DESC", PAGE_SIZE)
        params["$offset"] = offset
        page = _make_request(params)
        if not page:
            break
        all_records.extend(page)
        logger.info(f"Grade cache: fetched {len(all_records)} records so far...")
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE

    return all_records


def _is_cache_fresh() -> bool:
    if _grade_cache is None or _grade_cache_ts is None:
        return False
    age = datetime.now(timezone.utc) - _grade_cache_ts
    return age < timedelta(days=CACHE_TTL_DAYS)


def get_grade_distribution() -> dict:
    """Return grade distribution, rebuilding from full year of data every 14 days."""
    global _grade_cache, _grade_cache_ts

    if _is_cache_fresh():
        logger.debug("Grade cache hit")
        return _grade_cache

    logger.info("Grade cache miss — fetching full year of inspection data...")
    records = _fetch_full_year()

    counts = {g: 0 for g, _, _ in GRADE_SCALE}
    counts["?"] = 0

    # Only count the most recent inspection per unique restaurant
    seen: dict = {}
    for r in records:
        name = (r.get("restaurant_name") or "").lower()
        address = (r.get("address") or "").lower()
        key = (name, address)
        date = r.get("inspection_date") or ""
        if key not in seen or date > seen[key]["date"]:
            seen[key] = {"date": date, "score": r.get("score")}

    for entry in seen.values():
        grade = score_to_grade(entry["score"])
        counts[grade] = counts.get(grade, 0) + 1

    total = sum(v for k, v in counts.items() if k != "?")
    _grade_cache = {
        "counts": counts,
        "total": total,
        "record_count": len(records),
        "cached_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "refreshes_at": (datetime.now(timezone.utc) + timedelta(days=CACHE_TTL_DAYS)).strftime("%Y-%m-%d"),
    }
    _grade_cache_ts = datetime.now(timezone.utc)
    logger.info(f"Grade cache built: {total} unique restaurants from {len(records)} records")
    return _grade_cache


def format_grade_distribution(data: dict) -> str:
    counts = data["counts"]
    total = data["total"]

    if total == 0:
        return "📝 No scored restaurant data available right now."

    grade_emoji = {"A": "🟢", "B": "🔵", "C": "🟡", "D": "🟠", "F": "🔴", "?": "⚪"}

    record_count = data.get("record_count", total)
    cached_at = data.get("cached_at", "unknown")
    refreshes_at = data.get("refreshes_at", "unknown")

    msg = "🍽️ *Restaurant Inspection Grade Distribution*\n"
    msg += f"_Past 12 months · {total} restaurants · {record_count} inspections_\n"
    msg += f"_Data as of {cached_at} · refreshes {refreshes_at}_\n\n"

    for grade, _, _ in GRADE_SCALE:
        count = counts.get(grade, 0)
        pct = count / total * 100 if total else 0
        bar = "█" * min(20, round(pct / 5))
        emoji = grade_emoji[grade]
        ranges = {"A": "95-100", "B": "90-94", "C": "85-89", "D": "75-84", "F": "<75"}
        msg += f"{emoji} *{grade}* ({ranges[grade]}): {count:>4} restaurants ({pct:.1f}%)\n"
        msg += f"   {bar}\n\n"

    if counts.get("?", 0):
        msg += f"⚪ *Unscored:* {counts['?']} restaurants\n\n"

    msg += f"_Score ranges: A=95-100, B=90-94, C=85-89, D=75-84, F=below 75_"
    msg += "\n_Source: [Austin Restaurant Inspections](https://data.austintexas.gov/d/ecmv-9xxi)_"
    return msg


def format_low_scores(restaurants: list) -> str:
    if not restaurants:
        return "No restaurant scores found. Please try again later."

    msg = "🚨 *Lowest Scoring Restaurants* 🚨\n\n"
    msg += f"Showing {len(restaurants)} unique restaurants with lowest scores (most recently inspected first):\n\n"

    for i, r in enumerate(restaurants, 1):
        score = r.get("score")
        score_str = str(round(float(score))) if score else "N/A"
        msg += f"{i}. *{r.get('restaurant_name') or 'Unknown'}*\n"
        msg += f"💩 Score: {score_str}\n"
        msg += f"📍 {r.get('address') or 'Address not available'}\n"
        msg += f"📅 {r.get('inspection_date') or 'Date not available'}\n"
        msg += f"📋 {r.get('process_description') or 'N/A'}\n\n"

    msg += "_Source: [Austin Restaurant Inspections](https://data.austintexas.gov/d/ecmv-9xxi)_"
    return msg
