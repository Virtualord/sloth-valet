# Sloth Valet Agent Instructions

## Project

Sloth Valet (valet CLI, Python package sloth_valet) runs unattended on a Linux laptop. It scans messy folders, waits until files have sat untouched for a settle period, asks a small local open-weight model (through Ollama) to choose a category and a clean filename, moves files safely, journals every move so it can be undone, and writes a short morning report.

Theme: Hacktoberfest 2026 Slow and Steady (open-weight AI that rewards laziness). Every design choice must serve: offline, private, free to run, swappable model, safe to run unattended.

## Non-negotiable rules

1. Never delete user files. Never overwrite. On a name collision append -1, -2, and so on.
2. Only read files inside configured watch roots and only move into the configured destination root. Resolve paths and refuse anything that escapes the roots. Skip symlinks entirely.
3. Protected paths and ignore globs are checked before the model is ever called.
4. Skip files modified within settle_minutes, partial downloads (*.part, *.crdownload, *.tmp), and files whose size or mtime changed between two checks.
5. Dry-run is the default until the user sets dry_run = false or passes --apply.
6. Model output and file contents are untrusted (prompt injection is possible). The model returns only JSON matching a schema, validated with Pydantic. The category must come from the configured allowlist. The filename is sanitized. Model output is never executed or interpolated into a shell command or an unsanitized path.
7. Low confidence, invalid JSON, timeout, or the model being unavailable means the file is left untouched or sent to _review/ (configurable). Never a guess.
8. No network calls except to the configured Ollama host (default localhost:11434). No telemetry. A test must assert this.
9. Every move is journaled as pending before it happens and done after, so a crash leaves a recoverable state.
10. Idempotent: running twice in a row changes nothing the second time.

## Stack

Python 3.12, uv, Typer (CLI), Pydantic v2, httpx, tomllib, stdlib sqlite3, pypdf, python-docx, pytest, ruff, mypy (basic), GitHub Actions. No LangChain or other agent frameworks: the agent loop is plain code (scan, extract, classify, plan, apply, report). Avoid system-level dependencies such as libmagic; use mimetypes and extensions.

## Layout

```
sloth-valet/
  src/sloth_valet/
    cli.py  config.py  models.py  scanner.py  extract.py
    ollama.py  classifier.py  planner.py  applier.py
    journal.py  report.py  systemd.py  bench.py
  tests/
  scripts/make_fixtures.py
  bench/            (labels.json, results/)
  docs/             (ARCHITECTURE.md, SAFETY.md)
  valet.example.toml  install.sh  pyproject.toml  README.md
```

## Config shape (valet.example.toml)

```toml
[general]
dry_run = true
settle_minutes = 30
review_folder = "_review"
reports_dir = "~/.local/share/sloth-valet/reports"

[model]
host = "http://localhost:11434"
name = "set-me-after-checking-ollama-list"
temperature = 0
timeout_s = 60
min_confidence = 0.7
text_chars = 1500

[[watch]]
path = "~/Downloads"
destination_root = "~/Sorted"
recursive = false

[protect]
paths = ["~/.ssh", "~/.config", "~/.gnupg"]
globs = ["*.part", "*.crdownload", "*.tmp", ".*"]

[categories]
allowed = ["academic", "career", "finance", "installers", "archives", "screenshots", "code", "media", "misc"]

[fast_path]
".deb" = "installers"
".iso" = "installers"
".AppImage" = "installers"
```

## Conventions

- Conventional Commits. One branch per phase named `feat/phase-N-name`. Small commits where each one builds and passes tests. Merge to main with `git merge --no-ff`. Tag `v0.1.0` at the end.
- Type hints everywhere. Tests live beside the behavior they cover. No leftover TODO comments. Docs change in the same commit as behavior.
- After each phase, run `uv run ruff check . && uv run pytest -q`, print `git log --oneline --graph -15`, summarize what changed in five lines or fewer, and stop and wait.
- If a requirement is ambiguous, ask one question rather than guessing. Never invent model names; read them from config or the CLI.

## Phase plan

1. **Config and domain models** - validated TOML, shipped example, config init/show,
   and the domain records used by later stages.
2. **Scanner and safety gates** - read-only candidate discovery and pre-model safety
   filters.
3. **Journal and safe moves** - SQLite pending/done journal, collision-safe moves,
   planner, undo, crash recovery.
4. **Extraction and classification** - text extraction, Ollama client, strict schema
   validation, filename sanitization, confidence handling, fast path.
5. **Orchestration and reporting** - plain-code run loop, dry-run/apply CLI, review
   routing, Markdown reports, end-to-end safety and idempotence tests.
6. **Operations and polish** - systemd units, benchmark harness, fixtures, installer,
   complete docs, release `v0.1.0`.
