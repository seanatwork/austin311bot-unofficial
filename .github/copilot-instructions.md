# Copilot Instructions for austin311bot-unofficial

## Project Overview

Unofficial Telegram bot + companion website (https://austin311.com) for exploring Austin 311 service data. Users interact via Telegram slash commands and inline buttons to query live Open311/Socrata APIs. The `docs/` folder is a GitHub Pages site with 41+ static map/trend pages generated daily by scripts.

## Commands

```bash
source .venv/bin/activate          # virtualenv is .venv/
pip install -e .                    # editable install
python austin311_bot.py             # run the bot
PYTHONPATH=. python scripts/generate_map.py <category>  # generate a static map
```

No test runner; `graffiti/tests/` has unit tests runnable via `python -m pytest graffiti/tests/`.

## Commit & Push Policy

- **Push immediately** after adding a feature, fixing a bug, or making any user-visible change
- **Batch** small iterative tweaks into one commit
- **Don't push** for: CLAUDE.md/docs-only changes, internal notes, config tweaks that don't affect functionality
- Use Conventional Commits: `feat:`, `fix:`, `docs:`, `chore:`

## Architecture

`austin311_bot.py` (~3,700 lines) is the main entry point. It:
1. Imports data-fetching/formatting functions from service packages
2. Registers `CommandHandler` and `CallbackQueryHandler` instances in `create_application()`
3. Uses inline button callback patterns (no `ConversationHandler` state machine, except `/report`)
4. Runs alert jobs via `job_queue.run_daily()` at 08:00 UTC

**Adding a new 311 service requires:**
1. A new package directory with a `*_bot.py` module that queries the API and returns formatted Markdown
2. Importing that module in `austin311_bot.py`
3. Adding command/callback handlers in `create_application()`
4. If it has a map: add it to `scripts/generate_map.py`'s `CATEGORY_MAPS` dict and a GitHub Actions workflow

## Service Packages

| Package | Data Source | Service Codes |
|---------|------------|---------------|
| `graffiti/` | Open311 | `HHSGRAFF` |
| `bicycle/` | Open311 | 8 codes (PWBICYCL, OBSTMIDB, SBDEBROW, ATCOCIRW, SBSIDERE, TPPECRNE, PWSIDEWL, ZZARSTSW) |
| `homeless/` | Open311 | 5 codes (PRGRDISS, OBSTMIDB, SBDEBROW, DRCHANEL, ATCOCIRW) |
| `animalsvc/` | Open311 | 7+ codes (loose dogs, bites, coyotes, dead animals, etc.) |
| `noisecomplaints/` | Open311 | `NOISECMP` |
| `parking/` | Open311 | `PARKINGV` |
| `parks/` | Open311 | Park maintenance |
| `infrastructureandtransportation/` | Open311 | Potholes, signals, sidewalks |
| `storm/` | Open311 | 8 codes (SWSSTORM, DRCHANEL, DRILID, DRFLOODG, DRSSPIPE, DRFLOODR, ZZEROSIO, DRDITCH) |
| `restaurants/` | Socrata `ecmv-9xxi` | Health inspections |
| `waterconservation/` | Socrata | Water violations |
| `childcare/` | Socrata | Childcare facility inspections |
| `crime/` | Socrata `fdj4-gpfu` | APD crime, hate crime (`t99n-5ib4`) |
| `capmetro/` | Socrata `tyfh-5r8s` | MetroBike trips |

Each package has an `__init__.py` that re-exports its public API.

## Critical: Open311 Pagination Gotcha

**The Open311 API returns records in chronological order (oldest first).** A single request with `start_date` 365 days ago + `end_date` today with `MAX_PAGES=10` (1000 records) only returns records from the START of the window — you never see recent months.

**Fix:** Fetch month by month (30-day windows) so each request is small enough to return all records for that period. See `homeless/trends.py` → `fetch_encampment_reports_monthly()` for the reference implementation. This applies to any `_fetch_code`-style function that needs data beyond 90 days.

## Map Generator Pattern

Every `generate_*_map()` function:
1. Fetches records (with optional caching via `open311_cache.py`)
2. Filters to valid Austin coordinates (lat 30.0–30.5, lon -98.0–-97.5)
3. Buckets records into 30/60/90-day × open/closed FeatureGroups
4. Builds a Folium map with MarkerCluster, dynamic title bar, and filter buttons
5. Saves to `docs/<category>/index.html`
6. Returns `(BytesIO buffer, summary string)`

Shared utilities in `open311_client.py`: `open311_get()` with exponential backoff, `subscribe_popup_html()`, `og_meta_tags()`.

## Conventions

- **Python:** PEP 8, Black formatting. Module-level `_session` singleton for HTTP reuse.
- **Commits:** Conventional Commits (`feat:`, `fix:`, `docs:`)
- **Env vars:** `TELEGRAM_BOT_TOKEN` (required), `AUSTINAPIKEY` (used for both Socrata app token and Open311 API key — same value, same GitHub Actions secret), `ALERTS_DB_PATH` (optional)
- **Bot output:** All Markdown-formatted. Use `_send_chunked()` to split at 4000-char boundaries.

## Key Rules

- Never hardcode API keys or tokens in source code
- Don't store session state in memory that should persist across bot restarts
- `_send_chunked()` attaches `reply_markup` only to the last chunk
- Static maps live in `docs/` and are deployed via GitHub Pages — this repo is the single source of truth
- The alerts system uses SQLite at `ALERTS_DB_PATH` (default `/tmp/austin311_alerts.db`, Fly volume in production)
