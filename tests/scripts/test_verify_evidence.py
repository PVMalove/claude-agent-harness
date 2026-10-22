"""``scripts/verify.py --worktree-cache`` and ``--evidence-dir`` at the process seam: real stage
processes on a throwaway project, with the slow mypy and clean-room stages replaced by small
commands (ADR 0023, sections 4 and 5)."""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import pytest

from harness.gate_runner import stage_evidence
from scripts import verify
from scripts.verification import cache
from tests.scripts.verify_project import BAD_TEST, GOOD_TEST, make_repo

# Each fake stage writes to both streams and records the mypy cache directory it was given.
_FAKE_STAGE = (
    "import os, sys; print('{name}-out'); print('{name}-err', file=sys.stderr); "
    "print('MYPY_CACHE_DIR=' + os.environ.get('MYPY_CACHE_DIR', ''))"
)


def _fake(name: str, exit_code: int = 0) -> list[str]:
    return [
        sys.executable,
        "-c",
        _FAKE_STAGE.format(name=name) + f"; sys.exit({exit_code})",
    ]


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A committed project that ``verify`` verifies instead of this repository."""
    root = make_repo(tmp_path / "project", {"test_a.py": GOOD_TEST})
    for name in (cache.COLD_ENV, cache.JUNIT_ENV):
        monkeypatch.delenv(name, raising=False)
    outer = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    run_tmp = tmp_path / "run"
    run_tmp.mkdir()
    monkeypatch.setattr(verify, "ROOT", root)
    monkeypatch.setattr(verify, "_static_checks", lambda env: None)
    monkeypatch.setattr(
        verify,
        "_prepare_run_root",
        lambda: (run_tmp, verify.isolated_temp_env(outer, run_tmp)),
    )
    monkeypatch.setattr(verify, "mypy_command", lambda: _fake("mypy"))
    monkeypatch.setattr(verify, "clean_room_command", lambda: _fake("clean-room"))
    monkeypatch.setattr(os, "cpu_count", lambda: 1)
    return root


def _run(*argv: str) -> int:
    try:
        verify.main(list(argv))
    except SystemExit as stop:
        return stop.code if isinstance(stop.code, int) else 1
    return 0


def _document(directory: Path) -> stage_evidence.StageEvidenceDocument:
    return stage_evidence.parse(
        (directory / cache.EVIDENCE_FILE).read_text(encoding="utf-8")
    )


def _use_tests(project: Path, **files: str) -> None:
    for name, text in files.items():
        (project / "tests" / name).write_text(text, encoding="utf-8")


def test_a_passing_run_records_every_stage_with_separate_streams(
    project: Path, tmp_path: Path
) -> None:
    evidence = tmp_path / "evidence"

    assert _run("--evidence-dir", str(evidence)) == 0

    stages = _document(evidence).stages
    assert [(s.stage_id, s.kind, s.selection, s.result) for s in stages] == [
        ("mypy", "typecheck", "full", "pass"),
        ("pytest", "pytest", "full", "pass"),
        ("clean-room", "clean-room", "full", "pass"),
    ]
    head = cache.head_commit(project)
    for stage in stages:
        assert stage.candidate_sha == head
        assert stage.environment_fingerprint == cache.checkout_fingerprint(project)
        assert stage.exit_code == 0 and stage.duration_ms >= 0
        assert stage.stdout is not None and stage.stderr is not None
        assert stage.stdout.sha256 != stage.stderr.sha256
        for ref in (stage.stdout, stage.stderr):
            data = (evidence / ref.path).read_bytes()
            assert hashlib.sha256(data).hexdigest() == ref.sha256
    mypy = stages[0]
    assert mypy.stdout is not None and mypy.stderr is not None
    assert "mypy-out" in (evidence / mypy.stdout.path).read_text(encoding="utf-8")
    assert "mypy-err" not in (evidence / mypy.stdout.path).read_text(encoding="utf-8")
    assert "mypy-err" in (evidence / mypy.stderr.path).read_text(encoding="utf-8")


def test_failed_first_is_recorded_as_the_full_selection(
    project: Path, tmp_path: Path
) -> None:
    evidence = tmp_path / "evidence"

    assert (
        _run(
            "--only",
            "tests",
            "--failed-first",
            str(tmp_path / "ff"),
            "--evidence-dir",
            str(evidence),
        )
        == 0
    )

    pytest_stage = _document(evidence).stages[1]
    assert (pytest_stage.stage_id, pytest_stage.selection) == ("pytest", "full")
    assert (tmp_path / "ff").is_dir()


def test_a_failed_pytest_stage_lists_its_exact_nodeids_and_the_rest_is_not_run(
    project: Path, tmp_path: Path
) -> None:
    _use_tests(project, **{"test_bad.py": BAD_TEST})
    evidence = tmp_path / "evidence"

    assert _run("--evidence-dir", str(evidence)) == 1

    stages = _document(evidence).stages
    assert [(s.stage_id, s.result) for s in stages] == [
        ("mypy", "pass"),
        ("pytest", "fail"),
        ("clean-room", "not_run"),
    ]
    assert stages[1].exit_code == 1
    assert sorted(stages[1].failed_nodeids) == [
        "tests/test_bad.py::TestGroup::test_member",
        "tests/test_bad.py::test_bad",
        "tests/test_bad.py::test_param[a/b]",
    ]
    assert stages[2].exit_code is None and stages[2].stdout is None


def test_a_failed_mypy_stage_leaves_pytest_and_clean_room_not_run(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(verify, "mypy_command", lambda: _fake("mypy", 3))
    evidence = tmp_path / "evidence"

    assert _run("--evidence-dir", str(evidence)) == 3

    assert [
        (s.stage_id, s.result, s.exit_code) for s in _document(evidence).stages
    ] == [
        ("mypy", "fail", 3),
        ("pytest", "not_run", None),
        ("clean-room", "not_run", None),
    ]


def test_only_limits_the_recorded_stages(project: Path, tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"

    assert _run("--only", "clean-room", "--evidence-dir", str(evidence)) == 0

    assert [s.stage_id for s in _document(evidence).stages] == ["clean-room"]


def _seen(evidence: Path, stage: int) -> str:
    ref = _document(evidence).stages[stage].stdout
    assert ref is not None
    return (evidence / ref.path).read_text(encoding="utf-8")


def test_the_worktree_cache_holds_the_pytest_and_mypy_caches_and_the_junit_report(
    project: Path, tmp_path: Path
) -> None:
    _use_tests(project, **{"test_bad.py": BAD_TEST})
    evidence = tmp_path / "evidence"

    assert _run("--worktree-cache", "--evidence-dir", str(evidence)) == 1

    cache_dir = cache.open_cache(project, {}).directory
    assert (cache_dir / "pytest").is_dir() and (cache_dir / "junit.xml").is_file()
    assert f"MYPY_CACHE_DIR={cache_dir / 'mypy'}" in _seen(evidence, 0)


def test_the_clean_room_stage_never_sees_the_worktree_cache(
    project: Path, tmp_path: Path
) -> None:
    evidence = tmp_path / "evidence"

    assert _run("--worktree-cache", "--evidence-dir", str(evidence)) == 0

    cache_dir = cache.open_cache(project, {}).directory
    seen = _seen(evidence, 2)
    assert "MYPY_CACHE_DIR=" in seen and str(cache_dir) not in seen


def test_without_the_flag_the_stages_use_no_cache(
    project: Path, tmp_path: Path
) -> None:
    evidence = tmp_path / "evidence"

    assert _run("--evidence-dir", str(evidence)) == 0

    assert "worktree" not in _seen(evidence, 0)
    assert not (project / ".harness" / ".sandboxes" / "cache").exists()


def test_a_cold_environment_refuses_the_worktree_cache_before_any_stage(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(cache.COLD_ENV, "1")
    monkeypatch.setattr(
        verify, "_prepare_run_root", lambda: pytest.fail("a stage was prepared")
    )

    assert _run("--worktree-cache") == 2

    assert cache.COLD_ENV in capsys.readouterr().err
    assert not (project / ".harness" / ".sandboxes" / "cache").exists()


def test_the_worktree_cache_and_failed_first_both_own_the_pytest_cache(
    project: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run("--worktree-cache", "--failed-first", str(tmp_path / "ff")) == 2

    assert "pick one" in capsys.readouterr().err


def test_the_gate_junit_path_receives_the_pytest_report_without_any_flag(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = tmp_path / "gate" / "stage.junit.xml"
    monkeypatch.setenv(cache.JUNIT_ENV, str(report))

    assert _run("--only", "tests") == 0

    assert report.is_file() and "<testsuite" in report.read_text(encoding="utf-8")


def test_a_static_check_failure_still_leaves_evidence_with_every_stage_not_run(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(env: dict[str, str]) -> None:
        raise SystemExit(4)

    monkeypatch.setattr(verify, "_static_checks", broken)
    evidence = tmp_path / "evidence"

    assert _run("--evidence-dir", str(evidence)) == 4

    assert [(s.stage_id, s.result) for s in _document(evidence).stages] == [
        ("mypy", "not_run"),
        ("pytest", "not_run"),
        ("clean-room", "not_run"),
    ]
