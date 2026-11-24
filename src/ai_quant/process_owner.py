from __future__ import annotations

import ctypes
import os
import socket
from pathlib import Path


def process_identity(pid: int | None = None) -> dict[str, object] | None:
    """Return a process birth identity; inaccessible processes raise, never look dead."""
    pid = os.getpid() if pid is None else pid
    if os.name == "nt":
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            if error == 87:  # ERROR_INVALID_PARAMETER: PID no longer exists.
                return None
            raise OSError(error, "Cannot inspect task process")
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(item) for item in times)):
                raise ctypes.WinError(ctypes.get_last_error())
            # An exited process may still have a handle retained by its parent.
            if times[1].dwHighDateTime or times[1].dwLowDateTime:
                return None
            birth = str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
        finally:
            kernel.CloseHandle(handle)
    else:
        try:
            stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            if stat[0] == "Z":
                return None
            boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            birth = f"{boot}:{stat[19]}"
        except FileNotFoundError:
            return None
    return {"pid": pid, "birth": birth, "host": socket.gethostname()}


def owner_is_alive(owner: dict[str, object]) -> bool:
    if owner.get("host") != socket.gethostname():
        return True  # A different host cannot be safely inspected locally.
    try:
        return process_identity(int(owner["pid"])) == owner
    except (OSError, KeyError, TypeError, ValueError):
        return True
