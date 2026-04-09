# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Unofficial Telegram bot for exploring Austin 311 service data. Users interact via Telegram slash commands and inline buttons to query live Open311/Socrata APIs for graffiti, bicycle infrastructure, restaurant inspections, animal services, traffic, noise, parking, and crime data.

## Commands

```bash
# Setup
python -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env  # add TELEGRAM_BOT_TOKEN

# Run the bot
python austin311_bot.py

# Utility entrypoints
open311-ingest       # Bulk SQLite ingestion
scrape-bicycle       # Bicycle data scraper
search-311           # Browse 311 service codes
open311-aggregate    # Heatmap aggregator
```

No test runner is configured; the `graffiti/tests/` directory contains unit tests that can be run with `python -m pytest graffiti/tests/`.

## Deployment

Auto-deploys to Fly.io on push to `main` via `.github/workflows/deploy.yml`. The Fly.io app (`austin311bot`) runs as a single container (1 shared CPU, 256MB) in `iad`. Deploys use the `FLY_API_TOKEN` GitHub secret.

## Architecture

`austin311_bot.py` (the main file, ~3,700 lines) owns all Telegram routing. It imports data-fetching/formatting functions from service packages and wires them to handlers.

**Adding a new 311 service** requires three things:
1. A new package directory with a `*_bot.py` module that queries the API and returns formatted Markdown
2. Importing that module in `austin311_bot.py`
3. Adding command/callback handlers in the `create_application()` function

**Service packages** (each is independent):
- `graffiti/` — Open311 service code `HHSGRAFF`
- `bicycle/` — Open311 service code `PWBICYCL`; also has a local SQLite cache (`bicycle_complaints.db`)
- `restaurants/` — Socrata dataset `ecmv-9xxi` (health inspections)
- `animalsvc/` — Open311 across 7+ service codes (loose dogs, bites, coyotes, etc.)
- `infrastructureandtransportation/` — Open311 (potholes, signals, sidewalks)
- `noisecomplaints/` — Open311 service code `NOISECMP`
- `parking/` — Open311 service code `PARKINGV`
- `parks/` — Open311 park maintenance reports with drill-down by park name
- `waterconservation/` — Socrata water conservation violations (`/waterviolations`)
- `childcare/` — Socrata childcare facility inspections (`/childcare`)
- `homeless/` — Open311 encampment/unhoused 311 reports across 6 service codes (PRGRDISS, ATCOCIRW, OBSTMIDB, SBDEBROW, DRCHANEL, NOISECMP); supports stats, open locations, and a Folium-generated map (`/homeless`)
- Crime/safety — Socrata datasets `fdj4-gpfu` (APD crime) and `i7fg-wrk5` (NIBRS homicides), handled directly in `austin311_bot.py`

## Data Sources

| Source | Base URL | Used for |
|--------|----------|----------|
| Open311 API | `https://311.austintexas.gov/open311/v2` | All 311 service requests |
| Socrata API | `data.austintexas.gov` | Restaurant inspections, crime reports |

Query patterns: ISO8601 dates with `Z` suffix, `per_page`/`page` pagination, `$where` SoQL filtering (Socrata).

## Key Conventions

- Each service module uses a module-level `_session` singleton for HTTP connection reuse.
- All network calls use retry logic with exponential backoff (up to 3 retries, starting at 2s).
- Telegram messages over 4KB are split via `_send_chunked()` in `austin311_bot.py`.
- All bot output is Markdown-formatted.
- Environment variables: `TELEGRAM_BOT_TOKEN` (required), `AUSTIN_APP_TOKEN` (optional, raises Open311 rate limits), `GOOGLE_MAPS_API_KEY` (optional, for `/directory`).

## Static Map Website

A public encampment map is deployed at Netlify, generated from the same data as the `/homeless` Telegram command.

**Files:**
- `scripts/generate_map.py` — generates `docs/index.html` using `homeless.homeless_bot.generate_encampment_map(90)`
- `.github/workflows/deploy-map.yml` — GitHub Actions cron (daily noon UTC) that runs the script, commits `docs/index.html`, and pushes to `main`
- `netlify.toml` — tells Netlify to serve from `docs/`, no build command
- `docs/index.html` — pre-generated Folium HTML map (committed to repo)

**Map features:**
- 90 days of data fetched; user can filter to 30d / 60d / 90d via buttons
- Open / Closed status toggles
- Title bar updates dynamically with count reflecting active filters
- Popups show: clickable ticket link (`https://311.austintexas.gov/tickets?filter%5Bsearch%5D={id}`), address, filed/updated dates, description (up to 500 chars) falling back to resolution notes

**Netlify deploy workflow (manual):**
1. Test locally: `source .venv/bin/activate && PYTHONPATH=. python scripts/generate_map.py`
2. Commit and push changes to GitHub
3. GitHub Actions → "Refresh encampment map" → Run workflow (regenerates `docs/index.html`)
4. Netlify dashboard → Deploys → Trigger deploy → Deploy site (without cache)

**Notes:**
- Netlify auto-builds are currently **stopped** to conserve build minutes; trigger manually
- `AUSTIN_APP_TOKEN` must be set as a GitHub Actions secret for rate limit headroom
- 429 rate limit errors during local runs are normal without the token; CI has the secret
- `.venv/` is the working virtualenv (system Python is externally managed)
