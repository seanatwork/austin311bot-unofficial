# Austin 311 Bot — Session Context

## What this project is

A single Telegram bot (`austin311_bot.py`) that routes users through inline keyboard menus to explore Austin 311 service data. Each service lives in its own Python module. The bot uses the Austin Open311 API (`https://311.austintexas.gov/open311/v2`) for live data.

---

## Changes made this session

### 1. Graffiti and Restaurants — consolidated to single root commands

**Before:** Graffiti had four separate slash commands (`/analyze`, `/hotspot`, `/remediation`, `/trends`). Restaurants had three (`/lowscores`, `/restaurant_grades`, `/restaurant`).

**After:** Both follow the same pattern as `/bicycle` and `/animal` — one root command opens an inline submenu.

- `/graffiti` → inline menu: Analyze · Hotspots · Remediation · Trends
- `/rest` → inline menu: Worst Scores · Grade Report. Passing a name searches directly: `/rest Amy's Ice Cream`
- All old individual commands removed.

### 2. Restaurant command renamed `/restaurant` → `/rest`

Shorter for search-by-name use: `/rest Juan in a Million`.

### 3. README rewritten

Replaced the old 243-line README (which described scraper tools, Streamlit dashboards, and PowerShell scripts that no longer exist) with a focused ~40-line README covering commands, setup, and environment variables.

### 4. Traffic & Infrastructure — new `/traffic` command

New module: `infrastructureandtransportation/traffic_bot.py`

Two buttons:
- **Infra Backlog** — fetches open complaints across 4 high-volume service codes (potholes, traffic signals, street lights, debris in street) using `status=open` API param. Shows count by type and oldest unresolved tickets with age in days.
- **Pothole Timer** — fetches 180 days of `SBPOTREP` records, calculates reported→closed repair times. Shows avg, median, fastest, a bucket breakdown (< 1 week / 1–2 weeks / 2–4 weeks / > 4 weeks), and the 5 slowest individual repairs with addresses.

Stats and response times were considered and removed as not useful.

Infra Backlog went through one rework: original version made 18 API calls (one per service code) over a 365-day window and did client-side status filtering — too slow and returned nothing. Reworked to 4 codes, 90-day window, `status=open` passed server-side.

### 5. Noise Complaints — new `/noisecomplaints` command

New module: `noisecomplaints/noise_bot.py`

Service codes: `APDNONNO` (non-emergency noise), `DSOUCVMC` (outdoor venue/music), `AFDFIREW` (fireworks).

Two buttons:
- **Hotspots** — top streets by complaint volume over 90 days
- **Peak Times** — fetches 56 days of data, shows peak hour per day of week with mini bar charts, plus an 8-week complaint volume trend. Headline format: "Peak: Saturdays around 11pm (42 complaints)". Uses Austin local time (UTC−6 offset).

Stats and response times were added then removed at user request — not meaningful for this service type.

### 6. Both new services added to `/start` main menu and `/help`

Main menu now shows: Graffiti · Bicycle · Restaurants · Animal Services · Traffic & Infrastructure · Noise Complaints · Parking (coming soon).

---

## Module structure

| Path | Purpose |
|---|---|
| `austin311_bot.py` | Main bot — all handlers, menus, routing |
| `graffiti/graffiti_bot.py` | Graffiti analysis, hotspots, patterns |
| `graffiti/remediation_analysis.py` | Remediation times, trends |
| `bicycle/bicycle_bot.py` | Bicycle complaint data and formatters |
| `animalsvc/animal_bot.py` | Animal complaint data and formatters |
| `restaurants/restaurant_bot.py` | Restaurant inspection data and formatters |
| `infrastructureandtransportation/traffic_bot.py` | Infra backlog, pothole repair timer |
| `noisecomplaints/noise_bot.py` | Noise complaint hotspots, peak time analysis |

---

## Bot commands (registered with Telegram)

| Command | Description |
|---|---|
| `/start` | Main menu |
| `/help` | All commands |
| `/graffiti` | Analysis · hotspots · remediation · trends |
| `/animal` | Hotspots · stats · response times |
| `/bicycle` | Recent complaints · stats |
| `/traffic` | Infra backlog · pothole timer |
| `/noisecomplaints` | Hotspots · peak times |
| `/rest` | Worst scores · grade report · search by name |
| `/ticket <id>` | Look up any 311 ticket by ID |

---

## Known issues / notes

- Austin Open311 API rate limits to ~10 req/min without an app token. `AUSTIN_APP_TOKEN` in `.env` raises this limit.
- Peak times in noise complaints uses a fixed UTC−6 offset for Austin local time (approximates CST; CDT would be −5).
- Infra Backlog is capped at 100 results per service code (API `per_page` limit).
