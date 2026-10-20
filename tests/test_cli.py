from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import make_file, write_config
from typer.testing import CliRunner

from sloth_valet.cli import app
from sloth_valet.config import example_config_text
from sloth_valet.journal import Journal, RunMode

runner = CliRunner()


@pytest.fixture(autouse=True)
def _skip_stability_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sloth_valet.scanner.time.sleep", lambda _seconds: None)


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


def test_run_defaults_to_dry_run_even_with_fast_path(tmp_path: Path) -> None:
    paths = write_config(tmp_path, fast_path={".deb": "installers"})
    source = make_file(paths.watch / "tool.deb")

    result = runner.invoke(app, ["--config", str(paths.config_path), "run"])

    assert result.exit_code == 0, result.output
    assert "Dry run" in result.output
    assert "would move" in result.output
    assert source.exists()
    assert not (paths.destination / "installers" / "tool.deb").exists()
    with Journal(paths.load().journal_path) as journal:
        record = journal.last_run()
        assert record is not None
        assert record.mode is RunMode.DRY_RUN


def test_run_apply_moves_file(tmp_path: Path) -> None:
    paths = write_config(tmp_path, fast_path={".deb": "installers"})
    source = make_file(paths.watch / "tool.deb")

    result = runner.invoke(
        app,
        ["--config", str(paths.config_path), "run", "--apply"],
    )

    assert result.exit_code == 0, result.output
    assert "moved:" in result.output
    assert "moved=1" in result.output
    assert not source.exists()
    assert (paths.destination / "installers" / "tool.deb").exists()


def test_config_dry_run_false_applies_without_flag(tmp_path: Path) -> None:
    paths = write_config(
        tmp_path,
        dry_run=False,
        fast_path={".deb": "installers"},
    )
    source = make_file(paths.watch / "tool.deb")

    result = runner.invoke(app, ["--config", str(paths.config_path), "run"])

    assert result.exit_code == 0, result.output
    assert not source.exists()
    assert (paths.destination / "installers" / "tool.deb").exists()


def test_second_apply_run_is_a_noop(tmp_path: Path) -> None:
    paths = write_config(tmp_path, fast_path={".deb": "installers"})
    source = make_file(paths.watch / "tool.deb")
    destination = paths.destination / "installers" / "tool.deb"

    first = runner.invoke(
        app,
        ["--config", str(paths.config_path), "run", "--apply"],
    )
    second = runner.invoke(
        app,
        ["--config", str(paths.config_path), "run", "--apply"],
    )

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert not source.exists()
    assert destination.exists()
    assert "moved=0" in second.output


def test_undo_last_restores_file_and_journal_status(tmp_path: Path) -> None:
    paths = write_config(tmp_path, fast_path={".deb": "installers"})
    source = make_file(paths.watch / "tool.deb")

    applied = runner.invoke(
        app,
        ["--config", str(paths.config_path), "run", "--apply"],
    )
    undone = runner.invoke(
        app,
        ["--config", str(paths.config_path), "undo", "--last"],
    )

    assert applied.exit_code == 0, applied.output
    assert undone.exit_code == 0, undone.output
    assert "restored=1" in undone.output
    assert source.exists()
    assert not (paths.destination / "installers" / "tool.deb").exists()
    with Journal(paths.load().journal_path) as journal:
        record = journal.last_run()
        assert record is not None
        moves = journal.moves_for_run(record.id)
        assert moves[0].status.value == "reverted"


def test_undo_by_run_id(tmp_path: Path) -> None:
    paths = write_config(tmp_path, fast_path={".deb": "installers"})
    source = make_file(paths.watch / "tool.deb")
    applied = runner.invoke(
        app,
        ["--config", str(paths.config_path), "run", "--apply"],
    )
    assert applied.exit_code == 0, applied.output
    with Journal(paths.load().journal_path) as journal:
        record = journal.last_run()
        assert record is not None
        run_id = record.id

    undone = runner.invoke(
        app,
        ["--config", str(paths.config_path), "undo", "--run", run_id],
    )

    assert undone.exit_code == 0, undone.output
    assert source.exists()


def test_undo_refuses_to_overwrite_occupied_original_path(tmp_path: Path) -> None:
    paths = write_config(tmp_path, fast_path={".deb": "installers"})
    source = make_file(paths.watch / "tool.deb")
    applied = runner.invoke(
        app,
        ["--config", str(paths.config_path), "run", "--apply"],
    )
    assert applied.exit_code == 0, applied.output
    source.write_text("new file", encoding="utf-8")

    undone = runner.invoke(
        app,
        ["--config", str(paths.config_path), "undo", "--last"],
    )

    assert undone.exit_code == 0, undone.output
    assert "skipped=1" in undone.output
    assert source.read_text(encoding="utf-8") == "new file"
    assert (paths.destination / "installers" / "tool.deb").exists()


def test_undo_requires_exactly_one_selector(tmp_path: Path) -> None:
    paths = write_config(tmp_path)

    neither = runner.invoke(app, ["--config", str(paths.config_path), "undo"])
    both = runner.invoke(
        app,
        [
            "--config",
            str(paths.config_path),
            "undo",
            "--last",
            "--run",
            "abc",
        ],
    )

    assert neither.exit_code == 2
    assert both.exit_code == 2


def test_undo_unknown_run_exits_nonzero(tmp_path: Path) -> None:
    paths = write_config(tmp_path)

    result = runner.invoke(
        app,
        ["--config", str(paths.config_path), "undo", "--run", "missing"],
    )

    assert result.exit_code == 1
    assert "not found" in result.output


def test_report_last_prints_the_markdown_report(tmp_path: Path) -> None:
    paths = write_config(tmp_path, fast_path={".deb": "installers"})
    make_file(paths.watch / "tool.deb")
    applied = runner.invoke(
        app,
        ["--config", str(paths.config_path), "run", "--apply"],
    )
    assert applied.exit_code == 0, applied.output

    result = runner.invoke(
        app,
        ["--config", str(paths.config_path), "report", "--last"],
    )

    assert result.exit_code == 0, result.output
    assert "# Sloth Valet report" in result.output
    assert "Moved 1" in result.output
    assert "| moved |" in result.output


def test_report_by_run_id(tmp_path: Path) -> None:
    paths = write_config(tmp_path)
    dry = runner.invoke(
        app,
        ["--config", str(paths.config_path), "run"],
    )
    assert dry.exit_code == 0, dry.output
    with Journal(paths.load().journal_path) as journal:
        record = journal.last_run(with_report=True)
        assert record is not None
        run_id = record.id

    result = runner.invoke(
        app,
        ["--config", str(paths.config_path), "report", "--run", run_id],
    )

    assert result.exit_code == 0, result.output
    assert "Dry run: would move 0" in result.output


def test_report_requires_exactly_one_selector(tmp_path: Path) -> None:
    paths = write_config(tmp_path)

    neither = runner.invoke(
        app,
        ["--config", str(paths.config_path), "report"],
    )
    both = runner.invoke(
        app,
        [
            "--config",
            str(paths.config_path),
            "report",
            "--last",
            "--run",
            "abc",
        ],
    )

    assert neither.exit_code == 2
    assert both.exit_code == 2
