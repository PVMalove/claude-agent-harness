"""Стык запуска команд, используемый консолью для любых действий, порождающих процесс вне
собственного сбора данных только для чтения (harness.console.data): перезапуск `uv run` в launcher.py
и команды каталога экранов Harness, Orchestration и Repo Map. Тесты Pilot подменяют исполнитель
записывающим дублёром вместо обращения к реальному окружению или оболочке."""

from __future__ import annotations

import codecs
import ctypes
from ctypes import wintypes
import os
import signal
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable
from dataclasses import dataclass
from io import IncrementalNewlineDecoder
from pathlib import Path
from threading import Lock
from typing import Mapping, Protocol, Sequence, cast


_CANCEL_GRACE_SECONDS = 0.5


def _is_windows() -> bool:
    """Return whether this process uses the native Windows process APIs."""
    return os.name == "nt"


class CommandRunner(Protocol):
    """Протокол запуска внешних команд с передачей аргументов, окружения и рабочей директории."""

    def __call__(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> "subprocess.CompletedProcess[str]":
        """Выполняет команду подпроцесса и возвращает завершённый процесс со строковым выводом."""
        ...


def default_runner(
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> "subprocess.CompletedProcess[str]":
    """Запускает команду как обычный подпроцесс через subprocess.run со сквозным выводом."""
    return subprocess.run(
        list(argv),
        env=dict(env) if env is not None else None,
        cwd=cwd,
        text=True,
    )


class _OutputPipe(Protocol):
    def read1(self, size: int) -> bytes: ...


class _WindowsJob:
    """Own a Windows Job Object that follows descendants after the root exits."""

    def __init__(self, process: "subprocess.Popen[bytes]") -> None:
        loader = getattr(ctypes, "WinDLL", None)
        if loader is None:
            raise OSError("Windows Job Objects are unavailable")
        self._kernel32 = loader("kernel32", use_last_error=True)
        self._kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        self._kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        self._kernel32.AssignProcessToJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
        ]
        self._kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        self._kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self._kernel32.TerminateJobObject.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL
        self._handle: object | None = self._kernel32.CreateJobObjectW(None, None)
        if not self._handle:
            raise OSError(getattr(ctypes, "get_last_error")(), "CreateJobObjectW failed")
        process_handle = getattr(process, "_handle", None)
        if process_handle is None or not self._kernel32.AssignProcessToJobObject(
            self._handle, process_handle
        ):
            self.close()
            raise OSError(
                getattr(ctypes, "get_last_error")(),
                "AssignProcessToJobObject failed",
            )

    def terminate(self) -> bool:
        """Terminate every process currently assigned to the job, including descendants."""
        return bool(
            self._handle is not None
            and self._kernel32.TerminateJobObject(self._handle, 1)
        )

    def close(self) -> None:
        if self._handle is not None:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


def _create_windows_job(process: "subprocess.Popen[bytes]") -> _WindowsJob | None:
    """Create the Windows descendant boundary, falling back when the host denies it."""
    if not _is_windows():
        return None
    try:
        return _WindowsJob(process)
    except OSError:
        return None


def _stop_process_tree(
    process: "subprocess.Popen[bytes]",
    *,
    force: bool,
    windows_job: _WindowsJob | None = None,
) -> bool:
    """Signal the process and descendants started for a cancellable command.

    Cancellable commands start a new POSIX session or Windows Job Object below.  The boundary
    keeps a child that inherited stdout/stderr from keeping the job alive after its parent is
    stopped. ``taskkill /T`` is the fallback when a Windows host refuses Job Object assignment;
    its return code is checked so a failed tree stop cannot be reported as successful.
    """
    if _is_windows():
        if force:
            if windows_job is not None and windows_job.terminate():
                return True
            try:
                result = subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=5,
                )
                if result.returncode == 0:
                    return True
            except (OSError, subprocess.TimeoutExpired):
                pass
            try:
                process.kill()
            except OSError:
                pass
            return False
        try:
            process.send_signal(getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM))
            return True
        except (OSError, ValueError):
            # A process created without a console cannot receive CTRL_BREAK.  Keep the parent
            # alive until the force pass so taskkill can still walk its descendants.
            return False

    else:
        try:
            os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
            return True
        except OSError:
            try:
                (process.kill if force else process.terminate)()
            except OSError:
                pass
            return False


def capturing_runner(
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    on_output: Callable[[str, str], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> "subprocess.CompletedProcess[str]":
    """Исполнитель команд, запускаемых изнутри TUI, владеющего терминалом: вывод перехватывается
    для отображения на экране, а stdin закрывается, чтобы команды с запросами подтверждения (например,
    `harness init`) использовали неинтерактивное поведение по умолчанию. Переменная `PYTHONUTF8`
    обеспечивает кодировку UTF-8 для дочерних процессов Python. При наличии ``cancel_event``
    команда получает отдельную группу/сессию, чтобы отмена останавливала и её потомков."""
    child_env = dict(env if env is not None else os.environ)
    child_env.setdefault("PYTHONUTF8", "1")
    child_env.setdefault("PYTHONUNBUFFERED", "1")

    # This is the foreground CLI boundary, not a worker supervisor: runtime remains
    # the sole owner of worker/QA lifecycle. Navigation never signals this process.
    def drain(pipe: _OutputPipe, channel: str) -> str:
        decoder = IncrementalNewlineDecoder(
            codecs.getincrementaldecoder("utf-8")("replace"), translate=True
        )
        chunks: list[str] = []
        while chunk := pipe.read1(65536):
            text = decoder.decode(chunk)
            chunks.append(text)
            if on_output and text:
                on_output(channel, text)
        tail = decoder.decode(b"", final=True)
        chunks.append(tail)
        if on_output and tail:
            on_output(channel, tail)
        return "".join(chunks)

    with subprocess.Popen(
        list(argv),
        env=child_env,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        creationflags=(
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            if cancel_event is not None and _is_windows()
            else 0
        ),
        start_new_session=cancel_event is not None and not _is_windows(),
    ) as process:
        assert process.stdout is not None and process.stderr is not None
        windows_job: _WindowsJob | None = None
        try:
            if cancel_event is not None and _is_windows():
                windows_job = _create_windows_job(process)
            with ThreadPoolExecutor(max_workers=2) as readers:
                stdout = readers.submit(drain, cast(_OutputPipe, process.stdout), "stdout")
                stderr = readers.submit(drain, cast(_OutputPipe, process.stderr), "stderr")
                cancelled = False
                forced = False
                cancel_started: float | None = None
                returncode: int | None = None
                while True:
                    if cancel_event is not None and cancel_event.is_set() and not cancelled:
                        cancelled = True
                        cancel_started = time.monotonic()
                        if _is_windows():
                            # CTRL_BREAK may let the root exit before a later taskkill can find
                            # its tree. Force the checked tree operation while the root PID is
                            # still live; the Job Object covers descendants assigned after spawn.
                            forced = _stop_process_tree(
                                process, force=True, windows_job=windows_job
                            )
                        else:
                            _stop_process_tree(
                                process, force=False, windows_job=windows_job
                            )
                    if (
                        cancelled
                        and not forced
                        and cancel_started is not None
                        and time.monotonic() - cancel_started >= _CANCEL_GRACE_SECONDS
                    ):
                        forced = _stop_process_tree(
                            process, force=True, windows_job=windows_job
                        )
                    if returncode is None:
                        try:
                            returncode = process.wait(
                                timeout=0.1 if cancel_event else None
                            )
                        except subprocess.TimeoutExpired:
                            continue
                    # A parent can exit before a descendant closes an inherited output pipe. Keep
                    # sampling the cancellation event while readers drain; on Windows the Job
                    # Object remains valid after the parent exits, closing the taskkill race.
                    if stdout.done() and stderr.done() and (
                        not cancelled
                        or forced
                        or (not _is_windows() and returncode is not None)
                    ):
                        break
                    time.sleep(0.05)
                assert returncode is not None
                return subprocess.CompletedProcess(
                    list(argv), returncode, stdout.result(), stderr.result()
                )
        finally:
            if windows_job is not None:
                windows_job.close()


@dataclass
class HarnessRun:
    """One accepted invocation, independent of menu selection and screen lifetime."""

    token: int
    key: str
    cli_line: str
    argv: list[str]
    values: dict[str, str]
    phase: str = "pending"
    stdout: str = ""
    stderr: str = ""
    returncode: int | None = None
    error: str = ""
    output: str = ""
    revision: int = 0


class HarnessRuns:
    """App-local admission, held until the runner actually returns, even after UI departure."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._serial = 0
        self.current: HarnessRun | None = None
        self._busy = False

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._busy

    def reserve(
        self, key: str, cli_line: str, argv: list[str], values: Mapping[str, str]
    ) -> HarnessRun | None:
        with self._lock:
            if self._busy:
                return None
            self._serial += 1
            run = HarnessRun(self._serial, key, cli_line, argv, dict(values))
            self.current = run
            self._busy = True
            return run

    def abandon_pending(self, run: HarnessRun) -> None:
        with self._lock:
            if self.current is run and run.phase == "pending":
                run.phase = "cancelled"
                self._busy = False

    def execute(self, run: HarnessRun, runner: CommandRunner, repo: Path) -> None:
        with self._lock:
            if self.current is not run or run.phase != "pending":
                return
            run.phase = "running"

        def append(channel: str, text: str) -> None:
            with self._lock:
                if self.current is run and run.phase == "running":
                    if channel == "stdout":
                        run.stdout += text
                    else:
                        run.stderr += text
                    run.output += text
                    run.revision += 1

        result = None
        error = ""
        try:
            if runner is capturing_runner:
                result = capturing_runner(run.argv, cwd=repo, on_output=append)
            else:
                result = runner(run.argv, cwd=repo)
        except OSError as exc:
            error = f"не удалось запустить: {exc}"
            result = None
        except Exception as exc:
            error = f"сбой запуска: {exc!r}"
            result = None
        else:
            error = ""
        finally:
            # Worker cancellation does not release this section; only physical completion does.
            with self._lock:
                if self.current is run:
                    if result is not None:
                        run.stdout, run.stderr = (
                            result.stdout or "",
                            result.stderr or "",
                        )
                        run.returncode = result.returncode
                    run.error = error
                    run.phase = "completed"
                    self._busy = False
