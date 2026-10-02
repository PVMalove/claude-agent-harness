"""Deterministic offline retrieval metrics for labeled closed tickets."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import cast

from .search import search_candidates

DEFAULT_DATASET = (
    Path(__file__).resolve().parents[2] / "tests/memory/golden_tickets.json"
)


@dataclass(frozen=True)
class QueryEvalResult:
    """One query's ranked top-k paths and historical source labels."""

    ticket_id: int
    query: str
    retrieved: list[str]
    expected: list[str]
    recall_at_k: dict[int, float]
    noise_ratio: float
    search_status: str

    def to_dict(self) -> dict[str, object]:
        """Use string metric keys so JSON round-trips without changing the report."""
        return {
            "ticket_id": self.ticket_id,
            "query": self.query,
            "retrieved": self.retrieved,
            "expected": self.expected,
            "recall_at_k": {str(k): value for k, value in self.recall_at_k.items()},
            "noise_ratio": self.noise_ratio,
            "search_status": self.search_status,
        }


@dataclass(frozen=True)
class MemoryEvalReport:
    """Macro-averaged retrieval quality with an explicit pass/fail decision."""

    total_queries: int
    mean_recall: dict[int, float]
    mean_noise_ratio: float
    query_results: list[QueryEvalResult]
    gate_passed: bool

    def to_dict(self) -> dict[str, object]:
        """Serialize only deterministic evidence; omit clocks and cache locations."""
        return {
            "total_queries": self.total_queries,
            "mean_recall": {str(k): value for k, value in self.mean_recall.items()},
            "mean_noise_ratio": self.mean_noise_ratio,
            "query_results": [result.to_dict() for result in self.query_results],
            "gate_passed": self.gate_passed,
        }


def validate_dataset(value: object) -> list[dict[str, object]]:
    """Reject ambiguous IDs, empty labels and unsafe paths before evaluating."""
    if not isinstance(value, list) or not value:
        raise ValueError("memory eval dataset must be a non-empty list")
    ids: set[int] = set()
    for row in value:
        if not isinstance(row, dict):
            raise ValueError("memory eval dataset entries must be objects")
        ticket_id = row.get("id")
        paths = row.get("expected_sources")
        if type(ticket_id) is not int or ticket_id <= 0 or ticket_id in ids:
            raise ValueError("memory eval dataset needs unique positive integer IDs")
        ids.add(ticket_id)
        if any(
            not isinstance(row.get(field), str) or not row[field].strip()
            for field in ("title", "query")
        ):
            raise ValueError("memory eval dataset needs non-empty title and query")
        if not isinstance(paths, list) or not paths:
            raise ValueError("memory eval dataset needs non-empty expected_sources")
        for path in paths:
            if (
                not isinstance(path, str)
                or not path.strip()
                or "\\" in path
                or ":" in path
                or PurePosixPath(path).is_absolute()
                or any(part in {"", ".", ".."} for part in path.split("/"))
            ):
                raise ValueError(
                    "memory eval expected_sources must be relative POSIX paths"
                )
        if len(set(paths)) != len(paths):
            raise ValueError("memory eval expected_sources must be unique")
    return cast(list[dict[str, object]], value)


def load_golden_dataset(path: Path | None = None) -> list[dict[str, object]]:
    """Load checked-in labels, or a caller's offline dataset for another project."""
    selected = path if path is not None else DEFAULT_DATASET
    try:
        value = json.loads(selected.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(
            f"memory eval dataset not found: {selected}; pass --dataset <path>"
        ) from None
    return validate_dataset(value)


def calculate_recall_at_k(
    retrieved_paths: list[str], expected_sources: list[str], k: int
) -> float:
    """Ticket-level recall: at least one labeled source must appear in top-k."""
    if k <= 0:
        raise ValueError("k must be positive")
    return float(bool(set(retrieved_paths[:k]) & set(expected_sources)))


def calculate_noise_ratio(
    retrieved_paths: list[str], expected_sources: list[str], k: int | None = None
) -> float:
    """Count irrelevant returned hits; empty retrieval has zero noise."""
    if k is not None and k <= 0:
        raise ValueError("k must be positive")
    paths = retrieved_paths if k is None else retrieved_paths[:k]
    expected = set(expected_sources)
    return sum(path not in expected for path in paths) / len(paths) if paths else 0.0


def evaluate_memory(
    repo: Path,
    dataset: list[dict[str, object]] | None = None,
    dataset_path: Path | None = None,
    k_values: tuple[int, ...] = (1, 3, 5),
    min_recall_at_5: float = 0.6,
    max_noise_ratio: float = 0.7,
) -> MemoryEvalReport:
    """Read existing FTS5 candidates; score top-max(k) without refresh or network.

    Recall is ticket-level hit rate, as each label set contains alternative useful
    sources. Noise is macro-averaged over returned hits in the same top-max(k).
    A degraded search never passes, even with permissive numerical thresholds.
    """
    if dataset is not None and dataset_path is not None:
        raise ValueError("pass dataset or dataset_path, not both")
    if (
        5 not in k_values
        or any(type(k) is not int or k <= 0 for k in k_values)
        or len(set(k_values)) != len(k_values)
    ):
        raise ValueError("k_values must contain 5 and unique positive integers")
    if any(
        not math.isfinite(value) or not 0 <= value <= 1
        for value in (min_recall_at_5, max_noise_ratio)
    ):
        raise ValueError("memory eval thresholds must be finite values in [0, 1]")
    rows = (
        validate_dataset(dataset)
        if dataset is not None
        else load_golden_dataset(dataset_path)
    )
    results: list[QueryEvalResult] = []
    for row in rows:
        query = cast(str, row["query"])
        expected = cast(list[str], row["expected_sources"])
        response = search_candidates(repo, query)
        retrieved = [
            cast(str, pointer["path"])
            for pointer in cast(list[dict[str, object]], response["pointers"])
        ][: max(k_values)]
        results.append(
            QueryEvalResult(
                ticket_id=cast(int, row["id"]),
                query=query,
                retrieved=retrieved,
                expected=expected,
                recall_at_k={
                    k: calculate_recall_at_k(retrieved, expected, k) for k in k_values
                },
                noise_ratio=calculate_noise_ratio(retrieved, expected),
                search_status=cast(str, response["status"]),
            )
        )
    mean_recall = {
        k: math.fsum(result.recall_at_k[k] for result in results) / len(results)
        for k in k_values
    }
    noise = math.fsum(result.noise_ratio for result in results) / len(results)
    return MemoryEvalReport(
        total_queries=len(results),
        mean_recall=mean_recall,
        mean_noise_ratio=noise,
        query_results=results,
        gate_passed=(
            all(result.search_status == "ok" for result in results)
            and mean_recall[5] >= min_recall_at_5
            and noise <= max_noise_ratio
        ),
    )
