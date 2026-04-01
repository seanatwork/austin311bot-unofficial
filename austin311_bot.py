#!/usr/bin/env python3
"""
Austin 311 Multi-Service Bot

One bot for all Austin 311 services:
- 🎨 Graffiti: Analysis, hotspots, remediation tracking
- 🚴 Bicycle: Lane complaints and infrastructure issues
- 🍽️ Restaurants: Inspection scores and search
- 🅿️ Parking: Open311 enforcement (coming soon)

Deploy with TELEGRAM_BOT_TOKEN environment variable.
"""

import asyncio
import os
import logging
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand

load_dotenv()
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# Graffiti service
from graffiti.config import Config as GraffitiConfig
from graffiti.graffiti_bot import (
    analyze_graffiti_command,
    hotspot_command,
    patterns_command,
)
from graffiti.remediation_analysis import (
    remediation_command,
    compare_command,
    trends_command,
)

# Bicycle service
from bicycle.bicycle_bot import (
    get_recent_complaints,
    get_stats,
    lookup_ticket,
    format_complaints,
    format_stats,
    format_ticket,
)

# Animal services
from animalsvc.animal_bot import (
    get_hotspots,
    get_stats as get_animal_stats,
    get_response_times,
    format_hotspots,
    format_stats as format_animal_stats,
    format_response_times,
)

# Infrastructure & Transportation service
from infrastructureandtransportation.traffic_bot import (
    get_hotspots as get_traffic_hotspots,
    get_stats as get_traffic_stats,
    get_response_times as get_traffic_response_times,
    format_hotspots as format_traffic_hotspots,
    format_stats as format_traffic_stats,
    format_response_times as format_traffic_response_times,
)

# Noise complaints service
from noisecomplaints.noise_bot import (
    get_hotspots as get_noise_hotspots,
    get_stats as get_noise_stats,
    get_response_times as get_noise_response_times,
    format_hotspots as format_noise_hotspots,
    format_stats as format_noise_stats,
    format_response_times as format_noise_response_times,
)

# Restaurant inspections service
from restaurants.restaurant_bot import (
    search_restaurants,
    get_lowest_scoring,
    get_grade_distribution,
    format_search_results,
    format_low_scores,
    format_grade_distribution,
)

logging.basicConfig(
    level=getattr(logging, GraffitiConfig.LOG_LEVEL),
    format=GraffitiConfig.LOG_FORMAT,
)
logger = logging.getLogger(__name__)


# =============================================================================
# HELPERS
# =============================================================================


async def _send_chunked(target, text: str, parse_mode: str = "Markdown") -> None:
    """Send long messages in ≤4000-char chunks (Telegram limit is 4096)."""
    chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)]
    for i, chunk in enumerate(chunks):
        if i == 0 and hasattr(target, "edit_message_text"):
            await target.edit_message_text(chunk, parse_mode=parse_mode)
        else:
            msg = target.message if hasattr(target, "message") else target
            await msg.reply_text(chunk, parse_mode=parse_mode)


# =============================================================================
# MAIN MENU
# =============================================================================


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [InlineKeyboardButton("🎨 Graffiti", callback_data="service_graffiti")],
        [InlineKeyboardButton("🚴 Bicycle", callback_data="service_bicycle")],
        [InlineKeyboardButton("🍽️ Restaurants", callback_data="service_restaurants")],
        [InlineKeyboardButton("🐾 Animal Services", callback_data="service_animal")],
        [InlineKeyboardButton("🚦 Traffic & Infrastructure", callback_data="service_traffic")],
        [InlineKeyboardButton("🔊 Noise Complaints", callback_data="service_noise")],
        [InlineKeyboardButton("🅿️ Parking", callback_data="service_parking")],
    ]
    await update.message.reply_text(
        "🏛️ *Welcome to Austin 311 Bot!*\n\nSelect a service:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    help_text = """🏛️ *AUSTIN 311 BOT*

🎨 *Graffiti:*
/graffiti — Analysis · hotspots · remediation · trends
_💡 Austin cleans graffiti on public property in ~4 days on average_

🐾 *Animal Services:*
/animal — Hotspots · stats · response times
_💡 Loose dog complaints are the most common 311 call in Austin_

🚴 *Bicycle:*
/bicycle — Recent complaints · stats
_💡 Austin has 100\+ miles of dedicated bike lanes — among the most in Texas_

🚦 *Traffic & Infrastructure:*
/traffic — Potholes · signals · street lights · sidewalks
_💡 Pothole repair is one of the top 311 categories in Austin_

🔊 *Noise Complaints:*
/noisecomplaints — Hotspots · stats · response times
_💡 Austin's 6th Street corridor generates some of the highest noise complaint volumes in the city_

🍽️ *Restaurants:*
/rest — Worst scores · grade report
/rest <name or address> — Search directly
_💡 Austin inspects every food establishment at least once a year_

🎫 *Ticket Lookup:*
/ticket <id> — Look up any 311 ticket by ID

ℹ️ /start — Main menu  |  /help — This message"""
    await update.message.reply_text(help_text, parse_mode="Markdown")


# =============================================================================
# SERVICE SUBMENUS (inline buttons)
# =============================================================================


async def service_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    service = query.data.replace("service_", "")
    await query.answer()

    if service == "graffiti":
        keyboard = [
            [InlineKeyboardButton("📊 Analyze", callback_data="graffiti_analyze"),
             InlineKeyboardButton("🗺️ Hotspots", callback_data="graffiti_hotspot")],
            [InlineKeyboardButton("⏰ Remediation", callback_data="graffiti_remediation"),
             InlineKeyboardButton("📈 Trends", callback_data="graffiti_trends")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_to_main")],
        ]
        text = "*🎨 Graffiti Analysis*\nPattern detection, hotspots, remediation tracking."

    elif service == "bicycle":
        keyboard = [
            [InlineKeyboardButton("📋 Recent", callback_data="bicycle_recent"),
             InlineKeyboardButton("📊 Stats", callback_data="bicycle_stats")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_to_main")],
        ]
        text = "*🚴 Bicycle Complaints*\nRecent complaints and statistics from Open311."

    elif service == "restaurants":
        keyboard = [
            [InlineKeyboardButton("💩 Worst Scores", callback_data="restaurants_lowscores"),
             InlineKeyboardButton("📊 Grade Report", callback_data="restaurants_grades")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_to_main")],
        ]
        text = "*🍽️ Restaurant Inspections*\nSearch by name/address or see worst scores.\n\nTo search, type: `/rest <name>`"

    elif service == "animal":
        keyboard = [
            [InlineKeyboardButton("🗺 Hotspots", callback_data="animal_hotspots"),
             InlineKeyboardButton("📊 Stats", callback_data="animal_stats")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_to_main")],
        ]
        text = "*🐾 Animal Services*\nLoose dogs, bites, vicious animals and more."

    elif service == "traffic":
        keyboard = [
            [InlineKeyboardButton("📊 Stats", callback_data="traffic_stats"),
             InlineKeyboardButton("🗺️ Hotspots", callback_data="traffic_hotspots")],
            [InlineKeyboardButton("⏰ Response Times", callback_data="traffic_response")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_to_main")],
        ]
        text = "*🚦 Traffic & Infrastructure*\nPotholes, signals, street lights, sidewalks, and more."

    elif service == "noise":
        keyboard = [
            [InlineKeyboardButton("📊 Stats", callback_data="noise_stats"),
             InlineKeyboardButton("🗺️ Hotspots", callback_data="noise_hotspots")],
            [InlineKeyboardButton("⏰ Response Times", callback_data="noise_response")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_to_main")],
        ]
        text = "*🔊 Noise Complaints*\nNon-emergency noise, outdoor venues, fireworks."

    elif service == "parking":
        keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]]
        text = "*🅿️ Parking Enforcement*\n\n⚠️ Coming soon."

    else:
        return

    await query.edit_message_text(
        text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def back_to_main(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("🎨 Graffiti", callback_data="service_graffiti")],
        [InlineKeyboardButton("🚴 Bicycle", callback_data="service_bicycle")],
        [InlineKeyboardButton("🍽️ Restaurants", callback_data="service_restaurants")],
        [InlineKeyboardButton("🐾 Animal Services", callback_data="service_animal")],
        [InlineKeyboardButton("🚦 Traffic & Infrastructure", callback_data="service_traffic")],
        [InlineKeyboardButton("🔊 Noise Complaints", callback_data="service_noise")],
        [InlineKeyboardButton("🅿️ Parking", callback_data="service_parking")],
    ]
    await query.edit_message_text(
        "🏛️ *Welcome to Austin 311 Bot!*\n\nSelect a service:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# =============================================================================
# GRAFFITI HANDLERS
# =============================================================================


async def graffiti_analyze_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Analyzing graffiti data...")
    try:
        result = analyze_graffiti_command(90)
        await _send_chunked(query, result)
    except Exception as e:
        logger.error(f"graffiti analyze: {e}")
        await query.edit_message_text(f"❌ Error: {e}")


async def graffiti_hotspot_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Finding hotspots...")
    try:
        result = hotspot_command()
        await _send_chunked(query, result)
    except Exception as e:
        logger.error(f"graffiti hotspot: {e}")
        await query.edit_message_text(f"❌ Error: {e}")


async def graffiti_remediation_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Analyzing remediation times...")
    try:
        result = remediation_command(90)
        await _send_chunked(query, result)
    except Exception as e:
        logger.error(f"graffiti remediation: {e}")
        await query.edit_message_text(f"❌ Error: {e}")


async def graffiti_trends_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Analyzing trends...")
    try:
        result = trends_command()
        await _send_chunked(query, result)
    except Exception as e:
        logger.error(f"graffiti trends: {e}")
        await query.edit_message_text(f"❌ Error: {e}")


async def graffiti_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [InlineKeyboardButton("📊 Analyze", callback_data="graffiti_analyze"),
         InlineKeyboardButton("🗺️ Hotspots", callback_data="graffiti_hotspot")],
        [InlineKeyboardButton("⏰ Remediation", callback_data="graffiti_remediation"),
         InlineKeyboardButton("📈 Trends", callback_data="graffiti_trends")],
    ]
    await update.message.reply_text(
        "*🎨 Graffiti Analysis*\nChoose a view:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# =============================================================================
# BICYCLE HANDLERS
# =============================================================================


async def bicycle_recent_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Fetching recent bicycle complaints...")
    try:
        complaints = get_recent_complaints(limit=10)
        result = format_complaints(complaints)
        await _send_chunked(query, result)
    except Exception as e:
        logger.error(f"bicycle recent: {e}")
        await query.edit_message_text(f"❌ Error: {e}")


async def bicycle_stats_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Fetching bicycle statistics...")
    try:
        stats = get_stats()
        result = format_stats(stats)
        await _send_chunked(query, result)
    except Exception as e:
        logger.error(f"bicycle stats: {e}")
        await query.edit_message_text(f"❌ Error: {e}")


async def bicycle_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [InlineKeyboardButton("📋 Recent", callback_data="bicycle_recent"),
         InlineKeyboardButton("📊 Stats", callback_data="bicycle_stats")],
    ]
    await update.message.reply_text(
        "*🚴 Bicycle Complaints*\nChoose a view:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def ticket_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: `/ticket <ticket-id>`\nExample: `/ticket 16-00123456`", parse_mode="Markdown")
        return
    ticket_id = context.args[0]
    await update.message.reply_text(f"🔍 Looking up ticket #{ticket_id}...")
    try:
        record = lookup_ticket(ticket_id)
        if not record:
            await update.message.reply_text(f"❌ No ticket found for #{ticket_id}. Check the ID and try again.")
            return
        await _send_chunked(update.message, format_ticket(record))
    except Exception as e:
        logger.error(f"ticket cmd: {e}")
        await update.message.reply_text(f"❌ Error: {e}")


# =============================================================================
# RESTAURANT HANDLERS
# =============================================================================


async def restaurants_lowscores_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Fetching worst inspection scores...")
    try:
        restaurants = get_lowest_scoring(10)
        await _send_chunked(query, format_low_scores(restaurants))
    except Exception as e:
        logger.error(f"restaurants lowscores: {e}")
        await query.edit_message_text(f"❌ Error: {e}")


async def restaurants_grades_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Loading grade report... (first load may take ~15s while fetching a full year of data)")
    try:
        data = get_grade_distribution()
        await _send_chunked(query, format_grade_distribution(data))
    except Exception as e:
        logger.error(f"restaurants grades: {e}")
        await query.edit_message_text(f"❌ Error: {e}")


async def restaurant_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.args:
        search_term = " ".join(context.args)
        await update.message.reply_text(f"🔍 Searching for: {search_term}...")
        try:
            results = search_restaurants(search_term)
            await _send_chunked(update.message, format_search_results(results, search_term))
        except Exception as e:
            logger.error(f"restaurant search cmd: {e}")
            await update.message.reply_text(f"❌ Error: {e}")
        return
    keyboard = [
        [InlineKeyboardButton("💩 Worst Scores", callback_data="restaurants_lowscores"),
         InlineKeyboardButton("📊 Grade Report", callback_data="restaurants_grades")],
    ]
    await update.message.reply_text(
        "*🍽️ Restaurant Inspections*\nChoose a view, or type `/rest <name>` to search:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# =============================================================================
# ANIMAL SERVICE HANDLERS
# =============================================================================


async def animal_hotspots_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Finding animal complaint hotspots...")
    try:
        await _send_chunked(query, format_hotspots(get_hotspots()))
    except Exception as e:
        logger.error(f"animal hotspots: {e}")
        await query.edit_message_text(f"❌ Error: {e}")


async def animal_stats_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Fetching animal complaint stats...")
    try:
        await _send_chunked(query, format_animal_stats(get_animal_stats()))
    except Exception as e:
        logger.error(f"animal stats: {e}")
        await query.edit_message_text(f"❌ Error: {e}")


async def animal_response_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Calculating response times...")
    try:
        await _send_chunked(query, format_response_times(get_response_times()))
    except Exception as e:
        logger.error(f"animal response: {e}")
        await query.edit_message_text(f"❌ Error: {e}")


async def animal_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [InlineKeyboardButton("🗺 Hotspots", callback_data="animal_hotspots"),
         InlineKeyboardButton("📊 Stats", callback_data="animal_stats")],
    ]
    await update.message.reply_text(
        "*🐾 Animal Services*\nChoose a view:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# =============================================================================
# TRAFFIC & INFRASTRUCTURE HANDLERS
# =============================================================================


async def traffic_stats_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Fetching infrastructure stats...")
    try:
        await _send_chunked(query, format_traffic_stats(get_traffic_stats()))
    except Exception as e:
        logger.error(f"traffic stats: {e}")
        await query.edit_message_text(f"❌ Error: {e}")


async def traffic_hotspots_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Finding infrastructure hotspots...")
    try:
        await _send_chunked(query, format_traffic_hotspots(get_traffic_hotspots()))
    except Exception as e:
        logger.error(f"traffic hotspots: {e}")
        await query.edit_message_text(f"❌ Error: {e}")


async def traffic_response_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Calculating response times...")
    try:
        await _send_chunked(query, format_traffic_response_times(get_traffic_response_times()))
    except Exception as e:
        logger.error(f"traffic response: {e}")
        await query.edit_message_text(f"❌ Error: {e}")


async def traffic_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [InlineKeyboardButton("📊 Stats", callback_data="traffic_stats"),
         InlineKeyboardButton("🗺️ Hotspots", callback_data="traffic_hotspots")],
        [InlineKeyboardButton("⏰ Response Times", callback_data="traffic_response")],
    ]
    await update.message.reply_text(
        "*🚦 Traffic & Infrastructure*\nChoose a view:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# =============================================================================
# NOISE COMPLAINT HANDLERS
# =============================================================================


async def noise_stats_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Fetching noise complaint stats...")
    try:
        await _send_chunked(query, format_noise_stats(get_noise_stats()))
    except Exception as e:
        logger.error(f"noise stats: {e}")
        await query.edit_message_text(f"❌ Error: {e}")


async def noise_hotspots_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Finding noise complaint hotspots...")
    try:
        await _send_chunked(query, format_noise_hotspots(get_noise_hotspots()))
    except Exception as e:
        logger.error(f"noise hotspots: {e}")
        await query.edit_message_text(f"❌ Error: {e}")


async def noise_response_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Calculating response times...")
    try:
        await _send_chunked(query, format_noise_response_times(get_noise_response_times()))
    except Exception as e:
        logger.error(f"noise response: {e}")
        await query.edit_message_text(f"❌ Error: {e}")


async def noisecomplaints_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [InlineKeyboardButton("📊 Stats", callback_data="noise_stats"),
         InlineKeyboardButton("🗺️ Hotspots", callback_data="noise_hotspots")],
        [InlineKeyboardButton("⏰ Response Times", callback_data="noise_response")],
    ]
    await update.message.reply_text(
        "*🔊 Noise Complaints*\nChoose a view:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# =============================================================================
# FALLBACK
# =============================================================================


async def echo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "❓ Unknown command. Type /help for available commands or /start for the menu."
    )


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Update {update} caused error {context.error}")


# =============================================================================
# APPLICATION SETUP
# =============================================================================


def create_application() -> Application:
    token = os.getenv("TELEGRAM_BOT_TOKEN") or GraffitiConfig.TELEGRAM_BOT_TOKEN
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN environment variable is not set.")

    app = Application.builder().token(token).build()

    # Core
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))

    # Inline menu navigation
    app.add_handler(CallbackQueryHandler(service_menu, pattern="^service_"))
    app.add_handler(CallbackQueryHandler(back_to_main, pattern="^back_to_main"))

    # Graffiti inline
    app.add_handler(CallbackQueryHandler(graffiti_analyze_cb, pattern="^graffiti_analyze"))
    app.add_handler(CallbackQueryHandler(graffiti_hotspot_cb, pattern="^graffiti_hotspot"))
    app.add_handler(CallbackQueryHandler(graffiti_remediation_cb, pattern="^graffiti_remediation"))
    app.add_handler(CallbackQueryHandler(graffiti_trends_cb, pattern="^graffiti_trends"))

    # Bicycle inline
    app.add_handler(CallbackQueryHandler(bicycle_recent_cb, pattern="^bicycle_recent"))
    app.add_handler(CallbackQueryHandler(bicycle_stats_cb, pattern="^bicycle_stats"))

    # Restaurant inline
    app.add_handler(CallbackQueryHandler(restaurants_lowscores_cb, pattern="^restaurants_lowscores"))
    app.add_handler(CallbackQueryHandler(restaurants_grades_cb, pattern="^restaurants_grades"))

    # Animal inline
    app.add_handler(CallbackQueryHandler(animal_hotspots_cb, pattern="^animal_hotspots"))
    app.add_handler(CallbackQueryHandler(animal_stats_cb, pattern="^animal_stats"))

    # Traffic inline
    app.add_handler(CallbackQueryHandler(traffic_stats_cb, pattern="^traffic_stats"))
    app.add_handler(CallbackQueryHandler(traffic_hotspots_cb, pattern="^traffic_hotspots"))
    app.add_handler(CallbackQueryHandler(traffic_response_cb, pattern="^traffic_response"))

    # Noise inline
    app.add_handler(CallbackQueryHandler(noise_stats_cb, pattern="^noise_stats"))
    app.add_handler(CallbackQueryHandler(noise_hotspots_cb, pattern="^noise_hotspots"))
    app.add_handler(CallbackQueryHandler(noise_response_cb, pattern="^noise_response"))

    # Graffiti slash command
    app.add_handler(CommandHandler("graffiti", graffiti_command))

    # Animal slash command
    app.add_handler(CommandHandler("animal", animal_command))

    # Bicycle slash commands
    app.add_handler(CommandHandler("bicycle", bicycle_command))
    app.add_handler(CommandHandler("ticket", ticket_command))

    # Restaurant slash command
    app.add_handler(CommandHandler("rest", restaurant_command))

    # Traffic slash command
    app.add_handler(CommandHandler("traffic", traffic_command))

    # Noise slash command
    app.add_handler(CommandHandler("noisecomplaints", noisecomplaints_command))

    # Fallback
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo_handler))
    app.add_error_handler(error_handler)

    # Register commands with Telegram so they appear in autocomplete
    async def post_init(application) -> None:
        await application.bot.set_my_commands([
            BotCommand("start",           "Main menu"),
            BotCommand("help",            "All commands"),
            BotCommand("graffiti",        "Graffiti — analysis · hotspots · remediation · trends"),
            BotCommand("animal",          "Animal complaints — hotspots · stats · response times"),
            BotCommand("bicycle",         "Bicycle complaints — recent · stats"),
            BotCommand("traffic",         "Traffic & infrastructure — potholes · signals · lights"),
            BotCommand("noisecomplaints", "Noise complaints — hotspots · stats · response times"),
            BotCommand("rest",            "Restaurant inspections — worst scores · grades · search"),
            BotCommand("ticket",          "Look up any 311 ticket by ID"),
        ])

    app.post_init = post_init

    return app


def main() -> None:
    logger.info("🤖 Starting Austin 311 Bot...")
    try:
        asyncio.set_event_loop(asyncio.new_event_loop())
        app = create_application()
        logger.info("✅ Bot started. Polling for updates...")
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    except KeyboardInterrupt:
        logger.info("👋 Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        raise


if __name__ == "__main__":
    main()
