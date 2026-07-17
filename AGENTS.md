# AGENTS.md

Guidance for AI coding agents working in this repository.

## Project Overview

Unofficial civic data site for Austin, TX — **[austin311.com](https://austin311.com)**, tracking what the city government is (and isn't) doing with publicly available open data.

Python generator scripts pull from the Open311 and Socrata APIs and produce **static HTML pages** (Folium maps, Chart.js trends, JSON snapshots) in `docs/`, which is served directly by **GitHub Pages**. GitHub Actions workflows regenerate the data on a schedule and commit the output back to the repo. There is no server-side application — the site is fully static (a few pages fetch live JSON client-side).

Note: several service modules keep historical `*_bot.py` filenames (e.g. `graffiti/graffiti_bot.py`). They are plain data modules for the site — treat the filename as an artifact, not a description.

## Commit & Push Policy

**Push immediately (after verifying correctness) when:**
- Adding a new page, feature, or section to the site (e.g., new card on homepage, new data viz, new map)
- Fixing a broken feature or visual bug
- Making any change that would be visible to site visitors

**Don't push for:**
- Small iterative tweaks during an active conversation — batch them into one commit
- Documentation-only changes or internal notes
- Changes to config files that don't affect functionality

**Process:**
1. Verify the change is correct (read the file, check for syntax errors)
2. Commit with a conventional prefix: `feat:` for new features, `fix:` for bug fixes, `docs:` for docs
3. Push

## Commands

```bash
# Setup
python -m venv .venv && source .venv/bin/activate
pip install -e .

# Regenerate a page locally
python scripts/generate_map.py <category>

# Run the unit tests (only graffiti/ has tests)
python -m pytest graffiti/tests/
```

`.venv/` is the working virtualenv (system Python is externally managed).

**Categories** (keys of `CATEGORY_MAPS` in `scripts/generate_map.py`):

- Maps: `bicycle`, `graffiti`, `homeless`, `traffic`, `parking`, `crime`, `noise`, `parks`, `parks-hub`, `water`, `childcare`, `animal`, `dead-animal`, `storm`, `trees`, `cameras`
- Trends: `parking-trends`, `crime-trends`, `noise-trends`, `graffiti-trends`, `homeless-trends`, `storm-trends`, `parks-trends`, `animal-trends`, `dead-animal-trends`
- Other: `hate-crime`, `budget`, `nearby`, `shelter`

Other standalone generators: `scripts/generate_911_data.py`, `generate_capmetro_data.py`, `generate_card_stats.py`, `generate_court_data.py`, `generate_covid_data.py`, `generate_fire_data.py`, `generate_fun_data.py`, `generate_og_image.py`, `generate_pool_data.py`, `generate_pulse.py`, `generate_weekly_digest.py`.

## Data Refresh (Deployment)

No app server to deploy — "deployment" means regenerating the static output. Workflows in `.github/workflows/`:

| Workflow | Schedule | Generates |
|----------|----------|-----------|
| `daily.yml` | Daily noon UTC | bicycle, traffic, animal, homeless, crime (+trends), water, childcare, budget, hate-crime maps; pools, fire, shelter, court, traffic cameras, homepage card stats |
| `weekly.yml` | Monday noon UTC | graffiti, noise, parking, parks (+hub), storm, trees maps; all trends pages; fun data |
| `weekly-digest.yml` | Monday 12:30 UTC | Weekly 311 digest (`generate_weekly_digest.py`) |
| `quarterly.yml` | 1st of Jan/Apr/Jul/Oct | 911 data |

All workflows support `workflow_dispatch` with an optional `categories` input, restore the Open311 cache from GitHub Actions cache, and commit results back to `main`. `AUSTINAPIKEY` must be set as a GitHub Actions secret for rate-limit headroom (429s during local runs without it are normal).

## Architecture

### Service packages

Each package is independent and owns one data domain. Its `__init__.py` re-exports the public API.

| Package | Data | Key Exports |
|---------|------|-------------|
| `graffiti/` | Open311 `HHSGRAFF` | `generate_graffiti_map`, `analyze_graffiti_command`, `patterns_command`, `Config` |
| `bicycle/` | Open311, 8 codes (PWBICYCL, OBSTMIDB, …) | `generate_bicycle_map`, `get_recent_complaints`, `get_stats`, `lookup_ticket` |
| `homeless/` | Open311, 5 codes (PRGRDISS, OBSTMIDB, SBDEBROW, DRCHANEL, ATCOCIRW) | `generate_homeless_map`, `get_encampment_stats`, `format_encampment_stats` |
| `animalsvc/` | Open311, 7+ codes (loose dogs, bites, coyotes, dead animals) | `generate_animal_map`, `generate_dead_animal_map`, `get_hotspots`, `get_stats` |
| `infrastructureandtransportation/` | Open311 (potholes, signals, sidewalks) | `generate_traffic_map` + traffic/infra stats functions |
| `noisecomplaints/` | Open311 `NOISECMP` | `generate_noise_map`, `get_hotspots`, `get_peak_times` |
| `parking/` | Open311 `PARKINGV` | `generate_parking_map`, `get_stats`, `get_hotspots` |
| `parks/` | Open311 park maintenance | `generate_parks_map`, `get_park_stats`, `get_park_hotspots` |
| `storm/` | Open311, 8 codes (debris/drainage/flooding/erosion) | `generate_storm_map`, `storm_stats` |
| `trees/` | Open311 tree service codes | `generate_tree_map`, `get_tree_stats` |
| `trafficcameras/` | Austin traffic camera feeds | `generate_cameras_map`, `fetch_cameras`, `get_camera_stats` |
| `restaurants/` | Socrata `ecmv-9xxi` (health inspections) | `get_restaurant_stats`, `format_restaurant_stats` |
| `waterconservation/` | Socrata water conservation violations | `generate_water_map`, `get_water_conservation_stats` |
| `childcare/` | Socrata childcare facility inspections | `generate_childcare_map`, `get_childcare_stats` |
| `crime/` | Socrata `fdj4-gpfu` (APD), `i7fg-wrk5` (NIBRS), `t99n-5ib4` (hate crime) | `generate_crime_map`, `generate_hate_crime` |
| `capmetro/` | Socrata `tyfh-5r8s` (MetroBike trips) | `get_electric_vs_classic`, `get_kiosk_flow`, `get_membership_breakdown`, `KIOSK_LOCATIONS` |

### Map generators

All point-map generators share the same contract, wired up in `scripts/generate_map.py`'s `CATEGORY_MAPS`:

```python
generate_<category>_map(days_back: int = 90) -> tuple[Optional[io.BytesIO], str]
# Returns (BytesIO buffer with HTML, summary message). None buffer = failure.
```

`generate_map.py` picks `days_back` by category: 30 for traffic, 180 for homeless, 365 for `*-trends`, 90 otherwise; it injects a "Last ran" timestamp and writes the result to `docs/<category>/index.html`.

Each generator:
1. Fetches records (with optional SQLite caching)
2. Filters to valid coordinates (Austin bounding box: 30.0–30.5 lat, -98.0–-97.5 lon)
3. Buckets records into 30/60/90-day × open/closed FeatureGroups
4. Builds a Folium map with MarkerCluster, dynamic title bar, and filter buttons
5. Includes `og_meta_tags()` in `<head>`
6. Returns `(BytesIO buffer, summary string)`

Map features (all point maps): 90 days of data with 30d/60d/90d filter buttons, open/closed toggles, dynamic count in the title bar, popups with clickable ticket link/address/dates/description, mobile-friendly viewport.

### Trends modules

Five+ packages have a `trends.py` for historical aggregation, all following `generate_<category>_trends(days_back=365)`:

| Module | Data Source | Output |
|--------|-------------|--------|
| `graffiti/trends.py` | Open311 HHSGRAFF via `fetch_graffiti_monthly()` | HTML with Chart.js line charts |
| `homeless/trends.py` | Open311 5 codes via `fetch_encampment_reports_monthly()` | HTML with SVG line chart |
| `noisecomplaints/trends.py` | Open311 3 codes via `fetch_noise_monthly()` | HTML with Chart.js bar/doughnut charts |
| `parking/trends.py` | Open311 PARKINGV via `fetch_parking_monthly()` | HTML with Chart.js + drill-down |
| `crime/trends.py` | Socrata `fdj4-gpfu` via SoQL aggregation | HTML with Chart.js line charts |

Common pattern: fetch month-by-month (30-day windows) to avoid the Open311 pagination gotcha, aggregate into monthly buckets, emit a standalone HTML page with dark mode/responsive layout to `docs/<category>/trends/index.html`.

### Shared utilities

**`open311_client.py`**
- `open311_get(session, url, params, retries=0)` — GET with exponential backoff (up to 8 retries; 15s starting delay for 429, respects `Retry-After`)
- `og_meta_tags(slug="")` — Open Graph + Twitter Card meta tags for a docs page
- `SITE_BASE_URL` — `https://austin311.com`

**`open311_cache.py`** — SQLite caching layer for Open311 data. First run fetches everything; later runs only fetch new records since the last fetch. Cache lives at `.cache/open311_cache.db` (gitignored, persisted in CI via GitHub Actions cache with 7-day retention).
- API: `init_cache()`, `get_cached_records(category, since, service_codes)`, `cache_records(category, records)`, `get_last_fetch_date(category)`, `should_refresh_cache(category, max_age_hours=24)`, `get_cache_stats(category)`, `clear_cache(category)`

### Common code patterns

**HTTP session singleton** — every module that makes HTTP requests:
```python
_session: Optional[requests.Session] = None

def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({"Accept": "application/json", "User-Agent": "austin311bot/0.1"})
    return _session
```

**Open311 pagination (`_fetch_code`)** — all Open311 fetchers follow this shape:
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

**Date/time helpers** (consistent across modules): `_utc_now()`, `_isoformat_z(dt)`, `_format_central_time()`, `_age_days(record)`.

### Scripts (`scripts/`)

| Script | Purpose |
|--------|---------|
| `generate_map.py` | Generic map/trends generator — `python scripts/generate_map.py <category>` |
| `generate_budget.py` | City budget visualization |
| `generate_capmetro_data.py` | MetroBike trip analytics |
| `generate_card_stats.py` | Homepage card stats JSON (`docs/homepage/`) |
| `generate_court_data.py` | Court caseload data |
| `generate_covid_data.py` | COVID data page |
| `generate_fire_data.py` | Fire data snapshots |
| `generate_fun_data.py` | Fun page (bar of the month, coyote sightings, graffiti hall of fame) |
| `generate_911_data.py` | 911 call data (quarterly) |
| `generate_nearby_page.py` | "311 Near You" dynamic map |
| `generate_og_image.py` | Open Graph preview images |
| `generate_pool_data.py` | Pool data |
| `generate_pulse.py` | Real-time pulse JSON (`docs/pulse.json`) |
| `generate_shelter_data.py` | Shelter data JSON |
| `generate_weekly_digest.py` | Weekly 311 digest (`docs/complaints/`) |

### Tools (`tools/`)

| Script | Purpose |
|--------|---------|
| `search_311_categories.py` | Browse/search all available 311 service codes (CLI entrypoint: `search-311`) |
| `discover_homeless_codes.py` | Discover homeless-related 311 service codes |

## Static Website (`docs/`)

The `docs/` folder is the GitHub Pages site at https://austin311.com/.

| Path | Content | Generator |
|------|---------|-----------|
| `index.html` | Landing page hub | Manual (hand-written) |
| `animal/` | Animal services map | `animalsvc.animal_bot.generate_animal_map()` |
| `animal/dead/` | Dead animal collection map | `animalsvc.dead_animal_bot.generate_dead_animal_map()` |
| `bicycle/` | Bicycle infrastructure map | `bicycle.bicycle_bot.generate_bicycle_map()` |
| `budget/` | City budget spending | `scripts.generate_budget.main()` |
| `cameras/` | Traffic cameras map | `trafficcameras.cameras_bot.generate_cameras_map()` |
| `capmetro/` | MetroBike trip analytics | `scripts.generate_capmetro_data.main()` |
| `childcare/` | Childcare compliance map | `childcare.childcare_bot.generate_childcare_map()` |
| `complaints/` | Weekly 311 digest | `scripts.generate_weekly_digest.main()` |
| `court/` | Court caseloads (+trends) | `scripts.generate_court_data.main()` |
| `covid/` | COVID data | `scripts.generate_covid_data.main()` |
| `crashes/` | Crash map (client-side only) | Fetches live from Socrata `y2wy-tgr5` |
| `crime/` | Crime choropleth (+trends, hate crime) | `crime.crime_map.generate_crime_map()` |
| `environment/` | TCEQ spills + water quality | Manual (hand-written HTML) |
| `fire/` | Fire incidents | `scripts.generate_fire_data.main()` |
| `fun/` | Fun data (bars, coyotes) | `scripts.generate_fun_data.main()` |
| `graffiti/` | Graffiti abatement map (+trends) | `graffiti.graffiti_bot.generate_graffiti_map()` |
| `homeless/` | Homeless encampment map (+trends) | `homeless.homeless_bot.generate_homeless_map()` |
| `noise/` | Noise complaint map (+trends) | `noisecomplaints.noise_bot.generate_noise_map()` |
| `parking/` | Parking enforcement map (+trends) | `parking.parking_bot.generate_parking_map()` |
| `parks/` | Parks maintenance map (+trends) | `parks.parks_bot.generate_parks_map()` |
| `pools/` | Pool data | `scripts.generate_pool_data.main()` |
| `restaurants/` | Restaurant inspection map | Manual (hand-written HTML) |
| `shelter/` | Shelter data | `scripts.generate_shelter_data.main()` |
| `storm/` | Storm debris/drainage map (+trends) | `storm.storm_bot.generate_storm_map()` |
| `traffic/` | Traffic & infrastructure map | `infrastructureandtransportation.traffic_bot.generate_traffic_map()` |
| `trees/` | Tree service map | `trees.trees_bot.generate_tree_map()` |
| `water/` | Water conservation map | `waterconservation.water_conservation_bot.generate_water_map()` |
| `911/` | 911 call data | `scripts.generate_911_data.main()` |
| `pulse.json` | Real-time pulse data | `scripts.generate_pulse.main()` |

## Data Sources

| Source | Base URL | Used for |
|--------|----------|----------|
| Open311 API | `https://311.austintexas.gov/open311/v2` | All 311 service requests |
| Socrata API | `data.austintexas.gov` | Restaurant inspections, crime, crashes, water, childcare, MetroBike |
| Texas HHSC/TABC Socrata | `data.texas.gov` | Child care licensing (`bc5r-88dy`, `tqgd-mf4x`), mixed beverage sales (`g5bj-yb6k`) |
| City of Austin ArcGIS | `services.arcgis.com/...` | Council district GeoJSON for crime choropleth |

Key Socrata datasets:

| Dataset | ID | Used for |
|---------|-----|----------|
| APD Crime Reports | `fdj4-gpfu` | Crime choropleth, crime trends |
| NIBRS Homicides | `i7fg-wrk5` | Homicide data |
| Hate Crime Incidents | `t99n-5ib4` | Hate crime trends (`crime/hate_crime.py`) |
| Real-Time Traffic Incidents | `dx9v-zd7x` | Live traffic incidents |
| Crash Report Data | `y2wy-tgr5` | Crash map/stats |
| Austin MetroBike Trips | `tyfh-5r8s` | Bike trip analytics (`capmetro/`) |
| TABC Mixed Beverage | `g5bj-yb6k` | Bar of the month (fun page) |
| Building Permits | `3syk-w9eu` | Permit activity |
| Surface Water Quality | `5tye-7ray` | Water quality |
| Parking Meter Transactions | `5bb2-gtef` | Parking pulse (24h activity) |

Open311: ISO8601 dates with `Z` suffix, `per_page`/`page` pagination.
Socrata: `$where` SoQL filtering, `$group`/`$select` aggregation.

**Environment variables:** `AUSTINAPIKEY` (used for both the Socrata app token and the Open311 API key — same value, same GitHub Actions secret). No other secrets are required for site generation.

## Open311 API — Known Pagination Gotcha

**The API returns records in chronological order (oldest first).** A single request with `start_date` 365 days ago and `end_date` today returns the *oldest* records first — with `MAX_PAGES=10` (1,000 records) you only see records from the start of the window, never recent months.

**Fix (see `homeless/trends.py`):** fetch month by month — one 30-day window per API call — so each request is small enough that all records for that period are returned.

**Applies to:** any `_fetch_code`-style function across bicycle, graffiti, homeless, noise, parking, parks, storm modules if they ever need historical data beyond 90 days.

## Richer Map Popups

The Open311 v2 API does NOT return a `description` field for some service codes (notably `ACBITE2`, `WILDEXPO`, `ACINFORM`). The 311 website at `https://311.austintexas.gov/tickets/<id>` shows an "Additional Details" section (`<dd class="mt-1 text-sm text-gray-900">` elements) with structured form answers.

**Implemented in `animalsvc/animal_bot.py`:** `_fetch_ticket_page_details(req_id)` and `_fetch_all_ticket_details(req_ids)` scrape these details using BeautifulSoup with `ThreadPoolExecutor(max_workers=15)`. The pattern can be replicated for graffiti, bicycle, homeless, noise, parking, parks maps.

## Crime Choropleth Map

`crime/crime_map.py` — `generate_crime_map()` fetches Socrata `fdj4-gpfu` grouped by `council_district` for 30/60/90-day windows; fetches district GeoJSON from City of Austin ArcGIS; builds a Leaflet choropleth injected into a Folium base map.

- Join key: `COUNCIL_DI` (integer 1–10) in GeoJSON matches Socrata `council_district` string
- Color scale: YlOrRd 5-step
- 30d/60d/90d buttons update polygon fill via `geoLayer.setStyle()` (no open/closed toggle — APD data has no status field)

## Service Code Notes

**Homeless service codes (optimized 2026-04-29):** PRGRDISS, OBSTMIDB, SBDEBROW, DRCHANEL, ATCOCIRW. The "Homeless - Violet Kiosk" and "Homelessness Matters" 311 categories reuse general maintenance codes — there are no unique homeless-specific service codes. APDNONNO and HHSGRAFF were excluded after analysis (too many false positives).
