"""Реальный Git-репозиторий с картой Repo Map схемы v1 для коммита HEAD, записанной
в кэш результатов через harness.repo_map.cache.write_cache — фикстура для тестов раздела Repo Map."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from harness.repo_map.cache import write_cache
from harness.repo_map.contract import validation_error
from harness.storage import storage_path

HUB = "pkg/core.py"


def _git(repo: Path, *args: str) -> str:
    """Выполнить команду git в репозитории и вернуть результат без концевых пробелов."""
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def init_repo(repo: Path) -> str:
    """Инициализировать репозиторий с одним коммитом и вернуть полный SHA коммита HEAD."""
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "fixture@example.com")
    _git(repo, "config", "user.name", "Fixture")
    (repo / "pkg").mkdir(exist_ok=True)
    (repo / "pkg" / "core.py").write_text("def load_config(path):\n    pass\n", "utf-8")
    (repo / "pkg" / "cli.py").write_text("from pkg.core import load_config\n", "utf-8")
    (repo / "README.md").write_text("fixture\n", "utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "fixture")
    return _git(repo, "rev-parse", "HEAD")


def map_payload(commit: str, *, tier: str = "full") -> dict[str, object]:
    """Сформировать полезную нагрузку схемы v1: все виды рёбер, достоверность, хаб и диагностика."""
    if tier == "minimal":
        payload: dict[str, object] = {
            "schema_version": 1,
            "commit": commit,
            "tier": "minimal",
            "parser": "path-only",
            "degradation_reason": "offline parser bundle unavailable",
            "token_estimator_version": "1",
            "parser_provenance": {
                "policy_mode": "portable",
                "policy_sha256": None,
                "max_file_bytes": 1000,
                "max_files": 100,
                "timeout_seconds": 30,
                "max_tokens": 4000,
                "bundle_mode": "degraded",
                "bundle_source": "none",
            },
            "files": [{"path": HUB}, {"path": "pkg/cli.py"}, {"path": "README.md"}],
            "edges": [],
            "diagnostics": [],
            "estimated_tokens": 120,
        }
    else:
        payload = {
            "schema_version": 1,
            "commit": commit,
            "tier": "full",
            "parser": "bundle",
            "degradation_reason": "parser bundle applied",
            "token_estimator_version": "1",
            "parser_provenance": {
                "policy_mode": "enforced",
                "policy_sha256": "ab" * 32,
                "policy_tier": "full",
                "max_file_bytes": 1000,
                "max_files": 100,
                "timeout_seconds": 30,
                "max_tokens": 4000,
                "bundle_mode": "applied",
                "bundle_source": "local-cache",
                "python_tag": "cp314",
                "platform_tag": "linux_x86_64",
                "lock_sha256": "cd" * 32,
                "script_hash": "ef" * 32,
                "core_version": "0.25.0",
                "core_abi_range": "13-15",
                "grammars": [
                    {
                        "name": "python",
                        "version": "0.23.6",
                        "abi": 14,
                        "sha256": "12" * 32,
                    }
                ],
            },
            "files": [
                {
                    "path": HUB,
                    "signatures": ["def load_config(path)", "class Settings"],
                    "parser_status": "ok",
                },
                {
                    "path": "pkg/cli.py",
                    "signatures": ["def main(argv)"],
                    "parser_status": "ok",
                },
                {
                    "path": "pkg/broken.py",
                    "signatures": [],
                    "parser_status": "syntax_error",
                },
                {"path": "README.md", "signatures": [], "parser_status": "ok"},
            ],
            "edges": [
                {
                    "source": "pkg/cli.py",
                    "target": HUB,
                    "kind": "import",
                    "confidence": "high",
                },
                {
                    "source": "pkg/cli.py",
                    "target": HUB,
                    "kind": "unique-name-ref",
                    "confidence": "medium",
                },
                {
                    "source": "pkg/broken.py",
                    "target": HUB,
                    "kind": "ambiguous-name-ref",
                    "confidence": "low",
                },
                {
                    "source": HUB,
                    "target": "pkg/cli.py",
                    "kind": "unique-name-ref",
                    "confidence": "medium",
                },
            ],
            "diagnostics": [{"code": "syntax_error", "path": "pkg/broken.py"}],
            "estimated_tokens": 512,
        }
    assert validation_error(payload) is None
    return payload


def cache_dir(repo: Path) -> Path:
    """Возвратить путь к каталогу кэша результатов карты репозитория."""
    return storage_path(repo, "cache", "repo_map", "results")


def write_cached_map(repo: Path, payload: dict[str, object], key: str) -> Path:
    """Записать данные карты в кэш результатов repo_map и вернуть путь к файлу."""
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    write_cache(cache_dir(repo), key, text)
    return cache_dir(repo) / f"{key}.json"


def build_repo_map_fixture(repo: Path, *, cached: bool = True) -> str:
    """Создать тестовый репозиторий с закэшированной картой для HEAD при cached=True."""
    commit = init_repo(repo)
    if cached:
        write_cached_map(repo, map_payload(commit), "a" * 64)
    return commit
