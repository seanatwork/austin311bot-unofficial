#!/usr/bin/env python3
"""
Austin 311 Multi-Service Bot

One bot for all Austin 311 services:
- 🎨 Graffiti: Analysis, hotspots, remediation tracking
- 🅿️ Parking: Open311 enforcement heatmap
- 🚴 Bicycle: Lane complaints and infrastructure issues
- 📊 General 311: Category analysis and city-wide trends

Deploy to Railway with TELEGRAM_BOT_TOKEN environment variable.
"""

import os
import logging
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# Import service modules
from graffitibot.config import Config as GraffitiConfig
from graffitibot.graffiti_bot import (
    analyze_graffiti_command,
    hotspot_command,
    patterns_command,
)
from graffitibot.remediation_analysis import (
    remediation_command,
    compare_command,
    trends_command,
)

# Configure logging
logging.basicConfig(
    level=getattr(logging, GraffitiConfig.LOG_LEVEL),
    format=GraffitiConfig.LOG_FORMAT,
)
logger = logging.getLogger(__name__)

# Database path
DB_PATH = GraffitiConfig.DB_PATH


# =============================================================================
# MAIN MENU
# =============================================================================


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show main service selection menu"""
    keyboard = [
        [InlineKeyboardButton("🎨 Graffiti", callback_data="service_graffiti")],
        [InlineKeyboardButton("🅿️ Parking", callback_data="service_parking")],
        [InlineKeyboardButton("🚴 Bicycle", callback_data="service_bicycle")],
        [InlineKeyboardButton("📊 General 311", callback_data="service_general")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    welcome_message = """
🏛️ *Welcome to Austin 311 Bot!*

I provide analysis and insights from Austin's 311 system.

*Select a service to get started:*
"""
    await update.message.reply_text(
        welcome_message,
        parse_mode="Markdown",
        reply_markup=reply_markup,
    )


async def service_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle service selection from main menu"""
    query = update.callback_query
    service = query.data.replace("service_", "")

    await query.answer()

    if service == "graffiti":
        keyboard = [
            [
                InlineKeyboardButton("📊 Analyze", callback_data="graffiti_analyze"),
                InlineKeyboardButton("🗺️ Hotspots", callback_data="graffiti_hotspot"),
            ],
            [
                InlineKeyboardButton("⏰ Remediation", callback_data="graffiti_remediation"),
                InlineKeyboardButton("📈 Trends", callback_data="graffiti_trends"),
            ],
            [InlineKeyboardButton("🔙 Back", callback_data="back_to_main")],
        ]
        title = "🎨 Graffiti Analysis Service"
        description = """
Analyze graffiti complaints from Austin 311:
• Pattern detection and trends
• Geographic hotspot identification
• Remediation time tracking
• Status distribution analysis
"""

    elif service == "parking":
        keyboard = [
            [
                InlineKeyboardButton("🗺️ View Heatmap", callback_data="parking_heatmap"),
                InlineKeyboardButton("📊 Analyze", callback_data="parking_analyze"),
            ],
            [InlineKeyboardButton("🔙 Back", callback_data="back_to_main")],
        ]
        title = "🅿️ Parking Enforcement Service"
        description = """
Visualize parking enforcement requests:
• Interactive heatmap of 311 requests
• Aggregate density analysis
• Time-based filtering
• Note: This shows requests, not citations
"""

    elif service == "bicycle":
        keyboard = [
            [
                InlineKeyboardButton("📊 Analyze", callback_data="bicycle_analyze"),
                InlineKeyboardButton("🗺️ Map", callback_data="bicycle_map"),
            ],
            [InlineKeyboardButton("🔙 Back", callback_data="back_to_main")],
        ]
        title = "🚴 Bicycle Complaints Service"
        description = """
Track bicycle infrastructure issues:
• Lane obstruction complaints
• Infrastructure condition reports
• Geographic distribution
• Temporal patterns
"""

    elif service == "general":
        keyboard = [
            [
                InlineKeyboardButton("📊 Categories", callback_data="general_categories"),
                InlineKeyboardButton("📈 Trends", callback_data="general_trends"),
            ],
            [InlineKeyboardButton("🔙 Back", callback_data="back_to_main")],
        ]
        title = "📊 General 311 Analysis"
        description = """
City-wide 311 data analysis:
• Service category breakdown
• Request volume trends
• Response time metrics
• Geographic distribution
"""
    else:
        return

    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        f"*{title}*\n{description}",
        parse_mode="Markdown",
        reply_markup=reply_markup,
    )


# =============================================================================
# GRAFFITI SERVICE HANDLERS
# =============================================================================


async def graffiti_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle graffiti analysis request"""
    query = update.callback_query
    await query.answer("⏳ Analyzing graffiti data...")
    await query.edit_message_text("⏳ Analyzing graffiti data...")

    try:
        days = int(context.args[0]) if context.args else 90
        result = analyze_graffiti_command(days)
        await send_long_message(query, result)
    except Exception as e:
        logger.error(f"Error in graffiti analysis: {e}")
        await query.edit_message_text(f"❌ Error: {str(e)}")


async def graffiti_hotspot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle graffiti hotspot request"""
    query = update.callback_query
    await query.answer("⏳ Finding hotspots...")
    await query.edit_message_text("⏳ Finding graffiti hotspots...")

    try:
        result = hotspot_command()
        await send_long_message(query, result)
    except Exception as e:
        logger.error(f"Error in graffiti hotspot: {e}")
        await query.edit_message_text(f"❌ Error: {str(e)}")


async def graffiti_remediation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle graffiti remediation request"""
    query = update.callback_query
    await query.answer("⏳ Analyzing remediation times...")
    await query.edit_message_text("⏳ Analyzing remediation times...")

    try:
        days = int(context.args[0]) if context.args else 90
        result = remediation_command(days)
        await send_long_message(query, result)
    except Exception as e:
        logger.error(f"Error in graffiti remediation: {e}")
        await query.edit_message_text(f"❌ Error: {str(e)}")


async def graffiti_trends(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle graffiti trends request"""
    query = update.callback_query
    await query.answer("⏳ Analyzing trends...")
    await query.edit_message_text("⏳ Analyzing graffiti trends...")

    try:
        result = trends_command()
        await send_long_message(query, result)
    except Exception as e:
        logger.error(f"Error in graffiti trends: {e}")
        await query.edit_message_text(f"❌ Error: {str(e)}")


# =============================================================================
# PARKING SERVICE HANDLERS
# =============================================================================


async def parking_heatmap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle parking heatmap request"""
    query = update.callback_query
    await query.answer()

    message = """
🅿️ *Parking Enforcement Heatmap*

To view the interactive heatmap:

1️⃣ **Web Dashboard:**
Open in your browser:
`https://your-railway-app.railway.app/parking`

2️⃣ **Streamlit App (Local):**
```bash
streamlit run tools/open311_parking_heatmap_app.py
```

3️⃣ **CLI Analysis:**
```bash
python tools/open311_aggregate_heatmap.py run --service-code <CODE>
```

*Note:* This shows 311 requests for parking enforcement, not actual citations.
"""
    await query.edit_message_text(message, parse_mode="Markdown")


async def parking_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle parking analysis request"""
    query = update.callback_query
    await query.answer("⏳ Analyzing parking data...")
    await query.edit_message_text("⏳ Analyzing parking enforcement data...")

    # TODO: Implement parking analysis
    message = """
🅿️ *Parking Analysis*

This feature is under development.

Coming soon:
• Request volume by area
• Peak request times
• Service code breakdown
• Response time metrics
"""
    await query.edit_message_text(message, parse_mode="Markdown")


# =============================================================================
# BICYCLE SERVICE HANDLERS
# =============================================================================


async def bicycle_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle bicycle analysis request"""
    query = update.callback_query
    await query.answer("⏳ Analyzing bicycle data...")
    await query.edit_message_text("⏳ Analyzing bicycle complaints...")

    # TODO: Implement bicycle analysis
    message = """
🚴 *Bicycle Complaints Analysis*

This feature is under development.

Coming soon:
• Complaint volume trends
• Infrastructure issue types
• Geographic distribution
• Resolution rates
"""
    await query.edit_message_text(message, parse_mode="Markdown")


async def bicycle_map(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle bicycle map request"""
    query = update.callback_query
    await query.answer()

    message = """
🚴 *Bicycle Complaints Map*

This feature is under development.

Coming soon:
• Interactive map of complaints
• Heatmap of problem areas
• Filter by issue type
• Time-based filtering
"""
    await query.edit_message_text(message, parse_mode="Markdown")


# =============================================================================
# GENERAL 311 SERVICE HANDLERS
# =============================================================================


async def general_categories(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle general categories request"""
    query = update.callback_query
    await query.answer("⏳ Analyzing 311 categories...")
    await query.edit_message_text("⏳ Analyzing 311 service categories...")

    # TODO: Implement general 311 analysis
    message = """
📊 *311 Category Analysis*

This feature is under development.

Coming soon:
• Top service categories
• Request volume by category
• Category trends over time
• Response time by category
"""
    await query.edit_message_text(message, parse_mode="Markdown")


async def general_trends(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle general trends request"""
    query = update.callback_query
    await query.answer("⏳ Analyzing 311 trends...")
    await query.edit_message_text("⏳ Analyzing city-wide 311 trends...")

    # TODO: Implement general 311 trends
    message = """
📈 *311 Trends Analysis*

This feature is under development.

Coming soon:
• Monthly request volume
• Seasonal patterns
• Emerging issues
• Resolution rate trends
"""
    await query.edit_message_text(message, parse_mode="Markdown")


# =============================================================================
# NAVIGATION & HELPERS
# =============================================================================


async def back_to_main(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Navigate back to main menu"""
    query = update.callback_query
    await query.answer()
    await start(update, context)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show help information"""
    help_text = """
🏛️ *AUSTIN 311 BOT HELP*

📊 *SERVICES:*

🎨 *Graffiti Analysis:*
/analyze [days] - Full analysis (default: 90)
/hotspot - Geographic hotspots
/remediation [days] - Remediation times
/trends - 6-month trends

🅿️ *Parking Enforcement:*
/parking_heatmap - View heatmap dashboard
/parking_analyze - Statistical analysis

🚴 *Bicycle Complaints:*
/bicycle_analyze - Complaint analysis
/bicycle_map - Interactive map

📊 *General 311:*
/categories - Service category breakdown
/trends - City-wide trends

ℹ️ *GENERAL:*
/start - Main menu
/help - This help message

💡 *TIPS:*
• Use inline buttons for easy navigation
• Most analyses accept optional [days] parameter
• View detailed heatmaps in web dashboard
"""
    await update.message.reply_text(help_text, parse_mode="Markdown")


async def echo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle unknown commands"""
    await update.message.reply_text(
        "❓ Unknown command. Type /help for available commands or use /start for the menu."
    )


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors"""
    logger.error(f"Update {update} caused error {context.error}")


async def send_long_message(query, message: str, max_length: int = 4000) -> None:
    """Send long messages in chunks (Telegram limit is 4096 chars)"""
    chunks = [message[i : i + max_length] for i in range(0, len(message), max_length)]

    for i, chunk in enumerate(chunks):
        if i == 0:
            await query.edit_message_text(chunk, parse_mode="Markdown")
        else:
            await query.message.reply_text(chunk, parse_mode="Markdown")


# =============================================================================
# COMMAND ALIASES (for direct command access)
# =============================================================================


async def analyze_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Direct /analyze command - defaults to graffiti analysis"""
    await update.message.reply_text("⏳ Analyzing graffiti data...")
    try:
        days = int(context.args[0]) if context.args else 90
        result = analyze_graffiti_command(days)
        for chunk in [result[i : i + 4000] for i in range(0, len(result), 4000)]:
            await update.message.reply_text(chunk, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error in analyze command: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")


async def hotspot_command_direct(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Direct /hotspot command"""
    await update.message.reply_text("⏳ Finding graffiti hotspots...")
    try:
        result = hotspot_command()
        for chunk in [result[i : i + 4000] for i in range(0, len(result), 4000)]:
            await update.message.reply_text(chunk, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error in hotspot command: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")


async def remediation_command_direct(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Direct /remediation command"""
    await update.message.reply_text("⏳ Analyzing remediation times...")
    try:
        days = int(context.args[0]) if context.args else 90
        result = remediation_command(days)
        for chunk in [result[i : i + 4000] for i in range(0, len(result), 4000)]:
            await update.message.reply_text(chunk, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error in remediation command: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")


# =============================================================================
# MAIN APPLICATION
# =============================================================================


def create_application() -> Application:
    """Create and configure the bot application"""
    token = GraffitiConfig.TELEGRAM_BOT_TOKEN

    if not token:
        raise ValueError(
            "TELEGRAM_BOT_TOKEN environment variable not set! "
            "Please set it in Railway dashboard or .env file."
        )

    logger.info("✅ Austin 311 Bot configuration loaded")

    application = Application.builder().token(token).build()

    # Main menu
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))

    # Service selection (inline buttons)
    application.add_handler(
        CallbackQueryHandler(service_menu, pattern="^service_")
    )

    # Navigation
    application.add_handler(
        CallbackQueryHandler(back_to_main, pattern="^back_to_main")
    )

    # Graffiti service
    application.add_handler(
        CallbackQueryHandler(graffiti_analyze, pattern="^graffiti_analyze")
    )
    application.add_handler(
        CallbackQueryHandler(graffiti_hotspot, pattern="^graffiti_hotspot")
    )
    application.add_handler(
        CallbackQueryHandler(graffiti_remediation, pattern="^graffiti_remediation")
    )
    application.add_handler(
        CallbackQueryHandler(graffiti_trends, pattern="^graffiti_trends")
    )

    # Parking service
    application.add_handler(
        CallbackQueryHandler(parking_heatmap, pattern="^parking_heatmap")
    )
    application.add_handler(
        CallbackQueryHandler(parking_analyze, pattern="^parking_parking_analyze")
    )

    # Bicycle service
    application.add_handler(
        CallbackQueryHandler(bicycle_analyze, pattern="^bicycle_analyze")
    )
    application.add_handler(
        CallbackQueryHandler(bicycle_map, pattern="^bicycle_map")
    )

    # General 311 service
    application.add_handler(
        CallbackQueryHandler(general_categories, pattern="^general_categories")
    )
    application.add_handler(
        CallbackQueryHandler(general_trends, pattern="^general_trends")
    )

    # Direct command aliases (for backward compatibility)
    application.add_handler(CommandHandler("analyze", analyze_command))
    application.add_handler(CommandHandler("hotspot", hotspot_command_direct))
    application.add_handler(CommandHandler("remediation", remediation_command_direct))
    application.add_handler(CommandHandler("trends", lambda u, c: graffiti_trends(u, c)))

    # Fallback for unknown text
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, echo_handler)
    )

    # Error handler
    application.add_error_handler(error_handler)

    return application


def main() -> None:
    """Start the bot"""
    logger.info("🤖 Starting Austin 311 Bot...")

    try:
        # Create and start the bot
        application = create_application()

        logger.info("✅ Bot started successfully. Polling for updates...")
        application.run_polling(allowed_updates=Update.ALL_TYPES)

    except KeyboardInterrupt:
        logger.info("👋 Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        raise


if __name__ == "__main__":
    main()
