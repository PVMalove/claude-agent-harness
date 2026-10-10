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
from harness.errors import HarnessError
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
        assert set(row["source_evidence"]) == set(row["expected_sources"])
        for path in row["expected_sources"]:
            assert (root / path).is_file()
            evidence = row["source_evidence"][path]
            assert len(evidence["excerpt"]) >= 20
            assert evidence["excerpt"] in (root / path).read_text(encoding="utf-8")
            assert evidence["relevance"].strip()
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
    assert process.returncode == 2
    assert "non-empty list" in process.stderr
    assert "ERROR:" in process.stderr and "REMEDY:" in process.stderr


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

    with pytest.raises(HarnessError, match="dataset|expected_sources") as error:
        evaluate_memory(tmp_path, dataset=dataset)
    assert error.value.remedy


@pytest.mark.parametrize(
    "k_values",
    [(1, 3), (0, 5), (5, 5)],
)
def test_invalid_recall_windows_are_rejected(
    tmp_path: Path, k_values: tuple[int, ...]
) -> None:
    """A recall@5 gate requires unique positive windows including five."""
    from harness.memory import evaluate_memory

    with pytest.raises(HarnessError, match="k_values") as error:
        evaluate_memory(tmp_path, k_values=k_values)
    assert error.value.remedy


@pytest.mark.parametrize(
    "min_recall,max_noise",
    [(float("nan"), 0.7), (-0.1, 0.7), (0.6, 1.1)],
)
def test_invalid_gate_thresholds_are_rejected(
    tmp_path: Path, min_recall: float, max_noise: float
) -> None:
    """Gate thresholds must be finite probabilities with a corrective diagnostic."""
    from harness.memory import evaluate_memory

    with pytest.raises(HarnessError, match="thresholds") as error:
        evaluate_memory(tmp_path, min_recall_at_5=min_recall, max_noise_ratio=max_noise)
    assert error.value.remedy


@pytest.mark.parametrize("content", [None, "{", "[]", "invalid UTF-8"])
def test_dataset_read_errors_use_the_cli_error_contract(
    tmp_path: Path, content: str | None
) -> None:
    """Missing, malformed and undecodable input all expose a concrete remedy."""
    path = tmp_path / "dataset.json"
    if content == "invalid UTF-8":
        path.write_bytes(b"\xff")
    elif content is not None:
        path.write_text(content, encoding="utf-8")
    process = subprocess.run(
        [
            sys.executable,
            str(CLI),
            "memory",
            "eval",
            str(tmp_path),
            "--dataset",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    assert process.returncode == 2
    assert "ERROR:" in process.stderr and "REMEDY:" in process.stderr
    assert "Traceback" not in process.stderr


def _baseline_corpus(tmp_path: Path, filename: str) -> Path:
    """Build current or historical source bytes, with every input pinned by its stored hash."""
    baseline = json.loads(GOLDEN.with_name(filename).read_text(encoding="utf-8"))
    archived = (
        json.loads(
            (GOLDEN.parent / "fixtures/initial-corpus.json").read_text(encoding="utf-8")
        )
        if filename == "baseline_fts5_initial.json"
        else {}
    )
    root = GOLDEN.resolve().parents[2]
    configure(tmp_path)
    for path, digest in baseline["source_hashes"].items():
        raw = (
            archived[path].encode("utf-8")
            if path in archived
            else (root / path).read_bytes()
        )
        assert (
            hashlib.sha256(raw).hexdigest() == digest
        ), "baseline corpus changed; re-evaluate explicitly"
        source(tmp_path, path, raw.decode("utf-8"))
    assert build(tmp_path)["indexed"] == len(baseline["source_hashes"])
    return tmp_path


@pytest.fixture
def golden_corpus(tmp_path: Path) -> Path:
    """Build the actively measured corpus without changing this checkout's policy."""
    baseline = json.loads(
        GOLDEN.with_name("baseline_fts5.json").read_text(encoding="utf-8")
    )
    assert hashlib.sha256(GOLDEN.read_bytes()).hexdigest() == baseline["dataset_hash"]
    return _baseline_corpus(tmp_path, "baseline_fts5.json")


@pytest.fixture
def initial_corpus(tmp_path: Path) -> Path:
    """Preserve the original source corpus when authoritative project documents change."""
    return _baseline_corpus(tmp_path, "baseline_fts5_initial.json")


def test_fts5_baseline_is_reproducible(golden_corpus: Path) -> None:
    """The real corpus passes the default gate through both the public API and CLI."""
    from harness.memory import evaluate_memory

    baseline = json.loads(
        GOLDEN.with_name("baseline_fts5.json").read_text(encoding="utf-8")
    )
    report = evaluate_memory(golden_corpus).to_dict()
    assert report == baseline["report"]
    assert report["gate_passed"] is True
    process = subprocess.run(
        [sys.executable, str(CLI), "memory", "eval", str(golden_corpus), "--json"],
        capture_output=True,
        text=True,
    )
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout) == report


def test_initial_single_label_measurement_is_preserved(initial_corpus: Path) -> None:
    """Relabeling must not be reported as an improvement in the FTS5 engine."""
    from harness.memory import evaluate_memory
    from harness.memory.eval import load_golden_dataset

    initial = json.loads(
        GOLDEN.with_name("baseline_fts5_initial.json").read_text(encoding="utf-8")
    )
    current = json.loads(
        GOLDEN.with_name("baseline_fts5.json").read_text(encoding="utf-8")
    )
    assert initial["engine"] == current["engine"]
    old_labels = {
        result["ticket_id"]: result["expected"]
        for result in initial["report"]["query_results"]
    }
    dataset = [
        {
            key: row[key]
            for key in ("id", "title", "query", "expected_sources", "url", "state")
        }
        for row in load_golden_dataset()
    ]
    for row in dataset:
        row["expected_sources"] = old_labels[row["id"]]
    original_bytes = (json.dumps(dataset, ensure_ascii=False, indent=2) + "\n").encode(
        "utf-8"
    )
    assert hashlib.sha256(original_bytes).hexdigest() == initial["dataset_hash"]
    report = evaluate_memory(initial_corpus, dataset=dataset)
    assert report.to_dict() == initial["report"]
    assert report.gate_passed is False
