from __future__ import annotations

from pathlib import Path

from conftest import EnvPaths, make_file, write_config

from sloth_valet.models import (
    Classification,
    ClassificationFailure,
    ClassificationResult,
    FileCandidate,
)
from sloth_valet.planner import ClassifiedFile, Planner


def _candidate(paths: EnvPaths, name: str = "invoice.pdf") -> FileCandidate:
    source = make_file(paths.watch / name, content=b"fixture content")
    file_stat = source.stat()
    return FileCandidate(
        source=source,
        watch_root=paths.watch,
        destination_root=paths.destination,
        size=file_stat.st_size,
        mtime_ns=file_stat.st_mtime_ns,
        device=file_stat.st_dev,
        inode=file_stat.st_ino,
    )


def _success() -> ClassificationResult:
    return ClassificationResult(
        classification=Classification(
            category="finance",
            subfolder="statements",
            new_name="january-statement.pdf",
            confidence=0.95,
            reason="Bank statement",
        ),
        parsed_json=True,
    )


def test_plan_resolves_category_subfolder_and_safe_name(tmp_path: Path) -> None:
    paths = write_config(tmp_path)
    candidate = _candidate(paths)

    plan = Planner(paths.load()).plan([ClassifiedFile(candidate, _success())])

    assert not plan.skipped
    assert len(plan.moves) == 1
    move = plan.moves[0]
    assert move.destination == (
        paths.destination / "finance" / "statements" / "january-statement.pdf"
    )
    assert not move.is_review


def test_collisions_append_numbered_suffixes_on_disk_and_within_plan(tmp_path: Path) -> None:
    paths = write_config(tmp_path)
    existing = paths.destination / "finance" / "statements" / "january-statement.pdf"
    existing.parent.mkdir(parents=True)
    existing.write_text("already here", encoding="utf-8")
    first = _candidate(paths, "one.pdf")
    second = _candidate(paths, "two.pdf")

    plan = Planner(paths.load()).plan(
        [
            ClassifiedFile(first, _success()),
            ClassifiedFile(second, _success()),
        ]
    )

    assert [move.destination.name for move in plan.moves] == [
        "january-statement-1.pdf",
        "january-statement-2.pdf",
    ]
    assert existing.read_text(encoding="utf-8") == "already here"


def test_leaves_same_name_collision_without_overwriting_a_symlink(tmp_path: Path) -> None:
    paths = write_config(tmp_path)
    destination = paths.destination / "finance" / "statements" / "january-statement.pdf"
    destination.parent.mkdir(parents=True)
    outside = tmp_path / "outside.pdf"
    outside.write_text("outside", encoding="utf-8")
    destination.symlink_to(outside)
    candidate = _candidate(paths)

    plan = Planner(paths.load()).plan([ClassifiedFile(candidate, _success())])

    assert plan.moves[0].destination.name == "january-statement-1.pdf"
    assert destination.is_symlink()
    assert outside.read_text(encoding="utf-8") == "outside"


def test_classifier_failure_routes_to_review_folder(tmp_path: Path) -> None:
    paths = write_config(tmp_path)
    candidate = _candidate(paths, "Messy Notes.pdf")
    failure = ClassificationResult(
        classification=None,
        failure=ClassificationFailure.LOW_CONFIDENCE,
        detail="confidence 0.2 is below 0.7",
    )

    plan = Planner(paths.load()).plan([ClassifiedFile(candidate, failure)])

    assert len(plan.moves) == 1
    move = plan.moves[0]
    assert move.is_review
    assert move.destination == paths.destination / "_review" / "messy-notes.pdf"
    assert move.review_reason == "low_confidence"
    assert move.review_detail == "confidence 0.2 is below 0.7"


def test_review_collision_is_also_numbered(tmp_path: Path) -> None:
    paths = write_config(tmp_path)
    review_file = paths.destination / "_review" / "messy-notes.pdf"
    review_file.parent.mkdir(parents=True)
    review_file.write_text("existing", encoding="utf-8")
    candidate = _candidate(paths, "Messy Notes.pdf")
    failure = ClassificationResult(
        classification=None,
        failure=ClassificationFailure.MODEL_UNAVAILABLE,
    )

    plan = Planner(paths.load()).plan([ClassifiedFile(candidate, failure)])

    assert plan.moves[0].destination == paths.destination / "_review" / "messy-notes-1.pdf"


def test_review_disabled_leaves_file_untouched(tmp_path: Path) -> None:
    paths = write_config(tmp_path, review_on_failure=False)
    candidate = _candidate(paths)
    failure = ClassificationResult(
        classification=None,
        failure=ClassificationFailure.INVALID_JSON,
        detail="bad model output",
    )

    plan = Planner(paths.load()).plan([ClassifiedFile(candidate, failure)])

    assert plan.moves == ()
    assert len(plan.skipped) == 1
    assert plan.skipped[0].candidate == candidate
    assert plan.skipped[0].reason == "invalid_json"
    assert "review_on_failure is false" in (plan.skipped[0].detail or "")


def test_traversal_in_model_output_is_slugified_before_joining(tmp_path: Path) -> None:
    paths = write_config(tmp_path)
    candidate = _candidate(paths)
    malicious = ClassificationResult(
        classification=Classification(
            category="finance",
            subfolder="../../outside",
            new_name="../../etc/passwd",
            confidence=0.99,
            reason="test",
        ),
        parsed_json=True,
    )

    plan = Planner(paths.load()).plan([ClassifiedFile(candidate, malicious)])

    move = plan.moves[0]
    assert move.destination == paths.destination / "finance" / "outside" / "etc-passwd.pdf"
    assert not move.is_review


def test_disallowed_category_is_routed_to_review_not_used_as_a_path(tmp_path: Path) -> None:
    paths = write_config(tmp_path)
    candidate = _candidate(paths)
    result = ClassificationResult(
        classification=Classification(
            category="../escape",
            subfolder="",
            new_name="passwd",
            confidence=0.99,
            reason="test",
        ),
        parsed_json=True,
    )

    plan = Planner(paths.load()).plan([ClassifiedFile(candidate, result)])

    assert len(plan.moves) == 1
    move = plan.moves[0]
    assert move.is_review
    assert move.review_reason == "invalid_destination"
    assert move.destination == paths.destination / "_review" / "invoice.pdf"


def test_symlinked_category_directory_cannot_escape_the_destination_root(
    tmp_path: Path,
) -> None:
    paths = write_config(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (paths.destination / "finance").symlink_to(outside, target_is_directory=True)
    candidate = _candidate(paths)

    plan = Planner(paths.load()).plan([ClassifiedFile(candidate, _success())])

    assert len(plan.moves) == 1
    move = plan.moves[0]
    assert move.is_review
    assert move.review_reason == "invalid_destination"
    assert move.destination == paths.destination / "_review" / "invoice.pdf"
    assert not (outside / "january-statement.pdf").exists()
