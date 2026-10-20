"""Read-only discovery of safe, settled candidates for classification."""

from __future__ import annotations

import fnmatch
import math
import os
import stat
import time
from collections.abc import Callable, Iterable
from pathlib import Path

from sloth_valet.config import ValetConfig, WatchConfig
from sloth_valet.models import (
    FileCandidate,
    ScanError,
    ScanResult,
    SkippedPath,
    SkipReason,
)

DEFAULT_STABILITY_DELAY_S = 2.0
PARTIAL_SUFFIXES = (".part", ".crdownload", ".tmp")


def is_partial_download(path: Path) -> bool:
    """Recognize in-progress downloads before spending any model time on them."""
    return path.name.casefold().endswith(PARTIAL_SUFFIXES)


class Scanner:
    """Find trustworthy files without opening them or contacting a model.

    Safety gates run in this order: protected paths, ignore globs (including
    partial-download suffixes), symlinks, settle time, file-size cap, and finally a
    stability check. The stability check reads size, mtime, device, and inode, waits
    once for the configured delay, then reads them again for every provisional
    candidate.
    """

    def __init__(
        self,
        config: ValetConfig,
        *,
        clock: Callable[[], float] = time.time,
        stability_delay_s: float = DEFAULT_STABILITY_DELAY_S,
        sleep_func: Callable[[float], None] | None = None,
    ) -> None:
        if not math.isfinite(stability_delay_s) or stability_delay_s < 0:
            raise ValueError("stability_delay_s must be a finite, non-negative number")
        self._config = config
        self._clock = clock
        self._stability_delay_s = stability_delay_s
        self._sleep = sleep_func or time.sleep
        self._max_file_size_mb = config.general.max_file_size_mb
        self._max_file_size_bytes = self._max_file_size_mb * 1024 * 1024
        self._protected_paths = tuple(
            _resolve_for_comparison(path) for path in config.protect.paths
        )

    def scan(self) -> ScanResult:
        provisional: list[FileCandidate] = []
        skipped: list[SkippedPath] = []
        errors: list[ScanError] = []
        seen_sources: set[Path] = set()

        for watch in self._config.watch:
            watch_candidates, watch_skipped, watch_errors = self._scan_watch(watch, seen_sources)
            provisional.extend(watch_candidates)
            skipped.extend(watch_skipped)
            errors.extend(watch_errors)

        candidates, stability_skips = self._verify_stability(provisional)
        skipped.extend(stability_skips)

        return ScanResult(
            candidates=tuple(sorted(candidates, key=lambda candidate: candidate.source)),
            skipped=tuple(sorted(skipped, key=lambda item: (item.path, item.reason))),
            errors=tuple(sorted(errors, key=lambda item: (item.path, item.message))),
        )

    def _scan_watch(
        self,
        watch: WatchConfig,
        seen_sources: set[Path],
    ) -> tuple[list[FileCandidate], list[SkippedPath], list[ScanError]]:
        candidates: list[FileCandidate] = []
        skipped: list[SkippedPath] = []
        errors: list[ScanError] = []

        try:
            if watch.path.is_symlink():
                raise OSError("watch root is a symlink")
            watch_root = watch.path.resolve(strict=True)
            if not watch_root.is_dir():
                errors.append(ScanError(watch.path, "watch root is not a directory"))
                return candidates, skipped, errors
        except (OSError, RuntimeError) as exc:
            errors.append(ScanError(watch.path, f"cannot use watch root: {exc}"))
            return candidates, skipped, errors

        directories = [watch_root]
        while directories:
            directory = directories.pop(0)
            try:
                directory_fd = _open_directory_without_symlinks(directory, watch_root)
            except (OSError, RuntimeError) as exc:
                errors.append(ScanError(directory, f"cannot read directory: {exc}"))
                continue

            try:
                with os.scandir(directory_fd) as directory_entries:
                    entries = sorted(directory_entries, key=lambda entry: entry.name)
                    for entry in entries:
                        path = directory / entry.name
                        try:
                            is_directory = entry.is_dir(follow_symlinks=False)
                            pre_skip = self._pre_metadata_skip(
                                path,
                                watch_root,
                                is_directory=is_directory,
                            )
                            if pre_skip is not None:
                                skipped.append(pre_skip)
                                continue
                            if entry.is_symlink():
                                skipped.append(SkippedPath(path, SkipReason.SYMLINK))
                                continue
                            if is_directory:
                                if watch.recursive:
                                    directories.append(path)
                                continue

                            first_stat = entry.stat(follow_symlinks=False)
                            candidate, file_skip = self._inspect_file(
                                path,
                                first_stat,
                                watch_root,
                                watch,
                                seen_sources,
                            )
                            if candidate is not None:
                                candidates.append(candidate)
                            if file_skip is not None:
                                skipped.append(file_skip)
                        except OSError as exc:
                            skipped.append(
                                SkippedPath(path, SkipReason.UNREADABLE, str(exc))
                            )
            except OSError as exc:
                errors.append(ScanError(directory, f"cannot read directory: {exc}"))
            finally:
                os.close(directory_fd)

        return candidates, skipped, errors

    def _pre_metadata_skip(
        self,
        path: Path,
        watch_root: Path,
        *,
        is_directory: bool,
    ) -> SkippedPath | None:
        """Apply pre-stat safety gates before metadata inspection."""
        if self._is_protected(path):
            return SkippedPath(path, SkipReason.PROTECTED)
        if not is_directory and is_partial_download(path):
            return SkippedPath(path, SkipReason.PARTIAL_DOWNLOAD)
        relative = path.relative_to(watch_root)
        if self._is_ignored(path, watch_root, relative):
            return SkippedPath(path, SkipReason.IGNORED)
        return None

    def _inspect_file(
        self,
        path: Path,
        first_stat: os.stat_result,
        watch_root: Path,
        watch: WatchConfig,
        seen_sources: set[Path],
    ) -> tuple[FileCandidate | None, SkippedPath | None]:
        if stat.S_ISLNK(first_stat.st_mode):
            return None, SkippedPath(path, SkipReason.SYMLINK)
        if not stat.S_ISREG(first_stat.st_mode):
            return None, SkippedPath(path, SkipReason.NOT_REGULAR)

        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            return None, SkippedPath(path, SkipReason.UNREADABLE, str(exc))
        if resolved != path:
            return None, SkippedPath(path, SkipReason.SYMLINK)
        if not resolved.is_relative_to(watch_root):
            return None, SkippedPath(path, SkipReason.OUTSIDE_WATCH)

        settle_seconds = self._config.general.settle_minutes * 60
        if self._clock() - first_stat.st_mtime_ns / 1_000_000_000 < settle_seconds:
            return None, SkippedPath(path, SkipReason.UNSETTLED)

        if first_stat.st_size > self._max_file_size_bytes:
            detail = (
                f"{first_stat.st_size} bytes exceeds "
                f"max_file_size_mb={self._max_file_size_mb}"
            )
            return None, SkippedPath(path, SkipReason.TOO_LARGE, detail)

        if resolved in seen_sources:
            return None, SkippedPath(path, SkipReason.DUPLICATE)
        seen_sources.add(resolved)

        return (
            FileCandidate(
                source=resolved,
                watch_root=watch_root,
                destination_root=watch.destination_root,
                size=first_stat.st_size,
                mtime_ns=first_stat.st_mtime_ns,
                device=first_stat.st_dev,
                inode=first_stat.st_ino,
            ),
            None,
        )

    def _verify_stability(
        self,
        candidates: list[FileCandidate],
    ) -> tuple[list[FileCandidate], list[SkippedPath]]:
        if not candidates:
            return [], []

        self._sleep(self._stability_delay_s)
        stable: list[FileCandidate] = []
        skipped: list[SkippedPath] = []

        for candidate in candidates:
            try:
                second_stat = candidate.source.lstat()
            except OSError as exc:
                skipped.append(SkippedPath(candidate.source, SkipReason.UNREADABLE, str(exc)))
                continue

            if stat.S_ISLNK(second_stat.st_mode):
                skipped.append(
                    SkippedPath(
                        candidate.source,
                        SkipReason.CHANGED,
                        "file became a symlink during the stability delay",
                    )
                )
                continue
            if not stat.S_ISREG(second_stat.st_mode):
                skipped.append(
                    SkippedPath(
                        candidate.source,
                        SkipReason.CHANGED,
                        "file is no longer a regular file",
                    )
                )
                continue

            try:
                resolved = candidate.source.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                skipped.append(
                    SkippedPath(candidate.source, SkipReason.UNREADABLE, str(exc))
                )
                continue
            if resolved != candidate.source:
                skipped.append(
                    SkippedPath(
                        candidate.source,
                        SkipReason.CHANGED,
                        "path now traverses a symlink",
                    )
                )
                continue
            if not resolved.is_relative_to(candidate.watch_root):
                skipped.append(SkippedPath(candidate.source, SkipReason.OUTSIDE_WATCH))
                continue
            if not candidate.matches_stat(second_stat):
                skipped.append(
                    SkippedPath(
                        candidate.source,
                        SkipReason.CHANGED,
                        "size, mtime, device, or inode changed",
                    )
                )
                continue

            stable.append(candidate)

        return stable, skipped

    def _is_protected(self, path: Path) -> bool:
        resolved = _resolve_for_comparison(path)
        return any(
            resolved == protected or resolved.is_relative_to(protected)
            for protected in self._protected_paths
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
