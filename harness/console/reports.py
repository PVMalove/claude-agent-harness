"""Stdlib-only data for the console's Reports section: completion reports read from the selected
lifecycle-ledger generation, their filters and sections, and a batch's chronology.

Everything is one lenient read of whatever is on disk (`LifecycleLedger.records_root_lenient` and
`read_record_lenient`, the same read-only calls the `orchestration.*` health checks and
`delivery_stats` use): a missing, partial or malformed record degrades to an absent value instead
of aborting the screen. Nothing here writes, migrates or locks the ledger.

A completion report carries no timestamp of its own (see
harness/orchestration/workflow/reports.py `_persist_report`), so its date is the ledger's own
append-only audit entry for `reports/<dispatch_id>.json`; batch state changes come from the same
audit's `transition` entries. QA gate logs (`qa-artifacts/<sha256>.log`, written by
harness/orchestration/qa_lane.py) are linked to their QA report through the `sha256:<digest>` its
Output field names; failed qa-lane attempts (`qa-lane/attempts/*.json`) through their `dispatch_id`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from ..orchestration.core.constants import STATE_REL
from .export import MarkdownDocument, MarkdownSection
from ..orchestration.ledger.lifecycle import JsonObject, JsonValue, LifecycleLedger

REPORT_SECTIONS = ("Output", "Checks", "Risks", "Blockers", "Next action")
QA_LOG_TAIL_LINES = 15
_QA_SHA256 = re.compile(r"sha256:([0-9a-f]{64})")
_QA_COMMAND = re.compile(r"^\$ (.*)$")
_QA_EXIT = re.compile(r"^exit_code=(-?\d+)$")
_ROLE_FLOW_SEPARATOR = " → "


@dataclass(frozen=True)
class ReportEntry:
    """One completion report plus the ledger facts the list shows and filters on."""

    dispatch_id: str
    batch_id: str
    ticket: str
    role: str
    outcome: str
    reported_at: str
    report: JsonObject


@dataclass(frozen=True)
class BatchSummary:
    batch_id: str
    ticket: str
    state: str
    created_at: str


@dataclass(frozen=True)
class TimelineEvent:
    """One line of a batch chronology. `kind` is one of: state, dispatch, report, decision, risk."""

    at: str
    kind: str
    text: str
    role: str | None = None


@dataclass(frozen=True)
class BatchTimeline:
    batch: BatchSummary
    role_flow: list[str]
    events: list[TimelineEvent]


@dataclass(frozen=True)
class QaAttempt:
    """A qa-lane run that stopped before its QA report was persisted."""

    dispatch_id: str
    stage: str
    failed_at: str
    message: str


@dataclass(frozen=True)
class QaRun:
    """One QA gate log. `dispatch_id` is None for a log no QA report points to (the run failed
    after persisting it); `commands` are (command, exit code) pairs parsed from the log itself."""

    artifact: str
    logged_at: str
    dispatch_id: str | None
    ticket: str
    outcome: str
    commands: list[tuple[str, int]]
    tail: list[str]
    total_lines: int


@dataclass
class LedgerView:
    """Every record the Reports section needs, read once. `unavailable` is a human-readable reason
    when there is no generation to read (orchestration not connected or not initialised)."""

    reports: list[ReportEntry] = field(default_factory=list)
    batches: list[BatchSummary] = field(default_factory=list)
    qa_runs: list[QaRun] = field(default_factory=list)
    qa_attempts: list[QaAttempt] = field(default_factory=list)
    unavailable: str | None = None
    _batch_records: dict[str, JsonObject] = field(default_factory=dict)
    _dispatches: dict[str, JsonObject] = field(default_factory=dict)
    _risks: dict[str, JsonObject] = field(default_factory=dict)
    _audit: dict[str, list[JsonObject]] = field(default_factory=dict)

    def timeline(self, batch_id: str) -> BatchTimeline | None:
        batch = self._batch_records.get(batch_id)
        if batch is None:
            return None
        return _build_timeline(self, batch)


def _text(value: JsonValue | None, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _strings(value: JsonValue | None) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _objects(value: JsonValue | None) -> list[JsonObject]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _read_directory(root: Path, directory: str) -> list[JsonObject]:
    records: list[JsonObject] = []
    for path in sorted((root / directory).glob("*.json")):
        record = LifecycleLedger.read_record_lenient(path)
        if record is not None:
            records.append(record)
    return records


def _audit_index(root: Path) -> dict[str, list[JsonObject]]:
    """`details.path` -> its audit events, oldest first."""
    index: dict[str, list[JsonObject]] = {}
    for event in _read_directory(root, "audit"):
        details = event.get("details")
        if not isinstance(details, dict) or not isinstance(event.get("at"), str):
            continue
        path = details.get("path")
        if isinstance(path, str):
            index.setdefault(path, []).append(event)
    for events in index.values():
        events.sort(key=lambda item: _text(item.get("at")))
    return index


def _first_write_at(view: LedgerView, relative: str) -> str:
    for event in view._audit.get(relative, []):
        if event.get("action") in {"immutable-record", "immutable-artifact"}:
            return _text(event.get("at"))
    return ""


def load_ledger_view(repo: Path) -> LedgerView:
    root = LifecycleLedger(repo / STATE_REL).records_root_lenient()
    if root is None:
        return LedgerView(
            unavailable="леджер оркестрации не найден или не инициализирован"
        )
    view = LedgerView(_audit=_audit_index(root))
    for record in _read_directory(root, "dispatches"):
        dispatch_id = record.get("dispatch_id")
        if isinstance(dispatch_id, str):
            view._dispatches[dispatch_id] = record
    for record in _read_directory(root, "risk-assessments"):
        risk_id = record.get("risk_assessment_id")
        if isinstance(risk_id, str):
            view._risks[risk_id] = record
    for record in _read_directory(root, "batches"):
        batch_id = record.get("batch_id")
        if not isinstance(batch_id, str):
            continue
        view._batch_records[batch_id] = record
        view.batches.append(
            BatchSummary(
                batch_id=batch_id,
                ticket=_text(record.get("ticket"), "?"),
                state=_text(record.get("state"), "?"),
                created_at=_text(record.get("created_at"))
                or _first_write_at(view, f"batches/{batch_id}.json"),
            )
        )
    view.batches.sort(key=lambda batch: batch.created_at, reverse=True)
    for report in _read_directory(root, "reports"):
        dispatch_id = report.get("dispatch_id")
        if not isinstance(dispatch_id, str):
            continue
        dispatch = view._dispatches.get(dispatch_id, {})
        view.reports.append(
            ReportEntry(
                dispatch_id=dispatch_id,
                batch_id=_text(dispatch.get("batch_id")),
                ticket=_text(report.get("ticket"), "?"),
                role=_text(report.get("role"), "?"),
                outcome=_text(report.get("outcome"), "?"),
                reported_at=_first_write_at(view, f"reports/{dispatch_id}.json")
                or _text(dispatch.get("created_at")),
                report=report,
            )
        )
    view.reports.sort(key=lambda entry: entry.reported_at, reverse=True)
    _load_qa(view, root)
    return view


def _qa_commands(lines: list[str]) -> list[tuple[str, int]]:
    commands: list[tuple[str, int]] = []
    for index, line in enumerate(lines):
        command = _QA_COMMAND.match(line)
        if command is None or index + 1 >= len(lines):
            continue
        exit_code = _QA_EXIT.match(lines[index + 1])
        if exit_code is not None:
            commands.append((command.group(1), int(exit_code.group(1))))
    return commands


def _load_qa(view: LedgerView, root: Path) -> None:
    reports_by_sha = {
        match.group(1): entry
        for entry in view.reports
        if entry.role == "qa"
        and (match := _QA_SHA256.search(_text(entry.report.get("output")))) is not None
    }
    for path in sorted((root / "qa-artifacts").glob("*.log")):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        relative = f"qa-artifacts/{path.name}"
        entry = reports_by_sha.get(path.stem)
        view.qa_runs.append(
            QaRun(
                artifact=relative,
                logged_at=_first_write_at(view, relative),
                dispatch_id=entry.dispatch_id if entry else None,
                ticket=entry.ticket if entry else "?",
                outcome=entry.outcome if entry else "нет отчёта",
                commands=_qa_commands(lines),
                tail=lines[-QA_LOG_TAIL_LINES:],
                total_lines=len(lines),
            )
        )
    view.qa_runs.sort(key=lambda run: run.logged_at, reverse=True)
    for record in _read_directory(root, "qa-lane/attempts"):
        view.qa_attempts.append(
            QaAttempt(
                dispatch_id=_text(record.get("dispatch_id"), "?"),
                stage=_text(record.get("stage"), "?"),
                failed_at=_text(record.get("failed_at")),
                message=_text(record.get("message")),
            )
        )
    view.qa_attempts.sort(key=lambda attempt: attempt.failed_at, reverse=True)


def qa_run_text(run: QaRun) -> str:
    """The log's verdict, then its tail."""
    failed = [(command, code) for command, code in run.commands if code != 0]
    verdict = "failed" if failed else "passed"
    summary = f"итог: {verdict} — команд {len(run.commands)}, с ошибкой {len(failed)}"
    if failed:
        summary += (
            " ("
            + ", ".join(f"{command}: exit {code}" for command, code in failed)
            + ")"
        )
    lines = [
        f"{run.artifact} · {run.logged_at or 'время ?'} · "
        f"{run.dispatch_id or 'без отчёта'} · {run.ticket} · отчёт: {run.outcome}",
        summary,
        f"хвост лога (последние {len(run.tail)} из {run.total_lines} строк):",
        *(f"  {line}" for line in run.tail),
    ]
    return "\n".join(lines)


def qa_attempts_text(attempts: list[QaAttempt]) -> str:
    return "\n".join(
        [f"попытки qa-lane: {len(attempts)}"]
        + [
            f"  {attempt.failed_at or 'время ?'} · {attempt.dispatch_id} · "
            f"{attempt.stage}: {attempt.message}"
            for attempt in attempts
        ]
    )


def qa_log_text(view: LedgerView, dispatch_id: str) -> str:
    """Everything the ledger holds about one QA dispatch's runs; empty for a non-QA dispatch."""
    runs = [run for run in view.qa_runs if run.dispatch_id == dispatch_id]
    attempts = [item for item in view.qa_attempts if item.dispatch_id == dispatch_id]
    if not runs and not attempts:
        return ""
    return "\n\n".join(
        [qa_attempts_text(attempts), *(qa_run_text(run) for run in runs)]
    )


def filter_reports(
    reports: list[ReportEntry],
    *,
    ticket: str = "",
    role: str = "",
    outcome: str = "",
    date: str = "",
) -> list[ReportEntry]:
    """Empty filters match everything. `ticket` and `role` are case-insensitive substrings,
    `outcome` is exact, `date` is a prefix of the ISO timestamp (`2026-09`, `2026-09-27`)."""
    ticket, role = ticket.strip().lower(), role.strip().lower()
    outcome, date = outcome.strip().lower(), date.strip()
    return [
        entry
        for entry in reports
        if (not ticket or ticket in entry.ticket.lower())
        and (not role or role in entry.role.lower())
        and (not outcome or outcome == entry.outcome.lower())
        and (not date or entry.reported_at.startswith(date))
    ]


def _checks_text(report: JsonObject) -> str:
    checks = _objects(report.get("checks_run"))
    if not checks:
        return "проверки не запускались"
    return "\n".join(
        f"• {_text(check.get('command'), '?')} — {_text(check.get('result'), '?')}: "
        f"{_text(check.get('evidence'))}"
        for check in checks
    )


def _review_text(review: JsonObject) -> str:
    lines = [f"candidate: {_text(review.get('candidate_commit'), '?')}"]
    for axis in ("standards", "spec"):
        evidence = review.get(axis)
        if not isinstance(evidence, dict):
            continue
        findings = _objects(evidence.get("findings"))
        lines.append(
            f"{axis}: {_text(evidence.get('severity'), '?')}, находок: {len(findings)}"
        )
        lines.extend(
            f"  [{_text(item.get('severity'), '?')}] {_text(item.get('summary'))}: "
            f"{_text(item.get('evidence'))}"
            for item in findings
        )
    return "\n".join(lines)


def report_sections(report: JsonObject) -> list[tuple[str, str]]:
    """The report body in reading order: the five REPORT_SECTIONS, plus Review when present."""
    sections = [
        ("Output", _text(report.get("output"))),
        ("Checks", _checks_text(report)),
    ]
    review = report.get("review")
    if isinstance(review, dict):
        sections.append(("Review", _review_text(review)))
    sections.extend(
        [
            ("Risks", _text(report.get("risks"))),
            ("Blockers", _text(report.get("blockers"))),
            ("Next action", _text(report.get("next_coordinator_action"))),
        ]
    )
    return sections


def report_header(entry: ReportEntry) -> str:
    files = ", ".join(_strings(entry.report.get("changed_files")))
    return (
        f"{entry.ticket} · {entry.role} · {entry.outcome} · {entry.reported_at or 'дата неизвестна'}\n"
        f"dispatch: {entry.dispatch_id}  batch: {entry.batch_id or '?'}\n"
        f"commit: {_text(entry.report.get('commit_sha'), '—')}  файлы: {files or '—'}"
    )


def _build_timeline(view: LedgerView, batch: JsonObject) -> BatchTimeline:
    batch_id = cast(str, batch["batch_id"])
    summary = next(item for item in view.batches if item.batch_id == batch_id)
    events: list[TimelineEvent] = [
        TimelineEvent(at=summary.created_at, kind="state", text="батч создан")
    ]
    for audit in view._audit.get(f"batches/{batch_id}.json", []):
        details = audit.get("details")
        if audit.get("action") != "transition" or not isinstance(details, dict):
            continue
        before, after = _text(details.get("from")), _text(details.get("to"))
        if before and after and before != after:
            events.append(
                TimelineEvent(
                    at=_text(audit.get("at")),
                    kind="state",
                    text=f"состояние: {before} → {after}",
                )
            )

    role_flow: list[str] = []
    report_times: dict[str, str] = {}
    for entry in _objects(batch.get("dispatches")):
        dispatch_id = _text(entry.get("dispatch_id"))
        dispatch = view._dispatches.get(dispatch_id, {})
        role = _text(entry.get("role")) or _text(dispatch.get("role"), "?")
        role_flow.append(role)
        purpose = _text(dispatch.get("purpose"))
        events.append(
            TimelineEvent(
                at=_text(dispatch.get("created_at")),
                kind="dispatch",
                role=role,
                text=f"{role}{f' ({purpose})' if purpose and purpose != 'work' else ''}: "
                f"{dispatch_id}, итог: {_text(entry.get('state'), '?')}",
            )
        )
        report = next(
            (item for item in view.reports if item.dispatch_id == dispatch_id), None
        )
        if report is not None:
            report_times[dispatch_id] = report.reported_at
            events.append(
                TimelineEvent(
                    at=report.reported_at,
                    kind="report",
                    role=role,
                    text=f"отчёт {role}: {report.outcome}",
                )
            )

    for decision in _objects(batch.get("coordinator_decisions")):
        parts = [f"решение coordinator: {_text(decision.get('decision'), '?')}"]
        next_role = _text(decision.get("next_role"))
        if next_role:
            parts.append(f"→ {next_role}")
        approved_by = _text(decision.get("approved_by"))
        if approved_by:
            parts.append(f"({approved_by})")
        note = _text(decision.get("note"))
        if note and note != "none":
            parts.append(f"— {note}")
        events.append(
            TimelineEvent(
                at=_text(decision.get("approved_at")),
                kind="decision",
                text=" ".join(parts),
            )
        )

    for assessment in _objects(batch.get("risk_assessments")):
        risk = view._risks.get(_text(assessment.get("risk_assessment_id")), {})
        triggers = _strings(assessment.get("matched_triggers"))
        review = (
            "нужен review" if assessment.get("review_required") else "review не нужен"
        )
        events.append(
            TimelineEvent(
                at=_text(risk.get("created_at")),
                kind="risk",
                text=f"оценка риска: {', '.join(triggers) or 'триггеров нет'}; {review}",
            )
        )
    for escalation in _objects(batch.get("risk_escalations")):
        triggers = _strings(escalation.get("triggers"))
        events.append(
            TimelineEvent(
                at=report_times.get(_text(escalation.get("dispatch_id")), ""),
                kind="risk",
                role="developer",
                text=f"эскалация риска: {', '.join(triggers)}",
            )
        )

    # Stable sort: events with no recorded time keep their order and go last.
    events.sort(key=lambda event: (event.at == "", event.at))
    return BatchTimeline(batch=summary, role_flow=role_flow, events=events)


def role_flow_text(timeline: BatchTimeline) -> str:
    return _ROLE_FLOW_SEPARATOR.join(timeline.role_flow) or "диспатчей ещё не было"


def _cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def report_document(view: LedgerView, entry: ReportEntry) -> MarkdownDocument:
    """A completion report (plus its QA log for a QA dispatch) as an exportable document."""
    report = entry.report
    checks = _objects(report.get("checks_run"))
    sections: list[MarkdownSection] = []
    for title, body in report_sections(report):
        if title == "Checks" and checks:
            body = "\n".join(
                f"- `{_text(check.get('command'), '?')}` — {_text(check.get('result'), '?')}: "
                f"{_text(check.get('evidence'))}"
                for check in checks
            )
        sections.append(MarkdownSection(title, body, preformatted=title == "Review"))
    qa_log = qa_log_text(view, entry.dispatch_id)
    if qa_log:
        sections.append(MarkdownSection("QA log", qa_log, preformatted=True))
    return MarkdownDocument(
        title=f"Completion report {entry.dispatch_id}",
        slug=f"report-{entry.dispatch_id}",
        ticket=entry.ticket,
        meta=[
            ("Ticket", entry.ticket),
            ("Role", entry.role),
            ("Outcome", entry.outcome),
            ("Reported at", entry.reported_at or "неизвестно"),
            ("Dispatch", entry.dispatch_id),
            ("Batch", entry.batch_id or "?"),
            ("Commit", _text(report.get("commit_sha"), "—")),
            ("Changed files", ", ".join(_strings(report.get("changed_files"))) or "—"),
        ],
        sections=sections,
    )


def timeline_document(timeline: BatchTimeline) -> MarkdownDocument:
    batch = timeline.batch
    rows = [
        "| Время | Тип | Роль | Событие |",
        "| --- | --- | --- | --- |",
        *(
            f"| {_cell(event.at or '?')} | {event.kind} | {_cell(event.role or '—')} "
            f"| {_cell(event.text)} |"
            for event in timeline.events
        ),
    ]
    return MarkdownDocument(
        title=f"Хронология батча {batch.batch_id}",
        slug=f"timeline-{batch.batch_id}",
        ticket=batch.ticket,
        meta=[
            ("Ticket", batch.ticket),
            ("Batch", batch.batch_id),
            ("State", batch.state),
            ("Маршрут", role_flow_text(timeline)),
        ],
        sections=[MarkdownSection("События", "\n".join(rows))],
    )
