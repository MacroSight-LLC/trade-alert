"""Unit tests for alerts table partitioning prep (FU-007)."""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def _read(rel_path: str) -> str:
    return (_ROOT / rel_path).read_text(encoding="utf-8")


class TestSchemaPartitioning:
    def test_enable_partitioning_sql_exists(self) -> None:
        path = _ROOT / "scripts" / "enable_partitioning.sql"
        assert path.is_file()

    def test_enable_partitioning_has_pg_partman_setup(self) -> None:
        text = _read("scripts/enable_partitioning.sql")
        assert "PARTITION BY RANGE (created_at)" in text
        assert "CREATE EXTENSION IF NOT EXISTS pg_partman" in text
        assert "partman.create_parent('public.alerts', 'created_at', 'native', 'monthly')" in text

    def test_enable_partitioning_has_migration_steps(self) -> None:
        text = _read("scripts/enable_partitioning.sql")
        assert "CREATE TABLE alerts_partitioned" in text
        assert "INSERT INTO alerts_partitioned SELECT * FROM alerts" in text
        assert "ALTER TABLE alerts RENAME TO alerts_old" in text
        assert "CREATE TABLE alerts_default PARTITION OF alerts_partitioned DEFAULT" in text

    def test_schema_sql_documents_partitioning_recipe(self) -> None:
        text = _read("schema.sql")
        assert "Partitioning prep" in text
        assert "pg_partman" in text
        assert "partman.create_parent" in text

    def test_purge_old_data_has_partitioned_table_note(self) -> None:
        text = _read("scripts/purge_old_data.py")
        assert "PARTITIONED TABLE NOTE" in text
        assert "enable_partitioning.sql" in text
