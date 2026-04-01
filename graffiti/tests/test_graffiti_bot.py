#!/usr/bin/env python3
"""
Tests for Graffiti Analysis Bot

Run with: pytest tests/test_graffiti_bot.py -v
"""

import pytest
import sqlite3
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

# Import modules to test
from graffitibot.graffiti_bot import GraffitiAnalysisBot, analyze_graffiti_command
from graffitibot.remediation_analysis import (
    GraffitiRemediationAnalyzer,
    remediation_command,
)
from graffitibot.config import Config


class TestConfig:
    """Test configuration module"""

    def test_config_defaults(self):
        """Test default configuration values"""
        assert Config.SERVICE_CODE == "HHSGRAFF"
        assert Config.DEFAULT_ANALYSIS_DAYS == 90
        assert Config.MIN_ANALYSIS_DAYS == 1
        assert Config.MAX_ANALYSIS_DAYS == 365
        assert Config.HOTSPOT_THRESHOLD == 0.001

    def test_config_validation(self):
        """Test config validation raises error without token"""
        # TELEGRAM_BOT_TOKEN should not be set in test environment
        assert Config.TELEGRAM_BOT_TOKEN is None


class TestGraffitiAnalysisBot:
    """Test GraffitiAnalysisBot class"""

    @pytest.fixture
    def temp_db(self):
        """Create a temporary database for testing"""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        # Create test schema and data
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        cursor.execute(
            """
            CREATE TABLE open311_requests (
                service_request_id TEXT,
                service_code TEXT,
                requested_datetime TEXT,
                updated_datetime TEXT,
                status TEXT,
                status_notes TEXT,
                address TEXT,
                zipcode TEXT,
                lat REAL,
                long REAL,
                media_url TEXT,
                attributes_json TEXT,
                extended_attributes_json TEXT
            )
        """
        )

        # Insert test data
        now = datetime.now()
        test_data = [
            (
                "SR001",
                "HHSGRAFF",
                (now - timedelta(days=5)).isoformat() + "Z",
                (now - timedelta(days=2)).isoformat() + "Z",
                "closed",
                "Removed",
                "123 Main St",
                "78701",
                30.2672,
                -97.7431,
                None,
                None,
                None,
            ),
            (
                "SR002",
                "HHSGRAFF",
                (now - timedelta(days=10)).isoformat() + "Z",
                (now - timedelta(days=5)).isoformat() + "Z",
                "closed",
                "Cleaned",
                "456 Oak Ave",
                "78702",
                30.2672,
                -97.7431,
                None,
                None,
                None,
            ),
            (
                "SR003",
                "HHSGRAFF",
                (now - timedelta(days=3)).isoformat() + "Z",
                None,
                "open",
                None,
                "789 Elm St",
                "78703",
                30.2672,
                -97.7431,
                None,
                None,
                None,
            ),
        ]

        cursor.executemany(
            """
            INSERT INTO open311_requests VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            test_data,
        )

        conn.commit()
        conn.close()

        yield db_path

        # Cleanup
        os.unlink(db_path)

    def test_bot_initialization(self, temp_db):
        """Test bot initializes with correct parameters"""
        bot = GraffitiAnalysisBot(db_path=temp_db)
        assert bot.service_code == "HHSGRAFF"
        assert bot.db_path == temp_db

    def test_get_graffiti_data(self, temp_db):
        """Test data retrieval"""
        bot = GraffitiAnalysisBot(db_path=temp_db)
        records = bot.get_graffiti_data(days_back=30)

        assert len(records) == 3
        assert all(r["service_code"] == "HHSGRAFF" for r in records)

    def test_get_graffiti_data_invalid_days(self, temp_db):
        """Test input validation for days_back parameter"""
        bot = GraffitiAnalysisBot(db_path=temp_db)

        with pytest.raises(ValueError):
            bot.get_graffiti_data(days_back=0)

        with pytest.raises(ValueError):
            bot.get_graffiti_data(days_back=400)

    def test_analyze_patterns(self, temp_db):
        """Test pattern analysis"""
        bot = GraffitiAnalysisBot(db_path=temp_db)
        records = bot.get_graffiti_data(days_back=30)
        analysis = bot.analyze_patterns(records)

        assert analysis["total_records"] == 3
        assert "status_distribution" in analysis
        assert "temporal_patterns" in analysis

    def test_find_hotspots(self, temp_db):
        """Test hotspot detection"""
        bot = GraffitiAnalysisBot(db_path=temp_db)

        # All test data has same coordinates, should form a hotspot
        locations = [(30.2672, -97.7431), (30.2672, -97.7431), (30.2672, -97.7431)]
        hotspots = bot.find_hotspots(locations)

        assert len(hotspots) == 1
        assert hotspots[0]["count"] == 3

    def test_temporal_analysis(self, temp_db):
        """Test temporal pattern analysis"""
        bot = GraffitiAnalysisBot(db_path=temp_db)
        records = bot.get_graffiti_data(days_back=30)
        temporal = bot.analyze_temporal_patterns(records)

        assert "busiest_day" in temporal
        assert "busiest_hour" in temporal
        assert "hourly_distribution" in temporal

    def test_format_analysis_report(self, temp_db):
        """Test report formatting"""
        bot = GraffitiAnalysisBot(db_path=temp_db)
        records = bot.get_graffiti_data(days_back=30)
        analysis = bot.analyze_patterns(records)
        report = bot.format_analysis_report(analysis)

        assert "GRAFFITI ANALYSIS REPORT" in report
        assert "Total Records: 3" in report


class TestRemediationAnalyzer:
    """Test GraffitiRemediationAnalyzer class"""

    @pytest.fixture
    def temp_db(self):
        """Create a temporary database for testing"""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        cursor.execute(
            """
            CREATE TABLE open311_requests (
                service_request_id TEXT,
                service_code TEXT,
                requested_datetime TEXT,
                updated_datetime TEXT,
                status TEXT,
                status_notes TEXT,
                address TEXT,
                zipcode TEXT,
                lat REAL,
                long REAL,
                media_url TEXT,
                attributes_json TEXT,
                extended_attributes_json TEXT
            )
        """
        )

        # Insert test data with known remediation times
        now = datetime.now()
        test_data = [
            (
                "SR001",
                "HHSGRAFF",
                (now - timedelta(days=10)).isoformat() + "Z",
                (now - timedelta(days=5)).isoformat() + "Z",
                "closed",
                "Removed",
                "123 Main St",
                "78701",
                30.2672,
                -97.7431,
                None,
                None,
                None,
            ),
            (
                "SR002",
                "HHSGRAFF",
                (now - timedelta(days=20)).isoformat() + "Z",
                (now - timedelta(days=10)).isoformat() + "Z",
                "closed",
                "Cleaned",
                "456 Oak Ave",
                "78702",
                30.2672,
                -97.7431,
                None,
                None,
                None,
            ),
        ]

        cursor.executemany(
            """
            INSERT INTO open311_requests VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            test_data,
        )

        conn.commit()
        conn.close()

        yield db_path
        os.unlink(db_path)

    def test_remediation_initialization(self, temp_db):
        """Test analyzer initialization"""
        analyzer = GraffitiRemediationAnalyzer(db_path=temp_db)
        assert analyzer.service_code == "HHSGRAFF"

    def test_calculate_remediation_times(self, temp_db):
        """Test remediation time calculation"""
        analyzer = GraffitiRemediationAnalyzer(db_path=temp_db)
        records = analyzer.get_graffiti_with_dates(days_back=30)
        remediation_times = analyzer.calculate_remediation_times(records)

        assert len(remediation_times) == 2
        # First record: 5 days, second record: 10 days
        assert remediation_times[0]["remediation_days"] == 5
        assert remediation_times[1]["remediation_days"] == 10

    def test_analyze_remediation_patterns(self, temp_db):
        """Test remediation pattern analysis"""
        analyzer = GraffitiRemediationAnalyzer(db_path=temp_db)
        records = analyzer.get_graffiti_with_dates(days_back=30)
        remediation_times = analyzer.calculate_remediation_times(records)
        analysis = analyzer.analyze_remediation_patterns(remediation_times)

        assert analysis["total_closed"] == 2
        assert analysis["average_days"] == 7.5
        assert analysis["median_days"] == 7.5

    def test_remediation_command_error_handling(self):
        """Test error handling in command function"""
        # Test with non-existent database
        result = remediation_command(90)
        # Should return error message, not raise exception
        assert isinstance(result, str)


class TestCommandFunctions:
    """Test command functions"""

    def test_analyze_graffiti_command_no_data(self):
        """Test analyze command with no data"""
        result = analyze_graffiti_command(90)
        assert isinstance(result, str)
        # Should return helpful message about no data or DB error

    def test_remediation_command_no_data(self):
        """Test remediation command with no data"""
        result = remediation_command(90)
        assert isinstance(result, str)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
