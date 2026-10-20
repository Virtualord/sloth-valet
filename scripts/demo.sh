#!/usr/bin/env bash
# Run a safe, self-contained end-to-end demo against generated fixtures.
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: scripts/demo.sh <ollama-model>

Choose a model name from `ollama list`. The demo creates a fresh temporary
Downloads folder, runs scan, dry-run, apply, a second apply, report, and undo.
It never touches a real Downloads folder.
EOF
}

if [[ $# -ne 1 || -z "$1" ]]; then
  usage
  exit 2
fi

model="$1"
model_host="${SLOTH_VALET_MODEL_HOST:-http://127.0.0.1:11434}"
project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

if ! command -v uv >/dev/null 2>&1; then
  printf 'error: uv is required (https://docs.astral.sh/uv/)\n' >&2
  exit 1
fi

cd "$project_root"
demo_dir="$(mktemp -d "${TMPDIR:-/tmp}/sloth-valet-demo.XXXXXX")"
mkdir -p "$demo_dir/Downloads" "$demo_dir/Sorted" "$demo_dir/reports" "$demo_dir/fixtures"

printf 'Generating fixtures in %s\n' "$demo_dir/fixtures"
uv run python scripts/make_fixtures.py --output "$demo_dir/fixtures" >/dev/null

for name in \
  academic-graph-theory-notes.pdf \
  finance-bank-statement-october.pdf \
  code-retry-helper.py \
  media-interview-recording.mp3 \
  screenshots-settings-window.png \
  installers-docker-desktop.deb
do
  cp "$demo_dir/fixtures/$name" "$demo_dir/Downloads/"
done
printf 'unfinished download\n' > "$demo_dir/Downloads/unfinished.part"

config="$demo_dir/valet.toml"
cat > "$config" <<EOF
[general]
dry_run = true
settle_minutes = 0
review_folder = "_review"
review_on_failure = true
notify = false
reports_dir = "$demo_dir/reports"

[model]
host = "$model_host"
name = "$model"
temperature = 0
timeout_s = 60
min_confidence = 0.7
text_chars = 1500

[[watch]]
path = "$demo_dir/Downloads"
destination_root = "$demo_dir/Sorted"
recursive = false

[protect]
paths = ["~/.ssh", "~/.config", "~/.gnupg"]
globs = ["*.part", "*.crdownload", "*.tmp", ".*"]

[categories]
allowed = [
  "academic",
  "career",
  "finance",
  "installers",
  "archives",
  "screenshots",
  "code",
  "media",
  "misc",
]

[fast_path]
".deb" = "installers"
".iso" = "installers"
".AppImage" = "installers"
EOF

run() {
  printf '\n$ %s\n' "$*"
  "$@"
}

printf '\nSloth Valet demo using model: %s\n' "$model"
run uv run valet --config "$config" scan
run uv run valet --config "$config" run
run uv run valet --config "$config" run --apply
run uv run valet --config "$config" report --last
run uv run valet --config "$config" run --apply
run uv run valet --config "$config" report --last
run uv run valet --config "$config" undo --last

printf '\nRestored Downloads contents:\n'
find "$demo_dir/Downloads" -maxdepth 1 -type f -printf '  %f\n' | sort
printf '\nDemo directory left in place for inspection:\n  %s\n' "$demo_dir"
