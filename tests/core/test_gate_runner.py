#!/usr/bin/env python3
"""Focused public-contract tests for the shared quality-gate runner."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Protocol, cast
from unittest import mock

import pytest

from harness.storage import storage_path

from harness.errors import HarnessError
from harness.gate_runner.gate_runner import (
    CleanRoomPolicy,
    GateRunnerError,
    LocalPolicy,
    _clean_room_python,
    diagnose,
    run_gate,
    StageCommand,
    run_qa_stages,
)


class _RunFn(Protocol):
    """Протокол вызываемого объекта для запуска команд subprocess."""

    def __call__(
        self, command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        """Выполнить команду subprocess и вернуть результат."""
        ...


class GateRunnerTests(unittest.TestCase):
    """Набор тестов для выполнения проверок (гейтов) через GateRunner."""

    def test_clean_room_python_command_ignores_a_broken_path_launcher(self) -> None:
        """Проверить, что запуск python в clean room игнорирует поврежденный лаунчер из PATH."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            root = Path(temporary)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Gate Runner Test"],
                cwd=repo,
                check=True,
            )
            (repo / "check.py").write_text(
                "print('valid interpreter')\n", encoding="utf-8"
            )
            subprocess.run(["git", "add", "check.py"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "test: pin candidate"], cwd=repo, check=True
            )
            candidate = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            broken_bin = root / "broken-bin"
            broken_bin.mkdir()
            broken_python = broken_bin / "python"
            broken_python.write_text("#!/bin/sh\nexit 73\n", encoding="utf-8")
            broken_python.chmod(0o755)
            original_path = os.environ["PATH"]
            self.addCleanup(os.environ.__setitem__, "PATH", original_path)
            os.environ["PATH"] = f"{broken_bin}{os.pathsep}{original_path}"

            result = run_gate(
                [["python", "check.py"]],
                CleanRoomPolicy(repo, candidate),
                stop_on_failure=True,
            )
            string_result = run_gate(
                ["python check.py"],
                CleanRoomPolicy(repo, candidate),
                stop_on_failure=True,
            )

        self.assertEqual(result.checks[0]["result"], "pass")
        self.assertIn("valid interpreter", result.artifact)
        self.assertNotIn("$ python check.py", result.artifact)
        self.assertEqual(string_result.checks[0]["result"], "pass")
        self.assertNotIn("$ python check.py", string_result.artifact)
        # The evidence keeps the approved command verbatim: the coordinator matches checks_run
        # against the brief's verification_commands, not against the interpreter actually used.
        self.assertEqual(result.checks[0]["command"], "python check.py")
        self.assertEqual(string_result.checks[0]["command"], "python check.py")

    def test_local_and_clean_room_return_the_same_sanitised_evidence_shape(
        self,
    ) -> None:
        """Проверить, что локальный и clean-room режимы возвращают одинаковую очищенную структуру подтверждений."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            root = Path(temporary)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Gate Runner Test"], cwd=repo, check=True
            )
            tracked = repo / "candidate.txt"
            tracked.write_text("candidate\n", encoding="utf-8")
            subprocess.run(["git", "add", "candidate.txt"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "test: pin candidate"], cwd=repo, check=True
            )
            candidate = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            tracked.write_text("dirty checkout\n", encoding="utf-8")

            command = f"{sys.executable} -c \"from pathlib import Path; print(Path('candidate.txt').read_text().strip()); print('token=visible')\""
            local = run_gate([command], LocalPolicy(repo), stop_on_failure=True)
            clean_room = run_gate(
                [command], CleanRoomPolicy(repo, candidate), stop_on_failure=False
            )

        self.assertEqual(
            [set(check) for check in local.checks],
            [set(check) for check in clean_room.checks],
        )
        self.assertEqual(set(local.checks[0]), {"command", "result", "evidence"})
        self.assertEqual(clean_room.checks[0]["result"], "pass")
        self.assertIn("candidate", clean_room.artifact)
        self.assertNotIn("dirty checkout", clean_room.artifact)
        for result in (local, clean_room):
            self.assertIn("token=<redacted>", result.artifact)
            self.assertNotIn("token=visible", result.artifact)
            self.assertNotIn("token=visible", result.checks[0]["evidence"])

    def test_stops_at_first_failure_when_the_policy_requests_it(self) -> None:
        """Проверить остановку проверок на первой ошибке при включенном stop_on_failure."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            root = Path(temporary)
            failed = f"{sys.executable} -c \"import sys; print('password=visible'); sys.exit(7)\""
            skipped = f"{sys.executable} -c \"print('must not run')\""
            result = run_gate(
                [failed, skipped], LocalPolicy(root), stop_on_failure=True
            )

        self.assertEqual(len(result.checks), 1)
        self.assertEqual(result.checks[0]["result"], "fail")
        self.assertIn("password=<redacted>", result.artifact)
        self.assertNotIn("password=visible", result.artifact)

    def test_clean_room_failures_raise_a_harness_error_with_message_and_remedy(
        self,
    ) -> None:
        """Проверить, что ошибки в clean room вызывают HarnessError с сообщением и рекомендацией."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            repo = Path(temporary) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Gate Runner Test"], cwd=repo, check=True
            )
            (repo / "candidate.txt").write_text("candidate\n", encoding="utf-8")
            subprocess.run(["git", "add", "candidate.txt"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "test: pin candidate"], cwd=repo, check=True
            )
            candidate_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            unknown_commit = "0" * 40
            abbreviated_commit = candidate_commit[:12]

            for candidate, expected_message in (
                (unknown_commit, "could not create clean QA worktree"),
                (abbreviated_commit, "does not match the pinned candidate commit"),
            ):
                with self.subTest(candidate=candidate):
                    with self.assertRaises(GateRunnerError) as raised:
                        run_gate(
                            ["true"],
                            CleanRoomPolicy(repo, candidate),
                            stop_on_failure=True,
                        )
                    self.assertIsInstance(raised.exception, HarnessError)
                    self.assertIn(expected_message, raised.exception.message)
                    self.assertIn(candidate, raised.exception.remedy)

    def test_dirty_clean_room_worktree_raises_with_a_status_specific_remedy(
        self,
    ) -> None:
        """Проверить, что измененное рабочее дерево clean room вызывает ошибку со специфичной рекомендацией."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            repo = Path(temporary) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Gate Runner Test"], cwd=repo, check=True
            )
            (repo / "candidate.txt").write_text("candidate\n", encoding="utf-8")
            subprocess.run(["git", "add", "candidate.txt"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "test: pin candidate"], cwd=repo, check=True
            )
            candidate_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            real_run = cast(_RunFn, subprocess.run)

            def fake_status(status_result: subprocess.CompletedProcess[str]) -> _RunFn:
                """Создать функцию для имитации статуса git через subprocess.run."""

                def run(
                    command: list[str], **kwargs: object
                ) -> subprocess.CompletedProcess[str]:
                    """Обработчик команд для подмены результата git status."""
                    if "status" in command:
                        return status_result
                    return real_run(command, **kwargs)

                return run

            for status_result, expected_remedy in (
                (
                    subprocess.CompletedProcess([], 128, stdout="", stderr="fatal"),
                    "inspect the 'git status' error",
                ),
                (
                    subprocess.CompletedProcess(
                        [], 0, stdout="?? stray.txt\n", stderr=""
                    ),
                    "internal invariant violated",
                ),
            ):
                with self.subTest(returncode=status_result.returncode):
                    with (
                        mock.patch(
                            "subprocess.run", side_effect=fake_status(status_result)
                        ),
                        self.assertRaises(GateRunnerError) as raised,
                    ):
                        run_gate(
                            ["true"],
                            CleanRoomPolicy(repo, candidate_commit),
                            stop_on_failure=True,
                        )
                    self.assertIn("contains mutable files", raised.exception.message)
                    self.assertIn(expected_remedy, raised.exception.remedy)

    def test_clean_room_python_prefers_harness_venv_over_root_venv(self) -> None:
        """Проверить, что clean room python отдает предпочтение .harness/.venv перед корневым .venv."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            checkout = Path(temporary) / "checkout"
            checkout.mkdir()

            # Create .harness/.venv/bin/python (the contract path)
            harness_python = checkout / ".harness" / ".venv" / "bin" / "python"
            harness_python.parent.mkdir(parents=True)
            harness_python.write_text(
                "#!/bin/sh\necho harness-venv\n", encoding="utf-8"
            )
            harness_python.chmod(0o755)

            with mock.patch("harness.gate_runner.gate_runner.sys") as mock_sys:
                mock_sys.platform = "linux"
                mock_sys.executable = "/nonexistent/python3"
                result = _clean_room_python(checkout)
            self.assertEqual(result, harness_python)

    def test_clean_room_python_does_not_use_root_level_venv(self) -> None:
        """Проверить, что clean room python не использует корневой .venv каталога checkout."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            checkout = Path(temporary) / "checkout"
            checkout.mkdir()

            # Create ONLY root-level .venv (the WRONG path)
            wrong_python = checkout / ".venv" / "bin" / "python"
            wrong_python.parent.mkdir(parents=True)
            wrong_python.write_text("#!/bin/sh\necho wrong\n", encoding="utf-8")
            wrong_python.chmod(0o755)

            # With no .harness/.venv, should fall back to sys.executable, not root .venv
            result = _clean_room_python(checkout)
            self.assertNotEqual(
                result,
                wrong_python,
                "must not use root-level .venv — contract is .harness/.venv",
            )

    def test_clean_room_python_resolves_windows_path_under_harness_venv(self) -> None:
        """Проверить определение пути к python.exe на платформе Windows внутри .harness/.venv."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            checkout = Path(temporary) / "checkout"
            checkout.mkdir()

            win_python = checkout / ".harness" / ".venv" / "Scripts" / "python.exe"
            win_python.parent.mkdir(parents=True)
            win_python.write_text("fake", encoding="utf-8")

            with mock.patch("harness.gate_runner.gate_runner.sys") as mock_sys:
                mock_sys.platform = "win32"
                mock_sys.executable = "/nonexistent/python3"
                result = _clean_room_python(checkout)

            self.assertEqual(result, win_python)

    def test_clean_room_policy_rejects_invalid_candidate_commit(self) -> None:
        """Проверить отклонение некорректного хэша коммита-кандидата в CleanRoomPolicy."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            repo = Path(temporary) / "repo"
            repo.mkdir()
            for invalid in ("-v", "not-a-hex-sha", "", 123):
                with self.subTest(invalid=invalid):
                    policy = CleanRoomPolicy(repo, cast(str, invalid))
                    with self.assertRaises(GateRunnerError) as raised:
                        with policy.checkout():
                            pass
                    self.assertIn(
                        "candidate_commit must be a hexadecimal commit SHA",
                        raised.exception.message,
                    )


if __name__ == "__main__":
    unittest.main()


def test_command_log_round_trips_between_writer_and_reader() -> None:
    """Проверить согласованность записи и последующего парсинга лога команд QA."""
    from harness.gate_runner.gate_runner import format_command_log, parse_command_log

    log = format_command_log("make test", 0, "ok") + format_command_log(
        "make lint", 2, "E1\nE2"
    )

    assert parse_command_log(log.splitlines()) == [("make test", 0), ("make lint", 2)]


def _committed_repo(root: Path) -> tuple[Path, str]:
    repo = root / "repo"
    repo.mkdir()
    for command in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "test@example.invalid"],
        ["git", "config", "user.name", "Gate Runner Test"],
    ):
        subprocess.run(command, cwd=repo, check=True)
    (repo / "candidate.txt").write_text("candidate\n", encoding="utf-8")
    subprocess.run(["git", "add", "candidate.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "test: pin"], cwd=repo, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return repo, head


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="a privileged process is not denied by file permissions",
)
def test_an_unwritable_checkout_storage_is_a_gate_error_not_an_os_error(
    tmp_path: Path,
) -> None:
    repo, head = _committed_repo(tmp_path)
    harness = repo / ".harness"
    harness.mkdir()
    harness.chmod(0o555)
    try:
        with pytest.raises(GateRunnerError) as raised:
            run_gate(["true"], CleanRoomPolicy(repo, head), stop_on_failure=True)
    finally:
        harness.chmod(0o755)
    assert "could not prepare clean QA checkout storage" in raised.value.message
    assert "write access" in raised.value.remedy


def test_an_unlaunchable_git_is_a_gate_error_and_leaves_no_checkout_directory(
    tmp_path: Path,
) -> None:
    repo, head = _committed_repo(tmp_path)
    with mock.patch(
        "harness.gate_runner.gate_runner.subprocess.run",
        side_effect=FileNotFoundError("git"),
    ):
        with pytest.raises(GateRunnerError) as raised:
            run_gate(["true"], CleanRoomPolicy(repo, head), stop_on_failure=True)
    assert "could not run Git" in raised.value.message
    runs = storage_path(repo, "runs", "qa")
    assert not runs.is_dir() or list(runs.iterdir()) == []


def _py(code: str) -> str:
    return f'{sys.executable} -c "{code}"'


def test_qa_stages_run_preparation_then_the_gate_in_one_clean_checkout(
    tmp_path: Path,
) -> None:
    repo, head = _committed_repo(tmp_path)
    result = run_qa_stages(
        [_py("open('prepared.txt','w').write('x')")],
        [_py("import os; print(os.path.exists('prepared.txt'))")],
        CleanRoomPolicy(repo, head),
    )

    assert result.failed_stage is None
    assert result.code_checks_started == "started"
    assert [(stage["stage"], stage["result"]) for stage in result.stages] == [
        ("preparation", "pass"),
        ("gate", "pass"),
    ]
    # The gate saw the file the preparation wrote: both ran in the same checkout.
    assert "True" in result.artifact
    assert [check["result"] for check in result.gate_checks] == ["pass"]
    assert not (repo / "prepared.txt").exists()


def test_a_failed_preparation_stops_before_the_gate_and_records_no_code_check(
    tmp_path: Path,
) -> None:
    repo, head = _committed_repo(tmp_path)
    result = run_qa_stages(
        [
            _py(
                "import sys; print('curl: Could not resolve host: registry.example', "
                "file=sys.stderr); sys.exit(6)"
            ),
            _py("print('second preparation must not run')"),
        ],
        [_py("print('gate must not run')")],
        CleanRoomPolicy(repo, head),
    )

    assert result.failed_stage == "preparation"
    assert result.code_checks_started == "not_started"
    assert result.gate_checks == []
    assert len(result.stages) == 1
    assert result.stages[0]["exit_code"] == 6
    assert "Could not resolve host" in str(result.stages[0]["diagnostics"])
    assert "must not run" not in result.artifact
    assert result.diagnosis is not None
    # The log signature is only corroboration: with no probe, nothing confirms the cause.
    assert result.diagnosis.category == "unknown"


def test_a_failed_probe_with_sound_project_files_confirms_infrastructure(
    tmp_path: Path,
) -> None:
    repo, head = _committed_repo(tmp_path)
    result = run_qa_stages(
        [_py("import sys; sys.exit(1)")],
        [_py("print('gate must not run')")],
        CleanRoomPolicy(repo, head),
        environment_probes=[_py("import sys; sys.exit(7)")],
        project_file_checks=[_py("print('lock is consistent')")],
    )

    assert [(s["stage"], s["result"]) for s in result.stages] == [
        ("preparation", "fail"),
        ("environment-probe", "fail"),
        ("project-file-check", "pass"),
    ]
    assert result.failed_stage == "preparation"
    assert result.code_checks_started == "not_started"
    assert result.diagnosis is not None
    assert result.diagnosis.category == "infrastructure"
    assert "lock is consistent" in result.artifact
    assert "gate must not run" not in result.artifact


def test_probes_and_file_checks_do_not_run_when_preparation_passes(
    tmp_path: Path,
) -> None:
    repo, head = _committed_repo(tmp_path)
    result = run_qa_stages(
        [_py("print('ready')")],
        [_py("print('gate')")],
        CleanRoomPolicy(repo, head),
        environment_probes=[_py("print('probe must not run')")],
        project_file_checks=[_py("print('check must not run')")],
    )

    assert [s["stage"] for s in result.stages] == ["preparation", "gate"]
    assert "must not run" not in result.artifact


def test_a_failing_gate_command_keeps_the_gate_stage_failure(tmp_path: Path) -> None:
    repo, head = _committed_repo(tmp_path)
    result = run_qa_stages(
        [],
        [_py("import sys; sys.exit(3)"), _py("print('skipped')")],
        CleanRoomPolicy(repo, head),
    )

    assert result.failed_stage == "gate"
    assert result.code_checks_started == "started"
    assert result.diagnosis is None
    assert [check["result"] for check in result.gate_checks] == ["fail"]
    assert result.record()["failed_stage"] == "gate"


def test_stage_diagnostics_are_sanitised_and_bounded(tmp_path: Path) -> None:
    result = run_qa_stages(
        [_py("import sys; print('x' * 5000); print('token=visible'); sys.exit(1)")],
        [],
        LocalPolicy(tmp_path),
    )

    diagnostics = str(result.stages[0]["diagnostics"])
    assert "token=<redacted>" in diagnostics
    assert "token=visible" not in diagnostics
    assert len(diagnostics) <= 1_200


LOCK_FILES = ("package.json", "package-lock.json", "src/app.py")


def _fact(command: str, exit_code: int) -> StageCommand:
    return StageCommand(command, command, exit_code, "")


OUTAGE = "npm ERR! network request failed: getaddrinfo EAI_AGAIN registry.npmjs.org"
LOCK_MISMATCH = (
    "npm ERR! `npm ci` can only install packages when your package.json and "
    "package-lock.json are in sync"
)


def test_diagnose_confirms_infrastructure_from_a_failed_probe_and_sound_project_files() -> (
    None
):
    diagnosis = diagnose(
        "npm ci",
        1,
        OUTAGE,
        LOCK_FILES,
        [_fact("curl -sI https://registry.npmjs.org", 6)],
        [_fact("npm ls --package-lock-only", 0)],
    )
    assert diagnosis.category == "infrastructure"
    assert "environment-probe-failed:curl -sI https://registry.npmjs.org" in (
        diagnosis.signals
    )
    assert "project-file-checks-passed:1" in diagnosis.signals


def test_diagnose_never_confirms_infrastructure_from_the_log_alone() -> None:
    # The finding's cases: a keyword plus no named project file is no independent fact.
    assert diagnose("uv sync", 1, "Connection refused", ()).category == "unknown"
    assert diagnose("make deps", 2, "ECONNRESET", ("Makefile",)).category == "unknown"
    typo = "npm ERR! getaddrinfo ENOTFOUND registry.typo-example.com"
    assert diagnose("npm ci", 1, typo, LOCK_FILES).category == "unknown"
    # A failed probe without a passing project-file check does not isolate the cause either.
    probe = [_fact("curl registry", 6)]
    assert diagnose("npm ci", 1, OUTAGE, LOCK_FILES, probe).category == "unknown"
    # Nor does a passing probe with only a log signature.
    assert (
        diagnose(
            "npm ci",
            1,
            OUTAGE,
            LOCK_FILES,
            [_fact("curl registry", 0)],
            [_fact("c", 0)],
        ).category
        == "unknown"
    )


def test_diagnose_routes_a_failing_project_file_check_to_a_project_defect() -> None:
    diagnosis = diagnose("uv sync", 1, "", (), [], [_fact("uv lock --check", 1)])
    assert diagnosis.category == "project-defect"
    assert "project-file-check-failed:uv lock --check" in diagnosis.signals


def test_diagnose_treats_both_a_failed_probe_and_a_failed_check_as_unknown() -> None:
    diagnosis = diagnose(
        "npm ci",
        1,
        OUTAGE,
        LOCK_FILES,
        [_fact("curl registry", 6)],
        [_fact("npm ls", 1)],
    )
    assert diagnosis.category == "unknown"


def test_diagnose_confirms_a_project_defect_only_with_a_tracked_project_file() -> None:
    confirmed = diagnose("npm ci", 1, LOCK_MISMATCH, LOCK_FILES)
    assert confirmed.category == "project-defect"
    assert "project-file:package-lock.json" in confirmed.signals

    # The same words with no tracked project file to back them confirm nothing.
    assert diagnose("npm ci", 1, LOCK_MISMATCH, ("src/app.py",)).category == "unknown"


def test_diagnose_never_decides_from_an_exit_code_or_a_keyword_alone() -> None:
    assert diagnose("make setup", 2, "", LOCK_FILES).category == "unknown"
    assert (
        diagnose("make setup", 1, "something failed", LOCK_FILES).category == "unknown"
    )
    # A bare keyword with no matching signature is not evidence either.
    assert diagnose("make setup", 1, "network", LOCK_FILES).category == "unknown"


def test_diagnose_treats_contradicting_signals_as_unknown() -> None:
    mixed = "Could not resolve host\nlockfile is out of date"
    assert diagnose("uv sync", 1, mixed, LOCK_FILES).category == "unknown"
    # A log defect that a passing project-file check contradicts is not a confirmed defect.
    assert (
        diagnose(
            "npm ci", 1, LOCK_MISMATCH, LOCK_FILES, [], [_fact("npm ls", 0)]
        ).category
        == "unknown"
    )
    # An outage signature next to a named tracked project file is not an outage proof.
    implicated = "Connection timed out while reading package-lock.json"
    assert diagnose("npm ci", 1, implicated, LOCK_FILES).category == "unknown"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell operator syntax")
@pytest.mark.parametrize("operator", ["&&", ";"])
def test_a_python_string_command_keeps_its_shell_operators(
    tmp_path: Path, operator: str
) -> None:
    """A rewritten Python launcher must not swallow the rest of the shell command as its argv."""
    result = run_gate(
        [f"python -c \"print('first')\" {operator} echo $((40 + 2))"],
        LocalPolicy(tmp_path),
        stop_on_failure=True,
    )

    output_lines = result.artifact.splitlines()
    assert result.passed
    assert "first" in output_lines
    assert "42" in output_lines
    assert not output_lines[0].startswith("$ python ")


def _git_failing_run(failing: str, error: BaseException) -> _RunFn:
    """Return a subprocess.run stand-in that fails only the Git subcommand named by `failing`."""
    real_run = cast(_RunFn, subprocess.run)

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[0] == "git" and failing in command:
            raise error
        return real_run(command, **kwargs)

    return run


def test_a_hung_git_is_a_gate_error_and_leaves_no_checkout_directory(
    tmp_path: Path,
) -> None:
    repo, head = _committed_repo(tmp_path)
    with mock.patch(
        "harness.gate_runner.gate_runner.subprocess.run",
        side_effect=_git_failing_run(
            "worktree", subprocess.TimeoutExpired(["git"], 300)
        ),
    ):
        with pytest.raises(GateRunnerError) as raised:
            run_gate(["true"], CleanRoomPolicy(repo, head), stop_on_failure=True)
    assert "did not finish within" in raised.value.message
    assert "held Git lock" in raised.value.remedy
    runs = storage_path(repo, "runs", "qa")
    assert not runs.is_dir() or list(runs.iterdir()) == []


def test_a_failed_worktree_cleanup_does_not_mask_the_gate_result(
    tmp_path: Path,
) -> None:
    repo, head = _committed_repo(tmp_path)
    with mock.patch(
        "harness.gate_runner.gate_runner.subprocess.run",
        side_effect=_git_failing_run("remove", FileNotFoundError("git")),
    ):
        result = run_gate(["true"], CleanRoomPolicy(repo, head), stop_on_failure=True)
    assert result.passed
    runs = storage_path(repo, "runs", "qa")
    assert list(runs.iterdir()) == []


def test_an_unlaunchable_git_during_diagnosis_keeps_the_stage_evidence(
    tmp_path: Path,
) -> None:
    with mock.patch(
        "harness.gate_runner.gate_runner.subprocess.run",
        side_effect=_git_failing_run("ls-files", FileNotFoundError("git")),
    ):
        result = run_qa_stages(
            [_py("import sys; sys.exit(1)")],
            [_py("print('gate')")],
            LocalPolicy(tmp_path),
        )
    assert result.failed_stage == "preparation"
    assert result.code_checks_started == "not_started"
    assert result.diagnosis is not None
    assert result.diagnosis.category == "unknown"
