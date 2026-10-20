from __future__ import annotations

from pathlib import Path

import pytest
from conftest import write_config

from sloth_valet.config import (
    ConfigError,
    default_config_path,
    example_config_text,
    load_config,
)

PROJECT_ROOT = Path(__file__).parents[1]

MINIMAL_RELATIVE_CONFIG = """
[general]
reports_dir = "state/reports"

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
    assert config.general.max_file_size_mb == 100
    assert config.general.review_on_failure is True
    assert config.journal_path == paths.reports_dir.parent / "journal.sqlite3"
    assert config.watch[0].path == paths.watch
    assert config.watch[0].destination_root == paths.destination
    assert config.watch[0].recursive is True
    assert config.general.reports_dir == paths.reports_dir
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


def test_non_positive_file_size_cap_is_rejected(tmp_path: Path) -> None:
    paths = write_config(tmp_path, max_file_size_mb=0)

    with pytest.raises(ConfigError, match="max_file_size_mb"):
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


def test_missing_required_keys_are_rejected(tmp_path: Path) -> None:
    missing_name = tmp_path / "missing-name.toml"
    missing_name.write_text(
        MINIMAL_RELATIVE_CONFIG.replace('name = "fixture-model"\n', ""),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match=r"model\.name"):
        load_config(missing_name)

    missing_categories = tmp_path / "missing-categories.toml"
    missing_categories.write_text(
        MINIMAL_RELATIVE_CONFIG.replace('[categories]\nallowed = ["misc"]\n', ""),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="categories"):
        load_config(missing_categories)

    missing_watch = tmp_path / "missing-watch.toml"
    missing_watch.write_text(
        MINIMAL_RELATIVE_CONFIG.replace(
            '[[watch]]\npath = "watch"\ndestination_root = "sorted"\n', ""
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="watch"):
        load_config(missing_watch)


def test_bad_paths_are_rejected(tmp_path: Path) -> None:
    paths = write_config(tmp_path)
    text = paths.config_path.read_text(encoding="utf-8")

    paths.config_path.write_text(
        text.replace(f'path = "{paths.watch}"', 'path = ""'),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="path cannot be empty"):
        paths.load()

    paths.config_path.write_text(
        text.replace(f'path = "{paths.watch}"', "path = 42"),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="path must be a string"):
        paths.load()


def test_tilde_paths_are_expanded(tmp_path: Path) -> None:
    paths = write_config(tmp_path)
    text = paths.config_path.read_text(encoding="utf-8")
    paths.config_path.write_text(
        text.replace(f'path = "{paths.watch}"', 'path = "~/valet-test-watch"'),
        encoding="utf-8",
    )

    config = paths.load()

    assert config.watch[0].path == Path.home() / "valet-test-watch"


@pytest.mark.parametrize("destination_kind", ["child", "equal"])
def test_destination_root_inside_watch_is_rejected(
    tmp_path: Path,
    destination_kind: str,
) -> None:
    watch = tmp_path / "watch"
    destination = watch / "Sorted" if destination_kind == "child" else watch
    paths = write_config(tmp_path, watch_path=watch, destination=destination)

    with pytest.raises(ConfigError, match="inside watch root"):
        paths.load()


def test_reports_directory_inside_watch_is_rejected(tmp_path: Path) -> None:
    paths = write_config(tmp_path)
    text = paths.config_path.read_text(encoding="utf-8")
    paths.config_path.write_text(
        text.replace(
            f'reports_dir = "{paths.reports_dir}"',
            f'reports_dir = "{paths.watch / "reports"}"',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="reports_dir"):
        paths.load()


def test_destination_inside_a_different_watch_is_rejected(tmp_path: Path) -> None:
    paths = write_config(tmp_path)
    second_watch = tmp_path / "second-watch"
    nested_destination = paths.watch / "Sorted"
    with paths.config_path.open("a", encoding="utf-8") as config_file:
        config_file.write(
            f"""
[[watch]]
path = "{second_watch}"
destination_root = "{nested_destination}"
recursive = false
"""
        )

    with pytest.raises(ConfigError, match="inside watch root"):
        paths.load()


def test_destination_symlinked_into_watch_is_rejected(tmp_path: Path) -> None:
    paths = write_config(tmp_path)
    inside_watch = paths.watch / "Sorted"
    inside_watch.mkdir()
    destination_link = tmp_path / "destination-link"
    destination_link.symlink_to(inside_watch, target_is_directory=True)
    text = paths.config_path.read_text(encoding="utf-8")
    paths.config_path.write_text(
        text.replace(
            f'destination_root = "{paths.destination}"',
            f'destination_root = "{destination_link}"',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="inside watch root"):
        paths.load()


def test_default_config_path_uses_home_and_honors_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SLOTH_VALET_CONFIG", raising=False)
    assert default_config_path() == Path.home() / ".config" / "sloth-valet" / "valet.toml"

    monkeypatch.setenv("SLOTH_VALET_CONFIG", "~/custom/valet.toml")
    assert default_config_path() == Path("~/custom/valet.toml").expanduser()


def test_shipped_example_matches_package_and_loads() -> None:
    example_path = PROJECT_ROOT / "valet.example.toml"
    assert example_path.read_text(encoding="utf-8") == example_config_text()

    config = load_config(example_path)

    assert config.model.name == "set-me-after-checking-ollama-list"
    assert config.general.max_file_size_mb == 100
    assert config.fast_path[".appimage"] == "installers"
    assert config.watch[0].path == Path.home() / "Downloads"
