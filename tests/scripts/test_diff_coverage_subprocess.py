"""CLI subprocesses must contribute to the same coverage report as pytest."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]


def test_pytest_coverage_records_python_cli_subprocess(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    child = source / "worker.py"
    child.write_text("print('worker completed')\n", encoding="utf-8")
    test_file = tmp_path / "test_cli.py"
    test_file.write_text(
        "import subprocess, sys\n"
        "def test_worker():\n"
        f"    result = subprocess.run([sys.executable, {str(child)!r}], "
        "capture_output=True, text=True, check=True)\n"
        "    assert result.stdout == 'worker completed\\n'\n",
        encoding="utf-8",
    )
    report_path = tmp_path / "coverage.json"
    # This inner measurement owns its data and must not inherit an outer pytest-cov run.
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("COVERAGE_", "COV_CORE_"))
        and key != "PYTEST_ADDOPTS"
    }
    env["COVERAGE_FILE"] = str(tmp_path / ".coverage")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-c",
            str(ROOT / "pyproject.toml"),
            f"--cov={source}",
            f"--cov-config={ROOT / 'pyproject.toml'}",
            f"--cov-report=json:{report_path}",
            str(test_file),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    measured = next(
        data
        for path, data in report["files"].items()
        if Path(path).name == "worker.py"
    )
    assert 1 in measured["executed_lines"], measured
