"""Перезапуск `harness console` в одноразовом окружении `uv run --with textual==<pin>` (ADR 0009)
без изменения собственных зависимостей целевого проекта: список `dependencies` в `pyproject.toml`
остаётся `[]`, а флаг `--no-project` предотвращает установку пакетов из окружающих `pyproject.toml`/`uv.lock`.

Этот модуль и его точка входа `run_console` могут импортироваться без установленного пакета `textual`:
сам перезапуск только формирует список аргументов подпроцесса и запускает его через переданный
`CommandRunner`. Модуль `harness.console.app` (собственно UI на `textual`) импортируется отложенно,
только когда `run_console` подтверждает, что процесс уже выполняется внутри перезапущенного подпроцесса.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Callable, Sequence

from .pin import TEXTUAL_PIN
from .runner import CommandRunner, default_runner

# Set on the relaunched `uv run` subprocess's own environment so it knows not to relaunch again.
RELAUNCH_ENV = "HARNESS_CONSOLE_RELAUNCHED"
# A marker file the relaunched process creates once it reaches the TUI: a non-zero exit after that
# is the TUI's own failure, not a failed `uv run --with textual` start, so it gets no fallback.
STARTED_ENV = "HARNESS_CONSOLE_STARTED_FILE"

BIN_HARNESS_PATH = Path(__file__).resolve().parent.parent / "bin" / "harness.py"


def find_uv() -> str | None:
    """Находит исполняемый файл `uv` через `shutil.which`, не используя жёстко закодированные пути."""
    return shutil.which("uv")


def build_relaunch_argv(
    uv: str, repo: Path, extra_argv: Sequence[str] = ()
) -> list[str]:
    """Формирует точную команду перезапуска: флаг `--no-project` исключает установку зависимостей
    окружающего проекта или изменение lock-файла, `--with textual==<pin>` изолирует зависимость TUI
    в одноразовом окружении, а `--python <текущий интерпретатор>` повторно использует интерпретатор,
    уже прошедший проверку Python >= 3.12."""
    return [
        uv,
        "run",
        "--no-project",
        "--python",
        sys.executable,
        "--with",
        f"textual=={TEXTUAL_PIN}",
        "python",
        str(BIN_HARNESS_PATH),
        "console",
        str(repo),
        *extra_argv,
    ]


def _default_app_runner(repo: Path) -> int:
    """Запускает приложение Textual по умолчанию для указанного репозитория."""
    from . import app as console_app

    return console_app.run(repo)


def run_console(
    repo: Path,
    extra_argv: Sequence[str] = (),
    *,
    runner: CommandRunner = default_runner,
    app_runner: Callable[[Path], int] = _default_app_runner,
) -> int:
    """Точка входа, вызываемая `cmd_console`. Реализует три сценария:

    1. Процесс уже перезапущен (установлена переменная окружения `RELAUNCH_ENV`): запускает реальное
       Textual-приложение внутри процесса через `app_runner`. Это единственный путь, импортирующий
       `textual`.
    2. Утилита `uv` не найдена в PATH: выводит причину и переключается на отчёт `harness health`.
    3. `uv` найден: перезапускает процесс через `runner`. Ненулевой код завершения до старта TUI
       приводит к выводу отчёта health. Ненулевой код после старта TUI возвращает код завершения.
    """
    if os.environ.get(RELAUNCH_ENV) == "1":
        started = os.environ.get(STARTED_ENV)
        if started:
            try:
                Path(started).touch()
            except OSError:
                pass
        return app_runner(repo)

    uv = find_uv()
    if uv is None:
        _print_fallback(
            repo,
            "harness console: uv не найден в PATH — TUI недоступен, показан отчёт harness health",
        )
        return 1

    argv = build_relaunch_argv(uv, repo, extra_argv)
    env = dict(os.environ)
    env[RELAUNCH_ENV] = "1"
    # A process-liveness marker, not a task artifact: it lives only for this call.
    with tempfile.TemporaryDirectory(prefix="harness-console-") as marker_dir:
        marker = Path(marker_dir) / "started"
        env[STARTED_ENV] = str(marker)
        result = runner(argv, env=env)
        tui_started = marker.exists()
    if result.returncode != 0:
        if tui_started:
            print(f"harness console завершился с кодом {result.returncode}")
        else:
            _print_fallback(
                repo,
                "harness console: не удалось запустить textual через uv "
                f"(uv run завершился с кодом {result.returncode}) — показан отчёт harness health",
            )
    return result.returncode


def _print_fallback(repo: Path, reason: str) -> None:
    """Выводит причину сбоя и текстовый отчёт, аналогичный выводу `harness health`."""
    print(reason)
    from ..health import render as health_render
    from .data import run_health

    print(health_render.render_text(run_health(repo)), end="")
