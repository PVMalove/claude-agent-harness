"""Запуск команд и стадий проверки с изолированными временными каталогами."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path


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


def isolated_temp_env(base: Mapping[str, str], run_tmp: Path) -> dict[str, str]:
    """Окружение `base`, где все временные каталоги указывают на собственный корень запуска (#305).

    Корень pytest по умолчанию — %TEMP%\\pytest-of-<USERNAME>, общий для всех учётных записей с тем же
    USERNAME и TEMP (песочницы агентов работают под отдельными локальными пользователями). Python
    3.13+ создаёт его только для владельца на Windows, поэтому первая учётная запись блокирует
    остальные с WinError 5.
    """
    return dict(
        base,
        TMP=str(run_tmp),
        TEMP=str(run_tmp),
        TMPDIR=str(run_tmp),
        PYTHONPYCACHEPREFIX=str(run_tmp / "pycache"),
        MYPY_CACHE_DIR=str(run_tmp / "mypy"),
        GIT_CEILING_DIRECTORIES=os.pathsep.join(
            part for part in (base.get("GIT_CEILING_DIRECTORIES", ""), str(run_tmp)) if part
        ),
    )


def _clear_read_only(func: Callable[[str], object], path: str, _exc: BaseException) -> None:
    """Снять атрибут «только чтение» и повторить удаление: git оставляет такие объекты на Windows."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def remove_tree(path: Path) -> None:
    """Удалить каталог целиком, включая файлы только для чтения."""
    shutil.rmtree(path, onexc=_clear_read_only)
