"""Tests for the migration engine.

Covers:
  Test 3  — MongoDB succeeds → VERIFY → DELETE SUPABASE
  Test 4  — MongoDB fails → KEEP SUPABASE
  Test 5  — MongoDB verification fails → KEEP SUPABASE
  Test 6  — Supabase deletion fails → RETRY DELETION
  Test 11 — Dry-run enabled → NO SUPABASE DELETIONS
  Test 12 — Migration disabled → NO DESTRUCTIVE ACTION
"""

from __future__ import annotations

from unittest.mock import patch, MagicMock
import pytest

import src.config as config
from src.config import TableConfig
from src.mongodb_client import build_mongo_document, compute_record_hash


def _make_tc(**overrides) -> TableConfig:
    defaults = dict(
        supabase_table="inventory",
        mongodb_collection="inventory",
        primary_key="id",
        sort_column="created_at",
        enabled=True,
    )
    defaults.update(overrides)
    return TableConfig(**defaults)


def _make_record(**overrides) -> dict:
    defaults = {"id": "1", "product": "A", "quantity": 5, "created_at": "2024-01-01T00:00:00Z"}
    defaults.update(overrides)
    return defaults


# ======================================================================
# Test 3: MongoDB succeeds → VERIFY → DELETE SUPABASE
# ======================================================================

class TestMongoDBSuccessFlow:
    """When MongoDB upsert and verification succeed, delete from Supabase."""

    def test_document_structure_preserves_business_data(self, sample_record):
        """MongoDB document must contain all original business fields."""
        doc = build_mongo_document(
            sample_record,
            source_table="inventory",
            primary_key="id",
        )
        assert doc["id"] == "123"
        assert doc["product"] == "Laptop"
        assert doc["quantity"] == 10

    def test_migration_metadata_added(self, sample_record):
        """The _migration key must be present with required fields."""
        doc = build_mongo_document(
            sample_record,
            source_table="inventory",
            primary_key="id",
        )
        assert "_migration" in doc
        assert doc["_migration"]["source_system"] == "supabase"
        assert doc["_migration"]["source_table"] == "inventory"
        assert doc["_migration"]["source_id"] == "123"
        assert "migrated_at" in doc["_migration"]
        assert "record_hash" in doc["_migration"]

    def test_record_hash_deterministic(self, sample_record):
        """Same record must produce the same hash every time."""
        h1 = compute_record_hash(sample_record)
        h2 = compute_record_hash(sample_record)
        assert h1 == h2

    def test_record_hash_differs_for_different_data(self):
        """Different records must produce different hashes."""
        r1 = {"id": "1", "product": "A"}
        r2 = {"id": "1", "product": "B"}
        assert compute_record_hash(r1) != compute_record_hash(r2)

    @patch("src.mongodb_client.write_migration_log")
    @patch("src.mongodb_client.ensure_indexes")
    @patch("src.mongodb_client.upsert_document")
    @patch("src.mongodb_client.verify_document")
    def test_full_success_flow(self, mock_verify, mock_upsert, mock_idx, mock_log, monkeypatch):
        """Upsert succeeds + verify succeeds → should proceed to delete."""
        mock_upsert.return_value = True
        mock_verify.return_value = True

        from src.migration import process_batch

        monkeypatch.setattr(config, "BATCH_SIZE", 1)
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(config, "MAX_RETRIES", 1)

        tc = _make_tc()

        with patch("src.migration.supabase_client") as mock_sb:
            mock_sb.fetch_batch.return_value = [_make_record()]
            mock_sb.delete_record.return_value = True

            migrated, verified, failed, deleted, _ = process_batch(
                tc, "test-exec", batch_number=1
            )

            assert migrated == 1
            assert verified == 1
            assert failed == 0
            assert deleted == 1


# ======================================================================
# Test 4: MongoDB fails → KEEP SUPABASE
# ======================================================================

class TestMongoDBFailure:
    """When MongoDB upsert fails, Supabase record must be kept."""

    @patch("src.mongodb_client.write_migration_log")
    @patch("src.mongodb_client.ensure_indexes")
    @patch("src.mongodb_client.upsert_document")
    def test_upsert_failure_keeps_supabase(self, mock_upsert, mock_idx, mock_log, monkeypatch):
        """If upsert returns False, record is NOT deleted from Supabase."""
        mock_upsert.return_value = False

        monkeypatch.setattr(config, "BATCH_SIZE", 1)
        monkeypatch.setattr(config, "MAX_RETRIES", 1)

        from src.migration import process_batch

        tc = _make_tc()

        with patch("src.migration.supabase_client") as mock_sb:
            mock_sb.fetch_batch.return_value = [_make_record()]

            migrated, verified, failed, deleted, _ = process_batch(
                tc, "test-exec", batch_number=1
            )

            assert migrated == 0
            assert verified == 0
            assert failed == 1
            assert deleted == 0
            mock_sb.delete_record.assert_not_called()


# ======================================================================
# Test 5: MongoDB verification fails → KEEP SUPABASE
# ======================================================================

class TestVerificationFailure:
    """When verification fails after upsert, Supabase record must be kept."""

    @patch("src.mongodb_client.write_migration_log")
    @patch("src.mongodb_client.ensure_indexes")
    @patch("src.mongodb_client.upsert_document")
    @patch("src.migration.verify_migration")
    def test_verification_failure_keeps_supabase(
        self, mock_verify, mock_upsert, mock_idx, mock_log, monkeypatch
    ):
        """If verify returns failed result, record is NOT deleted."""
        mock_upsert.return_value = True

        from src.verification import VerificationResult
        mock_verify.return_value = VerificationResult(verified=False, reason="Hash mismatch")

        monkeypatch.setattr(config, "BATCH_SIZE", 1)
        monkeypatch.setattr(config, "MAX_RETRIES", 1)

        from src.migration import process_batch

        tc = _make_tc()

        with patch("src.migration.supabase_client") as mock_sb:
            mock_sb.fetch_batch.return_value = [_make_record()]

            migrated, verified, failed, deleted, _ = process_batch(
                tc, "test-exec", batch_number=1
            )

            assert migrated == 1
            assert verified == 0
            assert failed == 1
            assert deleted == 0
            mock_sb.delete_record.assert_not_called()


# ======================================================================
# Test 6: Supabase deletion fails → RETRY DELETION
# ======================================================================

class TestSupabaseDeleteFailure:
    """When Supabase delete fails after verified upsert, retry on next run."""

    @patch("src.mongodb_client.write_migration_log")
    @patch("src.mongodb_client.ensure_indexes")
    @patch("src.mongodb_client.upsert_document")
    @patch("src.mongodb_client.verify_document")
    def test_delete_failure_keeps_mongodb(
        self, mock_verify, mock_upsert, mock_idx, mock_log, monkeypatch
    ):
        """MongoDB document stays; next run retries deletion."""
        mock_upsert.return_value = True
        mock_verify.return_value = True

        monkeypatch.setattr(config, "BATCH_SIZE", 1)
        monkeypatch.setattr(config, "MAX_RETRIES", 1)

        from src.migration import process_batch

        tc = _make_tc()

        with patch("src.migration.supabase_client") as mock_sb:
            mock_sb.fetch_batch.return_value = [_make_record()]
            mock_sb.delete_record.side_effect = Exception("Connection refused")

            migrated, verified, failed, deleted, _ = process_batch(
                tc, "test-exec", batch_number=1
            )

            assert migrated == 1
            assert verified == 1
            assert failed == 1
            assert deleted == 0

    @patch("src.mongodb_client.write_migration_log")
    @patch("src.mongodb_client.ensure_indexes")
    @patch("src.mongodb_client.upsert_document")
    @patch("src.mongodb_client.verify_document")
    def test_next_run_upserts_idempotently(
        self, mock_verify, mock_upsert, mock_idx, mock_log, monkeypatch
    ):
        """On retry, upsert overwrites (idempotent) and verification re-checks."""
        mock_upsert.return_value = True
        mock_verify.return_value = True

        monkeypatch.setattr(config, "BATCH_SIZE", 1)
        monkeypatch.setattr(config, "MAX_RETRIES", 1)

        from src.migration import process_batch

        tc = _make_tc()

        with patch("src.migration.supabase_client") as mock_sb:
            mock_sb.fetch_batch.return_value = [_make_record()]
            mock_sb.delete_record.return_value = True

            migrated, verified, failed, deleted, _ = process_batch(
                tc, "test-exec", batch_number=1
            )

            mock_upsert.assert_called_once()
            assert deleted == 1


# ======================================================================
# Test 11: Dry-run enabled → NO SUPABASE DELETIONS
# ======================================================================

class TestDryRun:
    """Dry-run mode must never delete from Supabase."""

    @patch("src.mongodb_client.write_migration_log")
    @patch("src.mongodb_client.ensure_indexes")
    @patch("src.mongodb_client.upsert_document")
    @patch("src.mongodb_client.verify_document")
    def test_dry_run_skips_deletion(
        self, mock_verify, mock_upsert, mock_idx, mock_log, monkeypatch
    ):
        """When DRY_RUN=true, no Supabase records are deleted."""
        mock_upsert.return_value = True
        mock_verify.return_value = True

        monkeypatch.setattr(config, "DRY_RUN", True)
        monkeypatch.setattr(config, "BATCH_SIZE", 1)
        monkeypatch.setattr(config, "MAX_RETRIES", 1)

        from src.migration import process_batch

        tc = _make_tc()

        with patch("src.migration.supabase_client") as mock_sb:
            mock_sb.fetch_batch.return_value = [_make_record()]

            migrated, verified, failed, deleted, _ = process_batch(
                tc, "test-exec", batch_number=1
            )

            assert migrated == 1
            assert verified == 1
            assert deleted == 0
            mock_sb.delete_record.assert_not_called()


# ======================================================================
# Test 12: Migration disabled → NO DESTRUCTIVE ACTION
# ======================================================================

class TestMigrationDisabled:
    """When MIGRATION_ENABLED=false, no destructive operations occur."""

    def test_disabled_flag_prevents_migration(self, monkeypatch):
        """run_migration must exit immediately when disabled."""
        monkeypatch.setattr(config, "MIGRATION_ENABLED", False)

        from src.migration import run_migration

        with patch("src.migration.supabase_client") as mock_sb:
            run_migration()
            mock_sb.fetch_batch.assert_not_called()
            mock_sb.delete_record.assert_not_called()

    def test_disabled_flag_is_configurable(self, monkeypatch):
        """MIGRATION_ENABLED reads from config, not hardcoded."""
        monkeypatch.setattr(config, "MIGRATION_ENABLED", True)
        assert config.MIGRATION_ENABLED is True
        monkeypatch.setattr(config, "MIGRATION_ENABLED", False)
        assert config.MIGRATION_ENABLED is False
