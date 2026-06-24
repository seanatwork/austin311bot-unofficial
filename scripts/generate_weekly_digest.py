#!/usr/bin/env python3
"""
Generate docs/complaints/digest.json — LLM-powered weekly digest of 311 descriptions.

Fetches ALL descriptions from 29 description-rich Open311 service codes (past 7 days),
sends them to a free LLM via OpenRouter for summarization, and saves the result.

The digest is surfaced as a "📊 This Week" chip on the existing complaints page.

Run:  OPENROUTER_API_KEY=... python scripts/generate_weekly_digest.py
Output: docs/complaints/digest.json

Fallback: If OpenRouter is unavailable, generates a keyword-based digest.
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
# 25 proven codes from generate_fun_data.py's _FUNNY_CODES
# + 4 promising extras (bicycle, dead animal, animal protection, construction)
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
    # Promising extras
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
            "User-Agent": "austin311bot/weekly-digest (pre-generator)",
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


# ── Profanity censor (for fallback keyword-based digest) ──────────────────────

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
    total_fetched = 0

    logger.info(f"Fetching descriptions from {week_ago:%Y-%m-%d} to {now:%Y-%m-%d}")
    logger.info(f"Scanning {len(DIGEST_CODES)} service codes…")

    for code, label in DIGEST_CODES:
        code_records = 0
        page = 1
        while page <= 3:  # 7-day windows typically fit in 1-2 pages
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
                    # Prefer citizen-written description over city status_notes
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

        total_fetched += code_records
        if code_records:
            logger.info(f"  {code} ({label}): {code_records} descriptions")
        time.sleep(0.5)

    logger.info(f"Total: {len(all_records)} unique descriptions across {len(DIGEST_CODES)} codes")
    return all_records


# ── Phase 2a: LLM-powered digest (OpenRouter) ────────────────────────────────

def _build_prompt(records: list) -> str:
    """Build the system + user prompt for the LLM digest."""
    system = """You are an analyst for Austin 311, the city's non-emergency service request system.
Analyze the citizen complaint descriptions provided and return a JSON object with these keys:
- "weeklyHeadline": a one-sentence summary of the week in 311 (conversational, human)
- "themes": array of objects, each with {"theme": string, "count": int, "examples": [string]}. Top complaint themes this week.
- "emergingIssues": array of strings. Anything trending up, unusual, or notable that wasn't common before.
- "mostUnusualComplaint": object with {"text": string, "address": string, "category": string, "ticketId": string}. The weirdest, funniest, or most uniquely-Austin complaint.
- "mentionedVenues": array of strings. Business names, venue names, bar names, restaurant names, apartment complexes extracted from descriptions.
- "sentimentNote": one-sentence observation about the overall tone this week (angrier? more patient? any pattern?)
- "topComplaintTypes": array of objects, each with {"type": string, "count": int}. Fine-grained sub-types (e.g. "Loud Music/Party", "Barking Dog", "Blocked Driveway")

Keep descriptions concise and readable. If a description is too long, summarize the key point.
Return ONLY valid JSON. No markdown fences, no commentary."""

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

    user = f"Here are {len(records)} citizen 311 complaints from Austin this week (past 7 days):\n\n"
    user += "[\n" + ",\n".join(desc_lines) + "\n]"

    return system, user


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token."""
    return len(text) // 4


def _call_llm(records: list) -> Optional[dict]:
    """Send descriptions to OpenRouter free model and parse JSON response.

    If the payload is too large for the model's context, trims to fit.
    """
    api_key = _get_openrouter_key()
    if not api_key:
        logger.warning("OPENROUTER_API_KEY not set — skipping LLM digest")
        return None

    system, user = _build_prompt(records)
    total_chars = len(system) + len(user)
    estimated_tokens = _estimate_tokens(f"{system}\n{user}")

    logger.info(f"LLM prompt: ~{estimated_tokens:,} estimated tokens ({len(records)} descriptions)")

    # If payload seems too large for most free models (> 500K chars), trim
    if total_chars > 500_000:
        logger.warning(f"Payload too large ({total_chars:,} chars), trimming to newest records")
        # Rebuild with fewer records — keep newest first
        trimmed = records[:max(1, len(records) * 400_000 // total_chars)]
        system, user = _build_prompt(trimmed)
        total_chars = len(system) + len(user)
        estimated_tokens = _estimate_tokens(f"{system}\n{user}")
        logger.info(f"  Trimmed to {len(trimmed)} records (~{estimated_tokens:,} tokens)")

    payload = {
        "model": "openrouter/free",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.3,
        "max_tokens": 4096,
    }

    session = _get_session()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://austin311.com",
        "X-Title": "Austin 311 Weekly Digest",
    }

    try:
        logger.info("Calling OpenRouter…")
        resp = session.post(OPENROUTER_URL, headers=headers, json=payload, timeout=120)
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

        # Validate minimal structure
        if not isinstance(result, dict) or "themes" not in result:
            logger.warning(f"LLM response missing expected keys: {list(result.keys())[:5]}")
            return None

        logger.info(f"LLM digest generated: {result.get('weeklyHeadline', 'no headline')[:80]}")
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


# ── Phase 2b: Keyword-based fallback digest ──────────────────────────────────

_FALLBACK_KEYWORDS = {
    "🔊 Noise/Music": ["loud music", "party", "dj", "live band", "karaoke", "blasting",
                       "noise", "loud", "boom", "vibrating", "shaking"],
    "🐕 Animals": ["loose dog", "barking", "vicious", "biting", "coyote", "rabid",
                   "dead animal", "stray"],
    "🚗 Parking": ["parking", "blocking", "driveway", "fire hydrant", "handicap",
                   "bike lane", "sidewalk"],
    "🏚️ Homeless/Encampment": ["camp", "tent", "homeless", "encampment", "vagrant",
                                "transient", "shack"],
    "🚮 Trash/Debris": ["trash", "debris", "dumping", "garbage", "litter"],
    "🏗️ Construction": ["construction", "jackhammer", "drilling", "detour"],
    "🚦 Traffic/Infrastructure": ["pothole", "street light", "signal", "sign",
                                   "street sweeping", "sidewalk"],
    "🌊 Storm/Flooding": ["flood", "drain", "storm", "erosion", "standing water"],
    "🎆 Fireworks": ["firework", "firecracker"],
}


def _generate_fallback_digest(records: list) -> dict:
    """Generate a keyword-based digest when the LLM is unavailable."""
    logger.info("Generating keyword-based fallback digest")

    # Count by theme
    theme_counts: dict = {}
    theme_examples: dict = {}
    for r in records:
        text = r["text"].lower()
        matched = False
        for theme, keywords in _FALLBACK_KEYWORDS.items():
            for kw in keywords:
                if kw in text:
                    theme_counts[theme] = theme_counts.get(theme, 0) + 1
                    if theme not in theme_examples:
                        theme_examples[theme] = []
                    if len(theme_examples[theme]) < 3:
                        theme_examples[theme].append(r["text"][:120])
                    matched = True
                    break
            if matched:
                break

    themes = sorted(theme_counts.items(), key=lambda x: -x[1])
    total = len(records)

    # Pick most unusual (citizen-written text, not city boilerplate)
    _BOILERPLATE = ["close sr", "job#", "resolved", "no issue found", "inspection performed",
                     "no action needed", "future work scheduled", "referred to",
                     "citation issued", "contact made", "unable to locate", "utl"]
    def _is_citizen_text(t):
        t_lower = t.lower()
        return len(t) > 80 and not any(b in t_lower for b in _BOILERPLATE)

    unusual_candidates = [r for r in records if _is_citizen_text(r["text"])]
    if not unusual_candidates:
        unusual_candidates = [r for r in records if len(r["text"]) > 80]
    import random
    random.shuffle(unusual_candidates)
    most_unusual = unusual_candidates[0] if unusual_candidates else (records[0] if records else {})

    return {
        "weeklyHeadline": f"{total} notable complaints this week across {len(themes)} categories.",
        "themes": [
            {"theme": t, "count": c, "examples": theme_examples.get(t, [])}
            for t, c in themes[:6]
        ],
        "emergingIssues": [
            t for t, c in themes[:3]
        ] if themes else ["Not enough data"],
        "mostUnusualComplaint": {
            "text": most_unusual.get("text", ""),
            "address": most_unusual.get("address", ""),
            "category": most_unusual.get("category", ""),
            "ticketId": most_unusual.get("id", ""),
        } if most_unusual else None,
        "mentionedVenues": [],
        "sentimentNote": f"Keyword analysis of {total} descriptions across {len(themes)} themes.",
        "topComplaintTypes": [
            {"type": t, "count": c}
            for t, c in themes[:8]
        ],
        "_fallback": True,
    }


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    now = _utc_now()
    logger.info(f"Generating weekly digest at {now.isoformat()}")

    # Phase 1: Fetch descriptions
    records = fetch_weekly_descriptions()
    if not records:
        logger.warning("No descriptions found for this week")
        digest = {
            "weeklyHeadline": "No 311 complaints found this week — quiet in Austin!",
            "themes": [],
            "emergingIssues": [],
            "mostUnusualComplaint": None,
            "mentionedVenues": [],
            "sentimentNote": "No data available for this period.",
            "topComplaintTypes": [],
        }
    else:
        # Phase 2: Try LLM, fall back to keyword-based
        digest = _call_llm(records)
        if digest is None:
            logger.info("LLM unavailable, using keyword fallback")
            digest = _generate_fallback_digest(records)

    # Add metadata
    digest["_generated"] = now.isoformat()
    digest["_totalDescriptions"] = len(records)
    digest["_source"] = "Open311 (29 service codes, last 7 days)"

    # Write to docs/complaints/digest.json
    out_path = Path(__file__).resolve().parent.parent / "docs" / "complaints" / "digest.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(digest, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info(f"Wrote {out_path.stat().st_size:,} bytes to {out_path}")


if __name__ == "__main__":
    main()
