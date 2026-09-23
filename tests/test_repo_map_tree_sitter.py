"""Repo Map against a real, verified tree-sitter parser bundle (the bundle CI job).

These tests need `HARNESS_PARSER_BUNDLE_DIR` pointing at a directory assembled by
scripts/build_parser_bundle.py. Without the variable they are skipped, so the main test run stays
offline and tree-sitter-free; the bundle CI job sets it, and a set variable whose bundle is missing
fails instead of skipping (ADR 0024: an unavailable artifact is a failure, not a silent skip).
"""

import json
import os
import shutil
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


def _map(
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


def _records(result: dict[str, object], key: str) -> list[dict[str, object]]:
    records = result[key]
    assert isinstance(records, list)
    return records


def _file(result: dict[str, object], path: str) -> dict[str, object]:
    return next(item for item in _records(result, "files") if item["path"] == path)


PACKAGE = {
    "pkg/__init__.py": "",
    "pkg/helper.py": "def render(value: int) -> str:\n    return str(value)\n",
    "pkg/consumer.py": (
        "# comment that must not be serialized\n"
        "import os\n"
        "from . import helper\n"
        "from .helper import render as draw\n"
        "\n"
        "@decorator\n"
        "async def fetch(url: str, *, retries: int = 3, **options) -> bytes:\n"
        "    body_secret = 'function bodies are never serialized'\n"
        "    return render(url)\n"
        "\n"
        "class Client(Base, metaclass=Meta):\n"
        "    def send(self, payload: dict[str, int] = {}) -> None:\n"
        "        pass\n"
    ),
}


def test_python_signatures_and_edges_come_from_tree_sitter(
    tmp_path: Path, bundle_dir: Path
) -> None:
    raw, result = _map(tmp_path, bundle_dir, PACKAGE)

    assert result["tier"] == "full"
    assert result["parser"] == "bundle"
    provenance = result["parser_provenance"]
    assert isinstance(provenance, dict)
    assert provenance["bundle_mode"] == "applied"
    assert provenance["core_version"] == "0.26.0"
    assert provenance["core_abi_range"] == "13-15"
    grammars = provenance["grammars"]
    assert isinstance(grammars, list)
    python = next(grammar for grammar in grammars if grammar["name"] == "python")
    assert python["version"] == "0.25.0"
    assert python["abi"] == 15
    assert len(python["sha256"]) == 64

    consumer = _file(result, "pkg/consumer.py")
    assert consumer["parser_status"] == "ok"
    assert consumer["signatures"] == [
        "async def fetch(url: str, *, retries: int=..., **options) -> bytes",
        "class Client(Base)",
        "def Client.send(self, payload: dict[str, int]=...) -> None",
    ]
    assert _file(result, "pkg/helper.py")["signatures"] == ["def render(value: int) -> str"]
    assert {
        "source": "pkg/consumer.py",
        "target": "pkg/helper.py",
        "kind": "import",
        "confidence": "high",
    } in _records(result, "edges")
    assert {
        "source": "pkg/consumer.py",
        "target": "pkg/helper.py",
        "kind": "unique-name-ref",
        "confidence": "medium",
    } in _records(result, "edges")
    assert b"comment that must" not in raw
    assert b"body_secret" not in raw
    assert b"never serialized" not in raw


def test_python_syntax_error_keeps_intact_definitions(tmp_path: Path, bundle_dir: Path) -> None:
    _, result = _map(
        tmp_path,
        bundle_dir,
        {
            "broken.py": (
                "import helper\n\n"
                "def intact(value: int) -> int:\n"
                "    return value\n\n"
                "def broken(:\n"
                "    pass\n"
            ),
            "helper.py": "def run() -> None: pass\n",
        },
    )
    broken = _file(result, "broken.py")
    assert broken["parser_status"] == "syntax_error"
    assert broken["signatures"] == ["def intact(value: int) -> int"]
    assert result["diagnostics"] == [{"code": "syntax_error", "path": "broken.py"}]
    assert {
        "source": "broken.py",
        "target": "helper.py",
        "kind": "import",
        "confidence": "high",
    } in _records(result, "edges")


def test_python_invalid_encoding_is_a_diagnostic(tmp_path: Path, bundle_dir: Path) -> None:
    repo = tmp_path / "project"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "latin.py").write_bytes(b"name = '\xe9'\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "fixture")
    policy = repo_map.RepoMapPolicy(
        tier="full",
        parser_bundle_registry_paths=(str(bundle_dir),),
        parser_bundle_timeout_seconds=120,
    )
    decoded = json.loads(
        repo_map.build_map(repo, _git(repo, "rev-parse", "HEAD"), 4000, [], policy)
    )
    assert decoded["files"] == [
        {"path": "latin.py", "signatures": [], "parser_status": "invalid_encoding"}
    ]
    assert decoded["diagnostics"] == [{"code": "invalid_encoding", "path": "latin.py"}]


def test_python_symbol_redaction_applies_to_tree_sitter_facts(
    tmp_path: Path, bundle_dir: Path
) -> None:
    raw, result = _map(
        tmp_path,
        bundle_dir,
        {
            "main.py": (
                "import hidden_service\n\n"
                "def visible(value: int) -> int:\n    return value\n\n"
                "def leaks(arg: 'hidden_type') -> int:\n    return 0\n\n"
                "def hidden_helper() -> None:\n    pass\n"
            ),
            "hidden_service.py": "def run() -> None: pass\n",
        },
        redact_symbols=["hidden_*"],
    )
    assert _file(result, "main.py")["signatures"] == ["def visible(value: int) -> int"]
    assert result["edges"] == []
    assert b"hidden_helper" not in raw
    assert b"hidden_type" not in raw


def test_wrong_bundle_hash_degrades_to_minimal(tmp_path: Path, bundle_dir: Path) -> None:
    tampered = tmp_path / "tampered-bundle"
    shutil.copytree(bundle_dir, tampered)
    worker = tampered / "tree_sitter_worker.py"
    worker.write_text(worker.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8")

    _, result = _map(tmp_path, tampered, PACKAGE)

    assert result["tier"] == "minimal"
    assert result["parser"] == "path-only"
    assert result["degradation_reason"] == "parser bundle hash mismatch"
    assert all(set(item) == {"path"} for item in _records(result, "files"))
