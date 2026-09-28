"""State and usage statistics for the Harness and Orchestration sections, without textual."""

from __future__ import annotations

import json
from pathlib import Path

from harness.console import reports, stats
from tests.console._console_ledger_fixture import build_reports_fixture


def test_pipeline_stats_count_runs_states_roles_and_outcomes(tmp_path: Path) -> None:
    build_reports_fixture(tmp_path)
    view = reports.load_ledger_view(tmp_path)

    result = stats.pipeline_stats(view)

    assert (result.batches, result.tickets) == (2, 2)
    assert result.by_state == {"blocked": 1, "completed": 1}
    assert result.dispatches_by_role == {"developer": 2, "code-review": 1, "qa": 1}
    assert result.reports_by_outcome == {"completed": 3, "blocked": 1}
    text = stats.pipeline_stats_text(result)
    assert "Запусков (batch): 2, тикетов: 2" in text
    assert "заблокирован 1" in text and "завершён 1" in text
    assert "запусков 2" in stats.usage_text(view)


def test_state_filter_lists_every_lifecycle_state_and_filters_batches(
    tmp_path: Path,
) -> None:
    build_reports_fixture(tmp_path)
    view = reports.load_ledger_view(tmp_path)

    items = dict(stats.state_filter_items(view))

    assert list(items)[0] == stats.ALL_STATES
    assert set(stats.BATCH_STATES) <= set(items)
    assert items["blocked"] == "заблокирован (1)"
    assert items["planned"] == "запланирован (0)"
    assert [b.batch_id for b in stats.batches_in_state(view, "blocked")] == [
        "batch-stuck"
    ]
    assert len(stats.batches_in_state(view, stats.ALL_STATES)) == 2
    assert stats.batches_in_state(view, "active") == []


def test_empty_and_missing_ledgers_are_explained(tmp_path: Path) -> None:
    view = reports.load_ledger_view(tmp_path)
    assert view.unavailable
    assert view.unavailable in stats.usage_text(view)
    assert "не запускался" in stats.pipeline_stats_text(
        stats.pipeline_stats(reports.LedgerView())
    )


def test_harness_state_reads_the_lock_and_skills(tmp_path: Path) -> None:
    assert stats.harness_state_text(stats.harness_state(tmp_path), "1.0.0").startswith(
        "Харнесс не установлен"
    )
    harness_dir = tmp_path / ".harness"
    (harness_dir / "skills" / "grilling").mkdir(parents=True)
    (harness_dir / "skills" / "grilling" / "SKILL.md").write_text("x", encoding="utf-8")
    (harness_dir / "skills" / "notes").mkdir()
    (harness_dir / "harness.lock").write_text(
        json.dumps(
            {
                "package_version": "0.9.0",
                "capabilities": ["pvmalove-suite"],
                "files": {"a": "1", "b": "2"},
            }
        ),
        encoding="utf-8",
    )

    state = stats.harness_state(tmp_path)
    text = stats.harness_state_text(state, "1.0.0")

    assert (state.skills, state.managed_files, state.orchestration) == (1, 2, False)
    assert "0.9.0 (версия пакета харнесса: 1.0.0)" in text
    assert "Capability: pvmalove-suite" in text
    assert "Оркестрация: не подключена" in text


def test_short_time_keeps_minutes_only() -> None:
    assert stats.short_time("2026-09-28T09:52:05.4+00:00") == "2026-09-28 09:52"
    assert stats.short_time("") == ""
    assert stats.short_time("вчера") == "вчера"
