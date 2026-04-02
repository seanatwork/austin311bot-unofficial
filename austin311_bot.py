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
    patterns_command,
)
from graffiti.remediation_analysis import (
    remediation_command,
    compare_command,
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
    get_infra_backlog,
    format_infra_backlog,
    build_backlog_keyboard,
    get_pothole_repair_times,
    format_pothole_repair_times,
)

# Noise complaints service
from noisecomplaints.noise_bot import (
    get_hotspots as get_noise_hotspots,
    format_hotspots as format_noise_hotspots,
    get_peak_times as get_noise_peak_times,
    format_peak_times as format_noise_peak_times,
)

# Parking enforcement service
from parking.parking_bot import (
    get_stats as get_parking_stats,
    get_hotspots as get_parking_hotspots,
    format_stats as format_parking_stats,
    format_hotspots as format_parking_hotspots,
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

# Crime stats service
from crime.crime_bot import (
    get_recent_crime_stats,
    format_crime_stats,
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

🅿️ *Parking:*
/parking — Citations · hot zones · stats
_💡 Parking enforcement is one of the top 10 most-requested 311 services in Austin_

🍽️ *Restaurants:*
/rest — Worst scores · grade report
/rest <name or address> — Search directly
_💡 Austin inspects every food establishment at least once a year_

🎫 *Ticket Lookup:*
/ticket <id> — Look up any 311 ticket by ID

🚔 *Crime:*
/crime — Recent APD incident stats (citywide)
_Last 7 days, top crime types, clearance rate_
_From APD Crime Reports: https://data.austintexas.gov/resource/fdj4-gpfu_

🛡️ *Safety:*
/safety <district or neighborhood> — Crime check
_Examples: `/safety 3`, `/safety downtown`, `/safety east austin`_
_From APD Crime Reports_

 *Directory:*
/directory — Libraries & pools with hours
_Austin Public Library & City Pool hours_

_🚧 Under Consideration

📋 *Code Violations:*
/code — Building permits approved
_🏗️ permits data (last 365 days)_

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
             InlineKeyboardButton("⏰ Remediation", callback_data="graffiti_remediation")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_to_main")],
        ]
        text = "*🎨 Graffiti Analysis*\nPattern detection and remediation tracking."

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
            [InlineKeyboardButton("📋 Infra Backlog", callback_data="traffic_backlog"),
             InlineKeyboardButton("🕳️ Pothole Timer", callback_data="traffic_potholes")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_to_main")],
        ]
        text = "*🚦 Traffic & Infrastructure*\nPotholes, signals, street lights, sidewalks, and more."

    elif service == "noise":
        keyboard = [
            [InlineKeyboardButton("🗺️ Hotspots", callback_data="noise_hotspots"),
             InlineKeyboardButton("🕐 Peak Times", callback_data="noise_peak")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_to_main")],
        ]
        text = "*🔊 Noise Complaints*\nNon-emergency noise, outdoor venues, fireworks."

    elif service == "parking":
        keyboard = [
            [InlineKeyboardButton("🔥 Hot Zones", callback_data="parking_hotspots")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_to_main")],
        ]
        text = "*🅿️ Parking Enforcement*\nCitations, hot zones, and enforcement patterns."

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



async def graffiti_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [InlineKeyboardButton("📊 Analyze", callback_data="graffiti_analyze"),
         InlineKeyboardButton("⏰ Remediation", callback_data="graffiti_remediation")],
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


async def traffic_backlog_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Loading infrastructure backlog...")
    try:
        data = get_infra_backlog()
        text = format_infra_backlog(data)
        keyboard = build_backlog_keyboard(data)
        await query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None,
        )
    except Exception as e:
        logger.error(f"traffic backlog: {e}")
        await query.edit_message_text(f"❌ Error: {e}")


async def ticket_lookup_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    ticket_id = query.data.replace("tlookup_", "")
    await query.edit_message_text(f"🔍 Looking up ticket #{ticket_id}...")
    try:
        record = lookup_ticket(ticket_id)
        if not record:
            await query.edit_message_text(f"❌ No ticket found for #{ticket_id}.")
            return
        await _send_chunked(query, format_ticket(record))
    except Exception as e:
        logger.error(f"ticket lookup cb: {e}")
        await query.edit_message_text(f"❌ Error: {e}")


async def traffic_potholes_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Calculating pothole repair times...")
    try:
        await _send_chunked(query, format_pothole_repair_times(get_pothole_repair_times()))
    except Exception as e:
        logger.error(f"traffic potholes: {e}")
        await query.edit_message_text(f"❌ Error: {e}")


async def traffic_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [InlineKeyboardButton("📋 Infra Backlog", callback_data="traffic_backlog"),
         InlineKeyboardButton("🕳️ Pothole Timer", callback_data="traffic_potholes")],
    ]
    await update.message.reply_text(
        "*🚦 Traffic & Infrastructure*\nChoose a view:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# =============================================================================
# NOISE COMPLAINT HANDLERS
# =============================================================================


async def noise_hotspots_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Finding noise complaint hotspots...")
    try:
        await _send_chunked(query, format_noise_hotspots(get_noise_hotspots()))
    except Exception as e:
        logger.error(f"noise hotspots: {e}")
        await query.edit_message_text(f"❌ Error: {e}")


async def noise_peak_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Analyzing peak complaint times...")
    try:
        await _send_chunked(query, format_noise_peak_times(get_noise_peak_times()))
    except Exception as e:
        logger.error(f"noise peak: {e}")
        await query.edit_message_text(f"❌ Error: {e}")


async def noisecomplaints_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [InlineKeyboardButton("🗺️ Hotspots", callback_data="noise_hotspots"),
         InlineKeyboardButton("🕐 Peak Times", callback_data="noise_peak")],
    ]
    await update.message.reply_text(
        "*🔊 Noise Complaints*\nChoose a view:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


import requests
from datetime import datetime, timedelta, timezone


def _count_abandoned_vehicles() -> int:
    """Query Open311 API for abandoned vehicle report count (last 365 days)."""
    url = "https://311.austintexas.gov/open311/v2/requests.json"
    start_date = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat().replace("+00:00", "Z")
    
    params = {
        "service_code": "PARKINGV",
        "q": "abandoned",
        "start_date": start_date,
        "per_page": 100,
    }
    
    total = 0
    page = 1
    while True:
        params["page"] = page
        try:
            resp = requests.get(url, params=params, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                if not data:
                    break
                total += len(data)
                if len(data) < 100:
                    break
                page += 1
                if page > 50:  # Safety limit
                    break
            else:
                break
        except Exception:
            break
    return total


# =============================================================================
# PARKING ENFORCEMENT HANDLERS
# =============================================================================


async def parking_abandoned_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Counting abandoned vehicles...")
    try:
        count = _count_abandoned_vehicles()
        await query.edit_message_text(
            f"🚗 *Abandoned Vehicle Reports*\n\n"
            f"📊 *{count}* reports in the last 365 days\n\n"
            f"_Abandoned vehicles are handled under Parking Violation Enforcement (PARKINGV)_",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"parking abandoned: {e}")
        await query.edit_message_text(f"❌ Error: {e}")


async def parking_resolution_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Calculating resolution times...")
    try:
        stats = get_parking_stats()
        msg = "⏱️ *Parking Resolution Analysis*\n\n"
        if stats.get("avg_resolution_days"):
            msg += f"📊 *Average resolution:* {stats['avg_resolution_days']} days\n"
        else:
            msg += "📊 *Average resolution:* Not enough closed data\n"
        msg += f"📋 *Total citations:* {stats['total']}\n"
        msg += f"✅ *Closed:* {stats['closed']} | 🔴 *Open:* {stats['open']}\n\n"
        if stats.get("oldest_open"):
            o = stats["oldest_open"]
            msg += f"🕰️ *Oldest open:* #{o['id']}\n   {o['address']}\n   {o['days_ago']} days unresolved"
        await _send_chunked(query, msg)
    except Exception as e:
        logger.error(f"parking resolution: {e}")
        await query.edit_message_text(f"❌ Error: {e}")


async def parking_stats_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Fetching parking statistics...")
    try:
        stats = get_parking_stats()
        await _send_chunked(query, format_parking_stats(stats))
    except Exception as e:
        logger.error(f"parking stats: {e}")
        await query.edit_message_text(f"❌ Error: {e}")


async def parking_hotspots_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Finding parking hot zones...")
    try:
        hotspots = get_parking_hotspots()
        await _send_chunked(query, format_parking_hotspots(hotspots))
    except Exception as e:
        logger.error(f"parking hotspots: {e}")
        await query.edit_message_text(f"❌ Error: {e}")


async def parking_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [InlineKeyboardButton("🔥 Hot Zones", callback_data="parking_hotspots"),
         InlineKeyboardButton("🚗 Abandoned", callback_data="parking_abandoned")],
    ]
    await update.message.reply_text(
        "*🅿️ Parking Enforcement*\nChoose a view:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# =============================================================================
# CODE VIOLATIONS COMMAND
# =============================================================================


def _count_building_permits() -> int:
    """Query Open311 API for building/construction permits in last 365 days."""
    url = "https://311.austintexas.gov/open311/v2/requests.json"
    start_date = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat().replace("+00:00", "Z")
    
    # Construction and permitting related codes found in 311 data
    permit_codes = ["CONSTRU1", "CONSTRUC", "ATCOCIRW", "DSREFOUP"]
    total = 0
    
    for code in permit_codes:
        params = {
            "service_code": code,
            "start_date": start_date,
            "per_page": 100,
        }
        
        page = 1
        while True:
            params["page"] = page
            try:
                resp = requests.get(url, params=params, timeout=30)
                if resp.status_code == 200:
                    data = resp.json()
                    if not data:
                        break
                    total += len(data)
                    if len(data) < 100:
                        break
                    page += 1
                    if page > 50:
                        break
                else:
                    break
            except Exception:
                break
    
    return total


async def code_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "⏳ Querying building permits approved...",
        parse_mode="Markdown",
    )
    try:
        count = _count_building_permits()
        await update.message.reply_text(
            f"🏗️ *Building Permits Approved*\n\n"
            f"📊 *{count}* permits in the last 365 days\n\n"
            f"_Note: Includes building, residential, and construction permits_",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"code command: {e}")
        await update.message.reply_text(f"❌ Error querying data: {e}")


# =============================================================================
# REPORT COMMAND (Under Construction)
# =============================================================================


async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🚧 *Report 311 Issue*\n\nThis feature is under construction. Check back soon!",
        parse_mode="Markdown",
    )


# =============================================================================
# DIRECTORY COMMAND (Libraries & Pools)
# =============================================================================


# APL Library locations - official Austin Public Library branches only
APL_LIBRARIES = [
    {"name": "Austin Central Library", "address": "710 W Cesar Chavez St, Austin, TX", "filter": "Austin Central Library"},
    {"name": "Carver Branch", "address": "1161 Angelina St, Austin, TX", "filter": "Carver Branch Library"},
    {"name": "Cepeda Branch", "address": "651 N Pleasant Valley Rd, Austin, TX", "filter": "Cepeda Branch Library"},
    {"name": "Hampton Branch at Oak Hill", "address": "5125 Convict Hill Rd, Austin, TX", "filter": "Hampton Branch Library"},
    {"name": "Little Walnut Creek Branch", "address": "835 W Rundberg Ln, Austin, TX", "filter": "Little Walnut Creek Branch Library"},
    {"name": "Milwood Branch", "address": "12500 Amherst Dr, Austin, TX", "filter": "Milwood Branch Library"},
    {"name": "North Village Branch", "address": "2505 Steck Ave, Austin, TX", "filter": "North Village Branch Library"},
    {"name": "Old Quarry Branch", "address": "7051 Village Center Dr, Austin, TX", "filter": "Old Quarry Branch Library"},
    {"name": "Pleasant Hill Branch", "address": "211 E William Cannon Dr, Austin, TX", "filter": "Pleasant Hill Branch Library"},
    {"name": "Ruiz Branch", "address": "1600 Grove Blvd, Austin, TX", "filter": "Ruiz Branch Library"},
    {"name": "St. John Branch", "address": "7500 Blessing Ave, Austin, TX", "filter": "St. John Branch Library"},
    {"name": "Southeast Branch", "address": "5803 Nuckols Crossing Rd, Austin, TX", "filter": "Southeast Branch Library"},
    {"name": "Terrazas Branch", "address": "1105 E Cesar Chavez St, Austin, TX", "filter": "Terrazas Branch Library"},
    {"name": "University Hills Branch", "address": "4721 Loyola Ln, Austin, TX", "filter": "University Hills Branch Library"},
    {"name": "Willie Mae Kirk Branch", "address": "3101 Oak Springs Dr, Austin, TX", "filter": "Willie Mae Kirk Branch Library"},
    {"name": "Windsor Park Branch", "address": "5833 Westminster Dr, Austin, TX", "filter": "Windsor Park Branch Library"},
    {"name": "Yarborough Branch", "address": "2200 Hancock Dr, Austin, TX", "filter": "Yarborough Branch Library"},
]

# City of Austin Public Pools - official city pools only
AUSTIN_POOLS = [
    {"name": "Bartholomew Pool", "address": "5201 Berkman Dr, Austin, TX", "filter": "Bartholomew Pool"},
    {"name": "Barton Springs Pool", "address": "2201 William Barton Dr, Austin, TX", "filter": "Barton Springs Pool"},
    {"name": "Big Stacy Pool", "address": "700 E Live Oak St, Austin, TX", "filter": "Big Stacy Pool"},
    {"name": "Comal Pool", "address": "1709 Comal St, Austin, TX", "filter": "Comal Pool"},
    {"name": "Deep Eddy Pool", "address": "401 Deep Eddy Ave, Austin, TX", "filter": "Deep Eddy Pool"},
    {"name": "Dottie Jordan Pool", "address": "2803 Loyola Ln, Austin, TX", "filter": "Dottie Jordan Pool"},
    {"name": "Emma Long Metropolitan Park Pool", "address": "1700 City Park Rd, Austin, TX", "filter": "Emma Long Pool"},
    {"name": "Garrison Pool", "address": "6001 Manchaca Rd, Austin, TX", "filter": "Garrison Pool"},
    {"name": "Gillis Pool", "address": "2209 Hancock Dr, Austin, TX", "filter": "Gillis Pool"},
    {"name": "Govalle Pool", "address": "5200 Bolm Rd, Austin, TX", "filter": "Govalle Pool"},
    {"name": "Kimberly Lane Pool", "address": "9006 Galewood Dr, Austin, TX", "filter": "Kimberly Lane Pool"},
    {"name": "Lamar Pool", "address": "1924 S 1st St, Austin, TX", "filter": "Lamar Pool"},
    {"name": "Lyndon B. Johnson Pool", "address": "5808 Nuckols Crossing Rd, Austin, TX", "filter": "LBJ Pool"},
    {"name": "Mary Frances Baylor Pool", "address": "218 Robert E Lee Rd, Austin, TX", "filter": "Mary Frances Baylor Pool"},
    {"name": "Metz Pool", "address": "2407 Canterbury St, Austin, TX", "filter": "Metz Pool"},
    {"name": "Montopolis Pool", "address": "631 Montopolis Dr, Austin, TX", "filter": "Montopolis Pool"},
    {"name": "Murchison Pool", "address": "3700 N Hills Dr, Austin, TX", "filter": "Murchison Pool"},
    {"name": "Northeast Pool", "address": "1901 Cedar Bend Dr, Austin, TX", "filter": "Northeast Pool"},
    {"name": "Northwest Pool", "address": "7000 Ardath St, Austin, TX", "filter": "Northwest Pool"},
    {"name": "Packer Pool", "address": "1020 Duncan Ln, Austin, TX", "filter": "Packer Pool"},
    {"name": "Palm Park Pool", "address": "711 E 3rd St, Austin, TX", "filter": "Palm Park Pool"},
    {"name": "Pickle Pool", "address": "1000 Barton Springs Rd, Austin, TX", "filter": "Pickle Pool"},
    {"name": "Reilly Pool", "address": "1814 Niles Rd, Austin, TX", "filter": "Reilly Pool"},
    {"name": "Rosewood Pool", "address": "1180 N Pleasant Valley Rd, Austin, TX", "filter": "Rosewood Pool"},
    {"name": "Sanchez Pool", "address": "2021 Montopolis Dr, Austin, TX", "filter": "Sanchez Pool"},
    {"name": "Shipe Pool", "address": "6900 Manchaca Rd, Austin, TX", "filter": "Shipe Pool"},
    {"name": "South Austin Neighborhood Pool", "address": "1100 Cumberland Rd, Austin, TX", "filter": "South Austin Pool"},
    {"name": "Spicewood Springs Pool", "address": "8620 Spicewood Springs Rd, Austin, TX", "filter": "Spicewood Springs Pool"},
    {"name": "Springwoods Pool", "address": "13320 Lyndhurst St, Austin, TX", "filter": "Springwoods Pool"},
    {"name": "Tillery Pool", "address": "300 Tillery St, Austin, TX", "filter": "Tillery Pool"},
    {"name": "West Austin Neighborhood Pool", "address": "3000 Scenic Dr, Austin, TX", "filter": "West Austin Pool"},
]


def _get_place_hours(place_name: str, address: str, api_key: str, name_filter: str = "") -> str:
    """Fetch operating hours from Google Maps API with filtering."""
    if not api_key:
        return "⚠️ Google Maps API key not configured"
    
    try:
        # First, find the place
        find_url = "https://maps.googleapis.com/maps/api/place/findplacefromtext/json"
        find_params = {
            "input": f"{place_name} {address}",
            "inputtype": "textquery",
            "fields": "place_id",
            "key": api_key,
        }
        resp = requests.get(find_url, params=find_params, timeout=10)
        data = resp.json()
        
        if data.get("status") != "OK" or not data.get("candidates"):
            return "⏰ Hours: Not found"
        
        place_id = data["candidates"][0]["place_id"]
        
        # Get place details with opening hours
        details_url = "https://maps.googleapis.com/maps/api/place/details/json"
        details_params = {
            "place_id": place_id,
            "fields": "opening_hours,name",
            "key": api_key,
        }
        resp = requests.get(details_url, params=details_params, timeout=10)
        data = resp.json()
        
        if data.get("status") != "OK":
            return "⏰ Hours: Not available"
        
        result = data.get("result", {})
        returned_name = result.get("name", "")
        
        # Filter: verify this is the expected place (APL Library or City Pool)
        if name_filter and name_filter.lower() not in returned_name.lower():
            # Try alternative check for pools
            if "pool" in name_filter.lower() and "pool" not in returned_name.lower():
                return f"⏰ Hours: Location mismatch (got: {returned_name})"
            # Try alternative check for libraries
            if "library" in name_filter.lower() and "library" not in returned_name.lower():
                return f"⏰ Hours: Location mismatch (got: {returned_name})"
        
        hours_info = result.get("opening_hours", {})
        
        if hours_info.get("open_now") is True:
            status = "🟢 Open now"
        elif hours_info.get("open_now") is False:
            status = "🔴 Closed"
        else:
            status = "⏰ Hours unknown"
        
        weekday_text = hours_info.get("weekday_text", [])
        if weekday_text:
            hours_text = "\n".join(weekday_text[:3]) + ("\n..." if len(weekday_text) > 3 else "")
            return f"{status}\n{hours_text}"
        return status
        
    except Exception as e:
        logger.error(f"Google Maps API error for {place_name}: {e}")
        return "⏰ Hours: Error fetching"


def _format_directory_list(items: list, api_key: str, item_type: str) -> str:
    """Format a list of places with their hours."""
    lines = [f"📍 *{item_type}*\n"]
    
    for item in items[:5]:  # Show top 5
        name = item["name"]
        address = item["address"]
        name_filter = item.get("filter", "")
        hours = _get_place_hours(name, address, api_key, name_filter)
        lines.append(f"*{name}*\n{address}\n{hours}\n")
    
    if len(items) > 5:
        lines.append(f"... and {len(items) - 5} more")
    
    return "\n".join(lines)


async def directory_libraries_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Fetching library hours...")
    
    api_key = os.getenv("GOOGLE_MAPS_API_KEY", "")
    try:
        text = _format_directory_list(APL_LIBRARIES, api_key, "Austin Public Libraries")
        await _send_chunked(query, text)
    except Exception as e:
        logger.error(f"directory libraries: {e}")
        await query.edit_message_text(f"❌ Error: {e}")


async def directory_pools_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Fetching pool hours...")
    
    api_key = os.getenv("GOOGLE_MAPS_API_KEY", "")
    try:
        text = _format_directory_list(AUSTIN_POOLS, api_key, "Austin Public Pools")
        await _send_chunked(query, text)
    except Exception as e:
        logger.error(f"directory pools: {e}")
        await query.edit_message_text(f"❌ Error: {e}")


async def directory_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [InlineKeyboardButton("📚 Libraries", callback_data="directory_libraries"),
         InlineKeyboardButton("🏊 Pools", callback_data="directory_pools")],
    ]
    await update.message.reply_text(
        "📍 *Austin Directory*\n\nFind operating hours for:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# =============================================================================
# CRIME DATA COMMAND (APD Crime Reports)
# =============================================================================


def _get_recent_crime_stats(days: int = 7) -> dict:
    """Query APD Crime Reports API for recent incident stats."""
    url = "https://data.austintexas.gov/resource/fdj4-gpfu.json"
    
    from datetime import datetime, timedelta, timezone
    start_date = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    
    params = {
        "$where": f"occ_date >= '{start_date}'",
        "$limit": 1000,
    }
    
    # Use app token if available (increases rate limits)
    app_token = os.getenv("AUSTIN_APP_TOKEN", "")
    headers = {}
    if app_token:
        headers["X-App-Token"] = app_token
    
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            
            # Count by crime type
            crime_types = {}
            family_violence_count = 0
            cleared = 0
            total = len(data)
            hour_counts = {}
            
            for incident in data:
                crime_type = incident.get("crime_type", "Unknown")
                crime_types[crime_type] = crime_types.get(crime_type, 0) + 1
                
                if incident.get("family_violence") == "Y":
                    family_violence_count += 1
                
                if incident.get("clearance_status") == "C":
                    cleared += 1
                
                # Track hour of occurrence
                occ_time = incident.get("occ_time", "")
                if occ_time and len(occ_time) >= 2:
                    try:
                        hour = int(occ_time[:2])
                        hour_counts[hour] = hour_counts.get(hour, 0) + 1
                    except ValueError:
                        pass
            
            # Get top 5 crime types
            top_crimes = sorted(crime_types.items(), key=lambda x: -x[1])[:5]
            
            # Find peak crime hour
            peak_hour = max(hour_counts, key=hour_counts.get) if hour_counts else None
            
            return {
                "total": total,
                "cleared": cleared,
                "top_crimes": top_crimes,
                "family_violence": family_violence_count,
                "peak_hour": peak_hour,
                "days": days,
            }
    except Exception as e:
        logger.error(f"crime stats: {e}")
    
    return {"total": 0, "cleared": 0, "top_crimes": [], "family_violence": 0, "peak_hour": None, "days": days}


async def crime_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "⏳ Fetching recent crime data...",
        parse_mode="Markdown",
    )
    try:
        stats = _get_recent_crime_stats(days=7)
        
        msg = f"🚔 *APD Crime Reports (Last {stats['days']} Days)*\n\n"
        msg += f"📊 *{stats['total']}* total incidents\n"
        msg += f"✅ *{stats['cleared']}* cleared ({round(stats['cleared']/stats['total']*100) if stats['total'] else 0}%)\n\n"
        
        if stats['top_crimes']:
            msg += "*Top Crime Types:*\n"
            for crime, count in stats['top_crimes']:
                msg += f"• {crime}: {count}\n"
            msg += "\n"
        
        # Show family violence stat
        if stats['family_violence'] > 0:
            msg += f"⚠️ *{stats['family_violence']}* family violence incidents\n"
        
        # Show peak crime time
        if stats['peak_hour'] is not None:
            peak_time = f"{stats['peak_hour']}:00-{stats['peak_hour']+1}:00"
            msg += f"🕐 Peak crime time: *{peak_time}*\n"
        
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"crime command: {e}")
        await update.message.reply_text(f"❌ Error fetching crime data: {e}")


# Neighborhood to Council District mapping for /safety command
NEIGHBORHOOD_TO_DISTRICT = {
    # District 1 - East Austin
    "east austin": "1", "east side": "1", "cherrywood": "1", "manor park": "1",
    "upper boggy creek": "1", "windemere": "1", "harris branch": "1",
    "delwood": "1", "mlk": "1", "bartholomew park": "1",
    
    # District 2 - Southeast Austin
    "southeast austin": "2", "riverside": "2", "montopolis": "2", "pleasant valley": "2",
    "springdale": "2", "east riverside": "2", "ocky chase": "2", " Franklin park": "2",
    "govalle": "2", "gracywoods": "2",
    
    # District 3 - South Austin
    "south austin": "3", "south congress": "3", "soco": "3", "barton hills": "3",
    "zilker": "3", "bouldin creek": "3", "galindo": "3", "south lamar": "3",
    "westgate": "3", "sunset valley": "3", "manchaca": "3", "shady hollow": "3",
    "slaughter": "3", "onion creek": "3",
    
    # District 4 - Northeast Austin
    "northeast austin": "4", "highland": "4", "north lamar": "4", "windsor park": "4",
    "chestnut": "4", "pecan springs": "4", " MLK 183": "4", "mueller": "4",
    "barbara jordan": "4", "cambridge heights": "4", "sherwood heights": "4",
    
    # District 5 - Central Austin
    "downtown": "5", "west campus": "5", "hyde park": "5", "north university": "5",
    "clarksville": "5", "old west austin": "5", "tarrytown": "5", "rollingwood": "5",
    "west lake hills": "5", "casanova": "5", "westfield": "5",
    
    # District 6 - Northwest Austin
    "northwest austin": "6", "great hills": "6", "canyon creek": "6", "jollyville": "6",
    "balcones": "6", "flintrock": "6", "four points": "6", "steiner ranch": "6",
    "river place": "6", "cat hollow": "6", "milwood": "6", "rattan creek": "6",
    
    # District 7 - North Austin
    "north austin": "7", "north loop": "7", "brentwood": "7", "crestview": "7",
    "allandale": "7", "wooten": "7", "north shoal creek": "7", "lincoln village": "7",
    "gracywoods": "7", "quail creek": "7", "village at anderson mill": "7",
    
    # District 8 - Southwest Austin
    "southwest austin": "8", "circle c": "8", "travis country": "8", "village square": "8",
    "avery ranch": "8", "brushy creek": "8", "cat mountain": "8", "davenport": "8",
    "four points": "8", "lakeway": "8", "lake pointe": "8",
    
    # District 9 - Central/South Austin
    "mueller": "9", "east caesar chavez": "9", "rosewood": "9", "chestnut": "9",
    "east 11th": "9", "french place": "9", "holly": "9", "boggy creek": "9",
    "east cesar chavez": "9", "east sixth": "9",
    
    # District 10 - Far Northwest Austin
    "cedar park": "10", "leander": "10", "brushy creek": "10", "lago vista": "10",
    "steiner ranch": "10", "river place": "10", "lakewood": "10", "slaughter": "10",
    "teravista": "10", "palmera ridge": "10",
}


def _resolve_district(input_str: str) -> tuple[str, str]:
    """Resolve district number or neighborhood name to district.
    Returns (district, display_name) or (None, error_message)
    """
    input_lower = input_str.lower().strip()
    
    # Check if it's a district number 1-10
    if input_str.isdigit():
        district = int(input_str)
        if 1 <= district <= 10:
            return (str(district), f"District {district}")
        return (None, "District must be 1-10")
    
    # Check neighborhood mapping
    if input_lower in NEIGHBORHOOD_TO_DISTRICT:
        district = NEIGHBORHOOD_TO_DISTRICT[input_lower]
        return (district, f"{input_str.title()} (District {district})")
    
    # Try partial match
    matches = [(n, d) for n, d in NEIGHBORHOOD_TO_DISTRICT.items() if input_lower in n or n in input_lower]
    if matches:
        # Return the first match
        neighborhood, district = matches[0]
        return (district, f"{neighborhood.title()} (District {district})")
    
    # List valid options for user
    neighborhoods = ", ".join(sorted(set(NEIGHBORHOOD_TO_DISTRICT.keys()))[:15]) + "..."
    return (None, f"Unknown neighborhood. Try: {neighborhoods} or use district 1-10")


async def safety_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Get crime stats for a specific council district or neighborhood."""
    if not context.args:
        await update.message.reply_text(
            "🔍 *Safety Check*\n\n"
            "Usage: `/safety <district>` or `/safety <neighborhood>`\n"
            "Examples:\n"
            "• `/safety 3` (Council District 3)\n"
            "• `/safety downtown`\n"
            "• `/safety east austin`\n\n"
            "Or use `/crime` for citywide stats.",
            parse_mode="Markdown",
        )
        return
    
    input_str = " ".join(context.args)
    district, result_msg = _resolve_district(input_str)
    
    if district is None:
        await update.message.reply_text(f"❌ {result_msg}")
        return
    
    await update.message.reply_text(f"⏳ Checking crime in {result_msg}...")
    
    try:
        url = "https://data.austintexas.gov/resource/fdj4-gpfu.json"
        from datetime import datetime, timedelta, timezone
        start_date = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
        
        params = {
            "$where": f"council_district='{district}' AND occ_date >= '{start_date}'",
            "$limit": 500,
        }
        
        # Use app token if available (increases rate limits)
        app_token = os.getenv("AUSTIN_APP_TOKEN", "")
        headers = {}
        if app_token:
            headers["X-App-Token"] = app_token
        
        resp = requests.get(url, params=params, headers=headers, timeout=30)
        if resp.status_code != 200:
            await update.message.reply_text("❌ Error fetching crime data")
            return
        
        data = resp.json()
        total = len(data)
        
        if total == 0:
            await update.message.reply_text(f"✅ No incidents reported in {result_msg} (last 30 days)")
            return
        
        # Count crime types
        crime_types = {}
        cleared = 0
        for incident in data:
            crime_type = incident.get("crime_type", "Unknown")
            crime_types[crime_type] = crime_types.get(crime_type, 0) + 1
            if incident.get("clearance_status") == "C":
                cleared += 1
        
        top_crimes = sorted(crime_types.items(), key=lambda x: -x[1])[:5]
        clearance_pct = round(cleared/total*100) if total else 0
        
        msg = f" *{total}* incidents (last 30 days)\n"
        msg += f"✅ {clearance_pct}% cleared\n\n"
        msg += "*Top Crime Types:*\n"
        for crime, count in top_crimes:
            msg += f"• {crime}: {count}\n"
        
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"safety command: {e}")
        await update.message.reply_text(f"❌ Error: {e}")


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
    app.add_handler(CallbackQueryHandler(graffiti_remediation_cb, pattern="^graffiti_remediation"))

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
    app.add_handler(CallbackQueryHandler(traffic_backlog_cb, pattern="^traffic_backlog"))
    app.add_handler(CallbackQueryHandler(traffic_potholes_cb, pattern="^traffic_potholes"))
    app.add_handler(CallbackQueryHandler(ticket_lookup_cb, pattern="^tlookup_"))

    # Noise inline
    app.add_handler(CallbackQueryHandler(noise_hotspots_cb, pattern="^noise_hotspots"))
    app.add_handler(CallbackQueryHandler(noise_peak_cb, pattern="^noise_peak"))

    # Parking inline
    app.add_handler(CallbackQueryHandler(parking_stats_cb, pattern="^parking_stats"))
    app.add_handler(CallbackQueryHandler(parking_hotspots_cb, pattern="^parking_hotspots"))
    app.add_handler(CallbackQueryHandler(parking_resolution_cb, pattern="^parking_resolution"))
    app.add_handler(CallbackQueryHandler(parking_abandoned_cb, pattern="^parking_abandoned"))

    # Graffiti slash command
    app.add_handler(CommandHandler("graffiti", graffiti_command))

    # Crime slash command
    app.add_handler(CommandHandler("crime", crime_command))

    # Safety slash command
    app.add_handler(CommandHandler("safety", safety_command))

    # Bicycle slash commands
    app.add_handler(CommandHandler("bicycle", bicycle_command))
    app.add_handler(CommandHandler("ticket", ticket_command))

    # Restaurant slash command
    app.add_handler(CommandHandler("rest", restaurant_command))

    # Traffic slash command
    app.add_handler(CommandHandler("traffic", traffic_command))

    # Noise slash command
    app.add_handler(CommandHandler("noisecomplaints", noisecomplaints_command))

    # Report slash command
    app.add_handler(CommandHandler("report", report_command))

    # Code violations slash command
    app.add_handler(CommandHandler("code", code_command))

    # Directory inline
    app.add_handler(CallbackQueryHandler(directory_libraries_cb, pattern="^directory_libraries"))
    app.add_handler(CallbackQueryHandler(directory_pools_cb, pattern="^directory_pools"))

    # Directory slash command
    app.add_handler(CommandHandler("directory", directory_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo_handler))
    app.add_error_handler(error_handler)

    # Register commands with Telegram so they appear in autocomplete
    async def post_init(application) -> None:
        await application.bot.set_my_commands([
            BotCommand("start",           "Main menu"),
            BotCommand("help",            "All commands"),
            BotCommand("graffiti",        "Graffiti — analysis · hotspots · remediation"),
            BotCommand("animal",          "Animal complaints — hotspots · stats · response times"),
            BotCommand("bicycle",         "Bicycle complaints — recent · stats"),
            BotCommand("traffic",         "Traffic & infrastructure — potholes · signals · lights"),
            BotCommand("noisecomplaints", "Noise complaints — hotspots · stats · response times"),
            BotCommand("parking",         "Parking enforcement — citations · hot zones · stats"),
            BotCommand("rest",            "Restaurant inspections — worst scores · grades · search"),
            BotCommand("ticket",          "Look up any 311 ticket by ID"),
            BotCommand("report",          "Submit a 311 report (under construction)"),
            BotCommand("code",              "Building permits approved (last 365 days)"),
            BotCommand("directory",         "Libraries & pools with hours"),
            BotCommand("crime",             "Recent APD crime stats"),
            BotCommand("safety",            "Crime by district or neighborhood"),
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
