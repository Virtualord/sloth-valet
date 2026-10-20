"""Command-line entry point for Sloth Valet."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer

from sloth_valet import __version__
from sloth_valet.config import ConfigError, ValetConfig, default_config_path, load_config
from sloth_valet.scanner import Scanner

app = typer.Typer(
    name="valet",
    help="Safely triage settled files with a local open-weight model.",
    no_args_is_help=True,
    add_completion=False,
)


@dataclass(frozen=True, slots=True)
class CliState:
    config_path: Path


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"valet {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    config: Annotated[
        Path | None,
        typer.Option(
            "--config",
            "-c",
            help="TOML config file. Defaults to $SLOTH_VALET_CONFIG or the XDG config path.",
            dir_okay=False,
            resolve_path=True,
        ),
    ] = None,
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True),
    ] = False,
) -> None:
    """Load configuration and dispatch one valet command."""
    del version
    ctx.obj = CliState(config_path=config or default_config_path())


def _load_or_exit(state: CliState) -> ValetConfig:
    try:
        return load_config(state.config_path)
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc


def _format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


@app.command()
def scan(
    ctx: typer.Context,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable scanner output."),
    ] = False,
) -> None:
    """List settled candidates without moving or reading them."""
    state = ctx.obj
    if not isinstance(state, CliState):
        typer.echo("internal error: command state is missing", err=True)
        raise typer.Exit(code=2)
    config = _load_or_exit(state)
    result = Scanner(config).scan()

    if as_json:
        payload = {
            "dry_run": True,
            "candidates": [
                {
                    "path": str(candidate.source),
                    "watch_root": str(candidate.watch_root),
                    "destination_root": str(candidate.destination_root),
                    "size": candidate.size,
                    "mtime_ns": candidate.mtime_ns,
                }
                for candidate in result.candidates
            ],
            "skipped": [
                {
                    "path": str(item.path),
                    "reason": item.reason.value,
                    "detail": item.detail,
                }
                for item in result.skipped
            ],
            "errors": [
                {"path": str(error.path), "message": error.message} for error in result.errors
            ],
        }
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    else:
        typer.echo(
            "Read-only scan. No file contents were read, and no files were moved or changed."
        )
        for candidate in result.candidates:
            destination = candidate.destination_root / "<category>" / "<filename>"
            typer.echo(
                f"  {candidate.source}  ({_format_size(candidate.size)})\n"
                f"    -> {destination}"
            )
        typer.echo(f"Candidates: {len(result.candidates)}")
        if result.skipped:
            counts = Counter(item.reason.value for item in result.skipped)
            summary = ", ".join(f"{reason}={count}" for reason, count in sorted(counts.items()))
            typer.echo(f"Skipped: {len(result.skipped)} ({summary})")
        if result.errors:
            typer.echo(f"Errors: {len(result.errors)}", err=True)
            for error in result.errors:
                typer.echo(f"  {error.path}: {error.message}", err=True)

    if result.errors:
        raise typer.Exit(code=1)


def main_entrypoint() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":
    main_entrypoint()
