# Sloth Valet

A slow, private file valet for a messy downloads folder.

Sloth Valet waits for files to settle, asks a small local open-weight model for a
category and clean name, and moves them into a tidy destination tree. It runs
offline, journals every move, and never guesses when the model is unsure.

**Status:** config loading, domain models, the read-only scanner, bounded content
extraction, the Ollama client, and strict classification are implemented. The
`valet run` pipeline, moves, journaling, undo, and reports are landing in the
following phases.

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
```

`valet config init` writes the bundled example and refuses to overwrite an existing
file. `valet config show` prints the fully resolved effective config as JSON.
`valet scan` is read-only: it lists files that are old enough, under the size cap,
not ignored, not protected, not partial downloads, and stable across two reads. It
does not open them or call the model.

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

The model name has no default. Read an installed name from `ollama list` and put it
in the config; Sloth Valet never invents one.

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
