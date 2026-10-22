"""Запуск команд и стадий проверки с изолированными временными каталогами."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO


def run_ok(
    cmd: list[str],
    *,
    env: Mapping[str, str] | None = None,
    stdout: int | None = None,
    cwd: Path | None = None,
) -> None:
    """Запустить обязательную команду; при сбое завершиться её кодом выхода, как `set -e`.

    Трассировка `CalledProcessError` не печатается: команда уже вывела свою ошибку в stderr.
    """
    result = subprocess.run(cmd, env=env, stdout=stdout, cwd=cwd, check=False)
    if result.returncode != 0:
        sys.exit(result.returncode)


def run_stage(
    name: str,
    cmd: list[str],
    *,
    env: Mapping[str, str] | None = None,
    stdout: int | None = None,
    cwd: Path | None = None,
) -> None:
    """Запустить именованную стадию проверки и напечатать её длительность по монотонным часам."""
    started = time.perf_counter()
    try:
        run_ok(cmd, env=env, stdout=stdout, cwd=cwd)
    except SystemExit:
        print(f"[verify] {name}: failed in {time.perf_counter() - started:.2f}s")
        raise
    print(f"[verify] {name}: passed in {time.perf_counter() - started:.2f}s")


@dataclass(frozen=True)
class CapturedRun:
    """One finished command: its exit code, both output streams kept apart, and its duration."""

    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int


def _tee(source: TextIO, sink: TextIO, kept: list[str]) -> None:
    """Pass each line on to ``sink`` as it arrives and keep it."""
    for line in iter(source.readline, ""):
        kept.append(line)
        sink.write(line)
        sink.flush()


def run_captured(
    cmd: list[str],
    *,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> CapturedRun:
    """Run a command, echoing stdout and stderr live while keeping each stream separately.

    The stage evidence stores the two streams apart, so they cannot share one pipe.
    """
    started = time.perf_counter()
    process = subprocess.Popen(
        cmd,
        env=env,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert process.stdout is not None and process.stderr is not None
    kept: tuple[list[str], list[str]] = ([], [])
    threads = [
        threading.Thread(target=_tee, args=(process.stdout, sys.stdout, kept[0])),
        threading.Thread(target=_tee, args=(process.stderr, sys.stderr, kept[1])),
    ]
    for thread in threads:
        thread.start()
    exit_code = process.wait()
    for thread in threads:
        thread.join()
    return CapturedRun(
        exit_code,
        "".join(kept[0]),
        "".join(kept[1]),
        round((time.perf_counter() - started) * 1000),
    )


def isolated_temp_env(base: Mapping[str, str], run_tmp: Path) -> dict[str, str]:
    """Окружение `base`, где все временные каталоги указывают на собственный корень запуска (#305).

    Корень pytest по умолчанию — %TEMP%\\pytest-of-<USERNAME>, общий для всех учётных записей с тем же
    USERNAME и TEMP (песочницы агентов работают под отдельными локальными пользователями). Python
    3.13+ создаёт его только для владельца на Windows, поэтому первая учётная запись блокирует
    остальные с WinError 5.

    Байткод не пишется вовсе: PYTHONPYCACHEPREFIX зеркалирует под собой полный абсолютный путь
    исходника, и QA checkout внутри того же репозитория выходил за MAX_PATH (#381).
    """
    env = dict(base)
    env.pop("PYTHONPYCACHEPREFIX", None)
    env.update(
        TMP=str(run_tmp),
        TEMP=str(run_tmp),
        TMPDIR=str(run_tmp),
        PYTHONDONTWRITEBYTECODE="1",
        MYPY_CACHE_DIR=str(run_tmp / "mypy"),
        GIT_CEILING_DIRECTORIES=os.pathsep.join(
            part
            for part in (base.get("GIT_CEILING_DIRECTORIES", ""), str(run_tmp))
            if part
        ),
    )
    return env


def _clear_read_only(
    func: Callable[[str], object], path: str, _exc: BaseException
) -> None:
    """Снять атрибут «только чтение» и повторить удаление: git оставляет такие объекты на Windows."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def remove_tree(path: Path) -> None:
    """Удалить каталог целиком, включая файлы только для чтения."""
    shutil.rmtree(path, onexc=_clear_read_only)
