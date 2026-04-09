#!/usr/bin/env python3
"""
Generate the encampment map HTML and write it to docs/index.html.

Run locally:
    python scripts/generate_map.py

Run in CI (GitHub Actions) with AUSTIN_APP_TOKEN set for higher rate limits.
"""
from pathlib import Path
from homeless.homeless_bot import generate_encampment_map


def main():
    buf, summary = generate_encampment_map(days_back=30)
    if not buf:
        raise RuntimeError(f"Map generation failed: {summary}")
    out = Path("docs/index.html")
    out.parent.mkdir(exist_ok=True)
    out.write_bytes(buf.getvalue())
    print(f"Written {out.stat().st_size:,} bytes to {out}")
    print(summary)


if __name__ == "__main__":
    main()
