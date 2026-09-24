"""Behavior of the standalone Repo Map CLI at its process boundary."""

import json
import re
import socket
import subprocess
import sys
import time
import tracemalloc
from pathlib import Path

import pytest
from _parser_bundle_fixtures import build_bundle_dir

from harness.errors import HarnessError
from harness.repo_map import parser_bundle, repo_map

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "harness" / "repo_map" / "repo_map.py"


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def _uv_available() -> bool:
    import shutil

    return shutil.which("uv") is not None


def _forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any socket construction or outbound connection fail the test immediately."""

    def _raise(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("unexpected network call during repo map build")

    monkeypatch.setattr(socket.socket, "__init__", _raise)
    monkeypatch.setattr(socket, "create_connection", _raise)


def _facts(
    *,
    signatures: list[tuple[str, list[str]]] | None = None,
    imports: list[tuple[str, int, list[str]]] | None = None,
    definitions: list[str] | None = None,
    references: list[str] | None = None,
    status: str = "ok",
) -> dict[str, object]:
    """A `FileFacts` record as the tree-sitter worker would return it."""
    return {
        "parser_status": status,
        "signatures": [
            {"text": text, "symbols": symbols} for text, symbols in signatures or []
        ],
        "imports": [
            {"module": module, "level": level, "names": names}
            for module, level, names in imports or []
        ],
        "definitions": definitions or [],
        "references": references or [],
    }


def _fake_python_bundle(
    monkeypatch: pytest.MonkeyPatch, facts: dict[str, dict[str, object]]
) -> list[dict[str, object]]:
    """Stand in for a verified tree-sitter bundle in-process: canned facts, no subprocess.

    The real worker is exercised by tests/test_repo_map_tree_sitter.py in the bundle CI job; this
    fake keeps the facts-to-graph logic (edges, ranking, redaction) in the main test run. Returns
    the list that records every parser request.
    """
    lock = parser_bundle.BundleLock(
        core_version="0.26.0",
        core_abi_range="13-15",
        worker_script="worker.py",
        script_sha256="0" * 64,
        grammars=(
            parser_bundle.GrammarSpec(
                name="python",
                version="0.25.0",
                abi=15,
                sha256="1" * 64,
                extensions=(".py",),
            ),
        ),
        wheelhouses={},
        raw_sha256="2" * 64,
    )
    applied = parser_bundle.AppliedBundle(
        lock=lock,
        install_dir=Path("unused"),
        worker_script=Path("unused"),
        python_tag="cp3x",
        platform_tag="test",
        bundle_source="local-cache",
        worker_extensions={".py": "python"},
    )
    requests: list[dict[str, object]] = []

    def _run(
        _python: str,
        _script: Path,
        _install: Path,
        request: dict[str, object],
        **_: object,
    ) -> dict[str, object]:
        requests.append(request)
        paths = request["paths"]
        assert isinstance(paths, dict)
        return {"files": {path: facts.get(path, _facts()) for path in paths}}

    monkeypatch.setattr(parser_bundle, "acquire_bundle", lambda **_: applied)
    monkeypatch.setattr(parser_bundle, "run_bundle_parser", _run)
    return requests


def _commit_files(repo: Path, files: dict[str, str]) -> str:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    for name, content in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(content)
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "fixture")
    return _git(repo, "rev-parse", "HEAD")


def test_repo_map_without_bundle_is_minimal_path_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "project"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "main.py").write_text(
        "import helper\n\ndef run(x: int) -> str:\n    return helper.render(x)\n"
    )
    (repo / "helper.py").write_text(
        "def render(value: int) -> str:\n    return str(value)\n"
    )
    (repo / "broken.py").write_text("def broken(:\n")
    (repo / "secret.env").write_text("TOKEN=private")
    (repo / "secrets").mkdir()
    (repo / "secrets" / "config.py").write_text("def leaked(): pass\n")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "ignored.py").write_text("def ignored(): pass\n")
    (repo / "third_party").mkdir()
    (repo / "third_party" / "ignored.py").write_text("def ignored(): pass\n")
    (repo / "generated").mkdir()
    (repo / "generated" / "client.py").write_text("def ignored(): pass\n")
    (repo / "client.generated.py").write_text("def ignored(): pass\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "fixture")
    commit = _git(repo, "rev-parse", "HEAD")
    (repo / "main.py").write_text("def changed(): pass\n")
    (repo / "untracked.py").write_text("def hidden(): pass\n")

    command = [sys.executable, str(CLI), "--repo", str(repo), "--commit", commit]
    first = subprocess.check_output(command)
    assert subprocess.check_output(command) == first
    result = json.loads(first)
    # No verified bundle: Python is never parsed (no stdlib `ast` fallback), only paths remain.
    assert result["tier"] == "minimal"
    assert result["parser"] == "path-only"
    assert result["degradation_reason"] == "offline parser bundle unavailable"
    assert result["parser_provenance"]["policy_tier"] == "full"
    assert result["parser_provenance"]["bundle_mode"] == "degraded"
    assert result["token_estimator_version"] == "utf8-bytes-per-2-v1"
    assert result["files"] == [
        {"path": "broken.py"},
        {"path": "helper.py"},
        {"path": "main.py"},
    ]
    assert result["edges"] == []
    assert result["diagnostics"] == []
    for leaked in (b"render", b"changed", b"hidden", b"private", b"ignored", b"leaked"):
        assert leaked not in first

    bounded = json.loads(subprocess.check_output(command + ["--max-tokens", "300"]))
    assert bounded["estimated_tokens"] <= 300

    # The degraded default never touches the network.
    _forbid_network(monkeypatch)
    in_process = json.loads(repo_map.build_map(repo, commit, 4000, []))
    assert in_process["tier"] == "minimal"


def test_missing_bundle_keeps_python_typescript_and_javascript_path_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "project"
    commit = _commit_files(repo, {
        "example.py": "def python_symbol(): pass\n",
        "example.ts": "export function tsSymbol() {}\n",
        "example.js": "export function jsSymbol() {}\n",
    })
    monkeypatch.setattr(parser_bundle, "acquire_bundle", lambda **_: "offline parser bundle unavailable")
    result = json.loads(repo_map.build_map(
        repo, commit, 4000, [], repo_map.RepoMapPolicy(tier="full"), cache_dir=tmp_path / "cache"
    ))
    assert result["tier"] == "minimal"
    assert result["parser"] == "path-only"
    assert result["edges"] == []
    assert all(set(item) == {"path"} for item in result["files"])


def test_full_cache_rebuilds_after_bundle_becomes_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "project"
    commit = _commit_files(repo, {"entry.py": "def entry(): pass\n"})
    cache_dir = tmp_path / "cache"
    policy = repo_map.RepoMapPolicy(tier="full")
    monkeypatch.setattr(parser_bundle, "acquire_bundle", lambda **_: "offline parser bundle unavailable")
    first = json.loads(repo_map.build_map(repo, commit, 4000, [], policy, cache_dir=cache_dir))
    assert first["tier"] == "minimal"
    assert list(cache_dir.glob("*.json")) == []
    calls = _fake_python_bundle(monkeypatch, {"entry.py": _facts(signatures=[("def entry()", ["entry"])])})
    second = json.loads(repo_map.build_map(repo, commit, 4000, [], policy, cache_dir=cache_dir))
    assert second["tier"] == "full"
    assert second["files"][0]["signatures"] == ["def entry()"]
    assert len(calls) == 1


def test_full_cache_identity_changes_when_bundle_bytes_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "project"
    commit = _commit_files(repo, {"entry.py": "def entry(): pass\n"})
    python_tag, platform_tag = parser_bundle.python_platform_tags(sys.executable, 30)
    bundle = build_bundle_dir(tmp_path / "bundle", pair=f"{python_tag}-{platform_tag}")
    policy = repo_map.RepoMapPolicy(tier="full", parser_bundle_registry_paths=(str(bundle),))
    cache_dir = tmp_path / "cache"
    calls = _fake_python_bundle(monkeypatch, {"entry.py": _facts()})
    first = repo_map.build_map(repo, commit, 4000, [], policy, cache_dir=cache_dir)
    assert repo_map.build_map(repo, commit, 4000, [], policy, cache_dir=cache_dir) == first
    assert len(calls) == 1
    worker = bundle / "worker.py"
    worker.write_text(worker.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
    assert repo_map.build_map(repo, commit, 4000, [], policy, cache_dir=cache_dir) == first
    assert len(calls) == 2


def test_repo_map_cache_reuses_byte_identical_result_and_keys_every_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "project"
    commit = _commit_files(
        repo,
        {"main.py": "import helper\n", "helper.py": "def render() -> None: ...\n"},
    )
    cache_dir = tmp_path / "cache"
    calls = _fake_python_bundle(monkeypatch, {"main.py": _facts()})
    policy = repo_map.RepoMapPolicy(tier="full")

    first = repo_map.build_map(
        repo, commit, 4000, ["main.py", "main.py"], policy, cache_dir=cache_dir
    )
    assert (
        repo_map.build_map(repo, commit, 4000, ["main.py"], policy, cache_dir=cache_dir)
        == first
    )
    assert len(calls) == 1

    repo_map.build_map(repo, commit, 3999, ["main.py"], policy, cache_dir=cache_dir)
    repo_map.build_map(repo, commit, 4000, ["helper.py"], policy, cache_dir=cache_dir)
    repo_map.build_map(
        repo,
        commit,
        4000,
        ["main.py"],
        repo_map.RepoMapPolicy(tier="full", max_file_bytes=999),
        cache_dir=cache_dir,
    )
    assert len(calls) == 4


def test_default_result_cache_is_shared_by_linked_worktrees(tmp_path: Path) -> None:
    repo = tmp_path / "project"
    commit = _commit_files(repo, {"main.py": "def run() -> None: pass\n"})
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "--detach", str(linked), commit)
    policy = repo_map.RepoMapPolicy(tier="minimal")

    first = repo_map.build_map(repo, commit, 4000, [], policy)
    cache = repo / ".harness" / ".cache" / "repo_map" / "results"
    assert len(list(cache.glob("*.json"))) == 1
    assert repo_map.build_map(linked, commit, 4000, [], policy) == first
    assert len(list(cache.glob("*.json"))) == 1
    assert not (linked / ".harness").exists()


def test_repo_map_cache_discards_tampered_entry_and_never_touches_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "project"
    commit = _commit_files(repo, {"main.py": "def run() -> None: ...\n"})
    cache_dir = tmp_path / "cache"
    calls = _fake_python_bundle(monkeypatch, {"main.py": _facts()})
    policy = repo_map.RepoMapPolicy(tier="full")
    first = repo_map.build_map(repo, commit, 4000, [], policy, cache_dir=cache_dir)
    entry = next(cache_dir.glob("*.json"))
    entry.write_text('{"sha256":"bad","payload":"tampered"}', encoding="utf-8")

    assert (
        repo_map.build_map(repo, commit, 4000, [], policy, cache_dir=cache_dir) == first
    )
    assert len(calls) == 2
    assert _git(repo, "status", "--porcelain") == ""


def test_repo_map_cache_large_fixture_measures_cold_warm_limits(tmp_path: Path) -> None:
    repo = tmp_path / "project"
    commit = _commit_files(
        repo,
        {
            f"src/module_{index:04}.py": "def run() -> None: pass\n"
            for index in range(500)
        },
    )
    cache_dir = tmp_path / "cache"
    policy = repo_map.RepoMapPolicy(tier="minimal")

    tracemalloc.start()
    started = time.perf_counter()
    cold = repo_map.build_map(repo, commit, 1600, [], policy, cache_dir=cache_dir)
    cold_latency = time.perf_counter() - started
    started = time.perf_counter()
    warm = repo_map.build_map(repo, commit, 1600, [], policy, cache_dir=cache_dir)
    warm_latency = time.perf_counter() - started
    _, peak_memory = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert warm == cold
    assert len(cold.encode("utf-8")) <= 8_000
    assert peak_memory < 32_000_000
    assert cold_latency < 10
    assert warm_latency < 10


def test_repo_map_production_modules_never_import_stdlib_ast() -> None:
    for source_path in (
        ROOT / "harness" / "repo_map" / "repo_map.py",
        ROOT / "harness" / "repo_map" / "parser_bundle.py",
        ROOT / "harness" / "repo_map" / "tree_sitter_worker.py",
    ):
        source = source_path.read_text(encoding="utf-8")
        assert (
            re.search(r"^\s*(import ast\b|from ast import)", source, re.MULTILINE)
            is None
        )
        assert "ast-only" not in source


def test_repo_map_full_tier_builds_signatures_and_edges_from_bundle_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "project"
    commit = _commit_files(
        repo,
        {
            "main.py": "import helper\n",
            "helper.py": "def render(value: int) -> str: ...\n",
            "alpha.py": "def alpha(): pass\n",
            "beta.py": "def beta(): pass\n",
            "notes.txt": "plain text\n",
        },
    )
    requests = _fake_python_bundle(
        monkeypatch,
        {
            "main.py": _facts(
                signatures=[("def run(x: int) -> str", ["run", "x", "int", "str"])],
                imports=[("helper", 0, [])],
                references=["helper"],
            ),
            "helper.py": _facts(
                signatures=[
                    ("def render(value: int) -> str", ["render", "value", "int", "str"])
                ],
                definitions=["render"],
            ),
        },
    )

    first = repo_map.build_map(repo, commit, 4000, [])
    assert repo_map.build_map(repo, commit, 4000, []) == first
    result = json.loads(first)
    assert result["tier"] == "full"
    assert result["parser"] == "bundle"
    assert result["parser_provenance"]["grammars"] == [
        {"name": "python", "version": "0.25.0", "abi": 15, "sha256": "1" * 64}
    ]
    # Only files with a grammar extension are sent to the worker.
    sent = requests[0]["paths"]
    assert isinstance(sent, dict)
    assert sorted(sent) == ["alpha.py", "beta.py", "helper.py", "main.py"]
    assert [item["path"] for item in result["files"]] == [
        "helper.py",
        "alpha.py",
        "beta.py",
        "main.py",
        "notes.txt",
    ]
    assert result["files"][0] == {
        "path": "helper.py",
        "signatures": ["def render(value: int) -> str"],
        "parser_status": "ok",
    }
    assert result["files"][-1] == {
        "path": "notes.txt",
        "signatures": [],
        "parser_status": "ok",
    }
    assert result["edges"] == [
        {
            "source": "main.py",
            "target": "helper.py",
            "kind": "import",
            "confidence": "high",
        }
    ]


def test_repo_map_resolves_relative_package_imports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "project"
    commit = _commit_files(
        repo,
        {
            "package/__init__.py": "",
            "package/helper.py": "def run() -> None: pass\n",
            "package/consumer.py": "from . import helper\n",
        },
    )
    _fake_python_bundle(
        monkeypatch, {"package/consumer.py": _facts(imports=[("", 1, ["helper"])])}
    )

    result = json.loads(repo_map.build_map(repo, commit, 4000, []))
    assert {
        "source": "package/consumer.py",
        "target": "package/helper.py",
        "kind": "import",
        "confidence": "high",
    } in result["edges"]


def test_repo_map_ranks_normalized_seeds_and_definition_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "project"
    sources = {
        "seed.py": "from shared import target\n\ntarget()\n",
        "shared.py": "def target() -> None: pass\n",
        "medium_user.py": "target()\n",
        "first.py": "def ambiguous() -> None: pass\n",
        "second.py": "def ambiguous() -> None: pass\n",
        "low_user.py": "ambiguous()\n",
        "common_user.py": "common()\n",
        "private.py": "def hidden() -> None: pass\n",
    }
    facts = {
        "seed.py": _facts(imports=[("shared", 0, ["target"])], references=["target"]),
        "shared.py": _facts(definitions=["target"]),
        "medium_user.py": _facts(references=["target"]),
        "first.py": _facts(definitions=["ambiguous"]),
        "second.py": _facts(definitions=["ambiguous"]),
        "low_user.py": _facts(references=["ambiguous"]),
        "common_user.py": _facts(references=["common"]),
        "private.py": _facts(definitions=["hidden"]),
    }
    for index in range(5):
        sources[f"common_{index}.py"] = "def common() -> None: pass\n"
        facts[f"common_{index}.py"] = _facts(
            definitions=["common"], references=["common"] if index == 0 else []
        )
    commit = _commit_files(repo, sources)
    _fake_python_bundle(monkeypatch, facts)
    policy_path = tmp_path / "orchestration.json"
    policy_path.write_text(
        json.dumps({"repo_map_policy": {"deny_paths": ["private.py"]}})
    )
    policy = repo_map.load_policy(policy_path, explicit=True)

    normalized = json.loads(
        repo_map.build_map(
            repo,
            commit,
            4000,
            ["missing.py", "seed.py", "seed.py", "private.py"],
            policy,
        )
    )
    assert normalized == json.loads(
        repo_map.build_map(repo, commit, 4000, ["seed.py"], policy)
    )
    assert [item["path"] for item in normalized["files"]] == [
        "seed.py",
        "shared.py",
        "medium_user.py",
        "common_0.py",
        "common_1.py",
        "common_2.py",
        "common_3.py",
        "common_4.py",
        "common_user.py",
        "first.py",
        "low_user.py",
        "second.py",
    ]
    assert normalized["edges"] == [
        {
            "source": "low_user.py",
            "target": "first.py",
            "kind": "ambiguous-name-ref",
            "confidence": "low",
        },
        {
            "source": "low_user.py",
            "target": "second.py",
            "kind": "ambiguous-name-ref",
            "confidence": "low",
        },
        {
            "source": "medium_user.py",
            "target": "shared.py",
            "kind": "unique-name-ref",
            "confidence": "medium",
        },
        {
            "source": "seed.py",
            "target": "shared.py",
            "kind": "import",
            "confidence": "high",
        },
        {
            "source": "seed.py",
            "target": "shared.py",
            "kind": "unique-name-ref",
            "confidence": "medium",
        },
    ]
    assert not any(
        edge["source"] == "common_0.py" and edge["kind"].endswith("name-ref")
        for edge in normalized["edges"]
    )


def test_repo_map_enforces_project_policy_and_records_provenance(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "project"
    (repo / "src").mkdir(parents=True)
    (repo / "private").mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "src" / "public.py").write_text("def public() -> None: pass\n")
    (repo / "private" / "hidden.py").write_text("def hidden() -> None: pass\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "fixture")
    commit = _git(repo, "rev-parse", "HEAD")
    policy = tmp_path / "orchestration.json"
    policy.write_text(
        json.dumps(
            {
                "repo_map_policy": {
                    "allow_paths": ["src/**", "private/**"],
                    "redact_paths": ["private/**"],
                    "max_file_bytes": 1024,
                    "max_files": 10,
                    "timeout_seconds": 5,
                    "max_tokens": 500,
                }
            }
        )
    )

    result = json.loads(
        subprocess.check_output(
            [
                sys.executable,
                str(CLI),
                "--repo",
                str(repo),
                "--commit",
                commit,
                "--policy",
                str(policy),
            ]
        )
    )
    assert [item["path"] for item in result["files"]] == ["src/public.py"]
    assert result["parser_provenance"]["policy_sha256"]
    assert result["parser_provenance"]["policy_mode"] == "enforced"
    assert result["parser_provenance"]["timeout_seconds"] == 5
    assert result["parser_provenance"]["max_tokens"] == 500
    assert result["estimated_tokens"] <= 500


def test_repo_map_reports_unparseable_and_oversized_approved_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "project"
    commit = _commit_files(
        repo,
        {"broken.py": "def broken(:\n", "large.py": "x = '" + "a" * 200 + "'\n"},
    )
    requests = _fake_python_bundle(
        monkeypatch, {"broken.py": _facts(status="syntax_error")}
    )
    policy_path = tmp_path / "orchestration.json"
    policy_path.write_text(json.dumps({"repo_map_policy": {"max_file_bytes": 40}}))

    result = json.loads(
        repo_map.build_map(
            repo, commit, 4000, [], repo_map.load_policy(policy_path, explicit=True)
        )
    )
    # An oversized file is never read, so it is never sent to the worker either.
    sent = requests[0]["paths"]
    assert isinstance(sent, dict) and list(sent) == ["broken.py"]
    statuses = {item["path"]: item["parser_status"] for item in result["files"]}
    assert statuses == {"broken.py": "syntax_error", "large.py": "too_large"}
    assert result["diagnostics"] == [
        {"code": "syntax_error", "path": "broken.py"},
        {"code": "file_too_large", "path": "large.py"},
    ]


def test_repo_map_uses_project_orchestration_policy_when_present(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "project"
    (repo / "src").mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "src" / "safe.py").write_text("def safe() -> None: pass\n")
    (repo / "outside.py").write_text("def outside() -> None: pass\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "fixture")
    commit = _git(repo, "rev-parse", "HEAD")
    harness_dir = repo / ".harness"
    harness_dir.mkdir()
    (harness_dir / "orchestration.json").write_text(
        json.dumps({"repo_map_policy": {"allow_paths": ["src/**"]}})
    )

    result = json.loads(
        subprocess.check_output(
            [sys.executable, str(CLI), "--repo", str(repo), "--commit", commit]
        )
    )
    assert [item["path"] for item in result["files"]] == ["src/safe.py"]
    assert result["parser_provenance"]["policy_mode"] == "enforced"


def test_repo_map_budget_prefers_cli_then_policy_then_default(tmp_path: Path) -> None:
    repo = tmp_path / "project"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "main.py").write_text("def main() -> None: pass\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "fixture")
    commit = _git(repo, "rev-parse", "HEAD")
    policy = tmp_path / "orchestration.json"
    policy.write_text(json.dumps({"repo_map_policy": {"max_tokens": 500}}))
    command = [
        sys.executable,
        str(CLI),
        "--repo",
        str(repo),
        "--commit",
        commit,
        "--policy",
        str(policy),
    ]

    policy_result = json.loads(subprocess.check_output(command))
    cli_result = json.loads(subprocess.check_output(command + ["--max-tokens", "450"]))
    assert policy_result["estimated_tokens"] <= 500
    assert cli_result["estimated_tokens"] <= 450

    invalid = subprocess.run(
        command + ["--max-tokens", "0"], capture_output=True, text=True, check=False
    )
    assert invalid.returncode != 0
    assert "REMEDY:" in invalid.stderr


def test_repo_map_applies_symbol_and_length_redaction_before_serialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "project"
    commit = _commit_files(repo, {"public.py": "def visible(value: int) -> int: ...\n"})
    _fake_python_bundle(
        monkeypatch,
        {
            "public.py": _facts(
                signatures=[
                    ("def visible(value: int) -> int", ["visible", "value", "int"]),
                    (
                        "def hidden_symbol(value: int) -> int",
                        ["hidden_symbol", "value", "int"],
                    ),
                    (
                        "def shown(arg: hidden_type) -> int",
                        ["shown", "arg", "hidden_type", "int"],
                    ),
                    ("def " + "long" * 10 + "()", ["long" * 10]),
                    ("def wide(" + "a, " * 20 + "b)", ["wide", "a", "b"]),
                ],
                definitions=["visible", "hidden_symbol"],
            )
        },
    )
    policy_path = tmp_path / "orchestration.json"
    policy_path.write_text(
        json.dumps(
            {
                "repo_map_policy": {
                    "redact_symbols": ["hidden_*"],
                    "max_path_length": 20,
                    "max_symbol_length": 32,
                    "max_signature_length": 40,
                }
            }
        )
    )
    result = json.loads(
        repo_map.build_map(
            repo, commit, 4000, [], repo_map.load_policy(policy_path, explicit=True)
        )
    )
    # Symbol redaction, symbol length, and signature length each drop a whole signature.
    assert result["files"][0]["signatures"] == ["def visible(value: int) -> int"]
    encoded = json.dumps(result, ensure_ascii=False)
    assert "hidden_" not in encoded
    assert all(len(item["path"]) <= 20 for item in result["files"])
    assert all(
        len(signature) <= 40
        for item in result["files"]
        for signature in item["signatures"]
    )


def test_repo_map_minimal_tier_is_path_only_and_policy_filtered(tmp_path: Path) -> None:
    repo = tmp_path / "project"
    (repo / "src").mkdir(parents=True)
    (repo / "private").mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "src" / "safe.py").write_text("def safe() -> None: pass\n")
    (repo / "private" / "hidden.py").write_text("def hidden() -> None: pass\n")
    (repo / "outside.py").write_text("def outside() -> None: pass\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "fixture")
    commit = _git(repo, "rev-parse", "HEAD")
    policy = tmp_path / "orchestration.json"
    policy.write_text(
        json.dumps(
            {
                "repo_map_policy": {
                    "tier": "minimal",
                    "allow_paths": ["src/**", "private/**"],
                    "redact_paths": ["private/**"],
                }
            }
        )
    )
    result = json.loads(
        subprocess.check_output(
            [
                sys.executable,
                str(CLI),
                "--repo",
                str(repo),
                "--commit",
                commit,
                "--policy",
                str(policy),
            ]
        )
    )
    assert result["tier"] == "minimal"
    assert result["degradation_reason"]
    assert result["files"] == [{"path": "src/safe.py"}]
    assert result["edges"] == []
    assert result["diagnostics"] == []
    assert "private" not in json.dumps(result)
    assert "allow_paths" not in json.dumps(result)


def test_repo_map_invalid_policy_is_a_harness_error_with_remedy(tmp_path: Path) -> None:
    policy = tmp_path / "orchestration.json"
    policy.write_text(json.dumps({"repo_map_policy": {"max_files": 0}}))
    try:
        repo_map.load_policy(policy, explicit=True)
    except HarnessError as exc:
        assert exc.message.endswith("must be a positive integer")
        assert exc.remedy
    else:
        raise AssertionError("invalid Repo Map policy was accepted")


def test_repo_map_minimal_policy_never_acquires_a_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "project"
    commit = _commit_files(repo, {"main.py": "def main() -> None: pass\n"})

    def _refuse(**_: object) -> None:
        raise AssertionError("minimal tier must not look for a parser bundle")

    monkeypatch.setattr(parser_bundle, "acquire_bundle", _refuse)
    result = json.loads(
        repo_map.build_map(
            repo, commit, 4000, [], repo_map.RepoMapPolicy(tier="minimal")
        )
    )
    assert result["tier"] == "minimal"
    assert result["degradation_reason"] == "policy requested minimal tier"
    assert "bundle_mode" not in result["parser_provenance"]


def test_repo_map_redacts_signature_names_and_import_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "project"
    commit = _commit_files(
        repo,
        {
            "main.py": "import pkg.hidden_service\n",
            "pkg/__init__.py": "",
            "pkg/hidden_service.py": "def run(value: int) -> int: ...\n",
            "pkg/shown.py": "def show() -> None: ...\n",
        },
    )
    _fake_python_bundle(
        monkeypatch,
        {
            "main.py": _facts(
                signatures=[
                    ("def visible(value: int) -> int", ["visible", "value", "int"]),
                    (
                        "def public(hidden_arg: int) -> int",
                        ["public", "hidden_arg", "int"],
                    ),
                ],
                imports=[
                    ("pkg.hidden_service", 0, []),
                    ("pkg", 0, ["hidden_service", "shown"]),
                ],
                references=["hidden_service", "run"],
            ),
            "pkg/hidden_service.py": _facts(definitions=["run"]),
        },
    )
    policy_path = tmp_path / "orchestration.json"
    policy_path.write_text(
        json.dumps({"repo_map_policy": {"redact_symbols": ["hidden_*"]}})
    )

    result = json.loads(
        repo_map.build_map(
            repo, commit, 4000, [], repo_map.load_policy(policy_path, explicit=True)
        )
    )
    main_file = next(item for item in result["files"] if item["path"] == "main.py")
    assert main_file["signatures"] == ["def visible(value: int) -> int"]
    # A redacted module segment hides the import edge; the reference edge to `run` stays.
    assert {edge["target"] for edge in result["edges"] if edge["kind"] == "import"} == {
        "pkg/__init__.py",
        "pkg/shown.py",
    }
    assert "hidden_" not in json.dumps(
        [edge for edge in result["edges"] if edge["kind"] == "import"]
    )


def test_repo_map_rejects_malformed_tier_with_remedy(tmp_path: Path) -> None:
    policy = tmp_path / "orchestration.json"
    policy.write_text(json.dumps({"repo_map_policy": {"tier": ["minimal"]}}))
    try:
        repo_map.load_policy(policy, explicit=True)
    except HarnessError as exc:
        assert exc.message.endswith("must be one of: full, minimal")
        assert exc.remedy
    else:
        raise AssertionError("malformed tier was accepted")


def test_repo_map_rejects_removed_reduced_tier(tmp_path: Path) -> None:
    policy = tmp_path / "orchestration.json"
    policy.write_text(json.dumps({"repo_map_policy": {"tier": "reduced"}}))
    with pytest.raises(HarnessError) as caught:
        repo_map.load_policy(policy, explicit=True)
    assert caught.value.message.endswith("must be one of: full, minimal")
    assert caught.value.remedy


def test_repo_map_default_policy_tier_is_full() -> None:
    assert repo_map.RepoMapPolicy().tier == "full"


def _init_repo_with_stub_file(repo: Path) -> str:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "main.py").write_text("def run() -> None: pass\n")
    (repo / "widget.stub").write_text("widget contents\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "fixture")
    return _git(repo, "rev-parse", "HEAD")


def test_repo_map_full_tier_without_bundle_has_no_network_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "project"
    commit = _init_repo_with_stub_file(repo)
    policy = repo_map.RepoMapPolicy(tier="full")

    _forbid_network(monkeypatch)
    result = json.loads(repo_map.build_map(repo, commit, 4000, [], policy))

    assert result["tier"] == "minimal"
    assert result["parser"] == "path-only"
    assert all(set(item) == {"path"} for item in result["files"])
    assert result["degradation_reason"] == "offline parser bundle unavailable"


def test_repo_map_full_tier_missing_wheelhouse_for_pair_degrades(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "project"
    commit = _init_repo_with_stub_file(repo)
    bundle_dir = build_bundle_dir(tmp_path / "bundle", pair="cp1-nonexistent-platform")
    policy = repo_map.RepoMapPolicy(
        tier="full", parser_bundle_registry_paths=(str(bundle_dir),)
    )

    result = json.loads(repo_map.build_map(repo, commit, 4000, [], policy))

    assert result["tier"] == "minimal"
    assert result["parser"] == "path-only"
    assert all(set(item) == {"path"} for item in result["files"])
    assert (
        result["degradation_reason"]
        == "parser wheelhouse missing for interpreter/platform pair"
    )


def test_repo_map_full_tier_hash_mismatch_degrades(tmp_path: Path) -> None:
    repo = tmp_path / "project"
    commit = _init_repo_with_stub_file(repo)
    python_tag, platform_tag = parser_bundle.python_platform_tags(sys.executable, 30)
    pair = f"{python_tag}-{platform_tag}"
    bundle_dir = build_bundle_dir(tmp_path / "bundle", pair=pair)
    wheel_path = bundle_dir / "wheelhouse" / pair / "stubparser-1.0.0-py3-none-any.whl"
    wheel_path.write_bytes(b"corrupted")
    policy = repo_map.RepoMapPolicy(
        tier="full", parser_bundle_registry_paths=(str(bundle_dir),)
    )

    result = json.loads(repo_map.build_map(repo, commit, 4000, [], policy))

    assert result["tier"] == "minimal"
    assert result["parser"] == "path-only"
    assert all(set(item) == {"path"} for item in result["files"])
    assert result["degradation_reason"] == "parser bundle hash mismatch"


@pytest.mark.skipif(not _uv_available(), reason="uv is not on PATH")
def test_repo_map_full_tier_applies_bundle_and_leaves_repo_git_status_clean(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "project"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / ".gitignore").write_text(".harness/\n")
    (repo / "main.py").write_text("def run() -> None: pass\n")
    (repo / "widget.stub").write_text("widget contents\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "fixture")
    commit = _git(repo, "rev-parse", "HEAD")

    python_tag, platform_tag = parser_bundle.python_platform_tags(sys.executable, 30)
    pair = f"{python_tag}-{platform_tag}"
    bundle_dir = build_bundle_dir(tmp_path / "registry-bundle", pair=pair)

    policy_path = tmp_path / "orchestration.json"
    policy_path.write_text(
        json.dumps(
            {
                "repo_map_policy": {
                    "tier": "full",
                    "parser_bundle_registry_paths": [str(bundle_dir)],
                    "parser_bundle_timeout_seconds": 120,
                }
            }
        )
    )

    command = [
        sys.executable,
        str(CLI),
        "--repo",
        str(repo),
        "--commit",
        commit,
        "--policy",
        str(policy_path),
    ]
    result = json.loads(subprocess.check_output(command))

    assert result["tier"] == "full"
    assert result["parser"] == "bundle"
    assert result["degradation_reason"] == "parser bundle applied"
    provenance = result["parser_provenance"]
    assert provenance["bundle_mode"] == "applied"
    assert provenance["bundle_source"] == "internal-registry"
    assert provenance["python_tag"] == python_tag
    assert provenance["platform_tag"] == platform_tag
    assert provenance["lock_sha256"]
    assert provenance["script_hash"]
    assert provenance["core_version"] == "0.1.0"
    assert provenance["grammars"] == [
        {
            "name": "stub-lang",
            "version": "1.0.0",
            "abi": 14,
            "sha256": provenance["grammars"][0]["sha256"],
        }
    ]
    stub_file = next(item for item in result["files"] if item["path"] == "widget.stub")
    assert stub_file["parser_status"] == "ok"
    assert stub_file["signatures"]

    # No .venv, no requirements.txt, and no change visible to the mapped repository's own Git state.
    assert not (repo / ".venv").exists()
    assert not (repo / "requirements.txt").exists()
    assert _git(repo, "status", "--porcelain") == ""


def test_repo_map_hot_path_never_invokes_uv_run() -> None:
    for source_path in (
        ROOT / "harness" / "repo_map" / "repo_map.py",
        ROOT / "harness" / "repo_map" / "parser_bundle.py",
    ):
        assert "uv run" not in source_path.read_text(encoding="utf-8")
