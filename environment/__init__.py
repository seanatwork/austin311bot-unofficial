"""
Environment overview page.

Combines TCEQ spill reports, Lady Bird Lake chlorophyll-a samples, and Austin
Water conservation violations (311 ``WWREPORT``) into docs/environment/.
"""

from .environment_bot import (
    fetch_spills,
    fetch_water_quality,
    generate_environment_page,
    summarize_spills,
    summarize_water_quality,
)

__all__ = [
    "generate_environment_page",
    "fetch_water_quality",
    "summarize_water_quality",
    "fetch_spills",
    "summarize_spills",
]
