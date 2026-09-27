"""Diagnostics screen: the full `harness health` report plus two actions - "online checks"
(re-runs the same public `health.registry.run(..., online=True)` call `cmd_health` makes) and
"apply fixes" (runs each check's remedy shell command through the injected CommandRunner, then
applies the in-process `FIXERS` from `harness health --fix` (#399) via `apply_local_fixes`, after
an explicit confirmation press - some fixes are destructive, e.g. removing files or touching the
registry on Windows)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Static

from .. import data as console_data
from ..runner import CommandRunner, default_runner
from ...health.model import Report

_STATUS_MARKERS = {"ok": "[OK]", "warn": "[WARN]", "fail": "[FAIL]", "skipped": "-"}
_APPLY_LABEL = "Apply fixes"
_CONFIRM_LABEL = "Подтвердить применение фиксов"


def _render_report(report: Report, *, failed_fixes: list[tuple[str, str]] | None = None) -> str:
    summary = report.summary()
    lines = [
        f"Итого: ok={summary['ok']} warn={summary['warn']} "
        f"fail={summary['fail']} skipped={summary['skipped']}",
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
    if failed_fixes:
        lines.append("")
        lines.append("Не удалось применить:")
        for command, stderr_tail in failed_fixes:
            lines.append(f"  - {command}")
            if stderr_tail:
                lines.append(f"     {stderr_tail}")
    return "\n".join(lines)


class _CollectDiagnostics(Protocol):
    def __call__(self, repo: Path, *, online: bool = False) -> Report: ...


class _ApplyLocalFixes(Protocol):
    def __call__(self, repo: Path, *, online: bool = False) -> Report: ...


def _shell_argv(command: str) -> list[str]:
    if os.name == "nt":
        return ["powershell", "-Command", command]
    return ["/bin/sh", "-c", command]


class DiagnosticsScreen(Screen[None]):
    def __init__(
        self,
        repo: Path,
        *,
        collect_diagnostics: _CollectDiagnostics = console_data.collect_diagnostics,
        apply_local_fixes: _ApplyLocalFixes = console_data.apply_local_fixes,
        command_runner: CommandRunner = default_runner,
    ) -> None:
        super().__init__()
        self.repo = repo
        self._collect_diagnostics = collect_diagnostics
        self._apply_local_fixes = apply_local_fixes
        self._command_runner = command_runner
        self._report: Report = self._collect_diagnostics(repo)
        self._confirming_apply = False
        self._failed_fixes: list[tuple[str, str]] = []

    def compose(self) -> ComposeResult:
        yield Header()
        yield VerticalScroll(
            Static(
                _render_report(self._report, failed_fixes=self._failed_fixes),
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
            self._failed_fixes = []
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
        shell_fixes: list[str] = []
        failed_fixes: list[tuple[str, str]] = []
        for check in self._report.checks:
            fix = check.fix
            if fix is None or fix.command is None:
                continue
            result = self._command_runner(_shell_argv(fix.command))
            if result.returncode == 0:
                shell_fixes.append(fix.command)
            else:
                stderr_tail = "\n".join((result.stderr or "").strip().splitlines()[-3:])
                failed_fixes.append((fix.command, stderr_tail))
        self._report = self._apply_local_fixes(self.repo, online=self._report.online)
        self._report.fixes_applied = shell_fixes + self._report.fixes_applied
        self._failed_fixes = failed_fixes
        self._refresh_report()

    def _apply_button(self) -> Button:
        return self.query_one("#apply-fixes", Button)

    def _refresh_report(self) -> None:
        self.query_one("#diagnostics-report", Static).update(
            _render_report(self._report, failed_fixes=self._failed_fixes)
        )
