"""Единственный способ запуска внешних инструментов (git, uv, gh/glab и т. д.) в проверках здоровья."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def run_tool(
    argv: list[str],
    *,
    timeout: float,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str] | None:
    """Запустить утилиту в неинтерактивном режиме; возвращает None при ошибке запуска или таймауте.

    Никогда не вызывает исключений: отсутствие инструмента или зависший вызов преобразуются
    в `CheckResult` со статусом `warn`/`fail`, а не в аварийное завершение. Поток stdin закрыт,
    а Git никогда не запрашивает учетные данные, поэтому проверка может завершиться только
    по таймауту, никогда не блокируясь в ожидании ввода с клавиатуры.
    """
    child_env = dict(os.environ if env is None else env)
    child_env.setdefault("GIT_TERMINAL_PROMPT", "0")
    try:
        return subprocess.run(
            argv,
            cwd=cwd,
            env=child_env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
