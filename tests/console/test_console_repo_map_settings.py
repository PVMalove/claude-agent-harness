"""Repo Map settings: tier, project languages and project memory behind the console."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from harness.configuration_store import ConfigurationStore
from harness.console.repo_map_settings import (
    DEFAULT_MEMORY_POLICY,
    apply_tier,
    enable_memory,
    language_files,
    memory_status,
    tier_setting,
)
from harness.memory import build as memory_build
from harness.repo_map.bundle_fetch import ParserStatus
from tests.console.test_console_configuration_app import (
    _orchestration_project,
    installed_project,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.test",
            *args,
        ],
        check=True,
        capture_output=True,
    )


def test_the_tier_cannot_be_changed_without_orchestration(tmp_path: Path) -> None:
    installed_project(tmp_path)
    setting = tier_setting(tmp_path)
    assert (setting.tier, setting.editable) == ("full", False)
    assert "backend-orchestration" in setting.reason


def test_the_tier_is_written_to_the_policy_and_the_previous_files_are_kept(
    tmp_path: Path,
) -> None:
    _orchestration_project(tmp_path)
    assert tier_setting(tmp_path).tier == "full"
    backup = apply_tier(tmp_path, "minimal")
    written = json.loads((tmp_path / ".harness/orchestration.json").read_bytes())
    assert written["repo_map_policy"]["tier"] == "minimal"
    assert tier_setting(tmp_path).tier == "minimal"
    assert backup in {version.id for version in ConfigurationStore(tmp_path).versions()}
    apply_tier(tmp_path, "full")
    assert tier_setting(tmp_path).tier == "full"
    with pytest.raises(ValueError):
        apply_tier(tmp_path, "huge")


def test_memory_is_enabled_with_default_sources_then_indexed(tmp_path: Path) -> None:
    installed_project(tmp_path)
    (tmp_path / "docs/adr").mkdir(parents=True)
    (tmp_path / "docs/adr/0001-choice.md").write_text("# Choice\n\nWe chose it.\n")
    (tmp_path / "CONTEXT.md").write_text("# Glossary\n\nTerm: meaning.\n")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    before = memory_status(tmp_path)
    assert (before.enabled, before.active, before.indexed) == (False, False, None)
    enable_memory(tmp_path)
    config = json.loads((tmp_path / ".harness/project.json").read_bytes())
    assert config["memory"] == {"enabled": True}
    # allow_paths lists only the documents the project has (no CONTEXT-MAP.md here).
    assert config["memory_policy"] == {
        **DEFAULT_MEMORY_POLICY,
        "allow_paths": ["docs/adr/**/*.md", "CONTEXT.md"],
    }
    enabled = memory_status(tmp_path)
    assert (enabled.enabled, enabled.active, enabled.indexed) == (True, True, None)
    assert memory_build(tmp_path)["indexed"] == 2
    assert memory_status(tmp_path).indexed == 2


def test_an_existing_memory_policy_is_kept(tmp_path: Path) -> None:
    installed_project(tmp_path)
    path = tmp_path / ".harness/project.json"
    document = json.loads(path.read_bytes())
    own = {**DEFAULT_MEMORY_POLICY, "top_k": 9}
    document["memory_policy"] = own
    path.write_text(json.dumps(document))
    enable_memory(tmp_path)
    assert json.loads(path.read_bytes())["memory_policy"] == own


def test_files_are_counted_per_supported_language(tmp_path: Path) -> None:
    for name in ("a.py", "b.py", "c.ts", "d.tsx", "e.go", "f.md"):
        (tmp_path / name).write_text("x")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    assert language_files(tmp_path) == {"python": 2, "typescript": 2, "go": 1}
    assert language_files(tmp_path / "missing") == {}


def _status(*, ready: bool, languages: tuple[str, ...] = ()) -> ParserStatus:
    return ParserStatus(
        Path("/registry"),
        "cp314-linux_x86_64" if ready else None,
        "local-cache" if ready else None,
        tuple(languages),
        None if ready else "offline parser bundle unavailable",
    )


def _checks(**given: object) -> list[tuple[str, str]]:
    from harness.console.repo_map_settings import (
        MemoryStatus,
        TierSetting,
        repo_map_checks,
    )

    base: dict[str, object] = {
        "head": "a" * 40,
        "tier": TierSetting("full", True),
        "parsers": _status(ready=False),
        "files": {"python": 3, "go": 2},
        "memory": MemoryStatus(False, False, (), None),
        "view": None,
    }
    result = repo_map_checks(**{**base, **given})  # type: ignore[arg-type]
    return [(check.level, check.text) for check in result]


def test_the_checks_name_what_blocks_the_full_map_and_how_to_fix_it() -> None:
    lines = _checks()
    assert lines[0] == ("ok", "HEAD aaaaaaaaaaaa")
    text = "\n".join(f"{level}: {line}" for level, line in lines)
    assert "fail: Bundle парсеров не готов: offline parser bundle unavailable" in text
    assert "«Установить выбранные»" in text
    assert "skipped: Карта для HEAD не построена" in text
    assert "skipped: Память проекта выключена" in text


def test_the_checks_compare_project_languages_with_installed_parsers() -> None:
    lines = _checks(parsers=_status(ready=True, languages=("python",)))
    text = "\n".join(f"{level}: {line}" for level, line in lines)
    assert "ok: Python: парсер установлен, файлов: 3" in text
    assert "warn: Go: парсер не установлен" in text
    assert "ok: Bundle парсеров готов: cp314-linux_x86_64" in text


def test_minimal_mode_and_a_missing_head_are_reported() -> None:
    from harness.console.repo_map_settings import TierSetting

    lines = _checks(head=None, tier=TierSetting("minimal", True))
    text = "\n".join(f"{level}: {line}" for level, line in lines)
    assert "fail: HEAD не найден" in text
    assert "skipped: Режим minimal: парсер не нужен" in text
    assert "Bundle" not in text


def _committed_docs(repo: Path) -> None:
    (repo / "docs/adr").mkdir(parents=True)
    (repo / "docs/adr/0001-choice.md").write_text("# Choice\n\nWe chose it.\n")
    (repo / "CONTEXT.md").write_text("# Glossary\n\nTerm: meaning.\n")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")


def test_a_policy_with_no_allow_paths_gets_the_paths_of_the_documents_the_project_has(
    tmp_path: Path,
) -> None:
    installed_project(tmp_path)
    _committed_docs(tmp_path)
    path = tmp_path / ".harness/project.json"
    document = json.loads(path.read_bytes())
    document["memory"] = {"enabled": True}
    document["memory_policy"] = {
        "source_types": ["adr", "glossary", "ledger"],
        "allow_paths": [],
        "redact_rules": [],
        "min_similarity": 0.75,
        "top_k": 7,
        "max_tokens": 1000,
    }
    path.write_text(json.dumps(document))
    status = memory_status(tmp_path)
    assert (status.enabled, status.active) == (True, False)
    assert status.inactive == "не заданы allow_paths"
    enable_memory(tmp_path)
    policy = json.loads(path.read_bytes())["memory_policy"]
    # Only paths that exist are added (no ledger state here); the rest is kept.
    assert policy["allow_paths"] == ["docs/adr/**/*.md", "CONTEXT.md"]
    assert (policy["top_k"], policy["min_similarity"]) == (7, 0.75)
    assert memory_status(tmp_path).active
    assert memory_build(tmp_path)["indexed"] == 2


def test_enabling_memory_without_any_documents_says_so(tmp_path: Path) -> None:
    from harness.console.repo_map_settings import plan_memory_enable

    installed_project(tmp_path)
    path = tmp_path / ".harness/project.json"
    document = json.loads(path.read_bytes())
    document["memory"] = {"enabled": True}
    document["memory_policy"] = {**DEFAULT_MEMORY_POLICY, "allow_paths": []}
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="нет документов для памяти"):
        plan_memory_enable(tmp_path)
