"""Plain-code orchestration of scan, extract, classify, plan, apply, and journal."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from sloth_valet.applier import Applier, ApplyStatus, MoveOutcome, PendingRepair
from sloth_valet.classifier import Classifier
from sloth_valet.config import ValetConfig
from sloth_valet.extract import extract_file
from sloth_valet.journal import Journal, JournalError, RunMode, RunRecord, RunStatus
from sloth_valet.models import ClassificationResult, ScanResult
from sloth_valet.ollama import OllamaClient
from sloth_valet.planner import ClassifiedFile, Plan, Planner, PlanSkip
from sloth_valet.report import notify_if_enabled, write_report
from sloth_valet.scanner import DEFAULT_STABILITY_DELAY_S, Scanner


@dataclass(frozen=True, slots=True)
class RunResult:
    """Everything the report, CLI, and tests need to describe one run."""

    run_id: str
    mode: RunMode
    started_at: datetime
    finished_at: datetime | None
    scan: ScanResult
    plan: Plan
    outcomes: tuple[MoveOutcome, ...]
    pending_repairs: tuple[PendingRepair, ...]
    journal_path: Path
    report_path: Path | None = None

    @property
    def moved(self) -> tuple[MoveOutcome, ...]:
        return tuple(
            outcome
            for outcome in self.outcomes
            if outcome.status is ApplyStatus.MOVED
        )

    @property
    def reviewed(self) -> tuple[MoveOutcome, ...]:
        return tuple(
            outcome
            for outcome in self.outcomes
            if outcome.status is ApplyStatus.REVIEWED
            or (outcome.status is ApplyStatus.PLANNED and outcome.move.is_review)
        )

    @property
    def planned_moves(self) -> tuple[MoveOutcome, ...]:
        return tuple(
            outcome
            for outcome in self.outcomes
            if outcome.status is ApplyStatus.PLANNED and not outcome.move.is_review
        )

    @property
    def failed(self) -> tuple[MoveOutcome, ...]:
        return tuple(
            outcome for outcome in self.outcomes if outcome.status is ApplyStatus.FAILED
        )

    @property
    def skipped(self) -> tuple[PlanSkip, ...]:
        return self.plan.skipped


def execute_run(
    config: ValetConfig,
    *,
    apply: bool,
    stability_delay_s: float = DEFAULT_STABILITY_DELAY_S,
    client: OllamaClient | None = None,
    journal: Journal | None = None,
) -> RunResult:
    """Run the pipeline once. Dry-run mode never journals or moves a file."""
    owns_client = client is None
    active_client = client or OllamaClient(config)
    active_journal = journal or Journal(config.journal_path)
    owns_journal = journal is None
    mode = RunMode.APPLY if apply else RunMode.DRY_RUN
    run_id = active_journal.start_run(mode)

    try:
        applier = Applier(config, active_journal)
        pending_repairs = applier.reconcile_pending()
        scan_result = Scanner(config, stability_delay_s=stability_delay_s).scan()
        classifier = Classifier(config, active_client)

        classified: list[ClassifiedFile] = []
        for candidate in scan_result.candidates:
            fast_path = classifier.fast_path_classification(candidate)
            if fast_path is not None:
                classified.append(ClassifiedFile(candidate, fast_path))
                continue
            extraction = extract_file(
                candidate.source,
                max_chars=config.model.text_chars,
            )
            classification_result: ClassificationResult = classifier.classify(
                candidate, extraction
            )
            classified.append(ClassifiedFile(candidate, classification_result))

        plan = Planner(config).plan(classified)
        outcomes = applier.apply(plan, run_id) if apply else applier.preview(plan)

        moved_count = sum(
            1
            for outcome in outcomes
            if outcome.status in {ApplyStatus.MOVED, ApplyStatus.PLANNED}
            and not outcome.move.is_review
        )
        review_count = sum(
            1
            for outcome in outcomes
            if outcome.status is ApplyStatus.REVIEWED
            or (outcome.status is ApplyStatus.PLANNED and outcome.move.is_review)
        )
        failed_count = sum(
            1 for outcome in outcomes if outcome.status is ApplyStatus.FAILED
        )
        skipped_count = len(scan_result.skipped) + len(plan.skipped)
        active_journal.finish_run(
            run_id,
            status=RunStatus.COMPLETED,
            moved_count=moved_count,
            review_count=review_count,
            skipped_count=skipped_count,
            failed_count=failed_count,
        )
        run_record = active_journal.get_run(run_id)
        if run_record is None:
            raise RuntimeError("journal lost the run record immediately after finishing")
        result = _run_result(
            run_record,
            scan_result,
            plan,
            outcomes,
            pending_repairs,
            active_journal.path,
        )
        try:
            report_path = write_report(config, result)
            active_journal.set_report_path(run_id, report_path)
            result = replace(result, report_path=report_path)
        except (OSError, JournalError):
            # Reporting must never turn a completed move into a failed run.
            pass
        notify_if_enabled(config, result)
        return result
    except BaseException:
        with contextlib.suppress(Exception):
            active_journal.finish_run(
                run_id,
                status=RunStatus.FAILED,
                failed_count=1,
            )
        raise
    finally:
        if owns_client:
            active_client.close()
        if owns_journal:
            active_journal.close()


def _run_result(
    record: RunRecord,
    scan_result: ScanResult,
    plan: Plan,
    outcomes: tuple[MoveOutcome, ...],
    pending_repairs: tuple[PendingRepair, ...],
    journal_path: Path,
) -> RunResult:
    return RunResult(
        run_id=record.id,
        mode=record.mode,
        started_at=record.started_at,
        finished_at=record.finished_at,
        scan=scan_result,
        plan=plan,
        outcomes=outcomes,
        pending_repairs=pending_repairs,
        journal_path=journal_path,
    )
