"""
Animal Services module.

Queries Austin Open311 API for animal-related service codes.
"""

from .animal_bot import (
    get_hotspots,
    get_stats,
    get_response_times,
    format_hotspots,
    format_stats,
    format_response_times,
)

__all__ = [
    "get_hotspots",
    "get_stats",
    "get_response_times",
    "format_hotspots",
    "format_stats",
    "format_response_times",
]
