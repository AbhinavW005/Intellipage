from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path


def is_windows() -> bool:
    return os.name == "nt"


def is_admin() -> bool:
    if not is_windows():
        return os.geteuid() == 0 if hasattr(os, "geteuid") else False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def try_enable_debug_privilege() -> bool:
    """Enable SeDebugPrivilege for an already elevated monitor process.

    This only changes the current process token and is used to improve access
    to permitted process counters. It never grants access to another user or
    bypasses Windows security policy.
    """
    if not is_windows() or not is_admin():
        return False
    try:
        class LUID(ctypes.Structure):
            _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

        class LUID_AND_ATTRIBUTES(ctypes.Structure):
            _fields_ = [("Luid", LUID), ("Attributes", wintypes.DWORD)]

        class TOKEN_PRIVILEGES(ctypes.Structure):
            _fields_ = [
                ("PrivilegeCount", wintypes.DWORD),
                ("Privileges", LUID_AND_ATTRIBUTES * 1),
            ]

        token = wintypes.HANDLE()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.LookupPrivilegeValueW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            ctypes.POINTER(LUID),
        ]
        advapi32.LookupPrivilegeValueW.restype = wintypes.BOOL
        advapi32.AdjustTokenPrivileges.argtypes = [
            wintypes.HANDLE,
            wintypes.BOOL,
            ctypes.POINTER(TOKEN_PRIVILEGES),
            wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        advapi32.AdjustTokenPrivileges.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        TOKEN_ADJUST_PRIVILEGES = 0x0020
        TOKEN_QUERY = 0x0008
        SE_PRIVILEGE_ENABLED = 0x00000002
        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(),
            TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
            ctypes.byref(token),
        ):
            return False
        try:
            luid = LUID()
            if not advapi32.LookupPrivilegeValueW(None, "SeDebugPrivilege", ctypes.byref(luid)):
                return False
            privileges = TOKEN_PRIVILEGES()
            privileges.PrivilegeCount = 1
            privileges.Privileges[0].Luid = luid
            privileges.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED
            ctypes.set_last_error(0)
            if not advapi32.AdjustTokenPrivileges(
                token, False, ctypes.byref(privileges), 0, None, None
            ):
                return False
            return ctypes.get_last_error() == 0
        finally:
            kernel32.CloseHandle(token)
    except (AttributeError, OSError):
        return False


def relaunch_as_admin(argv: list[str] | None = None) -> bool:
    """Ask Windows UAC to relaunch this command. Returns True if dispatched."""
    if not is_windows() or is_admin():
        return False
    args = list(sys.argv[1:] if argv is None else argv)
    if getattr(sys, "frozen", False):
        executable = sys.executable
        parameters = subprocess.list2cmdline(args)
    else:
        executable = sys.executable
        parameters = subprocess.list2cmdline(_python_relaunch_arguments(args))
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.ShellExecuteW.argtypes = [
        wintypes.HWND,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        ctypes.c_int,
    ]
    shell32.ShellExecuteW.restype = wintypes.HINSTANCE
    result = shell32.ShellExecuteW(
        None, "runas", executable, parameters, None, 1
    )
    return result is not None and int(result) > 32


def _python_relaunch_arguments(
    args: list[str], entry_point: str | Path | None = None
) -> list[str]:
    """Build a valid elevated command for scripts, ``-m``, and console entry points."""
    entry = Path(entry_point or sys.argv[0]).resolve()
    if entry.suffix.lower() == ".py" and entry.name.lower() != "__main__.py":
        return [str(entry), *args]
    return ["-m", "intellipage", *args]


def ensure_admin(*, auto_elevate: bool, argv: list[str] | None = None) -> bool:
    if not is_windows() or is_admin():
        return True
    if auto_elevate and relaunch_as_admin(argv):
        return False
    raise PermissionError(
        "Administrator privileges are required. Reopen IntelliPage as administrator."
    )



