#!/usr/bin/env python3
"""
Socrata Smoke Check for Homeless Encampment Reports

This script:
1. Verifies Socrata dataset schema (field names)
2. Fetches records via simplified SoQL query
3. Runs client-side _is_encampment_report() filter
4. Compares and reports match rates + discrepancies
"""

import os
import re
import time
import logging
import requests
from datetime import datetime, timezone, timedelta
from typing import Optional

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

# Socrata API Configuration
SOCRATA_BASE = "https://data.austintexas.gov/resource"
DATASET_ID = "xwdj-i9he"
TIMEOUT = 30
MAX_RETRIES = 3

# Service codes from homeless_bot.py
SERVICE_CODES = ["PRGRDISS", "ATCOCIRW", "OBSTMIDB", "SBDEBROW", "DRCHANEL"]

# Keywords from homeless_bot.py
ENCAMPMENT_KEYWORDS = ("encampment", "homelessness", "homeless camp", "homeless", "camp", "tent", "transient", "vagrant")
HSO_KEYWORDS = ("homeless strategy", "hso")
TRASH_KEYWORDS = ("trash", "debris", "garbage")

_session: Optional[requests.Session] = None


def _get_session() -> requests.Session:
    """Get or create HTTP session with Socrata headers."""
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({
            "Accept": "application/json",
            "User-Agent": "austin311bot/0.1 (Socrata smoke check)",
        })
    return _session


def _get_app_token() -> str:
    """Get app token from environment if available."""
    return os.getenv("AUSTINAPIKEY", "")


def _make_request(url: str, params: dict, retries: int = 0) -> dict:
    """Make request to Socrata API with retry logic."""
    session = _get_session()
    
    # Add app token if available
    if "$$app_token" not in params:
        token = _get_app_token()
        if token:
            params["$$app_token"] = token
    
    try:
        resp = session.get(url, params=params, timeout=TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
        if retries < MAX_RETRIES:
            delay = 2 ** retries
            logger.warning(f"Request failed ({e}), retrying in {delay}s...")
            time.sleep(delay)
            return _make_request(url, params, retries + 1)
        raise


def verify_schema() -> dict:
    """Step 0: Verify Socrata dataset schema and return field name mappings."""
    logger.info("=" * 60)
    logger.info("STEP 0: Schema Verification")
    logger.info("=" * 60)
    
    # Try to fetch one record to see field names
    url = f"{SOCRATA_BASE}/{DATASET_ID}.json"
    params = {"$limit": 1}
    
    try:
        data = _make_request(url, params)
        if not data:
            logger.error("No records returned from dataset")
            return {}
        
        record = data[0]
        fields = list(record.keys())
        
        logger.info(f"Dataset: {DATASET_ID}")
        logger.info(f"Total fields: {len(fields)}")
        logger.info("\nField names found:")
        for field in sorted(fields):
            logger.info(f"  - {field}")
        
        # Map expected Open311 fields to actual Socrata fields
        field_mapping = {}
        
        # Common field name patterns (adjusted for xwdj-i9he dataset)
        expected_mappings = {
            "service_request_id": ["sr_number", "service_request_id", "id", "request_id", "sr_id"],
            "service_request_type": ["sr_type_desc", "service_request_type", "type", "request_type", "service_type"],
            "sr_type_code": ["sr_type_code"],  # May not exist - need to filter by description
            "description": ["description", "desc", "details", "issue_description"],  # NOTE: May not exist in Socrata
            "status_notes": ["status_notes", "resolution", "notes", "closure_notes"],  # NOTE: May not exist in Socrata
            "status": ["sr_status_desc", "status", "sr_status", "request_status"],
            "created_date": ["sr_created_date", "created_date", "open_date", "requested_datetime", "date_created"],
            "latitude": ["sr_location_lat", "latitude", "lat", "y", "location_latitude"],
            "longitude": ["sr_location_long", "longitude", "long", "lon", "x", "location_longitude"],
            "address": ["sr_location", "address", "location", "street_address"],
            "department": ["sr_department_desc", "department"],
        }
        
        logger.info("\nField mapping guess:")
        for expected, alternatives in expected_mappings.items():
            for alt in alternatives:
                if alt in fields:
                    field_mapping[expected] = alt
                    logger.info(f"  {expected} -> {alt}")
                    break
            else:
                logger.warning(f"  {expected} -> NOT FOUND")
        
        # Critical check for required fields
        if "description" not in field_mapping:
            logger.error("\n" + "=" * 60)
            logger.error("CRITICAL: 'description' field NOT found in Socrata dataset!")
            logger.error("The dataset does not contain complaint descriptions.")
            logger.error("Keyword filtering on descriptions is NOT possible.")
            logger.error("=" * 60)
        
        if "status_notes" not in field_mapping:
            logger.warning("\n'status_notes' field NOT found in Socrata dataset.")
            logger.warning("HSO routing keyword filtering is NOT possible.")
        
        return field_mapping
        
    except Exception as e:
        logger.error(f"Schema verification failed: {e}")
        return {}


def _word_in(keyword: str, text: str) -> bool:
    """Whole-word matching using regex boundaries (from homeless_bot.py)."""
    if not text:
        return False
    return bool(re.search(r"\b" + re.escape(keyword) + r"\b", text.lower()))


def is_encampment_report_client_side(record: dict, field_mapping: dict) -> bool:
    """
    Client-side filtering matching _is_encampment_report() from homeless_bot.py.
    Uses whole-word matching and field-specific logic.
    """
    # Get field names from mapping
    desc_field = field_mapping.get("description", "description")
    status_notes_field = field_mapping.get("status_notes", "status_notes")
    address_field = field_mapping.get("address", "address")
    
    # Build citizen text (description + address)
    citizen_text = " ".join(filter(None, [
        record.get(desc_field) or "",
        record.get(address_field) or "",
    ])).lower()
    
    # Get status notes (checked separately for HSO keywords)
    status_text = (record.get(status_notes_field) or "").lower()
    full_text = f"{citizen_text} {status_text}"
    
    if not full_text.strip():
        return False
    
    # Direct encampment / homeless keywords - whole-word match only
    for kw in ENCAMPMENT_KEYWORDS:
        if _word_in(kw, full_text):
            return True
    
    # HSO routing keywords - primarily appear in status_notes
    for kw in HSO_KEYWORDS:
        if _word_in(kw, status_text):
            return True
    
    # Fallback for asterisk-formatted HSO mentions like "Homeless* *Strategy* *Office"
    # Strip asterisks and recheck in case the city used asterisks as word separators.
    normalized_status = status_text.replace("*", " ")
    if "homeless strategy" in normalized_status:
        return True
    
    # Trash/debris only counts when "homeless" also appears as a whole word
    has_trash = any(_word_in(kw, full_text) for kw in TRASH_KEYWORDS)
    has_homeless = _word_in("homeless", full_text)
    if has_trash and has_homeless:
        return True
    
    return False


def build_soql_query(field_mapping: dict, days_back: int = 7) -> dict:
    """Build SoQL query parameters for Socrata."""
    # Get actual field names from mapping
    type_field = field_mapping.get("service_request_type", "service_request_type")
    date_field = field_mapping.get("created_date", "created_date")
    desc_field = field_mapping.get("description")  # May be None
    notes_field = field_mapping.get("status_notes")  # May be None
    
    # Calculate date cutoff
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%dT%H:%M:%S")
    
    # Check if we have a service type CODE field or just description
    # The dataset uses sr_type_desc which is "Park Maintenance - Grounds" not "PRGRDISS"
    # We need to filter by type description instead
    type_desc_field = field_mapping.get("service_request_type", "sr_type_desc")
    
    # Build service type filter (using type descriptions since codes may not be available)
    # From homeless_bot.py SERVICE_CODES:
    # PRGRDISS = Park Maintenance, ATCOCIRW = Construction Concerns in ROW, etc.
    type_filters = []
    if "Park" in type_desc_field:  # PRGRDISS
        type_filters.append(f"contains(upper({type_desc_field}), 'PARK')")
    if "Right of Way" in type_desc_field or "ROW" in type_desc_field:  # ATCOCIRW, OBSTMIDB
        type_filters.append(f"contains(upper({type_desc_field}), 'RIGHT OF WAY')")
        type_filters.append(f"contains(upper({type_desc_field}), 'OBSTRUCTION')")
    if "Debris" in type_desc_field or "Street" in type_desc_field:  # SBDEBROW
        type_filters.append(f"contains(upper({type_desc_field}), 'DEBRIS')")
    if "Drainage" in type_desc_field or "Channel" in type_desc_field:  # DRCHANEL
        type_filters.append(f"contains(upper({type_desc_field}), 'DRAINAGE')")
        type_filters.append(f"contains(upper({type_desc_field}), 'CHANNEL')")
    
    if not type_filters:
        # Fallback: just fetch recent records and filter client-side
        type_filter = "1=1"  # Always true
    else:
        type_filter = " OR ".join(type_filters)
    
    # Build WHERE clause
    where_parts = [f"{date_field} >= '{cutoff}'", f"({type_filter})"]
    
    # Add keyword filtering ONLY if description/status_notes fields exist
    keyword_filters = []
    if desc_field:
        keyword_filters.extend([
            f"contains(upper({desc_field}), 'HOMELESS')",
            f"contains(upper({desc_field}), 'ENCAMPMENT')",
            f"contains(upper({desc_field}), 'TENT')",
            f"contains(upper({desc_field}), 'CAMP')",
        ])
    if notes_field:
        keyword_filters.extend([
            f"contains(upper({notes_field}), 'HOMELESS STRATEGY')",
            f"contains(upper({notes_field}), 'HSO')",
            # Catch asterisk variants like "Homeless* *Strategy* *Office" where
            # asterisks break the contiguous "HOMELESS STRATEGY" substring match
            f"(contains(upper({notes_field}), 'HOMELESS') AND contains(upper({notes_field}), 'STRATEGY'))",
        ])
    
    if keyword_filters:
        where_parts.append(f"({' OR '.join(keyword_filters)})")
    
    where_clause = " AND ".join(where_parts)
    
    return {
        "$where": where_clause,
        "$limit": 100,
        "$order": f"{date_field} DESC",
    }


def run_smoke_check(days_back: int = 7):
    """Main smoke check execution."""
    logger.info("\n" + "=" * 60)
    logger.info("SOCRATA SMOKE CHECK: Homeless Encampment Reports")
    logger.info("=" * 60)
    
    # Step 0: Verify schema
    field_mapping = verify_schema()
    if not field_mapping:
        logger.error("Schema verification failed. Cannot proceed.")
        return
    
    logger.info("\n" + "=" * 60)
    logger.info(f"STEP 1: Fetching records (last {days_back} days)")
    logger.info("=" * 60)
    
    # Build and show query
    params = build_soql_query(field_mapping, days_back)
    logger.info(f"\nSoQL Query Parameters:")
    logger.info(f"  $where: {params['$where'][:80]}...")
    logger.info(f"  $limit: {params['$limit']}")
    logger.info(f"  $order: {params['$order']}")
    
    # Fetch from Socrata
    url = f"{SOCRATA_BASE}/{DATASET_ID}.json"
    try:
        records = _make_request(url, params)
        logger.info(f"\nRecords fetched from Socrata: {len(records)}")
    except Exception as e:
        logger.error(f"Failed to fetch records: {e}")
        return
    
    if not records:
        logger.warning("No records returned. Check date range and keywords.")
        return
    
    # Show sample record structure
    logger.info("\n" + "=" * 60)
    logger.info("STEP 2: Sample Record Structure")
    logger.info("=" * 60)
    
    sample = records[0]
    key_fields = [
        field_mapping.get("service_request_id", "service_request_id"),
        field_mapping.get("service_request_type", "service_request_type"),
        field_mapping.get("created_date", "created_date"),
        field_mapping.get("status", "status"),
        field_mapping.get("description", "description"),
        field_mapping.get("status_notes", "status_notes"),
        field_mapping.get("address", "address"),
        field_mapping.get("latitude", "latitude"),
        field_mapping.get("longitude", "longitude"),
    ]
    
    for field in key_fields:
        if field in sample:
            value = sample[field]
            # Truncate long values
            if isinstance(value, str) and len(value) > 60:
                value = value[:57] + "..."
            logger.info(f"  {field}: {value}")
    
    # Step 3: Keyword filter validation
    logger.info("\n" + "=" * 60)
    logger.info("STEP 3: Keyword Filter Validation")
    logger.info("=" * 60)
    
    soql_count = len(records)
    client_match_count = 0
    
    false_positives = []  # Caught by SoQL but rejected by client filter
    
    for record in records:
        if is_encampment_report_client_side(record, field_mapping):
            client_match_count += 1
        else:
            false_positives.append(record)
    
    logger.info(f"\nFilter Comparison:")
    logger.info(f"  SoQL (simplified) returned:     {soql_count} records")
    logger.info(f"  Client-side matched:          {client_match_count} records")
    logger.info(f"  Potential false positives:    {len(false_positives)} records")
    
    if soql_count > 0:
        match_rate = (client_match_count / soql_count) * 100
        logger.info(f"\n  Match rate: {match_rate:.1f}%")
    
    # Show false positives (if any)
    if false_positives:
        logger.info(f"\n--- Sample False Positives (caught by SoQL, rejected by client filter) ---")
        desc_field = field_mapping.get("description", "description")
        notes_field = field_mapping.get("status_notes", "status_notes")
        type_field = field_mapping.get("service_request_type", "service_request_type")
        id_field = field_mapping.get("service_request_id", "service_request_id")
        
        for i, record in enumerate(false_positives[:3], 1):
            logger.info(f"\n{i}. ID: {record.get(id_field, 'N/A')} | Type: {record.get(type_field, 'N/A')}")
            desc = record.get(desc_field, "")
            if desc:
                logger.info(f"   Description: {desc[:100]}..." if len(desc) > 100 else f"   Description: {desc}")
            notes = record.get(notes_field, "")
            if notes:
                logger.info(f"   Status Notes: {notes[:100]}..." if len(notes) > 100 else f"   Status Notes: {notes}")
    
    # Step 4: Recommendation
    logger.info("\n" + "=" * 60)
    logger.info("STEP 4: Recommendation")
    logger.info("=" * 60)
    
    # Check for critical limitations
    has_description = "description" in field_mapping
    has_status_notes = "status_notes" in field_mapping
    
    if not has_description and not has_status_notes:
        logger.error("\n" + "=" * 60)
        logger.error("CRITICAL LIMITATION IDENTIFIED")
        logger.error("=" * 60)
        logger.error("The Socrata dataset (xwdj-i9he) does NOT contain:")
        logger.error("  - 'description' field (citizen complaint text)")
        logger.error("  - 'status_notes' field (resolution notes with HSO routing)")
        logger.error("")
        logger.error("WITHOUT THESE FIELDS:")
        logger.error("  - Cannot keyword filter on complaint descriptions")
        logger.error("  - Cannot detect HSO routing signals")
        logger.error("  - Cannot implement full _is_encampment_report() logic")
        logger.error("")
        logger.error("CONCLUSION: Full migration to Socrata is NOT feasible")
        logger.error("for homeless encampment reports without these fields.")
        logger.error("")
        logger.error("Alternative: Use Socrata ONLY for record fetching,")
        logger.error("then call Open311 detail endpoint for description/status_notes.")
        logger.error("=" * 60)
    elif soql_count == 0:
        logger.warning("No records found. Cannot make recommendation.")
    elif len(false_positives) == 0:
        logger.info("\n✓ RECOMMENDATION: Simplified SoQL filtering is sufficient")
        logger.info("  No false positives detected in sample.")
        logger.info("  Server-side filtering may be adequate for full migration.")
    elif (client_match_count / soql_count) > 0.9:
        logger.info("\n⚠ RECOMMENDATION: SoQL is 'good enough' but not perfect")
        logger.info(f"  Match rate is {(client_match_count / soql_count) * 100:.1f}%.")
        logger.info("  Consider client-side post-filtering for precision.")
    else:
        logger.info("\n✗ RECOMMENDATION: Client-side filtering is required")
        logger.info(f"  Match rate is only {(client_match_count / soql_count) * 100:.1f}%.")
        logger.info("  SoQL misses too many cases or returns too many false positives.")
    
    # Final summary
    logger.info("\n" + "=" * 60)
    logger.info("SUMMARY")
    logger.info("=" * 60)
    logger.info(f"Dataset: {DATASET_ID}")
    logger.info(f"Records fetched: {soql_count}")
    logger.info(f"Date range: last {days_back} days")
    logger.info(f"Service codes: {', '.join(SERVICE_CODES)}")
    logger.info(f"\nKey field mappings discovered:")
    for key, value in field_mapping.items():
        logger.info(f"  {key} -> {value}")
    
    logger.info("\n" + "=" * 60)


if __name__ == "__main__":
    run_smoke_check(days_back=7)
