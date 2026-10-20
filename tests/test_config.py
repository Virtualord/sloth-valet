from __future__ import annotations

from pathlib import Path

import pytest
from conftest import write_config

from sloth_valet.config import ConfigError, load_config

MINIMAL_RELATIVE_CONFIG = """
[general]
reports_dir = "state/reports"
journal_path = "state/journal.db"

[model]
name = "fixture-model"

[[watch]]
path = "watch"
destination_root = "sorted"

[categories]
allowed = ["misc"]
"""


def test_load_config_resolves_absolute_paths_and_fast_path(tmp_path: Path) -> None:
    paths = write_config(
        tmp_path,
        recursive=True,
        fast_path={".AppImage": "installers"},
        protect_paths=(tmp_path / "private",),
    )

    config = paths.load()

    assert config.general.dry_run is True
    assert config.watch[0].path == paths.watch
    assert config.watch[0].destination_root == paths.destination
    assert config.watch[0].recursive is True
    assert config.general.reports_dir == paths.reports_dir
    assert config.general.journal_path == paths.journal_path
    assert config.protect.paths == [tmp_path / "private"]
    assert config.fast_path == {".appimage": "installers"}
    assert config.model.host == "http://127.0.0.1:11434"


def test_relative_paths_resolve_against_the_config_file(tmp_path: Path) -> None:
    (tmp_path / "watch").mkdir()
    (tmp_path / "sorted").mkdir()
    config_path = tmp_path / "settings.toml"
    config_path.write_text(MINIMAL_RELATIVE_CONFIG, encoding="utf-8")

    config = load_config(config_path)

    assert config.watch[0].path == tmp_path / "watch"
    assert config.watch[0].destination_root == tmp_path / "sorted"
    assert config.general.reports_dir == tmp_path / "state" / "reports"
    assert config.general.journal_path == tmp_path / "state" / "journal.db"


def test_model_host_is_normalized_and_rejects_unsafe_urls(tmp_path: Path) -> None:
    paths = write_config(tmp_path)
    text = paths.config_path.read_text(encoding="utf-8")
    paths.config_path.write_text(
        text.replace("http://127.0.0.1:11434", "http://127.0.0.1:11434/"),
        encoding="utf-8",
    )
    assert paths.load().model.host == "http://127.0.0.1:11434"

    paths.config_path.write_text(
        text.replace("http://127.0.0.1:11434", "file:///tmp/ollama"),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="http or https"):
        paths.load()

    paths.config_path.write_text(
        text.replace("http://127.0.0.1:11434", "http://user:secret@localhost:11434"),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="credentials"):
        paths.load()


def test_unknown_fields_are_rejected(tmp_path: Path) -> None:
    paths = write_config(tmp_path)
    text = paths.config_path.read_text(encoding="utf-8")
    paths.config_path.write_text(
        text.replace("dry_run = true", "dry_run = true\nsurprise = 1"),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="surprise"):
        paths.load()


def test_invalid_review_folder_is_rejected(tmp_path: Path) -> None:
    paths = write_config(tmp_path)
    text = paths.config_path.read_text(encoding="utf-8")
    paths.config_path.write_text(
        text.replace('review_folder = "_review"', 'review_folder = "../escape"'),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="review_folder"):
        paths.load()


def test_fast_path_must_target_an_allowed_category(tmp_path: Path) -> None:
    paths = write_config(tmp_path, fast_path={".deb": "not-a-category"})

    with pytest.raises(ConfigError, match=r"not in categories\.allowed"):
        paths.load()


def test_category_allowlist_is_unique_and_slug_safe(tmp_path: Path) -> None:
    duplicate = write_config(tmp_path, categories=("misc", "misc"))
    with pytest.raises(ConfigError, match="duplicates"):
        duplicate.load()

    upper = write_config(tmp_path, categories=("Misc",))
    with pytest.raises(ConfigError, match="lowercase"):
        upper.load()

    traversal = write_config(tmp_path, categories=("../misc",))
    with pytest.raises(ConfigError, match="lowercase"):
        traversal.load()


def test_empty_model_name_is_rejected(tmp_path: Path) -> None:
    paths = write_config(tmp_path)
    text = paths.config_path.read_text(encoding="utf-8")
    paths.config_path.write_text(
        text.replace('name = "fixture-model"', 'name = "  "'),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=r"model\.name"):
        paths.load()


def test_duplicate_watch_roots_are_rejected(tmp_path: Path) -> None:
    paths = write_config(tmp_path)
    with paths.config_path.open("a", encoding="utf-8") as config_file:
        config_file.write(
            f"""
[[watch]]
path = "{paths.watch}"
destination_root = "{paths.destination}"
recursive = false
"""
        )

    with pytest.raises(ConfigError, match="unique"):
        paths.load()


def test_missing_invalid_and_directory_configs_have_clear_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "missing.toml")

    invalid = tmp_path / "invalid.toml"
    invalid.write_text("[general\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="Invalid TOML"):
        load_config(invalid)

    with pytest.raises(ConfigError, match="is a directory"):
        load_config(tmp_path)
