from __future__ import annotations

import hashlib
import json
import os
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Sequence

from .config import OracleConfig
from .database import (
    DatabaseUnavailable,
    _mysql_datetime,
    _validate_identifier,
    _validate_read_only_query,
    split_sql_script,
)
from .models import ActionResult, ExperimentResult, ProcessSample, utc_now


def _driver_module(config: OracleConfig | None = None):
    try:
        import oracledb
    except ImportError as exc:
        raise DatabaseUnavailable(
            "python-oracledb is not installed. Run: pip install -r requirements.txt"
        ) from exc
    if config is not None and config.thick_mode and oracledb.is_thin_mode():
        lib_dir = None
        if config.oracle_home is not None:
            candidate = config.oracle_home / "bin"
            lib_dir = str(candidate if candidate.exists() else config.oracle_home)
        try:
            oracledb.init_oracle_client(lib_dir=lib_dir)
        except oracledb.Error as exc:
            raise DatabaseUnavailable(
                f"Oracle thick-mode initialization failed: {exc}"
            ) from exc
    return oracledb


class OracleRepository:
    provider_name = "oracle"

    def __init__(self, config: OracleConfig):
        self.config = config

    @contextmanager
    def connection(self) -> Iterator[Any]:
        driver = _driver_module(self.config)
        try:
            connection = driver.connect(
                user=self.config.user,
                password=self.config.password,
                dsn=self.config.dsn,
                tcp_connect_timeout=self.config.connect_timeout,
            )
        except driver.Error as exc:
            raise DatabaseUnavailable(str(exc)) from exc
        try:
            yield connection
        except driver.Error as exc:
            try:
                connection.rollback()
            except driver.Error:
                pass
            raise DatabaseUnavailable(str(exc)) from exc
        finally:
            try:
                connection.close()
            except driver.Error:
                # A dropped Oracle session is already unusable; do not mask the original result.
                pass

    def health_check(self) -> dict[str, Any]:
        with self.connection() as connection:
            cursor = connection.cursor()
            cursor.execute(
                """
                SELECT SYS_CONTEXT('USERENV', 'DB_NAME'),
                       SYS_CONTEXT('USERENV', 'CON_NAME'),
                       SYSTIMESTAMP
                FROM dual
                """
            )
            database_name, container_name, server_time = cursor.fetchone()
            cursor.close()
            return {
                "version": connection.version,
                "database_name": str(database_name),
                "container_name": str(container_name),
                "server_time": server_time,
            }

    def store_process_sample(self, sample: ProcessSample) -> None:
        started_at = _oracle_datetime(sample.process_started_at or sample.timestamp)
        sampled_at = _oracle_datetime(sample.timestamp)
        with self.connection() as connection:
            cursor = connection.cursor()
            cursor.execute(
                """
                MERGE INTO processes target
                USING (
                    SELECT :windows_pid AS windows_pid,
                           :process_started_at AS process_started_at
                    FROM dual
                ) source
                ON (target.windows_pid = source.windows_pid
                    AND target.process_started_at = source.process_started_at)
                WHEN MATCHED THEN UPDATE SET
                    target.process_name = :process_name,
                    target.last_seen = :last_seen
                WHEN NOT MATCHED THEN INSERT
                    (windows_pid, process_name, process_started_at, first_seen, last_seen)
                VALUES
                    (:windows_pid, :process_name, :process_started_at,
                     :first_seen, :last_seen)
                """,
                {
                    "windows_pid": sample.pid,
                    "process_name": sample.name,
                    "process_started_at": started_at,
                    "first_seen": sampled_at,
                    "last_seen": sampled_at,
                },
            )
            cursor.execute(
                """
                SELECT process_id
                FROM processes
                WHERE windows_pid = :1 AND process_started_at = :2
                """,
                (sample.pid, started_at),
            )
            process_id = int(cursor.fetchone()[0])
            cursor.execute(
                """
                INSERT INTO process_samples
                    (process_id, sampled_at, page_faults, working_set_bytes,
                     private_bytes, cpu_percent, read_bytes, write_bytes)
                VALUES (:1, :2, :3, :4, :5, :6, :7, :8)
                """,
                (
                    process_id,
                    sampled_at,
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
        with self.connection() as connection:
            cursor = connection.cursor()
            cursor.execute(
                """
                INSERT INTO system_samples
                    (sampled_at, memory_total_bytes, memory_available_bytes,
                     memory_percent, swap_used_bytes, swap_percent, process_count)
                VALUES (:1, :2, :3, :4, :5, :6, :7)
                """,
                (
                    _oracle_datetime(sampled_at or utc_now()),
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
            driver = _driver_module(self.config)
            cursor = active.cursor()
            action_id = cursor.var(driver.NUMBER)
            cursor.execute(
                """
                INSERT INTO optimization_actions
                    (action_kind, target, started_at, duration_ms, success,
                     bytes_touched, message, metadata_json)
                VALUES (:kind, :target, :started_at, :duration_ms, :success,
                        :bytes_touched, :message, :metadata_json)
                RETURNING action_id INTO :action_id
                """,
                {
                    "kind": result.kind.value,
                    "target": result.target,
                    "started_at": _oracle_datetime(result.started_at),
                    "duration_ms": result.duration_ms,
                    "success": int(result.success),
                    "bytes_touched": result.bytes_touched,
                    "message": result.message,
                    "metadata_json": json.dumps(result.metadata),
                    "action_id": action_id,
                },
            )
            value = _returning_value(action_id.getvalue())
            if owns_connection:
                active.commit()
            cursor.close()
            return int(value)

    def store_experiment(self, result: ExperimentResult) -> int:
        with self.connection() as connection:
            driver = _driver_module(self.config)
            action_id = self.store_action(result.action, connection=connection)
            cursor = connection.cursor()
            experiment_id = cursor.var(driver.NUMBER)
            cursor.execute(
                """
                INSERT INTO experiments
                    (experiment_type, target, policy_name, action_id,
                     baseline_latency_ms, post_action_latency_ms,
                     page_faults_before, page_faults_after,
                     working_set_before, working_set_after, reward, created_at)
                VALUES (:experiment_type, :target, :policy_name, :action_id,
                        :baseline_latency_ms, :post_action_latency_ms,
                        :page_faults_before, :page_faults_after,
                        :working_set_before, :working_set_after, :reward, :created_at)
                RETURNING experiment_id INTO :experiment_id
                """,
                {
                    "experiment_type": result.experiment_type,
                    "target": result.target,
                    "policy_name": result.policy,
                    "action_id": action_id,
                    "baseline_latency_ms": result.baseline_latency_ms,
                    "post_action_latency_ms": result.post_action_latency_ms,
                    "page_faults_before": result.page_faults_before,
                    "page_faults_after": result.page_faults_after,
                    "working_set_before": result.working_set_before,
                    "working_set_after": result.working_set_after,
                    "reward": result.reward,
                    "created_at": _oracle_datetime(result.created_at),
                    "experiment_id": experiment_id,
                },
            )
            value = _returning_value(experiment_id.getvalue())
            connection.commit()
            cursor.close()
            return int(value)

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
                VALUES (:1, :2, :3, :4, :5)
                """,
                (
                    json.dumps(list(state)),
                    action,
                    reward,
                    json.dumps(list(next_state)),
                    int(done),
                ),
            )
            connection.commit()
            cursor.close()

    def fetch_dqn_experiences(self, limit: int = 10_000) -> list[dict[str, Any]]:
        safe_limit = min(max(1, int(limit)), 100_000)
        with self.connection() as connection:
            cursor = connection.cursor()
            cursor.execute(
                f"""
                SELECT state_json, action_index, reward, next_state_json, done
                FROM dqn_experiences
                ORDER BY experience_id DESC
                FETCH FIRST {safe_limit} ROWS ONLY
                """
            )
            rows = cursor.fetchall()
            # Materialize Oracle LOBs before the connection is closed. Reading
            # them afterward can raise DPY-1001 on a dropped/closed session.
            parsed = [
                {
                    "state": json.loads(_lob_text(row[0])),
                    "action": int(row[1]),
                    "reward": float(row[2]),
                    "next_state": json.loads(_lob_text(row[3])),
                    "done": bool(row[4]),
                }
                for row in rows
            ]
            cursor.close()
        return parsed
    def fetch_query_metrics(
        self, query: str, limit: int = 9
    ) -> list[dict[str, Any]]:
        safe_limit = min(max(1, int(limit)), 100)
        sql = query.strip().rstrip(";").rstrip()
        digest = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        with self.connection() as connection:
            cursor = connection.cursor()
            cursor.execute(
                f"""
                SELECT statement_name, duration_ms, rows_returned, measured_at
                FROM query_metrics
                WHERE query_digest = :1
                ORDER BY metric_id DESC
                FETCH FIRST {safe_limit} ROWS ONLY
                """,
                (digest,),
            )
            rows = cursor.fetchall()
            cursor.close()
        return [
            {
                "statement_name": str(row[0]),
                "duration_ms": float(row[1]),
                "rows_returned": int(row[2]),
                "measured_at": row[3],
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
        sql = query.strip().rstrip(";").rstrip()
        values = tuple(params or ())
        explain_text = None
        with self.connection() as connection:
            cursor = connection.cursor()
            if explain:
                try:
                    cursor.execute(f"EXPLAIN PLAN FOR {sql}", values)
                    cursor.execute(
                        "SELECT plan_table_output "
                        "FROM TABLE(DBMS_XPLAN.DISPLAY(NULL, NULL, 'BASIC +COST +ROWS'))"
                    )
                    explain_text = "\n".join(str(row[0]) for row in cursor.fetchall())
                except Exception as exc:
                    explain_text = f"Oracle explain plan unavailable: {exc}"
                finally:
                    connection.rollback()

            cursor.execute("SET TRANSACTION READ ONLY")
            started = time.perf_counter()
            cursor.execute(sql, values)
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
            connection.rollback()

            digest = hashlib.sha256(sql.encode("utf-8")).hexdigest()
            cursor.execute(
                """
                INSERT INTO query_metrics
                    (query_digest, statement_name, duration_ms, rows_returned, explain_text)
                VALUES (:1, :2, :3, :4, :5)
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


def bootstrap_oracle_database(
    *,
    dsn: str,
    admin_user: str,
    admin_password: str,
    app_user: str,
    app_password: str,
    schema_path: str | Path,
    tablespace: str = "USERS",
    sysdba: bool = False,
    oracle_home: str | Path | None = None,
    thick_mode: bool = False,
    local_sysdba: bool = False,
    local_sid: str = "XE",
) -> None:
    admin_user, inline_sysdba = normalize_oracle_admin_user(admin_user)
    sysdba = sysdba or inline_sysdba
    home_path = Path(oracle_home) if oracle_home else None
    if local_sysdba:
        if not sysdba:
            raise ValueError("Local Oracle administration requires SYSDBA mode")
        if home_path is None:
            raise ValueError("ORACLE_HOME is required for local SYSDBA setup")
        os.environ["ORACLE_HOME"] = str(home_path)
        os.environ["ORACLE_SID"] = _validate_identifier(local_sid).upper()
    driver = _driver_module(
        OracleConfig(
            dsn=dsn,
            user=admin_user,
            password=admin_password,
            oracle_home=home_path,
            thick_mode=thick_mode or local_sysdba,
        )
    )
    username = _validate_identifier(app_user).upper()
    tablespace_name = _validate_identifier(tablespace).upper()
    password_sql = _quote_oracle_password(app_password)
    mode = driver.AUTH_MODE_SYSDBA if sysdba else driver.AUTH_MODE_DEFAULT
    phase = "connecting to Oracle as the administrator"
    try:
        connect_options: dict[str, Any] = {
            "user": admin_user,
            "password": admin_password,
            "mode": mode,
        }
        if not local_sysdba:
            connect_options.update(dsn=dsn, tcp_connect_timeout=10)
        admin_connection = driver.connect(**connect_options)
    except driver.Error as exc:
        raise DatabaseUnavailable(f"Oracle setup failed while {phase}: {exc}") from exc
    try:
        cursor = admin_connection.cursor()
        if local_sysdba:
            pdb_name = _pdb_from_dsn(dsn)
            phase = f"switching the administrator session to PDB {pdb_name}"
            cursor.execute(f"ALTER SESSION SET CONTAINER = {pdb_name}")
        phase = f"checking whether application user {username} exists"
        cursor.execute("SELECT COUNT(*) FROM dba_users WHERE username = :1", (username,))
        if int(cursor.fetchone()[0]):
            phase = f"updating application user {username}"
            cursor.execute(f'ALTER USER {username} IDENTIFIED BY "{password_sql}"')
        else:
            phase = f"creating application user {username}"
            cursor.execute(f'CREATE USER {username} IDENTIFIED BY "{password_sql}"')
        phase = f"granting limited runtime privileges to {username}"
        cursor.execute(
            f"GRANT CREATE SESSION, CREATE SEQUENCE, CREATE TABLE, CREATE VIEW TO {username}"
        )
        phase = f"setting {tablespace_name} quota for {username}"
        cursor.execute(
            f"ALTER USER {username} QUOTA 100M ON {tablespace_name}"
        )
        admin_connection.commit()
        cursor.close()
    except driver.Error as exc:
        admin_connection.rollback()
        raise DatabaseUnavailable(f"Oracle setup failed while {phase}: {exc}") from exc
    finally:
        admin_connection.close()

    runtime_config = OracleConfig(
        dsn=dsn,
        user=username,
        password=app_password,
        oracle_home=home_path,
        thick_mode=thick_mode or local_sysdba,
    )
    repository = OracleRepository(runtime_config)
    driver = _driver_module(runtime_config)
    try:
        with repository.connection() as connection:
            cursor = connection.cursor()
            script = Path(schema_path).read_text(encoding="utf-8")
            for statement in split_sql_script(script):
                try:
                    cursor.execute(statement)
                except driver.DatabaseError as exc:
                    error = exc.args[0]
                    if getattr(error, "code", None) != 955:
                        summary = " ".join(statement.split())[:100]
                        raise DatabaseUnavailable(
                            f"Oracle setup failed while applying schema statement "
                            f"'{summary}': {exc}"
                        ) from exc
            connection.commit()
            cursor.close()
    except DatabaseUnavailable as exc:
        if str(exc).startswith("Oracle setup failed"):
            raise
        raise DatabaseUnavailable(
            f"Oracle setup failed while connecting as runtime user {username}: {exc}"
        ) from exc


def _oracle_datetime(value: datetime) -> datetime:
    return _mysql_datetime(value)


def _quote_oracle_password(password: str) -> str:
    if not password or any(ord(char) < 32 for char in password):
        raise ValueError("Oracle password cannot be empty or contain control characters")
    return password.replace('"', '""')


def _pdb_from_dsn(dsn: str) -> str:
    service = dsn.rstrip("/").rsplit("/", 1)[-1]
    if not service or service == dsn.rstrip("/"):
        raise ValueError("Oracle DSN must include a PDB service such as /XEPDB1")
    return _validate_identifier(service).upper()


def normalize_oracle_admin_user(value: str) -> tuple[str, bool]:
    """Accept SQL*Plus-style ``SYS AS SYSDBA`` without treating it as a username."""
    normalized = " ".join(value.strip().split())
    suffix = " AS SYSDBA"
    inline_sysdba = normalized.upper().endswith(suffix)
    username = normalized[: -len(suffix)].strip() if inline_sysdba else normalized
    if not username:
        raise ValueError("Oracle administrator username cannot be empty")
    return _validate_identifier(username), inline_sysdba


def _returning_value(value: Any) -> Any:
    while isinstance(value, (list, tuple)) and value:
        value = value[0]
    return value


def _lob_text(value: Any) -> str:
    return str(value.read() if hasattr(value, "read") else value)


@contextmanager
def _passthrough(value: Any) -> Iterator[Any]:
    yield value

