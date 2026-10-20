# Sloth Valet

A slow, private file valet for a messy downloads folder.

Sloth Valet waits for files to settle, asks a small local open-weight model for a
category and clean name, and moves them into a tidy destination tree. It runs
offline, journals every move, and never guesses when the model is unsure.

**Status:** config loading, domain models, the read-only scanner, bounded content
extraction, the Ollama client, strict classification, planning, journaled moves,
review routing, crash reconciliation, undo, Markdown reports, systemd scheduling, and
the local benchmark harness are implemented.

## Why this shape

- **Offline and private:** the only network destination is a user-configured Ollama
  instance, by default on localhost. File contents never leave the machine unless the
  user explicitly points the model host elsewhere.
- **Free to run:** a local open-weight model and plain Python, with no hosted API.
- **Swappable:** any Ollama model can be selected in the config. Sloth Valet never
  assumes a model name.
- **Safe unattended:** dry-run is the default, protected paths and ignore globs are
  applied before any model call, and symlinks are never followed.

## Install

Requires Linux and [uv](https://docs.astral.sh/uv/). Python 3.12 is managed by uv.

```sh
git clone <repository-url> sloth-valet
cd sloth-valet
uv sync
```

Create the config, then edit the watch roots, destination, protected paths, and model
name:

```sh
uv run valet config init
$EDITOR ~/.config/sloth-valet/valet.toml
uv run valet config show
uv run valet scan
uv run valet run
```

`valet config init` writes the bundled example and refuses to overwrite an existing
file. `valet config show` prints the fully resolved effective config as JSON.
`valet scan` is read-only: it lists files that are old enough, under the size cap,
not ignored, not protected, not partial downloads, and stable across two reads. It
does not open them or call the model.

`valet run` classifies and plans but moves nothing unless `dry_run = false` or you
pass `--apply`:

```sh
uv run valet run --apply
uv run valet report --last
uv run valet undo --last
```

Each run writes a Markdown report under `reports_dir`; `report --last` prints the most
recent one. Set `notify = true` to also send a short count-only `notify-send` desktop
notification. Each applied run writes a SQLite journal and records every move as
pending before it happens and done afterwards. `undo --last` (or `undo --run ID`)
restores files only when the destination still exists, its SHA-256 still matches, and
the original path is free. Low-confidence or failed classifications go to `_review/`
by default; set `review_on_failure = false` to leave them in place instead.
Cross-filesystem moves copy, verify SHA-256, and only then remove the source. A
pending row left by a crash is reconciled on the next run without deleting ambiguous
files.

For a daily unattended pass, install a systemd user timer after testing a real
dry run:

```sh
uv run valet install-timer --time 03:00
uv run valet status
uv run valet uninstall-timer
```

The units are written to `$XDG_CONFIG_HOME/systemd/user` (default
`~/.config/systemd/user`). The service runs
`valet --config <config> run --apply` once at the requested time, with `Nice=10` and
idle I/O priority. `--valet PATH` overrides the executable path used in the unit.
`install-timer` calls `systemctl --user daemon-reload` and
`systemctl --user enable --now sloth-valet.timer`; if that fails, the units remain
written and the command explains how to finish. `status` is read-only and does not
invoke `systemctl`. The timer intentionally applies moves, so run a dry-run pass
first.

## Configuration

The CLI reads `$SLOTH_VALET_CONFIG` when set, otherwise
`~/.config/sloth-valet/valet.toml`. Use `--config PATH` to choose another file:

```sh
uv run valet --config ./valet.toml config init
```

Relative paths in a config are resolved against that config file's directory.
`~` is expanded. A destination root inside any watch root is rejected at load time.
Symlinked watch roots and filesystem symlinks are refused by the scanner.

A scan applies protected paths, ignore globs, symlink refusal, `settle_minutes`,
the `max_file_size_mb` cap (default `100`), and a two-read stability check. The
default scan waits two seconds between those reads for the whole candidate batch.

The journal lives at `<reports_dir parent>/journal.sqlite3`, and each run writes a
timestamped Markdown report into `reports_dir`. `reports_dir` may not be inside a watch
root, so the pipeline can never scan or move its own state. The review folder defaults
to `_review` under each destination root. `notify = true` enables best-effort
`notify-send` messages; the counts are fixed text and are never passed through a
shell.

The model name has no default. Read an installed name from `ollama list` and put it
in the config; Sloth Valet never invents one.

## Benchmark

The repository ships labels for 50 deterministic fixtures across the default
categories. Generate the ignored fixture files, then compare any models already
pulled into the configured Ollama host:

```sh
uv run python scripts/make_fixtures.py
uv run valet bench --models model-a,model-b
```

Model names are always CLI arguments; no model is hardcoded. The harness disables
fast paths, extracts each fixture, calls the same classifier used by `valet run`, and
records JSON validity, category accuracy against `bench/labels.json`, confidence,
latency, failures, and an optional best-effort peak Ollama RSS with
`--measure-memory`. It never plans, moves, journals, or deletes a file. Each run
writes a new timestamped JSON file and Markdown summary under `bench/results/`;
existing results are not overwritten. Review generated files before committing them.
This project does not ship invented benchmark numbers or model names.

Classification is strict: the model must return the Pydantic schema, the category
must come from the allowlist, the filename is slugified and keeps the original
extension, and low confidence is a failure rather than a guess. Configured
`fast_path` extensions skip the model entirely. Extracted text is fenced as untrusted
data in the prompt, but prompt injection is not magic: a small model can still pick a
wrong allowed category, which is why review routing and undo are part of the safety
model.

## Development

```sh
uv run ruff check .
uv run mypy src tests
uv run pytest -q
```

The default test suite uses `httpx.MockTransport` and never needs network access or
Ollama. One live smoke test is skipped unless both variables are set:

```sh
SLOTH_VALET_LIVE=1 SLOTH_VALET_TEST_MODEL="$(ollama list | awk 'NR==2 {print $1}')" \
  uv run pytest -q tests/test_classifier.py -m live
```
