from __future__ import annotations

import json
from pathlib import Path

from conftest import make_file, write_config
from typer.testing import CliRunner

from sloth_valet.cli import app

runner = CliRunner()


def test_scan_json_lists_candidates_without_changing_files(tmp_path: Path) -> None:
    paths = write_config(tmp_path)
    source = make_file(paths.watch / "report.pdf", content=b"original")

    result = runner.invoke(
        app,
        ["--config", str(paths.config_path), "scan", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is True
    assert [candidate["path"] for candidate in payload["candidates"]] == [str(source)]
    assert source.read_bytes() == b"original"
    assert not (paths.destination / "misc").exists()


def test_scan_output_is_human_readable(tmp_path: Path) -> None:
    paths = write_config(tmp_path)
    make_file(paths.watch / "report.pdf")

    result = runner.invoke(app, ["--config", str(paths.config_path), "scan"])

    assert result.exit_code == 0, result.output
    assert "Read-only scan" in result.output
    assert "Candidates: 1" in result.output
    assert "<category>" in result.output


def test_missing_config_exits_with_usage_error(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["--config", str(tmp_path / "absent.toml"), "scan"],
    )

    assert result.exit_code == 2


def test_scanner_errors_exit_nonzero(tmp_path: Path) -> None:
    paths = write_config(tmp_path)
    paths.watch.rmdir()

    result = runner.invoke(app, ["--config", str(paths.config_path), "scan"])

    assert result.exit_code == 1
    assert "Errors: 1" in result.output


def test_version_option_needs_no_subcommand() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.output.strip() == "valet 0.1.0"
