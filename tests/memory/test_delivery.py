"""Candidate-installed memory and health run without the source repository."""

import json
import os
import subprocess
import sys
from pathlib import Path

CLI = Path(__file__).resolve().parents[2] / "harness/bin/harness.py"


def test_installed_memory_uses_shared_contract(tmp_path: Path) -> None:
    """The capability ships dependencies needed by runtime memory and project health."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    result = subprocess.run(
        [
            sys.executable,
            str(CLI),
            "init",
            str(tmp_path),
            "--capability",
            "pvmalove-suite",
            "--qa-gate-command",
            "true",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    code = """from pathlib import Path
import harness.memory
from harness.health.project_files import validate_project_json
problems = []
validate_project_json(Path.cwd(), problems)
assert not problems, problems
assert harness.memory.build(Path.cwd()) == {"status": "built", "indexed": 0}
"""
    environment = {**os.environ, "PYTHONPATH": ""}
    # The installed .harness directory is the package alias used by existing standalone tools.
    code = (
        "import importlib.util, sys\nspec = importlib.util.spec_from_file_location('harness', '.harness/__init__.py', submodule_search_locations=['.harness'])\nassert spec and spec.loader\npackage = importlib.util.module_from_spec(spec)\nsys.modules['harness'] = package\nspec.loader.exec_module(package)\n"
        + code
    )
    installed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert installed.returncode == 0, installed.stderr
    (tmp_path / "AGENTS.md").write_text("# Fixture instructions\n", encoding="utf-8")
    health = subprocess.run(
        [sys.executable, str(CLI), "health", str(tmp_path), "--json"],
        capture_output=True,
        text=True,
    )
    assert health.returncode == 0, [
        check["id"]
        for check in json.loads(health.stdout)["checks"]
        if check["status"] == "fail"
    ]
