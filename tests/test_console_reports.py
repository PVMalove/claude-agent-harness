"""harness.console.reports: the stdlib-only data behind the console's Reports section, against a
real lifecycle-ledger fixture (tests/_console_ledger_fixture.py). No textual needed."""

from __future__ import annotations

from pathlib import Path

from _console_ledger_fixture import LONG_OUTPUT, build_reports_fixture
from harness.console.reports import (
    REPORT_SECTIONS,
    filter_reports,
    load_ledger_view,
    report_sections,
    role_flow_text,
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


def test_report_sections_follow_the_reading_order(tmp_path: Path) -> None:
    build_reports_fixture(tmp_path)
    reports = {entry.dispatch_id: entry for entry in load_ledger_view(tmp_path).reports}

    developer = report_sections(reports["dispatch-dev"].report)
    assert [title for title, _ in developer] == list(REPORT_SECTIONS)
    body = dict(developer)
    assert body["Output"] == LONG_OUTPUT
    assert "make test — passed: 412 passed" in body["Checks"]

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
