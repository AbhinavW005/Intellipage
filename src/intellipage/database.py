from __future__ import annotations

import hashlib
import json
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Protocol, Sequence

from .config import MySQLConfig
from .models import ActionResult, ExperimentResult, ProcessSample, utc_now


class DatabaseUnavailable(RuntimeError):
    pass


class DatabaseRepository(Protocol):
    provider_name: str

    def health_check(self) -> dict[str, Any]: ...

    def store_process_sample(self, sample: ProcessSample) -> None: ...

    def store_system_sample(
        self, summary: dict[str, float | int], sampled_at: datetime | None = None
    ) -> None: ...

    def store_experiment(self, result: ExperimentResult) -> int: ...

    def store_dqn_experience(
        self,
        state: Sequence[float],
        action: int,
        reward: float,
        next_state: Sequence[float],
        done: bool,
    ) -> None: ...

    def fetch_dqn_experiences(self, limit: int = 10_000) -> list[dict[str, Any]]: ...

    def fetch_query_metrics(
        self, query: str, limit: int = 9
    ) -> list[dict[str, Any]]: ...

    def execute_timed_query(
        self,
        statement_name: str,
        query: str,
        params: Sequence[Any] | None = None,
        *,
        explain: bool = True,
    ) -> dict[str, Any]: ...

def _connector_module():
    try:
        import mysql.connector
    except ImportError as exc:
        raise DatabaseUnavailable(
            "mysql-connector-python is not installed. Run: pip install -r requirements.txt"
        ) from exc
    return mysql.connector


class MySQLRepository:
    provider_name = "mysql"

    def __init__(self, config: MySQLConfig):
        self.config = config

    @contextmanager
    def connection(self, *, include_database: bool = True) -> Iterator[Any]:
        connector = _connector_module()
        kwargs: dict[str, Any] = {
            "host": self.config.host,
            "port": self.config.port,
            "user": self.config.user,
            "password": self.config.password,
            "connection_timeout": self.config.connect_timeout,
            "autocommit": False,
        }
        if include_database:
            kwargs["database"] = self.config.database
        try:
            connection = connector.connect(**kwargs)
        except connector.Error as exc:
            raise DatabaseUnavailable(str(exc)) from exc
        try:
            yield connection
        except connector.Error as exc:
            try:
                connection.rollback()
            except connector.Error:
                pass
            raise DatabaseUnavailable(str(exc)) from exc
        finally:
            connection.close()

    def health_check(self) -> dict[str, Any]:
        with self.connection() as connection:
            cursor = connection.cursor(dictionary=True)
            cursor.execute(
                "SELECT VERSION() AS version, DATABASE() AS database_name, NOW(6) AS server_time"
            )
            row = cursor.fetchone()
            cursor.close()
        return dict(row)

    def store_process_sample(self, sample: ProcessSample) -> None:
        with self.connection() as connection:
            cursor = connection.cursor()
            cursor.execute(
                """
                INSERT INTO processes
                    (windows_pid, process_name, process_started_at, first_seen, last_seen)
                VALUES (%s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE process_name = VALUES(process_name),
                                        last_seen = VALUES(last_seen),
                                        process_id = LAST_INSERT_ID(process_id)
                """,
                (
                    sample.pid,
                    sample.name,
                    _mysql_datetime(sample.process_started_at or sample.timestamp),
                    _mysql_datetime(sample.timestamp),
                    _mysql_datetime(sample.timestamp),
                ),
            )
            process_id = cursor.lastrowid
            cursor.execute(
                """
                INSERT INTO process_samples
                    (process_id, sampled_at, page_faults, working_set_bytes,
                     private_bytes, cpu_percent, read_bytes, write_bytes)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    process_id,
                    _mysql_datetime(sample.timestamp),
                    sample.page_faults,
                    sample.working_set_bytes,
                    sample.private_bytes,
                    sample.cpu_percent,
                    sample.read_bytes,
                    sample.write_bytes,
                ),
            )
            connection.commit()
            cursor.close()

    def store_system_sample(
        self,
        summary: dict[str, float | int],
        sampled_at: datetime | None = None,
    ) -> None:
        timestamp = sampled_at or utc_now()
        with self.connection() as connection:
            cursor = connection.cursor()
            cursor.execute(
                """
                INSERT INTO system_samples
                    (sampled_at, memory_total_bytes, memory_available_bytes,
                     memory_percent, swap_used_bytes, swap_percent, process_count)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    _mysql_datetime(timestamp),
                    int(summary["memory_total_bytes"]),
                    int(summary["memory_available_bytes"]),
                    float(summary["memory_percent"]),
                    int(summary["swap_used_bytes"]),
                    float(summary["swap_percent"]),
                    int(summary["process_count"]),
                ),
            )
            connection.commit()
            cursor.close()

    def store_action(self, result: ActionResult, connection: Any | None = None) -> int:
        owns_connection = connection is None
        context = self.connection() if owns_connection else _passthrough(connection)
        with context as active:
            cursor = active.cursor()
            cursor.execute(
                """
                INSERT INTO optimization_actions
                    (action_kind, target, started_at, duration_ms, success,
                     bytes_touched, message, metadata_json)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    result.kind.value,
                    result.target,
                    _mysql_datetime(result.started_at),
                    result.duration_ms,
                    result.success,
                    result.bytes_touched,
                    result.message,
                    json.dumps(result.metadata),
                ),
            )
            action_id = int(cursor.lastrowid)
            if owns_connection:
                active.commit()
            cursor.close()
            return action_id

    def store_experiment(self, result: ExperimentResult) -> int:
        with self.connection() as connection:
            action_id = self.store_action(result.action, connection=connection)
            cursor = connection.cursor()
            cursor.execute(
                """
                INSERT INTO experiments
                    (experiment_type, target, policy_name, action_id,
                     baseline_latency_ms, post_action_latency_ms,
                     page_faults_before, page_faults_after,
                     working_set_before, working_set_after, reward, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    result.experiment_type,
                    result.target,
                    result.policy,
                    action_id,
                    result.baseline_latency_ms,
                    result.post_action_latency_ms,
                    result.page_faults_before,
                    result.page_faults_after,
                    result.working_set_before,
                    result.working_set_after,
                    result.reward,
                    _mysql_datetime(result.created_at),
                ),
            )
            experiment_id = int(cursor.lastrowid)
            connection.commit()
            cursor.close()
            return experiment_id

    def store_dqn_experience(
        self,
        state: Sequence[float],
        action: int,
        reward: float,
        next_state: Sequence[float],
        done: bool,
    ) -> None:
        with self.connection() as connection:
            cursor = connection.cursor()
            cursor.execute(
                """
                INSERT INTO dqn_experiences
                    (state_json, action_index, reward, next_state_json, done)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (json.dumps(list(state)), action, reward, json.dumps(list(next_state)), done),
            )
            connection.commit()
            cursor.close()

    def fetch_dqn_experiences(self, limit: int = 10_000) -> list[dict[str, Any]]:
        safe_limit = min(max(1, int(limit)), 100_000)
        with self.connection() as connection:
            cursor = connection.cursor(dictionary=True)
            cursor.execute(
                """
                SELECT state_json, action_index, reward, next_state_json, done
                FROM dqn_experiences
                ORDER BY experience_id DESC
                LIMIT %s
                """,
                (safe_limit,),
            )
            rows = cursor.fetchall()
            cursor.close()
        return [
            {
                "state": json.loads(row["state_json"]),
                "action": int(row["action_index"]),
                "reward": float(row["reward"]),
                "next_state": json.loads(row["next_state_json"]),
                "done": bool(row["done"]),
            }
            for row in rows
        ]

    def fetch_query_metrics(
        self, query: str, limit: int = 9
    ) -> list[dict[str, Any]]:
        safe_limit = min(max(1, int(limit)), 100)
        digest = hashlib.sha256(query.strip().rstrip(";").encode("utf-8")).hexdigest()
        with self.connection() as connection:
            cursor = connection.cursor(dictionary=True)
            cursor.execute(
                """
                SELECT statement_name, duration_ms, rows_returned, measured_at
                FROM query_metrics
                WHERE query_digest = %s
                ORDER BY query_metric_id DESC
                LIMIT %s
                """,
                (digest, safe_limit),
            )
            rows = cursor.fetchall()
            cursor.close()
        return [
            {
                "statement_name": str(row["statement_name"]),
                "duration_ms": float(row["duration_ms"]),
                "rows_returned": int(row["rows_returned"]),
                "measured_at": row["measured_at"],
            }
            for row in rows
        ]
    def execute_timed_query(
        self,
        statement_name: str,
        query: str,
        params: Sequence[Any] | None = None,
        *,
        explain: bool = True,
    ) -> dict[str, Any]:
        _validate_read_only_query(query)
        explain_text = None
        with self.connection() as connection:
            connection.start_transaction(readonly=True)
            cursor = connection.cursor()
            if explain:
                cursor.execute(f"EXPLAIN ANALYZE {query}", tuple(params or ()))
                explain_text = "\n".join(str(row[0]) for row in cursor.fetchall())
            started = time.perf_counter()
            cursor.execute(query, tuple(params or ()))
            rows_returned = 0
            rows_preview: list[Any] = []
            while True:
                batch = cursor.fetchmany(1_000)
                if not batch:
                    break
                rows_returned += len(batch)
                if len(rows_preview) < 100:
                    rows_preview.extend(batch[: 100 - len(rows_preview)])
            duration_ms = (time.perf_counter() - started) * 1000
            cursor.close()
            connection.rollback()

            digest = hashlib.sha256(query.strip().rstrip(";").rstrip().encode("utf-8")).hexdigest()
            cursor = connection.cursor()
            cursor.execute(
                """
                INSERT INTO query_metrics
                    (query_digest, statement_name, duration_ms, rows_returned, explain_text)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (digest, statement_name, duration_ms, rows_returned, explain_text),
            )
            connection.commit()
            cursor.close()
        return {
            "duration_ms": duration_ms,
            "rows_returned": rows_returned,
            "explain": explain_text,
            "rows": rows_preview,
        }


@contextmanager
def _passthrough(value: Any) -> Iterator[Any]:
    yield value


def _validate_identifier(identifier: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", identifier):
        raise ValueError(f"Unsafe database identifier: {identifier!r}")
    return identifier


def _mysql_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _validate_read_only_query(query: str) -> None:
    without_comments = re.sub(r"/\*.*?\*/|--[^\r\n]*|#[^\r\n]*", " ", query, flags=re.S)
    without_strings = re.sub(r"'(?:''|\\.|[^'])*'|\"(?:\"\"|\\.|[^\"])*\"", "''", without_comments)
    normalized = without_strings.strip().lstrip("(").upper()
    if not (normalized.startswith("SELECT") or normalized.startswith("WITH")):
        raise ValueError("Only SELECT or WITH queries may be used for automatic warming")
    # Ignore semicolons inside quoted values, but reject stacked statements.
    if ";" in without_strings.rstrip().rstrip(";"):
        raise ValueError("Multiple SQL statements are not allowed")
    forbidden = (
        "INSERT",
        "UPDATE",
        "DELETE",
        "REPLACE",
        "ALTER",
        "DROP",
        "CREATE",
        "TRUNCATE",
        "GRANT",
        "REVOKE",
        "CALL",
        "LOAD",
        "INTO",
        "OUTFILE",
        "DUMPFILE",
        "FOR UPDATE",
        "LOCK IN SHARE MODE",
    )
    if any(
        re.search(
            r"\b" + re.escape(phrase).replace(r"\ ", r"\s+") + r"\b",
            normalized,
        )
        for phrase in forbidden
    ):
        raise ValueError("The warming query contains a write-capable SQL clause")


def split_sql_script(text: str) -> list[str]:
    statements: list[str] = []
    buffer: list[str] = []
    in_single = False
    in_double = False
    escaped = False
    for char in text:
        if escaped:
            buffer.append(char)
            escaped = False
            continue
        if char == "\\":
            buffer.append(char)
            escaped = True
            continue
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        if char == ";" and not in_single and not in_double:
            statement = "".join(buffer).strip()
            if statement:
                statements.append(statement)
            buffer = []
        else:
            buffer.append(char)
    trailing = "".join(buffer).strip()
    if trailing:
        statements.append(trailing)
    return statements


def bootstrap_database(
    *,
    host: str,
    port: int,
    admin_user: str,
    admin_password: str,
    database: str,
    app_user: str,
    app_password: str,
    schema_path: str | Path,
) -> None:
    connector = _connector_module()
    database = _validate_identifier(database)
    app_user = _validate_identifier(app_user)
    try:
        connection = connector.connect(
            host=host,
            port=port,
            user=admin_user,
            password=admin_password,
            connection_timeout=10,
            autocommit=False,
        )
    except connector.Error as exc:
        raise DatabaseUnavailable(str(exc)) from exc
    try:
        cursor = connection.cursor()
        cursor.execute(
            f"CREATE DATABASE IF NOT EXISTS `{database}` "
            "CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
        )
        cursor.execute(
            f"CREATE USER IF NOT EXISTS '{app_user}'@'localhost' IDENTIFIED BY %s",
            (app_password,),
        )
        cursor.execute(
            f"ALTER USER '{app_user}'@'localhost' IDENTIFIED BY %s",
            (app_password,),
        )
        cursor.execute(
            f"GRANT SELECT, INSERT, UPDATE, DELETE, EXECUTE, SHOW VIEW "
            f"ON `{database}`.* TO '{app_user}'@'localhost'"
        )
        cursor.execute(f"USE `{database}`")
        script = Path(schema_path).read_text(encoding="utf-8")
        for statement in split_sql_script(script):
            cursor.execute(statement)
            while cursor.nextset():
                pass
        _migrate_schema(cursor, database)
        connection.commit()
        cursor.close()
    except connector.Error as exc:
        connection.rollback()
        raise DatabaseUnavailable(str(exc)) from exc
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _migrate_schema(cursor: Any, database: str) -> None:
    """Upgrade databases created by earlier IntelliPage backend builds."""
    cursor.execute(
        """
        SELECT COUNT(*)
        FROM information_schema.columns
        WHERE table_schema = %s
          AND table_name = 'processes'
          AND column_name = 'process_started_at'
        """,
        (database,),
    )
    if int(cursor.fetchone()[0]) == 0:
        cursor.execute(
            "ALTER TABLE processes "
            "ADD COLUMN process_started_at DATETIME(6) NULL AFTER process_name"
        )
        cursor.execute(
            "UPDATE processes SET process_started_at = first_seen "
            "WHERE process_started_at IS NULL"
        )
        cursor.execute(
            "ALTER TABLE processes "
            "MODIFY COLUMN process_started_at DATETIME(6) NOT NULL"
        )

    cursor.execute(
        """
        SELECT index_name
        FROM information_schema.statistics
        WHERE table_schema = %s AND table_name = 'processes'
        """,
        (database,),
    )
    indexes = {str(row[0]) for row in cursor.fetchall()}
    if "uq_process_pid_name" in indexes:
        cursor.execute("ALTER TABLE processes DROP INDEX uq_process_pid_name")
    if "uq_process_instance" not in indexes:
        cursor.execute(
            "ALTER TABLE processes ADD UNIQUE KEY uq_process_instance "
            "(windows_pid, process_started_at)"
        )
    if "idx_process_pid_last_seen" not in indexes:
        cursor.execute(
            "ALTER TABLE processes ADD INDEX idx_process_pid_last_seen "
            "(windows_pid, last_seen DESC)"
        )




