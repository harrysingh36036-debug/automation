"""Supabase capacity monitoring.

Uses a direct PostgreSQL connection to execute:

    SELECT pg_database_size(current_database());

This returns the *database* size in bytes (not storage, not bandwidth).
The percentage is then computed as:

    usage_pct = (db_size_bytes / max_size_bytes) * 100

Supabase does NOT expose a "percentage used" via its public API.  The
database size is the authoritative metric and must be queried through
Postgres itself.

Tier reference (recommended max DB size from Supabase docs):
  Nano  (free)  ->  500 MB
  Micro         ->   10 GB
  Small         ->   50 GB
  Medium        ->  100 GB
  Large         ->  200 GB
  XL            ->  500 GB
  2XL           ->    1 TB
  4XL           ->    2 TB
  8XL           ->    4 TB
 12XL           ->    6 TB
 16XL           ->   10 TB
"""

from __future__ import annotations

import psycopg2
import psycopg2.extras

from . import config
from .logger import get_logger, log_error


class CapacityError(Exception):
    """Raised when capacity cannot be determined."""


def get_database_size_bytes() -> int:
    """Return the Supabase database size in bytes.

    Connects directly to the Supabase PostgreSQL instance and runs
    ``pg_database_size(current_database())``.

    Raises ``CapacityError`` on connection or query failure.
    """
    if not config.SUPABASE_DB_HOST:
        raise CapacityError(
            "SUPABASE_DB_HOST is not set.  "
            "Provide the direct Postgres connection host from the Supabase dashboard."
        )

    conn_params = {
        "host": config.SUPABASE_DB_HOST,
        "port": config.SUPABASE_DB_PORT,
        "dbname": config.SUPABASE_DB_NAME,
        "user": config.SUPABASE_DB_USER,
        "password": config.SUPABASE_DB_PASSWORD,
        "connect_timeout": 10,
        "sslmode": "require",
    }

    try:
        conn = psycopg2.connect(**conn_params)
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT pg_database_size(current_database()) AS size_bytes")
                row = cur.fetchone()
                return int(row["size_bytes"])
        finally:
            conn.close()
    except Exception as exc:
        log_error("Failed to query database size", exc)
        raise CapacityError(f"Could not determine database size: {exc}") from exc


def get_usage_percentage(db_size_bytes: int | None = None) -> float:
    """Return current usage as a percentage (0-100+).

    If *db_size_bytes* is ``None`` the value is fetched live.
    """
    if db_size_bytes is None:
        db_size_bytes = get_database_size_bytes()
    max_bytes = config.SUPABASE_MAX_DB_SIZE_BYTES
    if max_bytes <= 0:
        raise CapacityError("SUPABASE_MAX_DB_SIZE_BYTES must be > 0")
    return (db_size_bytes / max_bytes) * 100.0


def should_start_migration(usage_pct: float | None = None) -> bool:
    """Return ``True`` when usage >= START_THRESHOLD."""
    if usage_pct is None:
        usage_pct = get_usage_percentage()
    return usage_pct >= config.START_THRESHOLD


def should_stop_migration(usage_pct: float | None = None) -> bool:
    """Return ``True`` when usage <= TARGET_THRESHOLD."""
    if usage_pct is None:
        usage_pct = get_usage_percentage()
    return usage_pct <= config.TARGET_THRESHOLD
