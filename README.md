# ATX Pulse

A Telegram bot for exploring Austin 311 service data — graffiti, bicycle complaints, restaurant inspections, and animal services.

## Commands

| Command | Description |
|---|---|
| `/graffiti` | Analysis · hotspots · remediation times · trends |
| `/bicycle` | Recent complaints · statistics |
| `/rest` | Worst inspection scores · grade report |
| `/rest <name>` | Search by name or address |
| `/animal` | Hotspots · stats · response times |
| `/traffic` | Potholes · signals · street lights · sidewalks |
| `/noisecomplaints` | Noise hotspots · stats · response times |
| `/parks` | Park maintenance — hotspots · stats · resolution |
| `/ticket <id>` | Look up any 311 ticket by ID |

## Setup

```bash
git clone <repo-url>
cd austin311bot-unofficial
python -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env   # add your TELEGRAM_BOT_TOKEN
python austin311_bot.py
```

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Yes | From [@BotFather](https://t.me/BotFather) |
| `AUSTIN_APP_TOKEN` | No | Austin Open Data token (higher rate limits) |

## Data Sources

- [Austin Open311 API](https://austintexas.gov/department/311) — live service requests
- Austin Public Health restaurant inspection dataset

## License

MIT
