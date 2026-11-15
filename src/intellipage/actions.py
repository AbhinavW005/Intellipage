from __future__ import annotations

import ctypes
import mmap
import os
import time
from ctypes import wintypes
from pathlib import Path
from typing import Callable

from .models import ActionKind, ActionResult, SafetyMode, utc_now


class SafetyGuard:
    def __init__(
        self,
        mode: SafetyMode,
        confirmer: Callable[[str], bool] | None = None,
    ) -> None:
        self.mode = mode
        self.confirmer = confirmer or self._console_confirm

    def authorize(self, description: str, *, destructive: bool = False) -> bool:
        if self.mode == SafetyMode.MONITOR:
            return False
        if destructive or self.mode == SafetyMode.CONFIRM:
            return self.confirmer(description)
        return True

    @staticmethod
    def _console_confirm(description: str) -> bool:
        response = input(f"{description}\nType YES to continue: ").strip()
        return response == "YES"


def warm_file(path: str | Path, *, max_bytes: int | None = None) -> ActionResult:
    target = Path(path).expanduser().resolve()
    started_at = utc_now()
    started = time.perf_counter()
    touched = 0
    if max_bytes is not None and max_bytes < 1:
        return ActionResult(
            kind=ActionKind.FILE_WARM,
            target=str(target),
            success=False,
            started_at=started_at,
            duration_ms=(time.perf_counter() - started) * 1000,
            message="max_bytes must be at least one",
        )
    try:
        buffer = bytearray(1024 * 1024)
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
        return ActionResult(
            kind=ActionKind.FILE_WARM,
            target=str(target),
            success=True,
            started_at=started_at,
            duration_ms=(time.perf_counter() - started) * 1000,
            bytes_touched=touched,
            message="Read real file bytes through the Windows file cache.",
        )
    except OSError as exc:
        return ActionResult(
            kind=ActionKind.FILE_WARM,
            target=str(target),
            success=False,
            started_at=started_at,
            duration_ms=(time.perf_counter() - started) * 1000,
            bytes_touched=touched,
            message=str(exc),
        )


class WIN32_MEMORY_RANGE_ENTRY(ctypes.Structure):
    _fields_ = [("VirtualAddress", ctypes.c_void_p), ("NumberOfBytes", ctypes.c_size_t)]


def prefetch_mapped_file(
    path: str | Path, *, max_bytes: int | None = None
) -> ActionResult:
    """Map a real file and ask Windows to prefetch the mapped address range."""
    target = Path(path).expanduser().resolve()
    started_at = utc_now()
    started = time.perf_counter()
    if os.name != "nt":
        return ActionResult(
            kind=ActionKind.MMAP_PREFETCH,
            target=str(target),
            success=False,
            started_at=started_at,
            duration_ms=0.0,
            message="PrefetchVirtualMemory is available only on Windows.",
        )
    try:
        size = target.stat().st_size
        length = size if max_bytes is None else min(size, max_bytes)
        if length <= 0:
            raise ValueError("The target file is empty")
        # ACCESS_COPY gives ctypes a writable buffer view without requiring
        # write permission on the underlying file. Prefetching is read-only.
        with target.open("rb", buffering=0) as handle:
            with mmap.mmap(handle.fileno(), length=length, access=mmap.ACCESS_COPY) as mapping:
                address = ctypes.addressof(ctypes.c_char.from_buffer(mapping))
                entry = WIN32_MEMORY_RANGE_ENTRY(address, length)
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel32.PrefetchVirtualMemory.argtypes = [
                    wintypes.HANDLE,
                    ctypes.c_size_t,
                    ctypes.POINTER(WIN32_MEMORY_RANGE_ENTRY),
                    wintypes.ULONG,
                ]
                kernel32.PrefetchVirtualMemory.restype = wintypes.BOOL
                kernel32.GetCurrentProcess.argtypes = []
                kernel32.GetCurrentProcess.restype = wintypes.HANDLE
                ok = kernel32.PrefetchVirtualMemory(
                    kernel32.GetCurrentProcess(), 1, ctypes.byref(entry), 0
                )
                error = ctypes.get_last_error()
                del address
                if not ok:
                    raise OSError(error, "PrefetchVirtualMemory failed")
        return ActionResult(
            kind=ActionKind.MMAP_PREFETCH,
            target=str(target),
            success=True,
            started_at=started_at,
            duration_ms=(time.perf_counter() - started) * 1000,
            bytes_touched=length,
            message="Windows accepted a prefetch hint for a real memory-mapped file range.",
        )
    except (OSError, ValueError) as exc:
        return ActionResult(
            kind=ActionKind.MMAP_PREFETCH,
            target=str(target),
            success=False,
            started_at=started_at,
            duration_ms=(time.perf_counter() - started) * 1000,
            message=str(exc),
        )


def trim_working_set(pid: int, guard: SafetyGuard) -> ActionResult:
    """Experimental, confirmed action. It may increase faults and is never automatic."""
    started_at = utc_now()
    started = time.perf_counter()
    description = (
        f"Experimental action: remove as many resident pages as Windows permits from "
        f"PID {pid}. This can slow or destabilize the target application."
    )
    if not guard.authorize(description, destructive=True):
        return ActionResult(
            kind=ActionKind.TRIM_WORKING_SET,
            target=str(pid),
            success=False,
            started_at=started_at,
            duration_ms=(time.perf_counter() - started) * 1000,
            message="Action was not authorized.",
        )
    if os.name != "nt":
        return ActionResult(
            kind=ActionKind.TRIM_WORKING_SET,
            target=str(pid),
            success=False,
            started_at=started_at,
            duration_ms=(time.perf_counter() - started) * 1000,
            message="Working-set control is available only on Windows.",
        )
    # EmptyWorkingSet requires PROCESS_QUERY_INFORMATION, not the limited form.
    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_SET_QUOTA = 0x0100
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    psapi.EmptyWorkingSet.argtypes = [wintypes.HANDLE]
    psapi.EmptyWorkingSet.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(
        PROCESS_QUERY_INFORMATION | PROCESS_SET_QUOTA, False, pid
    )
    if not handle:
        error = ctypes.get_last_error()
        message = f"OpenProcess failed with Windows error {error}"
        ok = False
    else:
        try:
            ok = bool(psapi.EmptyWorkingSet(handle))
            error = ctypes.get_last_error()
            message = (
                "Windows trimmed the process working set."
                if ok
                else f"EmptyWorkingSet failed with Windows error {error}"
            )
        finally:
            kernel32.CloseHandle(handle)
    return ActionResult(
        kind=ActionKind.TRIM_WORKING_SET,
        target=str(pid),
        success=ok,
        started_at=started_at,
        duration_ms=(time.perf_counter() - started) * 1000,
        message=message,
    )

