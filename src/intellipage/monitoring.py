from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes
from datetime import datetime, timezone
from typing import Iterable

import psutil

from .models import ProcessSample


if os.name == "nt":
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    PROCESS_VM_READ = 0x0010

    class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivateUsage", ctypes.c_size_t),
        ]


def _windows_memory_counters(pid: int) -> tuple[int, int, int] | None:
    if os.name != "nt":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(PROCESS_MEMORY_COUNTERS_EX),
        wintypes.DWORD,
    ]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ, False, pid
    )
    if not handle:
        return None
    try:
        counters = PROCESS_MEMORY_COUNTERS_EX()
        counters.cb = ctypes.sizeof(counters)
        ok = psapi.GetProcessMemoryInfo(
            handle, ctypes.byref(counters), counters.cb
        )
        if not ok:
            return None
        return (
            int(counters.PageFaultCount),
            int(counters.WorkingSetSize),
            int(counters.PrivateUsage),
        )
    finally:
        kernel32.CloseHandle(handle)


class WindowsMonitor:
    def __init__(self) -> None:
        # psutil needs the same Process instance across samples to calculate a
        # non-blocking CPU percentage. Recreating it makes every reading zero.
        self._processes: dict[int, psutil.Process] = {}

    def sample_process(self, pid: int) -> ProcessSample:
        timestamp = datetime.now(timezone.utc)
        try:
            process = self._processes.get(pid)
            if process is None:
                process = psutil.Process(pid)
                process.cpu_percent(interval=None)
                self._processes[pid] = process
            with process.oneshot():
                name = process.name()
                process_started_at = datetime.fromtimestamp(
                    process.create_time(), timezone.utc
                )
                cpu = process.cpu_percent(interval=None)
                memory = process.memory_info()
                io = process.io_counters()
            native = _windows_memory_counters(pid)
            if native is None:
                page_faults = int(getattr(memory, "num_page_faults", 0))
                working_set = int(getattr(memory, "rss", 0))
                private = int(getattr(memory, "private", getattr(memory, "vms", 0)))
            else:
                page_faults, working_set, private = native
            return ProcessSample(
                pid=pid,
                name=name,
                timestamp=timestamp,
                page_faults=page_faults,
                working_set_bytes=working_set,
                private_bytes=private,
                cpu_percent=float(cpu),
                read_bytes=int(getattr(io, "read_bytes", 0)),
                write_bytes=int(getattr(io, "write_bytes", 0)),
                process_started_at=process_started_at,
            )
        except psutil.NoSuchProcess:
            self._processes.pop(pid, None)
            return ProcessSample(
                pid=pid,
                name=f"pid-{pid}",
                timestamp=timestamp,
                page_faults=0,
                working_set_bytes=0,
                private_bytes=0,
                cpu_percent=0.0,
                read_bytes=0,
                write_bytes=0,
                process_started_at=None,
                accessible=False,
                error="process_ended",
            )
        except psutil.AccessDenied:
            self._processes.pop(pid, None)
            return ProcessSample(
                pid=pid,
                name=f"pid-{pid}",
                timestamp=timestamp,
                page_faults=0,
                working_set_bytes=0,
                private_bytes=0,
                cpu_percent=0.0,
                read_bytes=0,
                write_bytes=0,
                process_started_at=None,
                accessible=False,
                error="access_denied",
            )
        except OSError as exc:
            self._processes.pop(pid, None)
            return ProcessSample(
                pid=pid,
                name=f"pid-{pid}",
                timestamp=timestamp,
                page_faults=0,
                working_set_bytes=0,
                private_bytes=0,
                cpu_percent=0.0,
                read_bytes=0,
                write_bytes=0,
                process_started_at=None,
                accessible=False,
                error=f"os_error:{exc}",
            )

    def list_processes(self, limit: int = 25) -> list[ProcessSample]:
        processes = list(psutil.process_iter())
        for process in processes:
            try:
                self._processes[process.pid] = process
                process.cpu_percent(interval=None)
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                self._processes.pop(process.pid, None)
        # Prime all processes together, then take one shared short interval.
        time.sleep(0.1)
        samples = [self.sample_process(proc.pid) for proc in processes]
        samples = [sample for sample in samples if sample.accessible]
        samples.sort(key=lambda item: item.working_set_bytes, reverse=True)
        return samples[: max(1, limit)]

    def sample_many(self, pids: Iterable[int]) -> list[ProcessSample]:
        return [self.sample_process(pid) for pid in pids]

    def system_summary(self) -> dict[str, float | int]:
        vm = psutil.virtual_memory()
        try:
            swap = psutil.swap_memory()
            swap_used = int(swap.used)
            swap_percent = float(swap.percent)
        except (RuntimeError, OSError):
            # Some Windows installations have PDH performance counters disabled.
            # Live process and memory collection remains available without them.
            swap_used = 0
            swap_percent = 0.0
        return {
            "memory_total_bytes": int(vm.total),
            "memory_available_bytes": int(vm.available),
            "memory_percent": float(vm.percent),
            "swap_used_bytes": swap_used,
            "swap_percent": swap_percent,
            "process_count": len(psutil.pids()),
        }


def current_process_sample() -> ProcessSample:
    return WindowsMonitor().sample_process(os.getpid())

