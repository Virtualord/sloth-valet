# Contributing

Sloth Valet is a small Python project with a deliberately plain pipeline. Changes
are welcome when they keep it offline, safe to run unattended, and easy to audit.

## Development setup

Requirements: Linux, Python 3.12, [uv](https://docs.astral.sh/uv/), and optionally a
local Ollama installation for live smoke tests.

```sh
uv sync
uv run valet --version
uv run pytest -q
```

The default suite uses `httpx.MockTransport`, temporary directories, and a fake
classifier. It must never need network access, a running Ollama, a real systemd
user manager, or a real desktop notification service.

Before opening a pull request, run the same checks as CI:

```sh
uv run ruff check .
uv run mypy src tests
uv run pytest -q
```

## Design rules

- Never delete or overwrite a user file. Collisions get numbered suffixes.
- Keep reads inside watch roots and moves inside destination roots. Refuse symlinks.
- Keep protected-path and ignore checks before extraction or model calls.
- Keep dry-run the default. Only `--apply` or `dry_run = false` may move files.
- Treat model output and file contents as untrusted. Validate JSON with the strict
  Pydantic schema, enforce the category allowlist, and sanitize names.
- A model failure, timeout, invalid response, or low confidence must never become a
  guessed category.
- Journal every move as pending before it happens and done afterwards.
- Keep the pipeline idempotent: a second applied run over the same tree is a no-op.
- Do not add network calls outside `sloth_valet.ollama`. No telemetry.
- Do not hardcode model names. Read them from config or the `--models` CLI option.
- Do not invent benchmark numbers. Commit only results produced by a real local run.
- Update tests and documentation in the same commit as behavior changes.

If a change touches a safety gate, add a regression test and update
[`docs/SAFETY.md`](docs/SAFETY.md). If it changes a stage boundary, update
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Commits and branches

Use Conventional Commits, for example `fix(scanner): reject symlinked watch roots`.
Keep commits small enough to review and buildable on their own. Do not leave TODO
comments in shipped code.

## Benchmarks

Generated fixture files are ignored; committed labels live in
[`bench/labels.json`](bench/labels.json). To run a comparison:

```sh
uv run python scripts/make_fixtures.py
uv run valet bench --models <model-one>,<model-two>
```

Review the JSON and Markdown under `bench/results/` before committing them. Include
the model names, host details that are safe to share, and the actual results from the
run. Never hand-edit metrics.

## Reporting safety issues

If you find a path escape, an overwrite, a move that is not journaled first, or a
case where file contents are read before the pre-model gates, treat it as a release
blocker. Include a minimal reproduction with temporary paths and redact any personal
file names.
