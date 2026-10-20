# Safety

Sloth Valet is designed to be boring when it fails. This document tracks the safety
invariants that are implemented and tested at each phase.

## Current guarantees (Phase 1)

- **Root confinement.** Only direct children of a watch root are considered unless
  `recursive = true`. A candidate is resolved and must still be inside the resolved
  watch root. The scanner never reads file contents.
- **Symlinks are refused.** Symlinked files are skipped, symlinked directories are
  not traversed, and a symlinked watch root is rejected. Directories are opened with
  `O_NOFOLLOW` and their inode must match the discovered path. Any candidate whose
  resolved location differs from its discovered location is skipped.
- **Regular files only.** Directories, sockets, FIFOs, devices, and other special
  files are not candidates.
- **Protected paths and ignore globs come first.** Both are checked during scanning,
  before any extraction or model call. Ignore patterns match the file name, relative
  path, any path component, or absolute path. Protected directories are pruned
  without being traversed.
- **Destination trees are excluded.** Files inside any configured destination root
  are never candidates, including when a destination is nested inside a watch root.
- **Incomplete and unsettled files wait.** Names ending in `.part`, `.crdownload`, or
  `.tmp` are always skipped. Files modified within `settle_minutes` are skipped.
- **Two checks.** The scanner compares size, mtime, device, and inode from the
  directory listing and a fresh `lstat`. A change means the file is skipped.
- **No network in the scanner.** The architectural test allows network-library
  imports only in the future `ollama.py` module. No current module imports one.
- **Model output schema and filename sanitizer are strict.** The schema rejects extra
  keys and invalid confidence values. The sanitizer rejects path separators and
  control characters, preserves the source extension, and caps names at 255 bytes.
  These are foundation pieces; they are not invoked by the read-only scan command.
- **Category names are constrained.** Category config values are lowercase path-safe
  slugs, so an allowlisted category cannot introduce a path separator.
- **Dry-run is the default.** `dry_run = true` is the config default. The current
  `scan` command is read-only regardless of the setting.

## Deferred guarantees

The following are specified and will be implemented in later phases. They are listed
here so the safety plan is visible rather than implied:

- collision-safe, non-overwriting moves and a pending/done move journal (Phase 2);
- path and fingerprint revalidation immediately before apply and undo (Phase 2);
- allowlist enforcement and confidence routing for model decisions (Phase 3);
- a behavioral network test using an injected HTTP transport to prove that requests
  can only reach the configured Ollama host (Phase 3);
- review routing and unattended reports (Phase 4).

## Reporting a safety issue

Do not run with `dry_run = false` on important data until the phase that introduces
moves has landed and its safety tests pass. If you find a path that escapes a root or
a case where file contents are read before the pre-model safety checks, treat it as a
release blocker.
