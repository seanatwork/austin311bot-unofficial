# 🚀 Graffitibot Quick Reference

## Deploy to Railway (5 minutes)

```bash
# 1. Get bot token from @BotFather on Telegram
# 2. Push to GitHub
git add . && git commit -m "Add graffitibot deployment" && git push

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
cd graffitibot

# Set environment
export TELEGRAM_BOT_TOKEN=your_token_here

# Run bot
python telegram_bot.py

# Or test CLI
python -c "from graffiti_bot import analyze_graffiti_command; print(analyze_graffiti_command(90))"
```

---

## Bot Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message |
| `/analyze [days]` | Full analysis (default: 90 days) |
| `/hotspot` | Show geographic hotspots |
| `/patterns [days]` | Temporal patterns (default: 30 days) |
| `/remediation [days]` | Remediation time analysis |
| `/compare` | Compare 30/60/90/180 day periods |
| `/trends` | 6-month trends |
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
| `telegram_bot.py` | Telegram bot interface |
| `graffiti_bot.py` | Core analysis logic |
| `remediation_analysis.py` | Remediation time tracking |
| `ingest_graffiti_data.py` | Data ingestion from Open311 |
| `config.py` | Configuration management |
| `DEPLOYMENT.md` | Full deployment guide |
| `CODE_REVIEW.md` | Code review & suggestions |

---

## Troubleshooting

**Bot doesn't respond:**
- Check `TELEGRAM_BOT_TOKEN` is set
- Verify bot isn't blocked by Telegram

**Database not found:**
- Ensure database exists at `DB_PATH`
- Run `python ingest_graffiti_data.py` first

**No data in analysis:**
- Check service code `HHSGRAFF` exists in database
- Verify date range has data

---

## Next Steps

1. ✅ Get Telegram bot token from @BotFather
2. ✅ Run `python ingest_graffiti_data.py` to populate database
3. ✅ Test locally with `python telegram_bot.py`
4. ✅ Deploy to Railway
5. ✅ Upload database to Railway volume
6. ✅ Test all commands in production

---

*Quick start: `python graffitibot/telegram_bot.py`*
