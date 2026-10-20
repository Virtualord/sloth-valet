"""Read-only discovery of safe, settled candidates for classification."""

from __future__ import annotations

import fnmatch
import os
import stat
import time
from collections.abc import Callable, Iterable
from pathlib import Path

from sloth_valet.config import ValetConfig, WatchConfig
from sloth_valet.models import (
    Candidate,
    ScanError,
    ScanResult,
    SkippedPath,
    SkipReason,
)

PARTIAL_SUFFIXES = (".part", ".crdownload", ".tmp")


def is_partial_download(path: Path) -> bool:
    """Recognize in-progress downloads before spending any model time on them."""
    return path.name.casefold().endswith(PARTIAL_SUFFIXES)


class Scanner:
    """Find trustworthy files without opening them or contacting a model."""

    def __init__(self, config: ValetConfig, *, clock: Callable[[], float] = time.time) -> None:
        self._config = config
        self._clock = clock
        self._protected_paths = tuple(
            _resolve_for_comparison(path) for path in config.protect.paths
        )
        self._destination_roots = tuple(
            _resolve_for_comparison(watch.destination_root) for watch in config.watch
        )

    def scan(self) -> ScanResult:
        candidates: list[Candidate] = []
        skipped: list[SkippedPath] = []
        errors: list[ScanError] = []
        seen_sources: set[Path] = set()

        for watch in self._config.watch:
            watch_candidates, watch_skipped, watch_errors = self._scan_watch(watch, seen_sources)
            candidates.extend(watch_candidates)
            skipped.extend(watch_skipped)
            errors.extend(watch_errors)

        return ScanResult(
            candidates=tuple(sorted(candidates, key=lambda candidate: candidate.source)),
            skipped=tuple(sorted(skipped, key=lambda item: (item.path, item.reason))),
            errors=tuple(sorted(errors, key=lambda item: (item.path, item.message))),
        )

    def _scan_watch(
        self,
        watch: WatchConfig,
        seen_sources: set[Path],
    ) -> tuple[list[Candidate], list[SkippedPath], list[ScanError]]:
        candidates: list[Candidate] = []
        skipped: list[SkippedPath] = []
        errors: list[ScanError] = []

        try:
            if watch.path.is_symlink():
                raise OSError("watch root is a symlink")
            watch_root = watch.path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            errors.append(ScanError(watch.path, f"cannot use watch root: {exc}"))
            return candidates, skipped, errors

        if not watch_root.is_dir():
            errors.append(ScanError(watch.path, "watch root is not a directory"))
            return candidates, skipped, errors

        directories = [watch_root]
        while directories:
            directory = directories.pop(0)
            try:
                directory_fd = _open_directory_without_symlinks(directory, watch_root)
            except OSError as exc:
                errors.append(ScanError(directory, f"cannot read directory: {exc}"))
                continue

            try:
                with os.scandir(directory_fd) as directory_entries:
                    entries = sorted(directory_entries, key=lambda entry: entry.name)
                    for entry in entries:
                        path = directory / entry.name
                        try:
                            if entry.is_symlink():
                                skipped.append(SkippedPath(path, SkipReason.SYMLINK))
                                continue
                            if entry.is_dir(follow_symlinks=False):
                                if self._directory_should_be_pruned(path, watch_root):
                                    continue
                                if watch.recursive:
                                    directories.append(path)
                                continue
                            if not entry.is_file(follow_symlinks=False):
                                skipped.append(SkippedPath(path, SkipReason.NOT_REGULAR))
                                continue
                            candidate, skip = self._inspect_file(
                                path,
                                entry.stat(follow_symlinks=False),
                                watch_root,
                                watch,
                                seen_sources,
                            )
                            if candidate is not None:
                                candidates.append(candidate)
                            if skip is not None:
                                skipped.append(skip)
                        except OSError as exc:
                            skipped.append(
                                SkippedPath(path, SkipReason.UNREADABLE, str(exc))
                            )
            finally:
                os.close(directory_fd)

        return candidates, skipped, errors

    def _directory_should_be_pruned(self, path: Path, watch_root: Path) -> bool:
        relative = path.relative_to(watch_root)
        return (
            self._is_protected(path)
            or self._is_inside_destination(path)
            or self._is_ignored(path, watch_root, relative)
        )

    def _inspect_file(
        self,
        path: Path,
        first_stat: os.stat_result,
        watch_root: Path,
        watch: WatchConfig,
        seen_sources: set[Path],
    ) -> tuple[Candidate | None, SkippedPath | None]:
        relative = path.relative_to(watch_root)
        if is_partial_download(path):
            return None, SkippedPath(path, SkipReason.PARTIAL_DOWNLOAD)
        if self._is_ignored(path, watch_root, relative):
            return None, SkippedPath(path, SkipReason.IGNORED)
        if self._is_protected(path):
            return None, SkippedPath(path, SkipReason.PROTECTED)
        if self._is_inside_destination(path):
            return None, SkippedPath(path, SkipReason.INSIDE_DESTINATION)

        settle_seconds = self._config.general.settle_minutes * 60
        if self._clock() - first_stat.st_mtime_ns / 1_000_000_000 < settle_seconds:
            return None, SkippedPath(path, SkipReason.UNSETTLED)

        try:
            final_stat = path.lstat()
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            return None, SkippedPath(path, SkipReason.UNREADABLE, str(exc))

        if stat.S_ISLNK(final_stat.st_mode) or resolved != path:
            return None, SkippedPath(path, SkipReason.SYMLINK)
        if not stat.S_ISREG(final_stat.st_mode):
            return None, SkippedPath(path, SkipReason.NOT_REGULAR)
        if not resolved.is_relative_to(watch_root):
            return None, SkippedPath(path, SkipReason.OUTSIDE_WATCH)
        if self._is_protected(resolved):
            return None, SkippedPath(path, SkipReason.PROTECTED)
        if self._is_inside_destination(resolved):
            return None, SkippedPath(path, SkipReason.INSIDE_DESTINATION)

        fingerprint = Candidate(
            source=resolved,
            watch_root=watch_root,
            destination_root=watch.destination_root,
            size=final_stat.st_size,
            mtime_ns=final_stat.st_mtime_ns,
            device=final_stat.st_dev,
            inode=final_stat.st_ino,
        )
        if not fingerprint.matches_stat(first_stat):
            return None, SkippedPath(path, SkipReason.CHANGED)

        if resolved in seen_sources:
            return None, SkippedPath(path, SkipReason.DUPLICATE)
        seen_sources.add(resolved)
        return fingerprint, None

    def _is_protected(self, path: Path) -> bool:
        resolved = _resolve_for_comparison(path)
        return any(
            resolved == protected or resolved.is_relative_to(protected)
            for protected in self._protected_paths
        )

    def _is_inside_destination(self, path: Path) -> bool:
        resolved = _resolve_for_comparison(path)
        return any(
            resolved == destination or resolved.is_relative_to(destination)
            for destination in self._destination_roots
        )

    def _is_ignored(self, path: Path, watch_root: Path, relative: Path) -> bool:
        return matches_ignore_globs(
            path.name, relative, path, watch_root, self._config.protect.globs
        )


def _resolve_for_comparison(path: Path) -> Path:
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError):
        return Path(os.path.abspath(path))


def _open_directory_without_symlinks(path: Path, watch_root: Path) -> int:
    """Open a directory without following links and prove it is the expected node."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_fd = os.open(path, flags)
    try:
        opened = os.fstat(directory_fd)
        listed = path.lstat()
        resolved = path.resolve(strict=True)
        if stat.S_ISLNK(listed.st_mode) or not stat.S_ISDIR(listed.st_mode):
            raise OSError("path is not a real directory")
        if (opened.st_dev, opened.st_ino) != (listed.st_dev, listed.st_ino):
            raise OSError("directory changed while opening")
        if resolved != path:
            raise OSError("directory path traverses a symlink")
        if not path.is_relative_to(watch_root):
            raise OSError("directory escapes the watch root")
    except BaseException:
        os.close(directory_fd)
        raise
    return directory_fd


def matches_ignore_globs(
    name: str,
    relative: Path,
    path: Path,
    watch_root: Path,
    globs: Iterable[str],
) -> bool:
    """Match a file name, relative path, path parts, or absolute path against globs."""
    relative_text = relative.as_posix()
    absolute_text = path.as_posix()
    for pattern in globs:
        if (
            fnmatch.fnmatchcase(name, pattern)
            or fnmatch.fnmatchcase(relative_text, pattern)
            or fnmatch.fnmatchcase(absolute_text, pattern)
            or any(fnmatch.fnmatchcase(part, pattern) for part in relative.parts)
        ):
            return True
    return False
