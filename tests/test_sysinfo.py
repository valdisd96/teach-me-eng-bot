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


def test_read_deploy_sha_returns_short_sha(tmp_path):  # AC1 — short_sha returned as-is
    p = tmp_path / "deploy.json"
    p.write_text('{"short_sha": "abc1234", "sha": "abc1234deadbeef"}')
    assert sysinfo.read_deploy_sha(str(p)) == "abc1234"


def test_read_deploy_sha_falls_back_to_first_7_of_sha(tmp_path):  # AC2 — derive from sha when short_sha absent
    p = tmp_path / "deploy.json"
    p.write_text('{"sha": "abc1234deadbeef"}')
    result = sysinfo.read_deploy_sha(str(p))
    assert result == "abc1234", f"expected first 7 chars of sha, got {result!r}"


def test_read_deploy_sha_missing_file_returns_none(tmp_path):  # AC3 — missing path
    assert sysinfo.read_deploy_sha(str(tmp_path / "nope.json")) is None


def test_read_deploy_sha_invalid_json_returns_none(tmp_path):  # AC4, error: JSON decode error
    p = tmp_path / "deploy.json"
    p.write_text("{not valid json")
    assert sysinfo.read_deploy_sha(str(p)) is None


def test_read_deploy_sha_neither_field_returns_none(tmp_path):  # AC5 — JSON missing both keys
    p = tmp_path / "deploy.json"
    p.write_text('{"timestamp": "2026-05-06T00:00:00Z"}')
    assert sysinfo.read_deploy_sha(str(p)) is None


def test_read_deploy_sha_short_sha_precedence(tmp_path):  # AC6 — short_sha wins over sha
    p = tmp_path / "deploy.json"
    # If precedence were reversed, derived "deadbee" would be returned instead.
    p.write_text('{"short_sha": "feedfac", "sha": "deadbeefcafe1234"}')
    result = sysinfo.read_deploy_sha(str(p))
    assert result == "feedfac", f"short_sha must win; got {result!r}"


def test_read_deploy_sha_empty_file_returns_none(tmp_path):  # edge: empty file
    p = tmp_path / "deploy.json"
    p.write_text("")
    assert sysinfo.read_deploy_sha(str(p)) is None


def test_read_deploy_sha_empty_short_sha_falls_through(tmp_path):  # edge: empty short_sha falls through to sha
    p = tmp_path / "deploy.json"
    p.write_text('{"short_sha": "", "sha": "abc1234deadbeef"}')
    result = sysinfo.read_deploy_sha(str(p))
    assert result == "abc1234", f"empty short_sha should fall through to sha-derived value, got {result!r}"


def test_read_deploy_sha_empty_short_sha_no_sha_is_none(tmp_path):  # edge: empty short_sha + no sha → None
    p = tmp_path / "deploy.json"
    p.write_text('{"short_sha": ""}')
    assert sysinfo.read_deploy_sha(str(p)) is None


def test_read_deploy_sha_short_sha_value_returned_as_is(tmp_path):  # edge: sha < 7 chars not padded
    p = tmp_path / "deploy.json"
    p.write_text('{"sha": "abc"}')
    result = sysinfo.read_deploy_sha(str(p))
    assert result == "abc", f"sha shorter than 7 chars must be returned as-is, got {result!r}"


def test_read_deploy_sha_non_string_returns_none(tmp_path):  # edge: non-string sha/short_sha
    p = tmp_path / "deploy.json"
    p.write_text('{"short_sha": 1234567, "sha": ["a", "b"]}')
    assert sysinfo.read_deploy_sha(str(p)) is None


def test_read_deploy_sha_unreadable_file_returns_none(tmp_path, monkeypatch):  # error: file unreadable (permissions)
    p = tmp_path / "deploy.json"
    p.write_text('{"short_sha": "abc1234"}')

    real_open = open

    def boom(path, *args, **kwargs):
        if str(path) == str(p):
            raise PermissionError("denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", boom)
    assert sysinfo.read_deploy_sha(str(p)) is None
