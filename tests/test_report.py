from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import make_file, write_config

from sloth_valet.report import notify_if_enabled, write_report
from sloth_valet.runner import execute_run


def test_zero_candidate_report_has_short_summary_and_no_details(tmp_path: Path) -> None:
    paths = write_config(tmp_path)

    result = execute_run(paths.load(), apply=False, stability_delay_s=0)

    assert result.report_path is not None
    content = result.report_path.read_text(encoding="utf-8")
    assert content.startswith("# Sloth Valet report\n")
    assert "Dry run: would move 0" in content
    assert "No files were ready." in content
    assert "No movement or skip details." in content
    assert "## Details" in content


def test_all_skipped_report_lists_the_skip_reason(tmp_path: Path) -> None:
    paths = write_config(tmp_path)
    partial = make_file(paths.watch / "download.part", content=b"still downloading")

    result = execute_run(paths.load(), apply=True, stability_delay_s=0)

    assert result.report_path is not None
    content = result.report_path.read_text(encoding="utf-8")
    assert "Moved 0" in content
    assert "left 1 untouched" in content
    assert "left untouched" in content
    assert "partial_download" in content
    assert str(partial) in content
    assert "No movement or skip details." not in content


def test_mixed_report_contains_moved_and_skipped_rows_and_escapes_paths(
    tmp_path: Path,
) -> None:
    paths = write_config(tmp_path, fast_path={".deb": "installers"})
    source = make_file(paths.watch / "tool|pipe<.deb")
    make_file(paths.watch / "partial.part", content=b"partial")

    result = execute_run(paths.load(), apply=True, stability_delay_s=0)

    assert result.report_path is not None
    content = result.report_path.read_text(encoding="utf-8")
    assert "Moved 1" in content
    assert "left 1 untouched" in content
    assert "| moved |" in content
    assert "tool\\|pipe&lt;.deb" in content
    assert "partial_download" in content
    assert source.exists() is False


def test_report_file_is_never_overwritten(tmp_path: Path) -> None:
    paths = write_config(tmp_path)
    result = execute_run(paths.load(), apply=False, stability_delay_s=0)
    assert result.report_path is not None

    second_path = write_report(paths.load(), result)

    assert second_path != result.report_path
    assert second_path.name.endswith("-1.md")
    assert result.report_path.exists()
    assert second_path.exists()


def test_notify_send_runs_only_when_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = write_config(
        tmp_path,
        fast_path={".deb": "installers"},
        notify=True,
    )
    make_file(paths.watch / "tool.deb")
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(args: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("sloth_valet.report.subprocess.run", fake_run)

    result = execute_run(paths.load(), apply=True, stability_delay_s=0)
    sent = notify_if_enabled(paths.load(), result)

    assert sent is True
    assert len(calls) == 2
    command, kwargs = calls[0]
    assert command[:2] == ["notify-send", "Sloth Valet"]
    assert "Moved 1" in command[2]
    assert "tool.deb" not in command[2]
    assert "shell" not in kwargs


def test_notify_failure_never_breaks_a_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = write_config(tmp_path, notify=True)
    make_file(paths.watch / "download.part")

    def missing_notify(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError("notify-send is not installed")

    monkeypatch.setattr("sloth_valet.report.subprocess.run", missing_notify)

    result = execute_run(paths.load(), apply=False, stability_delay_s=0)

    assert notify_if_enabled(paths.load(), result) is False
    assert result.report_path is not None
