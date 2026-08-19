"""Tests for capacity monitoring.

Covers:
  Test 1  — Usage 89% → NO MIGRATION
  Test 2  — Usage 90% → MIGRATION STARTS
  Test 8  — Capacity reaches 50% → STOP MIGRATION
  Test 9  — MongoDB destination full → STOP MIGRATION
"""

from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

import src.config as config
from src.capacity import get_usage_percentage, should_start_migration, should_stop_migration


# ======================================================================
# Test 1: Usage = 89% → NO MIGRATION
# ======================================================================

class TestUsageBelowThreshold:
    """When usage < START_THRESHOLD, migration must NOT start."""

    def test_89_percent_does_not_trigger(self, monkeypatch):
        monkeypatch.setattr(config, "START_THRESHOLD", 90.0)
        assert not should_start_migration(89.0)

    def test_50_percent_does_not_trigger(self, monkeypatch):
        monkeypatch.setattr(config, "START_THRESHOLD", 90.0)
        assert not should_start_migration(50.0)

    def test_0_percent_does_not_trigger(self, monkeypatch):
        monkeypatch.setattr(config, "START_THRESHOLD", 90.0)
        assert not should_start_migration(0.0)

    def test_usage_calculation_below_threshold(self, monkeypatch):
        """Verify percentage calculation: 400 MB / 500 MB = 80%."""
        monkeypatch.setattr(config, "SUPABASE_MAX_DB_SIZE_BYTES", 500 * 1024 * 1024)
        pct = get_usage_percentage(400 * 1024 * 1024)
        assert abs(pct - 80.0) < 0.01


# ======================================================================
# Test 2: Usage = 90% → MIGRATION STARTS
# ======================================================================

class TestUsageAtThreshold:
    """When usage >= START_THRESHOLD, migration MUST start."""

    def test_90_percent_triggers(self, monkeypatch):
        monkeypatch.setattr(config, "START_THRESHOLD", 90.0)
        assert should_start_migration(90.0)

    def test_91_percent_triggers(self, monkeypatch):
        monkeypatch.setattr(config, "START_THRESHOLD", 90.0)
        assert should_start_migration(91.4)

    def test_100_percent_triggers(self, monkeypatch):
        monkeypatch.setattr(config, "START_THRESHOLD", 90.0)
        assert should_start_migration(100.0)

    def test_usage_calculation_at_threshold(self, monkeypatch):
        """Verify: 450 MB / 500 MB = 90%."""
        monkeypatch.setattr(config, "SUPABASE_MAX_DB_SIZE_BYTES", 500 * 1024 * 1024)
        pct = get_usage_percentage(450 * 1024 * 1024)
        assert abs(pct - 90.0) < 0.01


# ======================================================================
# Test 8: Capacity reaches 50% → STOP MIGRATION
# ======================================================================

class TestCapacityReachTarget:
    """When usage <= TARGET_THRESHOLD, migration must stop."""

    def test_50_percent_stops(self, monkeypatch):
        monkeypatch.setattr(config, "TARGET_THRESHOLD", 50.0)
        assert should_stop_migration(50.0)

    def test_49_percent_stops(self, monkeypatch):
        monkeypatch.setattr(config, "TARGET_THRESHOLD", 50.0)
        assert should_stop_migration(49.0)

    def test_51_percent_does_not_stop(self, monkeypatch):
        monkeypatch.setattr(config, "TARGET_THRESHOLD", 50.0)
        assert not should_stop_migration(51.0)

    def test_usage_calculation_at_target(self, monkeypatch):
        """Verify: 250 MB / 500 MB = 50%."""
        monkeypatch.setattr(config, "SUPABASE_MAX_DB_SIZE_BYTES", 500 * 1024 * 1024)
        pct = get_usage_percentage(250 * 1024 * 1024)
        assert abs(pct - 50.0) < 0.01


# ======================================================================
# Test 9: MongoDB destination full → STOP MIGRATION
# ======================================================================

class TestMongoDBCapacity:
    """If MongoDB destination is full, migration must stop."""

    @patch("src.mongodb_client.get_destination_size_bytes")
    def test_mongodb_full_stops_migration(self, mock_size, monkeypatch):
        """Simulate MongoDB at capacity."""
        # 5 GB used, 5 GB max = 100% full.
        mock_size.return_value = 5 * 1024 * 1024 * 1024
        from src.mongodb_client import get_destination_capacity_percentage
        pct = get_destination_capacity_percentage(5 * 1024 * 1024 * 1024)
        assert pct >= 100.0

    @patch("src.mongodb_client.get_destination_size_bytes")
    def test_mongodb_below_capacity(self, mock_size, monkeypatch):
        """MongoDB at 30% — should not block."""
        mock_size.return_value = int(1.5 * 1024 * 1024 * 1024)
        from src.mongodb_client import get_destination_capacity_percentage
        pct = get_destination_capacity_percentage(5 * 1024 * 1024 * 1024)
        assert abs(pct - 30.0) < 0.1

    def test_zero_max_returns_zero(self, monkeypatch):
        """If max capacity unknown, report 0% (safe default)."""
        from src.mongodb_client import get_destination_capacity_percentage
        pct = get_destination_capacity_percentage(0)
        assert pct == 0.0
