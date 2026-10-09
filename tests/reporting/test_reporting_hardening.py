"""Hardening tests for harness/reporting: rate card and project config read errors, subprocess
timeouts, damaged session logs and ledger records, the interpreter guard, and the compact number
format shared by the terminal and HTML renderers."""

from __future__ import annotations

import ast
import json
import re
import subprocess
from pathlib import Path
from typing import Callable

import pytest

from harness.health.checks.environment import MIN_PYTHON as HARNESS_MIN_PYTHON
from harness.reporting import cost, delivery_stats, render_html
from harness.reporting.common import MISSING, JsonObject, StatsError, compact_count
from harness.reporting.terminal import render_terminal

NB = " "


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "0"),
        (999, "999"),
        (-999, "-999"),
        (1_000, "1 тыс"),
        (2_500_000, "2,5 млн"),
        (-2_500_000, "-2,5 млн"),
        (-3_000_000_000, "-3 млрд"),
    ],
)
def test_compact_count_scales_negative_values_like_positive(
    value: int, expected: str
) -> None:
    assert compact_count(value) == expected


def test_html_compact_keeps_the_non_breaking_space() -> None:
    assert render_html._compact(-2_500_000) == "-2,5" + NB + "млн"


def _report_with_comparison(delta_total: int) -> JsonObject:
    telemetry = {"status": "ok", "attribution": "exact", "total_tokens": 5_000_000}
    return {
        "epic": {"number": 7, "title": "Поставка"},
        "tickets_closed": 1,
        "tickets_total": 1,
        "volume": {
            "totals": {"insertions": 1, "deletions": 0, "files": 1, "commits": 1}
        },
        "adr_added": 0,
        "claude": {"status": MISSING, "reason": "нет логов"},
        "codex": {"status": MISSING, "reason": "нет сессий"},
        "comparison": {
            "baseline": {"epic": {"number": 6}, "providers": {"claude": telemetry}},
            "current": {"epic": {"number": 7}, "providers": {"claude": telemetry}},
            "delta": {"providers": {"claude": {"total_tokens": delta_total}}},
        },
        "cache": MISSING,
        "cost": {"status": MISSING, "reason": "нет ставок"},
        "orchestration": {"status": MISSING, "reason": "нет ledger"},
    }


def test_terminal_comparison_scales_a_negative_delta() -> None:
    text = render_terminal(_report_with_comparison(-1_500_000))

    assert "разница -1,5 млн" in text


def test_session_stats_panel_uses_only_classes_defined_in_the_style() -> None:
    claude = {
        "status": "ok",
        "session_stats": [
            {
                "branch": "feature/issue-1-x",
                "kind": "main",
                "turns": 2,
                "max_input": 10,
                "total_input": 20,
            }
        ],
    }

    markup = render_html._session_stats_panel(claude)
    classes = {
        name
        for group in re.findall(r"class=['\"]([^'\"]+)['\"]", markup)
        for name in group.split()
    }

    assert classes
    assert all(f".{name}" in render_html.STYLE for name in classes), classes


def _rate_card(tmp_path: Path, card: JsonObject) -> Path:
    path = tmp_path / "rates.json"
    path.write_text(json.dumps({"models": {"model-a": card}}), encoding="utf-8")
    return path


@pytest.mark.parametrize("field", cost.CACHE_MULTIPLIER_FIELDS)
@pytest.mark.parametrize("multiplier", [None, "abc", [1.25]])
def test_load_rates_rejects_a_non_numeric_cache_multiplier(
    tmp_path: Path, field: str, multiplier: object
) -> None:
    path = _rate_card(tmp_path, {"input": 3.0, "output": 15.0, field: multiplier})

    with pytest.raises(StatsError) as caught:
        cost.load_rates(path)

    assert field in caught.value.message
    assert field in caught.value.remedy


def test_load_rates_keeps_a_numeric_string_multiplier(tmp_path: Path) -> None:
    path = _rate_card(
        tmp_path, {"input": 3.0, "output": 15.0, "cache_read_multiplier": "0.2"}
    )

    assert cost.load_rates(path)["status"] == "ok"


def _unreadable(path: Path) -> Callable[[Path, str | None, str | None], str]:
    """Path.read_text replacement that fails with PermissionError for one path only."""
    original = Path.read_text

    def read_text(
        self: Path, encoding: str | None = None, errors: str | None = None
    ) -> str:
        if self == path:
            raise PermissionError(13, "Permission denied", str(self))
        return original(self, encoding=encoding, errors=errors)

    return read_text


def test_load_rates_reports_an_unreadable_card_as_stats_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _rate_card(tmp_path, {"input": 3.0, "output": 15.0})
    monkeypatch.setattr(Path, "read_text", _unreadable(path))

    with pytest.raises(StatsError) as caught:
        cost.load_rates(path)

    assert "not readable" in caught.value.message
    assert "file-system error" in caught.value.remedy


def test_project_config_reports_an_unreadable_file_as_stats_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / ".harness" / "project.json"
    path.parent.mkdir()
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(Path, "read_text", _unreadable(path))

    with pytest.raises(StatsError) as caught:
        delivery_stats._project_config(tmp_path)

    assert "not readable" in caught.value.message
    assert "file-system error" in caught.value.remedy


def test_run_bounds_every_command_and_reports_a_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[object] = []

    def hanging_run(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        seen.append(kwargs.get("timeout"))
        raise subprocess.TimeoutExpired(command, delivery_stats.COMMAND_TIMEOUT_SECONDS)

    monkeypatch.setattr(subprocess, "run", hanging_run)

    code, out, err = delivery_stats._run(["git", "log"])

    assert seen == [delivery_stats.COMMAND_TIMEOUT_SECONDS]
    assert (code, out) == (delivery_stats.TIMEOUT_EXIT_CODE, "")
    assert f"within {delivery_stats.COMMAND_TIMEOUT_SECONDS} s" in err
    with pytest.raises(StatsError) as caught:
        delivery_stats._git(Path("."), "rev-list", "HEAD")
    assert "did not finish" in caught.value.message
    assert caught.value.remedy


@pytest.mark.parametrize("ordinal", [[1], {"n": 1}])
def test_codex_usage_survives_an_unhashable_ordinal(
    tmp_path: Path, ordinal: object
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    record = json.dumps(
        {
            "timestamp": "2026-01-01T10:00:00.000Z",
            "ordinal": ordinal,
            "cwd": str(repo),
            "payload": {
                "type": "token_count",
                "model": "codex-a",
                "info": {
                    "last_token_usage": {
                        "input_tokens": 10,
                        "cached_input_tokens": 2,
                        "cache_write_input_tokens": 0,
                        "output_tokens": 3,
                    }
                },
            },
        }
    )
    (sessions / "rollout-1.jsonl").write_text(record + "\n", encoding="utf-8")
    window = (
        delivery_stats._moment("2026-01-01T00:00:00Z"),
        delivery_stats._moment("2026-01-02T00:00:00Z"),
    )

    report = delivery_stats.codex_usage(sessions, repo, window)

    assert report["status"] == "ok"
    assert report["turns"] == 1


@pytest.mark.parametrize("damaged", [None, {"not": "a list"}, "text"])
def test_orchestration_metrics_reads_a_damaged_list_field_as_empty(
    tmp_path: Path, damaged: object
) -> None:
    state = tmp_path / "repo" / ".harness" / "orchestration" / "state"
    (state / "batches").mkdir(parents=True)
    (state / "batches" / "batch-950-x.json").write_text(
        json.dumps(
            {
                "batch_id": "batch-950-x",
                "branch": "feature/issue-950-x",
                "dispatches": damaged,
                "coordinator_decisions": damaged,
            }
        ),
        encoding="utf-8",
    )

    report = delivery_stats.orchestration_metrics(tmp_path / "repo", {950}, state)

    assert report["status"] == "ok"
    assert report["tickets"]["950"]["worker_sessions"] == []


def test_orchestration_metrics_ignores_non_string_write_paths(tmp_path: Path) -> None:
    state = tmp_path / "repo" / ".harness" / "orchestration" / "state"
    (state / "batches").mkdir(parents=True)
    (state / "dispatches").mkdir()
    (state / "batches" / "batch-950-x.json").write_text(
        json.dumps(
            {
                "batch_id": "batch-950-x",
                "branch": "feature/issue-950-x",
                "dispatches": [
                    {"dispatch_id": "dispatch-dev", "role": "developer"},
                    {"dispatch_id": "dispatch-review", "role": "code-review"},
                ],
            }
        ),
        encoding="utf-8",
    )
    (state / "dispatches" / "dispatch-dev.json").write_text(
        json.dumps({"write_paths": [7, "services/**"]}), encoding="utf-8"
    )
    (state / "dispatches" / "dispatch-review.json").write_text(
        json.dumps({"review_scope": ["services/a.py", "docs/b.md"]}),
        encoding="utf-8",
    )

    report = delivery_stats.orchestration_metrics(tmp_path / "repo", {950}, state)

    scope = report["tickets"]["950"]["review_scope"]
    assert scope[0]["out_of_scope_files"] == ["docs/b.md"]


def test_delivery_stats_guard_matches_the_harness_floor_and_runs_first() -> None:
    assert delivery_stats.MIN_PYTHON == HARNESS_MIN_PYTHON
    source = Path(delivery_stats.__file__).read_text(encoding="utf-8")
    body = ast.parse(source).body
    guard = next(
        index
        for index, node in enumerate(body)
        if isinstance(node, ast.If) and "version_info" in ast.unparse(node.test)
    )
    imported_before = {
        alias.name
        for node in body[:guard]
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    # Only names that every Python 3 interpreter can import may precede the guard.
    assert imported_before <= {"annotations", "sys"}
