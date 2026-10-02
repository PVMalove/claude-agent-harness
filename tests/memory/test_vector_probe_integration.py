"""Real native/model probe, opt-in locally and mandatory in experiment CI."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


def test_real_offline_vector_probe(tmp_path: Path) -> None:
    artifacts = os.environ.get("HARNESS_VECTOR_PROBE_ARTIFACTS")
    if not artifacts:
        if os.environ.get("HARNESS_VECTOR_PROBE_REQUIRED") == "1":
            pytest.fail("required probe needs HARNESS_VECTOR_PROBE_ARTIFACTS")
        pytest.skip("optional #431 experiment; set HARNESS_VECTOR_PROBE_ARTIFACTS")
    root = Path(__file__).resolve().parents[2]
    output = tmp_path / "probe.json"
    command = [
        sys.executable,
        str(root / "scripts/memory_vector_worker.py"),
        "--artifacts",
        artifacts,
        "--output",
        str(output),
    ]
    result = subprocess.run(
        command, cwd=root, capture_output=True, text=True, timeout=300
    )
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["sqlite_vec"]["known_vector_order"] == [1, 2, 3]
    assert data["smoke"]["dimension"] == 384
    assert data["smoke"]["languages"] == ["ru", "en"]
    assert data["fts5"] == data["baseline"]
    assert data["recommendation"] == "deferred"
