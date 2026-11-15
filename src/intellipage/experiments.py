from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Callable, Sequence

import psutil

from .actions import prefetch_mapped_file, warm_file
from .database import DatabaseRepository, DatabaseUnavailable
from .models import ActionKind, ActionResult, ExperimentResult, ProcessSample, utc_now
from .monitoring import WindowsMonitor


def timed_file_read(path: str | Path, *, max_bytes: int | None = None) -> tuple[float, int]:
    if max_bytes is not None and max_bytes < 1:
        raise ValueError("max_bytes must be at least one")
    target = Path(path)
    touched = 0
    buffer = bytearray(1024 * 1024)
    started = time.perf_counter()
    with target.open("rb", buffering=0) as handle:
        while True:
            remaining = None if max_bytes is None else max_bytes - touched
            if remaining is not None and remaining <= 0:
                break
            view = memoryview(buffer)[: min(len(buffer), remaining or len(buffer))]
            read_count = handle.readinto(view)
            if not read_count:
                break
            touched += read_count
    return (time.perf_counter() - started) * 1000, touched


def create_demo_file(path: str | Path, size_mb: int = 32) -> Path:
    """Create a real file-backed workload without constructing fake page-fault results."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    expected_size = max(1, size_mb) * 1024 * 1024
    if target.exists() and target.stat().st_size == expected_size:
        return target
    chunk = os.urandom(1024 * 1024)
    with target.open("wb", buffering=0) as handle:
        remaining = expected_size
        while remaining:
            block = chunk[: min(len(chunk), remaining)]
            handle.write(block)
            remaining -= len(block)
        handle.flush()
        os.fsync(handle.fileno())
    return target


class ExperimentRunner:
    def __init__(
        self,
        monitor: WindowsMonitor,
        repository: DatabaseRepository | None = None,
        audit_path: str | Path = "data/action_audit.jsonl",
    ) -> None:
        self.monitor = monitor
        self.repository = repository
        self.audit_path = Path(audit_path)

    def run_file_experiment(
        self,
        path: str | Path,
        *,
        policy: str,
        action_kind: ActionKind,
        max_bytes: int | None = None,
    ) -> ExperimentResult:
        target = Path(path).expanduser().resolve()
        current_pid = os.getpid()
        baseline_ms, _ = timed_file_read(target, max_bytes=max_bytes)
        # Baseline work is measured separately. The action delta starts here so
        # we do not misattribute baseline page faults to the optimization.
        before = self.monitor.sample_process(current_pid)
        if action_kind == ActionKind.FILE_WARM:
            action = warm_file(target, max_bytes=max_bytes)
        elif action_kind == ActionKind.MMAP_PREFETCH:
            action = prefetch_mapped_file(target, max_bytes=max_bytes)
        else:
            raise ValueError("File experiments support file_warm or mmap_prefetch")
        post_ms, _ = timed_file_read(target, max_bytes=max_bytes)
        after = self.monitor.sample_process(current_pid)
        reward = self._reward(
            baseline_ms,
            post_ms,
            before,
            after,
            action.bytes_touched,
        )
        result = ExperimentResult(
            experiment_type="real_file_io",
            target=str(target),
            policy=policy,
            action=action,
            baseline_latency_ms=baseline_ms,
            post_action_latency_ms=post_ms,
            page_faults_before=before.page_faults,
            page_faults_after=after.page_faults,
            working_set_before=before.working_set_bytes,
            working_set_after=after.working_set_bytes,
            reward=reward,
        )
        self.persist(result)
        return result

    def run_file_trials(
        self,
        path: str | Path,
        *,
        policy: str,
        action_kind: ActionKind,
        trials: int = 3,
        max_bytes: int | None = None,
    ) -> list[ExperimentResult]:
        """Run repeated real trials and return every persisted measurement."""
        if trials < 1:
            raise ValueError("trials must be at least one")
        return [
            self.run_file_experiment(
                path,
                policy=policy,
                action_kind=action_kind,
                max_bytes=max_bytes,
            )
            for _ in range(trials)
        ]
    def run_database_warm_experiment(
        self,
        *,
        statement_name: str,
        query: str,
        params: Sequence[object] | None = None,
        policy: str = "manual",
        progress_callback: Callable[[dict[str, object]], None] | None = None,
    ) -> ExperimentResult:
        if self.repository is None:
            raise DatabaseUnavailable("A database is not configured")
        current_pid = os.getpid()
        baseline = self.repository.execute_timed_query(
            f"{statement_name}_baseline", query, params, explain=True
        )
        if progress_callback is not None:
            progress_callback({"name": "baseline", **baseline})
        before = self.monitor.sample_process(current_pid)
        started_at = utc_now()
        started = time.perf_counter()
        warmed = self.repository.execute_timed_query(
            f"{statement_name}_warm", query, params, explain=False
        )
        duration_ms = (time.perf_counter() - started) * 1000
        if progress_callback is not None:
            progress_callback({"name": "warm", **warmed})
        action = ActionResult(
            kind=ActionKind.DATABASE_WARM,
            target=statement_name,
            success=True,
            started_at=started_at,
            duration_ms=duration_ms,
            bytes_touched=0,
            message=(
                f"Executed a real read-only {self.repository.provider_name} query "
                "to warm database data and indexes."
            ),
            metadata={
                "rows_returned": warmed["rows_returned"],
                "database_provider": self.repository.provider_name,
            },
        )
        post = self.repository.execute_timed_query(
            f"{statement_name}_post", query, params, explain=False
        )
        if progress_callback is not None:
            progress_callback({"name": "post", **post})
        action.metadata.update(
            {
                "query_passes": [
                    {
                        "name": "baseline",
                        "duration_ms": float(baseline["duration_ms"]),
                        "rows_returned": int(baseline["rows_returned"]),
                    },
                    {
                        "name": "warm",
                        "duration_ms": float(warmed["duration_ms"]),
                        "rows_returned": int(warmed["rows_returned"]),
                    },
                    {
                        "name": "post",
                        "duration_ms": float(post["duration_ms"]),
                        "rows_returned": int(post["rows_returned"]),
                    },
                ],
                "explain_plan_captured": bool(baseline.get("explain")),
            }
        )
        after = self.monitor.sample_process(current_pid)
        result = ExperimentResult(
            experiment_type=f"real_{self.repository.provider_name}_query",
            target=statement_name,
            policy=policy,
            action=action,
            baseline_latency_ms=float(baseline["duration_ms"]),
            post_action_latency_ms=float(post["duration_ms"]),
            page_faults_before=before.page_faults,
            page_faults_after=after.page_faults,
            working_set_before=before.working_set_bytes,
            working_set_after=after.working_set_bytes,
            reward=self._reward(
                float(baseline["duration_ms"]),
                float(post["duration_ms"]),
                before,
                after,
                0,
            ),
        )
        self.persist(result)
        return result

    def run_mysql_warm_experiment(self, **kwargs) -> ExperimentResult:
        """Compatibility wrapper for integrations written before Oracle support."""
        return self.run_database_warm_experiment(**kwargs)

    def persist(self, result: ExperimentResult) -> None:
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(result.as_record(), sort_keys=True) + "\n")
        if self.repository is not None:
            self.repository.store_experiment(result)

    @staticmethod
    def _reward(
        baseline_ms: float,
        post_ms: float,
        before: ProcessSample,
        after: ProcessSample,
        bytes_touched: int,
    ) -> float:
        latency_gain = (baseline_ms - post_ms) / max(baseline_ms, 0.001)
        fault_cost = max(0, after.page_faults - before.page_faults) * 0.001
        memory_cost = max(0, after.working_set_bytes - before.working_set_bytes) / max(
            1, 256 * 1024 * 1024
        )
        touch_cost = bytes_touched / max(1, 4 * 1024 * 1024 * 1024)
        return float(latency_gain - fault_cost - memory_cost - touch_cost)


def build_dqn_state(
    sample: ProcessSample,
    system: dict[str, float | int],
    *,
    file_latency_ms: float = 0.0,
    mysql_latency_ms: float = 0.0,
    mysql_hit_rate: float = 0.0,
    recency: float = 0.0,
    frequency: float = 0.0,
    resource_size_mb: float = 0.0,
    previous_reward: float = 0.0,
    page_fault_rate: float = 0.0,
) -> list[float]:
    return [
        min(page_fault_rate / 10_000.0, 10.0),
        sample.working_set_bytes / (1024 * 1024 * 1024),
        float(system["memory_percent"]) / 100.0,
        sample.read_bytes / (1024 * 1024 * 1024),
        sample.write_bytes / (1024 * 1024 * 1024),
        min(file_latency_ms / 1000.0, 10.0),
        min(mysql_latency_ms / 1000.0, 10.0),
        min(max(mysql_hit_rate, 0.0), 1.0),
        min(max(recency, 0.0), 1.0),
        min(max(frequency, 0.0), 1.0),
        min(resource_size_mb / 1024.0, 10.0),
        min(max(previous_reward, -10.0), 10.0),
    ]



