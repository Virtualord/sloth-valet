"""Command-line entry point for Sloth Valet."""

from __future__ import annotations

import json
import shutil
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer

from sloth_valet import __version__
from sloth_valet.applier import (
    Applier,
    ApplyStatus,
    MoveOutcome,
    UndoOutcome,
    UndoStatus,
)
from sloth_valet.config import (
    ConfigError,
    ValetConfig,
    default_config_path,
    example_config_text,
    load_config,
)
from sloth_valet.journal import Journal
from sloth_valet.runner import RunResult, execute_run
from sloth_valet.scanner import Scanner
from sloth_valet.systemd import (
    TIMER_NAME,
    UnitPaths,
    default_unit_dir,
    install_units,
    installed_schedule,
    installed_state,
    parse_time,
    remove_units,
    systemctl,
)

app = typer.Typer(
    name="valet",
    help="Safely triage settled files with a local open-weight model.",
    no_args_is_help=True,
    add_completion=False,
)
config_app = typer.Typer(
    name="config",
    help="Create or inspect the Sloth Valet configuration.",
    no_args_is_help=True,
)
app.add_typer(config_app, name="config")


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
            help="TOML config file. Defaults to $SLOTH_VALET_CONFIG or "
            "~/.config/sloth-valet/valet.toml.",
            dir_okay=False,
            resolve_path=True,
        ),
    ] = None,
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True),
    ] = False,
) -> None:
    """Set the config path and dispatch one valet command."""
    del version
    ctx.obj = CliState(config_path=config or default_config_path())


def _state_or_exit(ctx: typer.Context) -> CliState:
    state = ctx.obj
    if not isinstance(state, CliState):
        typer.echo("internal error: command state is missing", err=True)
        raise typer.Exit(code=2)
    return state


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


@config_app.command("init")
def config_init(ctx: typer.Context) -> None:
    """Write the example config, refusing to overwrite an existing file."""
    state = _state_or_exit(ctx)
    target = state.config_path
    try:
        example = example_config_text()
    except OSError as exc:
        typer.echo(f"Could not read the bundled example config: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x", encoding="utf-8") as config_file:
            config_file.write(example)
    except FileExistsError as exc:
        typer.echo(f"Config already exists, refusing to overwrite: {target}", err=True)
        raise typer.Exit(code=1) from exc
    except OSError as exc:
        typer.echo(f"Could not write config {target}: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"Wrote {target}")
    typer.echo("Edit the watch roots, destination, and model name before running.")


@config_app.command("show")
def config_show(ctx: typer.Context) -> None:
    """Print the fully resolved effective configuration as JSON."""
    state = _state_or_exit(ctx)
    config = _load_or_exit(state)
    typer.echo(json.dumps(config.model_dump(mode="json"), indent=2, sort_keys=True))


@app.command()
def scan(
    ctx: typer.Context,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable scanner output."),
    ] = False,
) -> None:
    """List settled candidates without moving or reading them."""
    state = _state_or_exit(ctx)
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


@app.command()
def run(
    ctx: typer.Context,
    apply: Annotated[
        bool,
        typer.Option(
            "--apply",
            help="Actually move files. Without this, the config's dry_run setting decides.",
        ),
    ] = False,
) -> None:
    """Classify candidates, show the plan, and optionally apply it."""
    state = _state_or_exit(ctx)
    config = _load_or_exit(state)
    effective_apply = apply or not config.general.dry_run
    result = execute_run(config, apply=effective_apply)
    _print_run_result(result)
    if result.scan.errors or result.failed:
        raise typer.Exit(code=1)


def _print_run_result(result: RunResult) -> None:
    if result.mode.value == "dry-run":
        typer.echo("Dry run: no files were moved and no move rows were journaled.")
    else:
        typer.echo("Apply run: every completed move was journaled.")

    for outcome in result.outcomes:
        _print_move_outcome(outcome)
    for skipped in result.skipped:
        detail = f" ({skipped.detail})" if skipped.detail else ""
        typer.echo(
            f"  left untouched: {skipped.candidate.source} [{skipped.reason}]{detail}"
        )
    for repair in result.pending_repairs:
        typer.echo(
            f"  recovered pending row {repair.move_id}: {repair.status.value} - "
            f"{repair.detail}"
        )

    typer.echo(
        f"Run {result.run_id}: moved={len(result.moved)}, "
        f"review={len(result.reviewed)}, failed={len(result.failed)}, "
        f"skipped={len(result.scan.skipped) + len(result.skipped)}"
    )
    if result.scan.errors:
        typer.echo(f"Scan errors: {len(result.scan.errors)}", err=True)
        for error in result.scan.errors:
            typer.echo(f"  {error.path}: {error.message}", err=True)


def _print_move_outcome(outcome: MoveOutcome) -> None:
    move = outcome.move
    if outcome.status is ApplyStatus.MOVED:
        label = "moved"
    elif outcome.status is ApplyStatus.REVIEWED:
        label = "review"
    elif outcome.status is ApplyStatus.PLANNED:
        label = "would review" if move.is_review else "would move"
    else:
        label = "failed"
    line = f"  {label}: {move.source} -> {move.destination}"
    if outcome.error:
        line += f" ({outcome.error})"
    elif move.is_review and move.review_reason:
        line += f" ({move.review_reason})"
    typer.echo(line)


@app.command()
def undo(
    ctx: typer.Context,
    last: Annotated[
        bool,
        typer.Option("--last", help="Undo the most recent run that has completed moves."),
    ] = False,
    run_id: Annotated[
        str | None,
        typer.Option("--run", help="Undo a specific run id."),
    ] = None,
) -> None:
    """Restore journaled moves only when hashes and original paths still match."""
    if last == (run_id is not None):
        typer.echo("Choose exactly one of --last or --run ID.", err=True)
        raise typer.Exit(code=2)
    state = _state_or_exit(ctx)
    config = _load_or_exit(state)
    journal = Journal(config.journal_path)
    try:
        if last:
            record = journal.last_run(with_done_moves=True)
            if record is None:
                typer.echo("No completed run with restored candidates was found.", err=True)
                raise typer.Exit(code=1)
            target_run_id = record.id
        else:
            if run_id is None:
                typer.echo("A run id is required.", err=True)
                raise typer.Exit(code=2)
            target_run_id = run_id
            record = journal.get_run(target_run_id)
            if record is None:
                typer.echo(f"Run {target_run_id!r} was not found.", err=True)
                raise typer.Exit(code=1)
        outcomes = Applier(config, journal).undo_run(target_run_id)
    finally:
        journal.close()

    _print_undo_outcomes(outcomes)


@app.command()
def report(
    ctx: typer.Context,
    last: Annotated[
        bool,
        typer.Option("--last", help="Print the most recent run report."),
    ] = False,
    run_id: Annotated[
        str | None,
        typer.Option("--run", help="Print the report for a specific run id."),
    ] = None,
) -> None:
    """Print a Markdown report written by a previous run."""
    if last == (run_id is not None):
        typer.echo("Choose exactly one of --last or --run ID.", err=True)
        raise typer.Exit(code=2)
    state = _state_or_exit(ctx)
    config = _load_or_exit(state)
    journal = Journal(config.journal_path)
    try:
        if last:
            record = journal.last_run(with_report=True)
        else:
            if run_id is None:
                typer.echo("A run id is required.", err=True)
                raise typer.Exit(code=2)
            record = journal.get_run(run_id)
    finally:
        journal.close()
    if record is None:
        typer.echo("No matching run with a report was found.", err=True)
        raise typer.Exit(code=1)
    if record.report_path is None:
        typer.echo(f"Run {record.id!r} has no report.", err=True)
        raise typer.Exit(code=1)
    try:
        content = record.report_path.read_text(encoding="utf-8")
    except OSError as exc:
        typer.echo(f"Could not read report {record.report_path}: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(content, nl=False)


@app.command("install-timer")
def install_timer(
    ctx: typer.Context,
    time: Annotated[
        str,
        typer.Option("--time", help="Daily 24-hour HH:MM run time."),
    ] = "03:00",
    executable: Annotated[
        Path | None,
        typer.Option(
            "--valet",
            help="Path to the valet executable used by the systemd service.",
            dir_okay=False,
        ),
    ] = None,
) -> None:
    """Install and enable a daily systemd user timer."""
    try:
        hour, minute = parse_time(time)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    state = _state_or_exit(ctx)
    _load_or_exit(state)
    valet_path = executable
    if valet_path is None:
        discovered = shutil.which("valet")
        valet_path = Path(discovered) if discovered else Path.home() / ".local/bin" / "valet"
    unit_dir = default_unit_dir()
    try:
        service_path, timer_path = install_units(
            unit_dir,
            executable=valet_path,
            config_path=state.config_path,
            hour=hour,
            minute=minute,
        )
    except OSError as exc:
        typer.echo(f"Could not write systemd units: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"Wrote {service_path}")
    typer.echo(f"Wrote {timer_path}")
    reloaded = systemctl("daemon-reload")
    enabled = systemctl("enable", "--now", TIMER_NAME)
    if not reloaded or not enabled:
        typer.echo(
            "Units were written, but systemd could not reload or enable the timer. "
            "Run: systemctl --user daemon-reload && "
            f"systemctl --user enable --now {TIMER_NAME}",
            err=True,
        )
        raise typer.Exit(code=1)
    typer.echo(f"Enabled {TIMER_NAME} for {time.strip()} every day.")


@app.command("uninstall-timer")
def uninstall_timer(ctx: typer.Context) -> None:
    """Disable the timer and remove Sloth Valet's systemd units."""
    _state_or_exit(ctx)
    unit_dir = default_unit_dir()
    disabled = systemctl("disable", "--now", TIMER_NAME)
    try:
        removed = remove_units(unit_dir)
    except OSError as exc:
        typer.echo(f"Could not remove systemd units: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    reloaded = systemctl("daemon-reload")
    if removed:
        for path in removed:
            typer.echo(f"Removed {path}")
    else:
        typer.echo("No Sloth Valet systemd units were installed.")
    if not disabled or not reloaded:
        typer.echo(
            "If the timer was active, finish cleanup with: systemctl --user "
            "daemon-reload",
            err=True,
        )


@app.command()
def status(ctx: typer.Context) -> None:
    """Show config, timer installation, and the most recent run."""
    state = _state_or_exit(ctx)
    config = _load_or_exit(state)
    unit_paths = UnitPaths(default_unit_dir())
    service_installed, timer_installed = installed_state(unit_paths.unit_dir)
    schedule = installed_schedule(unit_paths.unit_dir)
    journal = Journal(config.journal_path)
    try:
        last_run = journal.last_run()
    finally:
        journal.close()

    typer.echo("Sloth Valet")
    typer.echo(f"  config: {state.config_path}")
    typer.echo(f"  model: {config.model.name} at {config.model.host}")
    typer.echo(f"  dry_run: {str(config.general.dry_run).lower()}")
    for watch in config.watch:
        typer.echo(
            f"  watch: {watch.path} -> {watch.destination_root}"
            f" (recursive={str(watch.recursive).lower()})"
        )
    typer.echo(f"  journal: {config.journal_path}")
    if last_run is None:
        typer.echo("  last run: none")
    else:
        typer.echo(
            f"  last run: {last_run.id} ({last_run.mode.value}, "
            f"{last_run.status.value}) moved={last_run.moved_count} "
            f"review={last_run.review_count} skipped={last_run.skipped_count} "
            f"failed={last_run.failed_count}"
        )
        if last_run.report_path is not None:
            suffix = "" if last_run.report_path.exists() else " (missing)"
            typer.echo(f"  report: {last_run.report_path}{suffix}")
    if service_installed and timer_installed:
        typer.echo(f"  timer: installed ({unit_paths.timer})")
        typer.echo(f"  schedule: {schedule or 'unknown'}")
    else:
        typer.echo("  timer: not installed")
        typer.echo("  next: valet install-timer --time 03:00")


def _print_undo_outcomes(outcomes: tuple[UndoOutcome, ...]) -> None:
    if not outcomes:
        typer.echo("No completed moves were found for that run.")
        return
    restored = 0
    for outcome in outcomes:
        if outcome.status is UndoStatus.RESTORED:
            restored += 1
            typer.echo(f"  restored: {outcome.move.destination} -> {outcome.move.source}")
        else:
            typer.echo(
                f"  skipped: {outcome.move.destination} -> {outcome.move.source}"
                f" ({outcome.reason or 'safety check failed'})"
            )
    typer.echo(f"Undo: restored={restored}, skipped={len(outcomes) - restored}")


def main_entrypoint() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":
    main_entrypoint()
