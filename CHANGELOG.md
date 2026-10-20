# Changelog

All notable changes to Sloth Valet are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-10-21

### Added

- Strict TOML configuration with relative-path resolution, protected paths, ignore
  globs, category and fast-path validation, and `config init` / `config show`.
- Read-only scanner with root confinement, symlink refusal, settle time, size limits,
  and a delayed stability check.
- Bounded text extraction for text, code, Markdown, CSV, PDF, and DOCX files, with
  metadata-only handling for images and unknown binaries.
- Local Ollama client with a tags health check and one schema-constrained chat
  request; no hosted APIs, telemetry, or hidden endpoints.
- Strict classification, allowlisted categories, filename and subfolder sanitization,
  confidence thresholds, review routing, and deterministic fast paths.
- SQLite journal, pending-before-move recording, collision-safe moves,
  cross-filesystem verification, crash reconciliation, and conditional undo.
- Escaped Markdown morning reports, `report --last` and `report --run`, and optional
  count-only `notify-send` notifications.
- Persistent daily systemd user timer, exact uninstall, and a read-only `status`
  command.
- Deterministic 50-fixture benchmark set, `valet bench --models`, JSON/Markdown
  result files, and optional best-effort peak Ollama RSS measurement.
- Committed authentic CPU-only benchmark run comparing `qwen2.5:0.5b` and
  `llama3.2:1b`, with 50/50 valid JSON for both and category accuracy of 12.0% and
  80.0% respectively.
- Safe, repeatable end-to-end demo script and a terminal recording of the verified
  `llama3.2:1b` run.
- Release installer using `uv tool install`, CI checks, architecture and safety
  documentation, and contribution guidance.

### Safety

- Dry-run remains the default until explicitly disabled or `--apply` is passed.
- Moves are journaled before they happen and never overwrite an existing path.
- Model output is treated as untrusted data and is never executed or interpolated
  into a shell command.
- Reports escape path text and do not reproduce free-form model output.
- Benchmark runs never journal, plan, move, or delete files.

### Notes

- Model names are never bundled. Users select their own Ollama model.
- Small local models can still choose a wrong allowlisted category. Use the benchmark,
  review folder, reports, and undo as the practical containment boundary.
- Benchmark results are not fabricated. The committed run came from real models on
  the machine described in `bench/results/README.md`; rerun locally and review new
  output before committing it.
