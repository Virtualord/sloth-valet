# Sloth Valet

A slow, private file valet for a messy downloads folder.

Sloth Valet waits for files to settle, asks a small local open-weight model for a
category and clean name, and moves them into a tidy destination tree. It runs
offline, journals every move, and never guesses when the model is unsure.

**Status:** Phase 1 of 5. The read-only scanner is implemented. Moving, journaling,
classification, and reports are landing in the following phases.

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

Copy the example config and edit the watch roots, destination, protected paths, and
model name:

```sh
mkdir -p ~/.config/sloth-valet
cp valet.example.toml ~/.config/sloth-valet/config.toml
uv run valet --config ~/.config/sloth-valet/config.toml scan
```

`valet scan` is read-only. It lists files that are old enough, not ignored, not
protected, and not partial downloads. It does not open them or call the model.

## Configuration

The CLI uses `$SLOTH_VALET_CONFIG`, then
`$XDG_CONFIG_HOME/sloth-valet/config.toml` (falling back to
`~/.config/sloth-valet/config.toml`). Use `--config PATH` to choose another file.

Relative paths in a config are resolved against that config file's directory.
`~` is expanded. Symlinked watch roots and symlinked destination roots are refused.

`general.reports_dir` and `general.journal_path` are added beyond the baseline
config shape because reports and the undo journal need stable locations. Model
failures can either leave a file in place or route it to `_review/`:

```toml
[general]
on_uncertain = "leave"  # or "review"
```

## Development

```sh
uv run ruff check .
uv run mypy src tests
uv run pytest -q
```

The test suite avoids network access and Ollama.

## Roadmap

1. **Foundation and scanner safety** - package scaffold, validated config, read-only
   scanner, tests, CI.
2. **Journal and safe moves** - SQLite pending/done journal, collision-safe moves,
   undo, crash recovery.
3. **Extraction and classification** - text extraction, Ollama client, strict schema
   validation, filename sanitization, confidence handling, fast path.
4. **Orchestration and reporting** - plain-code run loop, dry-run/apply CLI, review
   routing, Markdown reports, end-to-end safety and idempotence tests.
5. **Operations and polish** - systemd units, benchmark harness, fixtures, installer,
   complete documentation, release `v0.1.0`.
