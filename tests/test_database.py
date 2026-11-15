from pathlib import Path

import pytest

import intellipage.database as database_module
from intellipage.database import (
    DatabaseUnavailable,
    _migrate_schema,
    _validate_read_only_query,
    bootstrap_database,
    split_sql_script,
)


class MigrationCursor:
    def __init__(self, *, has_column: bool, indexes: set[str]):
        self.has_column = has_column
        self.indexes = indexes
        self.statements: list[str] = []
        self._query = ""

    def execute(self, statement, params=None):
        self._query = " ".join(statement.split()).lower()
        self.statements.append(self._query)

    def fetchone(self):
        return (1 if self.has_column else 0,)

    def fetchall(self):
        return [(name,) for name in self.indexes]


def test_split_sql_script_preserves_semicolon_in_string():
    statements = split_sql_script("SELECT 'a;b'; SELECT 2;")
    assert statements == ["SELECT 'a;b'", "SELECT 2"]


def test_mysql_views_match_provider_neutral_contract():
    schema = (Path(__file__).resolve().parents[1] / "database" / "schema.sql").read_text(
        encoding="utf-8"
    ).upper()
    assert "AVG(REWARD) AS AVG_REWARD" in schema
    assert "WHERE RECENCY_RANK = 1" in schema
    sample = (
        Path(__file__).resolve().parents[1] / "samples" / "mysql_read_workload.sql"
    ).read_text(encoding="utf-8").upper()
    assert "ORDER BY AVG_REWARD DESC" in sample
    assert "AVERAGE_REWARD" not in sample


def test_only_read_queries_are_allowed_for_warming():
    _validate_read_only_query("SELECT * FROM process_samples LIMIT 1")
    _validate_read_only_query("WITH x AS (SELECT 1) SELECT * FROM x")
    with pytest.raises(ValueError):
        _validate_read_only_query("DELETE FROM process_samples")
    with pytest.raises(ValueError):
        _validate_read_only_query("SELECT 1; DROP TABLE processes")
    with pytest.raises(ValueError):
        _validate_read_only_query("WITH x AS (SELECT 1) DELETE FROM processes")
    with pytest.raises(ValueError):
        _validate_read_only_query("SELECT * FROM processes INTO OUTFILE 'x.txt'")
    _validate_read_only_query("SELECT 'drop table processes' AS harmless_text")
    _validate_read_only_query("SELECT 'a;b' AS value_with_semicolon;")


def test_schema_migration_upgrades_previous_process_table():
    cursor = MigrationCursor(has_column=False, indexes={"uq_process_pid_name"})
    _migrate_schema(cursor, "intellipage")
    combined = "\n".join(cursor.statements)
    assert "add column process_started_at" in combined
    assert "drop index uq_process_pid_name" in combined
    assert "add unique key uq_process_instance" in combined
    assert "add index idx_process_pid_last_seen" in combined


def test_schema_migration_is_idempotent_for_current_schema():
    cursor = MigrationCursor(
        has_column=True,
        indexes={"uq_process_instance", "idx_process_pid_last_seen"},
    )
    _migrate_schema(cursor, "intellipage")
    assert not any("alter table" in statement for statement in cursor.statements)


def test_mysql_bootstrap_translates_connector_failure(monkeypatch, tmp_path):
    class ConnectorError(Exception):
        pass

    class FailedConnector:
        Error = ConnectorError

        @staticmethod
        def connect(**_kwargs):
            raise ConnectorError("connection refused")

    monkeypatch.setattr(database_module, "_connector_module", lambda: FailedConnector)
    with pytest.raises(DatabaseUnavailable, match="connection refused"):
        bootstrap_database(
            host="127.0.0.1",
            port=3306,
            admin_user="root",
            admin_password="secret",
            database="intellipage",
            app_user="intellipage_app",
            app_password="runtime-secret",
            schema_path=tmp_path / "unused.sql",
        )

