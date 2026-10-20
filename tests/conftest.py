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
    max_file_size_mb: int = 100,
    dry_run: bool = True,
    model_name: str = "fixture-model",
    model_host: str = "http://127.0.0.1:11434",
    model_temperature: float = 0,
    review_on_failure: bool = True,
    review_folder: str = "_review",
    notify: bool = False,
    globs: tuple[str, ...] = ("*.part", "*.crdownload", "*.tmp", ".*"),
    protect_paths: tuple[Path, ...] = (),
    categories: tuple[str, ...] = DEFAULT_CATEGORIES,
    fast_path: dict[str, str] | None = None,
) -> EnvPaths:
    watch = watch_path or root / "watch"
    destination_root = destination or root / "destination"
    reports_dir = root / "state" / "reports"
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
dry_run = {str(dry_run).lower()}
settle_minutes = {settle_minutes}
max_file_size_mb = {max_file_size_mb}
review_folder = {_toml_string(review_folder)}
review_on_failure = {str(review_on_failure).lower()}
notify = {str(notify).lower()}
reports_dir = {_toml_string(reports_dir)}

[model]
host = {_toml_string(model_host)}
name = {_toml_string(model_name)}
temperature = {model_temperature}
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
