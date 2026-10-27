# AGENTS.md

Guidance for AI coding agents working in this repository.

## Project Overview

Unofficial civic data site for Austin, TX — **[austin311.com](https://austin311.com)**, tracking what the city government is (and isn't) doing with publicly available open data.

Python generator scripts pull from the Open311 and Socrata APIs and produce **static HTML pages** (Folium maps, Chart.js trends, JSON snapshots) in `docs/`, which is served directly by **GitHub Pages**. GitHub Actions workflows regenerate the data on a schedule and commit the output back to the repo. There is no server-side application — the site is fully static (a few pages fetch live JSON client-side).

Note: several service modules keep historical `*_bot.py` filenames (e.g. `graffiti/graffiti_bot.py`). They are plain data modules for the site — treat the filename as an artifact, not a description. The Telegram bot and `austin311_bot.py` are gone; do not reintroduce bot code or bot-only env vars.

## Invariants

Break these and a weekly refresh silently produces wrong or empty pages:

1. **Never filter Open311 cache reads by the `category` column** — it is a provenance tag; filter by `service_codes`.
2. **Never switch trend fetchers back to `use_cache=False` full-year refetches** — use `open311_cache.fetch_monthly_with_cache()`.
3. **Never replace `actions/cache/restore` + `actions/cache/save` with `actions/cache@v4`** — the plain action discards the cache on red runs.
4. **Never hardcode API keys or tokens**; `AUSTINAPIKEY` and `CLOUDFLARE_API_TOKEN` come from secrets/env.
5. **Never commit generated `docs/` output by hand** — let the workflows write it.

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
1. Verify the change: `.venv/bin/python -m compileall -q .` plus regenerating the affected page (there is no test suite)
2. Commit with a conventional prefix: `feat:` for new features, `fix:` for bug fixes, `docs:` for docs
3. Push

## Commands

```bash
# Setup
python -m venv .venv && source .venv/bin/activate
pip install -e .

# Regenerate a page locally (writes docs/<category>/index.html)
python scripts/generate_map.py <category>
```

`.venv/` is the working virtualenv (system Python is externally managed).

**There is no test suite.** `graffiti/tests/` holds only `__init__.py` (`pytest graffiti/tests/` collects zero tests) and `pytest` is not a declared dependency. Verify a change by compiling and by regenerating the affected page:

```bash
.venv/bin/python -m compileall -q .            # syntax check
.venv/bin/python scripts/generate_map.py <category>   # then inspect the output + summary
```

**Categories** (keys of `CATEGORY_MAPS` in `scripts/generate_map.py`):

- Maps: `bicycle`, `graffiti`, `homeless`, `traffic`, `parking`, `crime`, `noise`, `parks`, `parks-hub`, `water`, `childcare`, `animal`, `dead-animal`, `storm`, `trees`, `cameras`
- Trends: `parking-trends`, `crime-trends`, `noise-trends`, `graffiti-trends`, `homeless-trends`, `storm-trends`, `parks-trends`, `animal-trends`, `dead-animal-trends`
- Other: `hate-crime`, `budget`, `shelter` (writes `docs/shelter/data.json`), `nearby` (maps to `docs/nearby/index.html`, which **no workflow generates** — the directory does not exist on the site)

Other standalone generators: `scripts/generate_911_data.py`, `generate_capmetro_data.py`, `generate_court_data.py`, `generate_covid_data.py`, `generate_fire_data.py`, `generate_fun_data.py`, `generate_nearby_page.py`, `generate_og_image.py`, `generate_pool_data.py`, `generate_pulse.py` (retired), `generate_query_data.py`, `generate_shelter_data.py`, `generate_weekly_digest.py`, `generate_wrapped_data.py`, plus the helpers `ci_report_failures.py`, `fetch_austin_parks.py`, `fetch_austin_zctas.py`.

## Data Refresh (Deployment)

No app server to deploy — "deployment" means regenerating the static output. Workflows in `.github/workflows/`:

| Workflow | Schedule | Generates |
|----------|----------|-----------|
| `weekly.yml` | Monday 14:00 UTC | Everything: every point/choropleth map (bicycle, graffiti, homeless, traffic, parking, crime, noise, parks + hub, water, childcare, animal, storm, trees, cameras), budget + hate-crime pages, all trends pages, pools, fire, shelter, court cache, querystore, fun data, 311 Wrapped |
| `weekly-digest.yml` | Monday 12:30 UTC | Weekly 311 digest (`generate_weekly_digest.py`) |
| `quarterly.yml` | 1st of Jan/Apr/Jul/Oct | 911 data |
| `deploy-worker.yml` | On push to `workers/query-gateway/**` | Cloudflare Worker (`workers/query-gateway`), needs `CLOUDFLARE_API_TOKEN` |

**Why one weekly job:** the Open311 generators gate on a ~6-day cache, so daily re-renders mostly rewrote week-old data. Day-sensitive leftovers (pools, fire, shelter, court, querystore) are cheap. `querystore` runs **last** on purpose: it refreshes the shared Open311 cache, so running it earlier would make the maps' 6-day gates skip their own refetch. Monday 14:00 UTC clears the 12:30 digest push. Actions cache expires after 7 days idle, so a lost cache means the next run backfills a full year (~76 min observed).

Each generator step is `continue-on-error: true` with a unique `id`, and the commit step is `if: always() && !cancelled()`, so one flaky source can't discard the other twenty steps' work. `scripts/ci_report_failures.py` runs last: it turns failed step outcomes into a job summary and exits non-zero, so the run goes red **after** good data is committed. `weekly.yml` accepts a `workflow_dispatch` `categories` input that guards only the `generate_map.py` steps; shared steps always run. `AUSTINAPIKEY` must be set as an Actions secret (429/403 throttling during keyless local runs is normal).

The Open311 cache uses `actions/cache/restore@v4` + `actions/cache/save@v4`, not plain `actions/cache@v4`: the plain action saves in a post step gated on `success()`, so a red run silently discarded the refreshed cache. `cache/save` writes inline and survives failures.

`quarterly.yml` needs a generous `timeout-minutes`: the 911 dataset is ~4.16M rows paged 5,000 at a time (~1s between chunks), so a 30-min cap cancels mid-fetch and writes nothing.

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
| `waterconservation/` | Open311 `WWREPORT` | `generate_water_map`, `get_water_conservation_stats` |
| `childcare/` | Socrata childcare facility inspections | `generate_childcare_map`, `get_childcare_stats` |
| `crime/` | Socrata `fdj4-gpfu` (APD), `i7fg-wrk5` (NIBRS), `t99n-5ib4` (hate crime) | `generate_crime_map`, `generate_hate_crime` |
| `capmetro/` | Socrata `tyfh-5r8s` (MetroBike trips) | `get_electric_vs_classic`, `get_kiosk_flow`, `get_membership_breakdown`, `KIOSK_LOCATIONS` |

### Map generators

All point-map generators share the same contract, wired up in `scripts/generate_map.py`'s `CATEGORY_MAPS`:

```python
generate_<category>_map(days_back: int = 90) -> tuple[Optional[io.BytesIO], str]
# Returns (BytesIO buffer with HTML, summary message). None buffer = failure.
```

`generate_map.py` picks `days_back` by category: 180 for `homeless`, 365 for any `*-trends`, 90 for everything else (including `traffic`); it injects a "Last ran" timestamp and writes the result to the path in `CATEGORY_MAPS`.

Each generator:
1. Fetches records (with optional SQLite caching)
2. Filters to valid coordinates (Austin bounding box: 30.0–30.5 lat, -98.0–-97.5 lon)
3. Buckets records into 30/60/90-day × open/closed FeatureGroups
4. Builds a Folium map with MarkerCluster, dynamic title bar, and filter buttons
5. Includes `og_meta_tags()` in `<head>`
6. Returns `(BytesIO buffer, summary string)`

Map features (all point maps): 90 days of data (180 for homeless) with 30d/60d/90d filter buttons, open/closed toggles, dynamic count in the title bar, popups with clickable ticket link/address/dates/description, mobile-friendly viewport.

### Trends modules

Eight packages ship trend pages (nine modules), all following `generate_<category>_trends(days_back=365)`:

| Module | Data Source | Output |
|--------|-------------|--------|
| `graffiti/trends.py` | Open311 HHSGRAFF via `fetch_graffiti_monthly()` | HTML with Chart.js line charts |
| `homeless/trends.py` | Open311 5 codes via `fetch_encampment_reports_monthly()` | HTML with SVG line chart |
| `noisecomplaints/trends.py` | Open311 3 codes via `fetch_noise_monthly()` | HTML with Chart.js bar/doughnut charts |
| `parking/trends.py` | Open311 PARKINGV via `fetch_parking_monthly()` | HTML with Chart.js + drill-down |
| `parks/trends.py` | Open311 park codes via `fetch_parks_monthly()` | HTML with Chart.js |
| `storm/trends.py` | Open311 8 codes via `fetch_storm_monthly()` | HTML with Chart.js |
| `animalsvc/trends.py` | Open311 animal codes via `fetch_animals_monthly()` | HTML with Chart.js |
| `animalsvc/dead_animal_trends.py` | Open311 dead-animal codes via `fetch_dead_animal_monthly()` | HTML with Chart.js |
| `crime/trends.py` | Socrata `fdj4-gpfu` via SoQL aggregation | HTML with Chart.js line charts |

Common pattern: fetch month-by-month (30-day windows) to avoid the Open311 pagination gotcha, aggregate into monthly buckets, emit a standalone HTML page with dark mode/responsive layout to `docs/<category>/trends/index.html`.

All Open311 trend fetchers (`fetch_*_monthly` in graffiti, homeless, noise, parking, parks, storm, animalsvc and `animalsvc/dead_animal_bot.py`) delegate to **`open311_cache.fetch_monthly_with_cache()`** — a shared cache-aware driver. It always fetches the current month (fresh to today) and only backfills past months that aren't already cached end-to-end (tracked per code-set + month). Do **not** switch trend generators back to `use_cache=False` full-year refetches: doing so re-downloads a 365-day window for every category each week, which repeatedly blew the CI `timeout-minutes` cap and froze the trend pages.

### Shared utilities

**`open311_client.py`**
- `open311_get(session, url, params, retries=0)` — GET with exponential backoff (up to 8 retries; 15s starting delay for 429, respects `Retry-After`).
- **Throttling shows up as 403, not 429.** A datacenter-IP burst gets 403 on every request for ~2–3 min, then recovers. `open311_get` retries 403 on a bounded 15s/30s/60s/120s ladder, then fails fast for `THROTTLE_COOLDOWN` rather than burning the job timeout. Callers tolerate a failed code.
- `og_meta_tags(slug="")` — Open Graph + Twitter Card meta tags for a docs page
- `SITE_BASE_URL` — `https://austin311.com`

**`open311_cache.py`** — SQLite caching layer for Open311 data. The cache mirrors raw Open311 records (one row per `service_request_id`, full record in `raw_json`); category semantics are applied at read/aggregation time, not at storage time. First run fetches everything; later runs only fetch new records since the last fetch. Cache lives at `.cache/open311_cache.db` (gitignored, persisted in CI via GitHub Actions cache with 7-day retention).
- API: `init_cache()`, `get_cached_records(service_codes, since)`, `cache_records(category, records)`, `get_last_fetch_date(service_codes)`, `should_refresh_cache(category, max_age_hours=24)`, `get_cache_stats(category)`, `clear_cache(category)`
- **`fetch_monthly_with_cache(...)`** — the shared cache-aware month-by-month fetcher used by every `fetch_*_monthly` (and therefore every Open311 trends page). Handles the rolling window, per-code-set month coverage markers (`mark_month_complete`/`missing_months`), always-refresh current month, dedupe, and slicing returned records to the requested window. `make_request` is a module callback; `keep` optionally filters returned records (used by homeless's encampment keyword filter — the cache still stores everything fetched).
- The `category` column on a row is only a provenance tag (which module cached it first) — service codes overlap across categories (e.g. `OBSTMIDB` is bicycle + homeless + traffic), so **never filter reads by category**; filter by `service_codes` instead.
- The water map is the one generator that also uses the cache as a *fallback data source* (`waterconservation/water_conservation_bot.py` caches its `WWREPORT` records and falls back to them when a throttled fetch returns nothing). `WWREPORT` belongs to no reporting category, so those rows are inert for every other reader.

**`categories.py`** — canonical reporting taxonomy: `CATEGORY_CODES` (category → Open311 service codes) and `CATEGORY_NAMES`. Single source of truth for the aggregation script (`generate_query_data.py`). Map packages keep their own broader code lists for their map's domain — e.g. the bicycle *map* shows 5 cycling-relevant ROW codes, but the Bicycle *reporting category* counts `PWBICYCL` only so volume comparisons stay honest. Categories are not a partition: the same ticket can count toward multiple categories. The `homeless` reporting category additionally applies the encampment keyword filter (`homeless.homeless_bot.is_encampment_report`) at aggregation time.

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

### Adding a page

1. Write `generate_<x>_page(days_back: int = 90) -> tuple[Optional[io.BytesIO], str]` in the owning package.
2. Register it in `scripts/generate_map.py`'s `CATEGORY_MAPS` (key → `(func, "docs-relative/path")`).
3. Add a `weekly.yml` step with a unique `id`, `continue-on-error: true`, and the same `categories`-input guard as its neighbours.
4. Add the page to the `docs/` table and the `scripts/` table here, and to the README page list.
5. Regenerate locally and confirm the returned summary is non-empty before pushing.

### Scripts (`scripts/`)

| Script | Purpose |
|--------|---------|
| `generate_map.py` | Generic map/trends generator — `python scripts/generate_map.py <category>` |
| `generate_budget.py` | City budget visualization |
| `generate_capmetro_data.py` | MetroBike trip analytics |
| `generate_court_data.py` | Court caseload data |
| `generate_covid_data.py` | COVID data page |
| `generate_fire_data.py` | Fire data snapshots |
| `generate_fun_data.py` | Fun page (bar of the month, coyote sightings, graffiti hall of fame) |
| `generate_911_data.py` | 911 call data (quarterly) |
| `generate_nearby_page.py` | "311 Near You" dynamic map |
| `generate_og_image.py` | Open Graph preview images |
| `generate_pool_data.py` | Pool data |
| `generate_pulse.py` | Real-time pulse JSON (`docs/pulse.json`) — **retired**, not wired into any workflow |
| `generate_shelter_data.py` | Shelter data JSON |
| `generate_weekly_digest.py` | Weekly 311 digest (`docs/complaints/`) + homepage ticker (`docs/homepage/ticker.json`) |
| `generate_query_data.py` | Pre-computed datasets for `docs/querystore/` (runs near-last in CI; slowest ~2 min) |
| `generate_wrapped_data.py` | 311 Wrapped per-ZIP page (`docs/wrapped/`) |
| `ci_report_failures.py` | Turns failed workflow step outcomes into a job summary; exits non-zero |
| `fetch_austin_parks.py` | Refreshes cached Austin parks GeoJSON |
| `fetch_austin_zctas.py` | Refreshes cached ZIP (ZCTA) boundaries |

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
| `capmetro/` | MetroBike trip analytics | `scripts.generate_capmetro_data.main()` — **not run by any workflow** |
| `childcare/` | Childcare compliance map | `childcare.childcare_bot.generate_childcare_map()` |
| `complaints/` | Weekly 311 digest | `scripts.generate_weekly_digest.main()` |
| `court/` | Court caseloads (+trends) | `scripts.generate_court_data.main()` |
| `covid/` | COVID data | `scripts.generate_covid_data.main()` — **not run by any workflow** |
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
| `911/` | 911 call data | `scripts.generate_911_data.main()` (quarterly) |
| `pulse.json` | Real-time pulse counters | **Retired.** `scripts.generate_pulse.main()` still exists but no workflow runs it; the homepage consumer is commented out ("disabled until counters are working") in `docs/index.html` |
| `ask/` | Question/answer page | Hand-written — **not regenerated by CI** |
| `homepage/ticker.json` | Homepage carousel | `scripts.generate_weekly_digest.main()` (via `weekly-digest.yml`) |
| `querystore/` | Pre-computed JSON datasets for the query engine | `scripts.generate_query_data.main()` |
| `wrapped/` | 311 Wrapped per-ZIP page | `scripts.generate_wrapped_data.main()` |
| `query-engine.js` | Client-side query engine consuming `querystore/` | Hand-written |

Rows flagged **not run by any workflow** or **retired** are hand-refreshed or dead; don't assume a weekly run updates them, and don't build on them without wiring a workflow step first.

## Query worker (`workers/query-gateway/`)

A Cloudflare Worker (`src/index.js`, `src/categories.js`, `wrangler.toml`) serving the query API behind `docs/ask/` and the `querystore` datasets. Deployed only by `deploy-worker.yml` on changes under that path, with `CLOUDFLARE_API_TOKEN`. `AUSTINAPIKEY` and `CLOUDFLARE_API_TOKEN` are the only secrets this repo needs.

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

## Open311 API — Pagination Gotcha

The API returns records **oldest first**, so a single 365-day request with `MAX_PAGES=10` only ever yields the newest 1,000 records at the *start* of the window — recent months go missing. Any `_fetch_code`-style fetch spanning more than ~90 days must go month-by-month through `open311_cache.fetch_monthly_with_cache()` (see the trends section).

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
