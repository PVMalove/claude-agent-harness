#!/usr/bin/env python3
"""Выполнение проверок качества через выбранный политикой checkout с безопасными свидетельствами."""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..errors import INTERNAL_INVARIANT_REMEDY, HarnessError
from ..storage import storage_path

SENSITIVE_OUTPUT: tuple[
    tuple[re.Pattern[str], str],
    tuple[re.Pattern[str], str],
    tuple[re.Pattern[str], str],
] = (
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"), "<REDACTED_GITHUB_TOKEN>"),
    (
        re.compile(
            r"(?i)(\b(?:api[_-]?key|credential|token|password|secret)\s*(?:[:=]|is)\s*)\S+"
        ),
        r"\1<redacted>",
    ),
    (re.compile(r"(?i)(\bauthorization\s*:\s*(?:bearer\s+)?)\S+"), r"\1<redacted>"),
)


# Bound for one Git plumbing command of the clean checkout: `worktree add` checks out a full tree.
GIT_TIMEOUT_SECONDS = 300
_PYTHON_LAUNCHERS = frozenset({"python", "python3", "py"})
# The first word of a shell command when it is a bare token: no quote, escape or whitespace.
_BARE_FIRST_WORD = re.compile(r"\s*([^\s'\"\\]+)(?=\s|$)")


class GateRunnerError(HarnessError):
    """Политика не смогла безопасно подготовить запрошенный checkout."""

    partial_result: GateResult | None = None


class ExecutionPolicy(Protocol):
    """Выбор checkout и границы изоляции для одного выполнения проверок качества."""

    def checkout(self) -> AbstractContextManager[Path]:
        """Предоставить контекстный менеджер с рабочим каталогом checkout."""
        ...


def _run_git(command: list[str]) -> subprocess.CompletedProcess[str]:
    """Run one bounded Git command of the checkout preparation; an unlaunchable or hung Git is a gate error."""
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise GateRunnerError(
            f"Git did not finish within {GIT_TIMEOUT_SECONDS} seconds while preparing the clean QA checkout",
            remedy="check the repository for a held Git lock or an overloaded disk, then run the QA runner again",
        ) from exc
    except OSError as exc:
        raise GateRunnerError(
            f"could not run Git to prepare the clean QA checkout: {sanitise(str(exc))}",
            remedy="restore Git and its execution environment for the coordinator process, then run the QA runner again",
        ) from exc


@dataclass(frozen=True)
class LocalPolicy:
    """Запуск проверок качества в существующем checkout вызывающей стороны."""

    root: Path

    @contextmanager
    def checkout(self) -> Iterator[Path]:
        """Предоставить контекстный менеджер для локального checkout."""
        yield self.root.resolve()


@dataclass(frozen=True)
class CleanRoomPolicy:
    """Запуск проверок качества в изолированном worktree, привязанном к коммиту-кандидату."""

    repository: Path
    candidate_commit: str

    @contextmanager
    def checkout(self) -> Iterator[Path]:
        """Создать временное изолированное worktree для коммита-кандидата и удалить его при выходе."""
        if (
            not isinstance(self.candidate_commit, str)
            or re.fullmatch(r"[0-9a-fA-F]{7,64}", self.candidate_commit.strip()) is None
        ):
            raise GateRunnerError(
                "candidate_commit must be a hexadecimal commit SHA",
                remedy="pass candidate_commit as a 7-64 character hex commit SHA",
            )

        temporary_parent = storage_path(self.repository, "runs", "qa")
        try:
            temporary_parent.mkdir(parents=True, exist_ok=True)
            worktree_root = Path(
                tempfile.mkdtemp(prefix="agent-harness-qa-", dir=temporary_parent)
            )
        except OSError as exc:
            raise GateRunnerError(
                f"could not prepare clean QA checkout storage {temporary_parent}: {sanitise(str(exc))}",
                remedy=f"grant the coordinator process write access to {temporary_parent} and run the QA runner again",
            ) from exc
        checkout = worktree_root / "checkout"
        try:
            created: subprocess.CompletedProcess[str] = _run_git(
                [
                    "git",
                    "-C",
                    str(self.repository),
                    "worktree",
                    "add",
                    "--detach",
                    str(checkout),
                    "--",
                    self.candidate_commit,
                ],
            )
            if created.returncode != 0:
                detail: str = sanitise((created.stderr or created.stdout).strip())
                raise GateRunnerError(
                    f"could not create clean QA worktree: {detail or 'unknown error'}",
                    remedy=f"inspect the git worktree error above and fix the repository/candidate commit {self.candidate_commit} before retrying",
                )
            resolved: subprocess.CompletedProcess[str] = _run_git(
                ["git", "-C", str(checkout), "rev-parse", "--verify", "HEAD^{commit}"]
            )
            if (
                resolved.returncode != 0
                or resolved.stdout.strip() != self.candidate_commit
            ):
                raise GateRunnerError(
                    "clean QA worktree HEAD does not match the pinned candidate commit",
                    remedy=f"verify commit {self.candidate_commit} exists and resolves cleanly, then retry",
                )
            status: subprocess.CompletedProcess[str] = _run_git(
                [
                    "git",
                    "-C",
                    str(checkout),
                    "status",
                    "--porcelain",
                    "--untracked-files=all",
                ]
            )
            if status.returncode != 0 or status.stdout:
                raise GateRunnerError(
                    "clean QA worktree contains mutable files",
                    remedy=(
                        "inspect the 'git status' error above and fix the worktree/repository before retrying"
                        if status.returncode != 0
                        else "the freshly created clean-room worktree should start clean -- "
                        + INTERNAL_INVARIANT_REMEDY
                    ),
                )
            yield checkout
        finally:
            if checkout.exists():
                try:
                    _run_git(
                        [
                            "git",
                            "-C",
                            str(self.repository),
                            "worktree",
                            "remove",
                            "--force",
                            "--",
                            str(checkout),
                        ]
                    )
                except GateRunnerError:
                    # Best-effort cleanup must not mask the QA outcome; the directory is still
                    # removed below and `git worktree prune` drops the stale registration.
                    pass
            shutil.rmtree(worktree_root, ignore_errors=True)


@dataclass(frozen=True)
class GateResult:
    """Общие свидетельства QA для локальной и чистой (clean-room) политик."""

    checks: list[dict[str, str]]
    artifact: str
    duration_seconds: float

    @property
    def passed(self) -> bool:
        """Проверить, завершились ли все проверки успешно."""
        return all(check["result"] == "pass" for check in self.checks)


@dataclass(frozen=True)
class StageCommand:
    """One executed QA stage command: the approved text, what ran, its exit code and sanitised output."""

    command: str
    executed: str
    exit_code: int
    output: str


_LOG_COMMAND = re.compile(r"^\$ (.*)$")
_LOG_EXIT = re.compile(r"^exit_code=(-?\d+)$")


def format_command_log(command: str, returncode: int, output: str) -> str:
    """Сформировать блок команды в артефакте QA: $ <команда>, exit_code=<код> и вывод."""
    return f"$ {command}\nexit_code={returncode}\n{output}\n"


def parse_command_log(lines: list[str]) -> list[tuple[str, int]]:
    """Извлечь пары (команда, код возврата) для каждого блока из лога в порядке записи."""
    commands: list[tuple[str, int]] = []
    for index, line in enumerate(lines[:-1]):
        command = _LOG_COMMAND.match(line)
        exit_code = _LOG_EXIT.match(lines[index + 1]) if command else None
        if command is not None and exit_code is not None:
            commands.append((command.group(1), int(exit_code.group(1))))
    return commands


def project_python(checkout: Path) -> Path:
    """Выбрать детерминированный интерпретатор Python без обращения к PATH.

    Использует `.harness/.venv` в checkout (созданный через `make bootstrap`), иначе текущий интерпретатор.
    """
    venv_python = (
        checkout / ".harness" / ".venv" / "Scripts" / "python.exe"
        if sys.platform == "win32"
        else checkout / ".harness" / ".venv" / "bin" / "python"
    )
    if venv_python.is_file():
        return venv_python
    interpreter = Path(sys.executable)
    if interpreter.is_file():
        return interpreter
    raise GateRunnerError(
        "clean-room QA has no usable explicit Python interpreter",
        remedy="create the project's .harness/.venv before QA or run the coordinator with a valid Python interpreter",
    )


_clean_room_python = project_python


def _prepared_command(
    command: str | list[str], checkout: Path
) -> tuple[str | list[str], bool]:
    """Заменить системный вызов Python детерминированным перед выполнением команды.

    В строковой команде с голым первым словом-лаунчером заменяется только это слово, а остаток
    по-прежнему выполняет shell: операторы (`&&`, `|`, `;`), перенаправления и подстановки сохраняются.
    """
    original = command
    if isinstance(command, str):
        bare = _BARE_FIRST_WORD.match(command)
        if bare is not None and Path(bare.group(1)).name.lower() in _PYTHON_LAUNCHERS:
            interpreter = str(_clean_room_python(checkout))
            quoted = (
                subprocess.list2cmdline([interpreter])
                if sys.platform == "win32"
                else shlex.quote(interpreter)
            )
            return quoted + command[bare.end() :], True
        try:
            tokens = shlex.split(command)
        except ValueError:
            return command, True
        if not tokens:
            return command, True
        command = tokens
    if not command:
        return command, isinstance(command, str)
    launcher = Path(command[0]).name.lower()
    if launcher not in _PYTHON_LAUNCHERS:
        return original, isinstance(original, str)
    return [str(_clean_room_python(checkout)), *command[1:]], False


def sanitise(text: str) -> str:
    """Скрыть значения, похожие на секреты или токены, перед сохранением в артефакт."""
    for pattern, replacement in SENSITIVE_OUTPUT:
        text = pattern.sub(replacement, text)
    return text


def concise_evidence(text: str) -> str:
    """Сформировать краткую строку свидетельств из вывода команды."""
    lines: list[str] = [
        line.strip() for line in sanitise(text).splitlines() if line.strip()
    ]
    return lines[0][:240] if lines else "no output"


def _execute_commands(
    commands: Sequence[str | list[str]],
    checkout: Path,
    *,
    stop_on_failure: bool,
    checks: list[dict[str, str]],
    outputs: list[str],
    started: float,
    stage: list[StageCommand] | None = None,
) -> None:
    """Run commands in one checkout, appending each check, artifact block and optional stage record.

    ``checks`` and ``outputs`` are the caller's accumulators, so an unlaunchable command can still
    hand the evidence gathered so far back through ``GateRunnerError.partial_result``.
    """
    for command in commands:
        prepared, shell = _prepared_command(command, checkout)
        # The artifact shows what actually ran; the check keeps the approved command verbatim,
        # which is what a completion report is matched against.
        command_text = (
            prepared if isinstance(prepared, str) else subprocess.list2cmdline(prepared)
        )
        approved_text = (
            command if isinstance(command, str) else subprocess.list2cmdline(command)
        )
        try:
            result: subprocess.CompletedProcess[str] = subprocess.run(
                prepared,
                cwd=checkout,
                shell=shell,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except OSError as exc:
            failure = GateRunnerError(
                f"could not launch QA command: {sanitise(str(exc))}",
                remedy="restore the command execution environment before an explicit retry",
            )
            failure.partial_result = GateResult(
                checks, "\n".join(outputs), time.monotonic() - started
            )
            raise failure from exc
        combined: str = sanitise(
            (result.stdout or "")
            + ("\n" if result.stdout and result.stderr else "")
            + (result.stderr or "")
        )
        outputs.append(
            format_command_log(sanitise(command_text), result.returncode, combined)
        )
        checks.append(
            {
                "command": approved_text,
                "result": "pass" if result.returncode == 0 else "fail",
                "evidence": f"exit {result.returncode}; {concise_evidence(combined)}",
            }
        )
        if stage is not None:
            stage.append(
                StageCommand(
                    command=approved_text,
                    executed=sanitise(command_text),
                    exit_code=result.returncode,
                    output=combined,
                )
            )
        if result.returncode != 0 and stop_on_failure:
            break


def run_gate(
    commands: list[str | list[str]], policy: ExecutionPolicy, *, stop_on_failure: bool
) -> GateResult:
    """Выполнить набор команд и вернуть единый очищенный результат, независимый от политики."""
    checks: list[dict[str, str]] = []
    outputs: list[str] = []
    started: float = time.monotonic()
    with policy.checkout() as checkout:
        _execute_commands(
            commands,
            checkout,
            stop_on_failure=stop_on_failure,
            checks=checks,
            outputs=outputs,
            started=started,
        )
    return GateResult(
        checks=checks,
        artifact="\n".join(outputs),
        duration_seconds=time.monotonic() - started,
    )


QA_STAGE_PREPARATION = "preparation"
QA_STAGE_ENVIRONMENT_PROBE = "environment-probe"
QA_STAGE_PROJECT_FILE_CHECK = "project-file-check"
QA_STAGE_GATE = "gate"
CODE_CHECKS_STARTED = "started"
CODE_CHECKS_NOT_STARTED = "not_started"
CODE_CHECKS_UNKNOWN = "unknown"
DIAGNOSIS_INFRASTRUCTURE = "infrastructure"
DIAGNOSIS_PROJECT_DEFECT = "project-defect"
DIAGNOSIS_UNKNOWN = "unknown"
DIAGNOSTICS_LIMIT = 1_200

# Log signatures are only corroborating signals. A cause is confirmed by independent facts: the
# project's own environment probes and project-file checks (see ``diagnose``); neither an exit code
# nor a log signature decides a category alone.
_INFRASTRUCTURE_SIGNATURES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "network",
        re.compile(
            r"(?i)temporary failure in name resolution|could not resolve host|"
            r"name or service not known|network is unreachable|connection (?:timed out|reset|refused)|"
            r"tls handshake timeout|read timed out|\b(?:econnreset|etimedout|enotfound|eai_again)\b|"
            r"\b50[234]\b[^\n]{0,40}(?:gateway|unavailable|timeout)"
        ),
    ),
    (
        "resource",
        re.compile(
            r"(?i)no space left on device|disk quota exceeded|cannot allocate memory"
        ),
    ),
)
_PROJECT_DEFECT_SIGNATURES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "lock-file-incompatible",
        re.compile(
            r"(?i)lock ?file[^\n]{0,80}(?:out of (?:date|sync)|not up to date|needs to be updated|"
            r"incompatible|mismatch|not consistent)|"
            r"(?:out of sync|not in sync)[^\n]{0,80}lock|"
            r"can only install packages when your package\.json and package-lock\.json|"
            r"(?:poetry|uv|cargo)\.lock[^\n]{0,80}(?:out of date|needs to be updated|not consistent)|"
            r"frozen[- ]lockfile"
        ),
    ),
    (
        "unsatisfiable-requirement",
        re.compile(
            r"(?i)resolutionimpossible|conflicting dependencies|no solution found|"
            r"could not find a version that satisfies"
        ),
    ),
)
_PROJECT_FILE_NAMES = frozenset(
    {
        "package.json",
        "package-lock.json",
        "npm-shrinkwrap.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "pyproject.toml",
        "poetry.lock",
        "uv.lock",
        "pipfile",
        "pipfile.lock",
        "setup.py",
        "setup.cfg",
        "cargo.toml",
        "cargo.lock",
        "go.mod",
        "go.sum",
        "gemfile",
        "gemfile.lock",
        "composer.json",
        "composer.lock",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
    }
)


@dataclass(frozen=True)
class Diagnosis:
    """The cause of a failed preparation command: a confirmed category, or ``unknown``."""

    category: str
    signals: tuple[str, ...]
    basis: str

    def record(self) -> dict[str, object]:
        """The report-ready form of the diagnosis."""
        return {
            "category": self.category,
            "signals": list(self.signals),
            "basis": self.basis,
        }


def _project_file_names(tracked_files: tuple[str, ...]) -> frozenset[str]:
    """The manifest and lock-file base names the candidate actually tracks."""
    names: set[str] = set()
    for path in tracked_files:
        name = path.replace("\\", "/").rsplit("/", 1)[-1].lower()
        if name in _PROJECT_FILE_NAMES or (
            name.startswith("requirements") and name.endswith(".txt")
        ):
            names.add(name)
    return frozenset(names)


def diagnose(
    command: str,
    exit_code: int,
    output: str,
    tracked_files: tuple[str, ...],
    probes: Sequence[StageCommand] = (),
    file_checks: Sequence[StageCommand] = (),
) -> Diagnosis:
    """Classify one failed preparation command from independent facts (pure, no I/O).

    ``probes`` are the project's environment probes (for example a reachability check) and
    ``file_checks`` its offline project-file checks (for example a lock-file consistency check), both
    run after the preparation failure in the same checkout. An infrastructure cause needs a failed
    probe and at least one project-file check, every one of which passes: the environment is shown
    broken while the project's files are shown sound. A project defect needs a failing project-file
    check, or a defect log signature agreeing with a tracked project file the output names. A bare
    non-zero exit, a log keyword alone, an absent probe or any contradiction is ``unknown``; log
    signatures are only recorded as corroboration.
    """
    if exit_code == 0:
        return Diagnosis(
            DIAGNOSIS_UNKNOWN,
            (),
            f"`{command}` succeeded, so there is no failure to classify",
        )
    text = sanitise(output)
    infrastructure = [
        name for name, rule in _INFRASTRUCTURE_SIGNATURES if rule.search(text)
    ]
    defects = [name for name, rule in _PROJECT_DEFECT_SIGNATURES if rule.search(text)]
    lowered = text.lower()
    named = sorted(
        name for name in _project_file_names(tracked_files) if name in lowered
    )
    failed_probes = [item.command for item in probes if item.exit_code != 0]
    failed_checks = [item.command for item in file_checks if item.exit_code != 0]
    sound = bool(file_checks) and not failed_checks
    logged = [f"log:{n}" for n in (*infrastructure, *defects)]
    if infrastructure and defects:
        return Diagnosis(
            DIAGNOSIS_UNKNOWN,
            (f"exit-code:{exit_code}", *logged),
            "the output carries both an infrastructure and a project-defect signature",
        )
    if failed_probes and failed_checks:
        return Diagnosis(
            DIAGNOSIS_UNKNOWN,
            (
                f"exit-code:{exit_code}",
                *(f"environment-probe-failed:{c}" for c in failed_probes),
                *(f"project-file-check-failed:{c}" for c in failed_checks),
                *logged,
            ),
            "an environment probe and a project-file check both failed, so neither cause is isolated",
        )
    if failed_checks:
        return Diagnosis(
            DIAGNOSIS_PROJECT_DEFECT,
            (
                f"exit-code:{exit_code}",
                *(f"project-file-check-failed:{c}" for c in failed_checks),
                *logged,
            ),
            "a project-file check failed while no environment probe did",
        )
    if defects and named and not sound:
        return Diagnosis(
            DIAGNOSIS_PROJECT_DEFECT,
            (
                f"exit-code:{exit_code}",
                *(f"log:{n}" for n in defects),
                *(f"project-file:{n}" for n in named),
            ),
            "a project-defect signature agrees with a tracked project file the output names",
        )
    if failed_probes and sound and not defects:
        return Diagnosis(
            DIAGNOSIS_INFRASTRUCTURE,
            (
                f"exit-code:{exit_code}",
                *(f"environment-probe-failed:{c}" for c in failed_probes),
                f"project-file-checks-passed:{len(file_checks)}",
                *logged,
            ),
            "an environment probe failed while every project-file check passed",
        )
    return Diagnosis(
        DIAGNOSIS_UNKNOWN,
        (
            f"exit-code:{exit_code}",
            *(f"environment-probe-failed:{c}" for c in failed_probes),
            *(f"project-file:{n}" for n in named),
            *logged,
        ),
        "the facts do not confirm one cause; a non-zero exit or a keyword alone is not evidence",
    )


@dataclass(frozen=True)
class QAStagesResult:
    """Per-stage evidence of one QA run: preparation, then the gate, in one clean checkout."""

    stages: list[dict[str, object]]
    gate_checks: list[dict[str, str]]
    artifact: str
    duration_seconds: float
    failed_stage: str | None
    code_checks_started: str
    diagnosis: Diagnosis | None

    def record(self) -> dict[str, object]:
        """The ``qa_stages`` report field."""
        record: dict[str, object] = {
            "stages": self.stages,
            "failed_stage": self.failed_stage,
            "code_checks_started": self.code_checks_started,
        }
        if self.diagnosis is not None:
            record["diagnosis"] = self.diagnosis.record()
        return record


def _diagnostic_tail(output: str) -> str:
    """The sanitised, bounded last lines of a failed stage command's output."""
    lines = [line.rstrip() for line in sanitise(output).splitlines() if line.strip()]
    return "\n".join(lines[-12:])[-DIAGNOSTICS_LIMIT:] or "no output"


def _stage_record(stage: str, item: StageCommand) -> dict[str, object]:
    record: dict[str, object] = {
        "stage": stage,
        "command": item.command,
        "result": "pass" if item.exit_code == 0 else "fail",
        "exit_code": item.exit_code,
    }
    if item.executed != item.command:
        record["executed_command"] = item.executed
    if item.exit_code != 0:
        record["diagnostics"] = _diagnostic_tail(item.output)
    return record


def _tracked_files(checkout: Path) -> tuple[str, ...]:
    """Files the candidate tracks, or none when Git cannot list them (a log-based defect then stays unknown)."""
    try:
        listed = _run_git(["git", "-C", str(checkout), "ls-files"])
    except GateRunnerError:
        # The diagnosis is only corroboration: losing it must not discard the stage evidence.
        return ()
    return tuple(listed.stdout.splitlines()) if listed.returncode == 0 else ()


def run_qa_stages(
    preparation: Sequence[str | list[str]],
    gate: Sequence[str | list[str]],
    policy: ExecutionPolicy,
    *,
    environment_probes: Sequence[str | list[str]] = (),
    project_file_checks: Sequence[str | list[str]] = (),
) -> QAStagesResult:
    """Run preparation, then the gate, in one checkout, stopping at the first failing stage.

    A failed preparation command stops the run before any gate command starts, so the result states
    that no code check ran; the gate is never recorded as failed. Only then are the environment
    probes and project-file checks run, in the same checkout, to diagnose the failure.
    """
    checks: list[dict[str, str]] = []
    outputs: list[str] = []
    started = time.monotonic()
    stages: list[dict[str, object]] = []
    with policy.checkout() as checkout:
        prepared: list[StageCommand] = []
        _execute_commands(
            preparation,
            checkout,
            stop_on_failure=True,
            checks=[],
            outputs=outputs,
            started=started,
            stage=prepared,
        )
        stages.extend(_stage_record(QA_STAGE_PREPARATION, item) for item in prepared)
        broken = next((item for item in prepared if item.exit_code != 0), None)
        if broken is not None:
            probed: list[StageCommand] = []
            checked: list[StageCommand] = []
            for commands, found in (
                (environment_probes, probed),
                (project_file_checks, checked),
            ):
                _execute_commands(
                    commands,
                    checkout,
                    stop_on_failure=False,
                    checks=[],
                    outputs=outputs,
                    started=started,
                    stage=found,
                )
            stages.extend(_stage_record(QA_STAGE_ENVIRONMENT_PROBE, i) for i in probed)
            stages.extend(
                _stage_record(QA_STAGE_PROJECT_FILE_CHECK, i) for i in checked
            )
            return QAStagesResult(
                stages=stages,
                gate_checks=[],
                artifact="\n".join(outputs),
                duration_seconds=time.monotonic() - started,
                failed_stage=QA_STAGE_PREPARATION,
                code_checks_started=CODE_CHECKS_NOT_STARTED,
                diagnosis=diagnose(
                    broken.command,
                    broken.exit_code,
                    broken.output,
                    _tracked_files(checkout),
                    probed,
                    checked,
                ),
            )
        ran: list[StageCommand] = []
        _execute_commands(
            gate,
            checkout,
            stop_on_failure=True,
            checks=checks,
            outputs=outputs,
            started=started,
            stage=ran,
        )
        stages.extend(_stage_record(QA_STAGE_GATE, item) for item in ran)
    failed = any(item.exit_code != 0 for item in ran)
    return QAStagesResult(
        stages=stages,
        gate_checks=checks,
        artifact="\n".join(outputs),
        duration_seconds=time.monotonic() - started,
        failed_stage=QA_STAGE_GATE if failed else None,
        code_checks_started=CODE_CHECKS_STARTED if ran else CODE_CHECKS_UNKNOWN,
        diagnosis=None,
    )
