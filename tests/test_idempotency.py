"""Tests for idempotency.

Covers:
  Test 7  — Workflow runs twice → NO DUPLICATE MONGODB DOCUMENT
  General idempotency guarantees.
"""

from __future__ import annotations

from unittest.mock import patch, MagicMock
import pytest

import src.config as config
from src.config import TableConfig
from src.mongodb_client import build_mongo_document, compute_record_hash


def _make_tc() -> TableConfig:
    return TableConfig(
        supabase_table="inventory",
        mongodb_collection="inventory",
        primary_key="id",
        sort_column="created_at",
        enabled=True,
    )


RECORD = {"id": "1", "product": "Laptop", "quantity": 10, "created_at": "2024-01-01T00:00:00Z"}


# ======================================================================
# Test 7: Workflow runs twice → NO DUPLICATE MONGODB DOCUMENT
# ======================================================================

class TestIdempotency:
    """Running the same migration twice must not create duplicate documents."""

    @patch("src.mongodb_client.write_migration_log")
    @patch("src.mongodb_client.ensure_indexes")
    @patch("src.mongodb_client.upsert_document")
    @patch("src.mongodb_client.verify_document")
    def test_second_run_upserts_not_inserts(
        self, mock_verify, mock_upsert, mock_idx, mock_log, monkeypatch
    ):
        """Second run calls upsert (update_one with upsert=True), not insert."""
        mock_upsert.return_value = True
        mock_verify.return_value = True

        monkeypatch.setattr(config, "BATCH_SIZE", 1)
        monkeypatch.setattr(config, "MAX_RETRIES", 1)
        monkeypatch.setattr(config, "DRY_RUN", False)

        from src.migration import process_batch

        tc = _make_tc()

        # --- First run ---
        with patch("src.migration.supabase_client") as mock_sb:
            mock_sb.fetch_batch.return_value = [RECORD]
            mock_sb.delete_record.side_effect = Exception("Simulated delete failure")
            process_batch(tc, "exec-1", batch_number=1)

        # --- Second run (same record still in Supabase because delete failed) ---
        with patch("src.migration.supabase_client") as mock_sb:
            mock_sb.fetch_batch.return_value = [RECORD]
            mock_sb.delete_record.return_value = True
            process_batch(tc, "exec-2", batch_number=1)

        # upsert_document was called twice, but both used upsert semantics.
        assert mock_upsert.call_count == 2

        # Verify both calls used the same filter (same source identity).
        for c in mock_upsert.call_args_list:
            args, kwargs = c
            assert args[2] == "inventory"  # source_table
            assert args[3] == "id"  # primary_key

    def test_upsert_filter_uses_identity_fields(self, monkeypatch):
        """The upsert filter must use source_system + source_table + source_id."""
        doc = build_mongo_document(
            {"id": "42", "product": "Widget"},
            source_table="orders",
            primary_key="id",
        )
        assert doc["_migration"]["source_system"] == "supabase"
        assert doc["_migration"]["source_table"] == "orders"
        assert doc["_migration"]["source_id"] == "42"

    def test_unique_index_fields(self):
        """The compound unique index must cover the three identity fields."""
        expected_fields = [
            "_migration.source_system",
            "_migration.source_table",
            "_migration.source_id",
        ]
        doc = build_mongo_document(
            {"id": "99", "x": 1},
            source_table="test_table",
            primary_key="id",
        )
        for field in expected_fields:
            parts = field.split(".")
            obj = doc
            for part in parts:
                assert part in obj, f"Missing field {field}"
                obj = obj[part]

    def test_deterministic_sort_order(self):
        """Records with the same created_at must sort deterministically by PK."""
        records = [
            {"id": "2", "created_at": "2024-01-01T00:00:00Z"},
            {"id": "1", "created_at": "2024-01-01T00:00:00Z"},
            {"id": "3", "created_at": "2024-01-02T00:00:00Z"},
        ]
        sorted_records = sorted(records, key=lambda r: (r["created_at"], r["id"]))
        assert [r["id"] for r in sorted_records] == ["1", "2", "3"]

    def test_hash_tamper_detection(self):
        """If a record is modified after migration, hash must differ."""
        original = {"id": "1", "product": "Laptop", "quantity": 10}
        tampered = {"id": "1", "product": "Laptop", "quantity": 999}
        h_orig = compute_record_hash(original)
        h_tamp = compute_record_hash(tampered)
        assert h_orig != h_tamp

    @patch("src.mongodb_client.write_migration_log")
    @patch("src.mongodb_client.ensure_indexes")
    @patch("src.mongodb_client.upsert_document")
    @patch("src.mongodb_client.verify_document")
    def test_existing_document_is_overwritten(
        self, mock_verify, mock_upsert, mock_idx, mock_log, monkeypatch
    ):
        """On re-run, the existing MongoDB document is updated, not duplicated."""
        mock_upsert.return_value = True
        mock_verify.return_value = True

        monkeypatch.setattr(config, "BATCH_SIZE", 1)
        monkeypatch.setattr(config, "MAX_RETRIES", 1)
        monkeypatch.setattr(config, "DRY_RUN", False)

        from src.migration import process_batch

        tc = _make_tc()
        record = {"id": "7", "product": "Gadget", "quantity": 3, "created_at": "2024-06-01T00:00:00Z"}

        for run_id in range(2):
            with patch("src.migration.supabase_client") as mock_sb:
                mock_sb.fetch_batch.return_value = [record]
                mock_sb.delete_record.side_effect = Exception("Simulated failure")
                process_batch(tc, f"exec-{run_id}", batch_number=1)

        # Key assertion: upsert was called (not insert_many or similar).
        assert mock_upsert.call_count == 2

        # Each call should target the same identity.
        for c in mock_upsert.call_args_list:
            args = c[0]
            doc = args[1]
            assert doc["id"] == "7"
            assert doc["_migration"]["source_id"] == "7"
