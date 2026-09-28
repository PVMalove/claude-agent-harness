"""Стык запуска команд, используемый консолью для любых действий, порождающих процесс вне
собственного сбора данных только для чтения (harness.console.data): перезапуск `uv run` в launcher.py
и команды каталога экранов Harness, Orchestration и Repo Map. Тесты Pilot подменяют исполнитель
записывающим дублёром вместо обращения к реальному окружению или оболочке."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Mapping, Protocol, Sequence


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


def capturing_runner(
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> "subprocess.CompletedProcess[str]":
    """Исполнитель команд, запускаемых изнутри TUI, владеющего терминалом: вывод перехватывается
    для отображения на экране, а stdin закрывается, чтобы команды с запросами подтверждения (например,
    `harness init`) использовали неинтерактивное поведение по умолчанию. Переменная `PYTHONUTF8`
    обеспечивает кодировку UTF-8 для дочерних процессов Python."""
    child_env = dict(env if env is not None else os.environ)
    child_env.setdefault("PYTHONUTF8", "1")
    return subprocess.run(
        list(argv),
        env=child_env,
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        stdin=subprocess.DEVNULL,
    )
