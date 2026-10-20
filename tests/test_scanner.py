from __future__ import annotations

import os
import time
from pathlib import Path

from conftest import make_file, write_config

from sloth_valet.models import FileCandidate, ScanResult, SkipReason
from sloth_valet.scanner import Scanner


def _reasons(result: ScanResult) -> set[SkipReason]:
    return {item.reason for item in result.skipped}


def test_settled_regular_file_becomes_a_candidate(tmp_path: Path) -> None:
    paths = write_config(tmp_path)
    source = make_file(paths.watch / "invoice.pdf")

    result = Scanner(paths.load()).scan()

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

    result = Scanner(paths.load()).scan()

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

    shallow = Scanner(paths.load()).scan()
    assert [candidate.source for candidate in shallow.candidates] == [top]

    write_config(tmp_path, recursive=True)
    recursive = Scanner(paths.load()).scan()
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

    result = Scanner(paths.load()).scan()

    assert {candidate.source.name for candidate in result.candidates} == {"keep.pdf"}
    assert SkipReason.IGNORED in _reasons(result)


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

    result = Scanner(paths.load()).scan()

    assert {candidate.source for candidate in result.candidates} == {visible}
    assert SkipReason.PROTECTED in _reasons(result)
    assert not {
        candidate.source for candidate in result.candidates
    } & {protected_file, private_dir / "secret.pdf"}


def test_symlinked_files_and_directories_are_never_followed(tmp_path: Path) -> None:
    paths = write_config(tmp_path, recursive=True)
    outside_file = make_file(tmp_path / "outside" / "target.pdf")
    outside_dir = tmp_path / "outside-dir"
    make_file(outside_dir / "nested.pdf")
    (paths.watch / "link.pdf").symlink_to(outside_file)
    (paths.watch / "link-dir").symlink_to(outside_dir, target_is_directory=True)
    visible = make_file(paths.watch / "visible.pdf")

    result = Scanner(paths.load()).scan()

    assert {candidate.source for candidate in result.candidates} == {visible}
    assert len([item for item in result.skipped if item.reason == SkipReason.SYMLINK]) == 2
    assert all(candidate.source != outside_file for candidate in result.candidates)


def test_symlinked_watch_root_is_refused(tmp_path: Path) -> None:
    real_watch = tmp_path / "real-watch"
    real_watch.mkdir()
    make_file(real_watch / "old.pdf")
    alias = tmp_path / "watch-alias"
    alias.symlink_to(real_watch, target_is_directory=True)
    paths = write_config(tmp_path, watch_path=alias)

    result = Scanner(paths.load()).scan()

    assert result.candidates == ()
    assert len(result.errors) == 1
    assert "symlink" in result.errors[0].message


def test_watch_root_that_is_a_file_is_an_error(tmp_path: Path) -> None:
    paths = write_config(tmp_path)
    paths.watch.rmdir()
    paths.watch.write_text("not a directory", encoding="utf-8")

    result = Scanner(paths.load()).scan()

    assert result.candidates == ()
    assert len(result.errors) == 1
    assert "not a directory" in result.errors[0].message


def test_second_stat_change_is_skipped(tmp_path: Path) -> None:
    paths = write_config(tmp_path)
    source = make_file(paths.watch / "changing.pdf", content=b"first")
    config = paths.load()
    scanner = Scanner(config)
    first_stat = source.lstat()

    time.sleep(0.01)
    source.write_bytes(b"second version is longer")
    os.utime(source, (time.time() - 3600, time.time() - 3600))

    candidate, skipped = scanner._inspect_file(
        source,
        first_stat,
        paths.watch,
        config.watch[0],
        set(),
    )

    assert candidate is None
    assert skipped is not None
    assert skipped.reason == SkipReason.CHANGED


def test_special_files_are_skipped(tmp_path: Path) -> None:
    paths = write_config(tmp_path)
    os.mkfifo(paths.watch / "pipe")

    result = Scanner(paths.load()).scan()

    assert result.candidates == ()
    assert SkipReason.NOT_REGULAR in _reasons(result)


def test_scan_is_deterministic_and_does_not_modify_files(tmp_path: Path) -> None:
    paths = write_config(tmp_path)
    first = make_file(paths.watch / "a.pdf", content=b"alpha")
    second = make_file(paths.watch / "b.pdf", content=b"beta")
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (first, second)}

    scanner = Scanner(paths.load())
    one = scanner.scan()
    two = scanner.scan()

    assert one == two
    assert [candidate.source.name for candidate in one.candidates] == ["a.pdf", "b.pdf"]
    after = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (first, second)}
    assert after == before


def test_candidate_fingerprint_detects_size_and_mtime_changes(tmp_path: Path) -> None:
    paths = write_config(tmp_path)
    source = make_file(paths.watch / "fingerprint.pdf")
    candidate = Scanner(paths.load()).scan().candidates[0]
    assert isinstance(candidate, FileCandidate)

    source.write_bytes(b"a different size")
    assert not candidate.matches_stat(source.stat())

    source.write_bytes(b"same size")
    old_mtime = source.stat().st_mtime_ns
    os.utime(source, ns=(old_mtime + 1_000_000, old_mtime + 1_000_000))
    assert not candidate.matches_stat(source.stat())
