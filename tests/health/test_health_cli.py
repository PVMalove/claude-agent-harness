"""Сквозные тесты CLI команды harness health."""

from __future__ import annotations

import json
import os
import runpy
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness.health import registry as health_registry
from harness.health.context import HealthContext
from harness.health.model import CheckResult

CLI = runpy.run_path(
    str(Path(__file__).resolve().parents[2] / "harness" / "bin" / "harness.py")
)


def _init_repo(path: Path) -> None:
    """Инициализировать пустой git-репозиторий по указанному пути."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)


def test_cmd_health_exits_1_when_a_check_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Проверить, что CLI возвращает код 1 при наличии упавшей проверки."""
    _init_repo(tmp_path)

    exit_code = CLI["cmd_health"](SimpleNamespace(repo=str(tmp_path), json=False))

    assert exit_code == 1
    out = capsys.readouterr().out
    assert "отсутствует .harness/harness.lock" in out
    assert "Итого:" in out


def test_cmd_health_runs_every_check_without_early_exit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Проверить, что CLI запускает все проверки без преждевременного выхода."""
    _init_repo(tmp_path)

    CLI["cmd_health"](SimpleNamespace(repo=str(tmp_path), json=True))

    data = json.loads(capsys.readouterr().out)
    ids = {check["id"] for check in data["checks"]}
    # Every registered group ran, not just the first failing one.
    assert {"files.lock", "files.agents_md", "repo_map.tier"} <= ids


def test_cmd_health_json_matches_schema_version_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Проверить, что вывод health --json соответствует версии схемы 1."""
    _init_repo(tmp_path)

    exit_code = CLI["cmd_health"](SimpleNamespace(repo=str(tmp_path), json=True))

    data = json.loads(capsys.readouterr().out)
    assert data["schema_version"] == 1
    assert data["repo"] == str(tmp_path.resolve())
    assert data["online"] is False
    assert set(data["summary"]) == {"ok", "warn", "fail", "skipped"}
    assert data["fixes_applied"] == []
    assert exit_code == (1 if data["summary"]["fail"] else 0)


def test_cmd_health_exits_0_when_nothing_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Проверить, что CLI возвращает код 0, если нет упавших проверок."""
    _init_repo(tmp_path)

    def _all_ok(_context: HealthContext) -> CheckResult:
        """Вернуть результат проверки со статусом warn."""
        return CheckResult(
            id="test.probe", group="test", status="warn", message="not a failure"
        )

    monkeypatch.setattr(health_registry, "REGISTRY", [("test.probe", _all_ok)])

    exit_code = CLI["cmd_health"](SimpleNamespace(repo=str(tmp_path), json=True))

    data = json.loads(capsys.readouterr().out)
    assert data["summary"] == {"ok": 0, "warn": 1, "fail": 0, "skipped": 0}
    assert exit_code == 0


@pytest.mark.parametrize(
    "lock_text",
    ["{broken", "[]", "[" * 100_000],
    ids=["invalid", "not-object", "too-deep"],
)
def test_broken_lock_is_a_fail_result_not_a_crash(
    tmp_path: Path, lock_text: str
) -> None:
    """Проверить, что поврежденный файл lock приводит к статусу fail, а не к падению процесса."""
    _init_repo(tmp_path)
    (tmp_path / ".harness").mkdir()
    (tmp_path / ".harness" / "harness.lock").write_text(lock_text, encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[2] / "harness" / "bin" / "harness.py"),
            "health",
            str(tmp_path),
            "--json",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert result.returncode == 1, result.stderr
    data = json.loads(result.stdout)
    checks = {check["id"]: check for check in data["checks"]}
    assert checks["files.lock"]["status"] == "fail"
    assert "повреждён" in checks["files.lock"]["message"]
    assert checks["files.lock"]["fix"] is not None
    assert "repo_map.tier" in checks
    # Lock-dependent checks point at the broken lock instead of claiming the lock is missing or
    # that backend-orchestration was never selected.
    for check_id in (
        "files.skill_registry",
        "files.orchestration_config",
        "orchestration.ledger_summary",
    ):
        assert checks[check_id]["status"] == "skipped"
        assert "повреждён" in checks[check_id]["message"]
    assert not any("не выбрана" in check["message"] for check in data["checks"])


@pytest.mark.parametrize("pythonpath_first", [True, False])
def test_cli_imports_the_harness_package_not_itself(
    tmp_path: Path, pythonpath_first: bool
) -> None:
    """Проверить, что CLI импортирует пакет harness, а не скрипт harness.py."""
    root = Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    if pythonpath_first:
        env["PYTHONPATH"] = str(root)
    result = subprocess.run(
        [sys.executable, str(root / "harness" / "bin" / "harness.py"), "--help"],
        cwd=root / "harness" / "bin",
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "health" in result.stdout


def test_health_reports_disabled_memory_without_creating_cache(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unconfigured project gets an explicit optional-memory section without writes."""
    _init_repo(tmp_path)
    CLI["cmd_health"](SimpleNamespace(repo=str(tmp_path), json=True))
    data = json.loads(capsys.readouterr().out)
    memory = {c["id"]: c for c in data["checks"] if c["group"] == "memory"}
    assert memory["memory.index"]["status"] == "skipped"
    assert "выключена" in memory["memory.index"]["message"]
    assert not (tmp_path / ".harness").exists()
    CLI["cmd_health"](SimpleNamespace(repo=str(tmp_path), json=False))
    assert "== Память проекта ==" in capsys.readouterr().out
    assert not (tmp_path / ".harness").exists()


def _enable_memory(repo: Path) -> None:
    """Select a known corpus through the public project policy."""
    (repo / ".harness").mkdir(exist_ok=True)
    (repo / ".harness/project.json").write_text(
        json.dumps(
            {
                "memory": {"enabled": True},
                "memory_policy": {
                    "source_types": ["glossary"],
                    "allow_paths": ["CONTEXT.md"],
                    "redact_rules": [],
                    "min_similarity": 0,
                    "top_k": 5,
                    "max_tokens": 1000,
                },
            }
        ),
        encoding="utf-8",
    )


def test_health_missing_memory_index_recommends_rebuild_without_writes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Inspection of an enabled but unbuilt cache only supplies a manual remedy."""
    _init_repo(tmp_path)
    _enable_memory(tmp_path)
    config = tmp_path / ".harness/project.json"
    before = config.read_bytes(), config.stat().st_mtime_ns
    CLI["cmd_health"](SimpleNamespace(repo=str(tmp_path), json=True))
    checks = {c["id"]: c for c in json.loads(capsys.readouterr().out)["checks"]}
    index = checks["memory.index"]
    assert index["status"] == "warn"
    assert "индекс отсутствует" in index["message"]
    assert "memory rebuild" in index["fix"]["command"]
    assert str(tmp_path) in index["fix"]["command"]
    assert (config.read_bytes(), config.stat().st_mtime_ns) == before
    assert sorted(p.name for p in config.parent.iterdir()) == ["project.json"]


def _build_memory(repo: Path) -> Path:
    """Build the fixture cache through the same public CLI as a project owner."""
    _enable_memory(repo)
    (repo / "CONTEXT.md").write_text("# Glossary\nA known source.\n", encoding="utf-8")
    assert (
        CLI["cmd_memory"](SimpleNamespace(repo=str(repo), memory_operation="rebuild"))
        == 0
    )
    return repo / ".harness/.sandboxes/cache/memory/index.sqlite3"


def _cache_snapshot(repo: Path) -> dict[str, tuple[bytes, int]]:
    """Observe cache bytes, timestamps and sidecar names as read-only evidence."""
    cache = repo / ".harness/.sandboxes/cache/memory"
    return {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in cache.iterdir()}


def test_health_valid_memory_reports_size_and_source_count_read_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A built FTS5 cache has usable statistics and remains untouched by inspection."""
    _init_repo(tmp_path)
    path = _build_memory(tmp_path)
    capsys.readouterr()
    before = _cache_snapshot(tmp_path)
    CLI["cmd_health"](SimpleNamespace(repo=str(tmp_path), json=True))
    checks = {c["id"]: c for c in json.loads(capsys.readouterr().out)["checks"]}
    index = checks["memory.index"]
    assert index["status"] == "ok"
    assert "индекс валиден" in index["message"]
    assert f"{path.stat().st_size} байт" in index["message"]
    assert "источников: 1" in index["message"]
    assert index["fix"] is None
    assert _cache_snapshot(tmp_path) == before
    CLI["cmd_health"](SimpleNamespace(repo=str(tmp_path), json=False))
    text = capsys.readouterr().out
    assert "== Память проекта ==" in text
    assert f"{path.stat().st_size} байт; источников: 1" in text
    assert _cache_snapshot(tmp_path) == before


@pytest.mark.parametrize(
    "damage",
    [
        "corrupt",
        "missing-fts",
        "wrong-fts-table",
        "wrong-rowids",
        "changed-policy",
        "changed-schema",
    ],
)
def test_health_unusable_memory_warns_with_manual_rebuild_even_with_fix(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], damage: str
) -> None:
    """Damaged or incompatible caches are diagnosed and never repaired by health --fix."""
    _init_repo(tmp_path)
    path = _build_memory(tmp_path)
    if damage == "corrupt":
        path.write_bytes(b"not a sqlite cache")
    elif damage == "changed-policy":
        config = tmp_path / ".harness/project.json"
        data = json.loads(config.read_text(encoding="utf-8"))
        data["memory_policy"]["top_k"] = 7
        config.write_text(json.dumps(data), encoding="utf-8")
    else:
        with sqlite3.connect(path) as connection:
            if damage == "missing-fts":
                connection.execute("DROP TABLE search_text")
            elif damage == "wrong-fts-table":
                connection.execute("DROP TABLE search_text")
                connection.execute("CREATE TABLE search_text(title TEXT, body TEXT)")
                connection.execute(
                    "INSERT INTO search_text VALUES ('Glossary', 'A known source.')"
                )
            elif damage == "wrong-rowids":
                connection.execute("UPDATE search_text SET rowid=rowid+100")
            else:
                connection.execute(
                    "UPDATE manifest SET value='unknown' WHERE key='schema_version'"
                )
    capsys.readouterr()
    before = _cache_snapshot(tmp_path)
    CLI["cmd_health"](SimpleNamespace(repo=str(tmp_path), json=True, fix=True))
    data = json.loads(capsys.readouterr().out)
    index = next(c for c in data["checks"] if c["id"] == "memory.index")
    assert index["status"] == "warn"
    assert "memory rebuild" in index["fix"]["command"]
    assert not any("memory" in fix for fix in data["fixes_applied"])
    assert _cache_snapshot(tmp_path) == before


def test_health_missing_model_does_not_invalidate_fts_memory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Slice one explicitly reports the absent optional model beside a valid FTS5 index."""
    _init_repo(tmp_path)
    _build_memory(tmp_path)
    capsys.readouterr()
    before = _cache_snapshot(tmp_path)
    CLI["cmd_health"](SimpleNamespace(repo=str(tmp_path), json=True))
    checks = {c["id"]: c for c in json.loads(capsys.readouterr().out)["checks"]}
    assert checks["memory.model"]["status"] == "skipped"
    assert "модель не скачана" in checks["memory.model"]["message"]
    assert "FTS5 работает без модели" in checks["memory.model"]["message"]
    assert checks["memory.index"]["status"] == "ok"
    assert _cache_snapshot(tmp_path) == before


def test_health_worktree_inspects_main_memory_and_recommends_main_rebuild(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Worktree configuration cannot replace the shared main-checkout memory policy."""
    main = tmp_path / "main checkout"
    _init_repo(main)
    path = _build_memory(main)
    subprocess.run(["git", "-C", str(main), "add", "CONTEXT.md"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(main),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    linked = tmp_path / "linked"
    subprocess.run(
        ["git", "-C", str(main), "worktree", "add", "-qb", "linked", str(linked)],
        check=True,
    )
    (linked / ".harness").mkdir()
    (linked / ".harness/project.json").write_text(
        '{"memory":{"enabled":false}}', encoding="utf-8"
    )
    capsys.readouterr()
    before = _cache_snapshot(main)
    CLI["cmd_health"](SimpleNamespace(repo=str(linked), json=True))
    index = next(
        c
        for c in json.loads(capsys.readouterr().out)["checks"]
        if c["id"] == "memory.index"
    )
    assert index["status"] == "ok"
    assert "источников: 1" in index["message"]
    assert _cache_snapshot(main) == before
    path.unlink()
    CLI["cmd_health"](SimpleNamespace(repo=str(linked), json=True))
    index = next(
        c
        for c in json.loads(capsys.readouterr().out)["checks"]
        if c["id"] == "memory.index"
    )
    assert index["status"] == "warn"
    assert str(main) in index["fix"]["command"]
    assert str(linked) not in index["fix"]["command"]
    assert not (linked / ".harness/.sandboxes").exists()


def test_console_diagnostics_include_the_same_memory_health_report(
    tmp_path: Path,
) -> None:
    """Console collection and its display/export document retain all memory diagnostics."""
    pytest.importorskip("textual")
    from harness.console.data import collect_diagnostics
    from harness.console.screens.diagnostics import health_document

    _init_repo(tmp_path)
    _enable_memory(tmp_path)
    report = collect_diagnostics(tmp_path)
    memory = {c.id: c for c in report.checks if c.group == "memory"}
    assert memory["memory.index"].status == "warn"
    assert memory["memory.model"].status == "skipped"
    document = health_document(report)
    body = "\n".join(section.body for section in document.sections)
    for check in memory.values():
        assert check.id in body
        assert check.message in body
    assert "memory rebuild" in body
    assert not (tmp_path / ".harness/.sandboxes").exists()
