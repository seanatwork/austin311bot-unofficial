# 🎨 Graffiti Analysis Bot

A data-driven graffiti analysis and heatmapping bot for Austin 311 system.

## 🚀 Quick Start

### Run the Bot
```bash
cd graffitibot
python graffiti_bot.py
```

## 📊 Available Commands

### 1. Full Analysis
```bash
# Default 90-day analysis
python graffiti_bot.py

# Custom time period (e.g., 30 days)
python -c "from graffiti_bot import analyze_graffiti_command; print(analyze_graffiti_command(30))"
```

### 2. Hotspot Analysis
```bash
# Show all hotspots
python -c "from graffiti_bot import hotspot_command; print(hotspot_command())"

# Specific area (if implemented)
python -c "from graffiti_bot import hotspot_command; print(hotspot_command('downtown'))"
```

### 3. Pattern Analysis
```bash
# Last 30 days patterns
python -c "from graffiti_bot import patterns_command; print(patterns_command(30))"

# Last 14 days
python -c "from graffiti_bot import patterns_command; print(patterns_command(14))"
```

### 4. Help
```bash
python -c "from graffiti_bot import help_command; print(help_command())"
```

## 🧪 Testing the Bot

### Test Command 1: Basic Analysis
```bash
cd graffitibot
python -c "
import sys
sys.path.append('.')
from graffiti_bot import handle_command

# Test full analysis
result = handle_command('/analyze', ['90'])
print(result)
"
```

### Test Command 2: Hotspot Detection
```bash
python -c "
import sys
sys.path.append('.')
from graffiti_bot import handle_command

# Test hotspot analysis
result = handle_command('/hotspot', [])
print(result)
"
```

### Test Command 3: Pattern Analysis
```bash
python -c "
import sys
sys.path.append('.')
from graffiti_bot import handle_command

# Test pattern analysis
result = handle_command('/patterns', ['30'])
print(result)
"
```

### Test Command 4: Help System
```bash
python -c "
import sys
sys.path.append('.')
from graffiti_bot import handle_command

# Test help
result = handle_command('/help', [])
print(result)
"
```

## 🔧 Interactive Testing

For interactive testing, you can modify the bot to accept user input:

```python
# Add this to graffiti_bot.py main section:
if __name__ == "__main__":
    print("🎨 Graffiti Analysis Bot - Interactive Mode")
    print("=" * 50)
    print("Type 'quit' to exit")
    
    while True:
        try:
            user_input = input("\n🤖 Enter command: ").strip()
            if user_input.lower() in ['quit', 'exit', 'q']:
                print("👋 Goodbye!")
                break
            
            parts = user_input.split()
            command = parts[0] if parts else ''
            args = parts[1:] if len(parts) > 1 else []
            
            result = handle_command(command, args)
            print(result)
            
        except KeyboardInterrupt:
            print("\n👋 Goodbye!")
            break
        except Exception as e:
            print(f"❌ Error: {e}")
```

## 📊 Expected Outputs

### Analysis Output Example
```
🎨 GRAFFITI ANALYSIS REPORT
==================================================
📊 Total Records: 404
📋 Status Distribution:
   closed: 260 (64.4%)
   open: 144 (35.6%)
⏰ Temporal Patterns:
   Busiest day: Tue
   Busiest hour: 19:00
🗺️ Geographic Hotspots:
   1. 5 reports near (30.1997, -97.7838)
💡 Key Insights:
   🔴 High open rate: 35.6% (144 open reports)
```

### Hotspot Output Example
```
🗺️ GRAFFITI HOTSPOTS
==============================
1. 5 reports clustered
   📍 Location: (30.1997, -97.7838)
   🏠 Near: 414 Eberhart Ln, Austin
```

### Pattern Output Example
```
📈 RECENT GRAFFITI PATTERNS
===================================
📅 Period: Last 30 days
⏰ Hourly Distribution:
   14:00 ████████ 10
   16:00 ███████ 7
   18:00 █████████ 9
```

## 🕒 Remediation Analysis

Tracks how quickly graffiti reports are resolved, with period comparisons and monthly trends.

### Run the Demo
```bash
cd graffitibot
python remediation_analysis.py
```

### Individual Commands

#### Remediation Time Report
```bash
# Default 90-day analysis
python -c "from remediation_analysis import remediation_command; print(remediation_command())"

# Custom period (e.g., last 30 days)
python -c "from remediation_analysis import remediation_command; print(remediation_command(30))"
```

#### Compare Multiple Periods
```bash
# Compare 30 / 60 / 90 / 180 / 365 day windows side-by-side
python -c "from remediation_analysis import compare_command; print(compare_command())"
```

#### 6-Month Trend Analysis
```bash
# Show monthly averages and improving/declining trend
python -c "from remediation_analysis import trends_command; print(trends_command())"
```

#### Via handle_command Interface
```bash
python -c "
from remediation_analysis import handle_command
print(handle_command('/remediation', ['90']))   # 90-day report
print(handle_command('/compare', []))            # period comparison
print(handle_command('/trends', []))             # 6-month trends
"
```

### Expected Output
```
🕒 GRAFFITI REMEDIATION TIME ANALYSIS
============================================================
📅 Period: Last 90 days
📊 Total Records: 404
✅ Closed Records: 260

⏱️ Remediation Statistics:
   Average time: 8.3 days
   Median time: 5.0 days
   Fastest: 0 days
   Slowest: 58 days
   Consistency: ±9.2 days

📈 Time Distribution:
   Same day: 12 (4.6%)
   1-3 days: 68 (26.2%)
   4-7 days: 81 (31.2%)
   8-14 days: 55 (21.2%)
   15-30 days: 33 (12.7%)
   30+ days: 11 (4.2%)
```

### Metrics Calculated
| Metric | Description |
|--------|-------------|
| Average/Median days | Central tendency of resolution time |
| Fastest/Slowest | Extreme cases (capped at 60 days) |
| Consistency (±) | Standard deviation |
| Time distribution | Bucketed breakdown by speed of closure |
| Period comparison | Side-by-side table across 30/60/90/180/365 days |
| Monthly trend | Improving vs. declining remediation speed |

---

## 🐛 Troubleshooting

### Common Issues
1. **Database not found**: Ensure `../311_categories.db` exists
2. **No data**: Run `ingest_graffiti_data.py` first
3. **Import errors**: Check Python path and dependencies

### Debug Mode
```bash
# Check database connection
python -c "
import sqlite3
conn = sqlite3.connect('../311_categories.db')
cursor = conn.cursor()
cursor.execute('SELECT COUNT(*) FROM open311_requests WHERE service_code=\"HHSGRAFF\"')
count = cursor.fetchone()[0]
print(f'Graffiti records: {count}')
conn.close()
"
```

## 📱 Integration Notes

- **Database**: Uses existing `311_categories.db`
- **Service Code**: `HHSGRAFF` (Graffiti Abatement)
- **Data Source**: 404 records from past 90 days
- **Focus**: Analysis over reporting (secondary feature)

## 🚀 Next Steps

1. **Test all commands** using examples above
2. **Add interactive mode** for user testing
3. **Integrate with Telegram API** when ready
4. **Add photo upload** functionality
5. **Schedule data updates** with `ingest_graffiti_data.py`
