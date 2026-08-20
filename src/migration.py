"""Core migration engine.

Orchestrates the full migration lifecycle:

    fetch batch → build documents → upsert → verify → delete → log

Every step is idempotent and failure-safe.
"""

from __future__ import annotations

import time
import uuid
from typing import Dict, List, Optional, Tuple

from . import config
from . import mongodb_client
from . import supabase_client
from .capacity import get_database_size_bytes, get_usage_percentage, should_stop_migration
from .logger import (
    get_logger,
    log_batch_result,
    log_batch_start,
    log_capacity_recheck,
    log_error,
    log_warning,
    now_iso,
    create_migration_log_entry,
)
from .verification import verify_migration


class MigrationError(Exception):
    """Non-recoverable migration error."""


# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------

def _retry(fn, *args, max_retries: int | None = None, context: str = "", **kwargs):
    """Call *fn* with retries and exponential backoff.

    Returns the function result on success, or raises the last exception.
    """
    if max_retries is None:
        max_retries = config.MAX_RETRIES

    last_exc: Optional[Exception] = None
    for attempt in range(1, max_retries + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            if attempt < max_retries:
                delay = config.RETRY_DELAY_SECONDS * (2 ** (attempt - 1))
                log_warning(
                    context,
                    f"Attempt {attempt}/{max_retries} failed, retrying in {delay}s: {exc}",
                )
                time.sleep(delay)
            else:
                log_error(
                    context,
                    Exception(f"All {max_retries} attempts failed: {exc}"),
                )
    raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Batch processor
# ---------------------------------------------------------------------------

def process_batch(
    table_config: "config.TableConfig",
    execution_id: str,
    batch_number: int,
    last_pk=None,
) -> Tuple[int, int, int, int, Optional]:
    """Process a single batch for one table.

    Returns ``(migrated, verified, failed, deleted, new_last_pk)``.
    """
    logger = get_logger()

    # 1. Fetch batch from Supabase.
    records = supabase_client.fetch_batch(
        table_name=table_config.supabase_table,
        primary_key=table_config.primary_key,
        sort_column=table_config.sort_column,
        batch_size=config.BATCH_SIZE,
        last_pk=last_pk,
        where_clause=table_config.where_clause,
    )

    if not records:
        return (0, 0, 0, 0, last_pk)

    log_batch_start(table_config.supabase_table, batch_number, len(records))

    # Ensure MongoDB indexes exist.
    mongodb_client.ensure_indexes(table_config.mongodb_collection)

    migrated = 0
    verified = 0
    failed = 0
    deleted = 0
    new_last_pk = last_pk

    for record in records:
        source_id = str(record.get(table_config.primary_key, ""))

        try:
            # 2. Build MongoDB document.
            doc = mongodb_client.build_mongo_document(
                record,
                source_table=table_config.supabase_table,
                primary_key=table_config.primary_key,
            )

            # 2b. Copy-only tables (delete_from_source=False): skip the write
            #     when an identical, verified copy already exists.  Reference
            #     tables like stores/brands are tiny but re-written every run,
            #     so this keeps each run lean.
            if not table_config.delete_from_source:
                existing = mongodb_client.find_document(
                    table_config.mongodb_collection,
                    table_config.supabase_table,
                    source_id,
                )
                if existing is not None:
                    existing_hash = existing.get("_migration", {}).get("record_hash", "")
                    if existing_hash == doc["_migration"]["record_hash"]:
                        migrated += 1
                        verified += 1
                        _write_log(
                            execution_id=execution_id,
                            source_table=table_config.supabase_table,
                            source_id=source_id,
                            destination_collection=table_config.mongodb_collection,
                            mongodb_status="verified",
                            supabase_delete_status="kept_copy_only",
                        )
                        new_last_pk = record.get(table_config.primary_key)
                        continue

            # 3. MongoDB upsert (idempotent).
            upsert_ok = _retry(
                mongodb_client.upsert_document,
                table_config.mongodb_collection,
                doc,
                table_config.supabase_table,
                table_config.primary_key,
                context=f"upsert {table_config.supabase_table}:{source_id}",
            )
            if not upsert_ok:
                failed += 1
                _write_log(
                    execution_id=execution_id,
                    source_table=table_config.supabase_table,
                    source_id=source_id,
                    destination_collection=table_config.mongodb_collection,
                    mongodb_status="upsert_failed",
                    supabase_delete_status="skipped",
                )
                continue

            migrated += 1

            # 4. Verify MongoDB document.
            vr = verify_migration(
                collection_name=table_config.mongodb_collection,
                source_table=table_config.supabase_table,
                source_id=source_id,
                original_record=record,
            )

            if not vr.verified:
                failed += 1
                _write_log(
                    execution_id=execution_id,
                    source_table=table_config.supabase_table,
                    source_id=source_id,
                    destination_collection=table_config.mongodb_collection,
                    mongodb_status="verification_failed",
                    supabase_delete_status="skipped",
                    error=vr.reason,
                )
                continue

            verified += 1

            # 5. DRY-RUN guard.
            if config.DRY_RUN:
                _write_log(
                    execution_id=execution_id,
                    source_table=table_config.supabase_table,
                    source_id=source_id,
                    destination_collection=table_config.mongodb_collection,
                    mongodb_status="verified",
                    supabase_delete_status="dry_run_skipped",
                )
                continue

            # 5b. Copy-only tables: never delete from Supabase.  The app keeps
            #     reading these live; MongoDB holds a mirror for joins.
            if not table_config.delete_from_source:
                _write_log(
                    execution_id=execution_id,
                    source_table=table_config.supabase_table,
                    source_id=source_id,
                    destination_collection=table_config.mongodb_collection,
                    mongodb_status="verified",
                    supabase_delete_status="kept_copy_only",
                )
                new_last_pk = record.get(table_config.primary_key)
                continue

            # 6. Delete from Supabase.
            try:
                del_ok = _retry(
                    supabase_client.delete_record,
                    table_config.supabase_table,
                    table_config.primary_key,
                    record[table_config.primary_key],
                    context=f"delete {table_config.supabase_table}:{source_id}",
                )
                if del_ok:
                    deleted += 1
                    _write_log(
                        execution_id=execution_id,
                        source_table=table_config.supabase_table,
                        source_id=source_id,
                        destination_collection=table_config.mongodb_collection,
                        mongodb_status="verified",
                        supabase_delete_status="deleted",
                    )
                else:
                    # Record didn't exist (already deleted on a previous run).
                    deleted += 1
                    _write_log(
                        execution_id=execution_id,
                        source_table=table_config.supabase_table,
                        source_id=source_id,
                        destination_collection=table_config.mongodb_collection,
                        mongodb_status="verified",
                        supabase_delete_status="already_deleted",
                    )
            except Exception as exc:
                # Supabase delete failed — MongoDB document stays.
                # Next run will upsert (idempotent) and retry deletion.
                failed += 1
                _write_log(
                    execution_id=execution_id,
                    source_table=table_config.supabase_table,
                    source_id=source_id,
                    destination_collection=table_config.mongodb_collection,
                    mongodb_status="verified",
                    supabase_delete_status="delete_failed",
                    error=str(exc),
                )

            new_last_pk = record.get(table_config.primary_key)

        except Exception as exc:
            failed += 1
            log_error(
                f"Record {source_id} in {table_config.supabase_table}",
                exc,
            )
            _write_log(
                execution_id=execution_id,
                source_table=table_config.supabase_table,
                source_id=source_id,
                destination_collection=table_config.mongodb_collection,
                mongodb_status="error",
                supabase_delete_status="skipped",
                error=str(exc),
            )

    log_batch_result(
        table_config.supabase_table,
        batch_number,
        migrated,
        verified,
        failed,
        deleted,
    )

    return (migrated, verified, failed, deleted, new_last_pk)


def _write_log(
    *,
    execution_id: str,
    source_table: str,
    source_id: str,
    destination_collection: str,
    mongodb_status: str,
    supabase_delete_status: str,
    ai_status: str = "skipped",
    error: Optional[str] = None,
) -> None:
    """Write a single migration log entry to MongoDB."""
    entry = create_migration_log_entry(
        execution_id=execution_id,
        source_table=source_table,
        source_id=source_id,
        destination_collection=destination_collection,
        mongodb_status=mongodb_status,
        supabase_delete_status=supabase_delete_status,
        ai_status=ai_status,
        error=error,
    )
    mongodb_client.write_migration_log(entry)


# ---------------------------------------------------------------------------
# Full table migration loop
# ---------------------------------------------------------------------------

def migrate_table(
    table_config: "config.TableConfig",
    execution_id: str,
) -> Dict[str, int]:
    """Migrate records from one Supabase table until batch is empty.

    Returns a summary dict with totals.
    """
    summary = {"migrated": 0, "verified": 0, "failed": 0, "deleted": 0}
    batch_num = 0
    last_pk = None

    while True:
        batch_num += 1
        migrated, verified, failed, deleted, new_last_pk = process_batch(
            table_config,
            execution_id,
            batch_num,
            last_pk=last_pk,
        )

        summary["migrated"] += migrated
        summary["verified"] += verified
        summary["failed"] += failed
        summary["deleted"] += deleted

        # If no records were returned, this table is done.
        if migrated + failed == 0:
            break

        # If the cursor didn't advance, stop to avoid an infinite loop.
        if new_last_pk == last_pk:
            break

        last_pk = new_last_pk

    return summary


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_migration() -> None:
    """Run the full migration cycle across all enabled tables.

    Flow:
    1. Check capacity → exit if below START_THRESHOLD.
    2. Loop through tables and migrate batches.
    3. Re-check capacity after each table.
    4. Stop when capacity <= TARGET_THRESHOLD.
    """
    logger = get_logger()
    execution_id = str(uuid.uuid4())

    # --- Emergency stop check ---
    if not config.MIGRATION_ENABLED:
        from .logger import log_migration_disabled
        log_migration_disabled()
        return

    # --- Dry-run notice ---
    if config.DRY_RUN:
        from .logger import log_dry_run
        log_dry_run()

    # --- Initial capacity check ---
    try:
        db_size = get_database_size_bytes()
        usage_pct = get_usage_percentage(db_size)
    except Exception as exc:
        log_error("Could not determine initial capacity", exc)
        return

    from .logger import log_capacity_usage, log_migration_start, log_migration_stop

    log_capacity_usage(usage_pct, db_size, config.SUPABASE_MAX_DB_SIZE_BYTES)

    if not config.DRY_RUN and usage_pct < config.START_THRESHOLD:
        logger.info(
            "Usage %.1f%% is below start threshold %.1f%% — nothing to do.",
            usage_pct,
            config.START_THRESHOLD,
        )
        return

    # --- Destination capacity guard ---
    # Never delete from Supabase if MongoDB itself is nearly full.  This keeps
    # the free-tier promise "a record is never deleted before it is safe".
    if config.MONGODB_MAX_SIZE_BYTES > 0:
        try:
            dest_pct = mongodb_client.get_destination_capacity_percentage(config.MONGODB_MAX_SIZE_BYTES)
            logger.info("MongoDB destination usage: %.1f%%", dest_pct)
            if not config.DRY_RUN and dest_pct >= config.DESTINATION_SAFE_PCT:
                log_migration_stop(
                    f"MongoDB {dest_pct:.1f}% >= safe {config.DESTINATION_SAFE_PCT}% — "
                    "destination full, preserving Supabase records"
                )
                return
        except Exception as exc:
            log_error("Destination capacity check failed", exc)

    log_migration_start()

    # --- Migration loop ---
    tables = config.get_enabled_tables()
    if not tables:
        logger.warning("No enabled tables in config — nothing to migrate.")
        return

    for table_cfg in tables:
        # Check capacity before each table.
        try:
            current_db_size = get_database_size_bytes()
            current_pct = get_usage_percentage(current_db_size)
        except Exception as exc:
            log_error("Capacity check failed", exc)
            break

        if not config.DRY_RUN and should_stop_migration(current_pct):
            log_migration_stop(f"Capacity {current_pct:.1f}% <= target {config.TARGET_THRESHOLD}%")
            return

        logger.info("--- Processing table: %s ---", table_cfg.supabase_table)
        summary = migrate_table(table_cfg, execution_id)
        logger.info(
            "Table %s complete: migrated=%d verified=%d failed=%d deleted=%d",
            table_cfg.supabase_table,
            summary["migrated"],
            summary["verified"],
            summary["failed"],
            summary["deleted"],
        )

    # --- Final capacity re-check ---
    try:
        final_db_size = get_database_size_bytes()
        final_pct = get_usage_percentage(final_db_size)
        log_capacity_recheck(final_pct)
        log_capacity_usage(final_pct, final_db_size, config.SUPABASE_MAX_DB_SIZE_BYTES)
    except Exception as exc:
        log_error("Final capacity check failed", exc)

    log_migration_stop("All tables processed")
