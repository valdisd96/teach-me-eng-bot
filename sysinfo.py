"""Host diagnostics for the /status command.

Each reader is a pure helper with injectable dependencies so tests can supply
fakes without monkeypatching, and with defensive fallbacks because /status
should never crash just because a sysfs file is missing off-Pi.
"""

from __future__ import annotations

import os
import platform
import shutil
from typing import Callable


def read_hardware(path: str = "/proc/device-tree/model") -> str:
    """Return a human string for the host device (e.g. 'Raspberry Pi 4 ...')."""
    try:
        with open(path, "rb") as f:
            raw = f.read().rstrip(b"\x00")
        return raw.decode("utf-8", errors="replace").strip() or platform.machine() or "unknown"
    except OSError:
        return platform.machine() or "unknown"


def read_os_release(path: str = "/etc/os-release") -> str:
    """Return PRETTY_NAME from os-release, else a platform.platform() fallback."""
    try:
        with open(path) as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return platform.platform()


def read_temperature(path: str = "/sys/class/thermal/thermal_zone0/temp") -> str:
    try:
        with open(path) as f:
            return f"{int(f.read().strip()) / 1000:.1f}°C"
    except (OSError, ValueError):
        return "n/a"


def read_loadavg(
    getloadavg: Callable[[], tuple[float, float, float]] = os.getloadavg,
) -> tuple[float, float, float]:
    try:
        return getloadavg()
    except OSError:
        return (0.0, 0.0, 0.0)


def read_disk_free(
    path: str = "/",
    disk_usage: Callable[[str], shutil._ntuple_diskusage] = shutil.disk_usage,
) -> tuple[int, int]:
    """Return (free_bytes, total_bytes) for the filesystem at `path`."""
    du = disk_usage(path)
    return du.free, du.total


def format_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"
