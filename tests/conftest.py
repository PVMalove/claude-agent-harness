"""Shared fixtures for the per-language real-bundle Repo Map tests (ADR 0024, #279).

`tests/test_repo_map_tree_sitter.py` keeps its own inline helpers (Python/TS/JS, #277); the
per-language files added by #279 (Go, Java, C#, and the unsupported-language regression) share
these instead of repeating them.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from harness.repo_map import repo_map

BUNDLE_ENV = "HARNESS_PARSER_BUNDLE_DIR"


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def bundle_dir() -> Path:
    configured = os.environ.get(BUNDLE_ENV)
    if not configured:
        pytest.skip(f"{BUNDLE_ENV} is not set; the bundle CI job runs these tests")
    path = Path(configured)
    if not (path / "parser_bundle.lock.json").is_file():
        pytest.fail(f"{BUNDLE_ENV}={configured} has no parser_bundle.lock.json")
    return path


def build_map(
    tmp_path: Path, bundle: Path, files: dict[str, str], **policy: object
) -> tuple[bytes, dict[str, object]]:
    repo = tmp_path / "project"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    for name, content in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(content, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "fixture")
    commit = _git(repo, "rev-parse", "HEAD")
    policy_path = tmp_path / "orchestration.json"
    policy_path.write_text(
        json.dumps(
            {
                "repo_map_policy": {
                    "tier": "full",
                    "parser_bundle_registry_paths": [str(bundle)],
                    "parser_bundle_timeout_seconds": 120,
                    **policy,
                }
            }
        )
    )
    loaded = repo_map.load_policy(policy_path, explicit=True)
    encoded = repo_map.build_map(repo, commit, 8000, [], loaded)
    assert repo_map.build_map(repo, commit, 8000, [], loaded) == encoded
    decoded: dict[str, object] = json.loads(encoded)
    return encoded.encode("utf-8"), decoded


def records(result: dict[str, object], key: str) -> list[dict[str, object]]:
    values = result[key]
    assert isinstance(values, list)
    return values


def file_record(result: dict[str, object], path: str) -> dict[str, object]:
    return next(item for item in records(result, "files") if item["path"] == path)
