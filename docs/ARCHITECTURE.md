# Architecture

Sloth Valet is a plain-code pipeline. There is no agent framework and no hidden loop:
each stage returns data for the next, and every stage is independently testable.

```text
scan -> extract -> classify -> plan -> apply -> report
         ^ model call only here
```

## Current stages: scan, extract, classify, plan, apply, report, undo

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

`extract.extract_file` turns one candidate into bounded text plus metadata. Text,
Markdown, code, and CSV files return the first `text_chars` characters. PDFs return
the first three pages through `pypdf`; DOCX files return their paragraphs through
`python-docx`. Images and unknown binaries return name, extension, size, and MIME
type only. Control characters are removed and every parser failure becomes empty text
with a reason instead of an exception.

`classifier.Classifier` checks the configured `fast_path` extension map before doing
anything else. Obvious files such as `.deb` or `.iso` become deterministic
classifications without a model call, even when Ollama is down. Everything else asks
`ollama.OllamaClient` for `GET /api/tags` once, then sends one non-streaming
`POST /api/chat` request with `format` set to the Pydantic classification JSON schema,
the configured temperature, and the configured timeout.

The system prompt contains the category allowlist and three allowed few-shot examples
(or fewer when the allowlist is short). It explicitly labels names, metadata, and
extracted text as untrusted data, not instructions. The extracted text is fenced and
marker-like strings inside it are neutralized. The model's JSON is parsed strictly,
the category is checked against the allowlist, both filename and subfolder are
slugified into single safe components, the source extension is retained, and the
confidence threshold is enforced. Invalid JSON, an unavailable model, timeout, HTTP
error, invalid category, unusable filename, or low confidence produces a
`ClassificationResult` failure rather than a guess.

`ollama.py` is the only package module that imports an HTTP library. It uses the
configured host as its client base URL, follows no redirects, ignores proxy
environment variables, sends no telemetry, and exposes only the tags health check and
the chat endpoint. Tests inject `httpx.MockTransport`, so CI never needs Ollama. A
single live test is skipped unless `SLOTH_VALET_LIVE=1` and
`SLOTH_VALET_TEST_MODEL` are set; it never hardcodes a model name.

`runner.execute_run` is the plain-code loop. It creates a run id, reconciles any
pending journal rows, scans, extracts only for files that did not take a fast path,
classifies, plans, and either previews or applies. It never asks the model to choose a
path and never executes model text. Every failed classification is reviewed or left
alone according to `general.review_on_failure`.

`planner.Planner` builds destinations from a resolved destination root, an
allowlisted category, one sanitized subfolder component, and one sanitized filename.
It checks the disk and a plan-local reservation set, then appends `-1`, `-2`, and so
on before the extension until the path is free. It rejects an existing destination
symlink as a collision and refuses a category directory that resolves outside the
destination root. Review moves use the configured `_review` folder and the sanitized
original filename.

`applier.Applier` revalidates the candidate fingerprint, source/watch-root
confinement, destination-root confinement, destination parent, and destination
absence immediately before moving. It hashes the source, writes a `pending` journal
row with run id, source, destination, SHA-256, size, and timestamp, then moves. Same
filesystem moves use `os.rename`; `EXDEV` falls back to a no-overwrite copy, SHA-256
verification, and source removal. The row becomes `done` only after success. A failed
move keeps the source and records `failed`. A pending row left by a crash is
reconciled on the next run without deleting an ambiguous file: completed destinations
are marked done, while any ambiguous state is marked failed with an explanation.

Undo reads only `done` rows from the selected run, in reverse order. It requires the
destination to still exist as a regular file, the stored hash to match, the path to
remain inside the configured roots, and the original path to be free and inside a
watch root. Anything else is skipped with a reason. A successful restore is marked
`reverted`.

The SQLite journal lives beside `general.reports_dir`
(`<reports_dir parent>/journal.sqlite3`), never inside a watch root because config
loading rejects a `reports_dir` under any watch root. It has a `runs` table for every
invocation (including dry runs) and a `moves` table with pending/done/failed/reverted
state. Each run row also stores the path of its Markdown report.

`report.write_report` writes a timestamped, never-overwritten Markdown file into
`reports_dir`: a title, a three-line summary, and a detail table of moves, review
routing, skipped paths, scan errors, and journal repairs. Paths and error text are
escaped so a crafted filename cannot inject table columns or raw HTML. The report
does not repeat free-form model output; it shows allowlisted categories and stable
failure reasons. The path is recorded in the journal, and `valet report --last` (or
`--run ID`) prints that file. If `general.notify` is true, a short count-only
`notify-send` call is attempted with a fixed argument list; notification failure never
changes a run result. Report-write failure is also non-fatal to an already-completed
move.

## Modules

- `config.py` - TOML loading and frozen, strict Pydantic models. Relative paths
  resolve against the config file. The default is
  `~/.config/sloth-valet/valet.toml`; `config init` writes the packaged example
  without overwriting, and `config show` prints the resolved config as JSON.
- `models.py` - domain records (`FileCandidate`, `Classification`, `PlannedMove`,
  `MoveRecord`) plus the strict decision schema and filename sanitizer used for
  untrusted model output.
- `extract.py` - bounded text extraction by extension using `pypdf` and
  `python-docx`; no libmagic and no execution.
- `ollama.py` - the only HTTP module. Tags health check plus one schema-constrained
  chat endpoint on the configured host.
- `classifier.py` - deterministic fast path, prompt construction, strict parsing,
  allowlist/confidence enforcement, and failure records.
- `scanner.py` - read-only candidate discovery and pre-model safety filters.
- `planner.py` - collision-free destinations and review routing, with no filesystem
  writes.
- `journal.py` - SQLite run/move records and explicit state transitions.
- `applier.py` - revalidating moves, cross-filesystem verification, pending-row
  reconciliation, and cautious undo.
- `runner.py` - the plain-code pipeline and dry-run/apply selection.
- `report.py` - Markdown rendering, report-file allocation, and optional desktop
  notifications.
- `cli.py` - Typer composition root for `scan`, `run`, `report`, `undo`, and config
  commands.

## Planned stages

- **operations** - systemd scheduling and the benchmark harness.

The journal, report, and undo path are part of the trust boundary: a crash between
any two stages must leave the filesystem and journal in a recoverable state.
