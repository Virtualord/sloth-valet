"""Configuration loading and validation for Sloth Valet."""

from __future__ import annotations

import os
import re
import tomllib
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

CATEGORY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,59}$")
EXAMPLE_CONFIG_NAME = "valet.example.toml"


class ConfigError(ValueError):
    """Raised when a configuration file cannot be read or validated."""


def default_config_path() -> Path:
    """Return the override or the default ~/.config/sloth-valet/valet.toml."""
    override = os.environ.get("SLOTH_VALET_CONFIG")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "sloth-valet" / "valet.toml"


def example_config_text() -> str:
    """Load the packaged example written by ``valet config init``."""
    resource = resources.files("sloth_valet").joinpath(EXAMPLE_CONFIG_NAME)
    return resource.read_text(encoding="utf-8")


class _ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


def _base_directory(info: ValidationInfo) -> Path:
    if info.context:
        base = info.context.get("base_dir")
        if isinstance(base, Path):
            return base
    return Path.cwd()


def _absolute_config_path(value: Any, info: ValidationInfo) -> Path:
    if isinstance(value, str) and not value.strip():
        raise ValueError("path cannot be empty")
    try:
        candidate = Path(value).expanduser()
    except (TypeError, ValueError) as exc:
        raise ValueError("path must be a string") from exc
    if "\x00" in str(candidate):
        raise ValueError("path must not contain NUL")
    if not candidate.is_absolute():
        candidate = _base_directory(info) / candidate
    return Path(os.path.abspath(candidate))


class GeneralConfig(_ConfigModel):
    dry_run: bool = True
    settle_minutes: int = Field(default=30, ge=0)
    max_file_size_mb: int = Field(default=100, ge=1)
    review_folder: str = "_review"
    reports_dir: Path = Path("~/.local/share/sloth-valet/reports")

    @field_validator("reports_dir", mode="before")
    @classmethod
    def resolve_state_path(cls, value: Any, info: ValidationInfo) -> Path:
        return _absolute_config_path(value, info)

    @field_validator("review_folder")
    @classmethod
    def validate_review_folder(cls, value: str) -> str:
        if (
            not value
            or value != value.strip()
            or value in {".", ".."}
            or "/" in value
            or "\\" in value
            or "\x00" in value
        ):
            raise ValueError("review_folder must be a single directory name")
        return value


class ModelConfig(_ConfigModel):
    host: str = "http://localhost:11434"
    name: str
    temperature: float = Field(default=0, ge=0, le=2)
    timeout_s: float = Field(default=60, gt=0)
    min_confidence: float = Field(default=0.7, ge=0, le=1)
    text_chars: int = Field(default=1500, ge=1, le=200_000)

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        value = value.strip()
        if not value or any(character.isspace() for character in value):
            raise ValueError("model host must be a single HTTP(S) URL")
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("model host must use http or https")
        if parts.username or parts.password:
            raise ValueError("model host must not contain credentials")
        if parts.query or parts.fragment:
            raise ValueError("model host must not contain a query or fragment")
        try:
            _ = parts.port
        except ValueError as exc:
            raise ValueError("model host has an invalid port") from exc
        return value.rstrip("/")

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("model name cannot be empty")
        return value


class WatchConfig(_ConfigModel):
    path: Path
    destination_root: Path
    recursive: bool = False

    @field_validator("path", "destination_root", mode="before")
    @classmethod
    def resolve_watch_path(cls, value: Any, info: ValidationInfo) -> Path:
        return _absolute_config_path(value, info)


class ProtectConfig(_ConfigModel):
    paths: list[Path] = Field(default_factory=list)
    globs: list[str] = Field(default_factory=list)

    @field_validator("paths", mode="before")
    @classmethod
    def resolve_protected_paths(cls, value: Any, info: ValidationInfo) -> list[Path]:
        if value is None:
            return []
        if isinstance(value, (str, Path)):
            value = [value]
        if not isinstance(value, (list, tuple)):
            raise ValueError("protect.paths must be a list of paths")
        return [_absolute_config_path(item, info) for item in value]

    @field_validator("globs")
    @classmethod
    def validate_globs(cls, value: list[str]) -> list[str]:
        for pattern in value:
            if not pattern or "\x00" in pattern:
                raise ValueError("protect.globs entries must be non-empty and contain no NUL")
        return value


class CategoriesConfig(_ConfigModel):
    allowed: list[str] = Field(min_length=1)

    @field_validator("allowed")
    @classmethod
    def validate_categories(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("categories.allowed must not contain duplicates")
        for category in value:
            if not CATEGORY_PATTERN.fullmatch(category):
                raise ValueError(
                    "categories.allowed entries must be lowercase letters, digits, underscores, "
                    "or hyphens, start with a letter or digit, and be at most 60 characters"
                )
        return value


class ValetConfig(_ConfigModel):
    general: GeneralConfig
    model: ModelConfig
    watch: list[WatchConfig] = Field(min_length=1)
    protect: ProtectConfig = Field(default_factory=ProtectConfig)
    categories: CategoriesConfig
    fast_path: dict[str, str] = Field(default_factory=dict)

    @field_validator("fast_path", mode="before")
    @classmethod
    def normalize_fast_path(cls, value: Any) -> dict[str, str]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("fast_path must be a table")
        normalized: dict[str, str] = {}
        for raw_suffix, category in value.items():
            if not isinstance(raw_suffix, str):
                raise ValueError("fast_path keys must be strings")
            suffix = raw_suffix.lower()
            if (
                not suffix.startswith(".")
                or suffix == "."
                or "/" in suffix
                or "\\" in suffix
                or "\x00" in suffix
            ):
                raise ValueError("fast_path keys must be filename extensions such as '.deb'")
            if not isinstance(category, str):
                raise ValueError("fast_path values must be category names")
            if suffix in normalized:
                raise ValueError(f"fast_path contains duplicate extension {suffix!r}")
            normalized[suffix] = category
        return normalized

    @model_validator(mode="after")
    def validate_cross_references(self) -> ValetConfig:
        allowed = set(self.categories.allowed)
        unknown_targets = sorted(set(self.fast_path.values()) - allowed)
        if unknown_targets:
            raise ValueError(
                f"fast_path targets {unknown_targets!r} are not in categories.allowed"
            )

        paths = [watch.path for watch in self.watch]
        if len(paths) != len(set(paths)):
            raise ValueError("watch paths must be unique")

        for watch in self.watch:
            watch_root = _resolve_for_nesting(watch.path)
            for destination_owner in self.watch:
                destination = _resolve_for_nesting(destination_owner.destination_root)
                if destination == watch_root or destination.is_relative_to(watch_root):
                    raise ValueError(
                        f"destination root {destination_owner.destination_root} is inside "
                        f"watch root {watch.path}"
                    )

        return self


def _resolve_for_nesting(path: Path) -> Path:
    """Resolve symlinks for overlap checks while allowing roots to not exist yet."""
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError):
        return Path(os.path.abspath(path))


def load_config(path: Path) -> ValetConfig:
    """Read a TOML config and resolve relative paths against its directory."""
    config_path = path.expanduser().resolve()
    try:
        with config_path.open("rb") as config_file:
            raw = tomllib.load(config_file)
    except FileNotFoundError as exc:
        raise ConfigError(f"Config file not found: {config_path}") from exc
    except IsADirectoryError as exc:
        raise ConfigError(f"Config path is a directory: {config_path}") from exc
    except OSError as exc:
        raise ConfigError(f"Could not read config {config_path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {config_path}: {exc}") from exc

    try:
        return ValetConfig.model_validate(raw, context={"base_dir": config_path.parent})
    except ValidationError as exc:
        details = []
        for error in exc.errors(include_url=False):
            location = ".".join(str(part) for part in error["loc"]) or "<root>"
            details.append(f"  {location}: {error['msg']}")
        message = "\n".join(details)
        raise ConfigError(f"Invalid config {config_path}:\n{message}") from exc
