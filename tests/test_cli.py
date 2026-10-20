from __future__ import annotations

import json
from pathlib import Path

from conftest import make_file, write_config
from typer.testing import CliRunner

from sloth_valet.cli import app
from sloth_valet.config import example_config_text

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


def test_config_init_writes_example_and_refuses_overwrite(tmp_path: Path) -> None:
    target = tmp_path / "new" / "valet.toml"

    first = runner.invoke(app, ["--config", str(target), "config", "init"])

    assert first.exit_code == 0, first.output
    assert target.read_text(encoding="utf-8") == example_config_text()
    assert str(target) in first.output

    target.write_text("sentinel\n", encoding="utf-8")
    second = runner.invoke(app, ["--config", str(target), "config", "init"])

    assert second.exit_code == 1
    assert target.read_text(encoding="utf-8") == "sentinel\n"


def test_config_show_prints_effective_config(tmp_path: Path) -> None:
    paths = write_config(tmp_path)

    result = runner.invoke(
        app,
        ["--config", str(paths.config_path), "config", "show"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["general"]["dry_run"] is True
    assert payload["watch"][0]["path"] == str(paths.watch)
    assert payload["watch"][0]["destination_root"] == str(paths.destination)
    assert payload["general"]["reports_dir"] == str(paths.reports_dir)


def test_config_show_rejects_invalid_config(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.toml"
    invalid.write_text("[general\n", encoding="utf-8")

    result = runner.invoke(app, ["--config", str(invalid), "config", "show"])

    assert result.exit_code == 2
