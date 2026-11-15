from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from .experiments import create_demo_file
from .models import ResourceState
from .monitoring import WindowsMonitor
from .policies import ReplacementPolicy


@dataclass(slots=True)
class PolicyRunResult:
    policy: str
    accesses: int
    hits: int
    misses: int
    bytes_read: int
    storage_bytes_read: int
    cache_bytes_served: int
    io_duration_ms: float
    page_fault_delta: int
    evictions: list[str] = field(default_factory=list)

    @property
    def hit_rate(self) -> float:
        return self.hits / max(1, self.accesses)


class ManagedFileCache:
    """A bounded in-process byte cache driven by a replacement policy.

    A miss reads real file bytes into IntelliPage memory. A hit serves those
    bytes from the bounded in-process cache without rereading the file. This
    is genuine application-level caching; it deliberately does not claim to
    evict or control Windows kernel file-cache pages.
    """

    def __init__(
        self,
        policy: ReplacementPolicy,
        capacity: int = 3,
        *,
        max_cached_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        if capacity < 1:
            raise ValueError("capacity must be at least one")
        if max_cached_bytes < 1:
            raise ValueError("max_cached_bytes must be at least one")
        self.policy = policy
        self.capacity = capacity
        self.max_cached_bytes = max_cached_bytes
        self._resources: dict[str, ResourceState] = {}
        self._payloads: dict[str, bytes] = {}
        self._order = 0
        self._cached_bytes = 0
        self.hits = 0
        self.misses = 0
        self.bytes_read = 0
        self.storage_bytes_read = 0
        self.cache_bytes_served = 0
        self.io_duration_ms = 0.0
        self.evictions: list[str] = []

    @property
    def cached_bytes(self) -> int:
        return self._cached_bytes

    def access(self, path: str | Path, *, max_bytes: int | None = None) -> bool:
        target = Path(path).expanduser().resolve()
        resource_id = str(target)
        self._order += 1
        state = self._resources.get(resource_id)
        payload = self._payloads.get(resource_id)
        hit = state is not None and payload is not None

        if hit:
            self.hits += 1
            started = time.perf_counter()
            # Touch one byte per page-sized stride so this is a real memory
            # access, not merely a dictionary lookup.
            checksum = 0
            for index in range(0, len(payload), 4096):
                checksum ^= payload[index]
            if checksum == -1:  # keeps the access observable to the interpreter
                raise RuntimeError("unreachable cache checksum")
            duration_ms = (time.perf_counter() - started) * 1000
            touched = len(payload)
            self.cache_bytes_served += touched
            state.last_access_order = self._order
            state.access_count += 1
        else:
            self.misses += 1
            duration_ms, payload = _timed_file_payload(target, max_bytes=max_bytes)
            touched = len(payload)
            self.storage_bytes_read += touched
            # A resource larger than the bounded cache is served for this
            # request but not retained, preventing unbounded memory growth.
            if touched <= self.max_cached_bytes:
                while (
                    self._resources
                    and (
                        len(self._resources) >= self.capacity
                        or self._cached_bytes + touched > self.max_cached_bytes
                    )
                ):
                    victim = self.policy.select_victim(list(self._resources.values()))
                    self._resources.pop(victim.resource_id, None)
                    victim_payload = self._payloads.pop(victim.resource_id, b"")
                    self._cached_bytes -= len(victim_payload)
                    self.evictions.append(Path(victim.resource_id).name)
                self._resources[resource_id] = ResourceState(
                    resource_id=resource_id,
                    first_seen_order=self._order,
                    last_access_order=self._order,
                    access_count=1,
                    size_bytes=target.stat().st_size,
                )
                self._payloads[resource_id] = payload
                self._cached_bytes += touched

        self.io_duration_ms += duration_ms
        self.bytes_read += touched
        return hit


def _timed_file_payload(
    path: str | Path, *, max_bytes: int | None = None
) -> tuple[float, bytes]:
    if max_bytes is not None and max_bytes < 1:
        raise ValueError("max_bytes must be at least one")
    target = Path(path)
    chunks: list[bytes] = []
    remaining = max_bytes
    started = time.perf_counter()
    with target.open("rb", buffering=0) as handle:
        while remaining is None or remaining > 0:
            request = 1024 * 1024 if remaining is None else min(1024 * 1024, remaining)
            block = handle.read(request)
            if not block:
                break
            chunks.append(block)
            if remaining is not None:
                remaining -= len(block)
    return (time.perf_counter() - started) * 1000, b"".join(chunks)


def run_real_policy_trace(
    policy: ReplacementPolicy,
    paths: Sequence[str | Path],
    *,
    capacity: int = 3,
    max_bytes: int | None = None,
    monitor: WindowsMonitor | None = None,
) -> PolicyRunResult:
    if not paths:
        raise ValueError("At least one real file access is required")
    windows_monitor = monitor or WindowsMonitor()
    cache = ManagedFileCache(
        policy,
        capacity,
        max_cached_bytes=max(64 * 1024 * 1024, (max_bytes or 0) * capacity),
    )
    before = windows_monitor.sample_process(os.getpid())
    for path in paths:
        cache.access(path, max_bytes=max_bytes)
    after = windows_monitor.sample_process(os.getpid())
    return PolicyRunResult(
        policy=policy.name,
        accesses=len(paths),
        hits=cache.hits,
        misses=cache.misses,
        bytes_read=cache.bytes_read,
        storage_bytes_read=cache.storage_bytes_read,
        cache_bytes_served=cache.cache_bytes_served,
        io_duration_ms=cache.io_duration_ms,
        page_fault_delta=after.page_faults - before.page_faults,
        evictions=cache.evictions,
    )


def create_policy_workload(root: str | Path, policy_name: str) -> list[Path]:
    directory = Path(root) / policy_name.lower()
    files = {
        name: create_demo_file(directory / f"{name}.bin", size_mb=4)
        for name in ("A", "B", "C", "D", "E")
    }
    return [files[name] for name in ("A", "B", "C", "A", "D", "B", "E", "A", "C", "D")]
