# Safety

Sloth Valet is designed to be boring when it fails. This document tracks the safety
invariants that are implemented and tested at each phase.

## Current guarantees

- **Configuration is validated before use.** Missing required keys, unknown keys,
  invalid category names, invalid model URLs, bad paths, and unsafe review-folder
  names fail closed. Relative paths resolve against the config file and `~` expands.
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
- **Dry-run is the default.** `dry_run = true` is the config default. The current
  `scan` command is read-only regardless of the setting.

## Deferred guarantees

The following are specified and will be implemented in later phases:

- collision-safe, non-overwriting moves and a pending/done move journal;
- path and fingerprint revalidation immediately before apply and undo;
- review routing for classifier failures and unattended Markdown reports.

## Reporting a safety issue

Do not run with `dry_run = false` on important data until the phase that introduces
moves has landed and its safety tests pass. If you find a path that escapes a root or
a case where file contents are read before the pre-model safety checks, treat it as a
release blocker.
