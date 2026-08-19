"""Supabase data-access layer.

Uses a direct PostgreSQL connection (via ``psycopg2``) so that the system
can perform ``SELECT … ORDER BY … LIMIT`` and ``DELETE WHERE id = …``
operations reliably.  The Supabase REST API does not support arbitrary
SQL and would require multiple round-trips for cursor-based pagination.

Table access is restricted to the tables listed in ``config/tables.json``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import psycopg2
import psycopg2.extras

from . import config
from .logger import get_logger, log_error


class SupabaseClientError(Exception):
    """Raised on Supabase connection or query errors."""


def _get_connection():
    """Create a new PostgreSQL connection to the Supabase database."""
    if not config.SUPABASE_DB_HOST:
        raise SupabaseClientError(
            "SUPABASE_DB_HOST is not set."
        )
    return psycopg2.connect(
        host=config.SUPABASE_DB_HOST,
        port=config.SUPABASE_DB_PORT,
        dbname=config.SUPABASE_DB_NAME,
        user=config.SUPABASE_DB_USER,
        password=config.SUPABASE_DB_PASSWORD,
        connect_timeout=10,
        sslmode="require",
    )


def fetch_batch(
    table_name: str,
    primary_key: str = "id",
    sort_column: str = "created_at",
    batch_size: int | None = None,
    last_pk: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """Fetch the next batch of records from a Supabase table.

    Records are ordered by ``sort_column ASC`` (oldest first) with
    cursor-based pagination using the primary key.

    Parameters
    ----------
    table_name:
        The PostgreSQL table name.
    primary_key:
        Column used for ordering and cursor pagination.
    sort_column:
        Column to sort by (oldest first).
    batch_size:
        Number of records to fetch (defaults to ``config.BATCH_SIZE``).
    last_pk:
        The primary key value of the last record from the previous batch.
        If ``None`` the fetch starts from the beginning.

    Returns
    -------
    List of dicts, each representing a row.
    """
    if batch_size is None:
        batch_size = config.BATCH_SIZE

    # Sanitize identifiers (allow only alphanumeric + underscore).
    for identifier in (table_name, primary_key, sort_column):
        if not identifier.replace("_", "").isalnum():
            raise SupabaseClientError(f"Invalid identifier: {identifier}")

    query = (
        f'SELECT * FROM "{table_name}" '
        f'ORDER BY "{sort_column}" ASC, "{primary_key}" ASC '
        f"LIMIT %s"
    )
    params: list = [batch_size]

    if last_pk is not None:
        query = (
            f'SELECT * FROM "{table_name}" '
            f'WHERE ("{sort_column}" > (SELECT "{sort_column}" FROM "{table_name}" WHERE "{primary_key}" = %s)) '
            f'OR ("{sort_column}" = (SELECT "{sort_column}" FROM "{table_name}" WHERE "{primary_key}" = %s) '
            f'AND "{primary_key}" > %s) '
            f'ORDER BY "{sort_column}" ASC, "{primary_key}" ASC '
            f'LIMIT %s'
        )
        params = [last_pk, last_pk, last_pk, batch_size]

    conn = _get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
            # Convert RealDictRow to plain dicts and ensure all values are serialisable.
            return [dict(row) for row in rows]
    except Exception as exc:
        log_error(f"Failed to fetch batch from {table_name}", exc)
        raise SupabaseClientError(str(exc)) from exc
    finally:
        conn.close()


def delete_record(table_name: str, primary_key: str, record_id: Any) -> bool:
    """Delete a single record by primary key.

    Returns ``True`` if a row was actually deleted.
    """
    for identifier in (table_name, primary_key):
        if not identifier.replace("_", "").isalnum():
            raise SupabaseClientError(f"Invalid identifier: {identifier}")

    query = f'DELETE FROM "{table_name}" WHERE "{primary_key}" = %s'
    conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(query, (record_id,))
            deleted = cur.rowcount > 0
            conn.commit()
            return deleted
    except Exception as exc:
        conn.rollback()
        log_error(f"Failed to delete record {record_id} from {table_name}", exc)
        raise SupabaseClientError(str(exc)) from exc
    finally:
        conn.close()


def record_exists(table_name: str, primary_key: str, record_id: Any) -> bool:
    """Check whether a record still exists in Supabase."""
    for identifier in (table_name, primary_key):
        if not identifier.replace("_", "").isalnum():
            raise SupabaseClientError(f"Invalid identifier: {identifier}")

    query = f'SELECT 1 FROM "{table_name}" WHERE "{primary_key}" = %s LIMIT 1'
    conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(query, (record_id,))
            return cur.fetchone() is not None
    except Exception as exc:
        log_error(f"Failed to check existence of {record_id} in {table_name}", exc)
        raise SupabaseClientError(str(exc)) from exc
    finally:
        conn.close()
