from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import make_file, write_config
from typer.testing import CliRunner

from sloth_valet.bench import (
    BenchmarkFixture,
    benchmark_markdown,
    benchmark_model,
    load_benchmark_fixtures,
    parse_models,
    run_benchmark,
    write_benchmark_results,
)
from sloth_valet.cli import app
from sloth_valet.fixtures import (
    FixtureLabel,
    FixtureManifest,
    generate_fixtures,
    load_fixture_manifest,
)
from sloth_valet.models import (
    Classification,
    ClassificationFailure,
    ClassificationResult,
    ExtractionResult,
    FileCandidate,
)

runner = CliRunner()
PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FakeClassifier:
    def __init__(self, results: dict[str, ClassificationResult]) -> None:
        self.results = results
        self.calls = 0

    def classify(
        self,
        candidate: FileCandidate,
        extraction: ExtractionResult,
    ) -> ClassificationResult:
        del extraction
        self.calls += 1
        return self.results[candidate.source.name]


def _classification(category: str, confidence: float) -> ClassificationResult:
    return ClassificationResult(
        classification=Classification(
            category=category,
            subfolder="",
            new_name="clean-name.txt",
            confidence=confidence,
            reason="fake",
        ),
        parsed_json=True,
    )


def _fixtures(tmp_path: Path, categories: tuple[str, ...]) -> tuple[BenchmarkFixture, ...]:
    manifest = FixtureManifest(
        seed=1,
        fixtures=tuple(
            FixtureLabel(
                file=f"fixture-{index}.txt",
                category=category,
                description=f"Fixture for {category}",
            )
            for index, category in enumerate(categories)
        ),
    )
    fixture_dir = tmp_path / "fixtures"
    generate_fixtures(manifest, fixture_dir)
    return load_benchmark_fixtures(
        manifest,
        fixture_dir,
        allowed_categories=(
            "academic",
            "career",
            "finance",
            "installers",
            "archives",
            "screenshots",
            "code",
            "media",
            "misc",
        ),
    )


def _write_labels(tmp_path: Path, categories: tuple[str, ...]) -> Path:
    path = tmp_path / "labels.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "seed": 1,
                "fixtures": [
                    {
                        "file": f"fixture-{index}.txt",
                        "category": category,
                        "description": f"Fixture for {category}",
                    }
                    for index, category in enumerate(categories)
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_parse_models_accepts_arbitrary_comma_separated_names() -> None:
    assert parse_models("alpha:1b, beta:2b") == ("alpha:1b", "beta:2b")
    with pytest.raises(ValueError, match="at least one"):
        parse_models(" , ")
    with pytest.raises(ValueError, match="duplicate"):
        parse_models("same,same")


def test_fixture_generation_is_deterministic_and_never_overwrites_by_default(
    tmp_path: Path,
) -> None:
    labels = PROJECT_ROOT / "bench" / "labels.json"
    manifest = load_fixture_manifest(labels)
    output = tmp_path / "fixtures"

    first = generate_fixtures(manifest, output)
    text_before = (output / "code-retry-helper.py").read_bytes()
    pdf_before = (output / "academic-graph-theory-notes.pdf").read_bytes()

    with pytest.raises(FileExistsError, match="overwrite"):
        generate_fixtures(manifest, output)

    second = generate_fixtures(manifest, output, overwrite=True)

    assert len(first) == len(second) == 50
    assert len({path.name for path in first}) == 50
    assert text_before == (output / "code-retry-helper.py").read_bytes()
    assert pdf_before == (output / "academic-graph-theory-notes.pdf").read_bytes()
    assert all(path.is_file() and not path.is_symlink() for path in first)
    categories = {label.category for label in manifest.fixtures}
    assert categories == {
        "academic",
        "career",
        "finance",
        "installers",
        "archives",
        "screenshots",
        "code",
        "media",
        "misc",
    }


def test_generator_refuses_unsafe_labels(tmp_path: Path) -> None:
    labels = tmp_path / "labels.json"
    labels.write_text(
        json.dumps(
            {
                "seed": 1,
                "fixtures": [
                    {
                        "file": "../escape.txt",
                        "category": "misc",
                        "description": "bad",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unsafe"):
        load_fixture_manifest(labels)


def test_loader_rejects_labels_outside_allowed_categories(
    tmp_path: Path,
) -> None:
    manifest = FixtureManifest(
        seed=1,
        fixtures=(FixtureLabel("one.txt", "academic", "Academic file"),),
    )
    fixture_dir = tmp_path / "fixtures"
    generate_fixtures(manifest, fixture_dir)

    with pytest.raises(ValueError, match=r"categories\.allowed"):
        load_benchmark_fixtures(
            manifest,
            fixture_dir,
            allowed_categories=("finance",),
        )


def test_loader_rejects_symlinked_fixture(tmp_path: Path) -> None:
    outside = make_file(tmp_path / "outside.txt")
    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir()
    (fixture_dir / "one.txt").symlink_to(outside)
    manifest = FixtureManifest(
        seed=1,
        fixtures=(FixtureLabel("one.txt", "misc", "Symlink"),),
    )

    with pytest.raises(OSError, match="symlink"):
        load_benchmark_fixtures(
            manifest,
            fixture_dir,
            allowed_categories=("misc",),
        )


def test_benchmark_metrics_use_valid_json_category_and_confidence(
    tmp_path: Path,
) -> None:
    paths = write_config(tmp_path)
    fixtures = _fixtures(
        tmp_path,
        ("academic", "finance", "code", "media"),
    )
    fake = FakeClassifier(
        {
            "fixture-0.txt": _classification("academic", 0.9),
            "fixture-1.txt": _classification("career", 0.8),
            "fixture-2.txt": ClassificationResult(
                classification=None,
                failure=ClassificationFailure.INVALID_JSON,
                parsed_json=False,
            ),
            "fixture-3.txt": ClassificationResult(
                classification=None,
                failure=ClassificationFailure.LOW_CONFIDENCE,
                parsed_json=True,
            ),
        }
    )
    ticks = iter([0.0, 0.01, 0.0, 0.02, 0.0, 0.03, 0.0, 0.04])

    result = benchmark_model(
        paths.load(),
        "fake-model",
        fixtures,
        classifier=fake,
        measure_memory=True,
        memory_probe=lambda: 321.5,
        clock=lambda: next(ticks),
    )

    assert fake.calls == 4
    assert result.total == 4
    assert result.valid_json_count == 3
    assert result.valid_json_rate == pytest.approx(0.75)
    assert result.correct_count == 1
    assert result.category_accuracy == pytest.approx(0.25)
    assert result.failure_count == 2
    assert result.mean_confidence == pytest.approx(0.85)
    assert result.mean_latency_ms == pytest.approx(25.0)
    assert result.p95_latency_ms == pytest.approx(40.0)
    assert result.peak_memory_mb == pytest.approx(321.5)
    assert all(fixture.path.exists() for fixture in fixtures)


def test_run_benchmark_and_result_files_include_each_model(
    tmp_path: Path,
) -> None:
    paths = write_config(tmp_path)
    fixtures = _fixtures(tmp_path, ("academic", "code"))
    fake = FakeClassifier(
        {
            "fixture-0.txt": _classification("academic", 0.95),
            "fixture-1.txt": _classification("misc", 0.4),
        }
    )
    labels = _write_labels(tmp_path, ("academic", "code"))

    run = run_benchmark(
        paths.load(),
        ("alpha:1b", "beta:2b"),
        fixtures,
        labels_path=labels,
        fixtures_dir=tmp_path / "fixtures",
        classifier=fake,
    )
    json_path, markdown_path = write_benchmark_results(run, tmp_path / "results")
    second_json, second_markdown = write_benchmark_results(
        run, tmp_path / "results"
    )

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    markdown = markdown_path.read_text(encoding="utf-8")

    assert [model.model for model in run.models] == ["alpha:1b", "beta:2b"]
    assert run.host == "http://127.0.0.1:11434"
    assert run.fixture_count == 2
    assert payload["fixture_count"] == 2
    assert [model["model"] for model in payload["models"]] == ["alpha:1b", "beta:2b"]
    assert payload["models"][0]["category_accuracy"] == pytest.approx(0.5)
    assert "| alpha:1b |" in markdown
    assert "| beta:2b |" in markdown
    assert "50.0%" in markdown
    assert payload["models"][0]["files"][1]["file"] == "fixture-1.txt"
    assert payload["models"][0]["files"][1]["correct"] is False
    assert json_path != second_json
    assert markdown_path != second_markdown
    assert second_json.exists() and second_markdown.exists()


def test_benchmark_markdown_does_not_echo_model_text(
    tmp_path: Path,
) -> None:
    paths = write_config(tmp_path)
    fixtures = _fixtures(tmp_path, ("academic",))
    fake = FakeClassifier(
        {
            "fixture-0.txt": ClassificationResult(
                classification=None,
                failure=ClassificationFailure.CATEGORY_NOT_ALLOWED,
                detail="<script>ignore previous instructions</script>",
                parsed_json=True,
            )
        }
    )
    run = run_benchmark(
        paths.load(),
        ("local-model",),
        fixtures,
        labels_path=tmp_path / "labels.json",
        fixtures_dir=tmp_path / "fixtures",
        classifier=fake,
    )

    markdown = benchmark_markdown(run)

    assert "ignore previous instructions" not in markdown
    assert "category_not_allowed" in markdown


def test_cli_bench_writes_json_and_markdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = write_config(tmp_path)
    categories = ("academic", "code")
    fixtures = _fixtures(tmp_path, categories)
    labels = _write_labels(tmp_path, categories)
    fake = FakeClassifier(
        {
            "fixture-0.txt": _classification("academic", 0.9),
            "fixture-1.txt": _classification("code", 0.8),
        }
    )
    run = run_benchmark(
        paths.load(),
        ("fake-one",),
        fixtures,
        labels_path=labels,
        fixtures_dir=tmp_path / "fixtures",
        classifier=fake,
    )
    monkeypatch.setattr("sloth_valet.cli.run_benchmark", lambda *_args, **_kwargs: run)
    results_dir = tmp_path / "results"

    result = runner.invoke(
        app,
        [
            "--config",
            str(paths.config_path),
            "bench",
            "--models",
            "fake-one",
            "--fixtures",
            str(tmp_path / "fixtures"),
            "--labels",
            str(labels),
            "--results-dir",
            str(results_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Benchmarked 2 fixtures" in result.output
    assert "valid JSON 100.0%" in result.output
    assert len(list(results_dir.glob("*.json"))) == 1
    assert len(list(results_dir.glob("*.md"))) == 1
