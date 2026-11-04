#!/usr/bin/env python3
"""Fail when the worker's mirrored taxonomy drifts from categories.py.

`categories.py` is the canonical reporting taxonomy, but the Cloudflare worker
cannot import Python, so `workers/query-gateway/src/categories.js` carries a
hand-maintained copy of CATEGORY_CODES / CATEGORY_NAMES / SERVICE_CODE_NAMES,
plus the Socrata type maps and the `homeless` encampment keyword lists owned by
`homeless/homeless_bot.py`.

That copy has drifted before: the worker's Bicycle category listed five
right-of-way codes while the reporting category counts PWBICYCL only, which
silently changed what the LLM was told a "bicycle" question means.

This compares the two — keys, per-category code sets, labels, service-code
display names, Socrata types and encampment keywords — and exits non-zero on
any difference. It reads both files without importing them (stdlib `ast` for
Python) so it works in the worker deploy job, which never installs the Python
dependencies.

Usage:
    python scripts/check_taxonomy_sync.py [--root PATH]

Exit codes: 0 in sync · 1 drift · 2 could not verify (node unavailable in CI).
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import subprocess
import sys
from pathlib import Path

PY_TAXONOMY = "categories.py"
PY_SERVICE_NAMES = Path("scripts") / "generate_query_data.py"
PY_HOMELESS_FILTER = Path("homeless") / "homeless_bot.py"
JS_TAXONOMY = Path("workers") / "query-gateway" / "src" / "categories.js"

_JS_SCRIPT = """
const m = await import(process.env.TAXONOMY_FILE);
process.stdout.write(JSON.stringify({
  codes: m.CATEGORY_CODES,
  names: m.CATEGORY_NAMES,
  service: m.SERVICE_CODE_NAMES,
  socrata: m.CATEGORY_SOCRATA_TYPES,
  socrataExclude: m.CATEGORY_SOCRATA_EXCLUDE,
  encampment: m.ENCAMPMENT_KEYWORDS,
  hso: m.HSO_KEYWORDS,
  trash: m.TRASH_KEYWORDS,
}));
"""


def _literals(path: Path, wanted: set[str]) -> dict:
    """Evaluate the module-level literals named in `wanted`, without importing."""
    found: dict = {}
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        # Handle both `X = {...}` and the annotated `X: Dict[str, str] = {...}`.
        if isinstance(node, ast.Assign):
            targets = [t for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target]
        else:
            continue
        for target in targets:
            if target.id in wanted:
                found[target.id] = ast.literal_eval(node.value)
    missing = wanted - set(found)
    if missing:
        raise SystemExit(f"{path}: could not find {', '.join(sorted(missing))}")
    return found


def load_python(root: Path) -> dict:
    tax = _literals(
        root / PY_TAXONOMY,
        {"CATEGORY_CODES", "CATEGORY_NAMES", "CATEGORY_SOCRATA_TYPES", "CATEGORY_SOCRATA_EXCLUDE"},
    )
    names = _literals(root / PY_SERVICE_NAMES, {"SERVICE_CODE_NAMES"})
    homeless = _literals(
        root / PY_HOMELESS_FILTER,
        {"ENCAMPMENT_KEYWORDS", "HSO_KEYWORDS", "TRASH_KEYWORDS"},
    )
    return {**tax, "service": names["SERVICE_CODE_NAMES"], **homeless}


def load_worker(root: Path) -> dict:
    js_path = root / JS_TAXONOMY
    env = dict(os.environ, TAXONOMY_FILE=js_path.resolve().as_uri())
    try:
        proc = subprocess.run(
            ["node", "--input-type=module", "-e", _JS_SCRIPT],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
    except FileNotFoundError:
        print("note: node is not installed, cannot read the worker taxonomy", file=sys.stderr)
        if os.environ.get("CI"):
            raise SystemExit("cannot verify taxonomy sync in CI without node")
        raise SystemExit(0)
    if proc.returncode != 0:
        raise SystemExit(f"could not load {js_path}:\n{proc.stderr.strip()}")
    data = json.loads(proc.stdout)
    return {
        "codes": data["codes"],
        "names": data["names"],
        "service": data["service"],
        "CATEGORY_SOCRATA_TYPES": data["socrata"],
        "CATEGORY_SOCRATA_EXCLUDE": data["socrataExclude"],
        "ENCAMPMENT_KEYWORDS": data["encampment"],
        "HSO_KEYWORDS": data["hso"],
        "TRASH_KEYWORDS": data["trash"],
    }


def diff(py: dict, js: dict) -> list[str]:
    py_codes, py_names, py_service = py["CATEGORY_CODES"], py["CATEGORY_NAMES"], py["service"]
    js_codes, js_names, js_service = js["codes"], js["names"], js["service"]
    problems: list[str] = []

    # The taxonomy has to be internally consistent before comparing it.
    if set(py_codes) != set(py_names):
        problems.append(
            f"categories.py: CATEGORY_NAMES keys differ from CATEGORY_CODES "
            f"({sorted(set(py_codes) ^ set(py_names))})"
        )

    if set(py_codes) != set(js_codes):
        problems.append(
            "category keys differ — "
            f"python only: {sorted(set(py_codes) - set(js_codes))}, "
            f"worker only: {sorted(set(js_codes) - set(py_codes))}"
        )

    for cat in sorted(set(py_codes) & set(js_codes)):
        a, b = set(py_codes[cat]), set(js_codes[cat])
        if a != b:
            problems.append(
                f"{cat}: code list differs — "
                f"python only: {sorted(a - b)}, worker only: {sorted(b - a)}"
            )

    for cat in sorted(set(py_names) & set(js_names)):
        if py_names[cat] != js_names[cat]:
            problems.append(
                f"{cat}: label differs — python {py_names[cat]!r} vs worker {js_names[cat]!r}"
            )

    for cat, codes in js_codes.items():
        for code in codes:
            if code not in js_service:
                problems.append(f"{cat}: worker has no SERVICE_CODE_NAMES entry for {code}")

    for code in sorted(set(py_service) & set(js_service)):
        if py_service[code] != js_service[code]:
            problems.append(
                f"{code}: display name differs — python {py_service[code]!r} vs worker {js_service[code]!r}"
            )

    problems += _diff_socrata(py, js)

    return problems


def _diff_socrata(py: dict, js: dict) -> list[str]:
    """Compare the Socrata type mappings the query gateway queries with.

    Two different keys, so two different invariants:
      * `homeless` must never have Socrata types — the category is a keyword
        filter over text Socrata does not publish.
      * So the other side, every *other* reporting category must have them,
        otherwise a "how many X" question silently falls back to the slower
        Open311 path (or, worse, answers with nothing).
    Socrata-only categories (trash, code_violations, abandoned_vehicles) are
    allowed on top; they have no Open311 codes by design.
    """
    problems: list[str] = []
    py_types = py["CATEGORY_SOCRATA_TYPES"]
    js_types = js["CATEGORY_SOCRATA_TYPES"]

    if "homeless" in py_types or "homeless" in js_types:
        problems.append(
            "homeless must not have Socrata types — the category needs "
            "description/status_notes, which xwdj-i9he does not publish"
        )

    missing = set(py["CATEGORY_CODES"]) - set(py_types) - {"homeless"}
    if missing:
        problems.append(
            f"categories.py: no Socrata types for {sorted(missing)} — those "
            "questions cannot use the fast path"
        )

    if set(py_types) != set(js_types):
        problems.append(
            "Socrata category keys differ — "
            f"python only: {sorted(set(py_types) - set(js_types))}, "
            f"worker only: {sorted(set(js_types) - set(py_types))}"
        )

    for cat in sorted(set(py_types) & set(js_types)):
        a, b = set(py_types[cat]), set(js_types[cat])
        if a != b:
            problems.append(
                f"{cat}: Socrata type list differs — "
                f"python only: {sorted(a - b)}, worker only: {sorted(b - a)}"
            )

    py_ex = py["CATEGORY_SOCRATA_EXCLUDE"]
    js_ex = js["CATEGORY_SOCRATA_EXCLUDE"]
    if set(py_ex) != set(js_ex):
        problems.append(
            f"Socrata exclusion keys differ — python {sorted(py_ex)} vs worker {sorted(js_ex)}"
        )
    for cat in sorted(set(py_ex) & set(js_ex)):
        a, b = set(py_ex[cat]), set(js_ex[cat])
        if a != b:
            problems.append(
                f"{cat}: Socrata exclusion list differs — "
                f"python only: {sorted(a - b)}, worker only: {sorted(b - a)}"
            )
    for cat in sorted(set(py_ex) & set(py_types)):
        if not py_ex[cat]:
            problems.append(f"{cat}: empty Socrata exclusion list")

    problems += _diff_homeless_keywords(py, js)

    return problems


def _diff_homeless_keywords(py: dict, js: dict) -> list[str]:
    """Compare the encampment keyword lists.

    `homeless` is a keyword filter over ticket text, so the worker reimplements
    `is_encampment_report` to show ticket samples. A keyword added to one side
    only would make the same report homeless on the site and not in `/ask`.
    """
    problems: list[str] = []
    for name in ("ENCAMPMENT_KEYWORDS", "HSO_KEYWORDS", "TRASH_KEYWORDS"):
        a, b = set(py[name]), set(js[name])
        if a != b:
            problems.append(
                f"{name}: keyword list differs — "
                f"python only: {sorted(a - b)}, worker only: {sorted(b - a)}"
            )
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args()

    py = load_python(args.root)
    js = load_worker(args.root)
    problems = diff(py, js)

    if problems:
        print("❌ Worker taxonomy has drifted from the Python source:\n")
        for p in problems:
            print(f"  - {p}")
        print(
            f"\nFix {JS_TAXONOMY} (or the Python side — {PY_TAXONOMY} / "
            f"{PY_HOMELESS_FILTER} — if it is wrong)."
        )
        return 1

    total = sum(len(c) for c in py["CATEGORY_CODES"].values())
    socrata = sum(len(c) for c in py["CATEGORY_SOCRATA_TYPES"].values())
    print(
        f"✅ taxonomy in sync: {len(py['CATEGORY_CODES'])} categories, {total} codes, "
        f"{socrata} Socrata types"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
