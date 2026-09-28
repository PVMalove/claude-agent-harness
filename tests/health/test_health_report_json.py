"""Тесты сериализации отчета диагностики в формат JSON."""

from __future__ import annotations

from harness.health.model import CheckResult, Fix, Report
from harness.health.report_json import to_json


def test_to_json_top_level_shape() -> None:
    """Проверить, что сериализация отчета формирует корректную структуру верхнего уровня."""
    report = Report(schema_version=1, repo="/repo/path", online=False)
    report.checks = [
        CheckResult(
            id="files.lock",
            group="files",
            status="ok",
            message="harness.lock присутствует",
        ),
    ]

    data = to_json(report)

    assert data == {
        "schema_version": 1,
        "repo": "/repo/path",
        "online": False,
        "summary": {"ok": 1, "warn": 0, "fail": 0, "skipped": 0},
        "checks": [
            {
                "id": "files.lock",
                "group": "files",
                "status": "ok",
                "message": "harness.lock присутствует",
                "fix": None,
            }
        ],
        "fixes_applied": [],
    }


def test_to_json_serializes_a_fix() -> None:
    """Проверить, что объект исправления корректно сериализуется в JSON."""
    report = Report(schema_version=1, repo="/repo", online=True)
    report.checks = [
        CheckResult(
            id="files.orchestration_config",
            group="files",
            status="fail",
            message="broken",
            fix=Fix(text="почините конфиг", command="harness registry ."),
        ),
    ]

    data = to_json(report)

    assert data["checks"][0]["fix"] == {
        "text": "почините конфиг",
        "command": "harness registry .",
    }
    assert data["online"] is True


def test_to_json_fix_command_defaults_to_none() -> None:
    """Проверить, что поле команды исправления по умолчанию сериализуется как None."""
    report = Report(schema_version=1, repo="/repo", online=False)
    report.checks = [
        CheckResult(
            id="files.a",
            group="files",
            status="warn",
            message="m",
            fix=Fix(text="fix text"),
        ),
    ]

    data = to_json(report)

    assert data["checks"][0]["fix"] == {"text": "fix text", "command": None}


def test_to_json_includes_fixes_applied() -> None:
    """Проверить, что список примененных исправлений включается в JSON-отчет."""
    report = Report(schema_version=1, repo="/repo", online=False)
    report.fixes_applied = ["files.skill_registry"]

    data = to_json(report)

    assert data["fixes_applied"] == ["files.skill_registry"]


def test_to_json_summary_counts_every_status() -> None:
    """Проверить, что сводка в JSON-отчете корректно подсчитывает каждый статус проверок."""
    report = Report(schema_version=1, repo="/repo", online=False)
    report.checks = [
        CheckResult(id="a", group="g", status="ok", message="m"),
        CheckResult(id="b", group="g", status="warn", message="m"),
        CheckResult(id="c", group="g", status="fail", message="m"),
        CheckResult(id="d", group="g", status="skipped", message="m"),
    ]

    data = to_json(report)

    assert data["summary"] == {"ok": 1, "warn": 1, "fail": 1, "skipped": 1}
