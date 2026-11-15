from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SafetyMode(str, Enum):
    MONITOR = "monitor"
    CONFIRM = "confirm"
    AUTO = "auto"


class ActionKind(str, Enum):
    NOOP = "noop"
    FILE_WARM = "file_warm"
    MMAP_PREFETCH = "mmap_prefetch"
    DATABASE_WARM = "database_warm"
    MYSQL_WARM = "database_warm"  # compatibility alias for older callers
    TRIM_WORKING_SET = "trim_working_set"


@dataclass(slots=True)
class ProcessSample:
    pid: int
    name: str
    timestamp: datetime
    page_faults: int
    working_set_bytes: int
    private_bytes: int
    cpu_percent: float
    read_bytes: int
    write_bytes: int
    process_started_at: datetime | None = None
    accessible: bool = True
    error: str | None = None

    def as_record(self) -> dict[str, Any]:
        data = asdict(self)
        data["timestamp"] = self.timestamp.isoformat()
        if self.process_started_at is not None:
            data["process_started_at"] = self.process_started_at.isoformat()
        return data


@dataclass(slots=True)
class ResourceState:
    resource_id: str
    first_seen_order: int
    last_access_order: int
    access_count: int
    size_bytes: int = 0
    next_access_order: int | None = None


@dataclass(slots=True)
class ActionResult:
    kind: ActionKind
    target: str
    success: bool
    started_at: datetime
    duration_ms: float
    bytes_touched: int = 0
    message: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ExperimentResult:
    experiment_type: str
    target: str
    policy: str
    action: ActionResult
    baseline_latency_ms: float
    post_action_latency_ms: float
    page_faults_before: int
    page_faults_after: int
    working_set_before: int
    working_set_after: int
    reward: float
    created_at: datetime = field(default_factory=utc_now)

    def as_record(self) -> dict[str, Any]:
        return {
            "experiment_type": self.experiment_type,
            "target": self.target,
            "policy": self.policy,
            "action_kind": self.action.kind.value,
            "action_success": self.action.success,
            "action_duration_ms": self.action.duration_ms,
            "bytes_touched": self.action.bytes_touched,
            "baseline_latency_ms": self.baseline_latency_ms,
            "post_action_latency_ms": self.post_action_latency_ms,
            "page_faults_before": self.page_faults_before,
            "page_faults_after": self.page_faults_after,
            "working_set_before": self.working_set_before,
            "working_set_after": self.working_set_after,
            "reward": self.reward,
            "created_at": self.created_at.isoformat(),
            "metadata": self.action.metadata,
        }

