"""Configuration module.

Reads all settings from environment variables and the table configuration
file.  Every value that could leak credentials is excluded from logs.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List


# ---------------------------------------------------------------------------
# Thresholds & sizing
# ---------------------------------------------------------------------------

START_THRESHOLD: float = float(os.getenv("START_THRESHOLD", "90"))
TARGET_THRESHOLD: float = float(os.getenv("TARGET_THRESHOLD", "50"))
BATCH_SIZE: int = int(os.getenv("BATCH_SIZE", "500"))
MAX_RETRIES: int = int(os.getenv("MAX_RETRIES", "3"))
RETRY_DELAY_SECONDS: int = int(os.getenv("RETRY_DELAY_SECONDS", "10"))

# ---------------------------------------------------------------------------
# Feature flags
# ---------------------------------------------------------------------------

MIGRATION_ENABLED: bool = os.getenv("MIGRATION_ENABLED", "true").lower() == "true"
DRY_RUN: bool = os.getenv("DRY_RUN", "false").lower() == "true"
AI_ENABLED: bool = os.getenv("AI_ENABLED", "false").lower() == "true"

# ---------------------------------------------------------------------------
# Supabase capacity limits (bytes)
# ---------------------------------------------------------------------------
# Supabase exposes database size via pg_database_size().  The hard limit
# depends on the compute tier.  Users set the maximum here so the system
# can compute a usage *percentage*.  Defaults match the Free plan (500 MB).
#
# TIER REFERENCE (from Supabase docs, Aug 2024):
#   Nano  (free)  ->  500 MB   recommended max DB size
#   Micro         ->   10 GB
#   Small         ->   50 GB
#   Medium        ->  100 GB
#   Large         ->  200 GB
#   XL            ->  500 GB
#   2XL           ->    1 TB
#   4XL           ->    2 TB
#   8XL           ->    4 TB
#  12XL           ->    6 TB
#  16XL           ->   10 TB
# ---------------------------------------------------------------------------

SUPABASE_MAX_DB_SIZE_BYTES: int = int(
    os.getenv("SUPABASE_MAX_DB_SIZE_BYTES", str(500 * 1024 * 1024))  # 500 MB
)


# ---------------------------------------------------------------------------
# Supabase connection
# ---------------------------------------------------------------------------

SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY: str = os.getenv("SUPABASE_KEY", "")

# For direct PostgreSQL connection (required for pg_database_size).
# Supabase projects expose a direct Postgres connection string.
SUPABASE_DB_HOST: str = os.getenv("SUPABASE_DB_HOST", "")
SUPABASE_DB_PORT: str = os.getenv("SUPABASE_DB_PORT", "5432")
SUPABASE_DB_NAME: str = os.getenv("SUPABASE_DB_NAME", "postgres")
SUPABASE_DB_USER: str = os.getenv("SUPABASE_DB_USER", "postgres")
SUPABASE_DB_PASSWORD: str = os.getenv("SUPABASE_DB_PASSWORD", "")

# ---------------------------------------------------------------------------
# MongoDB connection
# ---------------------------------------------------------------------------

MONGODB_URI: str = os.getenv("MONGODB_URI", "")
MONGODB_DATABASE: str = os.getenv("MONGODB_DATABASE", "")

# Optional destination capacity guard: when set (>0), migration refuses to
# delete anything from Supabase once MongoDB usage reaches DESTINATION_SAFE_PCT
# (default 95) of this size.  MongoDB Atlas M0 free tier = 512 MB.
MONGODB_MAX_SIZE_BYTES: int = int(os.getenv("MONGODB_MAX_SIZE_BYTES", "0"))
DESTINATION_SAFE_PCT: float = float(os.getenv("DESTINATION_SAFE_PCT", "95"))

# ---------------------------------------------------------------------------
# Optional AI
# ---------------------------------------------------------------------------

AI_PROVIDER: str = os.getenv("AI_PROVIDER", "")
AI_API_KEY: str = os.getenv("AI_API_KEY", "")
AI_MODEL: str = os.getenv("AI_MODEL", "")


# ---------------------------------------------------------------------------
# Table configuration
# ---------------------------------------------------------------------------

@dataclass
class TableConfig:
    """Single table mapping between Supabase and MongoDB.

    ``where_clause`` is a raw SQL condition applied to the SELECT (and only the
    SELECT — deletes always happen by primary key).  It lets a config migrate
    only a subset of a table, e.g. ``status = 'Sold'`` for laptops.

    ``delete_from_source`` controls whether the record is removed from Supabase
    after it has been verified in MongoDB.  Set to ``False`` for small reference
    tables (stores, brands, vendors, customers) that the app must keep live:
    they are mirrored to MongoDB for the read API to join against, but never
    deleted from Supabase.
    """
    supabase_table: str
    mongodb_collection: str
    primary_key: str = "id"
    sort_column: str = "created_at"
    where_clause: Optional[str] = None
    delete_from_source: bool = True
    enabled: bool = True


CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def load_table_configs(config_path: Path | None = None) -> List[TableConfig]:
    """Load table mapping from config/tables.json."""
    path = config_path or CONFIG_DIR / "tables.json"
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    tables: List[TableConfig] = []
    for entry in data.get("tables", []):
        tables.append(
            TableConfig(
                supabase_table=entry["supabase_table"],
                mongodb_collection=entry["mongodb_collection"],
                primary_key=entry.get("primary_key", "id"),
                sort_column=entry.get("sort_column", "created_at"),
                where_clause=entry.get("where_clause"),
                delete_from_source=entry.get("delete_from_source", True),
                enabled=entry.get("enabled", True),
            )
        )
    return tables


def get_enabled_tables(config_path: Path | None = None) -> List[TableConfig]:
    """Return only the enabled table configurations."""
    return [t for t in load_table_configs(config_path) if t.enabled]
