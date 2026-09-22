"""Behavior of the standalone Repo Map CLI at its process boundary."""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "harness" / "repo_map" / "repo_map.py"


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def test_repo_map_reads_commit_and_is_deterministic(tmp_path: Path) -> None:
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
        "helper.py", "alpha.py", "beta.py", "main.py"
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


def test_repo_map_ranks_normalized_seeds_and_definition_references(tmp_path: Path) -> None:
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
    policy.write_text(
        json.dumps({"repo_map_policy": {"deny_paths": ["private.py"]}})
    )

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
    assert normalized == json.loads(subprocess.check_output(command + ["--seed", "seed.py"]))
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


def test_repo_map_enforces_project_policy_and_records_provenance(tmp_path: Path) -> None:
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
    assert result["estimated_tokens"] <= 500


def test_repo_map_reports_unparseable_and_oversized_approved_files(tmp_path: Path) -> None:
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


def test_repo_map_uses_project_orchestration_policy_when_present(tmp_path: Path) -> None:
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
