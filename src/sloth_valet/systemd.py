"""Render and install systemd user units without embedding a shell."""

from __future__ import annotations

import os
import re
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

SERVICE_NAME = "sloth-valet.service"
TIMER_NAME = "sloth-valet.timer"
UNIT_DESCRIPTION = "Sloth Valet unattended file triage"
_SAFE_EXEC_ARGUMENT = re.compile(r"^[A-Za-z0-9_@+=:,./-]+$")
_TIME_PATTERN = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_CALENDAR_PATTERN = re.compile(r"^OnCalendar=(.+)$", re.MULTILINE)


def default_unit_dir() -> Path:
    """Return the systemd user-unit directory, honoring XDG_CONFIG_HOME."""
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    base = (
        Path(xdg_config_home).expanduser()
        if xdg_config_home
        else Path.home() / ".config"
    )
    return base / "systemd" / "user"


def parse_time(value: str) -> tuple[int, int]:
    """Parse a 24-hour HH:MM clock time for OnCalendar."""
    match = _TIME_PATTERN.fullmatch(value.strip())
    if match is None:
        raise ValueError("time must be a 24-hour HH:MM value such as 03:00")
    return int(match.group(1)), int(match.group(2))


def format_time(hour: int, minute: int) -> str:
    return f"{hour:02d}:{minute:02d}"


def render_service(executable: Path, config_path: Path) -> str:
    """Render a oneshot service that runs an applied pass with one config file."""
    exec_start = " ".join(
        [
            _quote_exec_argument(str(executable)),
            "--config",
            _quote_exec_argument(str(config_path)),
            "run",
            "--apply",
        ]
    )
    return (
        "[Unit]\n"
        f"Description={UNIT_DESCRIPTION}\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "WorkingDirectory=%h\n"
        f"ExecStart={exec_start}\n"
        "TimeoutStartSec=2h\n"
        "Nice=10\n"
        "IOSchedulingClass=idle\n"
        "NoNewPrivileges=true\n"
        "PrivateTmp=true\n"
        "UMask=0077\n"
        "StandardOutput=journal\n"
        "StandardError=journal\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def render_timer(hour: int, minute: int) -> str:
    """Render a persistent daily timer for the service."""
    clock = format_time(hour, minute)
    return (
        "[Unit]\n"
        "Description=Daily Sloth Valet run\n"
        f"Requires={SERVICE_NAME}\n"
        "\n"
        "[Timer]\n"
        f"OnCalendar=*-*-* {clock}:00\n"
        "Persistent=true\n"
        f"Unit={SERVICE_NAME}\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )


def install_units(
    unit_dir: Path,
    *,
    executable: Path,
    config_path: Path,
    hour: int,
    minute: int,
) -> tuple[Path, Path]:
    """Write both units atomically and idempotently."""
    unit_dir.mkdir(parents=True, exist_ok=True)
    service_path = unit_dir / SERVICE_NAME
    timer_path = unit_dir / TIMER_NAME
    _write_unit(service_path, render_service(executable, config_path))
    _write_unit(timer_path, render_timer(hour, minute))
    return service_path, timer_path


def remove_units(unit_dir: Path) -> tuple[Path, ...]:
    """Remove only Sloth Valet's two known unit files."""
    removed: list[Path] = []
    for name in (TIMER_NAME, SERVICE_NAME):
        path = unit_dir / name
        if path.is_symlink() or path.exists():
            path.unlink()
            removed.append(path)
    return tuple(removed)


def installed_state(unit_dir: Path) -> tuple[bool, bool]:
    """Return whether the service and timer files are present."""
    return (unit_dir / SERVICE_NAME).is_file(), (unit_dir / TIMER_NAME).is_file()


def installed_schedule(unit_dir: Path) -> str | None:
    """Read the installed OnCalendar value, if the timer file is readable."""
    timer_path = unit_dir / TIMER_NAME
    try:
        content = timer_path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = _CALENDAR_PATTERN.search(content)
    return match.group(1).strip() if match is not None else None


def systemctl(*arguments: str) -> bool:
    """Run one systemctl --user command without a shell; best-effort boolean."""
    try:
        completed = subprocess.run(
            ["systemctl", "--user", *arguments],
            check=False,
            timeout=30,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _write_unit(path: Path, content: str) -> None:
    if path.is_symlink():
        raise OSError(f"refusing to replace symlinked unit file: {path}")
    if path.exists():
        try:
            if path.read_text(encoding="utf-8") == content:
                return
        except OSError:
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as unit_file:
            unit_file.write(content)
            unit_file.flush()
            os.fsync(unit_file.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _quote_exec_argument(value: str) -> str:
    if _SAFE_EXEC_ARGUMENT.fullmatch(value) and "%" not in value and "$" not in value:
        return value
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("%", "%%")
        .replace("$", "$$")
    )
    return f'"{escaped}"'


@dataclass(frozen=True, slots=True)
class UnitPaths:
    """Convenience view for status and tests."""

    unit_dir: Path

    @property
    def service(self) -> Path:
        return self.unit_dir / SERVICE_NAME

    @property
    def timer(self) -> Path:
        return self.unit_dir / TIMER_NAME
