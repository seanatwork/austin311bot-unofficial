#!/usr/bin/env python3
"""
Generate docs/complaints/digest.json — "Hidden Austin" curated weekly digest.

Fetches all descriptions from 29 Open311 service codes (past 7 days), separates
citizen-written complaints from city boilerplate responses, deduplicates near-duplicates,
then uses OpenRouter to curate 10-15 "Editor's Picks" with editorial commentary.

Run:  OPENROUTER_API_KEY=... python scripts/generate_weekly_digest.py
Output: docs/complaints/digest.json

Fallback: If OpenRouter is unavailable, surfaces the top 25 citizen-written
complaints ranked by an interestingness heuristic.
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
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

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


def _get_openrouter_key() -> Optional[str]:
    return os.getenv("OPENROUTER_API_KEY") or None


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


# ── Phase 4a: LLM-powered curation (OpenRouter) ──────────────────────────────

def _build_prompt(records: list) -> tuple:
    """Build the system + user prompt for the LLM curation."""
    system = """You are the editor of "Hidden Austin," a weekly column that surfaces the most
surprising, funny, infuriating, and uniquely-Austin 311 citizen complaints.

Your job: select 10-15 individual complaints from the list below and, for each one,
write a 1-2 sentence editorial note saying what's interesting about the complaint.

CRITICAL RULE — NEVER fabricate or embellish facts. You must:
- Only describe details that are EXPLICITLY present in the complaint text
- NEVER add specific numbers (floor counts, decibel levels, distances, prices, etc.) unless they appear verbatim in the complaint
- NEVER invent quotes or claims the complainant didn't write
- NEVER speculate about things not in the complaint text
- If you're not sure whether a detail is in the text, DON'T include it
- Keep it short: 1-2 sentences max, 200 characters max

What makes a complaint worth picking:
- Emotional language (frustration, humor, disbelief)
- Unexpected situations or creative descriptions
- Complaints that reflect something about Austin life
- Complaints about well-known venues or neighborhoods

Return ONLY valid JSON (no markdown fences, no commentary) with this structure:
{
  "headline": "A conversational one-sentence summary capturing the mood of this week's picks",
  "picks": [
    {
      "ticketId": "the id field from the complaint",
      "editorialNote": "1-2 sentence note on what's interesting, using ONLY facts from the complaint text. No fabricated details, no embellishment."
    }
  ]
}

Do NOT include the complaint text in your response — we already have it.
Only include ticketId (to match back) and editorialNote."""

    # Build compact description list
    desc_lines = []
    for r in records:
        desc_lines.append(json.dumps({
            "id": r["id"],
            "cat": r["category"],
            "addr": r["address"],
            "date": r["date"],
            "text": r["text"],
        }, ensure_ascii=False))

    user = f"Here are {len(records)} citizen-written 311 complaints from Austin this week (past 7 days):\n\n"
    user += "[\n" + ",\n".join(desc_lines) + "\n]\n\n"
    user += "Select 10-15 of the most interesting ones. For each, provide "
    user += "the ticketId and your editorial note. Return ONLY valid JSON."

    return system, user


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token."""
    return len(text) // 4


def _call_llm(records: list) -> Optional[dict]:
    """Send citizen-written descriptions to OpenRouter and parse the curated picks.

    If the payload is too large for the model's context, trims to fit.
    """
    api_key = _get_openrouter_key()
    if not api_key:
        logger.warning("OPENROUTER_API_KEY not set — skipping LLM curation")
        return None

    system, user = _build_prompt(records)
    estimated_tokens = _estimate_tokens(f"{system}\n{user}")

    logger.info(f"LLM prompt: ~{estimated_tokens:,} estimated tokens ({len(records)} descriptions)")

    # Trim if needed
    total_chars = len(system) + len(user)
    if total_chars > 500_000:
        logger.warning(f"Payload too large ({total_chars:,} chars), trimming")
        keep = max(1, len(records) * 400_000 // total_chars)
        # Keep newest first
        trimmed = sorted(records, key=lambda r: r["date"], reverse=True)[:keep]
        system, user = _build_prompt(trimmed)
        estimated_tokens = _estimate_tokens(f"{system}\n{user}")
        logger.info(f"  Trimmed to {len(trimmed)} records (~{estimated_tokens:,} tokens)")

    payload = {
        "model": "openrouter/free",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.7,
        "max_tokens": 4096,
    }

    session = _get_session()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://austin311.com",
        "X-Title": "Hidden Austin Weekly Digest",
    }

    try:
        logger.info("Calling OpenRouter…")
        resp = session.post(OPENROUTER_URL, headers=headers, json=payload, timeout=180)
        resp.raise_for_status()
        data = resp.json()

        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        if not content:
            logger.warning("OpenRouter returned empty response")
            return None

        # Strip markdown fences if present
        content = content.strip()
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*", "", content)
            content = re.sub(r"\s*```$", "", content)

        result = json.loads(content)

        if not isinstance(result, dict) or "picks" not in result:
            logger.warning(f"LLM response missing 'picks' key: {list(result.keys())[:5]}")
            return None

        logger.info(
            f"LLM curated {len(result.get('picks', []))} picks. "
            f"Headline: {result.get('headline', '')[:80]}"
        )
        return result

    except requests.exceptions.HTTPError as e:
        logger.warning(f"OpenRouter HTTP error: {e.response.status_code} {e.response.text[:200]}")
        return None
    except json.JSONDecodeError as e:
        logger.warning(f"Failed to parse LLM JSON response: {e}")
        return None
    except Exception as e:
        logger.warning(f"OpenRouter call failed: {e}")
        return None


def _merge_picks_with_records(picks: list, records: list) -> list:
    """Match LLM picks back to full record data by ticketId.

    Each pick from the LLM has {ticketId, editorialNote}.
    We match them to the full record to get text, address, category, date.
    """
    record_map = {r["id"]: r for r in records}
    merged = []
    for pick in picks:
        tid = pick.get("ticketId", "")
        record = record_map.get(tid)
        if record:
            note = pick.get("editorialNote", "")
            validated = _validate_editorial_note(note, record["text"])
            # If LLM note was fabricated, use the simple factual fallback
            if not validated and note:
                logger.info(f"  Using fallback note for {tid}")
                validated = _fallback_note(record)
            merged.append({
                "text": record["text"],
                "editorialNote": validated,
                "address": record["address"],
                "category": record["category"],
                "ticketId": tid,
                "date": record["date"],
                "dedupCount": record.get("_dedupCount", 1),
            })
        else:
            logger.warning(f"  Pick ticketId '{tid}' not found in records — skipping")
    return merged


def _validate_editorial_note(note: str, complaint_text: str) -> str:
    """Strip fabricated details from editorial notes.

    If the note contains specific numbers (floor counts, decibel levels,
    dollar amounts, distances, years) not found in the complaint text,
    or exceeds 300 chars, falls back to a simple factual note.
    """
    if not note:
        return ""

    # Cap length — no 4-sentence essays
    if len(note) > 300:
        logger.warning(f"  Editorial note too long ({len(note)} chars), truncating")
        note = note[:297] + "..."

    # Check for fabricated numbers: extract all digits from note and complaint
    note_numbers = set(re.findall(r'\b\d+', note))
    complaint_numbers = set(re.findall(r'\b\d+', complaint_text))

    # Numbers like 1 (a/an/one) and common small numbers are fine
    harmless = {"0", "1", "2", "3", "4", "5", "1st", "2nd", "3rd"}
    note_numbers -= harmless

    fabricated = note_numbers - complaint_numbers
    if fabricated:
        logger.warning(
            f"  Editorial note contains numbers not in complaint: "
            f"{sorted(fabricated, key=int) if all(n.isdigit() for n in fabricated) else sorted(fabricated)} "
            f"— reverting to factual note"
        )
        return ""

    return note


def _fallback_note(record: dict) -> str:
    """Generate a simple, factual editorial note for a pick."""
    cat = record.get("category", "")
    addr = (record.get("address", "") or "").split(",")[0]
    date = record.get("date", "")
    dedup = record.get("_dedupCount", 1)
    parts = [f"Filed in the {cat} category on {date} near {addr}."] if cat and date and addr else []
    if dedup > 1:
        parts.append(f" Similar complaint(s) filed {dedup - 1} other time(s) this week.")
    return " ".join(parts)


# ── Phase 4b: Interestingness-based fallback ────────────────────────────────

def _interestingness_score(record: dict) -> float:
    """Heuristic score for ranking citizen complaints when LLM is unavailable.

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


def _generate_fallback_digest(citizen_complaints: list, city_responses: list) -> dict:
    """Generate a fallback digest by ranking citizen complaints by interestingness.

    Returns the top 25 with simple auto-generated editorial notes.
    """
    logger.info("Generating interestingness-based fallback digest")

    scored = [(r, _interestingness_score(r)) for r in citizen_complaints]
    scored.sort(key=lambda x: -x[1])
    top = scored[:25]

    picks = []
    for record, score in top:
        picks.append({
            "text": record["text"],
            "editorialNote": _fallback_note(record),
            "address": record["address"],
            "category": record["category"],
            "ticketId": record["id"],
            "date": record["date"],
            "dedupCount": record.get("_dedupCount", 1),
        })

    return {
        "headline": f"Top {len(picks)} citizen complaints this week in Austin",
        "picks": picks,
        "_fallback": True,
    }


# ── Homepage ticker output ──────────────────────────────────────────────────

def _write_ticker_json(citizen_deduped: list, digest_picks: list, headline: str) -> None:
    """Write docs/homepage/ticker.json for the homepage carousel.

    Combines LLM-curated picks (stripped of editorial notes — raw text only)
    with interestingness-ranked citizen complaints to produce ~40 items.
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

        # Phase 4: LLM curation or fallback
        llm_result = _call_llm(citizen_deduped)
        if llm_result and llm_result.get("picks"):
            picks = _merge_picks_with_records(llm_result["picks"], citizen_deduped)
            digest = {
                "headline": llm_result.get("headline", "This week in Austin 311"),
                "picks": picks,
            }
            logger.info(f"LLM: {len(picks)} picks merged from LLM response")
        else:
            logger.info("LLM unavailable or returned no picks, using fallback")
            digest = _generate_fallback_digest(citizen_deduped, city_responses)

        digest["citizenComplaints"] = citizen_complaints
        digest["cityResponses"] = city_responses

    # Metadata
    digest["_meta"] = {
        "generated": now.isoformat(),
        "totalDescriptions": len(records),
        "citizenWritten": len(digest.get("citizenComplaints", [])),
        "cityResponses": len(digest.get("cityResponses", [])),
        "source": "Open311 (29 service codes, last 7 days)",
        "fallback": digest.get("_fallback", False),
    }
    # Clean up top-level fallback flag — it's in _meta now
    digest.pop("_fallback", None)

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
