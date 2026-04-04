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
import re
import time
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
    get_signal_maintenance,
    format_signal_maintenance,
)

# Noise complaints service
from noisecomplaints.noise_bot import (
    get_hotspots as get_noise_hotspots,
    format_hotspots as format_noise_hotspots,
    get_peak_times as get_noise_peak_times,
    format_peak_times as format_noise_peak_times,
    get_resolution_by_type as get_noise_resolution,
    format_resolution_by_type as format_noise_resolution,
    get_night_breakdown as get_noise_night,
    format_night_breakdown as format_noise_night,
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
            await target.edit_message_text(chunk, parse_mode=parse_mode, disable_web_page_preview=True)
        else:
            msg = target.message if hasattr(target, "message") else target
            await msg.reply_text(chunk, parse_mode=parse_mode, disable_web_page_preview=True)


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
        [InlineKeyboardButton("🚔 Police & Crime", callback_data="service_police")],
        [InlineKeyboardButton("📝 Report Issue", callback_data="service_report")],
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

🚴 *Bicycle:*
/bicycle — Recent complaints · stats
_💡 Austin has 100\+ miles of dedicated bike lanes — among the most in Texas_

🍽️ *Restaurants:*
/rest — Worst scores · grade report
/rest <name or address> — Search directly
_💡 Austin inspects every food establishment at least once a year_

🐾 *Animal Services:*
/animal — Hotspots · stats · response times
_💡 Loose dog complaints are the most common 311 call in Austin_

🚦 *Traffic & Infrastructure:*
/traffic — Potholes · signals · street lights · sidewalks
_💡 Pothole repair is one of the top 311 categories in Austin_

🔊 *Noise Complaints:*
/noise — Hotspots · stats · response times
_💡 Austin's 6th Street corridor generates some of the highest noise complaint volumes in the city_

🅿️ *Parking:*
/parking — Citations · hot zones · stats
_💡 Parking enforcement is one of the top 10 most-requested 311 services in Austin_

🚔 *Police & Crime:*
/crime — Recent APD incident stats (citywide)
_Last 30 days, top crime types, clearance rate_
_From APD Crime Reports: https://data.austintexas.gov/resource/fdj4-gpfu_
/safety — Crime by district with city comparison
/hate — Hate crime incidents — bias motivation · offender race/ethnicity · offense breakdown

🎫 *Ticket Lookup:*
/ticket <id> — Look up any 311 ticket by ID

🏊 *Pool Hours:* https://www.austintexas.gov/parks/locations/pools-and-splash-pads

🚧 _Under Consideration_

📋 *Code Violations:*
/code — Building permits approved
_🏗️ permits data (last 365 days)_

📝 *Report Issue:*
/report — Under consideration

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
             InlineKeyboardButton("🚦 Broken Signals", callback_data="traffic_signals")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_to_main")],
        ]
        text = "*🚦 Traffic & Infrastructure*\nPotholes, signals, street lights, sidewalks, and more."

    elif service == "noise":
        keyboard = [
            [InlineKeyboardButton("🗺️ Hotspots", callback_data="noise_hotspots"),
             InlineKeyboardButton("🕐 Peak Times", callback_data="noise_peak")],
            [InlineKeyboardButton("📋 Resolution by Type", callback_data="noise_resolution"),
             InlineKeyboardButton("🌙 Night Breakdown", callback_data="noise_night")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_to_main")],
        ]
        text = "*🔊 Noise Complaints*\nNon-emergency noise, outdoor venues, fireworks."

    elif service == "parking":
        keyboard = [
            [InlineKeyboardButton("🔥 Hot Zones", callback_data="parking_hotspots")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_to_main")],
        ]
        text = "*🅿️ Parking Enforcement*\nCitations, hot zones, and enforcement patterns."

    elif service == "police":
        keyboard = [
            [InlineKeyboardButton("🚔 Crime Stats", callback_data="police_crime"),
             InlineKeyboardButton("🛡️ Safety by District", callback_data="police_safety")],
            [InlineKeyboardButton("🎯 Hate Crimes", callback_data="police_hate")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_to_main")],
        ]
        text = "*🚔 Police & Crime*\nAPD incident stats, safety by district, and hate crimes."

    elif service == "report":
        await query.answer()
        await query.edit_message_text(
            "🚧 *Report 311 Issue*\n\nThis feature is under construction. Check back soon!",
            parse_mode="Markdown",
        )
        return

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
        [InlineKeyboardButton("🚔 Police & Crime", callback_data="service_police")],
        [InlineKeyboardButton("📝 Report Issue", callback_data="service_report")],
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
        result = await asyncio.to_thread(analyze_graffiti_command, 90)
        await _send_chunked(query, result)
    except Exception as e:
        logger.error(f"graffiti analyze: {e}")
        await query.edit_message_text(f"❌ Error: {e}")



async def graffiti_remediation_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Analyzing remediation times...")
    try:
        result = await asyncio.to_thread(remediation_command, 90)
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
        complaints = await asyncio.to_thread(lambda: get_recent_complaints(limit=10))
        if not complaints:
            await query.edit_message_text("📝 No bicycle complaints found.")
            return

        keyboard = []
        for r in complaints:
            req_id = r.get("service_request_id") or "N/A"
            date = (r.get("requested_datetime") or "").split("T")[0]
            desc = (r.get("description") or r.get("service_name") or "Bicycle complaint")
            status = (r.get("status") or "").lower()
            icon = "🟢" if status == "closed" else "🔴"
            label = f"{icon} #{req_id} · {date} — {desc[:35]}"
            keyboard.append([InlineKeyboardButton(label, callback_data=f"bicycle_ticket_{req_id}")])

        await query.edit_message_text(
            "🚴 *Recent Bicycle Complaints*\n_Tap a ticket to see the full complaint:_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    except Exception as e:
        logger.error(f"bicycle recent: {e}")
        await query.edit_message_text(f"❌ Error: {e}")


async def bicycle_ticket_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    ticket_id = query.data.replace("bicycle_ticket_", "")
    await query.edit_message_text(f"⏳ Looking up ticket #{ticket_id}...")
    try:
        record = await asyncio.to_thread(lookup_ticket, ticket_id)
        if not record:
            await query.edit_message_text(f"❌ Ticket #{ticket_id} not found.")
            return
        result = format_ticket(record)
        await _send_chunked(query, result)
    except Exception as e:
        logger.error(f"bicycle ticket cb: {e}")
        await query.edit_message_text(f"❌ Error: {e}")


async def bicycle_stats_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Fetching bicycle statistics...")
    try:
        stats = await asyncio.to_thread(get_stats)
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
        record = await asyncio.to_thread(lookup_ticket, ticket_id)
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
        restaurants = await asyncio.to_thread(lambda: get_lowest_scoring(10))
        await _send_chunked(query, format_low_scores(restaurants))
    except Exception as e:
        logger.error(f"restaurants lowscores: {e}")
        await query.edit_message_text(f"❌ Error: {e}")


async def restaurants_grades_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Loading grade report... (first load may take ~15s while fetching a full year of data)")
    try:
        data = await asyncio.to_thread(get_grade_distribution)
        await _send_chunked(query, format_grade_distribution(data))
    except Exception as e:
        logger.error(f"restaurants grades: {e}")
        await query.edit_message_text(f"❌ Error: {e}")


async def restaurant_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.args:
        search_term = " ".join(context.args)
        await update.message.reply_text(f"🔍 Searching for: {search_term}...")
        try:
            results = await asyncio.to_thread(lambda: search_restaurants(search_term))
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
        data = await asyncio.to_thread(get_hotspots)
        await _send_chunked(query, format_hotspots(data))
    except Exception as e:
        logger.error(f"animal hotspots: {e}")
        await query.edit_message_text(f"❌ Error: {e}")


async def animal_stats_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Fetching animal complaint stats...")
    try:
        data = await asyncio.to_thread(get_animal_stats)
        await _send_chunked(query, format_animal_stats(data))
    except Exception as e:
        logger.error(f"animal stats: {e}")
        await query.edit_message_text(f"❌ Error: {e}")


async def animal_response_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Calculating response times...")
    try:
        data = await asyncio.to_thread(get_response_times)
        await _send_chunked(query, format_response_times(data))
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
        data = await asyncio.to_thread(get_infra_backlog)
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
        record = await asyncio.to_thread(lookup_ticket, ticket_id)
        if not record:
            await query.edit_message_text(f"❌ No ticket found for #{ticket_id}.")
            return
        await _send_chunked(query, format_ticket(record))
    except Exception as e:
        logger.error(f"ticket lookup cb: {e}")
        await query.edit_message_text(f"❌ Error: {e}")


async def traffic_signals_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Fetching broken signal data...")
    try:
        data = await asyncio.to_thread(get_signal_maintenance)
        await _send_chunked(query, format_signal_maintenance(data))
    except Exception as e:
        logger.error(f"traffic signals: {e}")
        await query.edit_message_text(f"❌ Error: {e}")


async def traffic_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [InlineKeyboardButton("📋 Infra Backlog", callback_data="traffic_backlog"),
         InlineKeyboardButton("🚦 Broken Signals", callback_data="traffic_signals")],
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
        data = await asyncio.to_thread(get_noise_hotspots)
        await _send_chunked(query, format_noise_hotspots(data))
    except Exception as e:
        logger.error(f"noise hotspots: {e}")
        await query.edit_message_text(f"❌ Error: {e}")


async def noise_peak_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Analyzing peak complaint times...")
    try:
        data = await asyncio.to_thread(get_noise_peak_times)
        await _send_chunked(query, format_noise_peak_times(data))
    except Exception as e:
        logger.error(f"noise peak: {e}")
        await query.edit_message_text(f"❌ Error: {e}")


async def noise_resolution_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Analyzing resolution rates...")
    try:
        data = await asyncio.to_thread(get_noise_resolution)
        await _send_chunked(query, format_noise_resolution(data))
    except Exception as e:
        logger.error(f"noise resolution: {e}")
        await query.edit_message_text(f"❌ Error: {e}")


async def noise_night_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Analyzing complaint timing...")
    try:
        data = await asyncio.to_thread(get_noise_night)
        await _send_chunked(query, format_noise_night(data))
    except Exception as e:
        logger.error(f"noise night: {e}")
        await query.edit_message_text(f"❌ Error: {e}")


async def noisecomplaints_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [InlineKeyboardButton("🗺️ Hotspots", callback_data="noise_hotspots"),
         InlineKeyboardButton("🕐 Peak Times", callback_data="noise_peak")],
        [InlineKeyboardButton("📋 Resolution by Type", callback_data="noise_resolution"),
         InlineKeyboardButton("🌙 Night Breakdown", callback_data="noise_night")],
    ]
    await update.message.reply_text(
        "*🔊 Noise Complaints*\nChoose a view:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


import requests
from datetime import datetime, timedelta, timezone


def _get_abandoned_vehicle_stats() -> dict:
    """Query Open311 API for abandoned vehicle stats (last 365 days)."""
    url = "https://311.austintexas.gov/open311/v2/requests.json"
    start_date = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat().replace("+00:00", "Z")

    params = {
        "service_code": "PARKINGV",
        "q": "abandoned",
        "start_date": start_date,
        "per_page": 100,
    }

    all_records = []
    page = 1
    while True:
        params["page"] = page
        try:
            resp = requests.get(url, params=params, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                if not data:
                    break
                all_records.extend(data)
                if len(data) < 100:
                    break
                page += 1
                if page > 50:
                    break
            else:
                break
        except Exception:
            break

    open_count = sum(1 for r in all_records if (r.get("status") or "").lower() == "open")
    closed_count = sum(1 for r in all_records if (r.get("status") or "").lower() == "closed")

    # Most recent ticket for sample link
    sample_id = None
    for r in all_records:
        rid = r.get("service_request_id")
        if rid:
            sample_id = rid
            break

    # Oldest open ticket
    oldest_open = None
    oldest_dt = None
    for r in all_records:
        if (r.get("status") or "").lower() != "open":
            continue
        dt_str = r.get("requested_datetime") or ""
        if dt_str:
            try:
                from dateutil import parser as dtparser
                dt = dtparser.parse(dt_str)
                if oldest_dt is None or dt < oldest_dt:
                    oldest_dt = dt
                    oldest_open = r
            except Exception:
                pass

    oldest_info = None
    if oldest_open and oldest_dt:
        days_ago = (datetime.now(timezone.utc) - oldest_dt.replace(tzinfo=timezone.utc) if oldest_dt.tzinfo is None else datetime.now(timezone.utc) - oldest_dt).days
        oldest_info = {
            "id": oldest_open.get("service_request_id"),
            "address": oldest_open.get("address") or "Unknown location",
            "days_ago": days_ago,
        }

    return {
        "total": len(all_records),
        "open": open_count,
        "closed": closed_count,
        "sample_id": sample_id,
        "oldest_open": oldest_info,
    }


# =============================================================================
# PARKING ENFORCEMENT HANDLERS
# =============================================================================


def _get_parking_pulse() -> dict:
    """Fetch today's parking transactions and compute activity summary."""
    from collections import Counter
    since = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
    url = "https://data.austintexas.gov/resource/5bb2-gtef.json"
    params = {
        "$where": f"start_time >= '{since}'",
        "$limit": 1000,
        "$order": "start_time DESC",
    }
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    entries = resp.json()
    if not entries:
        return {}

    amounts = []
    durations = []
    locations = Counter()
    methods = Counter()
    hours = Counter()

    for e in entries:
        try:
            amounts.append(float(e["amount"]))
        except (KeyError, ValueError, TypeError):
            pass
        try:
            durations.append(float(e["duration_min"]))
        except (KeyError, ValueError, TypeError):
            pass
        if e.get("location_name"):
            locations[e["location_name"]] += 1
        if e.get("payment_method"):
            methods[e["payment_method"]] += 1
        start = e.get("start_time") or ""
        if "T" in start:
            try:
                hours[int(start.split("T")[1][:2])] += 1
            except (ValueError, IndexError):
                pass

    peak_hour = max(hours, key=hours.get) if hours else None

    return {
        "total": len(entries),
        "revenue": round(sum(amounts), 2),
        "avg_amount": round(sum(amounts) / len(amounts), 2) if amounts else 0,
        "avg_duration_min": round(sum(durations) / len(durations)) if durations else 0,
        "locations": locations.most_common(5),
        "card": methods.get("CARD", 0),
        "coins": methods.get("COINS", 0),
        "peak_hour": peak_hour,
    }


async def parking_top_payments_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Fetching today's parking activity...")
    try:
        data = await asyncio.to_thread(_get_parking_pulse)
        if not data:
            await query.edit_message_text("📝 No parking transactions found for today.")
            return

        peak_str = f"{data['peak_hour']}:00–{data['peak_hour']+1}:00" if data["peak_hour"] is not None else "N/A"
        avg_hrs = round(data["avg_duration_min"] / 60, 1)

        msg = "🅿️ *Austin Parking — Last 24 Hours*\n\n"
        msg += f"🔢 *Transactions:* {data['total']:,}\n"
        msg += f"💵 *Total revenue:* ${data['revenue']:,.2f}\n"
        msg += f"📊 *Avg session:* ${data['avg_amount']} · {avg_hrs} hrs\n"
        msg += f"⏰ *Peak hour:* {peak_str}\n"
        msg += f"💳 *Card:* {data['card']}  🪙 *Coins:* {data['coins']}\n"

        if data["locations"]:
            msg += "\n*Activity by neighborhood:*\n"
            max_loc = data["locations"][0][1]
            for name, count in data["locations"]:
                bar = "█" * min(10, round(count / max_loc * 10))
                msg += f"  {bar} {name} — {count}\n"

        await query.edit_message_text(msg, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"parking pulse: {e}")
        await query.edit_message_text(f"❌ Error: {e}")


async def parking_abandoned_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Fetching abandoned vehicle data...")
    try:
        stats = await asyncio.to_thread(_get_abandoned_vehicle_stats)
        msg = (
            f"🚗 *Abandoned Vehicle Reports*\n\n"
            f"📊 *{stats['total']}* reports in the last 365 days\n"
            f"✅ *Closed:* {stats['closed']}  🔴 *Open:* {stats['open']}\n"
        )
        if stats.get("oldest_open"):
            o = stats["oldest_open"]
            msg += f"\n🕰️ *Longest open:* #{o['id']}\n   📍 {o['address']}\n   {o['days_ago']} days unresolved\n"
        if stats.get("sample_id"):
            msg += f"\n🔗 [View sample ticket #{stats['sample_id']}](https://311.austintexas.gov/open311/v2/requests/{stats['sample_id']}.json)"
        await query.edit_message_text(msg, parse_mode="Markdown", disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"parking abandoned: {e}")
        await query.edit_message_text(f"❌ Error: {e}")


async def parking_resolution_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Calculating resolution times...")
    try:
        stats = await asyncio.to_thread(get_parking_stats)
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
        stats = await asyncio.to_thread(get_parking_stats)
        await _send_chunked(query, format_parking_stats(stats))
    except Exception as e:
        logger.error(f"parking stats: {e}")
        await query.edit_message_text(f"❌ Error: {e}")


async def parking_hotspots_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Finding parking hot zones...")
    try:
        hotspots = await asyncio.to_thread(get_parking_hotspots)
        await _send_chunked(query, format_parking_hotspots(hotspots))
    except Exception as e:
        logger.error(f"parking hotspots: {e}")
        await query.edit_message_text(f"❌ Error: {e}")


async def parking_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [InlineKeyboardButton("🔥 Hot Zones", callback_data="parking_hotspots"),
         InlineKeyboardButton("🚗 Abandoned", callback_data="parking_abandoned"),
         InlineKeyboardButton("📈 Daily Pulse", callback_data="parking_top_payments")],
    ]
    await update.message.reply_text(
        "*🅿️ Parking Enforcement*\nChoose a view:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# =============================================================================
# CODE VIOLATIONS COMMAND
# =============================================================================


def _get_building_permit_stats() -> dict:
    """Query Open311 API for building/construction permits in last 365 days.

    Returns per-code counts and service names pulled from the API itself.
    """
    url = "https://311.austintexas.gov/open311/v2/requests.json"
    start_date = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat().replace("+00:00", "Z")
    permit_codes = ["CONSTRU1", "CONSTRUC", "ATCOCIRW", "DSREFOUP"]

    breakdown = {}  # code -> {"label": str, "count": int}

    for code in permit_codes:
        params = {"service_code": code, "start_date": start_date, "per_page": 100}
        count = 0
        label = code
        page = 1
        while True:
            params["page"] = page
            try:
                resp = requests.get(url, params=params, timeout=30)
                if resp.status_code != 200:
                    break
                data = resp.json()
                if not data:
                    break
                # Grab service_name from first record if we haven't yet
                if label == code and data:
                    label = data[0].get("service_name") or code
                count += len(data)
                if len(data) < 100 or page >= 50:
                    break
                page += 1
            except Exception:
                break
        if count > 0:
            breakdown[code] = {"label": label, "count": count}

    total = sum(v["count"] for v in breakdown.values())
    return {"total": total, "breakdown": breakdown}


def _format_permit_stats(stats: dict) -> str:
    total = stats["total"]
    breakdown = stats["breakdown"]
    msg = "🏗️ *Building Permits — Last 365 Days*\n\n"
    msg += f"📊 *{total}* total permits\n\n"
    if breakdown:
        max_count = max(v["count"] for v in breakdown.values())
        for v in sorted(breakdown.values(), key=lambda x: -x["count"]):
            count = v["count"]
            pct = round(count / total * 100) if total else 0
            bar = "█" * min(10, round(count / max_count * 10))
            msg += f"*{v['label']}*\n"
            msg += f"{bar} {count} ({pct}%)\n\n"
    return msg.strip()


async def code_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("⏳ Querying building permits...")
    try:
        stats = await asyncio.to_thread(_get_building_permit_stats)
        await update.message.reply_text(_format_permit_stats(stats), parse_mode="Markdown")
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
# CRIME DATA COMMAND (APD Crime Reports)
# =============================================================================


# Human-readable labels for raw APD crime_type values that are ambiguous or jargon-heavy.
# "Family Disturbance" = non-violent domestic dispute (officers keep the peace).
# "Family Violence"    = physical assault between family members / intimate partners (DV).
_CRIME_TYPE_LABELS: dict[str, str] = {
    "Family Disturbance":          "Family Disturbance (non-violent domestic dispute)",
    "Family Violence":             "Family/Domestic Violence (physical assault)",
    "Auto Theft":                  "Auto Theft (vehicle stolen)",
    "Burglary of Vehicle":         "Burglary of Vehicle (break-in, not stolen)",
    "Criminal Mischief":           "Criminal Mischief (vandalism/property damage)",
    "Disturbance - Other":         "Disturbance (non-family, non-violent)",
    "Assault W/Injury-Fv":         "Assault with Injury — Domestic Violence",
    "Assault W/Injury":            "Assault with Injury (non-domestic)",
    "Terroristic Threat-Family":   "Terroristic Threat — Domestic",
    "Harassment":                  "Harassment",
}


def _crime_label(raw: str) -> str:
    """Return a clearer display label for a raw APD crime_type string."""
    return _CRIME_TYPE_LABELS.get(raw, raw)


def _get_crime_stats(start_date: str, end_date: str = None) -> dict:
    """Query APD Crime Reports API between two dates (YYYY-MM-DD)."""
    from datetime import datetime, timezone
    url = "https://data.austintexas.gov/resource/fdj4-gpfu.json"

    where = f"occ_date >= '{start_date}'"
    if end_date:
        where += f" AND occ_date <= '{end_date}'"

    params = {"$where": where, "$limit": 5000}
    app_token = os.getenv("AUSTIN_APP_TOKEN", "")
    headers = {"X-App-Token": app_token} if app_token else {}

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            crime_types = {}
            cleared = 0
            for incident in data:
                ct = incident.get("crime_type", "Unknown")
                crime_types[ct] = crime_types.get(ct, 0) + 1
                if incident.get("clearance_status") == "C":
                    cleared += 1
            top_crimes = sorted(crime_types.items(), key=lambda x: -x[1])[:5]
            return {"total": len(data), "cleared": cleared, "top_crimes": top_crimes}
    except Exception as e:
        logger.error(f"crime stats: {e}")

    return {"total": 0, "cleared": 0, "top_crimes": []}


# Austin city population estimates by year (US Census / City of Austin projections)
AUSTIN_POPULATION = {
    2014: 912_791,
    2015: 931_830,
    2016: 950_715,
    2017: 964_177,
    2018: 978_908,
    2019: 994_137,
    2020: 961_855,   # Census count (undercounting noted by city)
    2021: 974_447,
    2022: 979_882,
    2023: 985_000,
    2024: 995_000,
    2025: 1_005_000,
    2026: 1_015_000,
}


def _austin_population(year: int) -> int | None:
    """Return the best population estimate for a given year."""
    if year in AUSTIN_POPULATION:
        return AUSTIN_POPULATION[year]
    # Clamp to known range rather than extrapolate wildly
    if year < min(AUSTIN_POPULATION):
        return AUSTIN_POPULATION[min(AUSTIN_POPULATION)]
    if year > max(AUSTIN_POPULATION):
        return AUSTIN_POPULATION[max(AUSTIN_POPULATION)]
    return None


def _format_crime_stats(stats: dict, label: str) -> str:
    total = stats['total']
    clearance_pct = round(stats['cleared'] / total * 100) if total else 0
    msg = f"*{label}*\n"
    msg += f"📊 *{total}* total incidents\n"
    msg += f"✅ {clearance_pct}% cleared\n"
    if stats['top_crimes']:
        msg += "*Top Crime Types:*\n"
        for crime, count in stats['top_crimes']:
            pct = round(count / total * 100) if total else 0
            msg += f"• {_crime_label(crime)}: {count} ({pct}%)\n"
    return msg


def _get_nibrs_homicides() -> list:
    """Fetch all homicide offenses from NIBRS Group A dataset."""
    resp = requests.get(
        "https://data.austintexas.gov/resource/i7fg-wrk5.json",
        params={"$where": "nibrs_group='Homicide Offenses'", "$limit": 2000},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


async def crime_homicides_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Fetching homicide data...")
    try:
        records = await asyncio.to_thread(_get_nibrs_homicides)
        if not records:
            await query.edit_message_text("📝 No homicide records found.")
            return

        # Short labels for nibrs_desc
        TYPE_LABELS = {
            "09A Homicide: Murder and Non- Negligent Manslaughter": "Murder / Non-Neg. Manslaughter",
            "09B Homicide: Negligent Manslaughter":                  "Negligent Manslaughter",
            "09C Homicide: Justifiable Homicide (NOT A CRIME)":      "Justifiable Homicide",
        }
        CLEARED = {"CLEARED BY ARREST", "CLEARED EXCEPTIONALLY", "CLEARED ADMINISTRATIVELY"}

        by_year: dict = {}
        for r in records:
            year = int(r.get("occ_year") or 0)
            if not year:
                continue
            if year not in by_year:
                by_year[year] = {"total": 0, "cleared": 0, "types": {}}
            by_year[year]["total"] += 1
            if r.get("internal_clearance_status") in CLEARED:
                by_year[year]["cleared"] += 1
            label = TYPE_LABELS.get(r.get("nibrs_desc", ""), r.get("nibrs_desc", "Unknown"))
            by_year[year]["types"][label] = by_year[year]["types"].get(label, 0) + 1

        years = sorted(y for y in by_year if y >= 2019)
        cur_year = datetime.now(timezone.utc).year
        cur_month = datetime.now(timezone.utc).month

        # YTD comparison: current year vs last year same months
        ytd_now = sum(
            1 for r in records
            if int(r.get("occ_year") or 0) == cur_year
        )
        ytd_prev = sum(
            1 for r in records
            if int(r.get("occ_year") or 0) == cur_year - 1
            and datetime.strptime(r["occurred_date"][:7], "%Y-%m").month <= cur_month
        )
        if ytd_prev > 0:
            ytd_pct = round((ytd_now - ytd_prev) / ytd_prev * 100)
            ytd_arrow = "📈" if ytd_pct > 0 else "📉" if ytd_pct < 0 else "➡️"
            ytd_str = f"{ytd_arrow} {cur_year} YTD: *{ytd_now}* vs {cur_year-1} same period: *{ytd_prev}* ({'+' if ytd_pct > 0 else ''}{ytd_pct}%)"
        else:
            ytd_str = f"📊 {cur_year} YTD: *{ytd_now}*"

        # Overall clearance rate (excluding current partial year)
        full_years = [y for y in years if y < cur_year]
        total_full = sum(by_year[y]["total"] for y in full_years)
        cleared_full = sum(by_year[y]["cleared"] for y in full_years)
        open_full = sum(
            1 for r in records
            if int(r.get("occ_year") or 0) in full_years
            and r.get("internal_clearance_status") == "OPEN"
        )
        clearance_pct = round(cleared_full / total_full * 100) if total_full else 0

        msg = "⚰️ *Austin Homicides — NIBRS Data*\n"
        msg += f"_Murder, manslaughter & justifiable homicide · 2019–{cur_year}_\n\n"
        msg += f"{ytd_str}\n"
        msg += f"🔍 *Clearance rate:* {clearance_pct}% solved · 🔴 *Open/unsolved:* {open_full} ({min(full_years)}–{max(full_years)})\n\n"

        msg += "*Year-by-year (per 100k population):*\n"
        max_total = max(by_year[y]["total"] for y in years)
        for year in years:
            d = by_year[year]
            pop = _austin_population(year)
            rate = round(d["total"] / pop * 100_000, 1) if pop else None
            rate_str = f"  _{rate}/100k_" if rate else ""
            bar = "█" * min(10, round(d["total"] / max_total * 10))
            suffix = " *(partial)*" if year == cur_year else ""
            msg += f"*{year}:* {bar} {d['total']}{rate_str}{suffix}\n"

        # Type breakdown across all years
        type_totals: dict = {}
        for d in by_year.values():
            for t, c in d["types"].items():
                type_totals[t] = type_totals.get(t, 0) + c
        msg += "\n*By offense type (all years):*\n"
        total_all = sum(type_totals.values())
        for t, c in sorted(type_totals.items(), key=lambda x: -x[1]):
            pct = round(c / total_all * 100)
            msg += f"  • {t}: {c} ({pct}%)\n"

        await _send_chunked(query, msg)
    except Exception as e:
        logger.error(f"crime homicides: {e}")
        await query.edit_message_text(f"❌ Error: {e}")


async def crime_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from datetime import datetime, timedelta, timezone
    await update.message.reply_text("⏳ Fetching crime data...")
    try:
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=30)
        start_str = start.strftime("%Y-%m-%d")
        end_str = now.strftime("%Y-%m-%d")
        label = f"🚔 APD Crime — {start.strftime('%b %d')} to {now.strftime('%b %d, %Y')}"

        stats = await asyncio.to_thread(_get_crime_stats, start_str, end_str)
        msg = _format_crime_stats(stats, label)

        keyboard = [
            [InlineKeyboardButton(
                "📅 Compare to 10 years ago",
                callback_data=f"crime_compare_{start_str}_{end_str}"
            )],
            [InlineKeyboardButton(
                "⚰️ Homicides (2019–present)",
                callback_data="crime_homicides"
            )],
        ]
        await update.message.reply_text(
            msg,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    except Exception as e:
        logger.error(f"crime command: {e}")
        await update.message.reply_text(f"❌ Error fetching crime data: {e}")


async def crime_compare_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    # Parse the current window dates from callback data
    _, _, start_str, end_str = query.data.split("_", 3)

    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text("⏳ Fetching data from 10 years ago...")

    try:
        from datetime import datetime, timedelta, timezone
        start_then = datetime.strptime(start_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end_then = datetime.strptime(end_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        start_10y = (start_then.replace(year=start_then.year - 10)).strftime("%Y-%m-%d")
        end_10y = (end_then.replace(year=end_then.year - 10)).strftime("%Y-%m-%d")

        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            f_now = pool.submit(_get_crime_stats, start_str, end_str)
            f_then = pool.submit(_get_crime_stats, start_10y, end_10y)
            stats_now = f_now.result()
            stats_then = f_then.result()

        year_now = datetime.strptime(end_str, "%Y-%m-%d").year
        year_then = datetime.strptime(end_10y, "%Y-%m-%d").year
        pop_now = _austin_population(year_now)
        pop_then = _austin_population(year_then)

        now_label = f"🚔 APD Crime — {datetime.strptime(start_str, '%Y-%m-%d').strftime('%b %d')} to {datetime.strptime(end_str, '%Y-%m-%d').strftime('%b %d, %Y')}"
        then_label = f"📅 Same Period — {datetime.strptime(start_10y, '%Y-%m-%d').strftime('%b %d')} to {datetime.strptime(end_10y, '%Y-%m-%d').strftime('%b %d, %Y')}"

        msg = _format_crime_stats(stats_now, now_label)
        msg += "\n"
        msg += _format_crime_stats(stats_then, then_label)

        if stats_then['total'] > 0 and pop_now and pop_then:
            # Scale 30-day window to annualised per-100k for a fair comparison
            rate_now = round(stats_now['total'] / pop_now * 100_000)
            rate_then = round(stats_then['total'] / pop_then * 100_000)
            rate_diff = rate_now - rate_then
            rate_pct = round(abs(rate_diff) / rate_then * 100)
            direction = "📈" if rate_diff > 0 else "📉"
            msg += f"\n*Per-capita rate (per 100k residents):*\n"
            msg += f"{direction} {rate_now}/100k now vs. {rate_then}/100k in {year_then}\n"
            msg += f"{'Up' if rate_diff > 0 else 'Down'} *{rate_pct}%* adjusted for population growth\n"
            msg += f"_Austin population: ~{pop_now:,} ({year_now}) vs. ~{pop_then:,} ({year_then})_"
        elif stats_then['total'] > 0:
            diff = stats_now['total'] - stats_then['total']
            pct = round(abs(diff) / stats_then['total'] * 100)
            direction = "📈 up" if diff > 0 else "📉 down"
            msg += f"\n{direction} *{pct}%* raw vs. 10 years ago ({diff:+,} incidents)"

        await query.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"crime compare cb: {e}")
        await query.message.reply_text(f"❌ Error: {e}")


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


DISTRICT_LABELS = {
    "1": "1 · East",
    "2": "2 · SE Austin",
    "3": "3 · South",
    "4": "4 · NE Austin",
    "5": "5 · Central",
    "6": "6 · NW Austin",
    "7": "7 · North",
    "8": "8 · SW Austin",
    "9": "9 · E Central",
    "10": "10 · Far NW",
}


async def safety_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [InlineKeyboardButton(DISTRICT_LABELS[str(d)], callback_data=f"safety_district_{d}") for d in pair]
        for pair in [(1, 2), (3, 4), (5, 6), (7, 8), (9, 10)]
    ]
    await update.message.reply_text(
        "🔍 *Safety by District*\n\nPick a council district to see crime stats and how it compares to the rest of Austin:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def safety_district_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    district = query.data.replace("safety_district_", "")
    label = DISTRICT_LABELS.get(district, f"District {district}")
    await query.edit_message_text(f"⏳ Fetching crime data for {label}...")

    try:
        from datetime import datetime, timedelta, timezone
        start_date = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
        url = "https://data.austintexas.gov/resource/fdj4-gpfu.json"
        app_token = os.getenv("AUSTIN_APP_TOKEN", "")
        headers = {"X-App-Token": app_token} if app_token else {}

        # Fetch district data and citywide data in parallel via two requests
        import concurrent.futures

        def fetch(params):
            r = requests.get(url, params=params, headers=headers, timeout=30)
            r.raise_for_status()
            return r.json()

        district_params = {
            "$where": f"council_district='{district}' AND occ_date >= '{start_date}'",
            "$limit": 1000,
        }
        city_params = {
            "$where": f"occ_date >= '{start_date}'",
            "$limit": 5000,
        }

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            f_district = pool.submit(fetch, district_params)
            f_city = pool.submit(fetch, city_params)
            district_data = f_district.result()
            city_data = f_city.result()

        d_total = len(district_data)
        city_total = len(city_data)

        if d_total == 0:
            await query.edit_message_text(f"✅ No incidents reported in {label} (last 30 days)")
            return

        # Per-district counts from citywide data for ranking
        district_counts = {}
        for inc in city_data:
            d = inc.get("council_district", "Unknown")
            district_counts[d] = district_counts.get(d, 0) + 1

        ranked = sorted(district_counts.items(), key=lambda x: -x[1])
        rank = next((i + 1 for i, (d, _) in enumerate(ranked) if d == district), None)
        num_districts = len(ranked)
        city_avg = round(city_total / num_districts) if num_districts else 0
        pct_of_city = round(d_total / city_total * 100) if city_total else 0
        diff = d_total - city_avg
        vs_avg = f"+{diff} above avg" if diff > 0 else f"{abs(diff)} below avg"

        # Crime type breakdown for this district
        crime_types = {}
        cleared = 0
        for inc in district_data:
            ct = inc.get("crime_type", "Unknown")
            crime_types[ct] = crime_types.get(ct, 0) + 1
            if inc.get("clearance_status") == "C":
                cleared += 1

        top_crimes = sorted(crime_types.items(), key=lambda x: -x[1])[:5]
        clearance_pct = round(cleared / d_total * 100)

        msg = f"🔍 *{label}* — Last 30 Days\n\n"
        msg += f"📊 *{d_total}* incidents ({pct_of_city}% of city total)\n"
        msg += f"🏙 City avg: {city_avg}/district  ·  {vs_avg}\n"
        if rank:
            msg += f"📈 Ranked #{rank} of {num_districts} districts\n"
        msg += f"✅ {clearance_pct}% of cases cleared\n\n"
        msg += "*Top Crime Types:*\n"
        for crime, count in top_crimes:
            pct = round(count / d_total * 100) if d_total else 0
            msg += f"• {_crime_label(crime)}: {count} ({pct}%)\n"

        await _send_chunked(query, msg)
    except Exception as e:
        logger.error(f"safety district cb: {e}")
        await query.edit_message_text(f"❌ Error: {e}")


async def police_crime_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from datetime import datetime, timedelta, timezone
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Fetching crime data...")
    try:
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=30)
        start_str = start.strftime("%Y-%m-%d")
        end_str = now.strftime("%Y-%m-%d")
        label = f"🚔 APD Crime — {start.strftime('%b %d')} to {now.strftime('%b %d, %Y')}"

        stats = await asyncio.to_thread(_get_crime_stats, start_str, end_str)
        msg = _format_crime_stats(stats, label)

        keyboard = [
            [InlineKeyboardButton("📅 Compare to 10 years ago", callback_data=f"crime_compare_{start_str}_{end_str}")],
            [InlineKeyboardButton("⚰️ Homicides (2019–present)", callback_data="crime_homicides")],
        ]
        await query.edit_message_text(
            msg, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.error(f"police crime cb: {e}")
        await query.edit_message_text(f"❌ Error fetching crime data: {e}")


async def police_safety_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    keyboard = [
        [InlineKeyboardButton(DISTRICT_LABELS[str(d)], callback_data=f"safety_district_{d}") for d in pair]
        for pair in [(1, 2), (3, 4), (5, 6), (7, 8), (9, 10)]
    ]
    await query.edit_message_text(
        "🔍 *Safety by District*\n\nPick a council district to see crime stats and how it compares to the rest of Austin:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# =============================================================================
# HATE CRIMES DATA (APD Hate Crimes dataset t99n-5ib4)
# =============================================================================

_HATE_BIAS_LABELS: dict[str, str] = {
    "Anti-Black or African American": "Anti-Black/African American",
    "Anti-Jewish":                    "Anti-Jewish",
    "Anti-White":                     "Anti-White",
    "Anti-Hispanic or Latino":        "Anti-Hispanic/Latino",
    "Anti-Gay (Male)":                "Anti-Gay (Male)",
    "Anti-Lesbian, Gay, Bisexual, or Transgender (Mixed Group)": "Anti-LGBTQ+",
    "Anti-Lesbian":                   "Anti-Lesbian",
    "Anti-Islamic (Muslim)":          "Anti-Muslim",
    "Anti-Asian":                     "Anti-Asian",
    "Anti-Other Race/Ethnicity/Ancestry": "Anti-Other Race",
    "Anti-Arab":                      "Anti-Arab",
    "Anti-Transgender":               "Anti-Transgender",
    "Anti-Gender Non-Conforming":     "Anti-Gender Non-Conforming",
    "Anti-Female":                    "Anti-Female",
    "Anti-Male":                      "Anti-Male",
    "Anti-Mental Disability":         "Anti-Mental Disability",
    "Anti-Physical Disability":       "Anti-Physical Disability",
    "Anti-Catholic":                  "Anti-Catholic",
    "Anti-Protestant":                "Anti-Protestant",
    "Anti-Other Christian":           "Anti-Other Christian",
    "Anti-Other Religion":            "Anti-Other Religion",
    "Anti-Multiple Races, Group":     "Anti-Multiple Races",
}


def _get_hate_crimes(years: int = 3) -> dict:
    """Fetch hate crime incidents from APD dataset, last N years."""
    from datetime import date
    url = "https://data.austintexas.gov/resource/t99n-5ib4.json"
    cutoff = f"{date.today().year - years}-01-01T00:00:00.000"
    params = {
        "$where":  f"date_of_incident >= '{cutoff}'",
        "$order":  "date_of_incident DESC",
        "$limit":  5000,
        "$select": "date_of_incident,bias,race_ethnicity_of_offenders,offense_s,zip_code",
    }
    app_token = os.getenv("AUSTIN_APP_TOKEN")
    headers = {"X-App-Token": app_token} if app_token else {}
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if not data:
            return {"total": 0, "years": years, "bias": {}, "race": {}, "offenses": {}}

        bias_counts: dict[str, int] = {}
        race_counts: dict[str, int] = {}
        offense_counts: dict[str, int] = {}

        for row in data:
            b = row.get("bias", "Unknown") or "Unknown"
            bias_counts[b] = bias_counts.get(b, 0) + 1

            r = row.get("race_ethnicity_of_offenders", "Unknown") or "Unknown"
            race_counts[r] = race_counts.get(r, 0) + 1

            o = row.get("offense_s", "Unknown") or "Unknown"
            offense_counts[o] = offense_counts.get(o, 0) + 1

        return {
            "total":    len(data),
            "years":    years,
            "bias":     dict(sorted(bias_counts.items(), key=lambda x: -x[1])),
            "race":     dict(sorted(race_counts.items(), key=lambda x: -x[1])),
            "offenses": dict(sorted(offense_counts.items(), key=lambda x: -x[1])),
        }
    except Exception as e:
        logger.error(f"hate crimes fetch: {e}")
        return {"total": 0, "years": years, "bias": {}, "race": {}, "offenses": {}}


def _format_hate_crimes(data: dict) -> str:
    total = data["total"]
    years = data["years"]
    if total == 0:
        return f"🎯 *Austin Hate Crimes*\n\nNo data found for the last {years} years."

    from datetime import date
    end_yr = date.today().year
    start_yr = end_yr - years
    msg = f"🎯 *Austin Hate Crimes — {start_yr}–{end_yr}*\n_{total} reported incidents_\n\n"

    msg += "*Bias Motivation:*\n"
    for raw, count in list(data["bias"].items())[:8]:
        label = _HATE_BIAS_LABELS.get(raw, raw)
        pct = round(count / total * 100)
        msg += f"• {label}: {count} ({pct}%)\n"

    msg += "\n*Offender Race/Ethnicity:*\n"
    for raw, count in list(data["race"].items())[:7]:
        label = raw.strip() or "Unknown"
        pct = round(count / total * 100)
        msg += f"• {label}: {count} ({pct}%)\n"

    msg += "\n*Top Offenses:*\n"
    for raw, count in list(data["offenses"].items())[:5]:
        pct = round(count / total * 100)
        msg += f"• {raw}: {count} ({pct}%)\n"

    msg += f"\n_Source: [APD Hate Crimes Dataset](https://data.austintexas.gov/d/t99n-5ib4)_"
    return msg


async def hate_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("⏳ Fetching hate crime data...")
    try:
        data = await asyncio.to_thread(_get_hate_crimes, 3)
        msg = _format_hate_crimes(data)
        await _send_chunked(update.message.reply_text, msg, parse_mode="Markdown",
                            disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"hate command: {e}")
        await update.message.reply_text(f"❌ Error fetching hate crime data: {e}")


async def police_hate_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Fetching hate crime data...")
    try:
        data = await asyncio.to_thread(_get_hate_crimes, 3)
        msg = _format_hate_crimes(data)
        keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="service_police")]]
        await query.edit_message_text(msg, parse_mode="Markdown",
                                      reply_markup=InlineKeyboardMarkup(keyboard),
                                      disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"police hate cb: {e}")
        await query.edit_message_text(f"❌ Error fetching hate crime data: {e}")


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
    app.add_handler(CallbackQueryHandler(bicycle_ticket_cb, pattern="^bicycle_ticket_"))

    # Restaurant inline
    app.add_handler(CallbackQueryHandler(restaurants_lowscores_cb, pattern="^restaurants_lowscores"))
    app.add_handler(CallbackQueryHandler(restaurants_grades_cb, pattern="^restaurants_grades"))

    # Animal inline
    app.add_handler(CallbackQueryHandler(animal_hotspots_cb, pattern="^animal_hotspots"))
    app.add_handler(CallbackQueryHandler(animal_stats_cb, pattern="^animal_stats"))

    # Traffic inline
    app.add_handler(CallbackQueryHandler(traffic_backlog_cb, pattern="^traffic_backlog"))
    app.add_handler(CallbackQueryHandler(traffic_signals_cb, pattern="^traffic_signals"))
    app.add_handler(CallbackQueryHandler(ticket_lookup_cb, pattern="^tlookup_"))

    # Noise inline
    app.add_handler(CallbackQueryHandler(noise_hotspots_cb, pattern="^noise_hotspots"))
    app.add_handler(CallbackQueryHandler(noise_peak_cb, pattern="^noise_peak"))
    app.add_handler(CallbackQueryHandler(noise_resolution_cb, pattern="^noise_resolution"))
    app.add_handler(CallbackQueryHandler(noise_night_cb, pattern="^noise_night"))

    # Parking slash command + inline
    app.add_handler(CommandHandler("parking", parking_command))
    app.add_handler(CallbackQueryHandler(parking_stats_cb, pattern="^parking_stats"))
    app.add_handler(CallbackQueryHandler(parking_hotspots_cb, pattern="^parking_hotspots"))
    app.add_handler(CallbackQueryHandler(parking_resolution_cb, pattern="^parking_resolution"))
    app.add_handler(CallbackQueryHandler(parking_abandoned_cb, pattern="^parking_abandoned"))
    app.add_handler(CallbackQueryHandler(parking_top_payments_cb, pattern="^parking_top_payments"))

    # Graffiti slash command
    app.add_handler(CommandHandler("graffiti", graffiti_command))

    # Crime slash command + inline
    app.add_handler(CommandHandler("crime", crime_command))
    app.add_handler(CallbackQueryHandler(crime_compare_cb, pattern="^crime_compare_"))
    app.add_handler(CallbackQueryHandler(crime_homicides_cb, pattern="^crime_homicides"))

    # Safety slash command + inline
    app.add_handler(CommandHandler("safety", safety_command))
    app.add_handler(CallbackQueryHandler(safety_district_cb, pattern="^safety_district_"))

    # Police & Crime menu inline
    app.add_handler(CallbackQueryHandler(police_crime_cb, pattern="^police_crime$"))
    app.add_handler(CallbackQueryHandler(police_safety_cb, pattern="^police_safety$"))
    app.add_handler(CallbackQueryHandler(police_hate_cb, pattern="^police_hate$"))

    # Hate crimes slash command
    app.add_handler(CommandHandler("hate", hate_command))

    # Bicycle slash commands
    app.add_handler(CommandHandler("animal", animal_command))
    app.add_handler(CommandHandler("bicycle", bicycle_command))
    app.add_handler(CommandHandler("ticket", ticket_command))

    # Restaurant slash command
    app.add_handler(CommandHandler("rest", restaurant_command))

    # Traffic slash command
    app.add_handler(CommandHandler("traffic", traffic_command))

    # Noise slash command
    app.add_handler(CommandHandler("noise", noisecomplaints_command))

    # Report slash command
    app.add_handler(CommandHandler("report", report_command))

    # Code violations slash command
    app.add_handler(CommandHandler("code", code_command))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo_handler))
    app.add_error_handler(error_handler)

    # Register commands with Telegram so they appear in autocomplete
    async def post_init(application) -> None:
        await application.bot.set_my_commands([
            BotCommand("start",    "Main menu"),
            BotCommand("crime",    "Recent APD crime stats"),
            BotCommand("hate",     "Hate crime incidents — bias · offender race · offense type"),
            BotCommand("safety",   "Crime by district — stats + city comparison"),
            BotCommand("traffic",  "Traffic & infrastructure — signals · lights · sidewalks"),
            BotCommand("parking",  "Parking enforcement — citations · hot zones · stats"),
            BotCommand("bicycle",  "Bicycle complaints — recent · stats"),
            BotCommand("rest",     "Restaurant inspections — worst scores · grades · search"),
            BotCommand("noise",    "Noise complaints — hotspots · stats · response times"),
            BotCommand("graffiti", "Graffiti — analysis · hotspots · remediation"),
            BotCommand("animal",   "Animal complaints — hotspots · stats · response times"),
            BotCommand("ticket",   "Look up any 311 ticket by ID"),
            BotCommand("help",     "All commands"),
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
