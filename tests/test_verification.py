"""Tests for the verification module.

Covers the core verification logic used by the migration engine.
"""

from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from src.verification import verify_migration, VerificationResult
from src.mongodb_client import compute_record_hash


# ======================================================================
# VerificationResult basics
# ======================================================================

class TestVerificationResult:
    """Verify the VerificationResult value object."""

    def test_verified_is_truthy(self):
        r = VerificationResult(verified=True, reason="ok")
        assert bool(r) is True

    def test_failed_is_falsy(self):
        r = VerificationResult(verified=False, reason="fail")
        assert bool(r) is False

    def test_repr_shows_status(self):
        r = VerificationResult(verified=True, reason="ok")
        assert "VERIFIED" in repr(r)

    def test_repr_shows_failure(self):
        r = VerificationResult(verified=False, reason="bad hash")
        assert "FAILED" in repr(r)
        assert "bad hash" in repr(r)


# ======================================================================
# verify_migration function
# ======================================================================

class TestVerifyMigrationFunction:
    """Integration-style tests for verify_migration."""

    @patch("src.verification.mongodb_client.verify_document")
    def test_returns_verified_on_success(self, mock_verify):
        mock_verify.return_value = True
        result = verify_migration(
            collection_name="inventory",
            source_table="inventory",
            source_id="123",
            original_record={"id": "123", "product": "Laptop"},
        )
        assert result.verified is True
        assert result.reason == "Document verified in MongoDB"

    @patch("src.verification.mongodb_client.verify_document")
    def test_returns_failed_on_mismatch(self, mock_verify):
        mock_verify.return_value = False
        result = verify_migration(
            collection_name="inventory",
            source_table="inventory",
            source_id="123",
            original_record={"id": "123", "product": "Laptop"},
        )
        assert result.verified is False
        assert "not found or hash mismatch" in result.reason

    @patch("src.verification.mongodb_client.verify_document")
    def test_passes_correct_parameters(self, mock_verify):
        mock_verify.return_value = True
        record = {"id": "456", "product": "Phone"}
        verify_migration(
            collection_name="orders",
            source_table="orders",
            source_id="456",
            original_record=record,
        )
        mock_verify.assert_called_once_with(
            collection_name="orders",
            source_table="orders",
            source_id="456",
            original_record=record,
        )

    @patch("src.verification.mongodb_client.verify_document")
    def test_source_id_is_stringified(self, mock_verify):
        """source_id should work with integer IDs."""
        mock_verify.return_value = True
        verify_migration(
            collection_name="inventory",
            source_table="inventory",
            source_id=789,  # integer
            original_record={"id": 789},
        )
        # Verify the call used string "789".
        call_kwargs = mock_verify.call_args[1]
        assert call_kwargs["source_id"] == "789"


# ======================================================================
# Hash integrity
# ======================================================================

class TestHashIntegrity:
    """Verify that record hashes are deterministic and tamper-evident."""

    def test_same_record_same_hash(self):
        r = {"id": "1", "name": "Alice"}
        assert compute_record_hash(r) == compute_record_hash(r)

    def test_different_record_different_hash(self):
        r1 = {"id": "1", "name": "Alice"}
        r2 = {"id": "1", "name": "Bob"}
        assert compute_record_hash(r1) != compute_record_hash(r2)

    def test_key_order_irrelevant(self):
        """JSON serialization must be canonical (sorted keys)."""
        r1 = {"b": 2, "a": 1}
        r2 = {"a": 1, "b": 2}
        assert compute_record_hash(r1) == compute_record_hash(r2)

    def test_migration_metadata_excluded(self):
        """_migration fields must not affect the hash."""
        r1 = {"id": "1", "name": "X"}
        r2 = {"id": "1", "name": "X", "_migration": {"junk": True}}
        assert compute_record_hash(r1) == compute_record_hash(r2)

    def test_hash_is_sha256(self):
        h = compute_record_hash({"id": "1"})
        assert len(h) == 64  # SHA-256 hex digest length.
