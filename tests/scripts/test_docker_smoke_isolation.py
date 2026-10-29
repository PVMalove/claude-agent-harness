"""The real container smoke stays out of every default pytest run (#710).

pyproject.toml registers the `docker_smoke` marker and deselects it through `addopts`. Only an
explicit `-m docker_smoke` on the command line (the CI docker-smoke job) selects it, so
scripts/verify.py, `make test` and the diff-coverage gate never run it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from scripts import verify

ROOT = Path(__file__).resolve().parents[2]
PROBE = """\
import pytest


@pytest.mark.docker_smoke
def test_marked():
    pass


def test_plain():
    pass
"""


def _selected(tmp_path: Path, *selection: str) -> list[str]:
    """The test names one collection selects under the repository's pytest configuration."""
    probe = tmp_path / "test_probe.py"
    probe.write_text(PROBE, encoding="utf-8")
    run = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            str(ROOT / "pyproject.toml"),
            "--rootdir",
            str(tmp_path),
            "-p",
            "no:cacheprovider",
            # An unregistered marker would only warn; registration is part of the contract.
            "-W",
            "error::pytest.PytestUnknownMarkWarning",
            "--collect-only",
            "-q",
            *selection,
            str(probe),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    return sorted(
        line.rsplit("::", 1)[1] for line in run.stdout.splitlines() if "::" in line
    )


def test_the_repository_configuration_deselects_the_smoke_by_default(
    tmp_path: Path,
) -> None:
    assert _selected(tmp_path) == ["test_plain"]


def test_an_explicit_marker_selection_runs_only_the_smoke(tmp_path: Path) -> None:
    assert _selected(tmp_path, "-m", "docker_smoke") == ["test_marked"]


def test_verify_adds_no_marker_selection_of_its_own() -> None:
    command = verify.pytest_command(Path("/tmp/run"), platform="linux", cpu_count=4)

    flags = [index for index, word in enumerate(command) if word == "-m"]
    assert [command[index + 1] for index in flags] == ["pytest"]
