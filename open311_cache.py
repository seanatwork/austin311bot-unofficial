"""
Shared caching layer for Open311 API data.

Uses SQLite for local caching with GitHub Actions cache persistence.
Reduces API calls by storing fetched records and only querying for new data.
"""

import os
import json
import sqlite3
import time
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any

from open311_client import throttle_active

logger = logging.getLogger(__name__)

# Default cache location (in repo root, excluded from git)
CACHE_DIR = Path(".cache")
CACHE_DB = CACHE_DIR / "open311_cache.db"


def _ensure_cache_dir():
    """Ensure cache directory exists."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def init_cache():
    """Initialize the cache database with required tables."""
    _ensure_cache_dir()
    
    conn = sqlite3.connect(CACHE_DB)
    try:
        cursor = conn.cursor()
        
        # Table for cached service requests
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS service_requests (
                service_request_id TEXT PRIMARY KEY,
                service_code TEXT,
                description TEXT,
                status TEXT,
                status_notes TEXT,
                requested_datetime TEXT,
                updated_datetime TEXT,
                address TEXT,
                lat REAL,
                long REAL,
                raw_json TEXT,
                cached_at TEXT,
                category TEXT
            )
        """)
        
        # Table for cache metadata
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS cache_metadata (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT
            )
        """)
        
        # Index for faster queries
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_sr_datetime 
            ON service_requests(requested_datetime)
        """)
        
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_sr_category 
            ON service_requests(category)
        """)
        
        conn.commit()
        logger.info("Cache initialized successfully")
    finally:
        conn.close()


def get_cache_metadata(key: str) -> Optional[str]:
    """Get a metadata value from cache."""
    if not CACHE_DB.exists():
        return None
    
    conn = sqlite3.connect(CACHE_DB)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM cache_metadata WHERE key = ?", (key,))
        result = cursor.fetchone()
        return result[0] if result else None
    finally:
        conn.close()


def set_cache_metadata(key: str, value: str):
    """Set a metadata value in cache."""
    _ensure_cache_dir()
    conn = sqlite3.connect(CACHE_DB)
    try:
        cursor = conn.cursor()
        now = datetime.now(timezone.utc).isoformat()
        cursor.execute("""
            INSERT OR REPLACE INTO cache_metadata (key, value, updated_at)
            VALUES (?, ?, ?)
        """, (key, value, now))
        conn.commit()
    finally:
        conn.close()


def delete_cache_metadata(key: str):
    """Remove a metadata value (a no-op when it is not there)."""
    if not CACHE_DB.exists():
        return
    conn = sqlite3.connect(CACHE_DB)
    try:
        conn.execute("DELETE FROM cache_metadata WHERE key = ?", (key,))
        conn.commit()
    finally:
        conn.close()


def get_cached_records(
    category: Optional[str] = None,
    since: Optional[datetime] = None,
    service_codes: Optional[List[str]] = None
) -> List[Dict[str, Any]]:
    """
    Retrieve cached records, filtered by service code and/or category.

    The cache mirrors raw Open311 records; the `category` column only records
    which module cached the row first and is NOT a reliable partition (service
    codes overlap across categories, e.g. OBSTMIDB is both bicycle and
    homeless). Prefer filtering by `service_codes` alone.

    Args:
        category: Only return rows tagged with this category (rarely wanted)
        since: Only return records since this datetime
        service_codes: Filter by specific service codes

    Returns:
        List of cached records as dictionaries
    """
    if not CACHE_DB.exists():
        return []

    conn = sqlite3.connect(CACHE_DB)
    try:
        cursor = conn.cursor()

        query = "SELECT * FROM service_requests WHERE 1=1"
        params = []

        if category:
            query += " AND category = ?"
            params.append(category)

        if since:
            since_str = since.isoformat()
            query += " AND requested_datetime >= ?"
            params.append(since_str)
        
        if service_codes:
            placeholders = ','.join('?' * len(service_codes))
            query += f" AND service_code IN ({placeholders})"
            params.extend(service_codes)
        
        query += " ORDER BY requested_datetime DESC"
        
        cursor.execute(query, params)
        columns = [desc[0] for desc in cursor.description]
        records = []
        
        for row in cursor.fetchall():
            record = dict(zip(columns, row))
            # Parse raw_json if present
            if record.get('raw_json'):
                try:
                    raw = json.loads(record['raw_json'])
                    record.update(raw)
                except json.JSONDecodeError:
                    pass
            records.append(record)
        
        return records
    finally:
        conn.close()


def attach_service_labels(
    records: List[Dict[str, Any]],
    service_label_map: Dict[str, str],
) -> List[Dict[str, Any]]:
    """Attach _service_label/_service_code to records derived from service_code.

    Cached records are mirrored from raw_json which does NOT include the
    in-memory _service_label/_service_code fields (only freshly-fetched records
    have them). Any module that returns cached records should call this so
    downstream code that groups by code/label works whether the data came from
    a fresh fetch or the cache.
    """
    for r in records:
        code = r.get("service_code") or r.get("_service_code") or ""
        if code:
            if not r.get("_service_label"):
                r["_service_label"] = service_label_map.get(code, code)
            if not r.get("_service_code"):
                r["_service_code"] = code
    return records


def cache_records(category: str, records: List[Dict[str, Any]]):
    """
    Store records in cache.

    Records are keyed by service_request_id (one row per Open311 ticket).
    `category` is only a provenance tag noting which module cached the row;
    reads should filter by service code, not category.

    Args:
        category: Provenance tag for the caching module
        records: List of Open311 records to cache
    """
    if not records:
        return
    
    _ensure_cache_dir()
    conn = sqlite3.connect(CACHE_DB)
    
    try:
        cursor = conn.cursor()
        now = datetime.now(timezone.utc).isoformat()
        
        for record in records:
            sr_id = record.get('service_request_id')
            if not sr_id:
                continue
            
            # Prepare the record
            raw_json = json.dumps(record)
            
            cursor.execute("""
                INSERT OR REPLACE INTO service_requests (
                    service_request_id,
                    service_code,
                    description,
                    status,
                    status_notes,
                    requested_datetime,
                    updated_datetime,
                    address,
                    lat,
                    long,
                    raw_json,
                    cached_at,
                    category
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                sr_id,
                record.get('service_code'),
                record.get('description', '')[:1000],  # Limit length
                record.get('status'),
                record.get('status_notes', '')[:2000],  # Limit length
                record.get('requested_datetime'),
                record.get('updated_datetime'),
                record.get('address'),
                record.get('lat'),
                record.get('long'),
                raw_json,
                now,
                category
            ))
        
        conn.commit()
        logger.info(f"Cached {len(records)} records for category '{category}'")
        
    finally:
        conn.close()


def get_cache_stats(category: Optional[str] = None) -> Dict[str, Any]:
    """Get statistics about the cache."""
    if not CACHE_DB.exists():
        return {"total_records": 0, "categories": {}}
    
    conn = sqlite3.connect(CACHE_DB)
    try:
        cursor = conn.cursor()
        
        if category:
            cursor.execute(
                "SELECT COUNT(*) FROM service_requests WHERE category = ?",
                (category,)
            )
            total = cursor.fetchone()[0]
            
            cursor.execute(
                "SELECT MIN(requested_datetime), MAX(requested_datetime) FROM service_requests WHERE category = ?",
                (category,)
            )
            min_dt, max_dt = cursor.fetchone()
            
            return {
                "total_records": total,
                "date_range": (min_dt, max_dt) if min_dt else None
            }
        else:
            cursor.execute("SELECT COUNT(*) FROM service_requests")
            total = cursor.fetchone()[0]
            
            cursor.execute("SELECT category, COUNT(*) FROM service_requests GROUP BY category")
            categories = {row[0]: row[1] for row in cursor.fetchall()}
            
            return {
                "total_records": total,
                "categories": categories
            }
    finally:
        conn.close()


def clear_cache(category: Optional[str] = None):
    """Clear cache for a category or all cache."""
    if not CACHE_DB.exists():
        return
    
    conn = sqlite3.connect(CACHE_DB)
    try:
        cursor = conn.cursor()
        
        if category:
            cursor.execute("DELETE FROM service_requests WHERE category = ?", (category,))
            # Partial-month markers name service codes, not categories, so they
            # cannot be matched against what was just deleted. Drop them all
            # rather than leave a marker claiming a month is fetched while its
            # rows are gone (that would skip the code forever).
            cursor.execute(
                "DELETE FROM cache_metadata WHERE key LIKE 'month_partial::%'"
            )
            logger.info(f"Cleared cache for category '{category}'")
        else:
            cursor.execute("DELETE FROM service_requests")
            cursor.execute("DELETE FROM cache_metadata")
            logger.info("Cleared all cache")
        
        conn.commit()
    finally:
        conn.close()


def get_last_fetch_date(
    category: Optional[str] = None,
    service_codes: Optional[List[str]] = None,
) -> Optional[datetime]:
    """Get the datetime of the most recent cached record.

    Args:
        category: Restrict to rows tagged with this category (provenance only —
            not a reliable partition; prefer service_codes)
        service_codes: Restrict to these service codes
    """
    if not CACHE_DB.exists():
        return None

    conn = sqlite3.connect(CACHE_DB)
    try:
        cursor = conn.cursor()
        query = "SELECT MAX(requested_datetime) FROM service_requests WHERE 1=1"
        params = []
        if category:
            query += " AND category = ?"
            params.append(category)
        if service_codes:
            placeholders = ','.join('?' * len(service_codes))
            query += f" AND service_code IN ({placeholders})"
            params.extend(service_codes)
        cursor.execute(query, params)
        result = cursor.fetchone()

        if result and result[0]:
            return datetime.fromisoformat(result[0])
        return None
    finally:
        conn.close()


def should_refresh_cache(category: str, max_age_hours: int = 24) -> bool:
    """
    Determine if cache should be refreshed based on age.
    
    Args:
        category: Category to check
        max_age_hours: Maximum age before refresh is needed
    
    Returns:
        True if cache needs refresh, False if still valid
    """
    last_fetch = get_cache_metadata(f"{category}_last_full_fetch")
    
    if not last_fetch:
        return True
    
    last_fetch_dt = datetime.fromisoformat(last_fetch)
    age = datetime.now(timezone.utc) - last_fetch_dt
    
    return age > timedelta(hours=max_age_hours)


def update_last_fetch_date(category: str):
    """Update the last full fetch timestamp for a category."""
    now = datetime.now(timezone.utc).isoformat()
    set_cache_metadata(f"{category}_last_full_fetch", now)


def get_all_records_with_location(
    category: str,
    since: Optional[datetime] = None,
) -> list:
    """Get cached records that have valid lat/long coordinates.

    Args:
        category: Category name
        since: Only return records since this datetime

    Returns:
        List of records with non-null lat/long
    """
    if not CACHE_DB.exists():
        return []

    conn = sqlite3.connect(CACHE_DB)
    try:
        cursor = conn.cursor()

        query = """
            SELECT service_request_id, service_code, status,
                   requested_datetime, updated_datetime,
                   lat, long, address, raw_json
            FROM service_requests
            WHERE category = ? AND lat IS NOT NULL AND long IS NOT NULL
        """
        params = [category]

        if since:
            query += " AND requested_datetime >= ?"
            params.append(since.isoformat())

        cursor.execute(query, params)
        columns = [desc[0] for desc in cursor.description]
        records = []

        for row in cursor.fetchall():
            record = dict(zip(columns, row))
            if record.get("raw_json"):
                try:
                    raw = json.loads(record["raw_json"])
                    record.update(raw)
                except json.JSONDecodeError:
                    pass
            records.append(record)

        return records
    finally:
        conn.close()


def get_record_count_by_date(
    category: str,
    since: Optional[datetime] = None,
    group_by_day: bool = True,
) -> dict:
    """Get record counts grouped by date for a category.

    Args:
        category: Category name
        since: Only count records since this datetime
        group_by_day: If True, group by day (YYYY-MM-DD). If False, return total.

    Returns:
        If group_by_day: { "2026-07-01": {"total": 15, "open": 5, "closed": 10}, ... }
        If not: {"total": 1500, "open": 200, "closed": 1300}
    """
    if not CACHE_DB.exists():
        return {}

    conn = sqlite3.connect(CACHE_DB)
    try:
        cursor = conn.cursor()

        if group_by_day:
            query = """
                SELECT
                    SUBSTR(requested_datetime, 1, 10) as day,
                    status,
                    COUNT(*) as cnt
                FROM service_requests
                WHERE category = ?
            """
            params = [category]

            if since:
                query += " AND requested_datetime >= ?"
                params.append(since.isoformat())

            query += " GROUP BY day, status ORDER BY day ASC"

            cursor.execute(query, params)
            result: dict = {}
            for day, status, cnt in cursor.fetchall():
                if day not in result:
                    result[day] = {"total": 0, "open": 0, "closed": 0}
                result[day]["total"] += cnt
                status_lower = (status or "").lower()
                if status_lower == "open":
                    result[day]["open"] += cnt
                else:
                    result[day]["closed"] += cnt
            return result
        else:
            query = """
                SELECT status, COUNT(*) FROM service_requests
                WHERE category = ?
            """
            params = [category]
            if since:
                query += " AND requested_datetime >= ?"
                params.append(since.isoformat())
            query += " GROUP BY status"

            cursor.execute(query, params)
            total = 0
            open_count = 0
            for status, cnt in cursor.fetchall():
                total += cnt
                if (status or "").lower() == "open":
                    open_count += cnt
            return {"total": total, "open": open_count, "closed": total - open_count}
    finally:
        conn.close()


def get_distinct_service_codes(category: str) -> list:
    """Get all distinct service codes stored for a category."""
    if not CACHE_DB.exists():
        return []

    conn = sqlite3.connect(CACHE_DB)
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT DISTINCT service_code FROM service_requests WHERE category = ? ORDER BY service_code",
            (category,),
        )
        return [row[0] for row in cursor.fetchall()]
    finally:
        conn.close()


def get_all_categories() -> list:
    """Get all distinct category tags in the cache."""
    if not CACHE_DB.exists():
        return []

    conn = sqlite3.connect(CACHE_DB)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT category FROM service_requests ORDER BY category")
        return [row[0] for row in cursor.fetchall()]
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Cache-aware month-by-month fetching (shared by trends + map fetchers)
#
# Why this exists: Open311 returns records oldest-first, so one big request only
# returns the oldest part of the window. The old "cache is fresh" shortcut also
# let a shallow cache pass as complete and undercount months (the reason trends
# used to bypass the cache entirely and re-download a full year every week).
# Cache-aware fetches instead:
#   * always fetch the current (in-progress) month so data stays fresh to today,
#   * only re-fetch a past month when it has NOT been fetched end-to-end before
#     (tracked with a per-code-set month marker), and
#   * return only the requested trailing window (the cache may hold more history).
# ─────────────────────────────────────────────────────────────────────────────

MONTHLY_MAX_PAGES = 10   # default per-(code, month) page cap
MONTHLY_PAGE_SIZE = 100  # Open311 max per_page

# A cold cache wants a full year (13 months × every code) in one run. That burst
# is what trips the city WAF: it starts answering 403 to the runner's IP, every
# remaining month fails, nothing gets marked complete, and the next run repeats
# the identical doomed burst — the loop that kept the weekly job red in Nov 2026.
# Cap how many months one run attempts so a cold cache fills in over a few runs
# (newest first, so the most useful months land first) instead of all at once.
BACKFILL_MONTHS_PER_RUN = 6

# Coverage of the most recent fetch_monthly_with_cache() call. Generators that
# cannot render what they fetched (e.g. a trends page with half a year) need to
# tell "the API throttled us" apart from "there is genuinely no data"; the
# caller reads this instead of having every fetch_*_monthly return a tuple.
_last_coverage: dict = {}


def last_fetch_coverage() -> dict:
    """Coverage summary of the most recent fetch_monthly_with_cache() call.

    Keys: category, months_in_window, months_scheduled, months_attempted,
    months_complete, months_deferred, months_with_data, months_missing, records,
    complete, throttled. Empty dict when no monthly fetch has run in this
    process.
    """
    return dict(_last_coverage)


def _month_key(dt) -> str:
    return dt.strftime("%Y-%m")


def _month_shift(dt, delta):
    """Shift a month-start datetime by `delta` months (may be negative)."""
    month_index = dt.year * 12 + (dt.month - 1) + delta
    year, month0 = divmod(month_index, 12)
    return dt.replace(year=year, month=month0 + 1)


def _month_end(month_start, now) -> datetime:
    if month_start.year == now.year and month_start.month == now.month:
        return now
    return _month_shift(month_start, 1)


def _isoformat_z(dt) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _month_complete_meta_key(service_codes, month_key) -> str:
    codes = ",".join(sorted(service_codes))
    return f"month_complete::{codes}::{month_key}"


def mark_month_complete(service_codes, month_start):
    """Record that a calendar month was fetched end-to-end (all pages, no
    errors) for `service_codes`, so future cache-aware fetches skip it."""
    set_cache_metadata(_month_complete_meta_key(service_codes, _month_key(month_start)), "1")


def _month_partial_meta_key(service_codes, month_key) -> str:
    codes = ",".join(sorted(service_codes))
    return f"month_partial::{codes}::{month_key}"


def mark_month_partial(service_codes, month_start, codes_done):
    """Record which service codes finished for a month that is *not* complete.

    Without this, a month that half-succeeded is re-downloaded whole on the next
    run — including every code that already worked — so a throttled backfill
    keeps re-spending its request budget on pages it already has and never
    catches up. Recording the finished codes makes the retry cover the gap only.
    """
    done = sorted(set(codes_done))
    if not done:
        return
    set_cache_metadata(
        _month_partial_meta_key(service_codes, _month_key(month_start)),
        ",".join(done),
    )


def partial_month_codes(service_codes, month_start) -> set:
    """Service codes already fetched for a month that is still incomplete."""
    raw = get_cache_metadata(
        _month_partial_meta_key(service_codes, _month_key(month_start))
    )
    return {code for code in (raw or "").split(",") if code}


def clear_month_partial(service_codes, month_start):
    """Drop the partial marker for a month that has since completed (or was
    never partially fetched)."""
    delete_cache_metadata(
        _month_partial_meta_key(service_codes, _month_key(month_start))
    )


def is_month_complete(service_codes, month_start) -> bool:
    return get_cache_metadata(_month_complete_meta_key(service_codes, _month_key(month_start))) == "1"


def window_months(months_back, now=None):
    """Month-start datetimes (newest first) covering the trailing `months_back`
    months, up to and including the month of `now`."""
    now = now or datetime.now(timezone.utc)
    current = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    first = (now - timedelta(days=30 * months_back)).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    months = []
    m = current
    while m >= first:
        months.append(m)
        m = _month_shift(m, -1)
    return months


def missing_months(service_codes, months_back, now=None):
    """window_months that still need fetching. The current (in-progress) month
    is always included; past months are included only when they have not been
    fetched end-to-end before (see mark_month_complete)."""
    now = now or datetime.now(timezone.utc)
    current = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return [
        m for m in window_months(months_back, now)
        if m == current or not is_month_complete(service_codes, m)
    ]


def slice_records_to_window(records, months_back, now=None):
    """Keep only records whose requested_datetime falls inside the trailing
    window (`now` minus 30 days * months_back). Records without a parseable
    date are kept defensively."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=30 * months_back)
    kept = []
    for r in records:
        ts = r.get("requested_datetime")
        if ts:
            try:
                if datetime.fromisoformat(ts.replace("Z", "+00:00")) < cutoff:
                    continue
            except ValueError:
                pass
        kept.append(r)
    return kept


def fetch_monthly_with_cache(
    *,
    category,
    service_codes,
    label_map,
    months_back,
    use_cache,
    make_request,
    keep=None,
    extra_params=None,
    max_pages=MONTHLY_MAX_PAGES,
    page_delay=0.6,
    code_delay=1.0,
    now=None,
    max_backfill_months=BACKFILL_MONTHS_PER_RUN,
):
    """Fetch Open311 records month-by-month for `service_codes` across the last
    `months_back` months.

    Args:
        category: Cache provenance tag.
        service_codes: Service codes to fetch and to filter cached rows by.
        label_map: {service_code: human label} attached to returned records.
        months_back: Number of months to fetch.
        use_cache: When True, read/write the SQLite cache and skip past months
            that were already fetched end-to-end. When False, fetch the whole
            window fresh and leave the cache untouched.
        make_request: Callable(params) -> list of records for one API page.
        keep: Optional predicate; only records passing it are returned (the
            cache still stores everything that was fetched).
        extra_params: Extra query params added to every request.
        max_pages: Cap on pages per (code, month).
        page_delay / code_delay: Throttle sleeps between pages / months.
        now: Override the clock (used by tests).
        max_backfill_months: Cap on how many months one cache-aware run
            attempts (BACKFILL_MONTHS_PER_RUN). None disables the cap.
    """
    if use_cache:
        init_cache()
        cached_records = get_cached_records(service_codes=service_codes)
        logger.info(f"Loaded {len(cached_records)} cached {category} records")
    else:
        cached_records = []

    now = now or datetime.now(timezone.utc)
    current_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    months_to_fetch = (
        missing_months(service_codes, months_back, now)
        if use_cache
        else window_months(months_back, now)
    )
    # Full window the page is asking for (months_to_fetch shrinks as months get
    # marked complete, so it cannot be the denominator for "how complete is the
    # chart?").
    months_in_window = len(window_months(months_back, now))

    # Spread a cold-cache backfill over several runs instead of one burst. The
    # list is newest-first, so the months a page needs most are fetched first and
    # whatever is left over is picked up by the next run.
    months_deferred = 0
    if (
        use_cache
        and max_backfill_months
        and len(months_to_fetch) > max_backfill_months
    ):
        months_deferred = len(months_to_fetch) - max_backfill_months
        months_to_fetch = months_to_fetch[:max_backfill_months]

    logger.info(
        f"Will fetch {len(months_to_fetch)} month(s) of {category} data"
        + (f" ({months_deferred} month(s) deferred to a later run)" if months_deferred else "")
    )

    cached_ids = {r.get("service_request_id") for r in cached_records}
    seen_ids = set(cached_ids)
    new_records: list = []
    fresh_records: list = []
    months_attempted = 0
    months_complete = 0
    throttled = False

    for month_start in months_to_fetch:  # newest first
        # A sustained 403 block fails every request it touches, and each one
        # still costs the full retry ladder. Stop here rather than spend the
        # budget (and the job's runtime) on requests that cannot succeed; the
        # months not reached stay missing and are retried next run.
        if throttle_active():
            throttled = True
            logger.warning(
                f"311 API is throttling this runner — stopped backfill for "
                f"{category}; remaining months retried next run"
            )
            break
        month_end = _month_end(month_start, now)
        month_key = _month_key(month_start)
        already_done = (
            partial_month_codes(service_codes, month_start) if use_cache else set()
        )
        codes_todo = [code for code in service_codes if code not in already_done]
        if already_done:
            logger.info(
                f"{month_key}: resuming {category}, "
                f"{len(already_done)} code(s) already cached"
            )
        month_ok = True
        done_codes = set(already_done)
        months_attempted += 1
        for code in codes_todo:
            finished = False
            try:
                page = 1
                while page <= max_pages:
                    params = {
                        "service_code": code,
                        "start_date": _isoformat_z(month_start),
                        "end_date": _isoformat_z(month_end),
                        "per_page": MONTHLY_PAGE_SIZE,
                        "page": page,
                    }
                    if extra_params:
                        params.update(extra_params)
                    batch = make_request(params)
                    if not batch:
                        finished = True
                        break
                    for r in batch:
                        sid = r.get("service_request_id")
                        if sid and sid not in seen_ids:
                            seen_ids.add(sid)
                            r["_service_code"] = code
                            if label_map and code in label_map:
                                r["_service_label"] = label_map[code]
                            new_records.append(r)
                            fresh_records.append(r)
                    if len(batch) < MONTHLY_PAGE_SIZE:
                        finished = True
                        break
                    page += 1
                    time.sleep(page_delay)
            except Exception as e:
                logger.warning(
                    f"Monthly {category} fetch failed {code} {_month_key(month_start)}: {e}"
                )
            if finished:
                done_codes.add(code)
            else:
                month_ok = False
        # Never mark the current (still-accumulating) month complete — it must
        # be re-fetched every run so cached data stays fresh to today.
        if use_cache and month_start != current_month:
            if month_ok:
                mark_month_complete(service_codes, month_start)
                clear_month_partial(service_codes, month_start)
                months_complete += 1
            elif done_codes:
                # Some codes landed before the block. Remember them so the retry
                # fetches only the gap instead of re-downloading the whole month.
                mark_month_partial(service_codes, month_start, done_codes)
        if throttle_active():
            throttled = True
            remaining = len(months_to_fetch) - months_attempted
            if remaining:
                logger.warning(
                    f"311 API is throttling this runner — {remaining} month(s) of "
                    f"{category} deferred to a later run"
                )
            break
        time.sleep(code_delay)

    if use_cache and new_records:
        cache_records(category, new_records)
        logger.info(f"Cached {len(new_records)} new {category} records")

    if use_cache:
        combined = {r.get("service_request_id"): r for r in cached_records}
        for r in fresh_records:
            combined[r.get("service_request_id")] = r
        result = list(combined.values())
        logger.info(
            f"Returning {len(result)} total {category} records "
            f"({len(cached_records)} cached + {len(new_records)} new)"
        )
    else:
        result = fresh_records

    result = slice_records_to_window(result, months_back, now)
    if keep is not None:
        result = [r for r in result if keep(r)]

    # "Complete" = no *past* month is still missing from the cache. The current
    # month is excluded on purpose: it is refetched every run and never marked
    # complete, so a routine 403 on it must not read as a partial backfill (that
    # would freeze the page over a one-week hiccup). A month with genuinely zero
    # reports still counts as complete — it is marked complete when the fetch
    # succeeds, so an empty month is not mistaken for a gap.
    past_missing = 0
    if use_cache:
        past_missing = sum(
            1
            for m in window_months(months_back, now)
            if m != current_month and not is_month_complete(service_codes, m)
        )
    covered_months = {
        (r.get("requested_datetime") or "")[:7]
        for r in result
        if (r.get("requested_datetime") or "")
    } - {""}
    global _last_coverage
    _last_coverage = {
        "category": category,
        "months_in_window": months_in_window,
        "months_scheduled": len(months_to_fetch),
        "months_attempted": months_attempted,
        "months_complete": months_complete,
        "months_deferred": months_deferred,
        "months_with_data": len(covered_months),
        "months_missing": past_missing,
        "records": len(result),
        "complete": past_missing == 0,
        "throttled": throttled or throttle_active(),
    }
    logger.info(
        f"{category} coverage: {len(covered_months)}/{months_in_window} month(s) "
        f"with data, {months_complete} newly complete, "
        f"{past_missing} past month(s) still missing"
        + (", API throttled" if _last_coverage["throttled"] else "")
    )

    return attach_service_labels(result, label_map)
