"""Данные для раздела Reports консоли средствами только стандартной библиотеки: отчёты о завершении
из выбранного поколения леджера жизненного цикла, их фильтры и секции, а также хронология батча.

Все операции представляют собой нестрогое чтение данных с диска (`LifecycleLedger.records_root_lenient`
и `read_record_lenient` — те же вызовы только для чтения, которые используют проверки health
`orchestration.*` и `delivery_stats`): отсутствующая, неполная или повреждённая запись приводит к отсутствию
значения вместо сбоя экрана. Модуль ничего не записывает, не мигрирует и не блокирует леджер.

Отчёт о завершении не содержит собственной метки времени (см. `_persist_report` в
harness/orchestration/workflow/reports.py), поэтому его датой служит запись журнала аудита для
`reports/<dispatch_id>.json`; изменения состояния батча считываются из записей `transition` того же аудита.
Логи QA gate (`qa-artifacts/<sha256>.log`) связываются с отчётом QA через `sha256:<digest>` в поле Output;
неудачные попытки qa-lane (`qa-lane/attempts/*.json`) — по `dispatch_id`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from ..gate_runner.gate_runner import parse_command_log
from ..orchestration.core.constants import STATE_REL
from ..orchestration.core.report_evidence import evidence_is_none
from ..orchestration.ledger.lifecycle import JsonObject, JsonValue, LifecycleLedger
from .export import MarkdownDocument, MarkdownSection
from .json_fields import strings, text
from .status import Status

REPORT_SECTIONS = ("Output", "Checks", "Risks", "Blockers", "Next action")
QA_LOG_TAIL_LINES = 15
_QA_SHA256 = re.compile(r"sha256:([0-9a-f]{64})")
_ROLE_FLOW_SEPARATOR = " → "


@dataclass(frozen=True)
class ReportEntry:
    """Отчёт о завершении задачи вместе с атрибутами леджера для фильтрации и отображения в списке."""

    dispatch_id: str
    batch_id: str
    ticket: str
    role: str
    outcome: str
    reported_at: str
    report: JsonObject


@dataclass(frozen=True)
class StageEntry:
    """A recorded dispatch, with its completion report when one is available."""

    dispatch_id: str
    batch_id: str
    ticket: str
    role: str
    state: str
    at: str
    report: ReportEntry | None = None

    @property
    def finished(self) -> bool:
        return self.report is not None or self.state in {
            "reported",
            "cancelled",
            "failed",
            "abandoned",
        }


@dataclass(frozen=True)
class BatchSummary:
    """Краткая сводка по батчу (идентификатор, тикет, состояние, время создания)."""

    batch_id: str
    ticket: str
    state: str
    created_at: str
    branch: str = ""
    pr_created_at: str = ""
    pr_url: str = ""
    pr_state: str = "unknown"


@dataclass(frozen=True)
class TimelineEvent:
    """Событие хронологии выполнения батча (смена состояния, диспатч, отчёт, решение координатора, оценка риска)."""

    at: str
    kind: str
    text: str
    role: str | None = None


@dataclass(frozen=True)
class BatchTimeline:
    """Полная хронология батча: сводка, последовательность ролей (role flow) и список событий."""

    batch: BatchSummary
    role_flow: list[str]
    events: list[TimelineEvent]


@dataclass(frozen=True)
class QaAttempt:
    """Попытка выполнения в qa-lane, завершившаяся сбоем до сохранения отчёта QA."""

    dispatch_id: str
    stage: str
    failed_at: str
    message: str


@dataclass(frozen=True)
class QaRun:
    """Лог прогона QA gate с результатами команд и хвостом вывода."""

    artifact: str
    logged_at: str
    dispatch_id: str | None
    ticket: str
    outcome: str
    commands: list[tuple[str, int]]
    tail: list[str]
    total_lines: int


@dataclass(frozen=True)
class LedgerView:
    """Полный снимок записей леджера оркестрации, считанный для раздела Reports.

    Поля не переназначаются: `load_ledger_view` только заполняет их контейнеры при чтении.
    """

    reports: list[ReportEntry] = field(default_factory=list)
    batches: list[BatchSummary] = field(default_factory=list)
    qa_runs: list[QaRun] = field(default_factory=list)
    qa_attempts: list[QaAttempt] = field(default_factory=list)
    unavailable: str | None = None
    _batch_records: dict[str, JsonObject] = field(default_factory=dict)
    _dispatches: dict[str, JsonObject] = field(default_factory=dict)
    _risks: dict[str, JsonObject] = field(default_factory=dict)
    _audit: dict[str, list[JsonObject]] = field(default_factory=dict)
    stages: list[StageEntry] = field(default_factory=list)

    def dispatch_records(self) -> list[JsonObject]:
        """Все записи диспатчей леджера (для статистики по ролям)."""
        return list(self._dispatches.values())

    def timeline(self, batch_id: str) -> BatchTimeline | None:
        """Строит хронологию для указанного батча либо возвращает None, если батч не найден."""
        batch = self._batch_records.get(batch_id)
        if batch is None:
            return None
        return _build_timeline(self, batch)


def _objects(value: JsonValue | None) -> list[JsonObject]:
    """Извлекает список JSON-объектов (словарей) из произвольного JSON-значения."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _read_directory(root: Path, directory: str) -> list[JsonObject]:
    """Считывает все валидные JSON-записи из указанной поддиректории леджера."""
    records: list[JsonObject] = []
    for path in sorted((root / directory).glob("*.json")):
        record = LifecycleLedger.read_record_lenient(path)
        if record is not None:
            records.append(record)
    return records


def _audit_index(root: Path) -> dict[str, list[JsonObject]]:
    """Строит индекс событий аудита леджера, сгруппированных по относительному пути файла."""
    index: dict[str, list[JsonObject]] = {}
    for event in _read_directory(root, "audit"):
        details = event.get("details")
        if not isinstance(details, dict) or not isinstance(event.get("at"), str):
            continue
        path = details.get("path")
        if isinstance(path, str):
            index.setdefault(path, []).append(event)
    for events in index.values():
        events.sort(key=lambda item: text(item.get("at")))
    return index


def _first_write_at(view: LedgerView, relative: str) -> str:
    """Определяет временную метку первой записи неизменяемого объекта по событиям аудита."""
    for event in view._audit.get(relative, []):
        if event.get("action") in {"immutable-record", "immutable-artifact"}:
            return text(event.get("at"))
    return ""


def load_ledger_view(repo: Path) -> LedgerView:
    """Загружает полное представление данных леджера оркестрации для указанного репозитория."""
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
                ticket=text(record.get("ticket"), "?"),
                state=text(record.get("state"), "?"),
                created_at=text(record.get("created_at")),
                branch=text(record.get("branch")),
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
                batch_id=text(dispatch.get("batch_id")),
                ticket=text(report.get("ticket"), "?"),
                role=text(report.get("role"), "?"),
                outcome=text(report.get("outcome"), "?"),
                reported_at=_first_write_at(view, f"reports/{dispatch_id}.json"),
                report=report,
            )
        )
    view.reports.sort(key=lambda entry: entry.reported_at, reverse=True)
    _load_stages(view, root)
    _load_qa(view, root)
    return view


def timestamp(value: str) -> datetime | None:
    """Parse a recorded, timezone-aware time; missing/invalid times remain unknown."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def batch_start_text(batch: BatchSummary) -> str:
    start = timestamp(batch.created_at)
    return start.strftime("%d.%m.%Y %H:%M UTC") if start else "начало: неизвестно"


def batch_timing_text(batch: BatchSummary, *, language: str = "ru") -> str:
    """Elapsed wall time to PR creation, never inferred from publish or merge."""
    if batch.pr_state == "missing":
        return "PR has not been created" if language == "en" else "PR ещё не создан"
    if batch.pr_state != "found":
        return "to PR: unknown" if language == "en" else "до PR: неизвестно"
    start, end = timestamp(batch.created_at), timestamp(batch.pr_created_at)
    if start is None or end is None or end < start:
        return "to PR: unknown" if language == "en" else "до PR: неизвестно"
    seconds = int((end - start).total_seconds())
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    units = ("d", "h", "min", "s") if language == "en" else ("д", "ч", "мин", "с")
    parts = [
        f"{count} {unit}"
        for count, unit in ((days, units[0]), (hours, units[1]), (minutes, units[2]))
        if count
    ]
    return ("to PR: " if language == "en" else "до PR: ") + (
        " ".join(parts) if parts else f"{seconds} {units[3]}"
    )


# Outcome of a completion report -> its status kind and Russian/English label.
_OUTCOMES = {
    "completed": ("accepted", "завершено", "completed"),
    "blocked": ("warning", "заблокировано", "blocked"),
    "failed": ("error", "ошибка", "failed"),
}
# State of a batch -> its status kind and Russian/English label.
_BATCH_STATES = {
    "planned": ("not_started", "запланирован", "planned"),
    "awaiting-approval": ("not_started", "ждёт подтверждения", "awaiting approval"),
    "active": ("running", "в работе", "in progress"),
    "blocked": ("warning", "заблокирован", "blocked"),
    "completed": ("accepted", "завершён", "completed"),
    "failed": ("error", "провален", "failed"),
    "not-required": ("not_started", "не требуется", "not required"),
    "abandoned": ("paused", "отменён", "abandoned"),
}


def stage_status(entry: StageEntry, *, narrow: bool = False) -> Status:
    """A stage row's status, a symbol and a label: `✔ отчёт получен · завершено`, or
    `⚠ в работе · отчёта ещё нет` while no report is recorded. A value the console does not know is
    shown as the ledger wrote it. A narrow terminal drops `получен`."""
    if entry.report is None:
        known = {"working": ("в работе", "in progress")}.get(entry.state)
        ru, en = known or (entry.state, entry.state)
        return Status("warning", f"{ru} · отчёта ещё нет", f"{en} · no report yet")
    outcome = entry.report.outcome
    kind, ru, en = _OUTCOMES.get(outcome, ("warning", outcome, outcome))
    word_ru, word_en = (
        ("отчёт", "report") if narrow else ("отчёт получен", "report received")
    )
    return Status(kind, f"{word_ru} · {ru}", f"{word_en} · {en}")


def batch_status(batch: BatchSummary) -> Status:
    """A batch row's status, a symbol and a label; an unknown state is shown as recorded."""
    kind, ru, en = _BATCH_STATES.get(batch.state, ("warning", batch.state, batch.state))
    return Status(kind, ru, en)


def outcome_status(outcome: str) -> Status:
    """A report outcome as a symbol and the ledger's own word: `✔ completed`."""
    kind = _OUTCOMES.get(outcome, ("warning",))[0]
    return Status(kind, outcome, outcome)


def qa_run_status(run: QaRun) -> Status:
    """The verdict of a QA gate run: `passed` unless a command exited non-zero."""
    if any(code != 0 for _, code in run.commands):
        return Status("error", "failed", "failed")
    return Status("accepted", "passed", "passed")


ReportLine = tuple[Status | None, str]
_REVIEW_SEVERITIES = {
    "none": "accepted",
    "clean": "accepted",
    "warning": "warning",
    "blocker": "error",
}
_CHECK_RESULTS = {
    "pass": "accepted",
    "fail": "error",
    "not_run": "not_started",
}


def _check_lines(report: JsonObject) -> list[ReportLine]:
    lines: list[ReportLine] = []
    for check in _objects(report.get("checks_run")):
        result = text(check.get("result"), "?")
        kind = _CHECK_RESULTS.get(result, "warning")
        evidence = text(check.get("evidence"))
        command = text(check.get("command"), "?")
        lines.append(
            (
                Status(kind, result, result),
                f"{command} — {evidence}" if evidence else command,
            )
        )
    return lines or [(None, _checks_text(report))]


def _review_lines(review: JsonObject) -> list[ReportLine]:
    lines: list[ReportLine] = [
        (None, f"candidate: {text(review.get('candidate_commit'), '?')}")
    ]
    for axis in ("standards", "spec"):
        evidence = review.get(axis)
        if not isinstance(evidence, dict):
            continue
        findings = _objects(evidence.get("findings"))
        severity = text(evidence.get("severity"), "?")
        status = Status(_REVIEW_SEVERITIES.get(severity, "warning"), severity, severity)
        lines.append((status, f"{axis} · находок: {len(findings)}"))
        lines.extend(
            (
                None,
                f"  [{text(item.get('severity'), '?')}] {text(item.get('summary'))}: "
                f"{text(item.get('evidence'))}",
            )
            for item in findings
        )
    return lines


def _evidence_line(body: str) -> ReportLine:
    if body == "none":
        return (Status("accepted", "нет", "none"), "")
    if body:
        return (Status("warning", "есть", "present"), body)
    return (Status("not_started", "не указано", "not stated"), "")


def report_section_lines(report: JsonObject) -> list[tuple[str, list[ReportLine]]]:
    """The sections of `report_sections`, each as lines a screen can draw: a check, a review axis,
    and a risk or blocker line carry a status (symbol and label); the rest is plain text. The text
    of `report_sections` itself, and the Markdown export built from it, do not change."""
    sections: list[tuple[str, list[ReportLine]]] = []
    for title, body in report_sections(report):
        lines: list[ReportLine]
        review = report.get("review")
        if title == "Checks":
            lines = _check_lines(report)
        elif title == "Review" and isinstance(review, dict):
            lines = _review_lines(review)
        elif title in ("Risks", "Blockers"):
            lines = [_evidence_line(body)]
        else:
            lines = [(None, body or "—")]
        sections.append((title, lines))
    return sections


def _load_stages(view: LedgerView, root: Path) -> None:
    statuses = {
        text(record.get("dispatch_id")): record
        for record in _read_directory(root, "dispatch-status")
    }
    dispatches = dict(view._dispatches)
    entries: dict[str, JsonObject] = {}
    for batch_id, batch in view._batch_records.items():
        for entry in _objects(batch.get("dispatches")):
            dispatch_id = text(entry.get("dispatch_id"))
            if dispatch_id:
                entries[dispatch_id] = entry
                dispatches.setdefault(dispatch_id, {**entry, "batch_id": batch_id})
    reports = {entry.dispatch_id: entry for entry in view.reports}
    for dispatch_id in reports:
        dispatches.setdefault(dispatch_id, {})
    for dispatch_id, dispatch in dispatches.items():
        report = reports.get(dispatch_id)
        batch_id = text(dispatch.get("batch_id")) or (report.batch_id if report else "")
        batch = view._batch_records.get(batch_id, {})
        view.stages.append(
            StageEntry(
                dispatch_id=dispatch_id,
                batch_id=batch_id,
                ticket=report.ticket
                if report
                else text(dispatch.get("ticket")) or text(batch.get("ticket"), "?"),
                role=report.role if report else text(dispatch.get("role"), "?"),
                state=text(statuses.get(dispatch_id, {}).get("state"))
                or text(entries.get(dispatch_id, {}).get("state"), "неизвестно"),
                at=report.reported_at
                if report
                else text(dispatch.get("created_at"))
                or _first_write_at(view, f"dispatches/{dispatch_id}.json"),
                report=report,
            )
        )
    view.stages[:] = select_stages(view.stages)


def select_stages(
    stages: list[StageEntry],
    *,
    ticket: str = "",
    role: str = "",
    outcome: str = "",
    date: str = "",
    state: str = "",
    completion: str = "all",
    sort_by: str = "time",
    descending: bool = True,
) -> list[StageEntry]:
    """Apply combined filters and stable ordering; unknown times always go last."""
    ticket, role, state = (value.strip().lower() for value in (ticket, role, state))
    outcome, date = outcome.strip().lower(), date.strip()
    shown = [
        entry
        for entry in stages
        if (not ticket or ticket in entry.ticket.lower())
        and (not role or role in entry.role.lower())
        and (not state or state == entry.state.lower())
        and (
            not outcome
            or entry.report is not None
            and outcome == entry.report.outcome.lower()
        )
        and (not date or entry.at.startswith(date))
        and (completion != "open" or not entry.finished)
        and (completion != "finished" or entry.finished)
    ]
    shown.sort(key=lambda entry: entry.dispatch_id)
    if sort_by == "time":
        known = [entry for entry in shown if timestamp(entry.at) is not None]
        unknown = [entry for entry in shown if timestamp(entry.at) is None]
        known.sort(
            key=lambda entry: timestamp(entry.at) or datetime.min.replace(tzinfo=UTC),
            reverse=descending,
        )
        return known + unknown
    if sort_by == "ticket":

        def ticket_key(entry: StageEntry) -> tuple[int, int, str]:
            match = re.fullmatch(r"#?(\d+)", entry.ticket)
            return (0, int(match[1]), "") if match else (1, 0, entry.ticket.lower())

        shown.sort(key=ticket_key, reverse=descending)
    else:
        shown.sort(
            key=lambda entry: (
                entry.role if sort_by == "role" else entry.state
            ).lower(),
            reverse=descending,
        )
    return shown


def _load_qa(view: LedgerView, root: Path) -> None:
    """Загружает логи запусков QA gate и записи неудавшихся попыток qa-lane."""
    reports_by_sha = {
        match.group(1): entry
        for entry in view.reports
        if entry.role == "qa"
        and (match := _QA_SHA256.search(text(entry.report.get("output")))) is not None
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
                commands=parse_command_log(lines),
                tail=lines[-QA_LOG_TAIL_LINES:],
                total_lines=len(lines),
            )
        )
    view.qa_runs.sort(key=lambda run: run.logged_at, reverse=True)
    for record in _read_directory(root, "qa-lane/attempts"):
        view.qa_attempts.append(
            QaAttempt(
                dispatch_id=text(record.get("dispatch_id"), "?"),
                stage=text(record.get("stage"), "?"),
                failed_at=text(record.get("failed_at")),
                message=text(record.get("message")),
            )
        )
    view.qa_attempts.sort(key=lambda attempt: attempt.failed_at, reverse=True)


def qa_run_text(run: QaRun) -> str:
    """Формирует текстовое резюме запуска QA gate (вердикт, список команд и хвост лога)."""
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
    """Формирует текстовое описание списка неудавшихся попыток qa-lane."""
    return "\n".join(
        [f"попытки qa-lane: {len(attempts)}"]
        + [
            f"  {attempt.failed_at or 'время ?'} · {attempt.dispatch_id} · "
            f"{attempt.stage}: {attempt.message}"
            for attempt in attempts
        ]
    )


def qa_log_text(view: LedgerView, dispatch_id: str) -> str:
    """Формирует полный текст логов QA для указанного dispatch_id."""
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
    """Фильтрует список отчётов по тикету, роли, итогу (outcome) и префиксу даты."""
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
    """Формирует текстовое описание запущенных проверок из тела отчёта."""
    checks = _objects(report.get("checks_run"))
    if not checks:
        return "проверки не запускались"
    return "\n".join(
        f"• {text(check.get('command'), '?')} — {text(check.get('result'), '?')}: "
        f"{text(check.get('evidence'))}"
        for check in checks
    )


def _review_text(review: JsonObject) -> str:
    """Формирует текстовое описание результатов ревью кандидатов из отчёта."""
    lines = [f"candidate: {text(review.get('candidate_commit'), '?')}"]
    for axis in ("standards", "spec"):
        evidence = review.get(axis)
        if not isinstance(evidence, dict):
            continue
        findings = _objects(evidence.get("findings"))
        lines.append(
            f"{axis}: {text(evidence.get('severity'), '?')}, находок: {len(findings)}"
        )
        lines.extend(
            f"  [{text(item.get('severity'), '?')}] {text(item.get('summary'))}: "
            f"{text(item.get('evidence'))}"
            for item in findings
        )
    return "\n".join(lines)


def _evidence_summary(value: JsonValue) -> str:
    if evidence_is_none(value):
        return "none"
    if isinstance(value, dict) and value.get("status") == "present":
        return text(value.get("details"))
    return ""


def report_sections(report: JsonObject) -> list[tuple[str, str]]:
    """Формирует список именованных секций отчёта в порядке чтения."""
    sections = [
        ("Output", text(report.get("output"))),
        ("Checks", _checks_text(report)),
    ]
    review = report.get("review")
    if isinstance(review, dict):
        sections.append(("Review", _review_text(review)))
    sections.extend(
        [
            ("Risks", _evidence_summary(report.get("risks"))),
            ("Blockers", _evidence_summary(report.get("blockers"))),
            ("Next action", text(report.get("next_coordinator_action"))),
        ]
    )
    return sections


def report_header(entry: ReportEntry) -> str:
    """Формирует сводный заголовок для экрана просмотра отчёта о завершении."""
    files = ", ".join(strings(entry.report.get("changed_files")))
    return (
        f"{entry.ticket} · {entry.role} · {entry.outcome} · {entry.reported_at or 'дата неизвестна'}\n"
        f"dispatch: {entry.dispatch_id}  batch: {entry.batch_id or '?'}\n"
        f"commit: {text(entry.report.get('commit_sha'), '—')}  файлы: {files or '—'}"
    )


def _build_timeline(view: LedgerView, batch: JsonObject) -> BatchTimeline:
    """Собирает структурированную хронологию событий батча из записей леджера."""
    batch_id = cast(str, batch["batch_id"])
    summary = next(item for item in view.batches if item.batch_id == batch_id)
    events: list[TimelineEvent] = [
        TimelineEvent(at=summary.created_at, kind="state", text="батч создан")
    ]
    for audit in view._audit.get(f"batches/{batch_id}.json", []):
        details = audit.get("details")
        if audit.get("action") != "transition" or not isinstance(details, dict):
            continue
        before, after = text(details.get("from")), text(details.get("to"))
        if before and after and before != after:
            events.append(
                TimelineEvent(
                    at=text(audit.get("at")),
                    kind="state",
                    text=f"состояние: {before} → {after}",
                )
            )

    role_flow: list[str] = []
    report_times: dict[str, str] = {}
    for entry in _objects(batch.get("dispatches")):
        dispatch_id = text(entry.get("dispatch_id"))
        dispatch = view._dispatches.get(dispatch_id, {})
        role = text(entry.get("role")) or text(dispatch.get("role"), "?")
        role_flow.append(role)
        purpose = text(dispatch.get("purpose"))
        events.append(
            TimelineEvent(
                at=text(dispatch.get("created_at")),
                kind="dispatch",
                role=role,
                text=f"{role}{f' ({purpose})' if purpose and purpose != 'work' else ''}: "
                f"{dispatch_id}, итог: {text(entry.get('state'), '?')}",
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
        parts = [f"решение coordinator: {text(decision.get('decision'), '?')}"]
        next_role = text(decision.get("next_role"))
        if next_role:
            parts.append(f"→ {next_role}")
        approved_by = text(decision.get("approved_by"))
        if approved_by:
            parts.append(f"({approved_by})")
        note = text(decision.get("note"))
        if note and note != "none":
            parts.append(f"— {note}")
        events.append(
            TimelineEvent(
                at=text(decision.get("approved_at")),
                kind="decision",
                text=" ".join(parts),
            )
        )

    for assessment in _objects(batch.get("risk_assessments")):
        risk = view._risks.get(text(assessment.get("risk_assessment_id")), {})
        triggers = strings(assessment.get("matched_triggers"))
        review = (
            "нужен review" if assessment.get("review_required") else "review не нужен"
        )
        events.append(
            TimelineEvent(
                at=text(risk.get("created_at")),
                kind="risk",
                text=f"оценка риска: {', '.join(triggers) or 'триггеров нет'}; {review}",
            )
        )
    for escalation in _objects(batch.get("risk_escalations")):
        triggers = strings(escalation.get("triggers"))
        events.append(
            TimelineEvent(
                at=report_times.get(text(escalation.get("dispatch_id")), ""),
                kind="risk",
                role="developer",
                text=f"эскалация риска: {', '.join(triggers)}",
            )
        )

    # Stable sort: events with no recorded time keep their order and go last.
    events.sort(key=lambda event: (event.at == "", event.at))
    return BatchTimeline(batch=summary, role_flow=role_flow, events=events)


def role_flow_text(timeline: BatchTimeline) -> str:
    """Формирует строку последовательности перехода между ролями через стрелки."""
    return _ROLE_FLOW_SEPARATOR.join(timeline.role_flow) or "диспатчей ещё не было"


def _cell(value: str) -> str:
    """Экранирует специальные символы для ячейки таблицы Markdown."""
    return value.replace("|", "\\|").replace("\n", " ")


def report_document(view: LedgerView, entry: ReportEntry) -> MarkdownDocument:
    """Формирует структурированный MarkdownDocument для экспорта отчёта о завершении."""
    report = entry.report
    checks = _objects(report.get("checks_run"))
    sections: list[MarkdownSection] = []
    for title, body in report_sections(report):
        if title == "Checks" and checks:
            body = "\n".join(
                f"- `{text(check.get('command'), '?')}` — {text(check.get('result'), '?')}: "
                f"{text(check.get('evidence'))}"
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
            ("Commit", text(report.get("commit_sha"), "—")),
            ("Changed files", ", ".join(strings(report.get("changed_files"))) or "—"),
        ],
        sections=sections,
    )


def timeline_document(timeline: BatchTimeline) -> MarkdownDocument:
    """Формирует структурированный MarkdownDocument для экспорта хронологии батча."""
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
            ("Начало", batch_start_text(batch)),
            ("До PR", batch_timing_text(batch)),
            ("PR", batch.pr_url or "неизвестно"),
            ("Маршрут", role_flow_text(timeline)),
        ],
        sections=[MarkdownSection("События", "\n".join(rows))],
    )
