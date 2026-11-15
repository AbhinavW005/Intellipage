from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable

from .actions import prefetch_mapped_file, warm_file
from .database import DatabaseRepository, DatabaseUnavailable
from .dqn import ACTION_NAMES, DoubleDQNAgent
from .experiments import ExperimentRunner, build_dqn_state, create_demo_file, timed_file_read
from .monitoring import WindowsMonitor


class LiveFileDQNTrainer:
    """Collects training transitions by executing real file operations on Windows."""

    def __init__(
        self,
        monitor: WindowsMonitor,
        agent: DoubleDQNAgent,
        repository: DatabaseRepository | None = None,
        audit_path: str | Path = "data/dqn_training.jsonl",
    ) -> None:
        self.monitor = monitor
        self.agent = agent
        self.repository = repository
        self.audit_path = Path(audit_path)

    def train(
        self,
        file_path: str | Path,
        *,
        steps: int = 40,
        max_bytes: int = 8 * 1024 * 1024,
        batch_size: int = 8,
        database_query: str | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        if steps < 1:
            raise ValueError("steps must be at least one")
        if max_bytes < 1:
            raise ValueError("max_bytes must be at least one")
        if batch_size < 1:
            raise ValueError("batch_size must be at least one")
        target = create_demo_file(file_path, size_mb=max(8, max_bytes // (1024 * 1024)))
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        previous_reward = 0.0
        previous_faults: int | None = None
        previous_fault_time = None
        losses: list[float] = []
        history: list[dict[str, Any]] = []

        for step in range(steps):
            baseline_ms, _ = timed_file_read(target, max_bytes=max_bytes)
            before = self.monitor.sample_process(os.getpid())
            system_before = self.monitor.system_summary()
            page_fault_rate = 0.0
            if previous_faults is not None and previous_fault_time is not None:
                elapsed = max(
                    (before.timestamp - previous_fault_time).total_seconds(), 0.001
                )
                page_fault_rate = max(0, before.page_faults - previous_faults) / elapsed
            state = build_dqn_state(
                before,
                system_before,
                file_latency_ms=baseline_ms,
                resource_size_mb=max_bytes / (1024 * 1024),
                previous_reward=previous_reward,
                page_fault_rate=page_fault_rate,
            )
            action_index = self.agent.act(state, explore=True)
            database_result = None
            action_domain = "file"
            started = time.perf_counter()
            if action_index == 0:
                action_success = True
                action_name = ACTION_NAMES[action_index]
                post_ms, _ = timed_file_read(target, max_bytes=max_bytes)
                after = self.monitor.sample_process(os.getpid())
                system_after = self.monitor.system_summary()
                fault_delta = max(0, after.page_faults - before.page_faults)
                latency_gain = (baseline_ms - post_ms) / max(baseline_ms, 0.001)
                memory_cost = max(0, after.working_set_bytes - before.working_set_bytes) / (256 * 1024 * 1024)
                reward = latency_gain - 0.001 * fault_delta - memory_cost
            elif action_index == 1:
                result = warm_file(target, max_bytes=max_bytes)
                action_success = result.success
                action_name = result.kind.value
                post_ms, _ = timed_file_read(target, max_bytes=max_bytes)
                after = self.monitor.sample_process(os.getpid())
                system_after = self.monitor.system_summary()
                fault_delta = max(0, after.page_faults - before.page_faults)
                latency_gain = (baseline_ms - post_ms) / max(baseline_ms, 0.001)
                memory_cost = max(0, after.working_set_bytes - before.working_set_bytes) / (256 * 1024 * 1024)
                reward = latency_gain - 0.001 * fault_delta - memory_cost
            elif action_index == 2:
                result = prefetch_mapped_file(target, max_bytes=max_bytes)
                action_success = result.success
                action_name = result.kind.value
                post_ms, _ = timed_file_read(target, max_bytes=max_bytes)
                after = self.monitor.sample_process(os.getpid())
                system_after = self.monitor.system_summary()
                fault_delta = max(0, after.page_faults - before.page_faults)
                latency_gain = (baseline_ms - post_ms) / max(baseline_ms, 0.001)
                memory_cost = max(0, after.working_set_bytes - before.working_set_bytes) / (256 * 1024 * 1024)
                reward = latency_gain - 0.001 * fault_delta - memory_cost
            elif self.repository is not None and database_query:
                action_domain = "database"
                try:
                    database_runner = ExperimentRunner(
                        self.monitor,
                        self.repository,
                        audit_path=self.audit_path.parent / "action_audit.jsonl",
                    )
                    database_result = database_runner.run_database_warm_experiment(
                        statement_name=f"dqn_step_{step + 1}",
                        query=database_query,
                        policy="DQN",
                    )
                    action_success = database_result.action.success
                    action_name = ACTION_NAMES[action_index]
                    post_ms = database_result.post_action_latency_ms
                    after = self.monitor.sample_process(os.getpid())
                    system_after = self.monitor.system_summary()
                    fault_delta = max(0, after.page_faults - before.page_faults)
                    reward = database_result.reward
                except Exception:
                    action_success = False
                    action_name = ACTION_NAMES[action_index]
                    post_ms, _ = timed_file_read(target, max_bytes=max_bytes)
                    after = self.monitor.sample_process(os.getpid())
                    system_after = self.monitor.system_summary()
                    fault_delta = max(0, after.page_faults - before.page_faults)
                    reward = -0.25
            else:
                action_success = False
                action_name = ACTION_NAMES[action_index]
                post_ms, _ = timed_file_read(target, max_bytes=max_bytes)
                after = self.monitor.sample_process(os.getpid())
                system_after = self.monitor.system_summary()
                fault_delta = max(0, after.page_faults - before.page_faults)
                reward = -0.25
            action_ms = (time.perf_counter() - started) * 1000
            if not action_success and database_result is None:
                reward -= 0.25
            next_state = build_dqn_state(
                after,
                system_after,
                file_latency_ms=post_ms,
                resource_size_mb=max_bytes / (1024 * 1024),
                previous_reward=reward,
                page_fault_rate=fault_delta
                / max((after.timestamp - before.timestamp).total_seconds(), 0.001),
            )
            done = step == steps - 1
            self.agent.remember(state, action_index, reward, next_state, done)
            loss = self.agent.train_step(batch_size=batch_size)
            if loss is not None:
                losses.append(loss)
            record = {
                "step": step + 1,
                "action_index": action_index,
                "action_name": action_name,
                "action_domain": action_domain,
                "action_success": action_success,
                "action_ms": action_ms,
                "baseline_ms": baseline_ms,
                "post_ms": post_ms,
                "fault_delta": fault_delta,
                "reward": reward,
                "loss": loss,
                "state": state,
                "next_state": next_state,
            }
            with self.audit_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
            if self.repository is not None:
                try:
                    self.repository.store_dqn_experience(
                        state, action_index, reward, next_state, done
                    )
                except Exception:
                    self.repository = None
            history.append(record)
            if progress_callback is not None:
                progress_callback(record)
            previous_reward = reward
            previous_faults = after.page_faults
            previous_fault_time = after.timestamp

        return {
            "steps": steps,
            "experiences": len(self.agent.memory),
            "training_steps": self.agent.training_steps,
            "epsilon": self.agent.epsilon,
            "mean_loss": sum(losses) / len(losses) if losses else 0.0,
            "last_reward": previous_reward,
            "workload_file": str(target),
            "history": history,
        }


def load_real_experiences(repository: DatabaseRepository, agent: DoubleDQNAgent) -> int:
    rows = repository.fetch_dqn_experiences()
    for row in rows:
        agent.remember(
            row["state"],
            row["action"],
            row["reward"],
            row["next_state"],
            row["done"],
        )
    return len(rows)






