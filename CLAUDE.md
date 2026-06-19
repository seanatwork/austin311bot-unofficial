# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commit & Push Policy

**Push immediately (after verifying correctness) when:**
- Adding a new page, feature, or section to the site (e.g., new card on homepage, new data viz, new map)
- Fixing a broken feature or visual bug
- Making any change that would be visible to site visitors

**Don't push for:**
- Small iterative tweaks during an active conversation — batch them into one commit
- Documentation-only changes to CLAUDE.md or internal notes
- Changes to config files that don't affect functionality

**Process:**
1. Verify the change is correct (read the file, check for syntax errors)
2. Commit with a conventional prefix: `feat:` for new features, `fix:` for bug fixes, `docs:` for docs
3. Push: `cd /home/sean/Documents/Projects/austin311bot-unofficial && git add <files> && git commit -m "..." && git push`

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

# Utility entrypoints (defined in pyproject.toml)
open311-ingest       # Bulk SQLite ingestion
scrape-bicycle       # Bicycle data scraper
search-311           # Browse all available 311 service codes
open311-aggregate    # Heatmap aggregator

# Generate a static map locally
source .venv/bin/activate && PYTHONPATH=. python scripts/generate_map.py <category>
# category values: bicycle, graffiti, homeless, traffic, parking, crime, noise, parks, water, childcare, animal, storm
```

No test runner is configured; the `graffiti/tests/` directory contains unit tests that can be run with `python -m pytest graffiti/tests/`.

## Deployment

Auto-deploys to Fly.io on push to `main` via `.github/workflows/deploy.yml`. The Fly.io app (`austin311bot`) runs as a single container (1 shared CPU, 256MB) in `iad`. Deploys use the `FLY_API_TOKEN` GitHub secret.

## Architecture

## `austin311_bot.py` Architecture Notes

The main file (~3,700 lines) is organized into these sections:
1. Imports + config (~100 lines)
2. Rate limiting (`_rate_limit_max=30`, `_rate_limit_window=60`)
3. Ticket ID validation (`_validate_ticket_id()`)
4. `_send_chunked()` helper
5. Command handlers: `/start`, `/help`
6. Service submenus (inline button menus for each category)
7. Individual service handlers (graffiti, bicycle, animal, traffic, noise, parking, parks, homeless)
8. Crash stats (fetches from Socrata `y2wy-tgr5`)
9. Water quality (fetches from Socrata `5tye-7ray`)
10. Building permits (fetches from Socrata `3syk-w9eu`)
11. Bar/TABC sales (fetches from data.texas.gov `g5bj-yb6k`)
12. Court caseloads
13. Report submission (ConversationHandler for Open311 POST)
14. Error handler + application setup (`create_application()`)
15. `main()` entry point

**Key handlers registered in `create_application()`:**
- `CommandHandler` for `/start`, `/help`, `/ticket`, `/report`
- `CallbackQueryHandler` for all inline button patterns (no `ConversationHandler` state machine, except `/report`)
- `MessageHandler` for text input (echo/fallback)
- Alert jobs via `job_queue.run_daily()`

**Adding a new 311 service** requires:
1. A new package directory with a `*_bot.py` module that queries the API and returns formatted Markdown
2. Importing that module in `austin311_bot.py`
3. An `async def <name>_command()` handler and registration in `create_application()`
4. If it has a map: add it to `scripts/generate_map.py`'s `CATEGORY_MAPS` dict and a GitHub Actions workflow

**Service packages** (each is independent):
- `graffiti/` — Open311 service code `HHSGRAFF`; supports analysis and a Folium-generated map
- `bicycle/` — Open311 across 8 service codes (PWBICYCL, OBSTMIDB, SBDEBROW, ATCOCIRW, SBSIDERE, TPPECRNE, PWSIDEWL, ZZARSTSW); includes keyword filtering for bicycle relevance
- `restaurants/` — Socrata dataset `ecmv-9xxi` (health inspections)
- `animalsvc/` — Open311 across 7+ service codes (loose dogs, bites, coyotes, etc.)
- `infrastructureandtransportation/` — Open311 (potholes, signals, sidewalks)
- `noisecomplaints/` — Open311 service code `NOISECMP`
- `parking/` — Open311 service code `PARKINGV`; `parking/trends.py` aggregates by month/street/violation type
- `parks/` — Open311 park maintenance reports with drill-down by park name
- `waterconservation/` — Socrata water conservation violations (`/waterviolations`)
- `childcare/` — Socrata childcare facility inspections (`/childcare`)
- `homeless/` — Open311 across 5 service codes (PRGRDISS, ATCOCIRW, OBSTMIDB, SBDEBROW, DRCHANEL); supports stats, open locations, and map
- `alerts/` — Subscription system for nearby 311 and animal service alerts; stores subscriptions in SQLite (`ALERTS_DB_PATH`, defaults to `/tmp/austin311_alerts.db`); background jobs in `alerts/jobs.py` run daily at 08:00 UTC via `job_queue.run_daily()`
- `storm/` — Open311 across 8 codes (SWSSTORM, DRCHANEL, DRILID, DRFLOODG, DRSSPIPE, DRFLOODR, ZZEROSIO, DRDITCH), grouped into debris/drainage/flooding/erosion buckets — **map generator exists but not yet integrated into austin311_bot.py**
- `capmetro/` — Socrata dataset `tyfh-5r8s` (MetroBike trip analytics, kiosk flow, membership breakdown) — **not yet integrated into austin311_bot.py**
- Crime/safety — Socrata datasets `fdj4-gpfu` (APD crime) and `i7fg-wrk5` (NIBRS homicides), handled directly in `austin311_bot.py`; `crime/hate_crime.py` covers APD hate crime via `t99n-5ib4` — **not yet integrated**

## Package Exports (via `__init__.py`)

Each service package's `__init__.py` re-exports the public API. The pattern is:

```python
# Example: graffiti/__init__.py
from .graffiti_bot import analyze_graffiti_command, patterns_command
from .remediation_analysis import remediation_command, compare_command
from .config import Config, setup_logging
```

| Package | Key Exports |
|---------|-------------|
| `graffiti/` | `analyze_graffiti_command, patterns_command, remediation_command, compare_command, Config` |
| `bicycle/` | `get_recent_complaints, get_stats, lookup_ticket, format_complaints, format_stats, format_ticket` |
| `homeless/` | `get_encampment_stats, format_encampment_stats, format_encampment_locations, generate_encampment_map` |
| `animalsvc/` | `get_hotspots, get_stats, get_response_times, format_hotspots, format_stats, format_response_times, generate_animal_map` |
| `noisecomplaints/` | `get_hotspots, format_hotspots, get_peak_times, format_peak_times, get_resolution_by_type, format_resolution_by_type, get_night_breakdown, format_night_breakdown, generate_noise_map` |
| `parking/` | `get_stats, get_hotspots, format_stats, format_hotspots, generate_parking_map` |
| `parks/` | `get_park_stats, get_park_hotspots, get_park_resolution, get_park_detail, format_stats, format_hotspots, format_resolution, format_park_detail, format_unified_overview, build_park_name_keyboard, generate_parks_map` |
| `restaurants/` | `get_restaurant_stats, format_restaurant_stats` |
| `waterconservation/` | `get_water_conservation_stats, format_water_conservation, generate_water_map` |
| `childcare/` | `get_childcare_stats, format_childcare, generate_childcare_map` |
| `infrastructureandtransportation/` | Various traffic/infra functions (imported directly from `traffic_bot.py`) |
| `storm/` | `storm_command, storm_stats, generate_storm_map` |
| `capmetro/` | `get_electric_vs_classic, get_total_trips, get_kiosk_flow, get_kiosk_evolution, get_membership_breakdown, get_kiosk_locations, KIOSK_LOCATIONS` |
| `crime/` | `generate_crime_map` (hate crime handled via `hate_crime.py`) |
| `alerts/` | `init_db, register_alert_handlers, nearby_311_job, animal_nearby_job, crash_nearby_job` |
```

### 2. Expand Map Generator Section with Function Signatures

Replace the current Map Generator Functions section with:

```markdown
## Map Generator Functions (called by `scripts/generate_map.py`)

Each generator follows the same signature: `generate_<category>_map(days: int = 90) -> tuple[Optional[io.BytesIO], str]`
Returns `(BytesIO buffer with HTML, summary message)`.

Available generators and their packages:
- `graffiti.graffiti_bot.generate_graffiti_map(days_back=30)`
- `bicycle.bicycle_bot.generate_bicycle_map(days_back=90)`
- `homeless.homeless_bot.generate_encampment_map(days_back=180)`
- `animalsvc.animal_bot.generate_animal_map(days_back=90)`
- `animalsvc.dead_animal_bot.generate_dead_animal_map(days_back=90)`
- `noisecomplaints.noise_bot.generate_noise_map(days_back=90)`
- `parking.parking_bot.generate_parking_map(days_back=30)`
- `parks.parks_bot.generate_parks_map(days_back=90)`
- `waterconservation.water_conservation_bot.generate_water_map(days_back=90)`
- `childcare.childcare_bot.generate_childcare_map(days_back=90)`
- `infrastructureandtransportation.traffic_bot.generate_traffic_map(days_back=30)`
- `storm.storm_bot.generate_storm_map(days_back=90)`
- `crime.crime_map.generate_crime_map(days_back=90)`

**Special generators** (not point maps, use different signatures):
- `crime.hate_crime.generate_hate_crime()` — No days parameter; always fetches all years
- `scripts.generate_budget.main()` — No days parameter; generates budget visualization
- `scripts.generate_court_data.main()` — No days parameter; generates court caseload data
- `scripts.generate_capmetro_data.main()` — No days parameter; generates MetroBike data
- `scripts.generate_nearby_page.main()` — Generates "311 Near You" dynamic map
- `scripts.generate_pulse.main()` — Generates real-time pulse JSON

**Trends generators** (follow `generate_<category>_trends(days_back=365)` signature):
- `graffiti.trends.generate_graffiti_trends(days_back=365)`
- `homeless.trends.generate_homeless_trends(days_back=365)`
- `noisecomplaints.trends.generate_noise_trends(days_back=365)`
- `parking.trends.generate_parking_trends(days_back=365)`
- `crime.trends.generate_crime_trends(days_back=365)`
```

### 3. Add Trends Module Pattern (new section)

```markdown
## Trends Modules

Five packages have a `trends.py` module for historical aggregation:

| Package | Trend Generator | Data Source | Output |
|---------|----------------|-------------|--------|
| `graffiti/trends.py` | `generate_graffiti_trends()` | Open311 HHSGRAFF via `fetch_graffiti_monthly()` | HTML with Chart.js line charts |
| `homeless/trends.py` | `generate_homeless_trends()` | Open311 5 codes via `fetch_encampment_reports_monthly()` | HTML with SVG line chart |
| `noisecomplaints/trends.py` | `generate_noise_trends()` | Open311 3 codes via `fetch_noise_monthly()` | HTML with Chart.js bar/doughnut charts |
| `parking/trends.py` | `generate_parking_trends()` | Open311 PARKINGV via `fetch_parking_monthly()` | HTML with Chart.js + drill-down |
| `crime/trends.py` | `generate_crime_trends()` | Socrata `fdj4-gpfu` via SoQL aggregation | HTML with Chart.js line charts |

**Common pattern:** All trends modules:
1. Fetch data month-by-month (30-day windows) to avoid the Open311 pagination gotcha
2. Aggregate into monthly buckets
3. Generate a standalone HTML page with Chart.js or SVG charts
4. Output to `docs/<category>/trends/index.html`
5. Include dark mode, responsive layout, and navigation back to the map
```

### 4. Add Common Code Patterns Section

```markdown
## Common Code Patterns

### HTTP Session Singleton
Every module that makes HTTP requests uses a module-level session singleton:
```python
_session: Optional[requests.Session] = None

def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({"Accept": "application/json", "User-Agent": "austin311bot/0.1"})
    return _session
```

### Open311 Pagination (`_fetch_code`)
All Open311 data-fetching functions follow this pattern:
```python
MAX_PAGES = 10  # Cap at 1,000 records per code
PER_PAGE = 100  # Max per Open311 API call

def _fetch_code(service_code: str, days_back: int) -> list:
    end = _utc_now()
    start = end - timedelta(days=days_back)
    all_records = []
    page = 1
    while page <= MAX_PAGES:
        params = {"service_code": code, "start_date": _isoformat_z(start), 
                  "end_date": _isoformat_z(end), "per_page": 100, "page": page}
        records = _make_request(params)
        if not records: break
        all_records.extend(records)
        if len(records) < 100: break
        page += 1
        time.sleep(1.0 if API_KEY else 2.0)
    return all_records
```

### Date/Time Helpers
Every module uses these consistent helpers:
- `_utc_now()` — Returns `datetime.now(timezone.utc)`
- `_isoformat_z(dt)` — Converts datetime to ISO8601 with `Z` suffix
- `_format_central_time()` — Returns human-readable Central Time string
- `_age_days(record)` — Calculates days since a record was filed (used in map bucket logic)

### Map Generator Pattern
Every `generate_*_map()` function:
1. Fetches records (with optional caching)
2. Filters to valid coordinates (Austin bounding box: 30.0–30.5 lat, -98.0–-97.5 lon)
3. Buckets records into 30/60/90-day × open/closed FeatureGroups
4. Builds a Folium map with MarkerCluster, dynamic title bar, and filter buttons
5. Includes `og_meta_tags()` in `<head>` and `subscribe_popup_html()` in popups
6. Saves to `docs/<category>/index.html`
7. Returns `(BytesIO buffer, summary string)`
```

### 5. Add Scripts Directory Reference

```markdown
## Scripts (`scripts/`)

| Script | Purpose | Run Command |
|--------|---------|-------------|
| `generate_map.py` | Generic map/trends generator | `python scripts/generate_map.py <category>` |
| `generate_budget.py` | City budget visualization | `python scripts/generate_budget.py` |
| `generate_capmetro_data.py` | MetroBike trip analytics | `python scripts/generate_capmetro_data.py` |
| `generate_court_data.py` | Court caseload data | `python scripts/generate_court_data.py` |
| `generate_nearby_page.py` | "311 Near You" dynamic map | `python scripts/generate_nearby_page.py` |
| `generate_pulse.py` | Real-time pulse JSON | `python scripts/generate_pulse.py` |
| `generate_og_image.py` | Open Graph preview images | `python scripts/generate_og_image.py` |
```

### 6. Add Tools Directory Reference

```markdown
## Tools (`tools/`)

| Script | Purpose |
|--------|---------|
| `search_311_categories.py` | Browse/search all available 311 service codes (CLI entrypoint: `search-311`) |
| `discover_homeless_codes.py` | Discover homeless-related 311 service codes |
```

### 7. Add Data Sources Section Enhancement

Add to the Data Sources table:

```markdown
| Source | Base URL | Used for |
|--------|----------|----------|
| Texas HHSC Socrata | `data.texas.gov` | Child care licensing (`bc5r-88dy`, `tqgd-mf4x`) |
| TABC Socrata | `data.texas.gov` | Mixed beverage sales (`g5bj-yb6k`) |
| City of Austin ArcGIS | `services.arcgis.com/...` | Council district GeoJSON for crime choropleth |
```

### 8. Add `open311_client.py` Functions Reference

```markdown
## `open311_client.py` — Shared Utilities

- **`open311_get(session, url, params, retries=0)`** — GET with exponential backoff (up to 8 retries; 15s starting delay for 429, respects `Retry-After`)
- **`telegram_subscribe_link(lat, lon, alert_code="311")`** — Builds `t.me/austin311bot?start=sub_...` deep link
- **`subscribe_popup_html(lat, lon, alert_code="311")`** — HTML snippet for Folium popup "Alert me near here" button
- **`og_meta_tags(slug="")`** — Returns Open Graph + Twitter Card meta tags for a docs page
- **`SITE_BASE_URL`** — `https://austin311.com`
- **`TELEGRAM_BOT_USERNAME`** — `austin311bot`
```

### 9. Add `open311_cache.py` API Reference

```markdown
## `open311_cache.py` — Caching API

- **`init_cache()`** — Creates SQLite tables (`service_requests`, `cache_metadata`)
- **`get_cached_records(category, since, service_codes)`** — Retrieve cached records
- **`cache_records(category, records)`** — Store new records in cache
- **`get_last_fetch_date(category)`** — Get most recent record datetime
- **`should_refresh_cache(category, max_age_hours=24)`** — Check if cache is stale
- **`get_cache_stats(category)`** — Get record counts per category
- **`clear_cache(category)`** — Clear cache for a category or all

Cache location: `.cache/open311_cache.db` (gitignored, persisted via GitHub Actions cache with 7-day retention)
```

### 10. Add Deployment Configuration Details

```markdown
## Deployment Configuration

| File | Purpose | Key Settings |
|------|---------|--------------|
| `Dockerfile` | Container build | Python 3.11-slim, `pip install -r requirements.txt`, `pip install -e .`, CMD `python austin311_bot.py` |
| `fly.toml` | Fly.io deployment | `app = "austin311bot"`, region `iad`, 1 shared CPU, 256MB RAM |
| `railway.json` | Railway deployment | Alternative deployment config |
| `nixpacks.toml` | Nixpacks build | Alternative build config |
| `requirements.txt` | Pip dependencies | Mirrors `pyproject.toml` dependencies |
```

### 11. Add Missing Map Pages to Maps Section

Add to the Maps list:

```
- `docs/storm/index.html` — Storm debris, drainage & flooding point map
- `docs/restaurants/index.html` — Restaurant inspection compliance map
- `docs/capmetro/index.html` — MetroBike trip analytics (kiosk flow, membership breakdown)
- `docs/fun/index.html` — Fun data page (bar of the month, coyote sightings)
- `docs/pulse.json` — Real-time pulse data (parking transactions, etc.)
```

### 12. Add Alerts System Detail

```markdown
## Alerts System (`alerts/`)

**Database:** SQLite at `ALERTS_DB_PATH` (default: `/tmp/austin311_alerts.db`, Fly volume path in production)

**Schema (`alerts/db.py`):**
- `subscriptions` table: `chat_id, lat, lon, radius_meters, categories, created_at`
- `sent_log` table: Tracks which alerts have been sent to avoid duplicates

**Alert Types:**
| Type | Code | Description | Background Job |
|------|------|-------------|----------------|
| Nearby 311 | `nearby_311` | 311 reports within radius | `nearby_311_job` (daily 08:00 UTC) |
| Animal nearby | `animal_nearby` | Animal incidents within radius | `animal_nearby_job` (daily 08:00 UTC) |
| Crash nearby | `crash_nearby` | Crashes within radius | `crash_nearby_job` (daily 08:00 UTC) |

**Handlers:** `alerts/handlers.py` registers callback handlers via `register_alert_handlers(app)`

**Key functions in `alerts/db.py`:**
- `init_db()` — Create tables
- `get_active_subscriptions(alert_type)` — Get subscribers for an alert type
- `already_sent(sub_id, date_key)` — Check if alert was already sent today
- `mark_sent(sub_id, date_key)` — Record that alert was sent
- `prune_sent_log()` — Clean up old sent-log entries
```

### 13. Add `_send_chunked` Enhancements

```markdown
**`_send_chunked(target, text, parse_mode="Markdown", reply_markup=None)`:**
- Splits output at 4000-char boundaries (Telegram limit is 4096)
- `reply_markup` is attached only to the last chunk
- First chunk uses `edit_message_text()` if target has it (callback query), subsequent chunks use `reply_text()`
```

### 14. Add Rate Limiting Pattern

```markdown
## Rate Limiting

The bot implements a global rate limiter (not per-user):
- **Limit:** 30 requests per 60 seconds across all users
- **Mechanism:** `_request_times` list tracks timestamps; `_is_rate_limited()` checks if limit is hit
- **Decorator:** `@rate_limited` on all command handlers returns a retry-after message if limited
- **Note:** No user data is stored or tracked by the rate limiter
```

### 15. Add `post_init()` Details Enhancement

```markdown
**`post_init()` hook** (called once on startup):
1. Clears stale slash commands from ALL Telegram scopes: `BotCommandScopeDefault()`, `AllPrivateChats()`, `AllGroupChats()`, `AllChatAdministrators()`
2. Re-registers the current command set: `/subscribe`, `/myalerts`, `/unsubscribe`, `/deletedata`, `/help`, `/start`
3. This is required because Telegram persists commands across deploys — removed commands linger without explicit clearing
```

### 16. Add Crime/Safety Section Detail

```markdown
## Crime & Safety

APD crime data uses Socrata datasets, handled directly in `austin311_bot.py`:

| Dataset | Socrata ID | Used For |
|---------|-----------|----------|
| APD Crime Reports | `fdj4-gpfu` | Crime choropleth map, crime trends, district digests |
| NIBRS Homicides | `i7fg-wrk5` | Homicide-specific data |
| Hate Crime Incidents | `t99n-5ib4` | Hate crime yearly trends, bias breakdown, offender demographics (`crime/hate_crime.py`) |
| Real-Time Traffic Incidents | `dx9v-zd7x` | Live traffic incidents (`/traffic` command) |
| Crash Report Data | `y2wy-tgr5` | Crash stats, crash map, crash nearby alerts |
| Austin MetroBike Trips | `tyfh-5r8s` | Bike trip analytics (`capmetro/`) |
| TABC Mixed Beverage | `g5bj-yb6k` | Bar of the month (`/bars` command) |
| Building Permits | `3syk-w9eu` | Permit activity (`/permits` command) |
| Surface Water Quality | `5tye-7ray` | Water quality (`/water` command) |
| Parking Meter Transactions | `5bb2-gtef` | Parking pulse (24h activity) |
```

### 17. Add `_validate_ticket_id()` Pattern

```markdown
## Ticket ID Validation

The bot validates 311 ticket IDs with pattern `/^[0-9]{2}-[0-9]{8}$/`:
- Format: `YY-XXXXXXXX` (e.g., `16-00123456`)
- Rejects years > 2050 as obviously wrong
- Used by `/ticket` command and `tlookup_*` callback handlers
```

### 18. Add Service Code Discovery Notes

```markdown
## Service Code Discovery

The `homeless/` package documents the discovery process for selecting service codes:
- Analysis of recent requests found homeless-related keywords across PRGRDISS, OBSTMIDB, SBDEBROW, DRCHANEL, ATCOCIRW
- APDNONNO and HHSGRAFF were excluded after analysis (too many false positives)
- The "Homeless - Violet Kiosk" and "Homelessness Matters" 311 categories reuse general maintenance codes — no unique homeless-specific codes exist

**Tools for discovery:**
- `tools/search_311_categories.py` — Browse all available 311 service codes (CLI: `search-311`)
- `tools/discover_homeless_codes.py` — Targeted discovery of homeless-related codes
```

### 19. Add `docs/` Directory Map Overview

```markdown
## Static Website (`docs/`)

The `docs/` folder serves as a GitHub Pages site at https://austin311.com/ with these subdirectories:

| Path | Content | Generator |
|------|---------|-----------|
| `index.html` | Landing page hub | Manual (hand-written) |
| `animal/` | Animal services map | `animalsvc.animal_bot.generate_animal_map()` |
| `animal/dead/` | Dead animal collection map | `animalsvc.dead_animal_bot.generate_dead_animal_map()` |
| `bicycle/` | Bicycle infrastructure map | `bicycle.bicycle_bot.generate_bicycle_map()` |
| `budget/` | City budget spending | `scripts.generate_budget.main()` |
| `capmetro/` | MetroBike trip analytics | `scripts.generate_capmetro_data.main()` |
| `childcare/` | Childcare compliance map | `childcare.childcare_bot.generate_childcare_map()` |
| `court/` | Court caseloads | `scripts.generate_court_data.main()` |
| `court/trends/` | Court caseload trends | `scripts.generate_court_data.main()` |
| `crashes/` | Crash map (client-side only) | Fetches live from Socrata `y2wy-tgr5` |
| `crashes/trends/` | Crash trends | `crime.trends.generate_crime_trends()` |
| `crime/` | Crime choropleth | `crime.crime_map.generate_crime_map()` |
| `crime/trends/` | Crime trends | `crime.trends.generate_crime_trends()` |
| `environment/` | TCEQ spills + water quality | Manual (hand-written HTML) |
| `fun/` | Fun data (bars, coyotes) | Manual (hand-written HTML) |
| `graffiti/` | Graffiti abatement map | `graffiti.graffiti_bot.generate_graffiti_map()` |
| `graffiti/trends/` | Graffiti trends | `graffiti.trends.generate_graffiti_trends()` |
| `homeless/` | Homeless encampment map | `homeless.homeless_bot.generate_encampment_map()` |
| `homeless/trends/` | Homeless trends | `homeless.trends.generate_homeless_trends()` |
| `noise/` | Noise complaint map | `noisecomplaints.noise_bot.generate_noise_map()` |
| `noise/trends/` | Noise trends | `noisecomplaints.trends.generate_noise_trends()` |
| `parking/` | Parking enforcement map | `parking.parking_bot.generate_parking_map()` |
| `parking/trends/` | Parking trends | `parking.trends.generate_parking_trends()` |
| `parks/` | Parks maintenance map | `parks.parks_bot.generate_parks_map()` |
| `restaurants/` | Restaurant inspection map | Manual (hand-written HTML) |
| `storm/` | Storm debris/drainage map | `storm.storm_bot.generate_storm_map()` |
| `traffic/` | Traffic & infrastructure map | `infrastructureandtransportation.traffic_bot.generate_traffic_map()` |
| `water/` | Water conservation map | `waterconservation.water_conservation_bot.generate_water_map()` |
| `pulse.json` | Real-time pulse data | `scripts.generate_pulse.main()` |

## Data Sources

| Source | Base URL | Used for |
|--------|----------|----------|
| Open311 API | `https://311.austintexas.gov/open311/v2` | All 311 service requests |
| Socrata API | `data.austintexas.gov` | Restaurant inspections, crime reports, water, childcare, MetroBike |

Open311: ISO8601 dates with `Z` suffix, `per_page`/`page` pagination.
Socrata: `$where` SoQL filtering, `$group`/`$select` aggregation.

## Telegram Bot Patterns

**Callback handlers:** The bot uses `CallbackQueryHandler(handler, pattern="^prefix_")` for all inline button interactions — no `ConversationHandler` state machine. State is passed via `callback_data` strings and persisted in `context.user_data`. Multiple handlers are stacked in `create_application()` with non-overlapping regex patterns.

**Map deep-links:** Inline keyboard buttons that open maps use `web_app=WebAppInfo(url="https://austin311.com/<category>/")`, which opens the Folium map inside Telegram's in-app browser without leaving the chat.

**Background jobs:** Alert delivery uses `application.job_queue.run_daily(job_fn, time=..., name="job_name")`. Job names (`"nearby_311"`, `"animal_nearby"`) appear in logs; don't rename them.

**`post_init()` hook:** Called once on startup. Clears stale slash commands from all Telegram scopes (global, private, group, supergroup) then re-registers the current command set. This is required — Telegram persists commands across deploys, so removed commands linger without explicit clearing.

**`_send_chunked(message, text, reply_markup)`:** Splits output at 4000-char boundaries; `reply_markup` is attached only to the last chunk.

## Key Conventions

- Each service module uses a module-level `_session` singleton for HTTP connection reuse.
- **`open311_client.py`** provides `open311_get(session, url, params)` with exponential backoff (up to 8 retries; 15s starting delay for 429, respects `Retry-After`). Also provides `subscribe_popup_html()` and `telegram_subscribe_link()` for alert subscription buttons embedded in map popups.
- All bot output is Markdown-formatted.
- Environment variables: `TELEGRAM_BOT_TOKEN` (required), `AUSTINAPIKEY` (used for both Socrata app token and Open311 API key — same value, same GitHub Actions secret), `GOOGLE_MAPS_API_KEY` (optional, for `/directory`), `ALERTS_DB_PATH` (optional, defaults to `/tmp/austin311_alerts.db` — set to a Fly volume path in production).

## Open311 API — Known Pagination Gotcha

**The API returns records in chronological order (oldest first).** A single request with `start_date` 365 days ago and `end_date` today will return the oldest records first — so with `MAX_PAGES=10` (1000 records) you only see records from the *start* of the window, never the recent months.

**Fix used in `homeless/trends.py`:** Fetch month by month — one 30-day window per API call — so each request is small enough that all records for that period are returned. See `fetch_encampment_reports_monthly()` in `homeless/homeless_bot.py`.

**Applies to:** any `_fetch_code`-style function across bicycle, graffiti, homeless, noise, parking, parks, storm modules if they ever need historical trend data beyond 90 days.

## Static Map Website

Public maps are deployed via GitHub Pages (`docs/` folder), generated from the same data as Telegram commands. This repo is the single source of truth — edits to map output (generators, `*_bot.py` modules, `scripts/generate_map.py`) deploy directly via this repo's workflows on push to `main`.

**Maps:**
- `docs/index.html` — Landing page hub (https://austin311.com/)
- `docs/homeless/index.html` — Homeless encampment map
- `docs/bicycle/index.html` — Bicycle infrastructure map
- `docs/graffiti/index.html` — Graffiti abatement map
- `docs/traffic/index.html` — Traffic & infrastructure map
- `docs/parking/index.html` — Parking enforcement map
- `docs/crime/index.html` — APD crime choropleth by council district
- `docs/noise/index.html` — Noise complaint point map
- `docs/parks/index.html` — Park maintenance point map
- `docs/water/index.html` — Water conservation violations map
- `docs/childcare/index.html` — Childcare facility compliance map
- `docs/animal/index.html` — Animal services map
- `docs/environment/index.html` — TCEQ spills + water quality (not yet exposed as Telegram command)
- `docs/crashes/index.html` — APD crash map (**client-side only**, fetches live from Socrata `y2wy-tgr5`; no Python generator)
- `docs/budget/index.html` — City budget spending
- `docs/court/index.html` — Austin court caseloads

**`scripts/generate_map.py`** — Generic map generator: `python scripts/generate_map.py <category>`. Each category is an entry in `CATEGORY_MAPS` that maps to a `generate_*_map()` function from the corresponding package.

**✅ Consolidated Workflows (as of 2026-04-30):**
- `generate-daily-open311-maps.yml` — Daily noon UTC: bicycle, traffic, animal, homeless maps
- `generate-all-open311-maps.yml` — Weekly: bicycle, graffiti, noise, parking, parks, traffic, animal maps
- `generate-all-open311-trends.yml` — Weekly Monday 13:00 UTC: graffiti, noise, homeless, parking trends
- `generate-all-socrata.yml` — Daily: crime map, crime trends, water, childcare, budget

**Map features (all point maps follow the same pattern):**
- 90 days of data; user filters to 30d / 60d / 90d via buttons
- Open / Closed status toggles
- Title bar updates dynamically with filtered count
- Popups: clickable ticket link, address, filed/updated dates, description (up to 500 chars)
- Mobile-friendly viewport meta tag

**Notes:**
- `AUSTINAPIKEY` must be set as a GitHub Actions secret for rate limit headroom
- 429 rate limit errors during local runs are normal without the token
- `.venv/` is the working virtualenv (system Python is externally managed)

## Caching System

**`open311_cache.py`** — Shared SQLite-based caching layer for Open311 API data.

- First run: Fetches all data from Open311 API, populates SQLite cache
- Subsequent runs: Loads cached data, only fetches new records since last fetch
- Cache persists via GitHub Actions cache (7-day retention)
- Cache location: `.cache/open311_cache.db` (gitignored)

**API:** `init_cache()`, `get_cached_records(category, since, service_codes)`, `cache_records(category, records)`, `get_last_fetch_date(category)`, `should_refresh_cache(category, max_age_hours)`

## Richer Map Popups

**Key finding:** The Open311 v2 API does NOT return a `description` field for some service codes (notably `ACBITE2`, `WILDEXPO`, `ACINFORM`). The 311 website at `https://311.austintexas.gov/tickets/<id>` shows an "Additional Details" section (`<dd class="mt-1 text-sm text-gray-900">` elements) with structured form answers.

**Implemented in `animalsvc/animal_bot.py`:** `_fetch_ticket_page_details(req_id)` and `_fetch_all_ticket_details(req_ids)` scrape these details using BeautifulSoup with `ThreadPoolExecutor(max_workers=15)`. Pattern can be replicated for graffiti, bicycle, homeless, noise, parking, parks maps.

## Crime Choropleth Map

`crime/crime_map.py` — `generate_crime_map()` fetches Socrata `fdj4-gpfu` grouped by `council_district` for 30/60/90-day windows; fetches district GeoJSON from City of Austin ArcGIS; builds a Leaflet choropleth injected into a Folium base map.

- Join key: `COUNCIL_DI` (integer 1–10) in GeoJSON matches Socrata `council_district` string
- Color scale: YlOrRd 5-step
- 30d/60d/90d buttons update polygon fill via `geoLayer.setStyle()` (no open/closed toggle — APD data has no status field)

## Service Code Notes

**Homeless service codes (optimized 2026-04-29):** PRGRDISS, OBSTMIDB, SBDEBROW, DRCHANEL, ATCOCIRW. The "Homeless - Violet Kiosk" and "Homelessness Matters" 311 categories reuse general maintenance codes — there are no unique homeless-specific service codes. Current 5 codes are optimal; APDNONNO and HHSGRAFF were excluded after analysis.
