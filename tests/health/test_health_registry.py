"""Тесты реестра проверок диагностической подсистемы harness health."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.health import registry
from harness.health.context import HealthContext
from harness.health.model import CheckResult, Report


def test_run_with_empty_registry_returns_a_schema_valid_empty_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Проверить, что запуск с пустым реестром возвращает валидный пустой отчет."""
    monkeypatch.setattr(registry, "REGISTRY", [])
    report = registry.run(tmp_path)

    assert isinstance(report, Report)
    assert report.schema_version == 1
    assert report.repo == str(tmp_path)
    assert report.online is False
    assert report.checks == []
    assert report.fixes_applied == []
    assert report.summary() == {"ok": 0, "warn": 0, "fail": 0, "skipped": 0}


def test_run_wires_every_registered_group(tmp_path: Path) -> None:
    """Проверить, что запуск охватывает все зарегистрированные группы проверок."""
    report = registry.run(tmp_path)

    groups = {check.group for check in report.checks}
    assert groups == {
        "files",
        "directories",
        "repo_map",
        "environment",
        "tracker",
        "orchestration",
    }


def test_run_passes_online_through_to_the_report(tmp_path: Path) -> None:
    """Проверить, что параметр online передается в результирующий отчет."""
    report = registry.run(tmp_path, online=True)

    assert report.online is True


def test_run_without_a_lock_file_builds_a_context_with_no_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Проверить, что при отсутствии lock-файла контекст формируется со значением lock=None."""
    captured: list[HealthContext] = []

    def _capture(context: HealthContext) -> CheckResult:
        """Зафиксировать контекст проверки и вернуть тестовый результат."""
        captured.append(context)
        return CheckResult(id="test.probe", group="test", status="ok", message="ok")

    monkeypatch.setattr(registry, "REGISTRY", [("test.probe", _capture)])
    report = registry.run(tmp_path)

    assert captured == [HealthContext(repo=tmp_path, lock=None, online=False)]
    assert [check.id for check in report.checks] == ["test.probe"]


def test_run_parses_an_existing_lock_file_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Проверить, что существующий lock-файл парсится ровно один раз за запуск."""
    lock_path = tmp_path / ".harness" / "harness.lock"
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text(
        json.dumps({"capabilities": ["pvmalove-suite"]}), encoding="utf-8"
    )
    captured: list[HealthContext] = []

    def _capture(context: HealthContext) -> CheckResult:
        """Зафиксировать контекст проверки и вернуть тестовый результат."""
        captured.append(context)
        return CheckResult(id="test.probe", group="test", status="ok", message="ok")

    monkeypatch.setattr(registry, "REGISTRY", [("test.probe", _capture)])
    registry.run(tmp_path)

    assert captured[0].lock == {"capabilities": ["pvmalove-suite"]}


def _ok(check_id: str) -> registry.CheckFn:
    """Создать функцию проверки, возвращающую статус ok."""

    def _check(_context: HealthContext) -> CheckResult:
        """Вернуть результат проверки со статусом ok."""
        return CheckResult(id=check_id, group="test", status="ok", message="ok")

    return _check


@pytest.mark.parametrize(
    "error", [RuntimeError("boom"), FileNotFoundError("git"), SystemExit(1)]
)
def test_a_crashing_check_becomes_a_fail_and_the_run_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    """Проверить, что исключение в проверке превращается в fail, а запуск продолжается."""

    def check_crashes(_context: HealthContext) -> CheckResult:
        """Имитировать аварийное завершение проверки."""
        raise error

    monkeypatch.setattr(
        registry,
        "REGISTRY",
        [
            ("test.before", _ok("test.before")),
            ("test.crashes", check_crashes),
            ("test.after", _ok("test.after")),
        ],
    )
    report = registry.run(tmp_path)

    assert [check.id for check in report.checks] == [
        "test.before",
        "test.crashes",
        "test.after",
    ]
    crashed = report.checks[1]
    assert crashed.group == "test"
    assert crashed.status == "fail"
    assert "check_crashes" in crashed.message
    assert type(error).__name__ in crashed.message


def test_keyboard_interrupt_is_not_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Проверить, что прерывание KeyboardInterrupt не подавляется реестром."""

    def _interrupted(_context: HealthContext) -> CheckResult:
        """Вызвать исключение KeyboardInterrupt."""
        raise KeyboardInterrupt

    monkeypatch.setattr(registry, "REGISTRY", [("test.interrupted", _interrupted)])

    with pytest.raises(KeyboardInterrupt):
        registry.run(tmp_path)


def test_every_registered_id_matches_the_id_its_check_returns(tmp_path: Path) -> None:
    """Проверить, что каждый зарегистрированный идентификатор совпадает с id результата проверки."""
    report = registry.run(tmp_path)

    assert [check.id for check in report.checks] == [
        check_id for check_id, _ in registry.REGISTRY
    ]


def test_summary_counts_every_status(tmp_path: Path) -> None:
    """Проверить, что метод summary корректно подсчитывает каждый статус проверок."""
    report = Report(schema_version=1, repo=str(tmp_path), online=False)
    report.checks = [
        CheckResult(id="a", group="g", status="ok", message="m"),
        CheckResult(id="b", group="g", status="ok", message="m"),
        CheckResult(id="c", group="g", status="warn", message="m"),
        CheckResult(id="d", group="g", status="fail", message="m"),
        CheckResult(id="e", group="g", status="skipped", message="m"),
    ]

    assert report.summary() == {"ok": 2, "warn": 1, "fail": 1, "skipped": 1}


def test_fix_actions_run_only_with_fix_and_recheck_the_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Проверить, что действия исправления выполняются только при флаге fix с повторной проверкой."""
    fixed: list[bool] = []

    def check(_context: HealthContext) -> CheckResult:
        """Тестовая проверка с динамическим статусом в зависимости от вызова фиксера."""
        return CheckResult(
            id="test.fixable",
            group="test",
            status="ok" if fixed else "fail",
            message="m",
        )

    def fix(_context: HealthContext, result: CheckResult) -> str | None:
        """Тестовое действие по исправлению статуса проверки."""
        assert result.status == "fail"
        fixed.append(True)
        return "починено"

    monkeypatch.setattr(registry, "REGISTRY", [("test.fixable", check)])
    monkeypatch.setattr(registry, "FIXERS", {"test.fixable": fix})

    assert registry.run(tmp_path).fixes_applied == []
    assert fixed == []

    report = registry.run(tmp_path, fix=True)

    assert report.fixes_applied == ["починено"]
    assert [check.status for check in report.checks] == ["ok"]


def test_a_crashing_fix_action_keeps_the_check_result_and_the_run_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Проверить, что исключение в действии исправления не прерывает работу реестра."""

    def fix_crashes(_context: HealthContext, _result: CheckResult) -> str | None:
        """Имитировать ошибку при выполнении исправления."""
        raise OSError("read-only")

    monkeypatch.setattr(
        registry,
        "REGISTRY",
        [("test.before", _ok("test.before")), ("test.after", _ok("test.after"))],
    )
    monkeypatch.setattr(registry, "FIXERS", {"test.before": fix_crashes})

    report = registry.run(tmp_path, fix=True)

    assert [check.id for check in report.checks] == ["test.before", "test.after"]
    assert report.fixes_applied == []
