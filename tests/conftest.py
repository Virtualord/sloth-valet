from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from sloth_valet.config import ValetConfig, load_config

DEFAULT_CATEGORIES = (
    "academic",
    "career",
    "finance",
    "installers",
    "archives",
    "screenshots",
    "code",
    "media",
    "misc",
)


@dataclass(frozen=True, slots=True)
class EnvPaths:
    config_path: Path
    watch: Path
    destination: Path
    reports_dir: Path
    journal_path: Path

    def load(self) -> ValetConfig:
        return load_config(self.config_path)


def _toml_string(value: str | Path) -> str:
    return json.dumps(str(value))


def write_config(
    root: Path,
    *,
    watch_path: Path | None = None,
    destination: Path | None = None,
    recursive: bool = False,
    settle_minutes: int = 30,
    globs: tuple[str, ...] = ("*.part", "*.crdownload", "*.tmp", ".*"),
    protect_paths: tuple[Path, ...] = (),
    categories: tuple[str, ...] = DEFAULT_CATEGORIES,
    fast_path: dict[str, str] | None = None,
    on_uncertain: str = "leave",
) -> EnvPaths:
    watch = watch_path or root / "watch"
    destination_root = destination or root / "destination"
    reports_dir = root / "state" / "reports"
    journal_path = root / "state" / "journal.db"
    config_path = root / "valet.toml"

    watch.mkdir(parents=True, exist_ok=True)
    destination_root.mkdir(parents=True, exist_ok=True)

    protected = ", ".join(_toml_string(path) for path in protect_paths)
    glob_values = ", ".join(_toml_string(pattern) for pattern in globs)
    category_values = ", ".join(_toml_string(category) for category in categories)
    fast_path_lines = "".join(
        f"{_toml_string(suffix)} = {_toml_string(category)}\n"
        for suffix, category in (fast_path or {}).items()
    )

    config_path.write_text(
        f"""
[general]
dry_run = true
settle_minutes = {settle_minutes}
review_folder = "_review"
reports_dir = {_toml_string(reports_dir)}
journal_path = {_toml_string(journal_path)}
on_uncertain = {_toml_string(on_uncertain)}

[model]
host = "http://127.0.0.1:11434"
name = "fixture-model"
temperature = 0
timeout_s = 5
min_confidence = 0.7
text_chars = 1500

[[watch]]
path = {_toml_string(watch)}
destination_root = {_toml_string(destination_root)}
recursive = {str(recursive).lower()}

[protect]
paths = [{protected}]
globs = [{glob_values}]

[categories]
allowed = [{category_values}]

[fast_path]
{fast_path_lines}""".lstrip(),
        encoding="utf-8",
    )
    return EnvPaths(
        config_path=config_path,
        watch=watch,
        destination=destination_root,
        reports_dir=reports_dir,
        journal_path=journal_path,
    )


def make_file(
    path: Path,
    *,
    content: bytes = b"fixture content\n",
    age_seconds: float = 3600,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    timestamp = time.time() - age_seconds
    os.utime(path, (timestamp, timestamp))
    return path
