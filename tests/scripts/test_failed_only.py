"""``scripts/verify.py --failed-only`` at the process seam: real pytest processes on a throwaway
project, with the slow mypy and clean-room stages replaced by small commands (ADR 0023, section 3).

The previous JUnit report of the worktree cache is the only source of the selection; ``--ff`` is
only an order; a vanished nodeid widens the selection to its test files; an empty selection is
``not_run``, never a pass."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.verification import cache
from tests.scripts.verify_project import (
    BAD_TEST,
    GOOD_TEST,
    install,
    read_document,
    run_verify,
    stream,
)

FAILED = [
    "tests/test_bad.py::TestGroup::test_member",
    "tests/test_bad.py::test_bad",
    "tests/test_bad.py::test_param[a/b]",
]
NOT_RUN_EXIT = 5


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project with one passing and one failing test file."""
    return install(
        monkeypatch, tmp_path, {"test_a.py": GOOD_TEST, "test_bad.py": BAD_TEST}
    )


def _first_failing_run() -> None:
    """The full run whose JUnit report lands in the worktree cache."""
    assert run_verify("--only", "tests", "--worktree-cache") == 1


def _pytest_stage(evidence: Path) -> tuple[str, str, int | None, list[str]]:
    stage = read_document(evidence).stages[0]
    assert stage.stage_id == "pytest" and len(read_document(evidence).stages) == 1
    return (
        stage.selection,
        stage.result,
        stage.exit_code,
        sorted(stage.failed_nodeids),
    )


def test_failed_first_is_an_order_and_still_runs_the_whole_suite(
    project: Path, tmp_path: Path
) -> None:
    evidence = tmp_path / "evidence"

    code = run_verify(
        "--only",
        "tests",
        "--failed-first",
        str(tmp_path / "ff"),
        "--evidence-dir",
        str(evidence),
    )

    assert code == 1
    stage = read_document(evidence).stages[1]
    assert stage.selection == "full"
    output = stream(evidence, 1)
    assert "3 failed" in output and "2 passed" in output


def test_failed_only_runs_exactly_the_failed_nodeids_of_the_previous_run(
    project: Path, tmp_path: Path
) -> None:
    _first_failing_run()
    evidence = tmp_path / "evidence"

    code = run_verify(
        "--worktree-cache", "--failed-only", "--evidence-dir", str(evidence)
    )

    assert code == 1
    selection, result, exit_code, failed = _pytest_stage(evidence)
    assert (selection, result, exit_code) == ("failed-only", "fail", 1)
    assert sorted(failed) == FAILED
    output = stream(evidence, 0)
    assert "3 failed" in output and "passed" not in output


def test_failed_only_narrows_as_failures_are_fixed(
    project: Path, tmp_path: Path
) -> None:
    _first_failing_run()
    (project / "tests" / "test_bad.py").write_text(
        BAD_TEST.replace("assert 0", "assert 1"), encoding="utf-8"
    )
    second = tmp_path / "second"
    third = tmp_path / "third"

    assert (
        run_verify("--worktree-cache", "--failed-only", "--evidence-dir", str(second))
        == 1
    )
    assert (
        run_verify("--worktree-cache", "--failed-only", "--evidence-dir", str(third))
        == 1
    )

    assert _pytest_stage(second)[3] == sorted(FAILED[:1] + FAILED[2:])
    assert "2 failed, 1 passed" in stream(second, 0)
    assert _pytest_stage(third)[3] == sorted(FAILED[:1] + FAILED[2:])
    assert "2 failed" in stream(third, 0) and "passed" not in stream(third, 0)


def test_a_vanished_nodeid_switches_the_selection_to_its_surviving_files(
    project: Path, tmp_path: Path
) -> None:
    _first_failing_run()
    (project / "tests" / "test_bad.py").write_text(
        BAD_TEST.replace("def test_bad()", "def test_renamed()")
        + "\n\ndef test_extra_ok():\n    assert True\n",
        encoding="utf-8",
    )
    evidence = tmp_path / "evidence"

    code = run_verify(
        "--worktree-cache", "--failed-only", "--evidence-dir", str(evidence)
    )

    assert code == 1
    selection, result, _, _ = _pytest_stage(evidence)
    assert (selection, result) == ("focused", "fail")
    output = stream(evidence, 0)
    # The whole surviving file ran, so a test the failure list never named ran too.
    assert "2 passed" in output and "test_a.py" not in output


def test_when_every_failed_file_is_gone_nothing_is_selected(
    project: Path, tmp_path: Path
) -> None:
    _first_failing_run()
    (project / "tests" / "test_bad.py").unlink()
    evidence = tmp_path / "evidence"

    code = run_verify(
        "--worktree-cache", "--failed-only", "--evidence-dir", str(evidence)
    )

    assert code == NOT_RUN_EXIT
    assert _pytest_stage(evidence)[1:3] == ("not_run", NOT_RUN_EXIT)


def test_an_empty_selection_is_not_run_and_never_a_pass(
    project: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (project / "tests" / "test_bad.py").unlink()
    assert run_verify("--only", "tests", "--worktree-cache") == 0
    capsys.readouterr()
    evidence = tmp_path / "evidence"

    code = run_verify(
        "--worktree-cache", "--failed-only", "--evidence-dir", str(evidence)
    )

    assert code == NOT_RUN_EXIT
    selection, result, exit_code, failed = _pytest_stage(evidence)
    assert (selection, result, exit_code, failed) == (
        "failed-only",
        "not_run",
        NOT_RUN_EXIT,
        [],
    )
    out = capsys.readouterr().out
    assert "not_run" in out and "verification passed" not in out
    assert "lists no failed test" in stream(evidence, 0, "stderr")


def test_no_previous_report_is_not_run(project: Path) -> None:
    assert run_verify("--worktree-cache", "--failed-only") == NOT_RUN_EXIT


def test_a_cache_of_another_environment_fingerprint_is_ignored(
    project: Path, tmp_path: Path
) -> None:
    _first_failing_run()
    lock = project / "uv.lock"
    lock.write_text("another environment\n", encoding="utf-8")

    assert run_verify("--worktree-cache", "--failed-only") == NOT_RUN_EXIT

    lock.write_text("lock-1\n", encoding="utf-8")
    evidence = tmp_path / "evidence"
    assert (
        run_verify("--worktree-cache", "--failed-only", "--evidence-dir", str(evidence))
        == 1
    )
    assert sorted(_pytest_stage(evidence)[3]) == FAILED


def test_a_report_of_another_worktree_is_never_read(
    project: Path, tmp_path: Path
) -> None:
    other = cache.open_cache(project, {}).directory.parent / "another-worktree"
    (other / cache.checkout_fingerprint(project)).mkdir(parents=True)
    (other / cache.checkout_fingerprint(project) / "junit.xml").write_text(
        "<testsuites><testsuite><testcase classname='tests.test_bad' name='test_bad'>"
        "<failure message='x'/></testcase></testsuite></testsuites>",
        encoding="utf-8",
    )

    assert run_verify("--worktree-cache", "--failed-only") == NOT_RUN_EXIT


def test_a_passing_failed_only_run_never_claims_a_verification_pass(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _first_failing_run()
    (project / "tests" / "test_bad.py").write_text(GOOD_TEST, encoding="utf-8")
    capsys.readouterr()

    code = run_verify("--worktree-cache", "--failed-only")

    out = capsys.readouterr().out
    assert code == 0
    assert "never authorizes a publish" in out
    assert "verification passed" not in out


def test_a_full_run_that_collects_no_test_is_not_run(
    project: Path, tmp_path: Path
) -> None:
    for name in ("test_a.py", "test_bad.py"):
        (project / "tests" / name).unlink()
    evidence = tmp_path / "evidence"

    code = run_verify("--only", "tests", "--evidence-dir", str(evidence))

    assert code == NOT_RUN_EXIT
    stage = read_document(evidence).stages[1]
    assert (stage.stage_id, stage.result, stage.exit_code) == (
        "pytest",
        "not_run",
        NOT_RUN_EXIT,
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["--failed-only"],
        ["--worktree-cache", "--failed-only", "--only", "tests"],
        ["--worktree-cache", "--failed-only", "--fail-fast"],
        ["--worktree-cache", "--failed-only", "--failed-first", "ff"],
    ],
)
def test_incompatible_options_are_refused_before_any_stage(
    project: Path, argv: list[str]
) -> None:
    assert run_verify(*argv) == 2
