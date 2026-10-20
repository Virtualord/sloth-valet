"""Model comparison harness over deterministic local fixtures."""

from __future__ import annotations

import json
import math
import os
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from sloth_valet.classifier import Classifier
from sloth_valet.config import ValetConfig
from sloth_valet.extract import extract_file
from sloth_valet.fixtures import FixtureManifest
from sloth_valet.models import (
    ClassificationResult,
    ExtractionResult,
    FileCandidate,
)
from sloth_valet.ollama import OllamaClient

MemoryProbe = Callable[[], float | None]
Clock = Callable[[], float]


class ClassifierLike(Protocol):
    """The one classifier method the benchmark needs."""

    def classify(
        self,
        candidate: FileCandidate,
        extraction: ExtractionResult,
    ) -> ClassificationResult: ...


@dataclass(frozen=True, slots=True)
class BenchmarkFixture:
    file: str
    category: str
    description: str
    path: Path


@dataclass(frozen=True, slots=True)
class FileBenchmark:
    file: str
    expected_category: str
    category: str | None
    confidence: float | None
    valid_json: bool
    latency_ms: float
    failure: str | None

    @property
    def correct(self) -> bool:
        return self.category == self.expected_category


@dataclass(frozen=True, slots=True)
class ModelBenchmark:
    model: str
    total: int
    valid_json_count: int
    correct_count: int
    failure_count: int
    mean_confidence: float | None
    mean_latency_ms: float
    p95_latency_ms: float
    peak_memory_mb: float | None
    files: tuple[FileBenchmark, ...]

    @property
    def valid_json_rate(self) -> float:
        return self.valid_json_count / self.total if self.total else 0.0

    @property
    def category_accuracy(self) -> float:
        return self.correct_count / self.total if self.total else 0.0


@dataclass(frozen=True, slots=True)
class BenchmarkRun:
    started_at: datetime
    host: str
    labels_path: Path
    fixtures_dir: Path
    measure_memory: bool
    models: tuple[ModelBenchmark, ...]

    @property
    def fixture_count(self) -> int:
        return self.models[0].total if self.models else 0


def parse_models(value: str) -> tuple[str, ...]:
    """Parse a comma-separated model list without assuming any model name."""
    models = tuple(part.strip() for part in value.split(",") if part.strip())
    if not models:
        raise ValueError("at least one model name is required")
    seen: set[str] = set()
    for model in models:
        if any(ord(character) < 32 for character in model):
            raise ValueError("model names must not contain control characters")
        if model in seen:
            raise ValueError(f"duplicate model name {model!r}")
        seen.add(model)
    return models


def load_benchmark_fixtures(
    manifest: FixtureManifest,
    fixtures_dir: Path,
    *,
    allowed_categories: Sequence[str],
) -> tuple[BenchmarkFixture, ...]:
    """Resolve labeled fixtures and reject paths outside the fixture directory."""
    try:
        root = fixtures_dir.resolve(strict=True)
    except OSError as exc:
        raise OSError(f"cannot use fixture directory {fixtures_dir}: {exc}") from exc
    if not root.is_dir():
        raise OSError(f"fixture path is not a directory: {fixtures_dir}")
    allowed = set(allowed_categories)
    fixtures: list[BenchmarkFixture] = []
    for label in manifest.fixtures:
        if label.category not in allowed:
            raise ValueError(
                f"fixture category {label.category!r} is not in categories.allowed"
            )
        path = root / label.file
        if path.is_symlink():
            raise OSError(f"fixture is a symlink: {path}")
        try:
            path.resolve(strict=True).relative_to(root)
        except (OSError, ValueError) as exc:
            raise OSError(f"fixture escapes the fixture directory: {path}") from exc
        if not path.is_file():
            raise OSError(f"fixture is missing or not a regular file: {path}")
        fixtures.append(
            BenchmarkFixture(
                file=label.file,
                category=label.category,
                description=label.description,
                path=path,
            )
        )
    return tuple(sorted(fixtures, key=lambda fixture: fixture.file))


def benchmark_model(
    config: ValetConfig,
    model: str,
    fixtures: tuple[BenchmarkFixture, ...],
    *,
    classifier: ClassifierLike | None = None,
    measure_memory: bool = False,
    memory_probe: MemoryProbe | None = None,
    clock: Clock = time.perf_counter,
) -> ModelBenchmark:
    """Classify every fixture with one model; never plan, move, or delete anything."""
    model = model.strip()
    if not model:
        raise ValueError("model name cannot be empty")
    if not fixtures:
        raise ValueError("at least one fixture is required")
    if memory_probe is None:
        memory_probe = ollama_peak_memory_mb
    model_config = _config_for_model(config, model)
    client: OllamaClient | None = None
    active = classifier
    if active is None:
        client = OllamaClient(model_config)
        active = Classifier(model_config, client)
    try:
        records = _classify_fixtures(
            active,
            fixtures,
            text_chars=model_config.model.text_chars,
            clock=clock,
        )
        peak_memory = memory_probe() if measure_memory else None
    finally:
        if client is not None:
            client.close()
    return _summarize(model, records, peak_memory=peak_memory)


def run_benchmark(
    config: ValetConfig,
    models: tuple[str, ...],
    fixtures: tuple[BenchmarkFixture, ...],
    *,
    labels_path: Path,
    fixtures_dir: Path,
    measure_memory: bool = False,
    classifier: ClassifierLike | None = None,
    memory_probe: MemoryProbe | None = None,
    clock: Clock = time.perf_counter,
) -> BenchmarkRun:
    """Run every named model against the same fixture set."""
    if not models:
        raise ValueError("at least one model name is required")
    started_at = datetime.now(UTC)
    results = tuple(
        benchmark_model(
            config,
            model,
            fixtures,
            classifier=classifier,
            measure_memory=measure_memory,
            memory_probe=memory_probe,
            clock=clock,
        )
        for model in models
    )
    return BenchmarkRun(
        started_at=started_at,
        host=config.model.host,
        labels_path=labels_path,
        fixtures_dir=fixtures_dir,
        measure_memory=measure_memory,
        models=results,
    )


def write_benchmark_results(
    run: BenchmarkRun,
    results_dir: Path,
) -> tuple[Path, Path]:
    """Write a fresh JSON/Markdown pair; an existing result is never overwritten."""
    results_dir.mkdir(parents=True, exist_ok=True)
    stamp = run.started_at.astimezone(UTC).strftime("%Y%m%d-%H%M%S")
    base = f"bench-{stamp}-{uuid.uuid4().hex[:8]}"
    json_path = results_dir / f"{base}.json"
    markdown_path = results_dir / f"{base}.md"
    _write_exclusive(
        json_path,
        json.dumps(_run_to_dict(run), indent=2, sort_keys=True, allow_nan=False) + "\n",
    )
    _write_exclusive(markdown_path, benchmark_markdown(run))
    return json_path, markdown_path


def benchmark_markdown(run: BenchmarkRun) -> str:
    lines = [
        "# Sloth Valet benchmark",
        "",
        f"Run at {run.started_at.astimezone(UTC):%Y-%m-%d %H:%M:%S UTC} against "
        f"{_md(run.host)}.",
        f"Fixtures: {run.fixture_count}. Memory probe: "
        f"{'enabled' if run.measure_memory else 'disabled'}.",
        "",
        "| Model | Valid JSON | Category accuracy | Mean confidence | Mean latency | "
        "P95 latency | Peak Ollama RSS |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for result in run.models:
        lines.append(
            f"| {_md(result.model)} | {_percent(result.valid_json_rate)} | "
            f"{_percent(result.category_accuracy)} | {_confidence(result.mean_confidence)} | "
            f"{result.mean_latency_ms:.1f} ms | {result.p95_latency_ms:.1f} ms | "
            f"{_memory(result.peak_memory_mb)} |"
        )
    lines.extend(
        [
            "",
            "The first request for each model includes Ollama model loading. "
            "Peak RSS is a best-effort sum of Linux `VmHWM` for processes whose "
            "name starts with `ollama`; restart Ollama before a run for a cleaner "
            "per-model reading.",
            "",
            "## Failures",
            "",
        ]
    )
    failures = [
        (result.model, record)
        for result in run.models
        for record in result.files
        if record.failure is not None
    ]
    if not failures:
        lines.append("No recorded failures.")
    else:
        for model, record in failures:
            lines.append(
                f"- `{_md(model)}` / `{_md(record.file)}`: `{_md(record.failure or '')}`"
            )
    lines.append("")
    return "\n".join(lines)


def ollama_peak_memory_mb() -> float | None:
    """Best-effort peak RSS of Ollama processes from Linux /proc VmHWM."""
    total_kib = 0.0
    found = False
    try:
        entries = tuple(Path("/proc").iterdir())
    except OSError:
        return None
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            status = (entry / "status").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        name = ""
        peak_kib: float | None = None
        for line in status.splitlines():
            if line.startswith("Name:"):
                name = line.partition(":")[2].strip()
            elif line.startswith("VmHWM:"):
                fields = line.partition(":")[2].split()
                if fields:
                    try:
                        peak_kib = float(fields[0])
                    except ValueError:
                        peak_kib = None
        if peak_kib is not None and name.casefold().startswith("ollama"):
            total_kib += peak_kib
            found = True
    return total_kib / 1024 if found else None


def _classify_fixtures(
    classifier: ClassifierLike,
    fixtures: tuple[BenchmarkFixture, ...],
    *,
    text_chars: int,
    clock: Clock,
) -> tuple[FileBenchmark, ...]:
    records: list[FileBenchmark] = []
    root = fixtures[0].path.parent
    for fixture in fixtures:
        file_stat = fixture.path.stat()
        candidate = FileCandidate(
            source=fixture.path,
            watch_root=root,
            destination_root=root,
            size=file_stat.st_size,
            mtime_ns=file_stat.st_mtime_ns,
            device=file_stat.st_dev,
            inode=file_stat.st_ino,
        )
        extraction = extract_file(fixture.path, max_chars=text_chars)
        started = clock()
        result = classifier.classify(candidate, extraction)
        latency_ms = max(0.0, (clock() - started) * 1000.0)
        classification = result.classification
        records.append(
            FileBenchmark(
                file=fixture.file,
                expected_category=fixture.category,
                category=classification.category if classification is not None else None,
                confidence=(
                    classification.confidence if classification is not None else None
                ),
                valid_json=result.parsed_json,
                latency_ms=latency_ms,
                failure=result.failure.value if result.failure is not None else None,
            )
        )
    return tuple(records)


def _summarize(
    model: str,
    records: tuple[FileBenchmark, ...],
    *,
    peak_memory: float | None,
) -> ModelBenchmark:
    latencies = [record.latency_ms for record in records]
    confidences = [
        record.confidence for record in records if record.confidence is not None
    ]
    total = len(records)
    return ModelBenchmark(
        model=model,
        total=total,
        valid_json_count=sum(1 for record in records if record.valid_json),
        correct_count=sum(1 for record in records if record.correct),
        failure_count=sum(1 for record in records if record.failure is not None),
        mean_confidence=(
            sum(confidences) / len(confidences) if confidences else None
        ),
        mean_latency_ms=sum(latencies) / total if total else 0.0,
        p95_latency_ms=_p95(latencies),
        peak_memory_mb=peak_memory,
        files=records,
    )


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(0.95 * len(ordered)) - 1))
    return ordered[index]


def _config_for_model(config: ValetConfig, model: str) -> ValetConfig:
    if any(ord(character) < 32 for character in model):
        raise ValueError("model names must not contain control characters")
    model_config = config.model.model_copy(update={"name": model})
    return config.model_copy(update={"model": model_config, "fast_path": {}})


def _run_to_dict(run: BenchmarkRun) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "started_at": run.started_at.astimezone(UTC).isoformat(),
        "host": run.host,
        "labels_path": str(run.labels_path),
        "fixtures_dir": str(run.fixtures_dir),
        "fixture_count": run.fixture_count,
        "measure_memory": run.measure_memory,
        "models": [
            {
                "model": result.model,
                "total": result.total,
                "valid_json": result.valid_json_count,
                "valid_json_rate": result.valid_json_rate,
                "correct": result.correct_count,
                "category_accuracy": result.category_accuracy,
                "failures": result.failure_count,
                "mean_confidence": result.mean_confidence,
                "mean_latency_ms": result.mean_latency_ms,
                "p95_latency_ms": result.p95_latency_ms,
                "peak_memory_mb": result.peak_memory_mb,
                "files": [
                    {
                        "file": record.file,
                        "expected_category": record.expected_category,
                        "category": record.category,
                        "correct": record.correct,
                        "confidence": record.confidence,
                        "valid_json": record.valid_json,
                        "latency_ms": record.latency_ms,
                        "failure": record.failure,
                    }
                    for record in result.files
                ],
            }
            for result in run.models
        ],
    }


def _write_exclusive(path: Path, content: str) -> None:
    with path.open("x", encoding="utf-8") as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())


def _percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def _confidence(value: float | None) -> str:
    return "-" if value is None else f"{value:.3f}"


def _memory(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f} MiB"


def _md(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("`", "'")
        .replace("|", "\\|")
        .replace("\r", " ")
        .replace("\n", " ")
    )
