#!/usr/bin/env python3
"""
CLI entry point for the Graffiti Analysis Bot

Usage:
    graffiti-bot demo          - Run demo analysis
    graffiti-bot ingest        - Ingest graffiti data from Open311
    graffiti-bot analyze       - Run full analysis
    graffiti-bot telegram      - Start Telegram bot
"""

import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    command = sys.argv[1].lower()

    if command == "demo":
        from graffiti.graffiti_bot import analyze_graffiti_command
        print(analyze_graffiti_command(90))

    elif command == "ingest":
        from graffiti.ingest_graffiti_data import ingest_graffiti_last_90_days
        ingest_graffiti_last_90_days()

    elif command == "analyze":
        from graffiti.graffiti_bot import analyze_graffiti_command
        print(analyze_graffiti_command(90))

    elif command == "telegram":
        from austin311_bot import main as telegram_main
        telegram_main()

    elif command == "remediation":
        from graffiti.remediation_analysis import remediation_command
        print(remediation_command(90))

    elif command == "help":
        print(__doc__)

    else:
        print(f"Unknown command: {command}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
