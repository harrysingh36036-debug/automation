"""Data integrity verification.

Provides the verification layer that runs after every MongoDB upsert
and before every Supabase deletion.  The philosophy is:

    If there is ANY doubt → keep the Supabase record.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from . import mongodb_client
from .logger import get_logger, log_error


class VerificationResult:
    """Outcome of a verification check."""

    def __init__(
        self,
        verified: bool,
        reason: str = "",
        details: Optional[Dict[str, Any]] = None,
    ):
        self.verified = verified
        self.reason = reason
        self.details = details or {}

    def __bool__(self) -> bool:
        return self.verified

    def __repr__(self) -> str:
        status = "VERIFIED" if self.verified else "FAILED"
        return f"VerificationResult({status}: {self.reason})"


def verify_migration(
    *,
    collection_name: str,
    source_table: str,
    source_id: str,
    original_record: Dict[str, Any],
) -> VerificationResult:
    """Run all verification checks for a migrated record.

    Checks performed:
    1. Document exists in MongoDB
    2. source_id matches
    3. source_table matches
    4. SHA-256 record hash matches
    """
    # 1. Core existence + hash verification via MongoDB client.
    is_valid = mongodb_client.verify_document(
        collection_name=collection_name,
        source_table=source_table,
        source_id=str(source_id),
        original_record=original_record,
    )

    if not is_valid:
        return VerificationResult(
            verified=False,
            reason="Document not found or hash mismatch in MongoDB",
            details={
                "collection": collection_name,
                "source_table": source_table,
                "source_id": str(source_id),
            },
        )

    return VerificationResult(
        verified=True,
        reason="Document verified in MongoDB",
        details={
            "collection": collection_name,
            "source_table": source_table,
            "source_id": str(source_id),
        },
    )
