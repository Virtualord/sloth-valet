from __future__ import annotations

import pytest

from sloth_valet.models import (
    DecisionError,
    FilenameError,
    ModelDecision,
    parse_decision_json,
    sanitize_filename,
)

VALID_DECISION = """
{"category": "finance", "filename": "january-statement.pdf", "confidence": 0.93,
 "reason": "Bank statement"}
"""

# Fullwidth Latin letters exercise NFKC normalization without relying on visual lookalikes.
FULLWIDTH_QUARTERLY = "".join(chr(ord(character) + 0xFEE0) for character in "Quarterly") + ".pdf"


def test_valid_decision_is_parsed_strictly() -> None:
    decision = parse_decision_json(VALID_DECISION)

    assert decision == ModelDecision(
        category="finance",
        filename="january-statement.pdf",
        confidence=0.93,
        reason="Bank statement",
    )


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        "```json\n{\"category\": \"misc\", \"filename\": \"x\", \"confidence\": 0.9}\n```",
        '{"category": "misc", "filename": "x", "confidence": 0.9, "extra": true}',
        '{"category": "misc", "filename": "x"}',
        '{"category": "misc", "filename": "x", "confidence": 1.2}',
        '{"category": "misc", "filename": "x", "confidence": "high"}',
        '{"category": "misc", "filename": "x", "confidence": NaN}',
        '["misc", "x", 0.9]',
    ],
)
def test_invalid_model_output_is_rejected(payload: str) -> None:
    with pytest.raises(DecisionError):
        parse_decision_json(payload)


def test_oversized_model_output_is_rejected() -> None:
    with pytest.raises(DecisionError, match="size limit"):
        parse_decision_json(" " * (64 * 1024 + 1))


@pytest.mark.parametrize(
    ("raw", "original", "expected"),
    [
        ("January Statement.pdf", "statement.pdf", "January Statement.pdf"),
        ("  spaced-out name  .pdf ", "x.pdf", "spaced-out name.pdf"),
        ("report", "scan.pdf", "report.pdf"),
        ("report.txt", "scan.pdf", "report.txt.pdf"),
        ("REPORT.PDF", "scan.pdf", "REPORT.PDF"),
        (".hidden", "notes.pdf", "hidden.pdf"),
        ("a:b?.pdf", "scan.pdf", "a_b_.pdf"),
        (FULLWIDTH_QUARTERLY, "scan.pdf", "Quarterly.pdf"),
        ("a\u200bb.pdf", "scan.pdf", "a_b.pdf"),
        ("notes", "README", "notes"),
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
        "../escape.pdf",
        "sub/file.pdf",
        "sub\\file.pdf",
        "bad\x00name.pdf",
        "bad\nname.pdf",
        "...",
    ],
)
def test_unsafe_filenames_are_rejected(raw: str) -> None:
    with pytest.raises(FilenameError):
        sanitize_filename(raw, "original.pdf")


def test_long_filename_is_capped_and_keeps_its_extension() -> None:
    safe = sanitize_filename("x" * 400, "original.pdf")

    assert len(safe.encode("utf-8")) <= 255
    assert safe.endswith(".pdf")
    assert safe.startswith("x")
