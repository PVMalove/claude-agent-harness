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

    bounded = json.loads(subprocess.check_output(command + ["--max-tokens", "180"]))
    assert bounded["estimated_tokens"] <= 180
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
