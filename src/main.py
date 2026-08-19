"""Entry point for the migration pipeline.

Run with:

    python -m src.main

This module is scheduler-agnostic.  It can be invoked by GitHub Actions,
cron, Airflow, or any other orchestrator.
"""

from __future__ import annotations

import sys

from .logger import get_logger, log_error, log_migration_disabled


def main() -> int:
    """Execute the migration pipeline.

    Returns 0 on success (even when no migration was needed) and 1 on
    unrecoverable error.
    """
    logger = get_logger()
    logger.info("Supabase → MongoDB Migration starting …")

    try:
        from . import config

        # --- Emergency stop ---
        if not config.MIGRATION_ENABLED:
            log_migration_disabled()
            logger.info("Migration disabled via MIGRATION_ENABLED=false.")
            return 0

        # --- Import migration engine (may fail if dependencies missing) ---
        from .migration import run_migration

        run_migration()

        logger.info("Migration finished successfully.")
        return 0

    except Exception as exc:
        log_error("Unhandled exception in migration pipeline", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
