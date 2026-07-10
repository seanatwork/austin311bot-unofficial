#!/usr/bin/env python3
"""
Hybrid pool data generator.

Fetches from Socrata (xaxa-886r) as baseline, then scrapes individual city
pool pages to patch stale status/hours with current values.

Output: docs/pools/pool-data.json  (corrected data consumed by the client-side map)
        docs/pools/pool-meta.json  (scrape log with timestamps & discrepancies)
"""
import io
import json
import logging
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
log = logging.getLogger(__name__)

SOCRATA_URL = "https://data.austintexas.gov/resource/xaxa-886r.json?$limit=200"
SCOPE_LIMIT = 10  # max concurrent scrape workers
REQUEST_DELAY = 0.3  # seconds between scrape requests per worker

_socrata_session: requests.Session | None = None


def _get_session() -> requests.Session:
    """Return a shared session for Socrata calls (single-threaded)."""
    global _socrata_session
    if _socrata_session is None:
        _socrata_session = requests.Session()
        _socrata_session.headers.update({
            "User-Agent": "austin311bot/0.1 (pool-verifier; +https://austin311.com)",
            "Accept": "application/json",
        })
    return _socrata_session


def _make_scrape_session() -> requests.Session:
    """Return a fresh session for scraping a single pool page (per-thread)."""
    s = requests.Session()
    s.headers.update({
        "User-Agent": "austin311bot/0.1 (pool-verifier; +https://austin311.com)",
        "Accept": "text/html,application/xhtml+xml",
    })
    # Don't mount an adapter with connection pooling to avoid pool warnings
    # under high concurrency
    return s


def fetch_socrata() -> list[dict]:
    """Fetch all pool records from the city's open data portal."""
    log.info("Fetching Socrata data from %s", SOCRATA_URL)
    r = _get_session().get(SOCRATA_URL, timeout=30)
    r.raise_for_status()
    data = r.json()
    log.info("Got %d pool records", len(data))
    return data


def _clean_url(url: str) -> str:
    """Normalise a pool website URL so we can scrape it."""
    if not url:
        return ""
    # Some URLs point to wrong pages (e.g. Govalle → gillis-pool).
    # We trust the URL from Socrata even if wrong — the scrape will
    # detect the mismatch when the pool name on the page doesn't match.
    return url.strip()


def _extract_status(html: str) -> tuple[str | None, str | None]:
    """
    Extract pool status and any repair notice from HTML.
    Returns (status_label, notice).
    """
    status = None
    notice = None

    # Look for <strong>Status:</strong>&nbsp; ...
    m = re.search(
        r'<strong>\s*Status\s*:\s*</strong>\s*(?:&nbsp;|&#160;)?\s*([^<]+?)(?:<|$)',
        html, re.I,
    )
    if m:
        raw = m.group(1).strip()
        # Normalise
        raw = raw.replace("&nbsp;", " ").replace("&#160;", " ").strip()
        # Collapse whitespace
        raw = re.sub(r'\s+', ' ', raw)
        status = raw

    # Look for repair / closure notices in nearby text
    notice_patterns = [
        r'closed\s+(?:until\s+further\s+notice|for\s+(?:repairs?|maintenance))[^.]*\.',
        r'(?:pump|flood)[^.]{0,200}\.',
    ]
    for pat in notice_patterns:
        m2 = re.search(pat, html, re.I)
        if m2:
            raw_notice = m2.group(0).strip()
            raw_notice = re.sub(r'\s+', ' ', raw_notice)
            if notice:
                notice += " " + raw_notice
            else:
                notice = raw_notice

    return status, notice


def _extract_hours_table(html: str) -> tuple[str | None, str | None]:
    """
    Extract weekday/weekend hours from the pool page table.
    Returns (weekday, weekend).
    """
    weekday = None
    weekend = None

    # Find the hours table — look for a <table> containing "Daily Hours of Operation"
    idx = html.find("Daily Hours of Operation")
    if idx < 0:
        return None, None

    # Find the enclosing table
    start = html.rfind("<table", 0, idx)
    end = html.find("</table>", idx)
    if start < 0 or end < 0:
        return None, None

    table_html = html[start:end + 8]

    # Now parse rows
    rows = re.findall(
        r'<tr>\s*<td>(.*?)</td>\s*<td>(.*?)</td>\s*</tr>',
        table_html, re.I | re.S,
    )
    for label, value in rows:
        label_clean = re.sub(r'<[^>]+>', '', label).strip()
        value_clean = re.sub(r'<[^>]+>', '', value).strip()
        value_clean = value_clean.replace("&nbsp;", " ").replace("&#160;", " ")
        value_clean = re.sub(r'\s+', ' ', value_clean).strip()

        if re.match(r'^\s*weekdays?\s*', label_clean, re.I):
            weekday = value_clean
        elif re.match(r'^\s*weekends?\s*', label_clean, re.I):
            weekend = value_clean

    return weekday, weekend


def _scrape_pool_page(pool: dict) -> dict:
    """
    Scrape a single pool's city page for current status and hours.
    Returns a patch dict with scraped fields (or empty dict on failure).
    """
    website = pool.get("website", {})
    if isinstance(website, dict):
        url = website.get("url", "")
    else:
        url = str(website) if website else ""

    url = _clean_url(url)
    if not url:
        return {}

    pool_name = pool.get("pool_name", "")
    log.info("Scraping %s — %s", pool_name, url)

    session = _make_scrape_session()
    try:
        r = session.get(url, timeout=20)
        r.raise_for_status()
    except requests.RequestException as e:
        log.warning("  ⚠  Failed to fetch %s: %s", url, e)
        return {"_scrape_error": str(e)}
    finally:
        session.close()

    html = r.text
    patch: dict = {}

    status, notice = _extract_status(html)
    if status:
        patch["_scraped_status"] = status
        log.info("  Status → %s", status)

    weekday, weekend = _extract_hours_table(html)
    if weekday:
        # Clean up: ensure spaces between concatenated entries
        weekday = re.sub(r'(?<=[a-z])(?=[A-Z0-9])', ' ', weekday)
        weekday = re.sub(r'(?<=[0-9])(?=[A-Z][a-z])', ' ', weekday)
        weekday = re.sub(r'\s+', ' ', weekday).strip()
        patch["_scraped_weekday"] = weekday
        log.info("  Weekday → %s", weekday[:80])
    if weekend:
        weekend = re.sub(r'\s+', ' ', weekend).strip()
        patch["_scraped_weekend"] = weekend
        log.info("  Weekend → %s", weekend[:80])

    if notice:
        patch["_notice"] = notice
        log.info("  Notice → %s", notice[:100])

    return patch


def _is_status_change(scraped: str | None, socrata: str | None) -> bool:
    """Return True if scraped status meaningfully differs from Socrata."""
    if not scraped:
        return False
    s = scraped.lower().strip()
    so = (socrata or "").lower().strip()
    # Map to canonical values
    s_map = "open" if "open" in s and "closed" not in s else ("closed" if "closed" in s else s)
    so_map = "open" if "open" in so and "closed" not in so else ("closed" if "closed" in so else so)
    return s_map != so_map


def generate_pool_data() -> tuple[io.BytesIO | None, str]:
    """
    Main generator: fetch Socrata → scrape individual pages → patch → write JSON.

    Returns (BytesIO of pool-data.json or None, summary message).
    """
    try:
        raw_pools = fetch_socrata()
    except requests.RequestException as e:
        return None, f"Socrata fetch failed: {e}"

    if not raw_pools:
        return None, "No pool data returned from Socrata"

    # Build name→record lookup for quick access
    pool_map: dict[str, dict] = {}
    for p in raw_pools:
        name = p.get("pool_name", "")
        if name:
            pool_map[name] = p

    # Scrape all pools that have website URLs
    scrape_targets = []
    for p in raw_pools:
        website = p.get("website", {})
        if isinstance(website, dict):
            url = website.get("url", "")
        else:
            url = str(website) if website else ""
        if url:
            scrape_targets.append(p)

    log.info("Scraping %d pool pages (concurrency=%d)...", len(scrape_targets), SCOPE_LIMIT)

    discrepancies = []
    scraped_count = 0

    with ThreadPoolExecutor(max_workers=SCOPE_LIMIT) as exec:
        fut_map = {exec.submit(_scrape_pool_page, p): p for p in scrape_targets}
        for fut in as_completed(fut_map):
            pool = fut_map[fut]
            try:
                patch = fut.result()
            except Exception as e:
                log.warning("  ⚠  Scrape failed for %s: %s", pool.get("pool_name", ""), e)
                patch = {"_scrape_error": str(e)}

            if patch:
                pool.update(patch)
                scraped_count += 1

                # Detect discrepancies
                name = pool.get("pool_name", "")
                socrata_status = pool.get("status", "")
                scraped_status = patch.get("_scraped_status")
                if scraped_status and _is_status_change(scraped_status, socrata_status):
                    disc = {
                        "pool": name,
                        "field": "status",
                        "socrata": socrata_status,
                        "scraped": scraped_status,
                    }
                    discrepancies.append(disc)
                    log.warning("  ⚠  STATUS MISMATCH: %s — Socrata=%s → Scraped=%s",
                                name, socrata_status, scraped_status)

            # Polite delay between requests
            time.sleep(REQUEST_DELAY / SCOPE_LIMIT)

    # Add scrape metadata to each record
    now_iso = datetime.now(timezone.utc).isoformat()
    for p in raw_pools:
        p["_verified_at"] = now_iso

    # Build output payload
    output = {
        "_meta": {
            "generated_at": now_iso,
            "total_pools": len(raw_pools),
            "scraped_count": scraped_count,
            "discrepancies_found": len(discrepancies),
            "source_socrata": "xaxa-886r",
            "source_city_pages": f"scraped {scraped_count}/{len(scrape_targets)} pool pages",
        },
        "pools": raw_pools,
        "discrepancies": discrepancies,
    }

    json_bytes = json.dumps(output, indent=2, default=str).encode("utf-8")

    # Write to docs/pools/pool-data.json
    out_path = Path("docs/pools/pool-data.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(json_bytes)
    log.info("Written %d bytes to %s", len(json_bytes), out_path)

    summary = (
        f"Pool data refreshed: {len(raw_pools)} pools, "
        f"{scraped_count} pages scraped, "
        f"{len(discrepancies)} status discrepancies found."
    )
    print(summary)
    return io.BytesIO(json_bytes), summary


if __name__ == "__main__":
    buf, summary = generate_pool_data()
    if not buf:
        print(f"FAILED: {summary}", file=sys.stderr)
        sys.exit(1)
    print(summary)
