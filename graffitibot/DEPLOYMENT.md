# Graffiti Analysis Bot - Railway Deployment Guide

## 🚀 Quick Deploy

### 1. Create a Telegram Bot

1. Open Telegram and search for `@BotFather`
2. Send `/newbot` command
3. Follow prompts to name your bot (e.g., "Austin Graffiti Bot")
4. Copy the **API token** (looks like: `123456789:ABCdefGHIjklMNOpqrsTUVwxyz`)

### 2. Deploy to Railway

#### Option A: GitHub Integration (Recommended)

1. Push your code to GitHub
2. Go to [railway.app](https://railway.app)
3. Click "New Project" → "Deploy from GitHub repo"
4. Select your repository
5. Add environment variables (see below)
6. Deploy!

#### Option B: Railway CLI

```bash
# Install Railway CLI
npm install -g @railway/cli

# Login
railway login

# Initialize project
railway init

# Add environment variables
railway variables set TELEGRAM_BOT_TOKEN=your_token_here
railway variables set DB_PATH=311_categories.db

# Deploy
railway up
```

### 3. Environment Variables

Set these in Railway dashboard (Variables tab):

| Variable | Description | Example |
|----------|-------------|---------|
| `TELEGRAM_BOT_TOKEN` | Your bot's API token from BotFather | `123456789:ABCdef...` |
| `DB_PATH` | Path to SQLite database | `311_categories.db` |
| `PYTHONUNBUFFERED` | Enable Python logging | `1` |

### 4. Database Handling (Auto-Ingest)

**The bot automatically ingests data on startup!** No manual database upload needed.

On each bot start/restart:
- Fetches last 90 days of graffiti data from Austin Open311 API
- Takes ~10-30 seconds depending on API response
- Stores data in SQLite database
- Bot becomes available after initialization completes

**Logs will show:**
```
🔄 Initializing database with latest graffiti data...
🎨 Ingesting graffiti data for service code: HHSGRAFF
✅ Database initialized successfully
✅ Bot started successfully. Polling for updates...
```

> **Note:** If you prefer to upload a pre-populated database instead of auto-ingest, you can use Railway Volumes (paid feature, ~$5/month). See "Alternative: Railway Volumes" below.

---

## 🧪 Testing Locally

### Run the Telegram Bot

```bash
cd graffitibot
export TELEGRAM_BOT_TOKEN=your_token_here
python telegram_bot.py
```

### Test Commands

Send these to your bot in Telegram:
- `/start` - Welcome message
- `/analyze` - Full 90-day analysis
- `/hotspot` - Show hotspots
- `/patterns 30` - Last 30 days patterns
- `/remediation` - Remediation time analysis
- `/compare` - Compare periods
- `/trends` - 6-month trends
- `/help` - All commands

---

## 💾 Alternative: Railway Volumes (Paid)

If you prefer to persist the database instead of auto-ingesting on each restart:

### Setup Railway Volumes

1. In Railway dashboard, go to your project
2. Click "Volumes" → "New Volume"
3. Mount path: `/app/data`
4. Update environment variable:
   ```
   DB_PATH=/app/data/311_categories.db
   ```

### Upload Database

```bash
# First, create the database locally
cd graffitibot
python ingest_graffiti_data.py

# Then upload to Railway volume
railway volume upload 311_categories.db
```

### Cost Consideration

- Railway Volumes cost **$5/month per GB**
- Not available on free trial plan
- Requires paid Hobby plan ($5/month base + volume costs)

**For free tier users, auto-ingest is recommended!**

---

## 📊 Current Analysis Capabilities

| Feature | Description |
|---------|-------------|
| `/analyze` | Status distribution, temporal patterns, geographic hotspots |
| `/hotspot` | Clustered locations with nearby addresses |
| `/patterns` | Hourly/day-of-week distribution charts |
| `/remediation` | Average/median time to close graffiti reports |
| `/compare` | Side-by-side comparison across time periods |
| `/trends` | Monthly trends showing improvement/decline |

---

## 🔧 Troubleshooting

### Bot doesn't respond
- Check `TELEGRAM_BOT_TOKEN` is set correctly
- Verify bot is not blocked by Telegram (some regions restrict bots)
- Check Railway logs for errors
- Wait for initialization to complete (~10-30 seconds after restart)

### Database not found
- Bot auto-creates database on first run
- Check `DB_PATH` environment variable
- Check Railway logs for ingestion errors

### No data in analysis
- Wait for auto-ingestion to complete on startup
- Check Railway logs for ingestion progress
- Verify service code `HHSGRAFF` exists in database
- Check Open311 API is accessible

### Slow startup
- Auto-ingestion takes 10-30 seconds (normal)
- API rate limiting causes delays (2 seconds between requests)
- Consider Railway Volumes for faster startup (paid feature)

---

## 📝 Code Improvements Made

1. **Telegram Integration** - `telegram_bot.py` for production deployment
2. **Environment Variables** - Configurable DB path and API token
3. **Error Handling** - Proper logging and error responses
4. **Message Chunking** - Split long responses for Telegram's 4096 char limit
5. **Railway Config** - `railway.json` and `nixpacks.toml` for deployment

---

## 🎯 Next Steps

1. **Add photo support** - Users can upload graffiti photos for analysis
2. **Scheduled ingestion** - Auto-update data daily via Railway cron
3. **Interactive buttons** - Inline keyboards for quick commands
4. **User subscriptions** - Allow users to subscribe to area-specific alerts
5. **Admin commands** - `/broadcast` for announcements, `/stats` for usage metrics
