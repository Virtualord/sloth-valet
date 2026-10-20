# Architecture

Sloth Valet is a plain-code pipeline. There is no agent framework and no hidden loop:
each stage returns data for the next, and every stage is independently testable.

```text
scan -> extract -> classify -> plan -> apply -> report
         ^ model call only here
```

## Current stages: scan, extract, classify

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
- `cli.py` - Typer composition root. The current `scan` command is intentionally
  read-only.

## Planned stages

- **plan** - resolve destination and collision names without touching disk.
- **apply** - journal pending, revalidate paths and fingerprints, move without
  overwriting, mark done.
- **report** - a short Markdown summary of moves, reviews, skips, and errors.

The journal, undo path, and report are part of the trust boundary: a crash between
any two stages must leave the filesystem and journal in a recoverable state.
