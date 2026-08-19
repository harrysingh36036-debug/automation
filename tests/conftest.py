"""Shared pytest fixtures."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure the src package is importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# Fake config values
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Reset environment variables before every test."""
    env_vars = {
        "SUPABASE_URL": "",
        "SUPABASE_KEY": "",
        "SUPABASE_DB_HOST": "",
        "SUPABASE_DB_PORT": "5432",
        "SUPABASE_DB_NAME": "postgres",
        "SUPABASE_DB_USER": "postgres",
        "SUPABASE_DB_PASSWORD": "",
        "SUPABASE_MAX_DB_SIZE_BYTES": str(500 * 1024 * 1024),  # 500 MB
        "MONGODB_URI": "",
        "MONGODB_DATABASE": "",
        "START_THRESHOLD": "90",
        "TARGET_THRESHOLD": "50",
        "BATCH_SIZE": "500",
        "MAX_RETRIES": "3",
        "RETRY_DELAY_SECONDS": "0",  # No delay in tests.
        "MIGRATION_ENABLED": "true",
        "DRY_RUN": "false",
        "AI_ENABLED": "false",
        "AI_PROVIDER": "",
        "AI_API_KEY": "",
        "AI_MODEL": "",
    }
    for key, val in env_vars.items():
        monkeypatch.setenv(key, val)

    # Force re-import of config module so it picks up new env vars.
    import importlib
    import src.config as cfg
    importlib.reload(cfg)


@pytest.fixture
def sample_record():
    """A representative Supabase row."""
    return {
        "id": "123",
        "product": "Laptop",
        "quantity": 10,
        "created_at": "2024-01-15T10:30:00Z",
    }


@pytest.fixture
def sample_records():
    """A list of Supabase rows."""
    return [
        {"id": str(i), "product": f"Item-{i}", "quantity": i, "created_at": f"2024-01-{i:02d}T10:00:00Z"}
        for i in range(1, 6)
    ]


@pytest.fixture
def table_config_path(tmp_path):
    """Create a temporary tables.json and return its path."""
    config = {
        "tables": [
            {
                "supabase_table": "inventory",
                "mongodb_collection": "inventory",
                "primary_key": "id",
                "sort_column": "created_at",
                "enabled": True,
            },
            {
                "supabase_table": "orders",
                "mongodb_collection": "orders",
                "primary_key": "id",
                "sort_column": "created_at",
                "enabled": False,
            },
        ]
    }
    path = tmp_path / "tables.json"
    path.write_text(json.dumps(config))
    return path
