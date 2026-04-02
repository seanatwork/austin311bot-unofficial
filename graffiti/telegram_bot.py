#!/usr/bin/env python3
"""
Telegram Bot Interface for Graffiti Analysis

Deploy to Railway with TELEGRAM_BOT_TOKEN environment variable.
Auto-ingests graffiti data on startup for fresh data.
"""

import os
import logging
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

from .config import Config, setup_logging
from .graffiti_bot import handle_command as graffiti_handle_command
from .remediation_analysis import handle_command as remediation_handle_command

# Configure logging
setup_logging()
logger = logging.getLogger(__name__)

# Database path from config
DB_PATH = Config.DB_PATH


def initialize_database() -> None:
    """Ingest graffiti data on bot startup
    
    This ensures fresh data on every restart.
    Takes ~10-30 seconds depending on API response.
    """
    logger.info("🔄 Initializing database with latest graffiti data...")
    
    try:
        from ingest_graffiti_data import ingest_graffiti_last_90_days
        
        # Ingest last 90 days of graffiti data (verbose=False for clean logs)
        ingest_graffiti_last_90_days(DB_PATH, verbose=False)
        
        logger.info("✅ Database initialized successfully")
    except ImportError as e:
        logger.warning(f"⚠️ Ingestion module not found: {e}")
        logger.info("Continuing with existing database...")
    except Exception as e:
        logger.error(f"⚠️ Ingestion failed: {e}")
        logger.info("Continuing with existing data (if any)...")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command"""
    welcome_message = """
🎨 *Graffiti Analysis Bot*

I analyze graffiti complaints from Austin's 311 system to identify patterns, hotspots, and remediation times.

*Available Commands:*
/analyze - Full graffiti analysis
/hotspot - Show geographic hotspots
/patterns - Recent temporal patterns
/remediation - Remediation time analysis
/compare - Compare multiple periods
/help - All available commands

*Quick Start:*
Type /analyze to see the latest graffiti analysis!
"""
    await update.message.reply_text(welcome_message, parse_mode="Markdown")


async def graffiti_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle graffiti analysis commands"""
    args = context.args
    command = update.message.text.split()[0] if update.message.text else ""

    await update.message.reply_text("⏳ Analyzing graffiti data...")

    try:
        result = graffiti_handle_command(command, args)
        # Split long messages (Telegram limit is 4096 chars)
        for chunk in [result[i : i + 4000] for i in range(0, len(result), 4000)]:
            await update.message.reply_text(chunk, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error in graffiti command: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")


async def remediation_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle remediation analysis commands"""
    args = context.args
    command = update.message.text.split()[0] if update.message.text else ""

    await update.message.reply_text("⏳ Analyzing remediation times...")

    try:
        result = remediation_handle_command(command, args)
        for chunk in [result[i : i + 4000] for i in range(0, len(result), 4000)]:
            await update.message.reply_text(chunk, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error in remediation command: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help command"""
    help_text = """
🎨 *GRAFFITI ANALYSIS BOT HELP*

📊 *ANALYSIS COMMANDS:*
/analyze [days] - Full graffiti analysis (default: 90 days)
/hotspot - Show geographic hotspots
/patterns [days] - Recent temporal patterns (default: 30 days)

🕒 *REMEDIATION COMMANDS:*
/remediation [days] - Remediation time analysis (default: 90 days)
/compare - Compare multiple time periods

ℹ️ *GENERAL:*
/start - Welcome message
/help - This help message

📋 *EXAMPLES:*
`/analyze` - 90-day graffiti analysis
`/analyze 30` - Last 30 days only
`/hotspot` - Show all hotspots
`/patterns 14` - Last 2 weeks patterns
`/remediation` - 90-day remediation analysis
`/compare` - Compare 30/60/90/180 day periods

💡 *FEATURES:*
• Geographic hotspot identification
• Temporal pattern detection
• Remediation time tracking
• Status distribution analysis
• Address pattern recognition
"""
    await update.message.reply_text(help_text, parse_mode="Markdown")


async def echo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle unknown commands"""
    await update.message.reply_text(
        "❓ Unknown command. Type /help for available commands."
    )


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors"""
    logger.error(f"Update {update} caused error {context.error}")


def create_application() -> Application:
    """Create and configure the bot application"""
    token = Config.TELEGRAM_BOT_TOKEN

    if not token:
        raise ValueError(
            "TELEGRAM_BOT_TOKEN environment variable not set! "
            "Please set it in Railway dashboard or .env file."
        )

    logger.info("✅ Telegram bot configuration loaded")

    application = Application.builder().token(token).build()

    # Add command handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))

    # Graffiti analysis commands
    application.add_handler(
        CommandHandler(["analyze", "analysis"], graffiti_command)
    )
    application.add_handler(CommandHandler(["hotspot", "hotspots"], graffiti_command))
    application.add_handler(CommandHandler(["patterns", "pattern"], graffiti_command))

    # Remediation commands
    application.add_handler(
        CommandHandler(["remediation", "remedy"], remediation_command)
    )
    application.add_handler(CommandHandler("compare", remediation_command))

    # Fallback for unknown text
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, echo_handler)
    )

    # Error handler
    application.add_error_handler(error_handler)

    return application


def main() -> None:
    """Start the bot"""
    logger.info("🤖 Starting Graffiti Analysis Bot...")

    try:
        # Initialize database with fresh data on startup
        initialize_database()

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
