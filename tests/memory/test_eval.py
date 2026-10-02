"""Golden tickets and public offline evaluation contracts."""

import json
import hashlib
import re
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from harness.memory import build
from harness.storage import storage_path
from .test_build import configure, source
from .test_delivery import CLI


GOLDEN = Path(__file__).with_name("golden_tickets.json")


def test_golden_dataset_structure_and_no_secrets() -> None:
    """Real closed tickets carry goal/DoD queries and existing source labels."""
    text = GOLDEN.read_text(encoding="utf-8")
    rows = json.loads(text)
    assert 15 <= len(rows) <= 30
    assert len({row["id"] for row in rows}) == len(rows)
    root = Path(__file__).resolve().parents[2]
    for row in rows:
        assert type(row["id"]) is int and row["id"] > 0
        assert isinstance(row["title"], str) and row["title"].strip()
        assert isinstance(row["query"], str) and len(row["query"]) >= 20
        assert "Цель:" in row["query"] and "DoD:" in row["query"]
        assert row["state"] == "CLOSED"
        assert row["url"].endswith(f'/issues/{row["id"]}')
        assert isinstance(row["expected_sources"], list) and row["expected_sources"]
        for path in row["expected_sources"]:
            assert (root / path).is_file()
    assert not re.search(r"Bearer|ghp_|gho_|glpat-|-----BEGIN", text, re.I)


@pytest.mark.parametrize(
    ("paths", "k", "expected"),
    [
        (["a", "b", "c", "d", "e"], 1, 0.0),
        (["a", "b", "c", "d", "e"], 3, 1.0),
        (["a", "b", "c", "d", "e"], 5, 1.0),
        ([], 5, 0.0),
        (["e"], 1, 1.0),
    ],
)
def test_eval_metrics_recall_at_k(paths: list[str], k: int, expected: float) -> None:
    """Any labeled source in top-k satisfies a ticket's retrieval goal."""
    from harness.memory.eval import calculate_recall_at_k

    assert calculate_recall_at_k(paths, ["c", "e"], k) == expected


@pytest.mark.parametrize(
    ("paths", "k", "expected"),
    [
        (["a", "b", "c", "d", "e"], None, 0.8),
        (["a", "b", "c", "d", "e"], 3, 2 / 3),
        ([], None, 0.0),
        (["c", "c"], None, 0.0),
    ],
)
def test_eval_metrics_noise_ratio(
    paths: list[str], k: int | None, expected: float
) -> None:
    """Noise is the fraction of returned hits outside the labeled sources."""
    from harness.memory.eval import calculate_noise_ratio

    assert calculate_noise_ratio(paths, ["c"], k) == expected


def test_offline_deterministic_evaluation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Evaluation is repeatable, offline and leaves existing cache bytes untouched."""
    from harness.memory import evaluate_memory

    configure(tmp_path)
    source(tmp_path, "docs/adr/0001-test.md", "# Decision\ntransaction")
    source(tmp_path, "CONTEXT.md", "# Glossary\nwidget")
    build(tmp_path)
    dataset = [
        {
            "id": 1,
            "title": "Decision",
            "query": "transaction",
            "expected_sources": ["docs/adr/0001-test.md"],
        },
        {
            "id": 2,
            "title": "Unmatched",
            "query": "absent",
            "expected_sources": ["CONTEXT.md"],
        },
    ]
    index = storage_path(tmp_path, "cache", "memory", "index.sqlite3")
    before = (
        index.read_bytes(),
        index.stat().st_mtime_ns,
        sorted(p.name for p in index.parent.iterdir()),
    )

    def deny_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("evaluation must not open network sockets")

    monkeypatch.setattr(socket, "socket", deny_network)
    monkeypatch.setattr(socket, "create_connection", deny_network)
    first = evaluate_memory(tmp_path, dataset=dataset)
    second = evaluate_memory(tmp_path, dataset=dataset)
    assert first.to_dict() == second.to_dict()
    assert first.total_queries == 2
    assert first.mean_recall == {1: 0.5, 3: 0.5, 5: 0.5}
    assert first.mean_noise_ratio == 0.0
    assert not first.gate_passed
    assert evaluate_memory(tmp_path, dataset=dataset, min_recall_at_5=0.5).gate_passed
    assert before == (
        index.read_bytes(),
        index.stat().st_mtime_ns,
        sorted(p.name for p in index.parent.iterdir()),
    )


def test_unavailable_search_cannot_pass_a_permissive_gate(tmp_path: Path) -> None:
    """Missing, revoked or broken memory is not a valid quality measurement."""
    from harness.memory import evaluate_memory

    configure(tmp_path)
    dataset = [
        {
            "id": 1,
            "title": "Test",
            "query": "transaction",
            "expected_sources": ["CONTEXT.md"],
        }
    ]
    report = evaluate_memory(tmp_path, dataset=dataset, min_recall_at_5=0)
    assert not report.gate_passed
    assert report.query_results[0].search_status == "index_missing"
    assert not storage_path(tmp_path, "cache", "memory").exists()


def test_noise_ceiling_is_inclusive_and_independent_of_recall(tmp_path: Path) -> None:
    """A found source does not excuse four irrelevant hits in a five-hit window."""
    from harness.memory import evaluate_memory

    configure(tmp_path)
    for name in ("a", "b", "c", "d", "e", "f"):
        source(tmp_path, f"docs/adr/{name}.md", "# Same\ntransaction")
    build(tmp_path)
    dataset = [
        {
            "id": 1,
            "title": "Test",
            "query": "transaction",
            "expected_sources": ["docs/adr/a.md"],
        }
    ]
    report = evaluate_memory(tmp_path, dataset=dataset)
    assert report.mean_recall == {1: 1.0, 3: 1.0, 5: 1.0}
    assert report.mean_noise_ratio == 0.8
    assert len(report.query_results[0].retrieved) == 5
    assert not report.gate_passed
    assert evaluate_memory(tmp_path, dataset=dataset, max_noise_ratio=0.8).gate_passed


@pytest.mark.parametrize("query,code", [("transaction", 0), ("absent", 1)])
def test_cli_memory_eval(tmp_path: Path, query: str, code: int) -> None:
    """The JSON CLI gate exits nonzero on missed expected sources."""
    configure(tmp_path)
    source(tmp_path, "CONTEXT.md", "# Glossary\ntransaction")
    build(tmp_path)
    dataset = tmp_path / "dataset.json"
    dataset.write_text(
        json.dumps(
            [
                {
                    "id": 1,
                    "title": "Test",
                    "query": query,
                    "expected_sources": ["CONTEXT.md"],
                }
            ]
        ),
        encoding="utf-8",
    )
    process = subprocess.run(
        [
            sys.executable,
            str(CLI),
            "memory",
            "eval",
            str(tmp_path),
            "--dataset",
            str(dataset),
            "--min-recall",
            "0.6",
            "--max-noise",
            "0.7",
            "--json",
        ],
        capture_output=True,
        text=True,
    )
    assert process.returncode == code, process.stderr
    report = json.loads(process.stdout)
    assert report["gate_passed"] is (code == 0)
    assert report["mean_recall"] == {"1": 1.0 - code, "3": 1.0 - code, "5": 1.0 - code}
    assert report["mean_noise_ratio"] == 0.0


def test_cli_memory_eval_text_and_invalid_input(tmp_path: Path) -> None:
    """Human reports name every metric; invalid data never emits a successful gate."""
    configure(tmp_path)
    source(tmp_path, "CONTEXT.md", "# Glossary\ntransaction")
    build(tmp_path)
    dataset = tmp_path / "dataset.json"
    dataset.write_text(
        json.dumps(
            [
                {
                    "id": 1,
                    "title": "Test",
                    "query": "transaction",
                    "expected_sources": ["CONTEXT.md"],
                }
            ]
        ),
        encoding="utf-8",
    )
    argv = [
        sys.executable,
        str(CLI),
        "memory",
        "eval",
        str(tmp_path),
        "--dataset",
        str(dataset),
    ]
    process = subprocess.run(argv, capture_output=True, text=True)
    assert process.returncode == 0, process.stderr
    assert all(
        label in process.stdout
        for label in ("recall@1", "recall@3", "recall@5", "noise_ratio", "PASS")
    )
    dataset.write_text("[]", encoding="utf-8")
    process = subprocess.run(argv, capture_output=True, text=True)
    assert process.returncode != 0
    assert "non-empty list" in process.stderr


@pytest.mark.parametrize(
    "dataset",
    [
        [],
        [
            {
                "id": True,
                "title": "Test",
                "query": "term",
                "expected_sources": ["CONTEXT.md"],
            }
        ],
        [{"id": 1, "title": "Test", "query": " ", "expected_sources": ["CONTEXT.md"]}],
        [{"id": 1, "title": "Test", "query": "term", "expected_sources": []}],
        [
            {
                "id": 1,
                "title": "Test",
                "query": "term",
                "expected_sources": ["../secret.md"],
            }
        ],
    ],
)
def test_invalid_dataset_is_rejected(
    tmp_path: Path, dataset: list[dict[str, object]]
) -> None:
    """Invalid labels cannot turn missing evidence into a passing quality gate."""
    from harness.memory import evaluate_memory

    with pytest.raises(ValueError, match="dataset|expected_sources"):
        evaluate_memory(tmp_path, dataset=dataset)


@pytest.mark.parametrize(
    "options",
    [
        {"k_values": (1, 3)},
        {"k_values": (0, 5)},
        {"k_values": (5, 5)},
        {"min_recall_at_5": float("nan")},
        {"min_recall_at_5": -0.1},
        {"max_noise_ratio": 1.1},
    ],
)
def test_invalid_gate_options_are_rejected(
    tmp_path: Path, options: dict[str, object]
) -> None:
    """A recall@5 gate requires a valid window and finite probability thresholds."""
    from harness.memory import evaluate_memory

    with pytest.raises(ValueError, match="k_values|thresholds"):
        evaluate_memory(tmp_path, **options)  # type: ignore[arg-type]


def test_fts5_baseline_is_reproducible(tmp_path: Path) -> None:
    """The real ADR/glossary corpus reproduces the recorded FTS5 decision offline."""
    from harness.memory import evaluate_memory

    baseline = json.loads(
        GOLDEN.with_name("baseline_fts5.json").read_text(encoding="utf-8")
    )
    root = GOLDEN.resolve().parents[2]
    configure(tmp_path)
    for path, digest in baseline["source_hashes"].items():
        raw = (root / path).read_bytes()
        assert (
            hashlib.sha256(raw).hexdigest() == digest
        ), "baseline corpus changed; re-evaluate explicitly"
        source(tmp_path, path, raw.decode("utf-8"))
    assert hashlib.sha256(GOLDEN.read_bytes()).hexdigest() == baseline["dataset_hash"]
    assert build(tmp_path)["indexed"] == len(baseline["source_hashes"])
    report = evaluate_memory(tmp_path).to_dict()
    assert report == baseline["report"]
