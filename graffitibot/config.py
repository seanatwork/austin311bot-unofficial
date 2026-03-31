#!/usr/bin/env python3
"""
Configuration module for Graffiti Bot

Centralizes configuration from environment variables with sensible defaults.
"""

import os
from pathlib import Path
from typing import Optional


class Config:
    """Configuration container"""

    # Database
    DB_PATH: str = os.getenv("DB_PATH", "../311_categories.db")

    # Telegram
    TELEGRAM_BOT_TOKEN: Optional[str] = os.getenv("TELEGRAM_BOT_TOKEN")

    # Service code for graffiti
    SERVICE_CODE: str = "HHSGRAFF"
    SERVICE_NAME: str = "Graffiti Abatement - Public Property"

    # Analysis defaults
    DEFAULT_ANALYSIS_DAYS: int = 90
    MIN_ANALYSIS_DAYS: int = 1
    MAX_ANALYSIS_DAYS: int = 365

    # Hotspot clustering
    HOTSPOT_THRESHOLD: float = 0.001  # ~100 meters
    MIN_HOTSPOT_SIZE: int = 3

    # Remediation
    MAX_REMEDIATION_DAYS: int = 60

    # API rate limiting
    API_REQUESTS_PER_SECOND: float = 0.5  # 1 request per 2 seconds
    API_BACKOFF_SECONDS: float = 2.0

    # Logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    LOG_FORMAT: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

    @classmethod
    def validate(cls) -> bool:
        """Validate required configuration"""
        if not cls.TELEGRAM_BOT_TOKEN:
            raise ValueError(
                "TELEGRAM_BOT_TOKEN environment variable is required. "
                "Set it in Railway dashboard or .env file."
            )
        return True

    @classmethod
    def get_project_root(cls) -> Path:
        """Get project root directory"""
        return Path(__file__).parent.parent

    @classmethod
    def get_db_path(cls) -> Path:
        """Get absolute path to database"""
        db_path = Path(cls.DB_PATH)
        if not db_path.is_absolute():
            db_path = cls.get_project_root() / db_path
        return db_path


def setup_logging() -> None:
    """Configure logging based on config"""
    import logging

    logging.basicConfig(
        level=getattr(logging, Config.LOG_LEVEL),
        format=Config.LOG_FORMAT,
    )


# Convenience function
def get_config() -> Config:
    """Get configuration instance"""
    return Config()
