from __future__ import annotations

from collections import namedtuple

import sysinfo


_DiskUsage = namedtuple("_DiskUsage", "total used free")


def test_read_hardware_strips_nul(tmp_path):
    p = tmp_path / "model"
    p.write_bytes(b"Raspberry Pi 4 Model B Rev 1.4\x00")
    assert sysinfo.read_hardware(str(p)) == "Raspberry Pi 4 Model B Rev 1.4"


def test_read_hardware_missing_file_falls_back(tmp_path):
    # Missing file → non-empty fallback from platform.machine() or "unknown".
    missing = tmp_path / "nope"
    result = sysinfo.read_hardware(str(missing))
    assert result and isinstance(result, str)


def test_read_os_release_extracts_pretty_name(tmp_path):
    p = tmp_path / "os-release"
    p.write_text(
        'NAME="Raspberry Pi OS"\n'
        'PRETTY_NAME="Raspberry Pi OS (bookworm)"\n'
        "VERSION_ID=12\n"
    )
    assert sysinfo.read_os_release(str(p)) == "Raspberry Pi OS (bookworm)"


def test_read_os_release_handles_unquoted(tmp_path):
    p = tmp_path / "os-release"
    p.write_text("PRETTY_NAME=Debian\n")
    assert sysinfo.read_os_release(str(p)) == "Debian"


def test_read_os_release_missing_falls_back(tmp_path):
    result = sysinfo.read_os_release(str(tmp_path / "nope"))
    assert result and isinstance(result, str)


def test_read_temperature_parses_millicelsius(tmp_path):
    p = tmp_path / "temp"
    p.write_text("48123\n")
    assert sysinfo.read_temperature(str(p)) == "48.1°C"


def test_read_temperature_missing_is_na(tmp_path):
    assert sysinfo.read_temperature(str(tmp_path / "nope")) == "n/a"


def test_read_temperature_garbage_is_na(tmp_path):
    p = tmp_path / "temp"
    p.write_text("not-a-number")
    assert sysinfo.read_temperature(str(p)) == "n/a"


def test_read_loadavg_uses_injected():
    assert sysinfo.read_loadavg(getloadavg=lambda: (0.5, 1.0, 2.0)) == (0.5, 1.0, 2.0)


def test_read_loadavg_oserror_defaults_to_zeros():
    def boom():
        raise OSError("no procfs")

    assert sysinfo.read_loadavg(getloadavg=boom) == (0.0, 0.0, 0.0)


def test_read_disk_free_returns_free_total():
    fake = _DiskUsage(total=1000, used=400, free=600)
    free, total = sysinfo.read_disk_free("/", disk_usage=lambda _: fake)
    assert (free, total) == (600, 1000)


def test_format_bytes_units():
    assert sysinfo.format_bytes(0) == "0.0 B"
    assert sysinfo.format_bytes(1023) == "1023.0 B"
    assert sysinfo.format_bytes(1024) == "1.0 KB"
    assert sysinfo.format_bytes(2 * 1024 * 1024) == "2.0 MB"
    assert sysinfo.format_bytes(5 * 1024 ** 3) == "5.0 GB"
