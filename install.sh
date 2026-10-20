#!/usr/bin/env bash
# Install Sloth Valet as an isolated uv tool. No sudo, no system services.
set -euo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
tool_bin_dir="${UV_TOOL_BIN_DIR:-$HOME/.local/bin}"

info() {
  printf '%s\n' "$*"
}

fail() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

if [[ "$(uname -s)" != "Linux" ]]; then
  fail "Sloth Valet currently targets Linux because scheduling uses systemd."
fi

if ! command -v uv >/dev/null 2>&1; then
  fail "uv is required. Install it from https://docs.astral.sh/uv/ and rerun this script."
fi

info "Installing Sloth Valet from $project_root"
uv tool install --force --python 3.12 "$project_root"

info ""
if command -v valet >/dev/null 2>&1; then
  valet --version
elif [[ -x "$tool_bin_dir/valet" ]]; then
  "$tool_bin_dir/valet" --version
  info "Add this to your shell profile if valet is not on PATH:"
  info "  export PATH=\"$tool_bin_dir:\$PATH\""
else
  info "Installed, but the valet executable was not found in $tool_bin_dir."
  info "Check 'uv tool list' and your PATH."
fi

cat <<'EOF'

Next steps:
  1. Pull an Ollama model you have chosen, then read its name with:
       ollama list
  2. Create the config and set that name, your watch root, and destination:
       valet config init
       $EDITOR ~/.config/sloth-valet/valet.toml
  3. Preview, then apply:
       valet scan
       valet run
       valet run --apply
  4. Review the report or undo:
       valet report --last
       valet undo --last

The default config is a dry run. The optional systemd timer intentionally runs
with --apply, so test the dry-run path before installing it:
  valet install-timer --time 03:00
EOF
