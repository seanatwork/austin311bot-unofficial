"""
Restaurant Inspections service module.

Queries Austin restaurant inspection data from the Socrata open data API
(dataset ecmv-9xxi). No local database — all queries are live.
"""

from .restaurant_bot import (
    search_restaurants,
    get_lowest_scoring,
    get_grade_distribution,
    format_search_results,
    format_low_scores,
    format_grade_distribution,
)

__all__ = [
    "search_restaurants",
    "get_lowest_scoring",
    "get_grade_distribution",
    "format_search_results",
    "format_low_scores",
    "format_grade_distribution",
]
