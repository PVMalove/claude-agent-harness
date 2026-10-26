"""harness.console.reports: the stdlib-only data behind the console's Reports section, against a
real lifecycle-ledger fixture (tests/_console_ledger_fixture.py). No textual needed."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import subprocess

import pytest

from tests.console._console_ledger_fixture import (
    LONG_OUTPUT,
    QA_LOG,
    QA_LOG_SHA256,
    build_reports_fixture,
)
from harness.console.export import render_markdown
from harness.orchestration.ledger.lifecycle import JsonObject
from harness.console.reports import (
    QA_LOG_TAIL_LINES,
    REPORT_SECTIONS,
    BatchSummary,
    ReportEntry,
    StageEntry,
    batch_status,
    outcome_status,
    qa_run_status,
    report_section_lines,
    stage_status,
    filter_reports,
    load_ledger_view,
    qa_log_text,
    qa_run_text,
    report_document,
    report_sections,
    role_flow_text,
    timeline_document,
)


def test_reports_are_listed_from_the_selected_generation_newest_first(
    tmp_path: Path,
) -> None:
    build_reports_fixture(tmp_path)
    view = load_ledger_view(tmp_path)

    assert view.unavailable is None
    assert [entry.dispatch_id for entry in view.reports] == [
        "dispatch-stuck",
        "dispatch-qa",
        "dispatch-review",
        "dispatch-dev",
    ]
    developer = view.reports[-1]
    assert (developer.ticket, developer.role, developer.outcome) == (
        "#101",
        "developer",
        "completed",
    )
    assert developer.batch_id == "batch-flow"
    # The report has no timestamp field; its date is the ledger audit entry of its first write.
    assert developer.reported_at.startswith("20")
    assert all(entry.reported_at for entry in view.reports)


def test_filters_by_ticket_role_outcome_and_date(tmp_path: Path) -> None:
    build_reports_fixture(tmp_path)
    reports = load_ledger_view(tmp_path).reports

    assert {entry.dispatch_id for entry in filter_reports(reports, ticket="202")} == {
        "dispatch-stuck"
    }
    assert {
        entry.dispatch_id for entry in filter_reports(reports, role="Developer")
    } == {
        "dispatch-dev",
        "dispatch-stuck",
    }
    assert [
        entry.dispatch_id
        for entry in filter_reports(reports, ticket="#101", role="review")
    ] == ["dispatch-review"]
    assert [
        entry.dispatch_id for entry in filter_reports(reports, outcome="blocked")
    ] == ["dispatch-stuck"]
    day = reports[0].reported_at[:10]
    assert len(filter_reports(reports, date=day)) == 4
    assert filter_reports(reports, date="1999-01") == []
    assert filter_reports(reports) == reports


def test_missing_report_time_is_not_replaced_by_dispatch_time(tmp_path: Path) -> None:
    import json

    from harness.console.reports import select_stages
    from harness.orchestration.core.constants import STATE_REL
    from harness.orchestration.ledger.lifecycle import LifecycleLedger

    build_reports_fixture(tmp_path)
    root = LifecycleLedger(tmp_path / STATE_REL).records_root()
    for path in (root / "audit").glob("*.json"):
        event = json.loads(path.read_text(encoding="utf-8"))
        if event.get("details", {}).get("path") == "reports/dispatch-qa.json":
            path.unlink()

    view = load_ledger_view(tmp_path)
    assert view._dispatches["dispatch-qa"]["created_at"]
    stage = next(item for item in view.stages if item.dispatch_id == "dispatch-qa")
    assert stage.report is not None
    assert stage.at == stage.report.reported_at == ""
    for descending in (True, False):
        assert select_stages(view.stages, descending=descending)[-1] == stage
    assert stage not in select_stages(view.stages, date="20")


def test_stages_include_working_dispatches_and_replace_them_with_reports(
    tmp_path: Path,
) -> None:
    from harness.orchestration.core.constants import STATE_REL
    from harness.orchestration.ledger.lifecycle import LifecycleLedger
    from tests.console._console_ledger_fixture import _Batch

    build_reports_fixture(tmp_path)
    ledger = LifecycleLedger(tmp_path / STATE_REL)
    batch = _Batch(ledger, "batch-live", "#303")
    batch.save("awaiting-approval")
    batch.dispatch("dispatch-live", "developer")
    ledger.write_immutable(
        batch.root / "dispatch-status/dispatch-live.json",
        {"dispatch_id": "dispatch-live", "state": "working"},
    )
    view = load_ledger_view(tmp_path)
    stage = next(item for item in view.stages if item.dispatch_id == "dispatch-live")
    assert (stage.ticket, stage.role, stage.state, stage.report) == (
        "#303",
        "developer",
        "working",
        None,
    )
    assert stage.at
    assert len(view.stages) == 5
    batch.report("dispatch-live", "developer", "completed", output="Готово")
    refreshed = load_ledger_view(tmp_path)
    matching = [
        item for item in refreshed.stages if item.dispatch_id == "dispatch-live"
    ]
    assert len(matching) == 1
    assert matching[0].report is not None
    assert matching[0].report.outcome == "completed"


def test_batches_without_titles_remain_local_read_only_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness.orchestration.core.constants import STATE_REL
    from harness.orchestration.ledger.lifecycle import LifecycleLedger
    from tests.console._console_ledger_fixture import _Batch

    ledger = LifecycleLedger(tmp_path / STATE_REL)
    ledger.ensure()
    batch = _Batch(ledger, "batch-legacy", "#303")
    batch.save("awaiting-approval")
    batch.dispatch("dispatch-legacy", "developer")
    before = {
        path: path.read_bytes()
        for path in (tmp_path / STATE_REL).rglob("*")
        if path.is_file()
    }

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("reading existing batches must not run a tracker CLI")

    monkeypatch.setattr(subprocess, "run", forbidden)

    [stage] = load_ledger_view(tmp_path).stages

    assert (stage.ticket, stage.dispatch_id, stage.role, stage.state) == (
        "#303",
        "dispatch-legacy",
        "developer",
        "dispatched",
    )
    assert {
        path: path.read_bytes()
        for path in (tmp_path / STATE_REL).rglob("*")
        if path.is_file()
    } == before


def test_stage_filters_combine_and_sort_unknown_times_last() -> None:
    from harness.console.reports import ReportEntry, StageEntry, select_stages

    report = ReportEntry(
        "dispatch-old",
        "batch-a",
        "#10",
        "developer",
        "completed",
        "2026-10-12T09:00:00Z",
        {},
    )
    stages = [
        StageEntry(
            "dispatch-unknown", "batch-b", "#20", "qa", "неизвестно", "bad date"
        ),
        StageEntry(
            "dispatch-live",
            "batch-a",
            "#10",
            "developer",
            "working",
            "2026-10-12T12:00:00+02:00",
        ),
        StageEntry(
            "dispatch-old",
            "batch-a",
            "#10",
            "developer",
            "reported",
            report.reported_at,
            report,
        ),
        StageEntry(
            "dispatch-tie",
            "batch-c",
            "#30",
            "architect",
            "dispatched",
            "2026-10-12T10:00:00Z",
        ),
    ]
    assert [entry.dispatch_id for entry in select_stages(stages)] == [
        "dispatch-live",
        "dispatch-tie",
        "dispatch-old",
        "dispatch-unknown",
    ]
    assert [entry.dispatch_id for entry in select_stages(stages, descending=False)] == [
        "dispatch-old",
        "dispatch-live",
        "dispatch-tie",
        "dispatch-unknown",
    ]
    assert [
        entry.dispatch_id
        for entry in select_stages(
            stages,
            ticket="10",
            role="Developer",
            state="working",
            completion="open",
            date="2026-10-12",
        )
    ] == ["dispatch-live"]
    assert [
        entry.dispatch_id
        for entry in select_stages(stages, outcome="completed", completion="finished")
    ] == ["dispatch-old"]
    assert select_stages(stages, state="working", outcome="completed") == []
    assert [
        entry.dispatch_id
        for entry in select_stages(stages, sort_by="role", descending=False)
    ] == ["dispatch-tie", "dispatch-live", "dispatch-old", "dispatch-unknown"]
    assert [
        entry.dispatch_id
        for entry in select_stages(stages, sort_by="state", descending=False)
    ] == ["dispatch-tie", "dispatch-old", "dispatch-live", "dispatch-unknown"]


def test_missing_batch_start_is_not_reconstructed_from_audit(tmp_path: Path) -> None:
    from harness.console.reports import batch_start_text
    from harness.orchestration.core.constants import STATE_REL
    from harness.orchestration.ledger.lifecycle import LifecycleLedger
    from tests.console._console_ledger_fixture import _Batch

    ledger = LifecycleLedger(tmp_path / STATE_REL)
    ledger.ensure()
    batch = _Batch(ledger, "batch-unknown", "#101")
    batch.record.pop("created_at")
    batch.save("awaiting-approval")
    [summary] = load_ledger_view(tmp_path).batches
    assert batch_start_text(summary) == "начало: неизвестно"


def test_report_sections_follow_the_reading_order(tmp_path: Path) -> None:
    build_reports_fixture(tmp_path)
    reports = {entry.dispatch_id: entry for entry in load_ledger_view(tmp_path).reports}

    developer = report_sections(reports["dispatch-dev"].report)
    assert [title for title, _ in developer] == list(REPORT_SECTIONS)
    body = dict(developer)
    assert body["Output"] == LONG_OUTPUT
    assert "make test — pass: 412 passed" in body["Checks"]

    blocked = dict(report_sections(reports["dispatch-stuck"].report))
    assert blocked["Blockers"] == "нет токена трекера в окружении"

    review = report_sections(reports["dispatch-review"].report)
    assert [title for title, _ in review] == [
        "Output",
        "Checks",
        "Review",
        "Risks",
        "Blockers",
        "Next action",
    ]
    assert "[low] имя функции: reports.py:10" in dict(review)["Review"]


def test_batch_timeline_orders_states_dispatches_decisions_and_risks(
    tmp_path: Path,
) -> None:
    build_reports_fixture(tmp_path)
    view = load_ledger_view(tmp_path)

    timeline = view.timeline("batch-flow")
    assert timeline is not None
    assert timeline.batch.state == "completed"
    assert role_flow_text(timeline) == "developer → code-review → qa"

    texts = [event.text for event in timeline.events]
    assert texts[0] == "батч создан"
    assert texts[-1] == "состояние: awaiting-approval → completed"
    assert [event.at for event in timeline.events] == sorted(
        event.at for event in timeline.events
    )

    def index(fragment: str) -> int:
        return next(i for i, text in enumerate(texts) if fragment in text)

    assert (
        index("developer: dispatch-dev")
        < index("отчёт developer: completed")
        < index("accept → code-review")
        < index("code-review (review): dispatch-review")
        < index("accept → qa")
        < index("qa: dispatch-qa")
        < index("отчёт qa: completed")
    )
    assert "эскалация риска: schema" in texts
    assert "оценка риска: schema; нужен review" in texts
    assert {event.kind for event in timeline.events} == {
        "state",
        "dispatch",
        "report",
        "decision",
        "risk",
    }
    assert view.timeline("batch-missing") is None


def test_missing_ledger_is_reported_as_unavailable(tmp_path: Path) -> None:
    view = load_ledger_view(tmp_path)
    assert view.unavailable is not None
    assert view.reports == []
    assert view.batches == []


def test_malformed_records_are_skipped_not_fatal(tmp_path: Path) -> None:
    build_reports_fixture(tmp_path)
    generation = next(
        (tmp_path / ".harness" / "orchestration" / "state" / "generations").iterdir()
    )
    (generation / "reports" / "dispatch-broken.json").write_text(
        "{not json", encoding="utf-8"
    )
    (generation / "batches" / "batch-broken.json").write_text("[]", encoding="utf-8")

    view = load_ledger_view(tmp_path)
    assert len(view.reports) == 4
    assert {batch.batch_id for batch in view.batches} == {"batch-flow", "batch-stuck"}


def test_qa_runs_link_the_gate_log_to_its_qa_report(tmp_path: Path) -> None:
    build_reports_fixture(tmp_path)
    view = load_ledger_view(tmp_path)

    assert len(view.qa_runs) == 1
    run = view.qa_runs[0]
    assert run.artifact == f"qa-artifacts/{QA_LOG_SHA256}.log"
    assert (run.dispatch_id, run.ticket, run.outcome) == (
        "dispatch-qa",
        "#101",
        "completed",
    )
    assert run.logged_at.startswith("20")
    assert run.commands == [("make lint", 0), ("make test", 0)]
    assert len(run.tail) == QA_LOG_TAIL_LINES
    assert run.tail[-1] == "412 passed in 9.81s"
    assert run.total_lines == len(QA_LOG.splitlines())

    [attempt] = view.qa_attempts
    assert (attempt.dispatch_id, attempt.stage) == ("dispatch-qa", "gate-run")
    assert attempt.message == "clean-room checkout failed"


def test_qa_log_text_shows_summary_attempts_and_tail(tmp_path: Path) -> None:
    build_reports_fixture(tmp_path)
    view = load_ledger_view(tmp_path)

    text = qa_log_text(view, "dispatch-qa")
    assert "итог: passed — команд 2, с ошибкой 0" in text
    assert "попытки qa-lane: 1" in text
    assert "gate-run: clean-room checkout failed" in text
    assert (
        f"хвост лога (последние {QA_LOG_TAIL_LINES} из {len(QA_LOG.splitlines())} строк)"
        in text
    )
    assert text.rstrip().endswith("412 passed in 9.81s")
    assert qa_log_text(view, "dispatch-dev") == ""


def test_qa_log_without_a_report_is_still_listed(tmp_path: Path) -> None:
    build_reports_fixture(tmp_path)
    generation = next(
        (tmp_path / ".harness" / "orchestration" / "state" / "generations").iterdir()
    )
    (generation / "qa-artifacts" / "orphan.log").write_text(
        "$ make test\nexit_code=2\nFAILED tests/test_x.py\n", encoding="utf-8"
    )

    view = load_ledger_view(tmp_path)
    orphan = next(
        run for run in view.qa_runs if run.artifact == "qa-artifacts/orphan.log"
    )
    assert orphan.dispatch_id is None
    assert orphan.outcome == "нет отчёта"
    assert orphan.commands == [("make test", 2)]
    assert "итог: failed — команд 1, с ошибкой 1 (make test: exit 2)" in qa_run_text(
        orphan
    )


def test_report_document_is_the_report_plus_its_qa_log(tmp_path: Path) -> None:
    build_reports_fixture(tmp_path)
    view = load_ledger_view(tmp_path)
    entry = next(item for item in view.reports if item.dispatch_id == "dispatch-qa")

    document = report_document(view, entry)
    assert (document.ticket, document.slug) == ("#101", "report-dispatch-qa")
    assert [section.heading for section in document.sections] == [
        *REPORT_SECTIONS,
        "QA log",
    ]
    text = render_markdown(document)
    assert "- **Outcome:** completed" in text
    assert "## Checks\n\n- `make test` — pass: 412 passed" in text
    assert "## QA log\n\n```text\nпопытки qa-lane: 1" in text


def test_timeline_document_is_a_table_of_events(tmp_path: Path) -> None:
    build_reports_fixture(tmp_path)
    timeline = load_ledger_view(tmp_path).timeline("batch-flow")
    assert timeline is not None

    text = render_markdown(timeline_document(timeline))
    assert text.startswith("# Хронология батча batch-flow\n")
    assert "- **Маршрут:** developer → code-review → qa" in text
    assert "| Время | Тип | Роль | Событие |" in text
    assert (
        "| decision | — | решение coordinator: accept → code-review (coordinator) |"
        in text
    )
    assert text.count("\n| 20") == len(timeline.events)


def _stage(state: str, outcome: str | None) -> StageEntry:
    report = (
        ReportEntry("dispatch-x", "batch-x", "#1", "developer", outcome, "", {})
        if outcome is not None
        else None
    )
    return StageEntry("dispatch-x", "batch-x", "#1", "developer", state, "", report)


def test_stage_status_is_a_symbol_and_a_label_for_every_known_outcome() -> None:
    cases = {
        ("reported", "completed"): "✔ отчёт получен · завершено",
        ("reported", "blocked"): "⚠ отчёт получен · заблокировано",
        ("reported", "failed"): "✖ отчёт получен · ошибка",
        ("working", None): "⚠ в работе · отчёта ещё нет",
    }
    for (state, outcome), shown in cases.items():
        assert stage_status(_stage(state, outcome)).render("ru") == shown


def test_stage_status_is_shorter_on_a_narrow_terminal_and_follows_the_language() -> (
    None
):
    entry = _stage("reported", "completed")
    assert stage_status(entry, narrow=True).render("ru") == "✔ отчёт · завершено"
    assert stage_status(entry).render("en") == "✔ report received · completed"


def test_stage_status_shows_an_unknown_ledger_value_as_it_is() -> None:
    assert (
        stage_status(_stage("reported", "weird")).render("ru")
        == "⚠ отчёт получен · weird"
    )
    assert (
        stage_status(_stage("queued", None)).render("ru") == "⚠ queued · отчёта ещё нет"
    )


def test_batch_status_names_the_state_with_a_symbol_and_keeps_an_unknown_one() -> None:
    def batch(state: str) -> BatchSummary:
        return BatchSummary("batch-x", "#1", state, "")

    assert batch_status(batch("completed")).render("ru") == "✔ завершён"
    assert batch_status(batch("blocked")).render("ru") == "⚠ заблокирован"
    assert batch_status(batch("active")).render("en") == "▶ in progress"
    assert batch_status(batch("mystery")).render("ru") == "⚠ mystery"


def test_outcome_status_keeps_the_ledger_word_beside_its_symbol() -> None:
    assert outcome_status("completed").render("ru") == "✔ completed"
    assert outcome_status("blocked").render("ru") == "⚠ blocked"
    assert outcome_status("failed").render("ru") == "✖ failed"
    assert outcome_status("odd").render("ru") == "⚠ odd"


def test_report_section_lines_give_every_check_and_every_risk_a_symbol_and_label(
    tmp_path: Path,
) -> None:
    build_reports_fixture(tmp_path)
    reports = {entry.dispatch_id: entry for entry in load_ledger_view(tmp_path).reports}

    developer = dict(report_section_lines(reports["dispatch-dev"].report))
    assert list(developer) == list(REPORT_SECTIONS)
    [(status, check)] = developer["Checks"]
    assert status is not None and status.render("ru") == "✔ pass"
    assert check == "make test — 412 passed"
    [(risk, _)] = developer["Risks"]
    assert risk is not None and risk.render("ru") == "✔ нет"
    assert developer["Output"] == [(None, LONG_OUTPUT)]

    blocked = dict(report_section_lines(reports["dispatch-stuck"].report))
    [(blocker, details)] = blocked["Blockers"]
    assert blocker is not None and blocker.render("ru") == "⚠ есть"
    assert details == "нет токена трекера в окружении"


def test_report_review_lines_mark_each_axis_and_keep_the_findings(
    tmp_path: Path,
) -> None:
    build_reports_fixture(tmp_path)
    reports = {entry.dispatch_id: entry for entry in load_ledger_view(tmp_path).reports}

    lines = dict(report_section_lines(reports["dispatch-review"].report))["Review"]
    shown = [(status.render("ru") if status else "", text) for status, text in lines]
    assert shown == [
        ("", "candidate: " + "a" * 40),
        ("⚠ low", "standards · находок: 1"),
        ("", "  [low] имя функции: reports.py:10"),
        ("✔ none", "spec · находок: 0"),
    ]


def test_report_review_axis_verdict_follows_the_recorded_severity_not_the_findings_count() -> (
    None
):
    info: JsonObject = {"severity": "info", "summary": "заметка", "evidence": "a.py:1"}
    review: JsonObject = {
        "candidate_commit": "c" * 40,
        "standards": {"severity": "clean", "findings": [info]},
        "spec": {"severity": "warning", "findings": []},
    }
    lines = dict(report_section_lines({"review": review}))["Review"]
    axes = {
        text.split(" · ")[0]: status.render("ru")
        for status, text in lines
        if status is not None
    }
    assert axes == {"standards": "✔ clean", "spec": "⚠ warning"}

    for severity, shown in (
        ("none", "✔ none"),
        ("clean", "✔ clean"),
        ("warning", "⚠ warning"),
        ("blocker", "✖ blocker"),
    ):
        single: JsonObject = {"standards": {"severity": severity, "findings": []}}
        [(status, text)] = dict(report_section_lines({"review": single}))["Review"][1:]
        assert status is not None and status.render("ru") == shown
        assert text == "standards · находок: 0"


def test_qa_run_status_is_failed_when_any_command_exits_non_zero(
    tmp_path: Path,
) -> None:
    build_reports_fixture(tmp_path)
    [run] = load_ledger_view(tmp_path).qa_runs
    assert qa_run_status(run).render("ru") == "✔ passed"
    failing = replace(run, commands=[("make lint", 1)])
    assert qa_run_status(failing).render("ru") == "✖ failed"
