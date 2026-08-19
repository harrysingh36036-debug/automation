"""Logging module.

Provides structured logging for the migration pipeline.  All log output
goes to stdout (captured by GitHub Actions).  Secrets are never logged.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional


_LOGGER_NAME = "supabase_mongodb_migration"


def get_logger() -> logging.Logger:
    """Return the configured application logger."""
    logger = logging.getLogger(_LOGGER_NAME)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s | %(levelname)-8s | %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S%z",
            )
        )
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger


def log_capacity_usage(usage_pct: float, db_size_bytes: int, max_size_bytes: int) -> None:
    """Log the current Supabase capacity."""
    logger = get_logger()
    db_size_mb = db_size_bytes / (1024 * 1024)
    max_size_mb = max_size_bytes / (1024 * 1024)
    logger.info(
        "Supabase capacity: %.1f%% (%.1f MB / %.1f MB)",
        usage_pct,
        db_size_mb,
        max_size_mb,
    )


def log_batch_start(table: str, batch_num: int, record_count: int) -> None:
    """Log the start of a batch migration."""
    logger = get_logger()
    logger.info(
        "Table: %s | Batch: %d | Records selected: %d",
        table,
        batch_num,
        record_count,
    )


def log_batch_result(
    table: str,
    batch_num: int,
    migrated: int,
    verified: int,
    failed: int,
    deleted: int,
) -> None:
    """Log the result of a batch migration."""
    logger = get_logger()
    logger.info(
        "Table: %s | Batch: %d | Migrated: %d | Verified: %d | Failed: %d | Deleted: %d",
        table,
        batch_num,
        migrated,
        verified,
        failed,
        deleted,
    )


def log_capacity_recheck(usage_pct: float) -> None:
    """Log a capacity re-check after a migration cycle."""
    logger = get_logger()
    logger.info("Current capacity: %.1f%%", usage_pct)


def log_migration_start() -> None:
    """Log that migration has been triggered."""
    logger = get_logger()
    logger.info("=== Migration triggered (capacity >= threshold) ===")


def log_migration_stop(reason: str) -> None:
    """Log that migration has stopped."""
    logger = get_logger()
    logger.info("=== Migration stopped: %s ===", reason)


def log_dry_run() -> None:
    """Log that dry-run mode is active."""
    logger = get_logger()
    logger.warning("DRY RUN — NO DATA DELETED")


def log_migration_disabled() -> None:
    """Log that migration is disabled."""
    logger = get_logger()
    logger.warning("MIGRATION DISABLED — no destructive operations")


def log_error(context: str, error: Exception) -> None:
    """Log an error with context."""
    logger = get_logger()
    logger.error("%s: %s", context, str(error))


def log_warning(context: str, message: str) -> None:
    """Log a warning with context."""
    logger = get_logger()
    logger.warning("%s: %s", context, message)


def now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def create_migration_log_entry(
    *,
    execution_id: str,
    source_table: str,
    source_id: str,
    destination_collection: str,
    mongodb_status: str,
    supabase_delete_status: str,
    ai_status: str = "skipped",
    error: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a structured migration log entry for MongoDB."""
    return {
        "execution_id": execution_id,
        "timestamp": now_iso(),
        "source_system": "supabase",
        "source_table": source_table,
        "source_id": str(source_id),
        "destination_collection": destination_collection,
        "mongodb_status": mongodb_status,
        "supabase_delete_status": supabase_delete_status,
        "ai_status": ai_status,
        "error": error,
    }
