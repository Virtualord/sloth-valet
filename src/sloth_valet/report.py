"""Short Markdown morning reports and optional desktop notifications."""

from __future__ import annotations

import os
import subprocess
from datetime import UTC
from pathlib import Path
from typing import TYPE_CHECKING

from sloth_valet.applier import ApplyStatus
from sloth_valet.config import ValetConfig
from sloth_valet.journal import RunMode

if TYPE_CHECKING:
    from sloth_valet.runner import RunResult


def report_markdown(result: RunResult) -> str:
    """Render a report from already-computed run data without trusting model text."""
    total_skipped = len(result.scan.skipped) + len(result.skipped)
    scan_errors = len(result.scan.errors)
    if result.mode is RunMode.DRY_RUN:
        counts = (
            f"Dry run: would move {len(result.planned_moves)}, "
            f"send {len(result.reviewed)} to review, "
            f"leave {total_skipped} untouched, fail {len(result.failed)}"
        )
    else:
        counts = (
            f"Moved {len(result.moved)}, sent {len(result.reviewed)} to review, "
            f"left {total_skipped} untouched, failed {len(result.failed)}"
        )
    if scan_errors:
        counts += f", scan errors {scan_errors}"
    counts += "."

    started = result.started_at.astimezone(UTC)
    lines = [
        "# Sloth Valet report",
        "",
        f"Run `{result.run_id[:8]}` ({result.mode.value}) at "
        f"{started:%Y-%m-%d %H:%M:%S UTC}.",
        counts,
    ]
    if result.pending_repairs:
        lines.append(
            f"Reconciled {len(result.pending_repairs)} pending journal row(s)."
        )
    elif not result.scan.candidates and total_skipped == 0 and scan_errors == 0:
        lines.append("No files were ready.")
    else:
        lines.append(
            f"Scanned {len(result.scan.candidates)} settled candidate(s); "
            f"{total_skipped} path(s) skipped."
        )

    rows = _detail_rows(result)
    lines.extend(["", "## Details", ""])
    if not rows:
        lines.append("No movement or skip details.")
    else:
        lines.extend(
            [
                "| Action | Source | Destination | Reason |",
                "| --- | --- | --- | --- |",
            ]
        )
        lines.extend(
            f"| {_cell(action)} | {_cell(source)} | {_cell(destination)} | "
            f"{_cell(reason)} |"
            for action, source, destination, reason in rows
        )
    lines.extend(["", f"Journal: {_cell(str(result.journal_path))}", ""])
    return "\n".join(lines)


def write_report(
    config: ValetConfig,
    result: RunResult,
) -> Path:
    """Write a new, never-overwritten Markdown report and return its path."""
    reports_dir = config.general.reports_dir
    reports_dir.mkdir(parents=True, exist_ok=True)
    started = result.started_at.astimezone(UTC)
    base_name = f"{started:%Y%m%d-%H%M%S}-{result.run_id[:8]}"
    content = report_markdown(result)
    counter = 0
    while True:
        suffix = "" if counter == 0 else f"-{counter}"
        path = reports_dir / f"{base_name}{suffix}.md"
        try:
            with path.open("x", encoding="utf-8") as report_file:
                report_file.write(content)
                report_file.flush()
                os.fsync(report_file.fileno())
            return path
        except FileExistsError:
            counter += 1


def notify_if_enabled(config: ValetConfig, result: RunResult) -> bool:
    """Send a fixed, count-only notification when configured; never fail the run."""
    if not config.general.notify:
        return False
    if result.mode is RunMode.DRY_RUN:
        summary = (
            f"Dry run: {len(result.planned_moves)} would move, "
            f"{len(result.reviewed)} to review."
        )
    else:
        summary = (
            f"Moved {len(result.moved)}, {len(result.reviewed)} to review, "
            f"{len(result.failed)} failed."
        )
    try:
        completed = subprocess.run(
            ["notify-send", "Sloth Valet", summary],
            check=False,
            timeout=5,
            capture_output=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _detail_rows(result: RunResult) -> list[tuple[str, str, str, str]]:
    rows: list[tuple[str, str, str, str]] = []
    for outcome in result.outcomes:
        move = outcome.move
        destination = str(move.destination)
        if outcome.status is ApplyStatus.MOVED:
            action = "moved"
            if move.classification is not None:
                reason = f"category {move.classification.category}"
            else:
                reason = "moved"
        elif outcome.status is ApplyStatus.REVIEWED:
            action = "review"
            reason = f"review: {move.review_reason or 'classifier failure'}"
        elif outcome.status is ApplyStatus.PLANNED and move.is_review:
            action = "would review"
            reason = f"review: {move.review_reason or 'classifier failure'}"
        elif outcome.status is ApplyStatus.PLANNED:
            action = "would move"
            if move.classification is not None:
                reason = f"dry run; category {move.classification.category}"
            else:
                reason = "dry run"
        else:
            action = "failed"
            reason = outcome.error or "move failed"
        rows.append((action, str(move.source), destination, reason))

    for plan_skip in result.skipped:
        detail = f": {plan_skip.detail}" if plan_skip.detail else ""
        rows.append(
            (
                "left untouched",
                str(plan_skip.candidate.source),
                "",
                f"{plan_skip.reason}{detail}",
            )
        )
    for scan_skip in result.scan.skipped:
        detail = f": {scan_skip.detail}" if scan_skip.detail else ""
        rows.append(
            (
                "left untouched",
                str(scan_skip.path),
                "",
                f"{scan_skip.reason.value}{detail}",
            )
        )
    for error in result.scan.errors:
        rows.append(("scan error", str(error.path), "", error.message))
    for repair in result.pending_repairs:
        rows.append(
            (
                "journal repair",
                f"move {repair.move_id}",
                "",
                repair.detail,
            )
        )
    return rows


def _cell(value: str) -> str:
    """Neutralize Markdown/HTML metacharacters from paths and error text."""
    escaped = (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("`", "'")
        .replace("|", "\\|")
        .replace("\r", " ")
        .replace("\n", " ")
    )
    return escaped
