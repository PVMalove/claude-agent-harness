"""scripts/ci_summary.py: the Markdown the CI run page shows."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import ci_summary

JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="4" time="125.5">
<testcase classname="tests.a" name="test_ok" time="0.5"/>
<testcase classname="tests.a" name="test_slow" time="42.0"/>
<testcase classname="tests.b" name="test_bad" time="1.0">
<failure message="AssertionError: 1 == 2&#10;more">trace</failure></testcase>
<testcase classname="tests.b" name="test_skip" time="0"><skipped message="x"/></testcase>
</testsuite></testsuites>
"""


def _report(tmp_path: Path, name: str = "junit-verify.xml") -> ci_summary.Report:
    path = tmp_path / name
    path.write_text(JUNIT, encoding="utf-8")
    return ci_summary.read_report(path)


def test_a_junit_file_becomes_counts_failures_and_the_slowest_tests(
    tmp_path: Path,
) -> None:
    report = _report(tmp_path)
    assert report.title == "verify"
    assert [report.count(o) for o in ("passed", "failed", "skipped")] == [2, 1, 1]
    text = ci_summary.render_tests(report)
    assert "❌ есть падения" in text
    assert "| 4 | 2 | 1 | 1 | 2 мин 05 с |" in text
    assert "`tests.b::test_bad` | AssertionError: 1 == 2" in text
    assert "`tests.a::test_slow` | 42.0 с" in text


def test_a_missing_report_is_said_not_hidden(tmp_path: Path) -> None:
    report = ci_summary.read_report(tmp_path / "junit-missing.xml")
    assert "Отчёт pytest не найден" in ci_summary.render_tests(report)


def test_the_dashboard_lists_jobs_failures_and_the_way_to_repeat(
    tmp_path: Path,
) -> None:
    jobs = ci_summary._load_jobs(
        '{"verify": {"result": "success"}, "verify-windows": {"result": "failure"}}'
    )
    text = ci_summary.render_dashboard([_report(tmp_path)], jobs, "https://example/run")
    assert "❌ **Есть падения:** `verify-windows`" in text
    assert "| `verify` | ✅ success |" in text
    assert "### Все упавшие тесты" in text and "test_bad" in text
    assert "[Запуск и полные журналы](https://example/run)" in text


def test_a_clean_run_says_so(tmp_path: Path) -> None:
    text = ci_summary.render_dashboard([], {"verify": "success"})
    assert "✅ **Все задания прошли**" in text


def test_the_command_line_writes_markdown(
    tmp_path: Path, capsys: "pytest.CaptureFixture[str]"
) -> None:
    (tmp_path / "junit-windows.xml").write_text(JUNIT, encoding="utf-8")
    assert (
        ci_summary.main(
            ["dashboard", "--junit-dir", str(tmp_path), "--needs-json", "{}"]
        )
        == 0
    )
    assert "windows" in capsys.readouterr().out
    assert ci_summary.main(["tests", str(tmp_path / "junit-windows.xml")]) == 0


def test_the_summary_survives_a_console_that_cannot_encode_cyrillic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import io
    import sys

    path = tmp_path / "junit-windows.xml"
    path.write_text(JUNIT, encoding="utf-8")
    raw = io.BytesIO()
    console = io.TextIOWrapper(raw, encoding="cp1252", write_through=True)
    monkeypatch.setattr(sys, "stdout", console)
    assert ci_summary.main(["tests", str(path), "--title", "Windows: тесты"]) == 0
    assert "Windows: тесты" in raw.getvalue().decode("utf-8")
