# Architecture

Sloth Valet is a plain-code pipeline. There is no agent framework and no hidden loop:
each stage returns data for the next, and every stage is independently testable.

```text
scan -> extract -> classify -> plan -> apply -> report
         ^ model call only here
```

## Current stage: scan

`scanner.Scanner` walks each configured watch root and produces:

- `FileCandidate` objects for settled, regular, non-ignored files;
- `SkippedPath` records with a stable reason for reporting;
- `ScanError` records for unreadable roots or directories.

Scanning is read-only. It does not extract text, call Ollama, or modify the
filesystem. Each candidate carries a fingerprint of size, mtime, device, and inode so
later stages can prove they are still looking at the same file.

The scanner enforces root confinement, symlink refusal, protected paths, ignore
globs, partial-download suffixes, settle time, a configurable size cap, and a delayed
double-stat check. It records a skip reason for every skipped file and directory.
The stability delay defaults to two seconds, happens once per scan after all
provisional candidates are known, and is injectable for tests. Config loading
separately rejects any destination root that sits inside a watch root.
`docs/SAFETY.md` records the exact guarantees.

## Modules

- `config.py` - TOML loading and frozen, strict Pydantic models. Relative paths
  resolve against the config file. The default is
  `~/.config/sloth-valet/valet.toml`; `config init` writes the packaged example
  without overwriting, and `config show` prints the resolved config as JSON.
- `models.py` - domain records (`FileCandidate`, `Classification`, `PlannedMove`,
  `MoveRecord`) plus the strict decision schema and filename sanitizer used for
  untrusted model output.
- `scanner.py` - read-only candidate discovery and pre-model safety filters.
- `cli.py` - Typer composition root. The current `scan` command is intentionally
  read-only.

## Planned stages

- **extract** - bounded text extraction by extension using `pypdf` and `python-docx`;
  no libmagic and no execution.
- **classify** - deterministic fast paths first, then one HTTP request to the
  configured Ollama endpoint. Strict JSON schema, allowlisted category, sanitized
  filename, confidence threshold. Every failure leaves the file alone or routes it to
  review.
- **plan** - resolve destination and collision names without touching disk.
- **apply** - journal pending, revalidate paths and fingerprints, move without
  overwriting, mark done.
- **report** - a short Markdown summary of moves, reviews, skips, and errors.

The journal, undo path, and report are part of the trust boundary: a crash between
any two stages must leave the filesystem and journal in a recoverable state.
