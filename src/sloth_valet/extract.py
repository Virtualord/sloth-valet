"""Bounded, failure-safe content extraction for local classification."""

from __future__ import annotations

import codecs
import mimetypes
import stat
import unicodedata
from pathlib import Path

from docx import Document
from pypdf import PdfReader

from sloth_valet.models import (
    ExtractionReason,
    ExtractionResult,
    FileMetadata,
)

DEFAULT_TEXT_CHARS = 1500
DEFAULT_PDF_PAGES = 3
BINARY_SNIFF_BYTES = 4096

MARKDOWN_EXTENSIONS = frozenset({".md", ".markdown", ".mdown"})
CSV_EXTENSIONS = frozenset({".csv", ".tsv"})
TEXT_EXTENSIONS = frozenset(
    {
        ".txt",
        ".text",
        ".log",
        ".rst",
        ".org",
        ".ini",
        ".cfg",
        ".conf",
    }
)
CODE_EXTENSIONS = frozenset(
    {
        ".bash",
        ".c",
        ".cc",
        ".clj",
        ".cpp",
        ".cs",
        ".css",
        ".go",
        ".h",
        ".hpp",
        ".html",
        ".java",
        ".js",
        ".json",
        ".jsx",
        ".kt",
        ".kts",
        ".lua",
        ".php",
        ".pl",
        ".py",
        ".r",
        ".rb",
        ".rs",
        ".scala",
        ".scss",
        ".sh",
        ".sql",
        ".swift",
        ".toml",
        ".ts",
        ".tsx",
        ".vue",
        ".xml",
        ".yaml",
        ".yml",
        ".zsh",
    }
)
TEXT_LIKE_EXTENSIONS = MARKDOWN_EXTENSIONS | CSV_EXTENSIONS | TEXT_EXTENSIONS | CODE_EXTENSIONS
IMAGE_EXTENSIONS = frozenset(
    {
        ".avif",
        ".bmp",
        ".gif",
        ".heic",
        ".heif",
        ".ico",
        ".jpeg",
        ".jpg",
        ".png",
        ".svg",
        ".tif",
        ".tiff",
        ".webp",
    }
)


def extract_file(
    path: Path,
    *,
    max_chars: int = DEFAULT_TEXT_CHARS,
    max_pdf_pages: int = DEFAULT_PDF_PAGES,
) -> ExtractionResult:
    """Extract bounded text without ever raising for an unreadable/corrupt file.

    Text-like files are decoded from a bounded prefix. PDFs use the first few pages,
    DOCX files use their paragraphs, and images or binary files return metadata only.
    """
    max_chars = max(1, max_chars)
    max_pdf_pages = max(1, max_pdf_pages)

    try:
        file_stat = path.lstat()
    except OSError as exc:
        return _result(path, 0, ExtractionReason.UNREADABLE, f"cannot stat file: {exc}")

    metadata = _metadata(path, file_stat.st_size)
    if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
        return _result(path, file_stat.st_size, ExtractionReason.UNREADABLE, "not a regular file")
    if file_stat.st_size == 0:
        return _result(path, 0, ExtractionReason.EMPTY_FILE)

    extension = metadata.extension
    if extension in IMAGE_EXTENSIONS:
        return _result(
            path,
            file_stat.st_size,
            ExtractionReason.IMAGE,
            "image files expose metadata only",
        )
    if extension == ".pdf":
        return _extract_pdf(path, metadata, max_chars=max_chars, max_pages=max_pdf_pages)
    if extension == ".docx":
        return _extract_docx(path, metadata, max_chars=max_chars)
    if extension in TEXT_LIKE_EXTENSIONS:
        return _extract_text(path, metadata, max_chars=max_chars)

    if _looks_binary(path):
        return _result(
            path,
            file_stat.st_size,
            ExtractionReason.BINARY,
            "binary files expose metadata only",
        )
    return _extract_text(path, metadata, max_chars=max_chars)


def _metadata(path: Path, size: int) -> FileMetadata:
    mime_type, _ = mimetypes.guess_type(path.name)
    return FileMetadata(
        name=path.name,
        extension=path.suffix.casefold(),
        size=size,
        mime_type=mime_type or "application/octet-stream",
    )


def _result(
    path: Path,
    size: int,
    reason: ExtractionReason,
    detail: str | None = None,
) -> ExtractionResult:
    return ExtractionResult(
        metadata=_metadata(path, size),
        text="",
        reason=reason,
        detail=detail,
    )


def _extract_text(path: Path, metadata: FileMetadata, *, max_chars: int) -> ExtractionResult:
    try:
        with path.open("rb") as handle:
            data = handle.read(max_chars * 4 + 16)
    except OSError as exc:
        return ExtractionResult(
            metadata=metadata,
            text="",
            reason=ExtractionReason.UNREADABLE,
            detail=f"cannot read file: {exc}",
        )

    if not data:
        return ExtractionResult(metadata=metadata, text="", reason=ExtractionReason.NO_TEXT)

    cleaned = _clean_text(_decode_bytes(data), max_chars)
    if not cleaned.strip():
        return ExtractionResult(metadata=metadata, text="", reason=ExtractionReason.NO_TEXT)
    return ExtractionResult(metadata=metadata, text=cleaned)


def _extract_pdf(
    path: Path,
    metadata: FileMetadata,
    *,
    max_chars: int,
    max_pages: int,
) -> ExtractionResult:
    try:
        reader = PdfReader(str(path))
        pages = []
        for index in range(min(len(reader.pages), max_pages)):
            pages.append(reader.pages[index].extract_text() or "")
    except Exception as exc:  # pypdf raises several parser-specific exception types.
        return ExtractionResult(
            metadata=metadata,
            text="",
            reason=ExtractionReason.CORRUPT,
            detail=f"cannot read PDF: {exc}",
        )

    cleaned = _clean_text("\n\n".join(pages), max_chars)
    if not cleaned.strip():
        return ExtractionResult(metadata=metadata, text="", reason=ExtractionReason.NO_TEXT)
    return ExtractionResult(metadata=metadata, text=cleaned)


def _extract_docx(path: Path, metadata: FileMetadata, *, max_chars: int) -> ExtractionResult:
    try:
        document = Document(str(path))
        paragraphs = [
            paragraph.text for paragraph in document.paragraphs if paragraph.text.strip()
        ]
    except Exception as exc:  # python-docx raises zip/XML/package-specific errors.
        return ExtractionResult(
            metadata=metadata,
            text="",
            reason=ExtractionReason.CORRUPT,
            detail=f"cannot read DOCX: {exc}",
        )

    cleaned = _clean_text("\n\n".join(paragraphs), max_chars)
    if not cleaned.strip():
        return ExtractionResult(metadata=metadata, text="", reason=ExtractionReason.NO_TEXT)
    return ExtractionResult(metadata=metadata, text=cleaned)


def _looks_binary(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            sample = handle.read(BINARY_SNIFF_BYTES)
    except OSError:
        return True
    if not sample:
        return False
    if sample.startswith((codecs.BOM_UTF8, codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return False
    if b"\x00" in sample:
        return True
    control_count = sum(
        1
        for byte in sample
        if byte < 32 and byte not in {9, 10, 13}
    )
    return control_count / len(sample) > 0.30


def _decode_bytes(data: bytes) -> str:
    if data.startswith(codecs.BOM_UTF8):
        return data.decode("utf-8-sig", errors="replace")
    if data.startswith(codecs.BOM_UTF16_LE):
        return data.decode("utf-16-le", errors="replace")
    if data.startswith(codecs.BOM_UTF16_BE):
        return data.decode("utf-16-be", errors="replace")
    return data.decode("utf-8", errors="replace")


def _clean_text(text: str, max_chars: int) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    without_controls = "".join(
        character
        for character in normalized
        if character in {"\n", "\t"} or unicodedata.category(character)[0] != "C"
    )
    return without_controls[:max_chars]
