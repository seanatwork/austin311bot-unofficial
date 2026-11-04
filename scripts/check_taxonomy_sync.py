#!/usr/bin/env python3
"""Fail when the worker's mirrored taxonomy drifts from categories.py.

`categories.py` is the canonical reporting taxonomy, but the Cloudflare worker
cannot import Python, so `workers/query-gateway/src/categories.js` carries a
hand-maintained copy of CATEGORY_CODES / CATEGORY_NAMES / SERVICE_CODE_NAMES.

That copy has drifted before: the worker's Bicycle category listed five
right-of-way codes while the reporting category counts PWBICYCL only, which
silently changed what the LLM was told a "bicycle" question means.

This compares the two — keys, per-category code sets, labels, and service-code
display names — and exits non-zero on any difference. It reads both files
without importing them (stdlib `ast` for Python) so it works in the worker
deploy job, which never installs the Python dependencies.

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
JS_TAXONOMY = Path("workers") / "query-gateway" / "src" / "categories.js"

_JS_SCRIPT = """
const m = await import(process.env.TAXONOMY_FILE);
process.stdout.write(JSON.stringify({
  codes: m.CATEGORY_CODES,
  names: m.CATEGORY_NAMES,
  service: m.SERVICE_CODE_NAMES,
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


def load_python(root: Path) -> tuple[dict, dict, dict]:
    tax = _literals(root / PY_TAXONOMY, {"CATEGORY_CODES", "CATEGORY_NAMES"})
    names = _literals(root / PY_SERVICE_NAMES, {"SERVICE_CODE_NAMES"})
    return tax["CATEGORY_CODES"], tax["CATEGORY_NAMES"], names["SERVICE_CODE_NAMES"]


def load_worker(root: Path) -> tuple[dict, dict, dict]:
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
    return data["codes"], data["names"], data["service"]


def diff(py: tuple[dict, dict, dict], js: tuple[dict, dict, dict]) -> list[str]:
    (py_codes, py_names, py_service) = py
    (js_codes, js_names, js_service) = js
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

    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args()

    py = load_python(args.root)
    js = load_worker(args.root)
    problems = diff(py, js)

    if problems:
        print("❌ Worker taxonomy has drifted from categories.py:\n")
        for p in problems:
            print(f"  - {p}")
        print(f"\nFix {JS_TAXONOMY} (or categories.py if the Python side is wrong).")
        return 1

    total = sum(len(c) for c in py[0].values())
    print(f"✅ taxonomy in sync: {len(py[0])} categories, {total} codes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
