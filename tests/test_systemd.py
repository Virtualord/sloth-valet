from __future__ import annotations

from pathlib import Path

import pytest
from conftest import write_config
from typer.testing import CliRunner

from sloth_valet.cli import app
from sloth_valet.systemd import (
    SERVICE_NAME,
    TIMER_NAME,
    default_unit_dir,
    install_units,
    installed_schedule,
    installed_state,
    parse_time,
    remove_units,
    render_service,
    render_timer,
)

runner = CliRunner()


def test_parse_time_accepts_24_hour_clock_values() -> None:
    assert parse_time("03:00") == (3, 0)
    assert parse_time("3:05") == (3, 5)
    assert parse_time("23:59") == (23, 59)


@pytest.mark.parametrize("value", ["24:00", "3:0", "noon", "", "12:60", "-1:00"])
def test_parse_time_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValueError, match="HH:MM"):
        parse_time(value)


def test_timer_renders_persistent_daily_calendar() -> None:
    content = render_timer(3, 0)

    assert f"Requires={SERVICE_NAME}" in content
    assert "OnCalendar=*-*-* 03:00:00" in content
    assert "Persistent=true" in content
    assert f"Unit={SERVICE_NAME}" in content
    assert "WantedBy=timers.target" in content


def test_service_renders_applied_run_and_low_priority_settings() -> None:
    content = render_service(
        Path("/home/ada/.local/bin/valet"),
        Path("/home/ada/.config/sloth-valet/valet.toml"),
    )

    assert "Type=oneshot" in content
    assert (
        "ExecStart=/home/ada/.local/bin/valet --config "
        "/home/ada/.config/sloth-valet/valet.toml run --apply"
    ) in content
    assert "WorkingDirectory=%h" in content
    assert "Nice=10" in content
    assert "IOSchedulingClass=idle" in content
    assert "NoNewPrivileges=true" in content
    assert "UMask=0077" in content
    assert "TimeoutStartSec=2h" in content
    assert "StandardOutput=journal" in content


def test_executable_and_config_paths_with_spaces_are_quoted() -> None:
    content = render_service(
        Path("/home/ada/My Apps/valet"),
        Path("/home/ada/My Config/valet.toml"),
    )

    assert '"/home/ada/My Apps/valet"' in content
    assert '"/home/ada/My Config/valet.toml"' in content


def test_install_units_is_idempotent_and_updates_the_schedule(tmp_path: Path) -> None:
    unit_dir = tmp_path / "systemd" / "user"
    service, timer = install_units(
        unit_dir,
        executable=Path("/usr/bin/valet"),
        config_path=tmp_path / "valet.toml",
        hour=3,
        minute=0,
    )
    first_timer = timer.read_text(encoding="utf-8")

    install_units(
        unit_dir,
        executable=Path("/usr/bin/valet"),
        config_path=tmp_path / "valet.toml",
        hour=4,
        minute=15,
    )

    assert service.is_file()
    assert "03:00:00" in first_timer
    assert "04:15:00" in timer.read_text(encoding="utf-8")
    assert installed_state(unit_dir) == (True, True)
    assert installed_schedule(unit_dir) == "*-*-* 04:15:00"


def test_installed_state_and_remove_units_only_touch_known_files(tmp_path: Path) -> None:
    unit_dir = tmp_path / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    unrelated = unit_dir / "other.service"
    unrelated.write_text("keep me", encoding="utf-8")
    install_units(
        unit_dir,
        executable=Path("/usr/bin/valet"),
        config_path=tmp_path / "valet.toml",
        hour=3,
        minute=0,
    )

    removed = remove_units(unit_dir)

    assert {path.name for path in removed} == {SERVICE_NAME, TIMER_NAME}
    assert unrelated.read_text(encoding="utf-8") == "keep me"
    assert installed_state(unit_dir) == (False, False)
    assert remove_units(unit_dir) == ()


def test_install_refuses_to_replace_a_symlinked_unit(tmp_path: Path) -> None:
    unit_dir = tmp_path / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    target = tmp_path / "outside.service"
    target.write_text("outside", encoding="utf-8")
    (unit_dir / SERVICE_NAME).symlink_to(target)

    with pytest.raises(OSError, match="symlinked"):
        install_units(
            unit_dir,
            executable=Path("/usr/bin/valet"),
            config_path=tmp_path / "valet.toml",
            hour=3,
            minute=0,
        )

    assert target.read_text(encoding="utf-8") == "outside"


def test_cli_install_status_and_uninstall_without_real_systemctl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = write_config(tmp_path)
    xdg_config = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))
    calls: list[tuple[str, ...]] = []

    def fake_systemctl(*arguments: str) -> bool:
        calls.append(arguments)
        return True

    monkeypatch.setattr("sloth_valet.cli.systemctl", fake_systemctl)

    installed = runner.invoke(
        app,
        [
            "--config",
            str(paths.config_path),
            "install-timer",
            "--time",
            "04:15",
            "--valet",
            "/usr/bin/valet",
        ],
    )
    status_result = runner.invoke(
        app,
        ["--config", str(paths.config_path), "status"],
    )
    status_calls = list(calls)
    uninstalled = runner.invoke(
        app,
        ["--config", str(paths.config_path), "uninstall-timer"],
    )

    unit_dir = xdg_config / "systemd" / "user"
    assert installed.exit_code == 0, installed.output
    assert status_result.exit_code == 0, status_result.output
    assert uninstalled.exit_code == 0, uninstalled.output
    assert (unit_dir / TIMER_NAME).exists() is False
    assert "OnCalendar=*-*-* 04:15:00" not in status_result.output
    assert "timer: installed" in status_result.output
    assert "schedule: *-*-* 04:15:00" in status_result.output
    assert status_calls == [
        ("daemon-reload",),
        ("enable", "--now", TIMER_NAME),
    ]
    assert calls[-2:] == [("disable", "--now", TIMER_NAME), ("daemon-reload",)]
    assert "Wrote" in installed.output
    assert "Enabled" in installed.output


def test_cli_install_timer_rejects_invalid_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = write_config(tmp_path)
    xdg_config = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))

    result = runner.invoke(
        app,
        [
            "--config",
            str(paths.config_path),
            "install-timer",
            "--time",
            "25:00",
        ],
    )

    assert result.exit_code == 2
    assert "HH:MM" in result.output
    assert not (xdg_config / "systemd").exists()


def test_cli_status_without_timer_does_not_require_systemctl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = write_config(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(
        "sloth_valet.cli.systemctl",
        lambda *_args: pytest.fail("status must not call systemctl"),
    )

    result = runner.invoke(app, ["--config", str(paths.config_path), "status"])

    assert result.exit_code == 0, result.output
    assert "timer: not installed" in result.output
    assert "last run: none" in result.output


def test_default_unit_dir_honors_xdg_config_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

    assert default_unit_dir() == tmp_path / "xdg" / "systemd" / "user"
