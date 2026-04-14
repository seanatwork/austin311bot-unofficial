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
- `graffiti/` — Open311 service code `HHSGRAFF`; supports analysis and a Folium-generated map (`/graffiti` → redirects to web map)
- `bicycle/` — Open311 across 8 service codes (PWBICYCL, OBSTMIDB, SBDEBROW, ATCOCIRW, SBSIDERE, TPPECRNE, PWSIDEWL, ZZARSTSW); includes keyword filtering for bicycle relevance; supports stats and a Folium-generated map (`/bicycle` → redirects to web map)
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

Public maps are deployed at Netlify, generated from the same data as Telegram commands.

**Maps:**
- `docs/index.html` — Landing page hub (https://atxpulse.netlify.app/)
- `docs/homeless/index.html` — Homeless encampment map (https://atxpulse.netlify.app/homeless/)
- `docs/bicycle/index.html` — Bicycle infrastructure map (https://atxpulse.netlify.app/bicycle/)
- `docs/graffiti/index.html` — Graffiti abatement map (https://atxpulse.netlify.app/graffiti/)
- `docs/traffic/index.html` — Traffic & infrastructure map (https://atxpulse.netlify.app/traffic/)
- `docs/parking/index.html` — Parking enforcement map (https://atxpulse.netlify.app/parking/)

**Files:**
- `scripts/generate_map.py` — generic map generator that accepts category as CLI argument
  - Usage: `python scripts/generate_map.py bicycle|graffiti|homeless|traffic|parking`
- `.github/workflows/deploy-map.yml` — GitHub Actions cron for homeless map (daily noon UTC)
- `.github/workflows/generate-bicycle-map.yml` — GitHub Actions cron for bicycle map (daily noon UTC)
- `.github/workflows/generate-graffiti-map.yml` — GitHub Actions cron for graffiti map (daily noon UTC)
- `.github/workflows/generate-traffic-map.yml` — GitHub Actions cron for traffic map (daily noon UTC)
- `.github/workflows/generate-parking-map.yml` — GitHub Actions cron for parking map (daily noon UTC)
- `netlify.toml` — tells Netlify to serve from `docs/`, no build command
- `docs/*/index.html` — pre-generated Folium HTML maps (committed to repo)

**Map features (all maps follow the same pattern):**
- 90 days of data fetched; user can filter to 30d / 60d / 90d via buttons
- Open / Closed status toggles
- Title bar updates dynamically with count reflecting active filters
- Popups show: clickable ticket link (`https://311.austintexas.gov/tickets?filter%5Bsearch%5D={id}`), address, filed/updated dates, description (up to 500 chars) falling back to resolution notes
- Mobile-friendly viewport meta tag

**Netlify deploy workflow (manual):**
1. Test locally: `source .venv/bin/activate && PYTHONPATH=. python scripts/generate_map.py <category>`
2. Commit and push changes to GitHub
3. GitHub Actions → "Refresh <category> map" → Run workflow (regenerates `docs/<category>/index.html`)
4. Netlify dashboard → Deploys → Trigger deploy → Deploy site (without cache)

**Notes:**
- Netlify auto-builds are currently **stopped** to conserve build minutes; trigger manually
- `AUSTIN_APP_TOKEN` must be set as a GitHub Actions secret for rate limit headroom
- 429 rate limit errors during local runs are normal without the token; CI has the secret
- `.venv/` is the working virtualenv (system Python is externally managed)

## In-Progress: Crime Choropleth Map

**Goal:** Add a crime map at `docs/crime/index.html` as a Folium choropleth colored by APD incident count per council district. Unlike the other maps (which use 311 lat/lon points), crime data has no coordinates — only `council_district`, `sector`, and `census_block_group`. A district-level choropleth is the right fit.

**Data sources:**
- APD Crime Reports: Socrata `fdj4-gpfu` — fields: `crime_type`, `clearance_status`, `occ_date`, `council_district`, `location_type`, `census_block_group` (no lat/lon)
- NIBRS Homicides: Socrata `i7fg-wrk5` — fields: `nibrs_desc`, `internal_clearance_status`, `occurred_date`, `council_district`, `zip_code`, `location_description` (no lat/lon)
- Austin council district boundaries GeoJSON — needed for choropleth polygons; source still being identified from data.austintexas.gov

**Remaining tasks:**
1. Find working GeoJSON source for Austin council district boundaries
2. Create `scripts/generate_crime_map.py` (or a `crime/` module) following the same pattern as existing map generators — fetches data, builds Folium choropleth, returns `io.BytesIO` HTML
3. Wire into `scripts/generate_map.py` and add `.github/workflows/generate-crime-map.yml`
4. Add crime card to `docs/index.html`
5. Update `docs/index.html` landing page title/header from "Austin 311" to something data-general (e.g. "ATX Pulse") and update footer to reference both 311 and APD data sources
