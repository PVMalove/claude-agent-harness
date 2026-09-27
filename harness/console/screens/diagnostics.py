"""Diagnostics screen: the full `harness health` report plus two actions - "online checks"
(re-runs the same public `health.registry.run(..., online=True)` call `cmd_health` makes) and
"apply fixes" (the same `health.registry.run(..., fix=True)` call `harness health --fix` makes,
#399: only the in-process `FIXERS` - missing `.harness` directories, a stale skill registry and,
after online checks, missing tracker labels - after an explicit confirmation press).

A check's `fix.command` is a remedy for the developer to run by hand and is only shown here, never
executed: those commands change machine settings (Windows registry, Developer Mode, global git
config, PATH) or delete data, which epic #341 keeps out of both `--fix` and the console."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Static

from .. import data as console_data
from ...health.model import Report

_STATUS_MARKERS = {"ok": "[OK]", "warn": "[WARN]", "fail": "[FAIL]", "skipped": "-"}
_APPLY_LABEL = "Apply fixes"
_CONFIRM_LABEL = "Подтвердить применение фиксов"
_MANUAL_NOTE = "Команды «Как исправить» пульт не выполняет — запустите их вручную."


def _render_report(report: Report) -> str:
    summary = report.summary()
    lines = [
        f"Итого: ok={summary['ok']} warn={summary['warn']} "
        f"fail={summary['fail']} skipped={summary['skipped']}",
        _MANUAL_NOTE,
        "",
    ]
    for check in report.checks:
        lines.append(f"{_STATUS_MARKERS[check.status]} {check.id}: {check.message}")
        if check.fix is not None:
            lines.append(f"  -> Как исправить: {check.fix.text}")
            if check.fix.command:
                lines.append(f"     {check.fix.command}")
    if report.fixes_applied:
        lines.append("")
        lines.append("Применённые фиксы:")
        lines.extend(f"  - {command}" for command in report.fixes_applied)
    return "\n".join(lines)


class _CollectDiagnostics(Protocol):
    def __call__(self, repo: Path, *, online: bool = False) -> Report: ...


class _ApplyLocalFixes(Protocol):
    def __call__(self, repo: Path, *, online: bool = False) -> Report: ...


class DiagnosticsScreen(Screen[None]):
    def __init__(
        self,
        repo: Path,
        *,
        collect_diagnostics: _CollectDiagnostics = console_data.collect_diagnostics,
        apply_local_fixes: _ApplyLocalFixes = console_data.apply_local_fixes,
    ) -> None:
        super().__init__()
        self.repo = repo
        self._collect_diagnostics = collect_diagnostics
        self._apply_local_fixes = apply_local_fixes
        self._report: Report = self._collect_diagnostics(repo)
        self._confirming_apply = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield VerticalScroll(
            Static(
                _render_report(self._report),
                id="diagnostics-report",
            )
        )
        yield Button("Online checks", id="online-checks")
        yield Button(_APPLY_LABEL, id="apply-fixes")
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "online-checks":
            self._report = self._collect_diagnostics(self.repo, online=True)
            self._confirming_apply = False
            self._apply_button().label = _APPLY_LABEL
            self._refresh_report()
        elif event.button.id == "apply-fixes":
            self._on_apply_fixes_pressed()

    def _on_apply_fixes_pressed(self) -> None:
        if not self._confirming_apply:
            self._confirming_apply = True
            self._apply_button().label = _CONFIRM_LABEL
            return
        self._confirming_apply = False
        self._apply_button().label = _APPLY_LABEL
        self._report = self._apply_local_fixes(self.repo, online=self._report.online)
        self._refresh_report()

    def _apply_button(self) -> Button:
        return self.query_one("#apply-fixes", Button)

    def _refresh_report(self) -> None:
        self.query_one("#diagnostics-report", Static).update(
            _render_report(self._report)
        )
