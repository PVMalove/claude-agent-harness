"""Behavior of the standalone Repo Map CLI at its process boundary."""

import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from _parser_bundle_fixtures import build_bundle_dir

from harness.errors import HarnessError
from harness.repo_map import parser_bundle, repo_map

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "harness" / "repo_map" / "repo_map.py"


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def _pip_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("pip") is not None


def _forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any socket construction or outbound connection fail the test immediately."""

    def _raise(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("unexpected network call during repo map build")

    monkeypatch.setattr(socket.socket, "__init__", _raise)
    monkeypatch.setattr(socket, "create_connection", _raise)


def test_repo_map_reads_commit_and_is_deterministic(
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
    (repo / "alpha.py").write_text("def alpha(): pass\n")
    (repo / "beta.py").write_text("def beta(): pass\n")
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
    assert result["tier"] == "reduced"
    assert result["parser"] == "ast-only"
    assert result["token_estimator_version"] == "utf8-bytes-per-2-v1"
    assert [item["path"] for item in result["files"]] == [
        "helper.py",
        "alpha.py",
        "beta.py",
        "main.py",
    ]
    assert result["files"][0]["signatures"] == ["def render(value: int) -> str"]
    assert result["edges"] == [
        {
            "source": "main.py",
            "target": "helper.py",
            "kind": "import",
            "confidence": "high",
        }
    ]
    assert (
        b"changed" not in first
        and b"hidden" not in first
        and b"private" not in first
        and b"ignored" not in first
        and b"leaked" not in first
    )

    bounded = json.loads(subprocess.check_output(command + ["--max-tokens", "500"]))
    assert bounded["estimated_tokens"] <= 500
    assert bounded["files"] == [] or bounded["files"][0]["path"] == "helper.py"

    # The default `reduced` tier never attempts a parser bundle or any network access.
    _forbid_network(monkeypatch)
    in_process = repo_map.build_map(repo, commit, 4000, [])
    assert json.loads(in_process)["tier"] == "reduced"


def test_repo_map_resolves_relative_package_imports(tmp_path: Path) -> None:
    repo = tmp_path / "project"
    package = repo / "package"
    package.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (package / "__init__.py").write_text("")
    (package / "helper.py").write_text("def run() -> None: pass\n")
    (package / "consumer.py").write_text("from . import helper\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "fixture")
    commit = _git(repo, "rev-parse", "HEAD")

    result = json.loads(
        subprocess.check_output(
            [sys.executable, str(CLI), "--repo", str(repo), "--commit", commit]
        )
    )
    assert {
        "source": "package/consumer.py",
        "target": "package/helper.py",
        "kind": "import",
        "confidence": "high",
    } in result["edges"]


def test_repo_map_ranks_normalized_seeds_and_definition_references(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "project"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "seed.py").write_text("from shared import target\n\ntarget()\n")
    (repo / "shared.py").write_text("def target() -> None: pass\n")
    (repo / "medium_user.py").write_text("target()\n")
    (repo / "first.py").write_text("def ambiguous() -> None: pass\n")
    (repo / "second.py").write_text("def ambiguous() -> None: pass\n")
    (repo / "low_user.py").write_text("ambiguous()\n")
    for index in range(5):
        source = "def common() -> None: pass\n"
        if index == 0:
            source += "common()\n"
        (repo / f"common_{index}.py").write_text(source)
    (repo / "common_user.py").write_text("common()\n")
    (repo / "private.py").write_text("def hidden() -> None: pass\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "fixture")
    commit = _git(repo, "rev-parse", "HEAD")
    policy = tmp_path / "orchestration.json"
    policy.write_text(json.dumps({"repo_map_policy": {"deny_paths": ["private.py"]}}))

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
    normalized = json.loads(
        subprocess.check_output(
            command
            + [
                "--seed",
                "missing.py",
                "--seed",
                "seed.py",
                "--seed",
                "seed.py",
                "--seed",
                "private.py",
            ]
        )
    )
    assert normalized == json.loads(
        subprocess.check_output(command + ["--seed", "seed.py"])
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
    tmp_path: Path,
) -> None:
    repo = tmp_path / "project"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "broken.py").write_text("def broken(:\n")
    (repo / "large.py").write_text("x = '" + "a" * 200 + "'\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "fixture")
    commit = _git(repo, "rev-parse", "HEAD")
    policy = tmp_path / "orchestration.json"
    policy.write_text(json.dumps({"repo_map_policy": {"max_file_bytes": 40}}))

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
    cli_result = json.loads(subprocess.check_output(command + ["--max-tokens", "300"]))
    assert policy_result["estimated_tokens"] <= 500
    assert cli_result["estimated_tokens"] <= 300

    invalid = subprocess.run(
        command + ["--max-tokens", "0"], capture_output=True, text=True
    )
    assert invalid.returncode != 0
    assert "REMEDY:" in invalid.stderr


def test_repo_map_applies_symbol_and_length_redaction_before_serialization(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "project"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "public.py").write_text(
        "# this comment must not be emitted\n"
        "def visible(value: int) -> int:\n"
        "    return value + 1\n"
        "\n"
        "def hidden_symbol(value: int) -> int:\n"
        "    return value + 2\n"
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "fixture")
    commit = _git(repo, "rev-parse", "HEAD")
    policy = tmp_path / "orchestration.json"
    policy.write_text(
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
    assert result["files"][0]["signatures"] == ["def visible(value: int) -> int"]
    encoded = json.dumps(result, ensure_ascii=False)
    assert "hidden_symbol" not in encoded
    assert "this comment" not in encoded
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


def test_repo_map_redacts_signature_names_and_import_edges(tmp_path: Path) -> None:
    repo = tmp_path / "project"
    (repo / "pkg").mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "main.py").write_text(
        "import pkg.hidden_service\n\n"
        "def visible(value: int) -> int:\n"
        "    return value\n\n"
        "def public(hidden_arg: int) -> int:\n"
        "    return hidden_service.run(hidden_arg)\n"
    )
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "hidden_service.py").write_text(
        "def run(value: int) -> int:\n" "    return value\n"
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "fixture")
    commit = _git(repo, "rev-parse", "HEAD")
    policy = tmp_path / "orchestration.json"
    policy.write_text(json.dumps({"repo_map_policy": {"redact_symbols": ["hidden_*"]}}))

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
    main_file = next(item for item in result["files"] if item["path"] == "main.py")
    assert main_file["signatures"] == ["def visible(value: int) -> int"]
    assert not any(
        edge["target"] == "pkg/hidden_service.py" for edge in result["edges"]
    )


def test_repo_map_rejects_malformed_tier_with_remedy(tmp_path: Path) -> None:
    policy = tmp_path / "orchestration.json"
    policy.write_text(json.dumps({"repo_map_policy": {"tier": ["minimal"]}}))
    try:
        repo_map.load_policy(policy, explicit=True)
    except HarnessError as exc:
        assert exc.message.endswith("must be one of: minimal, reduced, full")
        assert exc.remedy
    else:
        raise AssertionError("malformed tier was accepted")


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

    assert result["tier"] == "reduced"
    assert result["parser"] == "ast-only"
    assert result["degradation_reason"] == "offline parser bundle unavailable"


def test_repo_map_full_tier_missing_wheelhouse_for_pair_degrades(tmp_path: Path) -> None:
    repo = tmp_path / "project"
    commit = _init_repo_with_stub_file(repo)
    bundle_dir = build_bundle_dir(tmp_path / "bundle", pair="cp1-nonexistent-platform")
    policy = repo_map.RepoMapPolicy(
        tier="full", parser_bundle_registry_paths=(str(bundle_dir),)
    )

    result = json.loads(repo_map.build_map(repo, commit, 4000, [], policy))

    assert result["tier"] == "reduced"
    assert result["parser"] == "ast-only"
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

    assert result["tier"] == "reduced"
    assert result["parser"] == "ast-only"
    assert result["degradation_reason"] == "parser bundle hash mismatch"


@pytest.mark.skipif(not _pip_available(), reason="pip is not importable in this interpreter")
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
