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
from collections.abc import Iterator
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


class GateRunnerError(HarnessError):
    """Политика не смогла безопасно подготовить запрошенный checkout."""

    partial_result: GateResult | None = None


class ExecutionPolicy(Protocol):
    """Выбор checkout и границы изоляции для одного выполнения проверок качества."""

    def checkout(self) -> AbstractContextManager[Path]:
        """Предоставить контекстный менеджер с рабочим каталогом checkout."""
        ...


def _run_git(command: list[str]) -> subprocess.CompletedProcess[str]:
    """Run one Git command of the checkout preparation; an unlaunchable Git is a gate error."""
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
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
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(self.repository),
                        "worktree",
                        "remove",
                        "--force",
                        "--",
                        str(checkout),
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                )
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
    """Заменить системный вызов Python детерминированным перед выполнением команды."""
    original = command
    if isinstance(command, str):
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
    if launcher not in {"python", "python3", "py"}:
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
    commands: list[str | list[str]],
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
QA_STAGE_GATE = "gate"
CODE_CHECKS_STARTED = "started"
CODE_CHECKS_NOT_STARTED = "not_started"
CODE_CHECKS_UNKNOWN = "unknown"
DIAGNOSIS_INFRASTRUCTURE = "infrastructure"
DIAGNOSIS_PROJECT_DEFECT = "project-defect"
DIAGNOSIS_UNKNOWN = "unknown"
DIAGNOSTICS_LIMIT = 1_200

# Log signatures are only one signal. A category is confirmed only when a signature agrees with a
# structural fact (see ``diagnose``); neither an exit code nor a signature decides it alone.
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
    command: str, exit_code: int, output: str, tracked_files: tuple[str, ...]
) -> Diagnosis:
    """Classify one failed preparation command from independent signals (pure, no I/O).

    A category is confirmed only when a log signature and a structural fact agree: a project defect
    needs a signature and a manifest or lock file that the candidate tracks and the output names; an
    infrastructure failure needs an infrastructure signature, and no project-defect signature and no
    tracked project file named in the output, so a defect in the project's own files cannot pass as
    an outage. A bare non-zero exit, or a keyword alone, is ``unknown``; so is any contradiction.
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
    if infrastructure and defects:
        return Diagnosis(
            DIAGNOSIS_UNKNOWN,
            (
                f"exit-code:{exit_code}",
                *(f"log:{n}" for n in (*infrastructure, *defects)),
            ),
            "the output carries both an infrastructure and a project-defect signature",
        )
    if defects and named:
        return Diagnosis(
            DIAGNOSIS_PROJECT_DEFECT,
            (
                f"exit-code:{exit_code}",
                *(f"log:{n}" for n in defects),
                *(f"project-file:{n}" for n in named),
            ),
            "a project-defect signature agrees with a tracked project file the output names",
        )
    if infrastructure and not named:
        return Diagnosis(
            DIAGNOSIS_INFRASTRUCTURE,
            (
                f"exit-code:{exit_code}",
                *(f"log:{n}" for n in infrastructure),
                "no-project-file-implicated",
            ),
            "an infrastructure signature, with no project-defect signature and no tracked project file named",
        )
    return Diagnosis(
        DIAGNOSIS_UNKNOWN,
        (
            f"exit-code:{exit_code}",
            *(f"log:{n}" for n in (*infrastructure, *defects)),
            *(f"project-file:{n}" for n in named),
        ),
        "the signals do not confirm one cause; a non-zero exit or a keyword alone is not evidence",
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
    """Files the candidate tracks, or none when Git cannot list them (a diagnosis then stays unknown)."""
    listed = _run_git(["git", "-C", str(checkout), "ls-files"])
    return tuple(listed.stdout.splitlines()) if listed.returncode == 0 else ()


def run_qa_stages(
    preparation: list[str | list[str]],
    gate: list[str | list[str]],
    policy: ExecutionPolicy,
) -> QAStagesResult:
    """Run preparation, then the gate, in one checkout, stopping at the first failing stage.

    A failed preparation command stops the run before any gate command starts, so the result states
    that no code check ran and diagnoses the failure; the gate is never recorded as failed.
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
