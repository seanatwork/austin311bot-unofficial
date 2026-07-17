#!/usr/bin/env python3
"""
Generate docs/complaints/digest.json — "Hidden Austin" weekly digest.

Fetches all descriptions from 29 Open311 service codes (past 7 days), separates
citizen-written complaints from city boilerplate responses, deduplicates near-duplicates,
then surfaces the most interesting citizen-written complaints via a heuristic ranking.

Run:  python scripts/generate_weekly_digest.py
Output: docs/complaints/digest.json
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

from open311_client import open311_get

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(message)s")

OPEN311_URL = "https://311.austintexas.gov/open311/v2/requests.json"

# ── 29 description-rich service codes ──────────────────────────────────────
DIGEST_CODES = [
    ("DSOUCVMC", "Outdoor Music Venue"),
    ("APDNONNO", "Noise Complaint"),
    ("ACLONAG", "Loose Dog"),
    ("ACPROPER", "Animal Care"),
    ("WILDEXPO", "Wildlife"),
    ("ACLOANIM", "Loose Animal"),
    ("PRGRDISS", "Homeless/Grounds"),
    ("OBSTMIDB", "Obstruction"),
    ("PARKINGV", "Parking"),
    ("SBDEBROW", "Debris"),
    ("DSDENVCO", "Tree/Environmental"),
    ("ACCOYTE", "Coyote"),
    ("SIGNSTRE", "Street Sign"),
    ("ZZARSTSW", "Street Sweeping"),
    ("SBGENRL", "Street Misc"),
    ("AFDFIREW", "Fireworks"),
    ("SBPOTREP", "Pothole"),
    ("DRFLOODG", "Flooding"),
    ("SWSSTORM", "Storm Debris"),
    ("STREETL2", "Street Light"),
    ("HHSGRAFF", "Graffiti"),
    ("TRASIGMA", "Traffic Signal"),
    ("DRCHANEL", "Drainage"),
    ("ACBITE2", "Animal Bite"),
    ("COAACDD", "Vicious Dog"),
    ("PWBICYCL", "Bicycle Issue"),
    ("ZZARDEAC", "Dead Animal"),
    ("ACINFORM", "Animal Protection"),
    ("ATCOCIRW", "Construction in ROW"),
]

_session: Optional[requests.Session] = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({
            "Accept": "application/json",
            "User-Agent": "austin311bot/weekly-digest",
        })
    return _session


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _isoformat_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _fetch_open311(params: dict) -> Optional[list]:
    """Fetch from Open311 using the shared retry-aware client."""
    session = _get_session()
    token = os.getenv("AUSTINAPIKEY", "")
    if token:
        params["$$app_token"] = token
    try:
        return open311_get(session, OPEN311_URL, params)
    except Exception as e:
        logger.warning(f"Open311 fetch failed: {e}")
        return None


# ── Profanity censor ────────────────────────────────────────────────────────

_PROFANITY_CENSOR = {
    "fucking": "f***ing",
    "fuck": "f***",
    "fucker": "f***er",
    "fucked": "f***ed",
    "shit": "sh**",
    "bitch": "b****",
    "bitches": "b****es",
    "damn": "d***",
    "bastard": "b*****d",
}


def _censor(text: str) -> str:
    for word, replacement in _PROFANITY_CENSOR.items():
        text = re.sub(re.escape(word), replacement, text, flags=re.IGNORECASE)
    return text


# ── Phase 1: Fetch descriptions ──────────────────────────────────────────────

def fetch_weekly_descriptions() -> list:
    """Fetch ALL descriptions from all digest codes for the past 7 days.

    Returns a list of dicts, deduplicated by service_request_id.
    """
    now = _utc_now()
    week_ago = now - timedelta(days=7)
    start_str = _isoformat_z(week_ago)
    end_str = _isoformat_z(now)

    seen_ids: set = set()
    all_records: list = []

    logger.info(f"Fetching descriptions from {week_ago:%Y-%m-%d} to {now:%Y-%m-%d}")
    logger.info(f"Scanning {len(DIGEST_CODES)} service codes…")

    for code, label in DIGEST_CODES:
        code_records = 0
        page = 1
        while page <= 3:
            records = _fetch_open311({
                "service_code": code,
                "start_date": start_str,
                "end_date": end_str,
                "per_page": 100,
                "page": page,
            })
            if records is None:
                logger.warning(f"  {code} ({label}): fetch failed (all retries exhausted)")
                break
            if not records:
                break

            for r in records:
                sid = r.get("service_request_id")
                if sid and sid not in seen_ids:
                    seen_ids.add(sid)
                    desc = (r.get("description") or "").strip()
                    notes = (r.get("status_notes") or "").strip()
                    text = desc if len(desc) > 15 else (notes if len(notes) > 10 else "")
                    if not text:
                        continue
                    all_records.append({
                        "id": sid,
                        "code": code,
                        "category": label,
                        "text": _censor(text)[:400],
                        "address": (r.get("address") or "").strip(),
                        "date": (r.get("requested_datetime") or "")[:10],
                    })
                    code_records += 1

            if len(records) < 100:
                break
            page += 1
            time.sleep(1.0)

        if code_records:
            logger.info(f"  {code} ({label}): {code_records} descriptions")
        time.sleep(0.5)

    logger.info(f"Total: {len(all_records)} unique descriptions across {len(DIGEST_CODES)} codes")
    return all_records


# ── Phase 2: Classify & filter ───────────────────────────────────────────────

# Patterns that indicate a city-written status update, not a citizen complaint.
_CITY_BOILERPLATE = [
    "close sr", "closed sr", "duplicate - close", "no issue found",
    "inspection performed", "no action needed", "no violation",
    "future work scheduled", "added for future clean up",
    "referred to 311", "referred to", "citation issued",
    "contact made", "unable to locate", "utl",
    "patrol for bite", "patrolled for stray",
    "job#", "no sound violation", "investigated",
    "will monitor", "will continue to monitor",
    "no further action", "case closed",
    "sr closed", "work order submitted",
    "assigned to", "routed to",
]


def _is_city_response(text: str) -> bool:
    """Return True if the text looks like a city-written status update."""
    t = text.strip().lower()
    # Very short or all-caps descriptions are often city boilerplate
    if len(t) < 30:
        return True
    # All-uppercase and > 60% uppercase letters = likely city boilerplate
    alpha_chars = [c for c in text if c.isalpha()]
    if alpha_chars and sum(1 for c in alpha_chars if c.isupper()) / len(alpha_chars) > 0.6:
        return True
    # Check known boilerplate phrases
    for marker in _CITY_BOILERPLATE:
        if marker in t:
            return True
    return False


def _classify_and_filter(records: list) -> tuple:
    """Split records into citizen-written complaints and city responses.

    Returns (citizen_complaints, city_responses).
    Also adds a 'notable' field to citizen complaints for the browse UI.
    """
    citizen = []
    city = []
    for r in records:
        if _is_city_response(r["text"]):
            city.append(r)
        else:
            r["notable"] = True  # All citizen-written are notable
            citizen.append(r)

    logger.info(
        f"Classification: {len(citizen)} citizen-written, "
        f"{len(city)} city responses"
    )
    return citizen, city


# ── Phase 3: Deduplicate near-duplicates ─────────────────────────────────────

def _text_similarity(a: str, b: str) -> float:
    """Quick similarity check: Jaccard on word trigrams."""
    def trigrams(s):
        words = s.lower().split()
        return set(" ".join(words[i:i+3]) for i in range(len(words) - 2))

    ta, tb = trigrams(a), trigrams(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _deduplicate_citizen(records: list) -> list:
    """Remove near-duplicate citizen complaints (same address + similar text).

    Keeps the longest version of each cluster. Adds a '_dedupCount' field
    to indicate how many similar complaints were merged.
    """
    if len(records) <= 1:
        return records

    deduped = []
    skipped_indices = set()

    for i, r in enumerate(records):
        if i in skipped_indices:
            continue
        dup_count = 1
        best = r
        for j in range(i + 1, len(records)):
            if j in skipped_indices:
                continue
            other = records[j]
            # Must share same address and date to be considered a duplicate
            if r["address"] != other["address"] or r["date"] != other["date"]:
                continue
            if _text_similarity(r["text"], other["text"]) > 0.4:
                dup_count += 1
                skipped_indices.add(j)
                if len(other["text"]) > len(best["text"]):
                    best = other

        best["_dedupCount"] = dup_count
        deduped.append(best)

    logger.info(f"Deduplication: {len(records)} → {len(deduped)} unique citizen complaints")
    return deduped


# ── Phase 4: Interestingness-based curation ─────────────────────────────────

def _interestingness_score(record: dict) -> float:
    """Heuristic score for ranking citizen complaints.

    Higher score = more likely to be an interesting read.
    """
    text = record["text"]
    score = 0.0

    # Length sweet spot: 80-400 chars
    length = len(text)
    if 80 <= length <= 400:
        score += min(length / 80, 3.0)
    elif length > 400:
        score += 2.0  # Long but not too long
    else:
        score += length / 40  # Short but not boilerplate

    # Emotional/exclamatory language
    score += text.count("!") * 0.5
    score += text.count("?") * 0.3
    # ALL CAPS words suggest strong emotion
    caps_words = [w for w in text.split() if w.isupper() and len(w) > 2]
    score += len(caps_words) * 0.2

    # Descriptive language markers
    descriptive = ["please", "help", "need", "dangerous", "terrible",
                   "awful", "beautiful", "massive", "ridiculous", "unsafe",
                   "dying", "dead", "screaming", "yelling", "scared"]
    for word in descriptive:
        if word in text.lower():
            score += 0.4

    return score


def _generate_digest(citizen_complaints: list) -> dict:
    """Generate digest by ranking citizen complaints by interestingness.

    Returns the top 25 most interesting complaints (no editorial notes).
    """
    logger.info("Generating interestingness-based digest")

    scored = [(r, _interestingness_score(r)) for r in citizen_complaints]
    scored.sort(key=lambda x: -x[1])
    top = scored[:25]

    picks = []
    for record, score in top:
        picks.append({
            "text": record["text"],
            "address": record["address"],
            "category": record["category"],
            "ticketId": record["id"],
            "date": record["date"],
            "dedupCount": record.get("_dedupCount", 1),
        })

    return {
        "headline": f"{len(picks)} notable citizen-written complaints this week in Austin",
        "picks": picks,
    }


# ── Homepage ticker output ──────────────────────────────────────────────────

def _write_ticker_json(citizen_deduped: list, digest_picks: list, headline: str) -> None:
    """Write docs/homepage/ticker.json for the homepage carousel.

    Combines top-ranked picks (raw text only) with interestingness-ranked
    citizen complaints to produce ~40 items.
    """
    now = _utc_now()
    seen_ids: set = set()
    ticker_items: list = []

    # 1. LLM-curated picks first (raw text only, no editorial notes)
    for pick in digest_picks:
        tid = pick.get("ticketId", "")
        if tid and tid not in seen_ids:
            seen_ids.add(tid)
            ticker_items.append({
                "text": pick.get("text", ""),
                "category": pick.get("category", ""),
                "address": pick.get("address", ""),
                "date": pick.get("date", ""),
                "id": tid,
            })

    # 2. Fill remaining slots (up to 40) from interestingness-ranked citizen complaints
    scored = [(r, _interestingness_score(r)) for r in citizen_deduped]
    scored.sort(key=lambda x: -x[1])

    for record, score in scored:
        if len(ticker_items) >= 40:
            break
        rid = record.get("id", "")
        if rid and rid not in seen_ids:
            seen_ids.add(rid)
            ticker_items.append({
                "text": record.get("text", ""),
                "category": record.get("category", ""),
                "address": record.get("address", ""),
                "date": record.get("date", ""),
                "id": rid,
            })

    ticker_data = {
        "headline": headline,
        "generated": now.isoformat(),
        "items": ticker_items,
    }

    out_path = (
        Path(__file__).resolve().parent.parent / "docs" / "homepage" / "ticker.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(ticker_data, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    logger.info(
        f"Ticker: wrote {len(ticker_items)} items to {out_path} "
        f"({out_path.stat().st_size:,} bytes)"
    )


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    now = _utc_now()
    logger.info(f"=== Hidden Austin digest — {now.isoformat()} ===")

    # Phase 1: Fetch descriptions
    records = fetch_weekly_descriptions()

    citizen_deduped = []  # Track for ticker output

    if not records:
        logger.warning("No descriptions found for this week")
        digest = {
            "headline": "No 311 complaints found this week — quiet in Austin!",
            "picks": [],
            "citizenComplaints": [],
            "cityResponses": [],
        }
    else:
        # Phase 2: Classify
        citizen_raw, city_responses = _classify_and_filter(records)

        # Phase 3: Deduplicate citizen complaints
        citizen_deduped = _deduplicate_citizen(citizen_raw)

        # Remove internal dedup count for the browse list
        citizen_complaints = []
        for r in citizen_deduped:
            c = {k: v for k, v in r.items() if not k.startswith("_")}
            c["notable"] = True
            citizen_complaints.append(c)

        # Phase 4: Curation by interestingness heuristic
        digest = _generate_digest(citizen_deduped)

        digest["citizenComplaints"] = citizen_complaints
        digest["cityResponses"] = city_responses

    # Metadata
    digest["_meta"] = {
        "generated": now.isoformat(),
        "totalDescriptions": len(records),
        "citizenWritten": len(digest.get("citizenComplaints", [])),
        "cityResponses": len(digest.get("cityResponses", [])),
        "source": "Open311 (29 service codes, last 7 days)",
    }

    # Write to docs/complaints/digest.json
    out_path = Path(__file__).resolve().parent.parent / "docs" / "complaints" / "digest.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(digest, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info(f"Wrote {out_path.stat().st_size:,} bytes to {out_path}")

    # Write homepage ticker (raw descriptions, no editorial notes)
    if records:
        digest_picks = digest.get("picks", [])
        headline = digest.get("headline", "This week in Austin 311")
        _write_ticker_json(citizen_deduped, digest_picks, headline)


if __name__ == "__main__":
    main()
