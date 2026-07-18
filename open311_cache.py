"""
Shared caching layer for Open311 API data.

Uses SQLite for local caching with GitHub Actions cache persistence.
Reduces API calls by storing fetched records and only querying for new data.
"""

import os
import json
import sqlite3
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any

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
