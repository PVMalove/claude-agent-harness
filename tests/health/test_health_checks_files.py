"""Тесты проверок состояния файлов проекта группы 'files'."""

from __future__ import annotations

import json
from pathlib import Path

from harness.health.checks import files as checks
from harness.health.context import HealthContext

_NO_CAPABILITY_LOCK: dict[str, object] = {"capabilities": []}
_ORCHESTRATION_LOCK: dict[str, object] = {"capabilities": ["backend-orchestration"]}


def _context(repo: Path, lock: dict[str, object] | None = None) -> HealthContext:
    """Создать контекст проверки с заданным lock-файлом."""
    return HealthContext(repo=repo, lock=lock, online=False)


def test_check_lock_fails_without_harness_lock(tmp_path: Path) -> None:
    """Проверить, что проверка файла lock завершается ошибкой при его отсутствии."""
    result = checks.check_lock(_context(tmp_path))

    assert result.id == "files.lock"
    assert result.group == "files"
    assert result.status == "fail"


def test_check_lock_ok_with_harness_lock(tmp_path: Path) -> None:
    """Проверить, что проверка файла lock возвращает ok при его наличии."""
    result = checks.check_lock(_context(tmp_path, lock={}))

    assert result.status == "ok"


def test_check_agents_md_fails_when_missing(tmp_path: Path) -> None:
    """Проверить, что отсутствие AGENTS.md приводит к ошибке."""
    result = checks.check_agents_md(_context(tmp_path))

    assert result.id == "files.agents_md"
    assert result.status == "fail"
    assert "отсутствует AGENTS.md" in result.message
    # Only `harness init` writes AGENTS.md and it refuses once a lock exists, so the remedy restores
    # the file itself instead of pointing at `harness update`, which never writes it.
    assert result.fix is not None
    assert result.fix.command == "git checkout -- AGENTS.md"


def test_check_agents_md_fails_on_unresolved_template_markers(tmp_path: Path) -> None:
    """Проверить, что наличие неразрешенных маркеров шаблона в AGENTS.md приводит к ошибке."""
    (tmp_path / "AGENTS.md").write_text("# {{project_name}}\n", encoding="utf-8")

    result = checks.check_agents_md(_context(tmp_path))

    assert result.status == "fail"
    assert "нерешённые плейсхолдеры" in result.message


def test_check_agents_md_ok_when_resolved(tmp_path: Path) -> None:
    """Проверить, что корректно заполненный AGENTS.md возвращает статус ok."""
    (tmp_path / "AGENTS.md").write_text("# My Project\n", encoding="utf-8")

    result = checks.check_agents_md(_context(tmp_path))

    assert result.status == "ok"


def test_check_discovery_links_fails_when_absent(tmp_path: Path) -> None:
    """Проверить, что отсутствие discovery-ссылок приводит к ошибке."""
    result = checks.check_discovery_links(_context(tmp_path))

    assert result.id == "files.discovery_links"
    assert result.status == "fail"
    assert "неисправна discovery-ссылка" in result.message
    # `harness update` refuses to replace an existing wrong link, so the remedy says to remove it.
    assert result.fix is not None
    assert "удалите" in result.fix.text


def test_check_project_json_ok_when_absent(tmp_path: Path) -> None:
    """Проверить, что отсутствие опционального project.json возвращает статус ok."""
    result = checks.check_project_json(_context(tmp_path))

    assert result.id == "files.project_json"
    assert result.status == "ok"
    assert "отсутствует" in result.message


def test_check_project_json_fails_on_invalid_shape(tmp_path: Path) -> None:
    """Проверить, что некорректная структура project.json приводит к ошибке."""
    project_json = tmp_path / ".harness" / "project.json"
    project_json.parent.mkdir(parents=True)
    project_json.write_text(json.dumps({"language": "de"}), encoding="utf-8")

    result = checks.check_project_json(_context(tmp_path))

    assert result.status == "fail"


def test_check_project_json_ok_when_valid(tmp_path: Path) -> None:
    """Проверить, что валидный project.json возвращает статус ok."""
    project_json = tmp_path / ".harness" / "project.json"
    project_json.parent.mkdir(parents=True)
    project_json.write_text(
        json.dumps(
            {
                "language": "ru",
                "base_branch": "main",
                "branch_pattern": "^feature/.+",
                "qa_gate_commands": ["echo test"],
            }
        ),
        encoding="utf-8",
    )

    result = checks.check_project_json(_context(tmp_path))

    assert result.status == "ok"
    assert "корректен" in result.message


def test_check_orchestration_config_skipped_without_capability(tmp_path: Path) -> None:
    """Проверить, что конфигурация оркестрации пропускается без соответствующей возможности."""
    result = checks.check_orchestration_config(_context(tmp_path))
    assert result.status == "skipped"

    result = checks.check_orchestration_config(
        _context(tmp_path, lock=_NO_CAPABILITY_LOCK)
    )
    assert result.status == "skipped"


def test_check_orchestration_config_fails_without_schema_file(tmp_path: Path) -> None:
    """Проверить, что отсутствие файла схемы конфигурации оркестрации приводит к ошибке."""
    result = checks.check_orchestration_config(
        _context(tmp_path, lock=_ORCHESTRATION_LOCK)
    )

    assert result.id == "files.orchestration_config"
    assert result.status == "fail"


def test_check_skill_snapshot_skipped_without_lock(tmp_path: Path) -> None:
    """Проверить, что проверка снепшота навыков пропускается при отсутствии lock-файла."""
    result = checks.check_skill_snapshot(_context(tmp_path))

    assert result.id == "files.skill_snapshot"
    assert result.status == "skipped"


def test_check_skill_snapshot_skipped_without_injected_snapshot_diff(
    tmp_path: Path,
) -> None:
    """Проверить, что проверка снепшота навыков пропускается без функции snapshot_diff."""
    result = checks.check_skill_snapshot(_context(tmp_path, lock={}))

    assert result.id == "files.skill_snapshot"
    assert result.status == "skipped"
    assert "харнесс-пакетировщика" in result.message


def test_check_skill_snapshot_uses_the_injected_snapshot_diff(tmp_path: Path) -> None:
    """Проверить, что проверка снепшота навыков использует переданную функцию snapshot_diff."""
    context = HealthContext(
        repo=tmp_path,
        lock={},
        online=False,
        snapshot_diff=lambda _repo: {"state": "drift"},
    )

    result = checks.check_skill_snapshot(context)

    assert result.status == "fail"

    context = HealthContext(
        repo=tmp_path,
        lock={},
        online=False,
        snapshot_diff=lambda _repo: {"state": "clean"},
    )

    result = checks.check_skill_snapshot(context)

    assert result.status == "ok"


def test_check_skill_registry_skipped_without_lock(tmp_path: Path) -> None:
    """Проверить, что проверка реестра навыков пропускается без lock-файла."""
    result = checks.check_skill_registry(_context(tmp_path))

    assert result.id == "files.skill_registry"
    assert result.status == "skipped"


def test_check_skill_registry_fails_on_missing_skill_root(tmp_path: Path) -> None:
    """Проверить, что отсутствие корневого каталога навыков приводит к ошибке реестра."""
    result = checks.check_skill_registry(_context(tmp_path, lock={}))

    assert result.status == "fail"


def test_check_overlay_locks_skipped_without_lock(tmp_path: Path) -> None:
    """Проверить, что проверка overlay-lock файлов пропускается без lock-файла."""
    result = checks.check_overlay_locks(_context(tmp_path))

    assert result.id == "files.overlay_locks"
    assert result.status == "skipped"


def test_check_overlay_locks_fails_on_missing_skill_root(tmp_path: Path) -> None:
    """Проверить, что отсутствие каталога навыков приводит к ошибке проверки оверлеев."""
    result = checks.check_overlay_locks(_context(tmp_path, lock={}))

    assert result.status == "fail"


def test_check_integrations_skipped_without_lock(tmp_path: Path) -> None:
    """Проверить, что проверка интеграций пропускается без lock-файла."""
    result = checks.check_integrations(_context(tmp_path))

    assert result.id == "files.integrations"
    assert result.status == "skipped"


def test_check_integrations_ok_with_no_integrations(tmp_path: Path) -> None:
    """Проверить, что отсутствие интеграций при наличии lock-файла возвращает статус ok."""
    result = checks.check_integrations(_context(tmp_path, lock={}))

    assert result.status == "ok"


def test_check_verification_routing_skipped_without_capability(tmp_path: Path) -> None:
    """Проверить, что маршрутизация верификации пропускается без возможности оркестрации."""
    result = checks.check_verification_routing(_context(tmp_path))

    assert result.id == "files.verification_routing"
    assert result.status == "skipped"


def test_check_verification_routing_skipped_without_config_file(tmp_path: Path) -> None:
    """Проверить, что маршрутизация верификации пропускается при отсутствии orchestration.json."""
    result = checks.check_verification_routing(
        _context(tmp_path, lock=_ORCHESTRATION_LOCK)
    )

    assert result.status == "skipped"
    assert "orchestration.json" in result.message


def test_check_verification_routing_ok_when_developer_commands_set(
    tmp_path: Path,
) -> None:
    """Проверить, что маршрутизация верификации возвращает ok при заданных командах разработчика."""
    config = tmp_path / ".harness" / "orchestration.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps(
            {
                "verification_commands": ["python scripts/verify.py"],
                "developer_verification_commands": ["python -m pytest tests/unit"],
            }
        ),
        encoding="utf-8",
    )

    result = checks.check_verification_routing(
        _context(tmp_path, lock=_ORCHESTRATION_LOCK)
    )

    assert result.status == "ok"


def test_check_verification_routing_warns_without_developer_commands(
    tmp_path: Path,
) -> None:
    """Проверить, что маршрутизация верификации выдает предупреждение без команд разработчика."""
    config = tmp_path / ".harness" / "orchestration.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps({"verification_commands": ["python scripts/verify.py"]}),
        encoding="utf-8",
    )

    result = checks.check_verification_routing(
        _context(tmp_path, lock=_ORCHESTRATION_LOCK)
    )

    assert result.status == "warn"
    assert result.fix is not None
