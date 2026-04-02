#!/usr/bin/env python3
"""
Graffiti Data Ingestion Script

Pulls the past 30 days of graffiti tickets (HHSGRAFF) from Austin Open311 API
and adds them to the existing 311_categories database.
"""

import sys
import os
import logging
from datetime import datetime, timedelta, timezone

# Add parent directory to path to import open311_ingest functions
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from open311_ingest import (
    _requests_session,
    _request_json_with_backoff,
    _ensure_schema,
    _upsert_requests,
    _isoformat_z,
    _utc_now,
    OPEN311_BASE_URL
)

logger = logging.getLogger(__name__)


def ingest_graffiti_last_90_days(db_path="../311_categories.db", verbose: bool = True):
    """Ingest graffiti tickets from the past 90 days
    
    Args:
        db_path: Path to SQLite database
        verbose: If True, print detailed output. If False, use logging only.
        
    Returns:
        Total number of records ingested
    """
    log = print if verbose else lambda *args, **kwargs: None

    # Service code for graffiti abatement
    service_code = "HHSGRAFF"
    service_name = "Graffiti Abatement - Public Property"

    # Calculate date range (past 90 days)
    end_date = _utc_now()
    start_date = end_date - timedelta(days=90)

    log(f"🎨 Ingesting graffiti data for service code: {service_code}")
    log(f"📅 Date range: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
    log(f"💾 Database: {db_path}")
    
    logger.info(f"Ingesting graffiti data for {service_code} (last 90 days)")

    # Initialize API session with rate limiting
    session = _requests_session(None)  # No API key = conservative rate limiting

    # Connect to database and ensure schema
    import sqlite3
    conn = sqlite3.connect(db_path)
    try:
        _ensure_schema(conn)

        # Ensure index on service_code for query performance
        cursor = conn.cursor()
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_service_code ON open311_requests(service_code)
        """)
        conn.commit()

        # Check existing graffiti records
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COUNT(*), MIN(requested_datetime), MAX(requested_datetime)
            FROM open311_requests
            WHERE service_code = ?
        """, (service_code,))

        existing_count, min_date, max_date = cursor.fetchone()
        log(f"📊 Existing graffiti records: {existing_count:,}")
        if existing_count > 0:
            log(f"📅 Date range: {min_date} to {max_date}")
        
        logger.info(f"Existing graffiti records: {existing_count:,}")
        
        # API endpoint for requests
        url = f"{OPEN311_BASE_URL}/requests.json"

        # Parameters for graffiti requests
        params = {
            "service_code": service_code,
            "start_date": _isoformat_z(start_date),
            "end_date": _isoformat_z(end_date),
            "per_page": 100,
            "page": 1,
            "extensions": "true"
        }

        log(f"🔍 Fetching graffiti tickets from past 90 days...")
        logger.info("Fetching graffiti tickets from Open311 API...")

        total_ingested = 0
        page = 1
        seen_ids = set()

        while True:
            params["page"] = page

            try:
                log(f"   📄 Fetching page {page}...")
                payload = _request_json_with_backoff(session, url, params=params)

                if not isinstance(payload, list) or len(payload) == 0:
                    log(f"   ✅ No more data found")
                    break

                # Filter out already seen records
                new_records = []
                for record in payload:
                    service_request_id = record.get("service_request_id")
                    if service_request_id and service_request_id not in seen_ids:
                        seen_ids.add(service_request_id)
                        new_records.append(record)

                if not new_records:
                    log(f"   ✅ No new records on page {page}")
                    break

                # Insert records into database
                inserted = _upsert_requests(conn, new_records)
                total_ingested += inserted

                log(f"   📝 Page {page}: {inserted:,} new records (total: {total_ingested:,})")
                logger.debug(f"Page {page}: {inserted} new records")

                # Check if we might have more pages
                if len(payload) < params["per_page"]:
                    log(f"   ✅ Last page reached")
                    break

                page += 1

                # Rate limiting (conservative: 2 seconds between requests without API key)
                import time
                time.sleep(2.0)

            except Exception as e:
                log(f"   ❌ Error on page {page}: {e}")
                logger.error(f"Error on page {page}: {e}")
                break

        # Final statistics
        cursor.execute("""
            SELECT COUNT(*), MIN(requested_datetime), MAX(requested_datetime)
            FROM open311_requests
            WHERE service_code = ?
        """, (service_code,))

        final_count, final_min_date, final_max_date = cursor.fetchone()
        newly_added = final_count - existing_count

        log(f"\n🎉 Graffiti ingestion complete!")
        log(f"📊 Total graffiti records: {final_count:,}")
        log(f"🆕 New records added: {newly_added:,}")
        log(f"📅 Full date range: {final_min_date} to {final_max_date}")
        
        logger.info(f"Ingestion complete: {newly_added} new records, {final_count} total")

        # Show some sample data
        cursor.execute("""
            SELECT requested_datetime, status, address, status_notes
            FROM open311_requests
            WHERE service_code = ?
            ORDER BY requested_datetime DESC
            LIMIT 5
        """, (service_code,))

        samples = cursor.fetchall()
        log(f"\n📋 Recent graffiti tickets:")
        for i, (req_date, status, address, notes) in enumerate(samples, 1):
            log(f"   {i}. {req_date} - {status}")
            if address:
                log(f"      📍 {address}")
            if notes and len(notes) < 100:
                log(f"      📝 {notes}")

        conn.commit()

    except Exception as e:
        log(f"❌ Error during ingestion: {e}")
        logger.error(f"Error during ingestion: {e}")
        import traceback
        traceback.print_exc()
        conn.rollback()

    finally:
        conn.close()

    return total_ingested


def main():
    """Main execution function"""
    print("🎨 Graffiti Data Ingestion Tool")
    print("=" * 50)

    try:
        # Run with verbose=True for CLI usage
        total_ingested = ingest_graffiti_last_90_days(verbose=True)

        if total_ingested > 0:
            print(f"\n✅ Successfully ingested {total_ingested:,} graffiti records!")
            print(f"\nNext steps:")
            print(f"1. Run analysis tools to examine patterns")
            print(f"2. Use data for bot development")
            print(f"3. Set up automated ingestion scheduling")
        else:
            print(f"\n📝 No new graffiti records found in the past 90 days")
            print(f"You may already have up-to-date data")

    except KeyboardInterrupt:
        print(f"\n⚠️ Ingestion interrupted by user")
    except Exception as e:
        print(f"❌ Fatal error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
