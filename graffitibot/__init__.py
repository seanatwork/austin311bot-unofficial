"""
Graffiti Analysis Bot Package

A data-driven graffiti analysis and heatmapping bot for Austin 311 system.
"""

from .graffiti_bot import GraffitiAnalysisBot, analyze_graffiti_command, hotspot_command, patterns_command
from .remediation_analysis import GraffitiRemediationAnalyzer, remediation_command, compare_command, trends_command
from .config import Config, setup_logging

__version__ = "0.1.0"
__all__ = [
    "GraffitiAnalysisBot",
    "GraffitiRemediationAnalyzer",
    "analyze_graffiti_command",
    "hotspot_command",
    "patterns_command",
    "remediation_command",
    "compare_command",
    "trends_command",
    "Config",
    "setup_logging",
]
