"""Domain objects and strict schemas for untrusted model output."""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

MAX_CLASSIFICATION_BYTES = 64 * 1024
MAX_FILENAME_CHARS = 80
MAX_SUBFOLDER_CHARS = 60
_EXTENSION_PATTERN = re.compile(r"^\.[A-Za-z0-9][A-Za-z0-9._-]{0,20}$")
_NON_SLUG = re.compile(r"[^a-z0-9]+")


class SkipReason(StrEnum):
    """Reasons a path was never shown to the model."""

    SYMLINK = "symlink"
    NOT_REGULAR = "not_regular"
    PROTECTED = "protected"
    IGNORED = "ignored"
    PARTIAL_DOWNLOAD = "partial_download"
    UNSETTLED = "unsettled"
    TOO_LARGE = "too_large"
    CHANGED = "changed"
    OUTSIDE_WATCH = "outside_watch"
    DUPLICATE = "duplicate"
    UNREADABLE = "unreadable"


@dataclass(frozen=True, slots=True)
class FileCandidate:
    """A settled regular file that passed every pre-model safety check."""

    source: Path
    watch_root: Path
    destination_root: Path
    size: int
    mtime_ns: int
    device: int
    inode: int

    @property
    def relative_source(self) -> Path:
        return self.source.relative_to(self.watch_root)

    def matches_stat(self, stat_result: os.stat_result) -> bool:
        """Return whether a later stat still identifies the exact same file."""
        return (
            stat_result.st_size == self.size
            and stat_result.st_mtime_ns == self.mtime_ns
            and stat_result.st_dev == self.device
            and stat_result.st_ino == self.inode
        )


@dataclass(frozen=True, slots=True)
class SkippedPath:
    path: Path
    reason: SkipReason
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class ScanError:
    path: Path
    message: str


@dataclass(frozen=True, slots=True)
class ScanResult:
    candidates: tuple[FileCandidate, ...]
    skipped: tuple[SkippedPath, ...]
    errors: tuple[ScanError, ...]


class ExtractionReason(StrEnum):
    """Why a file produced no extractable text."""

    EMPTY_FILE = "empty_file"
    IMAGE = "image"
    BINARY = "binary"
    NO_TEXT = "no_text"
    CORRUPT = "corrupt"
    UNREADABLE = "unreadable"


@dataclass(frozen=True, slots=True)
class FileMetadata:
    """Metadata that is safe to share with the model for every file kind."""

    name: str
    extension: str
    size: int
    mime_type: str


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    """Bounded text plus the metadata used when no text can be extracted."""

    metadata: FileMetadata
    text: str
    reason: ExtractionReason | None = None
    detail: str | None = None

    @property
    def has_text(self) -> bool:
        return bool(self.text)


class Classification(BaseModel):
    """The only shape accepted from the local model."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    category: str = Field(min_length=1, max_length=60)
    subfolder: str = Field(max_length=100)
    new_name: str = Field(min_length=1, max_length=512)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    reason: str = Field(max_length=500)


class ClassificationFailure(StrEnum):
    """Failure modes that must leave a file untouched or route it to review."""

    MODEL_UNAVAILABLE = "model_unavailable"
    TIMEOUT = "timeout"
    HTTP_ERROR = "http_error"
    INVALID_RESPONSE = "invalid_response"
    INVALID_JSON = "invalid_json"
    CATEGORY_NOT_ALLOWED = "category_not_allowed"
    LOW_CONFIDENCE = "low_confidence"
    INVALID_FILENAME = "invalid_filename"


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    """One classification attempt; exactly one of classification/failure is set."""

    classification: Classification | None
    failure: ClassificationFailure | None = None
    detail: str | None = None
    used_fast_path: bool = False
    parsed_json: bool = False

    def __post_init__(self) -> None:
        if (self.classification is None) == (self.failure is None):
            raise ValueError("exactly one of classification or failure must be set")


@dataclass(frozen=True, slots=True)
class PlannedMove:
    """A validated candidate paired with its intended destination."""

    candidate: FileCandidate
    classification: Classification
    destination: Path

    @property
    def source(self) -> Path:
        return self.candidate.source


class MoveStatus(StrEnum):
    PENDING = "pending"
    DONE = "done"
    REVERTED = "reverted"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class MoveRecord:
    """One journaled move, pending before the operation and done afterwards."""

    source: Path
    destination: Path
    status: MoveStatus
    created_at: datetime
    completed_at: datetime | None = None
    id: int | None = None


class ClassificationError(ValueError):
    """Raised when model output is not a valid classification."""


class FilenameError(ValueError):
    """Raised when model output cannot be reduced to a safe filename."""


def parse_classification_json(payload: str) -> Classification:
    """Parse exactly one JSON classification without repairing model output."""
    if len(payload.encode("utf-8")) > MAX_CLASSIFICATION_BYTES:
        raise ClassificationError("model output exceeds the classification size limit")
    try:
        return Classification.model_validate_json(payload)
    except ValidationError as exc:
        raise ClassificationError(
            "model output does not match the classification schema"
        ) from exc


def _strip_proposed_extension(value: str) -> str:
    suffix = Path(value).suffix
    if suffix and _EXTENSION_PATTERN.fullmatch(suffix):
        return value[: -len(suffix)]
    return value


def _slugify(value: str, max_chars: int) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    slug = _NON_SLUG.sub("-", ascii_text.casefold()).strip("-")
    return slug[:max_chars].strip("-")


def sanitize_filename(raw: str, original_name: str) -> str:
    """Reduce model-proposed text to one safe, lowercase filename component.

    The source extension is retained, all other path-like text is slugified away,
    leading dots are impossible, and the complete name is capped at 80 characters.
    """
    if not isinstance(raw, str):
        raise FilenameError("filename must be a string")

    normalized = unicodedata.normalize("NFKC", raw).strip()
    if not normalized:
        raise FilenameError("filename is empty")

    original_suffix = Path(original_name).suffix
    if not _EXTENSION_PATTERN.fullmatch(original_suffix):
        original_suffix = ""
    if len(original_suffix) >= MAX_FILENAME_CHARS:
        raise FilenameError("filename extension is too long")
    if original_suffix and normalized.casefold() == original_suffix.casefold():
        raise FilenameError("filename is only the source extension")

    stem_source = _strip_proposed_extension(normalized)
    stem = _slugify(stem_source, MAX_FILENAME_CHARS - len(original_suffix))
    if not stem:
        raise FilenameError("filename has no usable characters")
    return f"{stem}{original_suffix}"


def sanitize_subfolder(raw: str) -> str:
    """Return one optional safe path component with no separators or leading dots."""
    if not isinstance(raw, str):
        raise FilenameError("subfolder must be a string")
    normalized = unicodedata.normalize("NFKC", raw).strip()
    if not normalized:
        return ""
    return _slugify(normalized, MAX_SUBFOLDER_CHARS)
