"""MongoDB data-access layer.

Handles upserts, verification queries, and destination capacity checks.
Uses ``pymongo`` (the official MongoDB Python driver).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from pymongo import MongoClient, ASCENDING
from pymongo.errors import ConnectionFailure, PyMongoError

from . import config
from .logger import get_logger, log_error


# ---------------------------------------------------------------------------
# Client singleton (created once per process)
# ---------------------------------------------------------------------------

_client: Optional[MongoClient] = None
_db = None


def _get_db():
    """Return (and lazily create) the MongoDB database handle."""
    global _client, _db
    if _db is not None:
        return _db
    if not config.MONGODB_URI:
        raise RuntimeError("MONGODB_URI is not set.")
    if not config.MONGODB_DATABASE:
        raise RuntimeError("MONGODB_DATABASE is not set.")
    _client = MongoClient(config.MONGODB_URI, serverSelectionTimeoutMS=10_000)
    _db = _client[config.MONGODB_DATABASE]
    return _db


# ---------------------------------------------------------------------------
# Index management
# ---------------------------------------------------------------------------

def ensure_indexes(collection_name: str) -> None:
    """Create a unique index on migration identity fields.

    The compound key ``(source_system, source_table, source_id)``
    prevents duplicate documents from retries.
    """
    db = _get_db()
    coll = db[collection_name]
    coll.create_index(
        [
            ("_migration.source_system", ASCENDING),
            ("_migration.source_table", ASCENDING),
            ("_migration.source_id", ASCENDING),
        ],
        unique=True,
        background=True,
    )


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------

def build_mongo_document(
    record: Dict[str, Any],
    *,
    source_table: str,
    primary_key: str,
) -> Dict[str, Any]:
    """Transform a Supabase row into a MongoDB document.

    Business fields are preserved.  Migration metadata is stored under
    the ``_migration`` key.
    """
    doc = dict(record)
    source_id = str(doc.get(primary_key, ""))

    doc["_migration"] = {
        "source_system": "supabase",
        "source_table": source_table,
        "source_id": source_id,
        "migrated_at": datetime.now(timezone.utc).isoformat(),
    }

    # Store a deterministic hash of the original record for integrity checks.
    doc["_migration"]["record_hash"] = compute_record_hash(record)

    return doc


def compute_record_hash(record: Dict[str, Any]) -> str:
    """Compute a SHA-256 hash of a canonical JSON representation."""
    # Remove any existing migration metadata to get the original record.
    clean = {k: v for k, v in record.items() if not k.startswith("_")}
    canonical = json.dumps(clean, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def upsert_document(
    collection_name: str,
    document: Dict[str, Any],
    source_table: str,
    primary_key: str,
) -> bool:
    """Upsert a document into the target MongoDB collection.

    Returns ``True`` on success.
    """
    db = _get_db()
    coll = db[collection_name]

    source_id = str(document.get(primary_key, ""))
    filter_query = {
        "_migration.source_system": "supabase",
        "_migration.source_table": source_table,
        "_migration.source_id": source_id,
    }

    try:
        coll.update_one(filter_query, {"$set": document}, upsert=True)
        return True
    except PyMongoError as exc:
        log_error(f"MongoDB upsert failed for {source_table}:{source_id}", exc)
        return False


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify_document(
    collection_name: str,
    source_table: str,
    source_id: str,
    original_record: Dict[str, Any],
) -> bool:
    """Verify that a document exists in MongoDB and matches the source.

    Checks:
    1. Document exists
    2. source_id matches
    3. source_table matches
    4. record hash matches (data integrity)
    """
    db = _get_db()
    coll = db[collection_name]

    filter_query = {
        "_migration.source_system": "supabase",
        "_migration.source_table": source_table,
        "_migration.source_id": str(source_id),
    }

    try:
        found = coll.find_one(filter_query)
        if found is None:
            return False

        # Verify the record hash.
        expected_hash = compute_record_hash(original_record)
        actual_hash = found.get("_migration", {}).get("record_hash", "")
        return expected_hash == actual_hash
    except PyMongoError as exc:
        log_error(f"MongoDB verification failed for {source_table}:{source_id}", exc)
        return False


# ---------------------------------------------------------------------------
# Destination capacity
# ---------------------------------------------------------------------------

def get_destination_size_bytes() -> int:
    """Return the total data size of the migration database in bytes."""
    db = _get_db()
    try:
        stats = db.command("dbStats")
        return int(stats.get("dataSize", 0))
    except PyMongoError as exc:
        log_error("Failed to get MongoDB database stats", exc)
        return 0


def get_destination_capacity_percentage(max_bytes: int = 0) -> float:
    """Return the destination usage as a percentage.

    If *max_bytes* is 0 or not provided the function returns 0 (unknown).
    The caller should supply the MongoDB Atlas free-tier limit or
    self-hosted capacity.
    """
    if max_bytes <= 0:
        return 0.0
    current = get_destination_size_bytes()
    return (current / max_bytes) * 100.0


# ---------------------------------------------------------------------------
# Logging collection
# ---------------------------------------------------------------------------

def write_migration_log(entry: Dict[str, Any]) -> bool:
    """Write a log entry to the ``migration_logs`` collection."""
    db = _get_db()
    try:
        db["migration_logs"].insert_one(entry)
        return True
    except PyMongoError as exc:
        log_error("Failed to write migration log", exc)
        return False
