"""Tests for table configuration parsing.

Covers the laptop-inventory mapping: where_clause (migrate only Sold laptops)
and delete_from_source (reference tables are mirrored, never deleted).
"""

from __future__ import annotations

import json

import src.config as config
from src.config import load_table_configs


class TestTableConfigParsing:
    def test_parses_where_clause_and_delete_flag(self, tmp_path):
        data = {
            "tables": [
                {
                    "supabase_table": "laptops",
                    "mongodb_collection": "laptops",
                    "primary_key": "id",
                    "sort_column": "created_at",
                    "where_clause": "status = 'Sold'",
                    "delete_from_source": True,
                    "enabled": True,
                },
                {
                    "supabase_table": "stores",
                    "mongodb_collection": "stores",
                    "delete_from_source": False,
                    "enabled": True,
                },
            ]
        }
        path = tmp_path / "tables.json"
        path.write_text(json.dumps(data))
        tables = load_table_configs(path)

        laptops = tables[0]
        assert laptops.where_clause == "status = 'Sold'"
        assert laptops.delete_from_source is True

        stores = tables[1]
        assert stores.where_clause is None
        assert stores.delete_from_source is False

    def test_defaults_when_fields_absent(self, tmp_path):
        data = {
            "tables": [
                {
                    "supabase_table": "sales",
                    "mongodb_collection": "sales",
                }
            ]
        }
        path = tmp_path / "tables.json"
        path.write_text(json.dumps(data))
        tables = load_table_configs(path)
        assert tables[0].where_clause is None
        assert tables[0].delete_from_source is True

    def test_enabled_tables_respect_delete_flag(self, tmp_path):
        data = {
            "tables": [
                {"supabase_table": "laptops", "mongodb_collection": "laptops", "delete_from_source": True, "enabled": True},
                {"supabase_table": "customers", "mongodb_collection": "customers", "delete_from_source": False, "enabled": True},
            ]
        }
        path = tmp_path / "tables.json"
        path.write_text(json.dumps(data))
        tables = config.get_enabled_tables(path)
        assert len(tables) == 2
        assert tables[1].delete_from_source is False