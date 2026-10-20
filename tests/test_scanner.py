from __future__ import annotations

import os
import time
from pathlib import Path

from conftest import EnvPaths, make_file, write_config

from sloth_valet.models import FileCandidate, ScanResult, SkipReason
from sloth_valet.scanner import Scanner


def _scan(paths: EnvPaths) -> ScanResult:
    return Scanner(paths.load(), stability_delay_s=0.0).scan()


def _reasons(result: ScanResult) -> set[SkipReason]:
    return {item.reason for item in result.skipped}


def test_settled_regular_file_becomes_a_candidate(tmp_path: Path) -> None:
    paths = write_config(tmp_path)
    source = make_file(paths.watch / "invoice.pdf")

    result = _scan(paths)

    assert result.errors == ()
    assert result.skipped == ()
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.source == source
    assert candidate.watch_root == paths.watch
    assert candidate.destination_root == paths.destination
    assert candidate.relative_source == Path("invoice.pdf")
    assert candidate.matches_stat(source.stat())


def test_partial_unsettled_and_hidden_files_wait(tmp_path: Path) -> None:
    paths = write_config(tmp_path)
    make_file(paths.watch / "download.part")
    make_file(paths.watch / "download.crdownload")
    make_file(paths.watch / "scratch.tmp")
    make_file(paths.watch / "UPPER.PART")
    make_file(paths.watch / "fresh.pdf", age_seconds=0)
    make_file(paths.watch / ".hidden.pdf")
    expected = make_file(paths.watch / "ready.pdf")

    result = _scan(paths)

    assert {candidate.source for candidate in result.candidates} == {expected}
    reasons = _reasons(result)
    assert SkipReason.PARTIAL_DOWNLOAD in reasons
    assert SkipReason.UNSETTLED in reasons
    assert SkipReason.IGNORED in reasons
    assert len(result.skipped) == 6


def test_nonrecursive_and_recursive_walks(tmp_path: Path) -> None:
    paths = write_config(tmp_path)
    top = make_file(paths.watch / "top.pdf")
    nested = make_file(paths.watch / "nested" / "deep.pdf")

    shallow = _scan(paths)
    assert [candidate.source for candidate in shallow.candidates] == [top]

    write_config(tmp_path, recursive=True)
    recursive = _scan(paths)
    assert {candidate.source for candidate in recursive.candidates} == {top, nested}


def test_ignore_globs_match_names_components_and_relative_paths(tmp_path: Path) -> None:
    paths = write_config(
        tmp_path,
        recursive=True,
        globs=("*.tmp", ".*", "*.psd", "incoming/*.pdf"),
    )
    make_file(paths.watch / "draft.psd")
    make_file(paths.watch / ".hidden" / "secret.pdf")
    make_file(paths.watch / "incoming" / "packet.pdf")
    make_file(paths.watch / "keep.pdf")

    result = _scan(paths)

    assert {candidate.source.name for candidate in result.candidates} == {"keep.pdf"}
    ignored_paths = {
        item.path for item in result.skipped if item.reason == SkipReason.IGNORED
    }
    assert paths.watch / ".hidden" in ignored_paths
    assert paths.watch / "draft.psd" in ignored_paths


def test_protected_paths_are_checked_before_candidacy(tmp_path: Path) -> None:
    protected_file = tmp_path / "watch" / "keep.pdf"
    private_dir = tmp_path / "watch" / "private"
    paths = write_config(
        tmp_path,
        recursive=True,
        protect_paths=(protected_file, private_dir),
    )
    make_file(protected_file)
    make_file(private_dir / "secret.pdf")
    visible = make_file(paths.watch / "visible.pdf")

    result = _scan(paths)

    assert {candidate.source for candidate in result.candidates} == {visible}
    protected_paths = {
        item.path for item in result.skipped if item.reason == SkipReason.PROTECTED
    }
    assert protected_paths == {protected_file, private_dir}


def test_symlink_path_escape_is_blocked(tmp_path: Path) -> None:
    paths = write_config(tmp_path, recursive=True)
    outside_file = make_file(tmp_path / "outside" / "target.pdf")
    outside_dir = tmp_path / "outside-dir"
    make_file(outside_dir / "nested.pdf")
    file_link = paths.watch / "link.pdf"
    directory_link = paths.watch / "link-dir"
    file_link.symlink_to(outside_file)
    directory_link.symlink_to(outside_dir, target_is_directory=True)
    visible = make_file(paths.watch / "visible.pdf")

    result = _scan(paths)

    assert {candidate.source for candidate in result.candidates} == {visible}
    symlink_skips = {
        item.path for item in result.skipped if item.reason == SkipReason.SYMLINK
    }
    assert symlink_skips == {file_link, directory_link}
    assert all(candidate.source != outside_file for candidate in result.candidates)


def test_symlinked_watch_root_is_refused(tmp_path: Path) -> None:
    real_watch = tmp_path / "real-watch"
    real_watch.mkdir()
    make_file(real_watch / "old.pdf")
    alias = tmp_path / "watch-alias"
    alias.symlink_to(real_watch, target_is_directory=True)
    paths = write_config(tmp_path, watch_path=alias)

    result = _scan(paths)

    assert result.candidates == ()
    assert len(result.errors) == 1
    assert "symlink" in result.errors[0].message


def test_watch_root_that_is_a_file_is_an_error(tmp_path: Path) -> None:
    paths = write_config(tmp_path)
    paths.watch.rmdir()
    paths.watch.write_text("not a directory", encoding="utf-8")

    result = _scan(paths)

    assert result.candidates == ()
    assert len(result.errors) == 1
    assert "not a directory" in result.errors[0].message


def test_size_cap_allows_the_limit_and_skips_larger_files(tmp_path: Path) -> None:
    paths = write_config(tmp_path, max_file_size_mb=1)
    one_mib = 1024 * 1024
    at_limit = make_file(paths.watch / "at-limit.bin", content=b"x" * one_mib)
    too_large = make_file(paths.watch / "too-large.bin", content=b"x" * (one_mib + 1))

    result = _scan(paths)

    assert {candidate.source for candidate in result.candidates} == {at_limit}
    assert [item.path for item in result.skipped] == [too_large]
    assert result.skipped[0].reason == SkipReason.TOO_LARGE


def test_gates_run_in_safety_order(tmp_path: Path) -> None:
    protected_file = tmp_path / "watch" / ".protected-large"
    paths = write_config(
        tmp_path,
        max_file_size_mb=1,
        protect_paths=(protected_file,),
    )
    one_mib = 1024 * 1024
    outside = make_file(tmp_path / "outside.pdf")
    make_file(protected_file, content=b"x" * (one_mib + 1), age_seconds=0)
    ignored_link = paths.watch / ".ignored-link"
    ignored_link.symlink_to(outside)
    fresh_large = make_file(
        paths.watch / "fresh-large.bin",
        content=b"x" * (one_mib + 1),
        age_seconds=0,
    )

    result = _scan(paths)

    assert result.candidates == ()
    reasons = {item.path: item.reason for item in result.skipped}
    assert reasons[protected_file] == SkipReason.PROTECTED
    assert reasons[ignored_link] == SkipReason.IGNORED
    assert reasons[fresh_large] == SkipReason.UNSETTLED


def test_stability_delay_is_injected_and_called_once(tmp_path: Path) -> None:
    paths = write_config(tmp_path)
    make_file(paths.watch / "a.pdf")
    make_file(paths.watch / "b.pdf")
    delays: list[float] = []

    result = Scanner(
        paths.load(),
        stability_delay_s=4.5,
        sleep_func=delays.append,
    ).scan()

    assert delays == [4.5]
    assert len(result.candidates) == 2
    assert result.skipped == ()


def test_file_changed_during_stability_delay_is_skipped(tmp_path: Path) -> None:
    paths = write_config(tmp_path)
    source = make_file(paths.watch / "changing.pdf", content=b"first")

    def change_during_delay(delay: float) -> None:
        assert delay == 5.0
        source.write_bytes(b"second version is longer")
        timestamp = time.time() - 3600
        os.utime(source, (timestamp, timestamp))

    result = Scanner(
        paths.load(),
        stability_delay_s=5.0,
        sleep_func=change_during_delay,
    ).scan()

    assert result.candidates == ()
    assert [(item.path, item.reason) for item in result.skipped] == [
        (source, SkipReason.CHANGED)
    ]


def test_mtime_change_during_stability_delay_is_skipped(tmp_path: Path) -> None:
    paths = write_config(tmp_path)
    source = make_file(paths.watch / "touched.pdf", content=b"same size")
    original = source.stat()

    def touch_during_delay(_delay: float) -> None:
        os.utime(
            source,
            ns=(original.st_atime_ns, original.st_mtime_ns + 1_000_000),
        )

    result = Scanner(
        paths.load(),
        stability_delay_s=5.0,
        sleep_func=touch_during_delay,
    ).scan()

    assert result.candidates == ()
    assert [(item.path, item.reason) for item in result.skipped] == [
        (source, SkipReason.CHANGED)
    ]


def test_special_files_are_skipped(tmp_path: Path) -> None:
    paths = write_config(tmp_path)
    os.mkfifo(paths.watch / "pipe")

    result = _scan(paths)

    assert result.candidates == ()
    assert SkipReason.NOT_REGULAR in _reasons(result)


def test_scan_is_deterministic_and_does_not_modify_files(tmp_path: Path) -> None:
    paths = write_config(tmp_path)
    first = make_file(paths.watch / "a.pdf", content=b"alpha")
    second = make_file(paths.watch / "b.pdf", content=b"beta")
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (first, second)}

    scanner = Scanner(paths.load(), stability_delay_s=0.0)
    one = scanner.scan()
    two = scanner.scan()

    assert one == two
    assert [candidate.source.name for candidate in one.candidates] == ["a.pdf", "b.pdf"]
    after = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (first, second)}
    assert after == before


def test_candidate_fingerprint_detects_size_and_mtime_changes(tmp_path: Path) -> None:
    paths = write_config(tmp_path)
    source = make_file(paths.watch / "fingerprint.pdf")
    candidate = _scan(paths).candidates[0]
    assert isinstance(candidate, FileCandidate)

    source.write_bytes(b"a different size")
    assert not candidate.matches_stat(source.stat())

    source.write_bytes(b"same size")
    old_mtime = source.stat().st_mtime_ns
    os.utime(source, ns=(old_mtime + 1_000_000, old_mtime + 1_000_000))
    assert not candidate.matches_stat(source.stat())
