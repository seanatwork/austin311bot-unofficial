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
    ConversationHandler,
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

# Coyote complaints (sub-service of animal)
from animalsvc.coyote_bot import (
    get_seasonal_patterns,
    get_coyote_overview,
    get_hotspots as get_coyote_hotspots,
    format_seasonal_patterns,
    format_overview as format_coyote_overview,
    format_hotspots as format_coyote_hotspots,
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

# Child care licensing service
from childcare.childcare_bot import get_childcare_stats, format_childcare

# Water conservation violations
from waterconservation.water_conservation_bot import get_water_conservation_stats, format_water_conservation

# Homeless encampment & trash 311 reports
from homeless.homeless_bot import (
    get_encampment_stats,
    format_encampment_stats,
    format_encampment_locations,
    generate_encampment_map,
)

# Parks maintenance service
from parks.parks_bot import (
    get_park_stats,
    get_park_hotspots,
    get_park_resolution,
    get_park_detail,
    format_stats as format_park_stats,
    format_hotspots as format_park_hotspots,
    format_resolution as format_park_resolution,
    format_park_detail,
    format_unified_overview,
    build_park_name_keyboard,
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
# RATE LIMITING (Global: 30 requests per 60 seconds across all users)
# No user data is stored or tracked.
# =============================================================================

from time import time

_RATE_LIMIT_MAX = 30      # Max requests per window (global)
_RATE_LIMIT_WINDOW = 60   # Window in seconds
_request_times: list[float] = []


def _is_rate_limited() -> tuple[bool, int]:
    """Check if global rate limit is hit. Returns (is_limited, retry_after_seconds)."""
    now = time()
    window_start = now - _RATE_LIMIT_WINDOW
    global _request_times
    
    # Remove old entries outside the window
    _request_times = [t for t in _request_times if t > window_start]
    
    if len(_request_times) >= _RATE_LIMIT_MAX:
        retry_after = int(_request_times[0] + _RATE_LIMIT_WINDOW - now) + 1
        return True, max(1, retry_after)
    
    _request_times.append(now)
    return False, 0


def rate_limited(handler):
    """Decorator to apply global rate limiting. No user data is collected."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        limited, retry = _is_rate_limited()
        if limited:
            msg = f"⏳ Bot is busy. Please try again in {retry}s."
            if update.callback_query:
                await update.callback_query.answer()
                await update.callback_query.edit_message_text(msg)
            elif update.message:
                await update.message.reply_text(msg)
            return
        return await handler(update, context)
    return wrapper


# =============================================================================
# TICKET ID VALIDATION
# =============================================================================

_TICKET_ID_PATTERN = re.compile(r'^[0-9]{2}-[0-9]{8}$')


def _validate_ticket_id(ticket_id: str) -> tuple[bool, str]:
    """Validate 311 ticket ID format. Returns (is_valid, error_message)."""
    if not ticket_id:
        return False, "Ticket ID cannot be empty."
    
    ticket_id = ticket_id.strip()
    
    # Check length (2 + 1 + 8 = 11 chars)
    if len(ticket_id) != 11:
        return False, f"Invalid ticket ID length. Expected 11 characters (YY-XXXXXXXX), got {len(ticket_id)}."
    
    # Check pattern: YY-XXXXXXXX
    if not _TICKET_ID_PATTERN.match(ticket_id):
        return False, "Invalid ticket ID format. Use: YY-XXXXXXXX (e.g., 16-00123456)"
    
    # Validate year (00-99 is technically valid, but reject obviously wrong ones)
    year_part = int(ticket_id[:2])
    if year_part > 50:  # Assume tickets from 2050+ are errors
        return False, "Invalid year in ticket ID."
    
    return True, ""


# =============================================================================
# HELPERS
# =============================================================================


async def _send_chunked(target, text: str, parse_mode: str = "Markdown", reply_markup=None) -> None:
    """Send long messages in ≤4000-char chunks (Telegram limit is 4096).

    reply_markup is attached to the last chunk only.
    """
    chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)]
    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        markup = reply_markup if is_last else None
        if i == 0 and hasattr(target, "edit_message_text"):
            await target.edit_message_text(chunk, parse_mode=parse_mode, disable_web_page_preview=True, reply_markup=markup)
        else:
            msg = target.message if hasattr(target, "message") else target
            await msg.reply_text(chunk, parse_mode=parse_mode, disable_web_page_preview=True, reply_markup=markup)


# =============================================================================
# MAIN MENU
# =============================================================================


@rate_limited
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [InlineKeyboardButton("🚔 Police & Crime", callback_data="service_police")],
        [InlineKeyboardButton("💰🏦 City Budget", callback_data="service_budget")],
        [InlineKeyboardButton("🚦 Traffic & Infrastructure", callback_data="service_traffic")],
        [InlineKeyboardButton("🚴 Bicycle", callback_data="service_bicycle")],
        [InlineKeyboardButton("💧 Water Quality", callback_data="service_water")],
        [InlineKeyboardButton("🎨 Graffiti", callback_data="service_graffiti")],
        [InlineKeyboardButton("🍽️ Restaurants", callback_data="service_restaurants")],
        [InlineKeyboardButton("🐾 Animal Services", callback_data="service_animal")],
        [InlineKeyboardButton("🔊 Noise Complaints", callback_data="service_noise")],
        [InlineKeyboardButton("🅿️ Parking", callback_data="service_parking")],
        [InlineKeyboardButton("🏞️ Parks", callback_data="service_parks")],
        [InlineKeyboardButton("ℹ️ About", callback_data="about")],
    ]
    await update.message.reply_text(
        "📡 *Welcome to ATX Pulse!*\n\nSelect a service:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


_HELP_TEXT = """📡 *ATX PULSE*

🚔 *Police & Crime:*
/crime — Recent APD incident stats (citywide)
/safety — Crime by district with city comparison

💰🏦 *City Budget:*
/budget — Homelessness services, NGO grants, pension & benefits

🚦 *Traffic & Infrastructure:*
/traffic — Potholes · signals · live incidents · crash stats

🚴 *Bicycle:*
/bicycle — Recent complaints · stats

💧 *Water Quality:*
/water — Surface water quality by watershed
/waterviolations — Water conservation violations · sprinklers · leaks

🎨 *Graffiti:*
/graffiti — Analysis · hotspots · remediation · trends

🍽️ *Restaurants:*
/rest — Worst scores · grade report
/rest <name or address> — Search directly

🐾 *Animal Services:*
/animal — Hotspots · stats · response times

🐺 *Coyote Complaints:*
/coyote — Seasonal patterns · hotspots · overview

🔊 *Noise Complaints:*
/noise — Hotspots · stats · response times

🅿️ *Parking:*
/parking — Citations · hot zones · stats

🏞️ *Parks:*
/parks — Hotspots · stats · resolution times

🎫 *Ticket Lookup:*
/ticket <id> — Look up any 311 ticket by ID

🏗️ *Building Permits:*
/permits — Permit activity last 30 days

🍺 *Bar of the Month:*
/bars — Top TABC mixed beverage sales · biggest movers

🧒 *Child Care:*
/childcare — Austin licensed facilities · compliance flags · top deficiencies

🏊 *Pool Hours:* https://www.austintexas.gov/parks/locations/pools-and-splash-pads

_This bot does not collect, store, or transmit any user data. All requests are processed anonymously._

ℹ️ /start — Main menu  |  /help — This message"""


@rate_limited
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(_HELP_TEXT, parse_mode="Markdown")


# =============================================================================
# SERVICE SUBMENUS (inline buttons)
# =============================================================================


@rate_limited
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
            [InlineKeyboardButton("🚨 Live Incidents", callback_data="traffic_live"),
             InlineKeyboardButton("💥 Crash Stats", callback_data="traffic_crashes")],
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

    elif service == "parks":
        keyboard = [
            [InlineKeyboardButton("Overview", callback_data="parks_overview"),
             InlineKeyboardButton("Resolution Times", callback_data="parks_resolution")],
            [InlineKeyboardButton("Change Time Window", callback_data="parks_time_window")],
            [InlineKeyboardButton("Back", callback_data="back_to_main")],
        ]
        text = "*Parks Maintenance*\nTrack unresolved complaints by park. Useful for choosing where to go."

    elif service == "police":
        keyboard = [
            [InlineKeyboardButton("🚔 Crime Stats", callback_data="police_crime"),
             InlineKeyboardButton("🛡️ Safety by District", callback_data="police_safety")],
            [InlineKeyboardButton("Hate Crimes", callback_data="police_hate")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_to_main")],
        ]
        text = "*🚔 Police & Crime*\nAPD incident stats, safety by district, hate crimes, and homelessness spending."

    elif service == "budget":
        await query.edit_message_text("⏳ Fetching budget insights...")
        try:
            data = await asyncio.to_thread(_get_homeless_budget)
            msg = _format_homeless_budget(data)
        except Exception as e:
            logger.error(f"service_budget: {e}")
            await query.edit_message_text(f"❌ Error fetching budget data: {e}")
            return
        back = [[InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]]
        await query.edit_message_text(
            msg, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(back),
            disable_web_page_preview=True,
        )
        return

    elif service == "water":
        await query.edit_message_text("⏳ Fetching water quality data...")
        try:
            data = await asyncio.to_thread(_get_water_quality)
            msg = _format_water_quality(data)
        except Exception as e:
            logger.error(f"service_water: {e}")
            await query.edit_message_text(f"❌ Error fetching water quality data: {e}")
            return
        back = [[InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]]
        await query.edit_message_text(
            msg, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(back),
            disable_web_page_preview=True,
        )
        return

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


@rate_limited
async def back_to_main(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("🚔 Police & Crime", callback_data="service_police")],
        [InlineKeyboardButton("💰🏦 City Budget", callback_data="service_budget")],
        [InlineKeyboardButton("🚦 Traffic & Infrastructure", callback_data="service_traffic")],
        [InlineKeyboardButton("🚴 Bicycle", callback_data="service_bicycle")],
        [InlineKeyboardButton("💧 Water Quality", callback_data="service_water")],
        [InlineKeyboardButton("🎨 Graffiti", callback_data="service_graffiti")],
        [InlineKeyboardButton("🍽️ Restaurants", callback_data="service_restaurants")],
        [InlineKeyboardButton("🐾 Animal Services", callback_data="service_animal")],
        [InlineKeyboardButton("🔊 Noise Complaints", callback_data="service_noise")],
        [InlineKeyboardButton("🅿️ Parking", callback_data="service_parking")],
        [InlineKeyboardButton("🏞️ Parks", callback_data="service_parks")],
        [InlineKeyboardButton("ℹ️ About", callback_data="about")],
    ]
    await query.edit_message_text(
        "📡 *Welcome to ATX Pulse!*\n\nSelect a service:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def about_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]]
    await query.edit_message_text(
        _HELP_TEXT,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# =============================================================================
# GRAFFITI HANDLERS
# =============================================================================


@rate_limited
async def graffiti_analyze_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Analyzing graffiti data...")
    try:
        result = await asyncio.to_thread(analyze_graffiti_command, 90)
        await _send_chunked(query, result)
    except Exception as e:
        logger.error(f"graffiti analyze failed: {e}", exc_info=True)
        await query.edit_message_text(f"❌ Error: {e}")



@rate_limited
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



@rate_limited
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


@rate_limited
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
    
    # Validate ticket ID format
    is_valid, error_msg = _validate_ticket_id(ticket_id)
    if not is_valid:
        await query.edit_message_text(f"❌ {error_msg}")
        return
    
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


@rate_limited
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


@rate_limited
async def ticket_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: `/ticket <ticket-id>`\nExample: `/ticket 16-00123456`", parse_mode="Markdown")
        return
    ticket_id = context.args[0]
    
    # Validate ticket ID format
    is_valid, error_msg = _validate_ticket_id(ticket_id)
    if not is_valid:
        await update.message.reply_text(f"❌ {error_msg}\n\nUsage: `/ticket <ticket-id>`\nExample: `/ticket 16-00123456`", parse_mode="Markdown")
        return
    
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


@rate_limited
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


@rate_limited
async def animal_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [InlineKeyboardButton("🗺 Hotspots", callback_data="animal_hotspots"),
         InlineKeyboardButton("📊 Stats", callback_data="animal_stats")],
        [InlineKeyboardButton("🐺 Coyote Complaints", callback_data="coyote_menu")],
    ]
    await update.message.reply_text(
        "*🐾 Animal Services*\nChoose a view:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# =============================================================================
# COYOTE COMPLAINT HANDLERS (Sub-service of Animal)
# =============================================================================


async def coyote_menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    
    keyboard = [
        [InlineKeyboardButton("📊 Overview", callback_data="coyote_overview"),
         InlineKeyboardButton("🌸 Seasonal", callback_data="coyote_seasonal")],
        [InlineKeyboardButton("🗺 Hotspots", callback_data="coyote_hotspots")],
        [InlineKeyboardButton("🔙 Back to Animal", callback_data="service_animal")],
    ]
    text = (
        "🐺 *Coyote Complaints*\n\n"
        "Austin sees ~11K coyote complaints. Pupping season (March–May) "
        "typically spikes activity as parents seek food and defend dens.\n\n"
        "Choose a view:"
    )
    await query.edit_message_text(
        text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def coyote_overview_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Fetching coyote complaint overview...")
    try:
        data = await asyncio.to_thread(get_coyote_overview)
        await _send_chunked(query, format_coyote_overview(data))
    except Exception as e:
        logger.error(f"coyote overview: {e}")
        await query.edit_message_text(f"❌ Error: {e}")


async def coyote_seasonal_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Analyzing seasonal patterns...")
    try:
        data = await asyncio.to_thread(get_seasonal_patterns)
        await _send_chunked(query, format_seasonal_patterns(data))
    except Exception as e:
        logger.error(f"coyote seasonal: {e}")
        await query.edit_message_text(f"❌ Error: {e}")


async def coyote_hotspots_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Finding coyote complaint hotspots...")
    try:
        data = await asyncio.to_thread(get_coyote_hotspots)
        await _send_chunked(query, format_coyote_hotspots(data))
    except Exception as e:
        logger.error(f"coyote hotspots: {e}")
        await query.edit_message_text(f"❌ Error: {e}")


@rate_limited
async def coyote_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [InlineKeyboardButton("📊 Overview", callback_data="coyote_overview"),
         InlineKeyboardButton("🌸 Seasonal", callback_data="coyote_seasonal")],
        [InlineKeyboardButton("🗺 Hotspots", callback_data="coyote_hotspots")],
        [InlineKeyboardButton("🔙 Back", callback_data="service_animal")],
    ]
    await update.message.reply_text(
        "🐺 *Coyote Complaints*\n\n"
        "Coyote pupping season is March–May. Choose a view:",
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
    
    # Validate ticket ID format
    is_valid, error_msg = _validate_ticket_id(ticket_id)
    if not is_valid:
        await query.edit_message_text(f"❌ {error_msg}")
        return
    
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


@rate_limited
async def traffic_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [InlineKeyboardButton("📋 Infra Backlog", callback_data="traffic_backlog"),
         InlineKeyboardButton("🚦 Broken Signals", callback_data="traffic_signals")],
        [InlineKeyboardButton("🚨 Live Incidents", callback_data="traffic_live"),
         InlineKeyboardButton("💥 Crash Stats", callback_data="traffic_crashes")],
    ]
    await update.message.reply_text(
        "*🚦 Traffic & Infrastructure*\nChoose a view:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# =============================================================================
# LIVE TRAFFIC INCIDENTS (Real-Time Traffic Incident Reports dx9v-zd7x)
# =============================================================================

_TRAFFIC_SESSION = None

def _get_traffic_session():
    global _TRAFFIC_SESSION
    if _TRAFFIC_SESSION is None:
        _TRAFFIC_SESSION = requests.Session()
        app_token = os.getenv("AUSTIN_APP_TOKEN")
        if app_token:
            _TRAFFIC_SESSION.headers.update({"X-App-Token": app_token})
    return _TRAFFIC_SESSION


# Normalise the inconsistent casing in issue_reported
_INCIDENT_LABELS: dict[str, str] = {
    "crash urgent":               "Crash (urgent)",
    "collision":                  "Collision",
    "collision with injury":      "Collision w/ injury",
    "collisn/ lvng scn":          "Collision — leaving scene",
    "collision/private property": "Collision (private property)",
    "traffic fatality":           "Traffic fatality",
    "crash service":              "Crash (service)",
    "traffic hazard":             "Traffic hazard",
    "trfc hazd/ debris":          "Hazard / debris",
    "stalled vehicle":            "Stalled vehicle",
    "vehicle fire":               "Vehicle fire",
    "loose livestock":            "Loose livestock",
}

def _normalise_incident(raw: str) -> str:
    return _INCIDENT_LABELS.get(raw.lower().strip(), raw.title())


def _get_live_incidents() -> list[dict]:
    """Fetch currently ACTIVE traffic incidents."""
    session = _get_traffic_session()
    resp = session.get(
        "https://data.austintexas.gov/resource/dx9v-zd7x.json",
        params={
            "$where":  "traffic_report_status='ACTIVE'",
            "$order":  "published_date DESC",
            "$limit":  50,
        },
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def _format_live_incidents(rows: list[dict]) -> str:
    if not rows:
        return "🚨 *Live Traffic Incidents*\n\n✅ No active incidents reported right now."

    now = datetime.now(timezone.utc)

    # Group by normalised type
    by_type: dict[str, list[dict]] = {}
    for r in rows:
        label = _normalise_incident(r.get("issue_reported", "Unknown"))
        by_type.setdefault(label, []).append(r)

    msg = f"🚨 *Live Traffic Incidents* — {len(rows)} active\n\n"

    # Severity-first ordering: fatalities and collisions first
    priority = ["Traffic fatality", "Collision w/ injury", "Collision (urgent)",
                "Crash (urgent)", "Collision", "Collision — leaving scene"]
    ordered = sorted(by_type.keys(), key=lambda k: (priority.index(k) if k in priority else 99, k))

    for label in ordered:
        incidents = by_type[label]
        msg += f"*{label}* ({len(incidents)})\n"
        for inc in incidents[:5]:  # cap per-type list
            addr = inc.get("address", "Unknown location")
            pub = inc.get("published_date", "")
            try:
                dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
                age_min = int((now - dt).total_seconds() / 60)
                age_str = f"{age_min}m ago" if age_min < 60 else f"{age_min // 60}h {age_min % 60}m ago"
            except Exception:
                age_str = ""
            agency = inc.get("agency", "")
            msg += f"  • {addr}"
            if age_str:
                msg += f" · {age_str}"
            if agency:
                msg += f" · {agency}"
            msg += "\n"
        if len(incidents) > 5:
            msg += f"  _+{len(incidents) - 5} more_\n"
        msg += "\n"

    msg += "_Source: [Real-Time Traffic Incidents](https://data.austintexas.gov/d/dx9v-zd7x)_"
    return msg


async def traffic_live_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Fetching live traffic incidents...")
    try:
        rows = await asyncio.to_thread(_get_live_incidents)
        msg = _format_live_incidents(rows)
        keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="service_traffic")]]
        await query.edit_message_text(msg, parse_mode="Markdown",
                                      reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        logger.error(f"traffic live cb: {e}")
        await query.edit_message_text(f"❌ Error fetching live incidents: {e}")


# =============================================================================
# CRASH STATS (Austin Crash Report Data y2wy-tgr5)
# =============================================================================

_CRASH_SEV: dict[str, str] = {
    "1": "Fatal",
    "2": "Serious injury",
    "3": "Minor injury",
    "4": "Possible injury",
    "5": "Property damage only",
    "0": "Unknown",
}


_COLLSN_LABELS: dict[str, str] = {
    "SAME DIRECTION - BOTH GOING STRAIGHT-REAR END":     "Rear-end",
    "ONE MOTOR VEHICLE - GOING STRAIGHT":                "Single vehicle",
    "ANGLE - BOTH GOING STRAIGHT":                       "Angle (T-bone)",
    "SAME DIRECTION - ONE STRAIGHT-ONE STOPPED":         "Hit stopped vehicle",
    "SAME DIRECTION - BOTH GOING STRAIGHT-SIDESWIPE":    "Sideswipe",
    "OPPOSITE DIRECTION - ONE STRAIGHT-ONE LEFT TURN":   "Head-on / left turn",
    "ANGLE - ONE STRAIGHT-ONE LEFT TURN":                "Left turn — angle",
}


def _get_crash_stats() -> dict:
    """Fetch 90-day crash summary, YTD fatalities, hot streets, collision types, peak hours."""
    session = _get_traffic_session()
    url = "https://data.austintexas.gov/resource/y2wy-tgr5.json"

    cutoff_90 = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%dT00:00:00")
    ytd_start = datetime.now().strftime("%Y-01-01T00:00:00")

    # 90-day totals + road-user fatality breakdown
    totals_resp = session.get(url, params={
        "$select": "sum(death_cnt) as deaths, sum(tot_injry_cnt) as injuries,"
                   "sum(sus_serious_injry_cnt) as serious,"
                   "sum(pedestrian_death_count) as ped_deaths,"
                   "sum(bicycle_death_count) as bike_deaths,"
                   "sum(motor_vehicle_death_count) as mv_deaths,"
                   "sum(motorcycle_death_count) as moto_deaths,"
                   "sum(micromobility_death_count) as micro_deaths,"
                   "count(*) as total",
        "$where":  f"crash_timestamp_ct > '{cutoff_90}'",
        "$limit":  1,
    }, timeout=20)
    totals_resp.raise_for_status()
    totals = totals_resp.json()[0] if totals_resp.json() else {}

    # YTD fatalities
    ytd_resp = session.get(url, params={
        "$select": "sum(death_cnt) as deaths, count(*) as total",
        "$where":  f"crash_timestamp_ct > '{ytd_start}'",
        "$limit":  1,
    }, timeout=20)
    ytd_resp.raise_for_status()
    ytd = ytd_resp.json()[0] if ytd_resp.json() else {}

    # Top crash streets
    streets_resp = session.get(url, params={
        "$select": "rpt_street_name, count(*) as cnt",
        "$where":  f"crash_timestamp_ct > '{cutoff_90}' AND rpt_street_name != 'NOT REPORTED'",
        "$group":  "rpt_street_name",
        "$order":  "cnt DESC",
        "$limit":  6,
    }, timeout=20)
    streets_resp.raise_for_status()
    top_streets = [
        (r["rpt_street_name"].title(), int(r["cnt"]))
        for r in streets_resp.json()
    ]

    # Top collision types
    collsn_resp = session.get(url, params={
        "$select": "collsn_desc, count(*) as cnt",
        "$where":  f"crash_timestamp_ct > '{cutoff_90}'",
        "$group":  "collsn_desc",
        "$order":  "cnt DESC",
        "$limit":  6,
    }, timeout=20)
    collsn_resp.raise_for_status()
    collision_types = [
        (_COLLSN_LABELS.get(r["collsn_desc"], r["collsn_desc"].title()), int(r["cnt"]))
        for r in collsn_resp.json()
        if r.get("collsn_desc")
    ][:5]

    # Peak crash hours
    hours_resp = session.get(url, params={
        "$select": "date_extract_hh(crash_timestamp_ct) as hour, count(*) as cnt",
        "$where":  f"crash_timestamp_ct > '{cutoff_90}'",
        "$group":  "hour",
        "$order":  "cnt DESC",
        "$limit":  3,
    }, timeout=20)
    hours_resp.raise_for_status()
    peak_hours = [
        (int(r["hour"]), int(r["cnt"]))
        for r in hours_resp.json()
        if r.get("hour") is not None
    ]

    return {
        "totals":          totals,
        "ytd":             ytd,
        "cutoff":          cutoff_90[:10],
        "ytd_start":       ytd_start[:4],
        "top_streets":     top_streets,
        "collision_types": collision_types,
        "peak_hours":      peak_hours,
    }


def _fmt_int(val) -> str:
    try:
        return f"{int(float(val)):,}"
    except (TypeError, ValueError):
        return "0"


def _fmt_hour(h: int) -> str:
    if h == 0:   return "12am"
    if h < 12:   return f"{h}am"
    if h == 12:  return "12pm"
    return f"{h - 12}pm"


def _format_crash_stats(data: dict) -> str:
    t      = data.get("totals", {})
    ytd    = data.get("ytd", {})
    cutoff = data.get("cutoff", "")
    year   = data.get("ytd_start", "")

    msg = f"💥 *Austin Crash Report — Last 90 Days*\n_{cutoff} to today_\n\n"

    msg += (
        f"*Overview:*\n"
        f"• Total crashes: {_fmt_int(t.get('total'))}\n"
        f"• Fatalities: {_fmt_int(t.get('deaths'))}\n"
        f"• Serious injuries: {_fmt_int(t.get('serious'))}\n"
        f"• All injuries: {_fmt_int(t.get('injuries'))}\n\n"
    )

    # Fatalities by road user — only show non-zero rows
    modes = [
        ("Motor vehicle", t.get("mv_deaths")),
        ("Pedestrian",    t.get("ped_deaths")),
        ("Motorcycle",    t.get("moto_deaths")),
        ("Bicycle",       t.get("bike_deaths")),
        ("Micromobility", t.get("micro_deaths")),
    ]
    mode_lines = [f"• {label}: {_fmt_int(v)}" for label, v in modes
                  if v and float(v) > 0]
    if mode_lines:
        msg += "*Fatalities by road user:*\n" + "\n".join(mode_lines) + "\n\n"

    msg += (
        f"*{year} YTD:*\n"
        f"• Crashes: {_fmt_int(ytd.get('total'))}\n"
        f"• Fatalities: {_fmt_int(ytd.get('deaths'))}\n\n"
    )

    top_streets = data.get("top_streets", [])
    if top_streets:
        msg += "*Most Crash-Prone Streets:*\n"
        for street, cnt in top_streets:
            msg += f"• {street}: {cnt:,}\n"
        msg += "\n"

    collision_types = data.get("collision_types", [])
    if collision_types:
        msg += "*Top Collision Types:*\n"
        for label, cnt in collision_types:
            msg += f"• {label}: {cnt:,}\n"
        msg += "\n"

    peak_hours = data.get("peak_hours", [])
    if peak_hours:
        hours_str = " · ".join(f"{_fmt_hour(h)} ({cnt:,})" for h, cnt in peak_hours)
        msg += f"*Peak crash hours:* {hours_str}\n\n"

    msg += "_Source: [Austin Crash Report Data](https://data.austintexas.gov/d/y2wy-tgr5)_"
    return msg


async def traffic_crashes_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Fetching crash stats...")
    try:
        data = await asyncio.to_thread(_get_crash_stats)
        msg = _format_crash_stats(data)
        keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="service_traffic")]]
        await query.edit_message_text(msg, parse_mode="Markdown",
                                      reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        logger.error(f"traffic crashes cb: {e}")
        await query.edit_message_text(f"❌ Error fetching crash stats: {e}")


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


@rate_limited
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

        msg += "\n_Source: [Austin Parking Meter Transactions](https://data.austintexas.gov/d/5bb2-gtef)_"
        await query.edit_message_text(msg, parse_mode="Markdown", disable_web_page_preview=True)
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
        msg += "\n\n_Source: [Austin Open311 API](https://311.austintexas.gov/open311/v2)_"
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


@rate_limited
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
# PARKS MAINTENANCE HANDLERS
# =============================================================================


def _parks_days_keyboard(view: str) -> InlineKeyboardMarkup:
    """Return a 30/60/90-day picker keyboard for the given parks view."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("30 days", callback_data=f"parks_{view}_30"),
        InlineKeyboardButton("60 days", callback_data=f"parks_{view}_60"),
        InlineKeyboardButton("90 days", callback_data=f"parks_{view}_90"),
    ]])


async def parks_overview_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show unified overview with 90-day default."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Loading park overview...")
    try:
        # Get both hotspots and stats data
        hotspots_data = await asyncio.to_thread(get_park_hotspots, 90)
        stats_data = await asyncio.to_thread(get_park_stats, 90)
        
        # Build keyboard with park names
        keyboard = build_park_name_keyboard(hotspots_data, 90)
        
        # Format unified overview
        overview_text = format_unified_overview(hotspots_data, stats_data)
        
        await _send_chunked(query, overview_text, reply_markup=keyboard)
    except Exception as e:
        logger.error(f"parks overview: {e}")
        await query.edit_message_text(f"❌ Error: {e}")


async def parks_detail_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show details for a specific park."""
    query = update.callback_query
    await query.answer()
    
    # Parse callback data: parks_detail_<park_name>_<days>
    parts = query.data.split("_")
    park_name = "_".join(parts[2:-1]).replace("_", " ")
    days = int(parts[-1])
    
    await query.edit_message_text(f"Loading details for {park_name}...")
    try:
        detail = await asyncio.to_thread(get_park_detail, park_name, days)
        await _send_chunked(query, format_park_detail(detail))
    except Exception as e:
        logger.error(f"parks detail: {e}")
        await query.edit_message_text(f"❌ Error: {e}")


async def parks_time_window_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show time window selection."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Park Time Window - Choose period:",
        reply_markup=_parks_days_keyboard("overview"),
    )


async def parks_overview_days_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show overview for specific days."""
    query = update.callback_query
    await query.answer()
    days = int(query.data.split("_")[-1])
    await query.edit_message_text(f"Loading park overview (last {days} days)...")
    try:
        hotspots_data = await asyncio.to_thread(get_park_hotspots, days)
        stats_data = await asyncio.to_thread(get_park_stats, days)
        keyboard = build_park_name_keyboard(hotspots_data, days)
        overview_text = format_unified_overview(hotspots_data, stats_data)
        await _send_chunked(query, overview_text, reply_markup=keyboard)
    except Exception as e:
        logger.error(f"parks overview days: {e}")
        await query.edit_message_text(f"❌ Error: {e}")




async def parks_resolution_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show resolution times with 90-day default."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Loading resolution times...")
    try:
        data = await asyncio.to_thread(get_park_resolution, 90)
        await _send_chunked(query, format_park_resolution(data))
    except Exception as e:
        logger.error(f"parks resolution: {e}")
        await query.edit_message_text(f"❌ Error: {e}")








@rate_limited
async def parks_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [InlineKeyboardButton("Overview", callback_data="parks_overview"),
         InlineKeyboardButton("Resolution Times", callback_data="parks_resolution")],
        [InlineKeyboardButton("Change Time Window", callback_data="parks_time_window")],
    ]
    await update.message.reply_text(
        "*Parks Maintenance*\nTrack unresolved complaints by park. Useful for choosing where to go.\n\nChoose a view:",
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
    msg = msg.strip()
    msg += "\n\n_Source: [Austin Open311 API](https://311.austintexas.gov/open311/v2)_"
    return msg


@rate_limited
async def code_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("⏳ Querying building permits...")
    try:
        stats = await asyncio.to_thread(_get_building_permit_stats)
        await update.message.reply_text(_format_permit_stats(stats), parse_mode="Markdown")
    except Exception as e:
        logger.error(f"code command: {e}")
        await update.message.reply_text(f"❌ Error querying data: {e}")


# =============================================================================
# REPORT COMMAND — Submit a 311 request via Open311 POST
# =============================================================================

# Conversation states
_REPORT_TYPE, _REPORT_LOCATION, _REPORT_DESCRIPTION, _REPORT_CONFIRM = range(4)

# Service types available for submission
_REPORT_SERVICES = [
    ("🕳️ Pothole",        "SBPOTREP"),
    ("🎨 Graffiti",        "HHSGRAFF"),
    ("🔊 Noise",           "APDNONNO"),
    ("🚗 Parking",         "PARKINGV"),
    ("💡 Street Light",    "STREETL2"),
    ("🪨 Debris/Dumping",  "SBDEBROW"),
    ("🚶 Sidewalk",        "SBSIDERE"),
    ("🚦 Traffic Signal",  "TRASIGMA"),
]

_REPORT_LABEL: dict[str, str] = {code: label for label, code in _REPORT_SERVICES}


@rate_limited
async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    keyboard = []
    row = []
    for label, code in _REPORT_SERVICES:
        row.append(InlineKeyboardButton(label, callback_data=f"rpt_type_{code}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="rpt_cancel")])

    await update.message.reply_text(
        "📋 *Report a 311 Issue*\n\nWhat would you like to report?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return _REPORT_TYPE


async def report_type_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    code = query.data.replace("rpt_type_", "")
    context.user_data["rpt_code"] = code
    context.user_data["rpt_label"] = _REPORT_LABEL.get(code, code)

    await query.edit_message_text(
        f"📍 *{_REPORT_LABEL.get(code, code)}*\n\n"
        "Where is the issue?\n"
        "• Type an address _or_\n"
        "• Share your location via the 📎 attachment menu\n\n"
        "_Send /cancel to abort at any time._",
        parse_mode="Markdown",
    )
    return _REPORT_LOCATION


async def report_location_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.location:
        loc = update.message.location
        context.user_data["rpt_lat"] = loc.latitude
        context.user_data["rpt_long"] = loc.longitude
        context.user_data["rpt_address"] = None
    else:
        context.user_data["rpt_address"] = update.message.text.strip()
        context.user_data["rpt_lat"] = None
        context.user_data["rpt_long"] = None

    await update.message.reply_text(
        "📝 *Describe the issue:*\n"
        "_Be specific — size, severity, how long it's been there._\n\n"
        "_Send /cancel to abort._",
        parse_mode="Markdown",
    )
    return _REPORT_DESCRIPTION


async def report_description_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["rpt_desc"] = update.message.text.strip()

    label   = context.user_data.get("rpt_label", "")
    address = context.user_data.get("rpt_address")
    lat     = context.user_data.get("rpt_lat")
    lon     = context.user_data.get("rpt_long")
    desc    = context.user_data["rpt_desc"]
    loc_str = address if address else f"{lat:.5f}, {lon:.5f}"

    keyboard = [[
        InlineKeyboardButton("✅ Submit to Austin 311", callback_data="rpt_confirm"),
        InlineKeyboardButton("❌ Cancel",               callback_data="rpt_cancel"),
    ]]

    await update.message.reply_text(
        f"*Review your report:*\n\n"
        f"*Type:* {label}\n"
        f"*Location:* {loc_str}\n"
        f"*Description:* {desc}\n\n"
        f"_Ready to submit?_",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return _REPORT_CONFIRM


async def report_confirm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Submitting your report to Austin 311…")

    try:
        result = await asyncio.to_thread(_submit_311_report, dict(context.user_data))
        ticket_id = result.get("service_request_id", "")
        notice    = result.get("service_notice", "")

        msg = "✅ *Report Submitted!*\n\n"
        if ticket_id:
            msg += f"*Ticket ID:* `{ticket_id}`\n"
            msg += f"Track it with /ticket {ticket_id}\n"
        if notice:
            msg += f"\n_{notice}_\n"
        msg += "\n_Austin 311 typically responds within 1–5 business days._"

        await query.edit_message_text(msg, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"311 submission failed: {e}")
        await query.edit_message_text(
            f"❌ *Submission failed*\n\n`{e}`\n\n"
            "You can also report at austintexas.gov/page/311 or call 3-1-1.",
            parse_mode="Markdown",
        )

    context.user_data.clear()
    return ConversationHandler.END


async def report_cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await query.edit_message_text("❌ Report cancelled.")
    return ConversationHandler.END


async def report_cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("❌ Report cancelled.")
    return ConversationHandler.END


def _submit_311_report(user_data: dict) -> dict:
    """POST a new service request to Austin's Open311 API."""
    url = "https://311.austintexas.gov/open311/v2/requests.json"

    payload: dict = {
        "service_code": user_data["rpt_code"],
        "description":  user_data.get("rpt_desc", ""),
    }

    if user_data.get("rpt_lat") is not None:
        payload["lat"]  = str(user_data["rpt_lat"])
        payload["long"] = str(user_data["rpt_long"])
    else:
        payload["address_string"] = user_data.get("rpt_address", "")

    app_token = os.getenv("AUSTIN_APP_TOKEN")
    if app_token:
        payload["api_key"] = app_token

    resp = requests.post(url, data=payload, timeout=30)
    resp.raise_for_status()
    result = resp.json()
    if isinstance(result, list) and result:
        return result[0]
    return result if isinstance(result, dict) else {}



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
    msg += f"\n_Source: [APD Crime Reports](https://data.austintexas.gov/d/fdj4-gpfu)_\n"
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

        msg += "\n_Source: [APD NIBRS Data](https://data.austintexas.gov/d/i7fg-wrk5)_"
        await _send_chunked(query, msg)
    except Exception as e:
        logger.error(f"crime homicides: {e}")
        await query.edit_message_text(f"❌ Error: {e}")


@rate_limited
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
            [InlineKeyboardButton(
                "Hate Crimes",
                callback_data="police_hate"
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


@rate_limited
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

        msg += "\n_Source: [APD Crime Reports](https://data.austintexas.gov/d/fdj4-gpfu)_"
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
# HOMELESSNESS BUDGET (Austin Open Budget dataset yeeq-kk6v)
# =============================================================================

_HOMELESS_DIRECT_DEPTS = [
    "Homeless Strategies and Operations",
    "Housing",
]

_HOMELESS_DOWNSTREAM_DEPTS = [
    ("Fire",                       "🔥"),
    ("Emergency Medical Services", "🚑"),
    ("Public Health",              "🏥"),
    ("Austin Resource Recovery",   "🗑️"),
    ("Parks and Recreation",       "🌳"),
    ("Police",                     "👮"),
]


def _fmt_millions(amount: float) -> str:
    if amount >= 1_000_000:
        return f"${amount / 1_000_000:.1f}M"
    if amount >= 1_000:
        return f"${amount / 1_000:.0f}K"
    return f"${amount:.0f}"


def _budget_trend(first: float, last: float) -> str:
    if not first:
        return ""
    pct = round((last - first) / first * 100)
    arrow = "📈" if pct > 5 else "📉" if pct < -5 else "➡️"
    return f"_{arrow} {'+' if pct > 0 else ''}{pct}%_"


def _dept_spend(dept_data: dict, fy: str) -> float:
    d = dept_data.get(fy, {})
    return d.get("actual") or d.get("budget") or 0.0


def _get_homeless_budget() -> dict:
    """Fetch annual spend for homelessness-related departments from Austin Open Budget."""
    url = "https://data.austintexas.gov/resource/yeeq-kk6v.json"
    all_depts = _HOMELESS_DIRECT_DEPTS + [n for n, _ in _HOMELESS_DOWNSTREAM_DEPTS]
    dept_list = ",".join(f"'{d}'" for d in all_depts)
    app_token = os.getenv("AUSTIN_APP_TOKEN")
    headers = {"X-App-Token": app_token} if app_token else {}
    try:
        resp = requests.get(url, params={
            "$select": "fy,dept_nm,sum(act) as actual,sum(bud) as budget",
            "$where":  f"dept_nm in({dept_list})",
            "$group":  "fy,dept_nm",
            "$order":  "fy ASC,dept_nm ASC",
            "$limit":  500,
        }, headers=headers, timeout=20)
        resp.raise_for_status()
        result: dict[str, dict[str, dict]] = {}
        for row in resp.json():
            dept = row.get("dept_nm", "")
            fy = row.get("fy", "")
            try:
                actual = float(row.get("actual") or 0)
                budget = float(row.get("budget") or 0)
            except (ValueError, TypeError):
                continue
            result.setdefault(dept, {})[fy] = {"actual": actual, "budget": budget}

        # Fetch grants-to-subrecipients (NGO/nonprofit pass-through) by year
        grants_resp = requests.get(url, params={
            "$select": "fy,sum(act) as actual,sum(bud) as budget",
            "$where":  "dept_nm='Homeless Strategies and Operations' AND obj_desc='Grants to subrecipients'",
            "$group":  "fy",
            "$order":  "fy ASC",
            "$limit":  100,
        }, headers=headers, timeout=20)
        grants_resp.raise_for_status()
        grants: dict[str, dict] = {}
        for row in grants_resp.json():
            fy = row.get("fy", "")
            try:
                actual = float(row.get("actual") or 0)
                budget = float(row.get("budget") or 0)
            except (ValueError, TypeError):
                continue
            grants[fy] = {"actual": actual, "budget": budget}
        result["_grants_to_subrecipients"] = grants

        # Fetch citywide pension contributions and health/dental benefits by year
        pension_obj = (
            "Contribution to employees ret",
            "Contribution to police ret",
            "Contribution to firefighter rt",
        )
        pension_list = ",".join(f"'{o}'" for o in pension_obj)
        pension_resp = requests.get(url, params={
            "$select": "fy,obj_desc,sum(act) as actual,sum(bud) as budget",
            "$where":  f"obj_desc in({pension_list},'Insurance-health/life/dental')",
            "$group":  "fy,obj_desc",
            "$order":  "fy ASC",
            "$limit":  200,
        }, headers=headers, timeout=20)
        pension_resp.raise_for_status()
        pension: dict[str, dict] = {}   # fy -> {pension: float, health: float}
        for row in pension_resp.json():
            fy = row.get("fy", "")
            desc = row.get("obj_desc", "")
            try:
                actual = float(row.get("actual") or 0)
            except (ValueError, TypeError):
                continue
            entry = pension.setdefault(fy, {"pension": 0.0, "health": 0.0})
            if desc in pension_obj:
                entry["pension"] += actual
            else:
                entry["health"] += actual
        result["_pension_benefits"] = pension

        return result
    except Exception as e:
        logger.error(f"homeless budget: {e}")
        return {}


def _austin_current_fy() -> str:
    """Return the current Austin fiscal year as a string (FY ends Sep 30)."""
    now = datetime.now()
    return str(now.year if now.month < 10 else now.year + 1)


def _format_homeless_budget(data: dict) -> str:
    if not data:
        return "🏠 *Austin Citywide Budget Impact*\n\nNo data available."

    all_years = sorted({fy for k, dept_data in data.items() if not k.startswith("_") for fy in dept_data})
    if not all_years:
        return "🏠 *Austin Citywide Budget Impact*\n\nNo data available."

    current_fy = _austin_current_fy()

    def fy_label(fy: str) -> str:
        return f"FY{fy}(partial)" if fy == current_fy else f"FY{fy}"

    recent = all_years[-5:]
    first_yr, last_yr = recent[0], recent[-1]
    # Use last completed year for trend; partial year would produce a misleading result
    completed = [fy for fy in recent if fy != current_fy]
    trend_yr = completed[-1] if completed else last_yr

    msg = f"🏠 *Austin Citywide Budget Impact*\n"
    msg += f"_FY{first_yr}–FY{last_yr} · actual spend where available_\n\n"

    msg += "*Direct Homeless Services:*\n"
    for dept in _HOMELESS_DIRECT_DEPTS:
        dept_data = data.get(dept)
        if not dept_data:
            continue
        label = "Homeless Strategies & Ops" if "Homeless" in dept else dept
        year_strs = "  ".join(
            f"{fy_label(fy)}: {_fmt_millions(_dept_spend(dept_data, fy))}" for fy in recent
        )
        trend = _budget_trend(_dept_spend(dept_data, first_yr), _dept_spend(dept_data, trend_yr))
        msg += f"*{label}*\n{year_strs}" + (f"\n{trend}" if trend else "") + "\n\n"

    grants = data.get("_grants_to_subrecipients", {})
    if grants:
        year_strs = "  ".join(
            f"{fy_label(fy)}: {_fmt_millions(_dept_spend(grants, fy))}" for fy in recent
        )
        trend = _budget_trend(_dept_spend(grants, first_yr), _dept_spend(grants, trend_yr))
        msg += f"*Grants to NGOs/Nonprofits*\n{year_strs}" + (f"\n{trend}" if trend else "") + "\n\n"

    msg += "*Downstream Departments:*\n"
    for dept_name, emoji in _HOMELESS_DOWNSTREAM_DEPTS:
        dept_data = data.get(dept_name)
        if not dept_data:
            continue
        first_spend = _dept_spend(dept_data, first_yr)
        trend_spend = _dept_spend(dept_data, trend_yr)
        last_spend = _dept_spend(dept_data, last_yr)
        trend = _budget_trend(first_spend, trend_spend)
        last_label = fy_label(last_yr)
        msg += (
            f"{emoji} *{dept_name}*\n"
            f"  FY{first_yr}: {_fmt_millions(first_spend)} → "
            f"{last_label}: {_fmt_millions(last_spend)}  {trend}\n\n"
        )

    pension = data.get("_pension_benefits", {})
    if pension:
        pension_years = sorted(pension.keys())
        p_recent = pension_years[-5:]
        p_first = p_recent[0]
        p_completed = [fy for fy in p_recent if fy != current_fy]
        p_trend_yr = p_completed[-1] if p_completed else p_recent[-1]
        msg += "*Citywide Pension & Benefits:*\n"
        p_strs = "  ".join(
            f"{fy_label(fy)}: {_fmt_millions(pension.get(fy, {}).get('pension', 0.0))}" for fy in p_recent
        )
        p_trend = _budget_trend(
            pension.get(p_first, {}).get("pension", 0.0),
            pension.get(p_trend_yr, {}).get("pension", 0.0),
        )
        msg += f"*Pension contributions*\n{p_strs}" + (f"\n{p_trend}" if p_trend else "") + "\n\n"
        h_strs = "  ".join(
            f"{fy_label(fy)}: {_fmt_millions(pension.get(fy, {}).get('health', 0.0))}" for fy in p_recent
        )
        h_trend = _budget_trend(
            pension.get(p_first, {}).get("health", 0.0),
            pension.get(p_trend_yr, {}).get("health", 0.0),
        )
        msg += f"*Health/dental insurance*\n{h_strs}" + (f"\n{h_trend}" if h_trend else "") + "\n\n"

    msg += "_Source: [Austin Open Budget](https://data.austintexas.gov/d/yeeq-kk6v)_"
    return msg


@rate_limited
async def homeless_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("⏳ Fetching budget insights...")
    try:
        data = await asyncio.to_thread(_get_homeless_budget)
        msg = _format_homeless_budget(data)
        await _send_chunked(update.message, msg)
    except Exception as e:
        logger.error(f"homeless command: {e}")
        await update.message.reply_text(f"❌ Error fetching budget data: {e}")


async def police_homeless_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Fetching budget insights...")
    try:
        data = await asyncio.to_thread(_get_homeless_budget)
        msg = _format_homeless_budget(data)
        keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="service_police")]]
        await query.edit_message_text(
            msg, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.error(f"police homeless cb: {e}")
        await query.edit_message_text(f"❌ Error fetching budget data: {e}")


# =============================================================================
# HOMELESS ENCAMPMENT 311 REPORTS (keyword-filtered across Open311 codes)
# =============================================================================

def _homeless_days_keyboard(view: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("30 days", callback_data=f"homeless311_{view}_30"),
        InlineKeyboardButton("60 days", callback_data=f"homeless311_{view}_60"),
        InlineKeyboardButton("90 days", callback_data=f"homeless311_{view}_90"),
    ]])


@rate_limited
async def homeless_311_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Entry point for /homeless — 311 encampment reports."""
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Stats (30 days)", callback_data="homeless311_stats_30"),
         InlineKeyboardButton("📊 Stats (90 days)", callback_data="homeless311_stats_90")],
        [InlineKeyboardButton("🗺️ View Map (30 days)", callback_data="homeless311_map_30"),
         InlineKeyboardButton("📍 Open Locations", callback_data="homeless311_locations_30")],
        [InlineKeyboardButton("Change Time Window", callback_data="homeless311_time_window")],
    ])
    await update.message.reply_text(
        "🏕️ *Encampment & Homeless-Related 311 Reports*\n\n"
        "Counts 311 complaints mentioning encampments, tents, homeless camps, "
        "or related keywords across Parks, Right-of-Way, Debris, and Drainage "
        "service codes.\n\n"
        "_Note: reflects voluntary public reporting only — not a full census "
        "of encampments._",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


async def homeless311_stats_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")   # homeless311_stats_<days>
    try:
        days = int(parts[-1])
    except (ValueError, IndexError):
        days = 30
    await query.edit_message_text(f"⏳ Fetching encampment reports for last {days} days…")
    try:
        data = await asyncio.to_thread(get_encampment_stats, days)
        msg = format_encampment_stats(data)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📍 Open Locations", callback_data=f"homeless311_locations_{days}")],
            [InlineKeyboardButton("Change Time Window", callback_data="homeless311_time_window")],
        ])
        await query.edit_message_text(msg, parse_mode="Markdown",
                                      reply_markup=keyboard,
                                      disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"homeless311 stats cb: {e}")
        await query.edit_message_text(f"❌ Error fetching encampment data: {e}")


async def homeless311_locations_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")   # homeless311_locations_<days>
    try:
        days = int(parts[-1])
    except (ValueError, IndexError):
        days = 30
    await query.edit_message_text(f"⏳ Fetching open encampment locations…")
    try:
        data = await asyncio.to_thread(get_encampment_stats, days)
        msg = format_encampment_locations(data)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Back to Stats", callback_data=f"homeless311_stats_{days}")],
        ])
        await query.edit_message_text(msg, parse_mode="Markdown",
                                      reply_markup=keyboard,
                                      disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"homeless311 locations cb: {e}")
        await query.edit_message_text(f"❌ Error fetching location data: {e}")


async def homeless311_time_window_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "🏕️ *Encampment Reports — Choose Time Window*",
        parse_mode="Markdown",
        reply_markup=_homeless_days_keyboard("stats"),
    )


async def homeless311_map_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")   # homeless311_map_<days>
    try:
        days = int(parts[-1])
    except (ValueError, IndexError):
        days = 30
    
    await query.edit_message_text(f"⏳ Generating encampment report map for last {days} days…")
    
    try:
        buffer, summary = await asyncio.to_thread(generate_encampment_map, days)
        
        if buffer is None:
            # Error or no data
            await query.edit_message_text(summary)
            return
        
        # Send the HTML map file
        buffer.name = "encampment_map.html"
        await query.message.reply_document(
            document=buffer,
            filename="encampment_map.html",
            caption=summary,
            parse_mode="Markdown",
        )
        
        # Update the original message with a back button
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Back to Stats", callback_data=f"homeless311_stats_{days}")],
        ])
        await query.edit_message_text(
            f"✅ Map generated! Check the HTML file above.",
            reply_markup=keyboard,
        )
    except Exception as e:
        logger.error(f"homeless311 map cb: {e}", exc_info=True)
        await query.edit_message_text(f"❌ Error generating map: {e}")


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


def _get_hate_crimes() -> dict:
    """Fetch all hate crime incidents from APD dataset (full history)."""
    url = "https://data.austintexas.gov/resource/t99n-5ib4.json"
    params = {
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
            return {"total": 0, "by_year": {}, "bias": {}, "race": {}, "offenses": {}}

        bias_counts: dict[str, int] = {}
        race_counts: dict[str, int] = {}
        offense_counts: dict[str, int] = {}
        by_year: dict[int, int] = {}

        for row in data:
            b = row.get("bias", "Unknown") or "Unknown"
            bias_counts[b] = bias_counts.get(b, 0) + 1

            r = row.get("race_ethnicity_of_offenders", "Unknown") or "Unknown"
            race_counts[r] = race_counts.get(r, 0) + 1

            o = row.get("offense_s", "Unknown") or "Unknown"
            offense_counts[o] = offense_counts.get(o, 0) + 1

            dt = row.get("date_of_incident", "")
            if dt:
                try:
                    year = int(dt[:4])
                    by_year[year] = by_year.get(year, 0) + 1
                except (ValueError, IndexError):
                    pass

        return {
            "total":    len(data),
            "by_year":  dict(sorted(by_year.items())),
            "bias":     dict(sorted(bias_counts.items(), key=lambda x: -x[1])),
            "race":     dict(sorted(race_counts.items(), key=lambda x: -x[1])),
            "offenses": dict(sorted(offense_counts.items(), key=lambda x: -x[1])),
        }
    except Exception as e:
        logger.error(f"hate crimes fetch: {e}")
        return {"total": 0, "by_year": {}, "bias": {}, "race": {}, "offenses": {}}


def _format_hate_crimes(data: dict) -> str:
    total = data["total"]
    if total == 0:
        return "🎯 *Austin Hate Crimes*\n\nNo data found."

    by_year = data["by_year"]
    years = sorted(by_year.keys())
    year_range = f"{years[0]}–{years[-1]}" if years else "All years"
    msg = f"🎯 *Austin Hate Crimes — {year_range}*\n_{total} reported incidents_\n\n"

    if by_year:
        msg += "*Incidents by Year:*\n"
        max_count = max(by_year.values())
        for year in years:
            count = by_year[year]
            bar = "█" * min(10, round(count / max_count * 10))
            msg += f"*{year}:* {bar} {count}\n"

    msg += "\n*Bias Motivation:*\n"
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


@rate_limited
async def hate_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("⏳ Fetching hate crime data...")
    try:
        data = await asyncio.to_thread(_get_hate_crimes)
        msg = _format_hate_crimes(data)
        await _send_chunked(update.message, msg)
    except Exception as e:
        logger.error(f"hate command: {e}")
        await update.message.reply_text(f"❌ Error fetching hate crime data: {e}")


async def police_hate_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Fetching hate crime data...")
    try:
        data = await asyncio.to_thread(_get_hate_crimes)
        msg = _format_hate_crimes(data)
        keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="service_police")]]
        await query.edit_message_text(msg, parse_mode="Markdown",
                                      reply_markup=InlineKeyboardMarkup(keyboard),
                                      disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"police hate cb: {e}")
        await query.edit_message_text(f"❌ Error fetching hate crime data: {e}")


# =============================================================================
# WATER QUALITY COMMAND (Surface Water Quality Sampling 5tye-7ray)
# =============================================================================

_WATER_SESSION = None


def _get_water_session():
    global _WATER_SESSION
    if _WATER_SESSION is None:
        _WATER_SESSION = requests.Session()
        app_token = os.getenv("AUSTIN_APP_TOKEN")
        if app_token:
            _WATER_SESSION.headers.update({"X-App-Token": app_token})
    return _WATER_SESSION


def _get_water_quality() -> dict:
    """Fetch latest surface water quality readings per watershed."""
    session = _get_water_session()
    url = "https://data.austintexas.gov/resource/5tye-7ray.json"

    # Parameters of interest
    params_of_interest = {
        "E COLI BACTERIA":            ("E. coli", "col/100mL"),
        "24-HOUR AVG DISSOLVED OXYGEN": ("Dissolved Oxygen", "mg/L"),
        "NITRATE AS N":               ("Nitrate (as N)", "mg/L"),
        "FREE REACTIVE PHOSPHORUS":   ("Phosphorus", "µg/L"),
        "24-HOUR AVG PH":             ("pH", ""),
    }

    results: dict[str, dict] = {}  # watershed -> param -> {value, unit, date}

    for param_raw, (label, unit) in params_of_interest.items():
        try:
            resp = session.get(url, params={
                "$select":  "watershed, parameter, result, unit, sample_date",
                "$where":   f"medium='Surface Water' AND upper(parameter) like '%{param_raw}%'",
                "$order":   "sample_date DESC",
                "$limit":   200,
            }, timeout=20)
            resp.raise_for_status()
            rows = resp.json()

            # Keep only the latest reading per watershed
            seen: set[str] = set()
            for row in rows:
                ws = row.get("watershed", "Unknown").strip()
                if ws in seen:
                    continue
                seen.add(ws)
                results.setdefault(ws, {})
                results[ws][label] = {
                    "value": row.get("result", ""),
                    "unit":  row.get("unit", unit) or unit,
                    "date":  row.get("sample_date", "")[:10],
                }
        except Exception as e:
            logger.error(f"water quality fetch failed for {param_raw}: {e}")

    return results


# EPA 2012 Recreational Water Quality Criteria — freshwater E. coli (primary contact)
# Geometric mean:   ≤126 CFU/100mL  → safe
# Single-sample:    ≤410 CFU/100mL  → elevated but within limit
# Above 410:        exceeds single-sample criterion → not recommended
# Note: these thresholds apply to freshwater only (lakes, rivers, creeks).
# Coastal/marine water uses enterococci, not E. coli — not applicable here.


def _ecoli_verdict(value: float) -> str:
    """EPA 2012 freshwater E. coli thresholds (primary contact recreation)."""
    if value <= 126:
        return "✅ Good"
    elif value <= 410:
        return "⚠️ Elevated"
    else:
        return "🚫 High — not recommended"


def _format_water_quality(data: dict) -> str:
    if not data:
        return "💧 *Austin Surface Water Quality*\n\n❌ No data available."

    msg = "💧 *Austin Surface Water Quality*\n_Freshwater E. coli by watershed (swimming safety)_\n\n"

    secondary_params = ["Dissolved Oxygen", "Nitrate (as N)", "Phosphorus", "pH"]

    for ws in sorted(data.keys()):
        params = data[ws]
        if not params:
            continue

        ecoli = params.get("E. coli")
        if ecoli:
            try:
                val = float(ecoli["value"])
                verdict = _ecoli_verdict(val)
                unit = ecoli.get("unit", "MPN/100mL")
                val_fmt = f"{val:,.0f}"
                date = ecoli["date"]
                msg += f"*{ws}* — {verdict}\n"
                msg += f"  E. coli: *{val_fmt} {unit}*"
                if date:
                    msg += f" _{date}_"
                msg += "\n"
            except (ValueError, TypeError):
                msg += f"*{ws}*\n  E. coli: N/A\n"
        else:
            msg += f"*{ws}*\n"

        # Secondary params as supporting detail
        for label in secondary_params:
            if label not in params:
                continue
            p = params[label]
            val_raw = p["value"]
            unit = p["unit"]
            try:
                val_fmt = f"{float(val_raw):,.2f}".rstrip("0").rstrip(".")
            except (ValueError, TypeError):
                val_fmt = val_raw or "N/A"
            unit_str = f" {unit}" if unit else ""
            msg += f"  {label}: {val_fmt}{unit_str}\n"
        msg += "\n"

    msg += (
        "_✅ ≤126 · ⚠️ 127–410 · 🚫 >410 MPN/100mL (EPA 2012 freshwater primary contact)_\n"
        "_Source: [Surface Water Quality Sampling](https://data.austintexas.gov/d/5tye-7ray)_"
    )
    return msg


async def water_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("⏳ Fetching water quality data...")
    try:
        data = await asyncio.to_thread(_get_water_quality)
        msg = _format_water_quality(data)
        await _send_chunked(update.message, msg)
    except Exception as e:
        logger.error(f"water command: {e}")
        await update.message.reply_text(f"❌ Error fetching water quality data: {e}")


@rate_limited
async def waterviolations_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("⏳ Fetching water conservation data...")
    try:
        stats = await asyncio.to_thread(get_water_conservation_stats)
        msg = format_water_conservation(stats)
        await _send_chunked(update.message, msg)
    except Exception as e:
        logger.error(f"waterviolations command: {e}")
        await update.message.reply_text(f"❌ Error fetching water conservation data: {e}")


# =============================================================================
# PERMITS COMMAND (Building Permits 3syk-w9eu)
# =============================================================================

_PERMITS_SESSION = None


def _get_permits_session():
    global _PERMITS_SESSION
    if _PERMITS_SESSION is None:
        _PERMITS_SESSION = requests.Session()
        app_token = os.getenv("AUSTIN_APP_TOKEN")
        if app_token:
            _PERMITS_SESSION.headers.update({"X-App-Token": app_token})
    return _PERMITS_SESSION


def _get_permit_stats() -> dict:
    """Fetch last-30-days building permit activity from Socrata 3syk-w9eu."""
    session = _get_permits_session()
    url = "https://data.austintexas.gov/resource/3syk-w9eu.json"
    base_where = "issued_in_last_30_days='Yes'"

    # Total count
    total_resp = session.get(url, params={
        "$select": "count(*) as total",
        "$where":  base_where,
        "$limit":  1,
    }, timeout=20)
    total_resp.raise_for_status()
    total = int(float((total_resp.json()[0] or {}).get("total", 0)))

    # By class (Residential / Commercial)
    class_resp = session.get(url, params={
        "$select": "permit_class_mapped, count(*) as cnt",
        "$where":  base_where,
        "$group":  "permit_class_mapped",
        "$order":  "cnt DESC",
        "$limit":  20,
    }, timeout=20)
    class_resp.raise_for_status()
    by_class = {r.get("permit_class_mapped", "Other"): int(float(r.get("cnt", 0)))
                for r in class_resp.json()}

    # By work class (New / Repair / Remodel / Addition / Demolition etc.)
    work_resp = session.get(url, params={
        "$select": "work_class, count(*) as cnt",
        "$where":  base_where,
        "$group":  "work_class",
        "$order":  "cnt DESC",
        "$limit":  20,
    }, timeout=20)
    work_resp.raise_for_status()
    by_work = {r.get("work_class", "Other"): int(float(r.get("cnt", 0)))
               for r in work_resp.json()}

    # By council district
    district_resp = session.get(url, params={
        "$select": "council_district, count(*) as cnt",
        "$where":  f"{base_where} AND council_district IS NOT NULL",
        "$group":  "council_district",
        "$order":  "cnt DESC",
        "$limit":  15,
    }, timeout=20)
    district_resp.raise_for_status()
    by_district = {r.get("council_district", "?"): int(float(r.get("cnt", 0)))
                   for r in district_resp.json()}

    return {
        "total":       total,
        "by_class":    by_class,
        "by_work":     by_work,
        "by_district": by_district,
    }


def _format_permit_activity(data: dict) -> str:
    total = data.get("total", 0)
    by_class = data.get("by_class", {})
    by_work = data.get("by_work", {})
    by_district = data.get("by_district", {})

    msg = f"🏗️ *Austin Building Permits — Last 30 Days*\n\n"
    msg += f"📊 *{total:,}* permits issued\n\n"

    if by_class:
        msg += "*By Type:*\n"
        for cls, cnt in sorted(by_class.items(), key=lambda x: -x[1]):
            pct = round(cnt / total * 100) if total else 0
            msg += f"  • {cls or 'Other'}: {cnt:,} ({pct}%)\n"
        msg += "\n"

    if by_work:
        msg += "*By Work Class:*\n"
        max_w = max(by_work.values()) if by_work else 1
        for wc, cnt in sorted(by_work.items(), key=lambda x: -x[1])[:8]:
            bar = "█" * min(8, round(cnt / max_w * 8))
            pct = round(cnt / total * 100) if total else 0
            msg += f"  {bar:<8} {wc or 'Other'}: {cnt:,} ({pct}%)\n"
        msg += "\n"

    if by_district:
        msg += "*By Council District:*\n"
        for dist, cnt in sorted(by_district.items(), key=lambda x: -x[1])[:10]:
            msg += f"  • District {dist}: {cnt:,}\n"
        msg += "\n"

    msg += "_Source: [Austin Building Permits](https://data.austintexas.gov/d/3syk-w9eu)_"
    return msg


async def permits_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("⏳ Fetching permit activity...")
    try:
        data = await asyncio.to_thread(_get_permit_stats)
        msg = _format_permit_activity(data)
        await _send_chunked(update.message, msg)
    except Exception as e:
        logger.error(f"permits command: {e}")
        await update.message.reply_text(f"❌ Error fetching permit data: {e}")


# =============================================================================
# BARS COMMAND — TABC Mixed Beverage Sales (data.texas.gov g5bj-yb6k)
# =============================================================================

_TABC_SESSION = None


def _get_tabc_session():
    global _TABC_SESSION
    if _TABC_SESSION is None:
        _TABC_SESSION = requests.Session()
    return _TABC_SESSION


def _get_bar_stats() -> dict:
    """Fetch top grossing and biggest movers for Austin bars/restaurants."""
    session = _get_tabc_session()
    url = "https://data.texas.gov/resource/g5bj-yb6k.json"

    # Get the 4 most recent months by date, skip the newest if still incomplete
    # (incomplete = fewer than 50% of the next month's row count)
    months_resp = session.get(url, params={
        "$select": "obligation_end_date, count(*) as cnt",
        "$where":  "upper(location_city)='AUSTIN'",
        "$group":  "obligation_end_date",
        "$order":  "obligation_end_date DESC",
        "$limit":  4,
    }, timeout=20)
    months_resp.raise_for_status()
    months = sorted(months_resp.json(), key=lambda r: r["obligation_end_date"], reverse=True)
    if len(months) < 2:
        raise ValueError("Not enough monthly data available")

    # Drop the most recent month if it has < 50% of the next month's count
    counts = [int(m.get("cnt", 0)) for m in months]
    if counts[0] < counts[1] * 0.5:
        months = months[1:]

    current_month = months[0]["obligation_end_date"][:10]
    prior_month   = months[1]["obligation_end_date"][:10]

    def fetch_month(month: str) -> dict[str, dict]:
        """Return {permit_number: {name, address, sales}} deduplicated.

        Some venues hold multiple TABC permits and report identical sales on
        each — dedup by (address, sales) to avoid counting the same revenue
        twice. Where the same address reports different amounts (genuinely
        separate outlets), keep each as its own entry keyed by permit number.
        """
        resp = session.get(url, params={
            "$select": "tabc_permit_number, location_name, location_address, total_sales_receipts",
            "$where":  f"upper(location_city)='AUSTIN' AND obligation_end_date='{month}T00:00:00.000'",
            "$order":  "total_sales_receipts DESC",
            "$limit":  5000,
        }, timeout=30)
        resp.raise_for_status()

        seen_addr_sales: set[tuple] = set()
        result: dict[str, dict] = {}
        for r in resp.json():
            permit  = r.get("tabc_permit_number", "")
            address = (r.get("location_address") or "").strip().upper()
            try:
                sales = float(r.get("total_sales_receipts", 0) or 0)
            except (ValueError, TypeError):
                continue
            key = (address, sales)
            if key in seen_addr_sales:
                continue
            seen_addr_sales.add(key)
            result[permit] = {
                "name":    (r.get("location_name") or "Unknown").title(),
                "address": (r.get("location_address") or "").title(),
                "sales":   sales,
            }
        return result

    current = fetch_month(current_month)
    prior   = fetch_month(prior_month)

    # Top 10 by sales this month
    top10 = sorted(current.values(), key=lambda r: -r["sales"])[:10]

    # Biggest movers: establishments in both months, ranked by $ increase
    movers = []
    for permit, cur in current.items():
        if permit in prior:
            delta = cur["sales"] - prior[permit]["sales"]
            if delta > 0 and prior[permit]["sales"] > 0:
                pct = delta / prior[permit]["sales"] * 100
                movers.append({
                    "name":    cur["name"],
                    "address": cur["address"],
                    "current": cur["sales"],
                    "prior":   prior[permit]["sales"],
                    "delta":   delta,
                    "pct":     pct,
                })
    movers.sort(key=lambda r: -r["delta"])
    top_movers = movers[:5]

    return {
        "current_month": current_month,
        "prior_month":   prior_month,
        "top10":         top10,
        "movers":        top_movers,
    }


def _fmt_dollars(val: float) -> str:
    if val >= 1_000_000:
        return f"${val / 1_000_000:.1f}M"
    if val >= 1_000:
        return f"${val / 1_000:.0f}K"
    return f"${val:,.0f}"


def _format_bar_stats(data: dict) -> str:
    month_label = data["current_month"][:7]  # YYYY-MM
    prior_label = data["prior_month"][:7]
    top10  = data["top10"]
    movers = data["movers"]

    if not top10:
        return "🍺 *Austin Bar & Restaurant Sales*\n\n❌ No data available."

    winner = top10[0]
    msg = (
        f"🍺 *Austin Bar & Restaurant Sales — {month_label}*\n"
        f"_Mixed beverage receipts reported to TABC_\n\n"
        f"🏆 *{winner['name']} is bar of the month!*\n"
        f"_{winner['address']}_\n"
        f"*{_fmt_dollars(winner['sales'])}* in sales\n\n"
    )

    msg += "*Top 10 by Sales:*\n"
    max_sales = top10[0]["sales"]
    for i, r in enumerate(top10, 1):
        bar = "█" * min(8, round(r["sales"] / max_sales * 8))
        msg += f"{i}. *{r['name']}* — {_fmt_dollars(r['sales'])}\n"
        msg += f"   {bar}\n"

    if movers:
        msg += f"\n📈 *Biggest Movers ({prior_label} → {month_label}):*\n"
        for r in movers:
            pct_str = f"+{r['pct']:.0f}%"
            msg += (
                f"• *{r['name']}*\n"
                f"  {_fmt_dollars(r['prior'])} → {_fmt_dollars(r['current'])} "
                f"_{pct_str} · +{_fmt_dollars(r['delta'])}_\n"
            )

    msg += "\n_Source: [TABC Mixed Beverage Sales](https://data.texas.gov/d/g5bj-yb6k)_"
    return msg


async def bars_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("⏳ Fetching TABC sales data...")
    try:
        data = await asyncio.to_thread(_get_bar_stats)
        msg = _format_bar_stats(data)
        await _send_chunked(update.message, msg)
    except Exception as e:
        logger.error(f"bars command: {e}")
        await update.message.reply_text(f"❌ Error fetching bar data: {e}")


# =============================================================================
# CHILD CARE LICENSING
# =============================================================================


@rate_limited
async def childcare_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("⏳ Fetching child care licensing data...")
    try:
        stats = await asyncio.to_thread(get_childcare_stats)
        msg = format_childcare(stats)
        await _send_chunked(update.message, msg)
    except Exception as e:
        logger.error(f"childcare command: {e}")
        await update.message.reply_text(f"❌ Error fetching child care data: {e}")


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
    app.add_handler(CallbackQueryHandler(about_cb, pattern="^about$"))

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

    # Coyote inline (sub-service of animal)
    app.add_handler(CallbackQueryHandler(coyote_menu_cb, pattern="^coyote_menu"))
    app.add_handler(CallbackQueryHandler(coyote_overview_cb, pattern="^coyote_overview"))
    app.add_handler(CallbackQueryHandler(coyote_seasonal_cb, pattern="^coyote_seasonal"))
    app.add_handler(CallbackQueryHandler(coyote_hotspots_cb, pattern="^coyote_hotspots"))

    # Traffic inline
    app.add_handler(CallbackQueryHandler(traffic_backlog_cb, pattern="^traffic_backlog"))
    app.add_handler(CallbackQueryHandler(traffic_signals_cb, pattern="^traffic_signals"))
    app.add_handler(CallbackQueryHandler(traffic_live_cb, pattern="^traffic_live$"))
    app.add_handler(CallbackQueryHandler(traffic_crashes_cb, pattern="^traffic_crashes$"))
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

    # Parks slash command + inline
    app.add_handler(CommandHandler("parks", parks_command))
    app.add_handler(CallbackQueryHandler(parks_overview_cb,       pattern="^parks_overview$"))
    app.add_handler(CallbackQueryHandler(parks_overview_days_cb,  pattern="^parks_overview_(30|60|90)$"))
    app.add_handler(CallbackQueryHandler(parks_detail_cb,          pattern="^parks_detail_"))
    app.add_handler(CallbackQueryHandler(parks_time_window_cb,    pattern="^parks_time_window$"))
    app.add_handler(CallbackQueryHandler(parks_resolution_cb,     pattern="^parks_resolution$"))

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
    app.add_handler(CallbackQueryHandler(police_homeless_cb, pattern="^police_homeless$"))
    app.add_handler(CommandHandler("budget", homeless_command))

    # Homeless encampment 311 reports
    app.add_handler(CommandHandler("homeless", homeless_311_command))
    app.add_handler(CallbackQueryHandler(homeless311_stats_cb,       pattern="^homeless311_stats_"))
    app.add_handler(CallbackQueryHandler(homeless311_locations_cb,   pattern="^homeless311_locations_"))
    app.add_handler(CallbackQueryHandler(homeless311_time_window_cb, pattern="^homeless311_time_window$"))
    app.add_handler(CallbackQueryHandler(homeless311_map_cb,         pattern="^homeless311_map_"))


    # Bicycle slash commands
    app.add_handler(CommandHandler("animal", animal_command))
    app.add_handler(CommandHandler("coyote", coyote_command))
    app.add_handler(CommandHandler("bicycle", bicycle_command))
    app.add_handler(CommandHandler("ticket", ticket_command))

    # Restaurant slash command
    app.add_handler(CommandHandler("rest", restaurant_command))

    # Traffic slash command
    app.add_handler(CommandHandler("traffic", traffic_command))

    # Noise slash command
    app.add_handler(CommandHandler("noise", noisecomplaints_command))

    # /report archived — incompatible with privacy-first (no user data) policy

    # Code violations slash command
    app.add_handler(CommandHandler("code", code_command))

    # Water quality & building permits
    app.add_handler(CommandHandler("water", water_command))
    app.add_handler(CommandHandler("waterviolations", waterviolations_command))
    app.add_handler(CommandHandler("permits", permits_command))
    app.add_handler(CommandHandler("bars", bars_command))
    app.add_handler(CommandHandler("childcare", childcare_command))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo_handler))
    app.add_error_handler(error_handler)

    # Register commands with Telegram so they appear in autocomplete
    async def post_init(application) -> None:
        await application.bot.set_my_commands([
            BotCommand("start",    "Main menu"),
            BotCommand("crime",    "Recent APD crime stats"),
            BotCommand("budget", "City budget — homelessness services, NGO grants, pension & benefits"),
            BotCommand("safety",   "Crime by district — stats + city comparison"),
            BotCommand("traffic",  "Traffic & infrastructure — signals · lights · sidewalks"),
            BotCommand("parking",  "Parking enforcement — citations · hot zones · stats"),
            BotCommand("parks",    "Park maintenance — hotspots · stats · resolution times"),
            BotCommand("bicycle",  "Bicycle complaints — recent · stats"),
            BotCommand("rest",     "Restaurant inspections — worst scores · grades · search"),
            BotCommand("noise",    "Noise complaints — hotspots · stats · response times"),
            BotCommand("graffiti", "Graffiti — analysis · hotspots · remediation"),
            BotCommand("animal",   "Animal complaints — hotspots · stats · response times"),
            BotCommand("coyote",   "Coyote complaints — seasonal patterns · hotspots"),
            BotCommand("ticket",   "Look up any 311 ticket by ID"),
            BotCommand("water",            "Surface water quality — fecal coliform · DO · nutrients"),
            BotCommand("waterviolations",  "Water conservation violations — sprinklers · leaks · waste"),
            BotCommand("permits",          "Building permits — last 30 days by type · district"),
            BotCommand("bars",      "Bar of the month — top TABC mixed beverage sales"),
            BotCommand("childcare", "Child care licensing — Austin facilities · compliance flags"),
            BotCommand("homeless",  "Encampment & trash 311 reports — dept burden · trends · locations"),
            BotCommand("help",      "All commands"),
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
