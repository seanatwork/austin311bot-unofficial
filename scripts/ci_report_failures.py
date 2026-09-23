#!/usr/bin/env python3
"""Report (and fail on) generator steps that failed but were allowed to continue.

Every generator step in the data-refresh workflows sets ``continue-on-error: true``
plus a unique ``id``, so a single flaky data source (Open311 403s, a Socrata
outage, a rate limit) can no longer discard the work of the other twenty steps.
A step that fails under ``continue-on-error`` is recorded with
``outcome == "failure"`` while the job itself stays green.

This script runs last (after the commit step) and turns those outcomes into a
job summary plus a non-zero exit code, so the run still goes red — *after* the
data that did generate has been committed.

Usage (CI only)::

    env STEP_RESULTS='${{ toJSON(steps) }}' python scripts/ci_report_failures.py
"""
from __future__ import annotations

import json
import os
import sys


def main() -> int:
    raw = (os.environ.get("STEP_RESULTS") or "").strip()
    if not raw:
        print("STEP_RESULTS is empty — nothing to report.", file=sys.stderr)
        return 0

    try:
        steps = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"Could not parse STEP_RESULTS ({exc}) — skipping failure report.", file=sys.stderr)
        return 0

    failed = sorted(
        str(step_id)
        for step_id, info in steps.items()
        if isinstance(info, dict) and info.get("outcome") == "failure"
    )

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if failed:
        lines = [
            "## ⚠️ Generators that failed",
            "",
            f"{len(failed)} step(s) failed and were skipped. Everything else was",
            "generated and committed as usual.",
            "",
        ]
        lines += [f"- `{step_id}`" for step_id in failed]
        text = "\n".join(lines) + "\n"
        print(text)
        if summary_path:
            with open(summary_path, "a", encoding="utf-8") as fh:
                fh.write(text + "\n")
        return 1

    print("✅ All generator steps succeeded.")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write("## ✅ All generator steps succeeded\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
