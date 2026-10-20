from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from sloth_valet.models import (
    MAX_FILENAME_CHARS,
    Classification,
    ClassificationError,
    FileCandidate,
    FilenameError,
    MoveRecord,
    MoveStatus,
    PlannedMove,
    parse_classification_json,
    sanitize_filename,
    sanitize_subfolder,
)

VALID_CLASSIFICATION = """
{"category": "finance", "subfolder": "statements",
 "new_name": "january-statement.pdf", "confidence": 0.93,
 "reason": "Bank statement"}
"""

# Fullwidth Latin letters exercise NFKC normalization without relying on visual lookalikes.
FULLWIDTH_QUARTERLY = "".join(chr(ord(character) + 0xFEE0) for character in "Quarterly") + ".pdf"


def test_valid_classification_is_parsed_strictly() -> None:
    classification = parse_classification_json(VALID_CLASSIFICATION)

    assert classification == Classification(
        category="finance",
        subfolder="statements",
        new_name="january-statement.pdf",
        confidence=0.93,
        reason="Bank statement",
    )


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        '```json\n{"category": "misc", "subfolder": "", "new_name": "x", '
        '"confidence": 0.9, "reason": ""}\n```',
        '{"category": "misc", "subfolder": "", "new_name": "x", '
        '"confidence": 0.9, "reason": "", "extra": true}',
        '{"category": "misc", "new_name": "x", "confidence": 0.9, "reason": ""}',
        '{"category": "misc", "subfolder": "", "confidence": 0.9, "reason": ""}',
        '{"category": "misc", "subfolder": "", "new_name": "x", "confidence": 0.9}',
        '{"category": "misc", "subfolder": "", "new_name": "x", "confidence": 1.2, '
        '"reason": ""}',
        '{"category": "misc", "subfolder": "", "new_name": "x", '
        '"confidence": "high", "reason": ""}',
        '{"category": "misc", "subfolder": "", "new_name": "x", '
        '"confidence": NaN, "reason": ""}',
        '["misc", "", "x", 0.9, ""]',
    ],
)
def test_invalid_model_output_is_rejected(payload: str) -> None:
    with pytest.raises(ClassificationError):
        parse_classification_json(payload)


def test_oversized_model_output_is_rejected() -> None:
    with pytest.raises(ClassificationError, match="size limit"):
        parse_classification_json(" " * (64 * 1024 + 1))


def test_pipeline_domain_models_compose() -> None:
    classification = parse_classification_json(VALID_CLASSIFICATION)
    candidate = FileCandidate(
        source=Path("/watch/statement.pdf"),
        watch_root=Path("/watch"),
        destination_root=Path("/sorted"),
        size=123,
        mtime_ns=456,
        device=7,
        inode=8,
    )
    destination = Path("/sorted/finance/statements/january-statement.pdf")
    planned = PlannedMove(
        candidate=candidate,
        classification=classification,
        destination=destination,
    )
    created_at = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)

    assert planned.source == candidate.source
    assert planned.classification is not None
    assert planned.classification.category == "finance"
    record = MoveRecord(
        source=planned.source,
        destination=planned.destination,
        status=MoveStatus.PENDING,
        created_at=created_at,
    )
    assert record.status == MoveStatus.PENDING
    assert record.completed_at is None
    assert record.id is None


@pytest.mark.parametrize(
    ("raw", "original", "expected"),
    [
        ("January Statement.pdf", "statement.pdf", "january-statement.pdf"),
        ("  spaced-out name  .pdf ", "x.pdf", "spaced-out-name.pdf"),
        ("report", "scan.pdf", "report.pdf"),
        ("report.txt", "scan.pdf", "report.pdf"),
        ("REPORT.PDF", "scan.pdf", "report.pdf"),
        (".hidden", "notes.pdf", "hidden.pdf"),
        ("a:b?.pdf", "scan.pdf", "a-b.pdf"),
        (FULLWIDTH_QUARTERLY, "scan.pdf", "quarterly.pdf"),
        ("a\u200bb.pdf", "scan.pdf", "ab.pdf"),
        ("notes", "README", "notes"),
        ("../../etc/passwd", "scan.pdf", "etc-passwd.pdf"),
        ("sub\\file.txt", "scan.pdf", "sub-file.pdf"),
        ("bad\x00name.pdf", "scan.pdf", "bad-name.pdf"),
    ],
)
def test_filename_sanitization(raw: str, original: str, expected: str) -> None:
    assert sanitize_filename(raw, original) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        ".",
        "..",
        "...",
        ".pdf",
    ],
)
def test_unsafe_filenames_are_rejected(raw: str) -> None:
    with pytest.raises(FilenameError):
        sanitize_filename(raw, "original.pdf")


def test_sanitized_filename_has_no_path_or_hidden_prefix() -> None:
    safe = sanitize_filename("../../.config/passwd", "original.pdf")

    assert safe == "config-passwd.pdf"
    assert "/" not in safe
    assert "\\" not in safe
    assert not safe.startswith(".")


def test_sanitized_subfolder_is_one_safe_component() -> None:
    assert sanitize_subfolder("") == ""
    assert sanitize_subfolder("Statements/2026") == "statements-2026"
    assert sanitize_subfolder("../../etc") == "etc"
    assert sanitize_subfolder("...") == ""
    assert len(sanitize_subfolder("x" * 100)) == 60


def test_long_filename_is_capped_and_keeps_its_extension() -> None:
    safe = sanitize_filename("x" * 400, "original.pdf")

    assert len(safe) <= MAX_FILENAME_CHARS
    assert len(safe) == MAX_FILENAME_CHARS
    assert safe.endswith(".pdf")
    assert safe.startswith("x")
