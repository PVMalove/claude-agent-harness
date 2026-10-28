"""Стык запуска команд, используемый консолью для любых действий, порождающих процесс вне
собственного сбора данных только для чтения (harness.console.data): перезапуск `uv run` в launcher.py
и команды каталога экранов Harness, Orchestration и Repo Map. Тесты Pilot подменяют исполнитель
записывающим дублёром вместо обращения к реальному окружению или оболочке."""

from __future__ import annotations

import os
import subprocess
import codecs
from io import IncrementalNewlineDecoder
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable
from dataclasses import dataclass
from threading import Event, Lock
from pathlib import Path
from typing import Mapping, Protocol, Sequence, cast

from .process_tree import wait_cancellable


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


def capturing_runner(
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    on_output: Callable[[str, str], None] | None = None,
    cancel_event: Event | None = None,
) -> "subprocess.CompletedProcess[str]":
    """Исполнитель команд, запускаемых изнутри TUI, владеющего терминалом: вывод перехватывается
    для отображения на экране, а stdin закрывается, чтобы команды с запросами подтверждения (например,
    `harness init`) использовали неинтерактивное поведение по умолчанию. Переменная `PYTHONUTF8`
    обеспечивает кодировку UTF-8 для дочерних процессов Python. С `cancel_event` команда
    запускается в своей сессии: установленное событие останавливает её вместе с потомками, и
    исполнитель поднимает `CommandCancelled` (см. process_tree.py)."""
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
        # Windows ignores it; there the Job Object of process_tree.py holds the descendants.
        start_new_session=cancel_event is not None,
    ) as process:
        assert process.stdout is not None and process.stderr is not None
        with ThreadPoolExecutor(max_workers=2) as readers:
            stdout = readers.submit(drain, cast(_OutputPipe, process.stdout), "stdout")
            stderr = readers.submit(drain, cast(_OutputPipe, process.stderr), "stderr")
            returncode = (
                process.wait()
                if cancel_event is None
                else wait_cancellable(
                    process, cancel_event, lambda: stdout.done() and stderr.done()
                )
            )
            return subprocess.CompletedProcess(
                list(argv), returncode, stdout.result(), stderr.result()
            )


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
