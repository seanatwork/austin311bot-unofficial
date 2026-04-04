"""
Graffiti Remediation Analysis — live Open311 API queries.
Replaces the old SQLite-backed implementation.
"""

import logging
from datetime import datetime, timezone, timedelta

from .graffiti_bot import _fetch_graffiti, _utc_now

logger = logging.getLogger(__name__)


# =============================================================================
# REMEDIATION COMMAND
# =============================================================================

def remediation_command(days_back: int = 90) -> str:
    try:
        records = _fetch_graffiti(days_back)
    except Exception as e:
        logger.error(f"graffiti remediation fetch: {e}")
        return f"❌ Could not fetch graffiti data: {e}"

    if not records:
        return f"📝 No graffiti reports found in the last {days_back} days."

    total = len(records)
    now = _utc_now()
    repair_days = []
    open_waiting = []  # (days_waiting, address) for still-open tickets

    for r in records:
        status = (r.get("status") or "").lower()
        requested_str = r.get("requested_datetime") or ""
        updated_str = r.get("updated_datetime") or ""
        addr = (r.get("address") or "Unknown").replace(", Austin", "").strip()

        if not requested_str:
            continue

        try:
            req = datetime.fromisoformat(requested_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue

        if status != "closed":
            days_waiting = (now - req).days
            if 0 <= days_waiting <= 365:
                open_waiting.append((days_waiting, addr))
            continue

        if not updated_str:
            continue
        try:
            upd = datetime.fromisoformat(updated_str.replace("Z", "+00:00"))
            d = (upd - req).days
            if 0 <= d <= 365:
                repair_days.append(d)
        except (ValueError, TypeError):
            pass

    open_count = len(open_waiting)

    if not repair_days:
        return (
            f"📝 No closed graffiti reports found in the last {days_back} days.\n"
            f"_({open_count} currently open)_"
        )

    repair_days.sort()
    avg = round(sum(repair_days) / len(repair_days), 1)
    median = repair_days[len(repair_days) // 2]
    fastest = repair_days[0]
    closed_count = len(repair_days)

    buckets = {"Same day": 0, "1–3 days": 0, "4–7 days": 0, "8–14 days": 0, "15+ days": 0}
    for d in repair_days:
        if d == 0:
            buckets["Same day"] += 1
        elif d <= 3:
            buckets["1–3 days"] += 1
        elif d <= 7:
            buckets["4–7 days"] += 1
        elif d <= 14:
            buckets["8–14 days"] += 1
        else:
            buckets["15+ days"] += 1

    if avg <= 4:
        verdict = "🟢 Austin is cleaning graffiti quickly"
    elif avg <= 10:
        verdict = "🟡 Cleanup times are moderate"
    else:
        verdict = "🔴 Cleanup is running slow"

    msg = f"🎨 *Graffiti Remediation — Last {days_back} Days*\n"
    msg += f"_{total} total · {closed_count} closed · {open_count} open_\n\n"
    msg += f"{verdict}\n\n"
    msg += f"⏱ *Avg cleanup time:* {avg} days\n"
    msg += f"📊 *Median:* {median} days  ·  *Fastest:* {fastest} day{'s' if fastest != 1 else ''}\n\n"

    msg += "*How long cleanups took:*\n"
    max_bucket = max(buckets.values()) or 1
    for label, count in buckets.items():
        bar = "█" * min(10, round(count / max_bucket * 10))
        pct = round(count / closed_count * 100)
        msg += f"  `{label:<12}` {bar} {count} ({pct}%)\n"

    if open_waiting:
        top_waiting = sorted(open_waiting, key=lambda x: -x[0])[:5]
        msg += "\n*Still open — longest waiting:*\n"
        for d, addr in top_waiting:
            msg += f"  🕐 {d} days — _{addr}_\n"

    msg += "\n_Source: [Austin Open311 API](https://311.austintexas.gov/open311/v2)_"
    return msg


# =============================================================================
# COMPARE COMMAND
# =============================================================================

def compare_command() -> str:
    try:
        records_90 = _fetch_graffiti(90)
        records_30 = [
            r for r in records_90
            if _days_ago(r.get("requested_datetime")) <= 30
        ]
    except Exception as e:
        logger.error(f"graffiti compare fetch: {e}")
        return f"❌ Could not fetch graffiti data: {e}"

    def _stats(records):
        days_list = []
        for r in records:
            if (r.get("status") or "").lower() != "closed":
                continue
            req_str = r.get("requested_datetime") or ""
            upd_str = r.get("updated_datetime") or ""
            if not req_str or not upd_str:
                continue
            try:
                req = datetime.fromisoformat(req_str.replace("Z", "+00:00"))
                upd = datetime.fromisoformat(upd_str.replace("Z", "+00:00"))
                d = (upd - req).days
                if 0 <= d <= 365:
                    days_list.append(d)
            except (ValueError, TypeError):
                pass
        if not days_list:
            return None
        days_list.sort()
        return {
            "closed": len(days_list),
            "avg": round(sum(days_list) / len(days_list), 1),
            "median": days_list[len(days_list) // 2],
        }

    s30 = _stats(records_30)
    s90 = _stats(records_90)

    msg = "📊 *Graffiti Remediation Comparison*\n\n"

    for label, s, total in [("Last 30 days", s30, len(records_30)), ("Last 90 days", s90, len(records_90))]:
        msg += f"*{label}* — {total} reports\n"
        if s:
            msg += f"  Closed: {s['closed']}  ·  Avg: {s['avg']}d  ·  Median: {s['median']}d\n"
        else:
            msg += "  Not enough closed data\n"
        msg += "\n"

    return msg


def _days_ago(dt_str: str) -> int:
    if not dt_str:
        return 9999
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        return (_utc_now() - dt).days
    except (ValueError, TypeError):
        return 9999
