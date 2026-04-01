# 🚀 Austin 311 Bot Quick Reference

## Deploy to Railway (5 minutes)

```bash
# 1. Get bot token from @BotFather on Telegram
# 2. Push to GitHub
git add . && git commit -m "Add austin311 bot deployment" && git push

# 3. Deploy on Railway
# - Go to railway.app → New Project → Deploy from GitHub
# - Set environment variables:
#   TELEGRAM_BOT_TOKEN=your_token_here
#   DB_PATH=311_categories.db
# - Deploy!
```

---

## Test Locally (2 minutes)

```bash
# Set environment
export TELEGRAM_BOT_TOKEN=your_token_here

# Run unified bot
python austin311_bot.py

# Or test CLI
python -c "from graffitibot.graffiti_bot import analyze_graffiti_command; print(analyze_graffiti_command(90))"
```

---

## Bot Architecture

The **Austin 311 Bot** unifies multiple services under one bot:

| Service | Status | Description |
|---------|--------|-------------|
| 🎨 Graffiti | ✅ Complete | Analysis, hotspots, remediation |
| 🅿️ Parking | 🚧 Coming Soon | Enforcement heatmap |
| 🚴 Bicycle | 🚧 Coming Soon | Lane complaints |
| 📊 General 311 | 🚧 Coming Soon | City-wide trends |

---

## Bot Commands

### Main Menu (Inline Buttons)
Type `/start` to see the interactive service menu with inline buttons for all services.

### Direct Commands (Graffiti)

| Command | Description |
|---------|-------------|
| `/start` | Main service menu |
| `/analyze [days]` | Graffiti analysis (default: 90 days) |
| `/hotspot` | Show geographic hotspots |
| `/remediation [days]` | Remediation time analysis |
| `/trends` | 6-month remediation trends |
| `/help` | All commands |

---

## CLI Commands

```bash
# Install package
pip install -e .

# Use CLI
graffiti-bot demo         # Demo analysis
graffiti-bot ingest       # Ingest data from Open311
graffiti-bot analyze      # Full analysis
graffiti-bot remediation  # Remediation analysis
graffiti-bot telegram     # Start Telegram bot
```

---

## Run Tests

```bash
cd graffitibot
pytest tests/ -v
```

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | ✅ Yes | Bot token from @BotFather |
| `DB_PATH` | ❌ No | Database path (default: `311_categories.db`) |
| `LOG_LEVEL` | ❌ No | Logging level (default: `INFO`) |

---

## Files Overview

| File | Purpose |
|------|---------|
| `austin311_bot.py` | 🆕 Unified multi-service bot |
| `graffitibot/telegram_bot.py` | Legacy graffiti-only bot |
| `graffitibot/graffiti_bot.py` | Core graffiti analysis logic |
| `graffitibot/remediation_analysis.py` | Remediation time tracking |
| `graffitibot/ingest_graffiti_data.py` | Data ingestion from Open311 |
| `graffitibot/config.py` | Configuration management |
| `tools/open311_parking_heatmap_app.py` | Parking heatmap dashboard |
| `dashboards/bicycle_dashboard.py` | Bicycle complaints dashboard |
| `DEPLOYMENT.md` | Full deployment guide |
| `CODE_REVIEW.md` | Code review & suggestions |

---

## Troubleshooting

**Bot doesn't respond:**
- Check `TELEGRAM_BOT_TOKEN` is set
- Verify bot isn't blocked by Telegram
- Check logs: `railway logs` or Railway dashboard

**Database not found:**
- Ensure database exists at `DB_PATH`
- Run `python graffitibot/ingest_graffiti_data.py` first
- On Railway, mount a volume for persistent storage

**No data in analysis:**
- Check service code `HHSGRAFF` exists in database
- Verify date range has data
- Re-run ingestion script

**Inline buttons not working:**
- Make sure you're using `/start` to open the menu
- Bot must be running to handle callback queries

---

## Next Steps

1. ✅ Get Telegram bot token from @BotFather
2. ✅ Run `python graffitibot/ingest_graffiti_data.py` to populate database
3. ✅ Test locally with `python austin311_bot.py`
4. ✅ Deploy to Railway
5. ✅ Upload database to Railway volume
6. ✅ Test all services in production
7. 🚧 Implement parking service (see `tools/open311_parking_heatmap_app.py`)
8. 🚧 Implement bicycle service (see `dashboards/bicycle_dashboard.py`)

---

*Quick start: `python austin311_bot.py`*
