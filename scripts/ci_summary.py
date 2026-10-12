#!/usr/bin/env python3
"""Сводка CI для страницы запуска GitHub Actions: результаты pytest и общий дашборд.

`tests` превращает JUnit XML одного задания в Markdown: итоги, упавшие тесты с первой строкой причины
и самые медленные тесты. `dashboard` собирает все отчёты запуска и результаты заданий в одну таблицу:
что прошло, что упало, где именно и сколько это заняло. Скрипт использует только стандартную
библиотеку и пишет Markdown в stdout; workflow добавляет его в `$GITHUB_STEP_SUMMARY`."""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

SLOWEST = 10
MESSAGE_CHARS = 300
ICONS = {
    "success": "✅",
    "failure": "❌",
    "cancelled": "🚫",
    "skipped": "⏭️",
    "passed": "✅",
    "failed": "❌",
}


@dataclass
class TestResult:
    name: str
    seconds: float
    outcome: str  # passed, failed, skipped
    message: str = ""


@dataclass
class Report:
    """The results of one pytest run (one JUnit file)."""

    title: str
    results: list[TestResult] = field(default_factory=list)
    seconds: float = 0.0

    def count(self, outcome: str) -> int:
        return sum(1 for result in self.results if result.outcome == outcome)

    @property
    def failed(self) -> list[TestResult]:
        return [result for result in self.results if result.outcome == "failed"]


def _first_line(text: str) -> str:
    for line in text.strip().splitlines():
        if line.strip():
            return line.strip()[:MESSAGE_CHARS]
    return ""


def read_report(path: Path, title: str | None = None) -> Report:
    """Parse one JUnit XML file; a missing or broken file is an empty report named in `title`."""
    report = Report(title or path.stem.removeprefix("junit-"))
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return report
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    for suite in suites:
        report.seconds += float(suite.get("time") or 0)
        for case in suite.iter("testcase"):
            name = f"{case.get('classname', '')}::{case.get('name', '')}".strip(":")
            seconds = float(case.get("time") or 0)
            problem = case.find("failure")
            if problem is None:
                problem = case.find("error")
            skipped = case.find("skipped")
            if problem is not None:
                text = problem.get("message") or problem.text or ""
                report.results.append(
                    TestResult(name, seconds, "failed", _first_line(text))
                )
            elif skipped is not None:
                report.results.append(TestResult(name, seconds, "skipped"))
            else:
                report.results.append(TestResult(name, seconds, "passed"))
    return report


def _minutes(seconds: float) -> str:
    return (
        f"{int(seconds // 60)} мин {int(seconds % 60):02d} с"
        if seconds >= 60
        else f"{seconds:.0f} с"
    )


def _escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render_tests(report: Report) -> str:
    """Markdown for one pytest run."""
    verdict = "❌ есть падения" if report.failed else "✅ всё прошло"
    lines = [
        f"### {report.title}: {verdict}",
        "",
        "| Тестов | Прошло | Упало | Пропущено | Время тестов (сумма по воркерам) |",
        "|---:|---:|---:|---:|---:|",
        f"| {len(report.results)} | {report.count('passed')} | {report.count('failed')} "
        f"| {report.count('skipped')} | {_minutes(report.seconds)} |",
        "",
    ]
    if not report.results:
        lines += [
            "_Отчёт pytest не найден: прогон остановился до тестов или упал раньше._",
            "",
        ]
    if report.failed:
        lines += ["#### Упавшие тесты", "", "| Тест | Причина |", "|---|---|"]
        lines += [
            f"| `{_escape(result.name)}` | {_escape(result.message) or '—'} |"
            for result in report.failed
        ]
        lines.append("")
    slow = sorted(report.results, key=lambda result: -result.seconds)[:SLOWEST]
    if slow and slow[0].seconds >= 1:
        lines += [
            "<details><summary>Самые медленные тесты</summary>",
            "",
            "| Тест | Время |",
            "|---|---:|",
        ]
        lines += [
            f"| `{_escape(result.name)}` | {result.seconds:.1f} с |" for result in slow
        ]
        lines += ["", "</details>", ""]
    return "\n".join(lines)


def render_dashboard(
    reports: list[Report], jobs: dict[str, str], run_url: str | None = None
) -> str:
    """One page for the whole run: job results, tests per job, every failed test."""
    failed_jobs = [name for name, result in jobs.items() if result == "failure"]
    lines = [
        "## Дашборд проверки",
        "",
        ("❌ **Есть падения:** " + ", ".join(f"`{name}`" for name in failed_jobs))
        if failed_jobs
        else "✅ **Все задания прошли**",
        "",
        "### Задания",
        "",
        "| Задание | Результат |",
        "|---|---|",
    ]
    lines += [
        f"| `{name}` | {ICONS.get(result, '❔')} {result} |"
        for name, result in jobs.items()
    ]
    lines.append("")
    if reports:
        lines += [
            "### Тесты по заданиям",
            "",
            "| Задание | Тестов | Прошло | Упало | Пропущено | Время тестов (сумма по воркерам) |",
            "|---|---:|---:|---:|---:|---:|",
        ]
        for report in reports:
            icon = "❌" if report.failed else "✅"
            lines.append(
                f"| {icon} {report.title} | {len(report.results)} | {report.count('passed')} "
                f"| {report.count('failed')} | {report.count('skipped')} | {_minutes(report.seconds)} |"
            )
        lines.append("")
    failures = [
        (report.title, result) for report in reports for result in report.failed
    ]
    if failures:
        lines += [
            "### Все упавшие тесты",
            "",
            "| Задание | Тест | Причина |",
            "|---|---|---|",
        ]
        lines += [
            f"| {title} | `{_escape(result.name)}` | {_escape(result.message) or '—'} |"
            for title, result in failures
        ]
        lines += [
            "",
            "Повторить локально: `make verify` или `pytest <тест>`; "
            "в CI упавшие тесты запускаются первыми.",
            "",
        ]
    if run_url:
        lines += [f"[Запуск и полные журналы]({run_url})", ""]
    return "\n".join(lines)


def _load_jobs(raw: str) -> dict[str, str]:
    """`needs` of a workflow as JSON ({job: {result: ...}}) -> {job: result}."""
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return {
        str(name): str(value.get("result", "unknown"))
        for name, value in data.items()
        if isinstance(value, dict)
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0] if __doc__ else None
    )
    commands = parser.add_subparsers(dest="command", required=True)
    tests = commands.add_parser("tests", help="Markdown for JUnit XML files")
    tests.add_argument("junit", nargs="+", type=Path)
    tests.add_argument("--title")
    dashboard = commands.add_parser("dashboard", help="one page for the whole run")
    dashboard.add_argument("--junit-dir", type=Path, required=True)
    dashboard.add_argument("--needs-json", default="{}", help="JSON of `needs`")
    dashboard.add_argument("--run-url")
    args = parser.parse_args(argv)
    if args.command == "tests":
        for path in args.junit:
            print(render_tests(read_report(path, args.title)))
        return 0
    reports = [
        read_report(path) for path in sorted(args.junit_dir.rglob("junit-*.xml"))
    ]
    print(render_dashboard(reports, _load_jobs(args.needs_json), args.run_url))
    return 0


if __name__ == "__main__":
    sys.exit(main())
