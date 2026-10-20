from __future__ import annotations

from pathlib import Path

import httpx
from conftest import make_file, write_config

from sloth_valet.applier import ApplyStatus, sha256_file
from sloth_valet.journal import Journal, RunMode, RunStatus
from sloth_valet.models import ClassificationFailure, MoveStatus
from sloth_valet.ollama import OllamaClient
from sloth_valet.runner import execute_run


def test_dry_run_plans_without_moving_or_journaling_moves(tmp_path: Path) -> None:
    paths = write_config(tmp_path, fast_path={".deb": "installers"})
    source = make_file(paths.watch / "tool.deb")

    result = execute_run(paths.load(), apply=False, stability_delay_s=0)

    assert result.mode is RunMode.DRY_RUN
    assert result.moved == ()
    assert len(result.planned_moves) == 1
    assert result.outcomes[0].status is ApplyStatus.PLANNED
    assert source.exists()
    assert not (paths.destination / "installers" / "tool.deb").exists()
    with Journal(paths.load().journal_path) as journal:
        record = journal.last_run()
        assert record is not None
        assert record.mode is RunMode.DRY_RUN
        assert record.status is RunStatus.COMPLETED
        assert journal.moves_for_run(record.id) == ()


def test_apply_moves_fast_path_file_and_second_run_is_a_noop(tmp_path: Path) -> None:
    paths = write_config(tmp_path, fast_path={".deb": "installers"})
    source = make_file(paths.watch / "tool.deb")
    destination = paths.destination / "installers" / "tool.deb"

    first = execute_run(paths.load(), apply=True, stability_delay_s=0)
    second = execute_run(paths.load(), apply=True, stability_delay_s=0)

    assert first.mode is RunMode.APPLY
    assert len(first.moved) == 1
    assert not source.exists()
    assert destination.exists()
    assert second.scan.candidates == ()
    assert second.moved == ()
    assert second.planned_moves == ()
    with Journal(paths.load().journal_path) as journal:
        assert len(journal.moves_for_run(first.run_id)) == 1
        assert len(journal.moves_for_run(second.run_id)) == 0


def test_model_unavailable_routes_files_to_review(tmp_path: Path) -> None:
    paths = write_config(tmp_path)
    source = make_file(paths.watch / "notes.txt", content=b"some notes")
    config = paths.load()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"models": []}, request=request)

    client = OllamaClient(config, transport=httpx.MockTransport(handler))
    try:
        result = execute_run(
            config,
            apply=True,
            stability_delay_s=0,
            client=client,
        )
    finally:
        client.close()

    assert len(result.reviewed) == 1
    assert result.outcomes[0].status is ApplyStatus.REVIEWED
    assert result.outcomes[0].move.review_reason == ClassificationFailure.MODEL_UNAVAILABLE.value
    assert not source.exists()
    review_file = paths.destination / "_review" / "notes.txt"
    assert review_file.read_bytes() == b"some notes"
    with Journal(config.journal_path) as journal:
        record = journal.moves_for_run(result.run_id)[0]
        assert record.is_review is True
        assert record.status is MoveStatus.DONE


def test_review_disabled_leaves_classifier_failure_in_place(tmp_path: Path) -> None:
    paths = write_config(tmp_path, review_on_failure=False)
    source = make_file(paths.watch / "notes.txt", content=b"some notes")
    config = paths.load()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"models": []}, request=request)

    client = OllamaClient(config, transport=httpx.MockTransport(handler))
    try:
        result = execute_run(
            config,
            apply=True,
            stability_delay_s=0,
            client=client,
        )
    finally:
        client.close()

    assert result.reviewed == ()
    assert len(result.skipped) == 1
    assert result.skipped[0].reason == ClassificationFailure.MODEL_UNAVAILABLE.value
    assert source.exists()
    assert not (paths.destination / "_review").exists()


def test_next_run_reconciles_a_pending_row_and_then_retries_the_file(tmp_path: Path) -> None:
    paths = write_config(tmp_path, fast_path={".deb": "installers"})
    source = make_file(paths.watch / "tool.deb")
    stale_destination = paths.destination / "installers" / "stale.deb"
    config = paths.load()
    with Journal(config.journal_path) as journal:
        stale_run = journal.start_run(RunMode.APPLY)
        journal.record_pending(
            stale_run,
            source=source,
            destination=stale_destination,
            sha256=sha256_file(source),
            size=source.stat().st_size,
        )

    result = execute_run(config, apply=True, stability_delay_s=0)

    assert len(result.pending_repairs) == 1
    assert result.pending_repairs[0].status is MoveStatus.FAILED
    assert not source.exists()
    assert (paths.destination / "installers" / "tool.deb").exists()
    assert not stale_destination.exists()
