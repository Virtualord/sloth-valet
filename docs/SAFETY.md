# Safety

Sloth Valet is designed to be boring when it fails. This document tracks the safety
invariants that are implemented and tested at each phase.

## Current guarantees

- **Configuration is validated before use.** Missing required keys, unknown keys,
  invalid category names, invalid model URLs, bad paths, unsafe review-folder names,
  destination roots inside watch roots, and a `reports_dir` inside any watch root fail
  closed. The journal is derived from the reports directory, so it cannot be scanned
  and moved by the pipeline. Relative paths resolve against the config file and `~`
  expands.
- **Destination roots cannot sit inside watch roots.** Config loading compares every
  destination root with every watch root, including through symlinks, and refuses any
  overlap that would let a scan see its own output tree.
- **Root confinement.** Only direct children of a watch root are considered unless
  `recursive = true`. A candidate is resolved and must still be inside the resolved
  watch root. The scanner never reads file contents.
- **Fixed gate order.** For each entry the scanner applies protected paths, ignore
  globs (including built-in partial-download suffixes), symlink refusal, settle time,
  the file-size cap, and finally the stability check. Every skipped file or directory
  produces a `SkippedPath` with a stable reason for reports. Ordinary subdirectories
  are simply not visited when `recursive = false`.
- **Symlinks are refused.** Symlinked files are skipped, symlinked directories are
  not traversed, and a symlinked watch root is rejected. Directories are opened with
  `O_NOFOLLOW` and their inode must match the discovered path. Any candidate whose
  resolved location differs from its discovered location is skipped.
- **Regular files only.** Directories, sockets, FIFOs, devices, and other special
  files are not candidates.
- **Protected paths and ignore globs come first.** Both are checked during scanning,
  before content access, extraction, or any model call. Ignore patterns match the
  file name, relative path, any path component, or absolute path. Protected
  directories are pruned without being traversed.
- **Incomplete and unsettled files wait.** Names ending in `.part`, `.crdownload`, or
  `.tmp` are always skipped. Files modified within `settle_minutes` are skipped.
- **The size cap is enforced before stability.** Files larger than
  `general.max_file_size_mb` (default `100`) are skipped with the `too_large` reason
  after the settle check and before the stability delay.
- **Two delayed reads.** The scanner fingerprints size, mtime, device, and inode from
  the directory listing, waits once for all provisional candidates, then reads them
  again. The default delay is two seconds and is injectable for tests. A file whose
  identity, size, or mtime changed, or whose path now traverses a symlink, is skipped
  with the `changed` reason.
- **Content extraction is bounded and failure-safe.** Text-like formats read a
  bounded prefix and return at most `model.text_chars` characters. PDFs use the first
  three pages, DOCX files use paragraphs, and images or unknown binaries return only
  metadata. Control characters are stripped. Corrupt or unreadable files produce
  empty text with a reason; extraction never raises.
- **Only one module can speak HTTP.** An architectural test parses every package
  module and allows network-library imports only in `ollama.py`. That client uses the
  configured host as its base URL, follows no redirects, ignores proxy environment
  variables, and has only two operations: `GET /api/tags` and `POST /api/chat`. A
  behavioral test records every request URL and proves they all target the configured
  scheme/host/port. CI uses `httpx.MockTransport`, and the live Ollama test is opt-in.
- **Model output is untrusted and strictly shaped.** The Pydantic schema forbids extra
  keys and validates confidence. Output over 64 KiB, invalid JSON, or any schema
  mismatch is a failure. The category must exactly match the configured allowlist.
- **Filenames and subfolders are sanitized.** Model-proposed names are NFKC-normalized,
  stripped of any proposed extension, lowercased and slugified, reduced to one path
  component, denied leading dots, and given the original source extension. The total
  filename is capped at 80 characters; a subfolder is one safe component capped at 60.
  Path separators, traversal text, control characters, shell syntax, and hidden-file
  prefixes cannot survive. The filename sanitizer is deterministic and is never
  bypassed for model output.
- **File content is data, not instructions.** The classifier prompt states this
  explicitly, places the allowlist and examples outside the content fence, fences the
  untrusted text with markers, and neutralizes those markers if they occur inside the
  file. Prompt injection can still fool a small model into a wrong but allowlisted
  category; the allowlist, sanitizer, confidence threshold, review routing, and undo
  are the containment boundary.
- **Fast paths still obey the allowlist.** Extension mappings are configuration, their
  target categories must be allowed, and they skip the health check and model call.
  They produce a deterministic sanitized filename with confidence 1.0.
- **Every classifier failure is explicit.** An unavailable model, timeout, HTTP error,
  malformed response, invalid JSON, category outside the allowlist, unusable filename,
  or confidence below `model.min_confidence` returns a failure reason. It is never
  converted into a made-up category or name.
- **Category names are constrained.** Category config values are lowercase path-safe
  slugs, so an allowlisted category cannot introduce a path separator.
- **Review routing is configurable and safe.** Classifier failures are moved only to
  `<destination_root>/<review_folder>/<sanitized original name>` when
  `general.review_on_failure` is true (the default). The review name goes through the
  same sanitizer and collision numbering as every other name. When it is false, the
  file is left untouched and reported. A review file is still a journaled move and can
  be undone.
- **Planning never overwrites.** Destinations are checked against the disk and a
  plan-local reservation set. Existing files, directories, and symlinks force a
  numbered suffix. The applier checks destination absence again immediately before the
  move, and cross-filesystem copies use exclusive creation (`O_EXCL`).
- **Apply revalidates everything.** Before a move the applier re-stats the source and
  requires the same size, mtime, device, and inode as the scan. It resolves the source,
  requires it to be a regular non-symlink inside a configured watch root, resolves the
  destination parent, requires it inside a configured destination root, and refuses a
  destination that exists. A changed file is never moved.
- **Every move is journaled.** The source is SHA-256 hashed and a `pending` row is
  committed with run id, source, destination, hash, size, and timestamp before the
  move. Only a successful move becomes `done`; a failure becomes `failed` and leaves
  the source. A crash after the rename but before the update leaves a pending row.
- **Cross-filesystem moves verify before removing the source.** After an `EXDEV`
  rename, the applier creates the destination exclusively, copies, fsyncs, hashes the
  destination, compares it with the pre-move source hash, and only then unlinks the
  source. A failed verification removes only the partial copy it created.
- **Crash reconciliation never guesses destructively.** On startup, pending rows are
  examined. A missing source plus a matching destination is completed as `done`; a
  present source with no destination is marked failed and can be retried; both copies
  present, a hash mismatch, or both missing is marked failed with an explanation and
  both files are left for inspection. Reconciliation never deletes an ambiguous file.
- **Undo is conditional.** Undo requires the destination to exist as a regular
  non-symlink, its current SHA-256 to equal the journal, both paths to remain inside
  configured roots, and the original path to be free with an existing non-symlink
  parent. It restores in reverse order and marks the journal `reverted`. Any refusal
  leaves both paths untouched and prints the reason.
- **Dry-run is the default.** `general.dry_run = true` is the config default. `valet
  run` previews by default, writes no pending rows, and moves nothing; `--apply` or
  `dry_run = false` is required to act. `valet scan` remains read-only regardless of
  the setting. Undo always requires an explicit completed run.
- **Reports are escaped audit artifacts.** Every run writes a timestamped Markdown
  report with exclusive creation, so an existing report is never overwritten. The
  report path is recorded in the journal and `valet report --last` prints it. Untrusted
  path text is escaped for Markdown table columns and HTML; free-form model output is
  not copied into the report, only allowlisted categories and stable failure reasons.
  Report-write failure never changes the outcome of a completed move.
- **Notifications cannot run arbitrary text.** `notify-send` is optional, receives a
  fixed title and a count-only summary, is invoked with an argument list rather than a
  shell, has a five-second timeout, and is best-effort. A missing or failing
  notification tool is ignored.
- **Scheduled runs use the same applied pipeline.** `install-timer` writes a oneshot
  service whose `ExecStart` is an argument list equivalent to
  `valet --config <absolute config> run --apply`; the config path and executable are
  quoted as systemd arguments, never passed through a shell. The timer is persistent,
  so a missed run starts after boot. Installation writes only the two known unit files,
  uses atomic replacement, refuses a symlinked unit path, and `uninstall-timer` removes
  only those two names. `status` reads local state and never invokes `systemctl`.
  Installing the timer is an explicit decision to apply moves automatically; run a
  dry-run first.
- **The benchmark is read-only.** It loads labels from a validated JSON file, rejects
  unsafe or duplicate names, resolves each fixture to a regular non-symlink file
  inside the fixture directory, and requires every expected category to be in the
  configured allowlist. It calls the classifier directly, disables fast paths so the
  model is genuinely measured, and never creates journal rows, plans, moves, deletes,
  or writes reports. Fixture generation refuses to replace existing files without an
  explicit overwrite request, never deletes unrelated files, and treats a symlinked
  fixture path as an error. Benchmark results use exclusive creation, and the
  repository does not ship invented model names or fabricated metrics.

## Reporting a safety issue

Always read a dry-run plan against a copy of real data before the first `--apply`.
If you find a path that escapes a root, an overwrite, a move that is not journaled
before it happens, or a case where file contents are read before the pre-model safety
checks, treat it as a release blocker.
