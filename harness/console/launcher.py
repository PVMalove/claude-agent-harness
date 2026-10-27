"""Перезапуск `harness console` в одноразовом окружении `uv run --with textual==<pin>` (ADR 0009)
без изменения собственных зависимостей целевого проекта: список `dependencies` в `pyproject.toml`
остаётся `[]`, а флаг `--no-project` предотвращает установку пакетов из окружающих `pyproject.toml`/`uv.lock`.

Этот модуль и его точка входа `run_console` могут импортироваться без установленного пакета `textual`:
сам перезапуск только формирует список аргументов подпроцесса и запускает его через переданный
`CommandRunner`. Модуль `harness.console.app` (собственно UI на `textual`) импортируется отложенно,
только когда `run_console` подтверждает, что процесс уже выполняется внутри перезапущенного подпроцесса.

Перед TUI и для `--web` модуль запускает или находит Сервер консоли проекта (`start_server`, ADR 0028):
отдельный процесс `harness console-server` под `uv run --with` с пинами сервера из `pin.py`.
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import webbrowser
from pathlib import Path
from typing import BinaryIO, Callable, NoReturn, Sequence

from .discovery import ServerEndpoint, read_record
from .pin import CONSOLE_SERVER_REQUIREMENTS, TEXTUAL_PIN
from .runner import CommandRunner, default_runner

# Set on the relaunched `uv run` subprocess's own environment so it knows not to relaunch again.
RELAUNCH_ENV = "HARNESS_CONSOLE_RELAUNCHED"
# A marker file the relaunched process creates once it reaches the TUI: a non-zero exit after that
# is the TUI's own failure, not a failed `uv run --with textual` start, so it gets no fallback.
STARTED_ENV = "HARNESS_CONSOLE_STARTED_FILE"

BIN_HARNESS_PATH = Path(__file__).resolve().parent.parent / "bin" / "harness.py"


def restart_console(repo: Path) -> NoReturn:
    """Reload console code in the existing isolated Python, retaining the selected project."""
    os.execv(
        sys.executable,
        [sys.executable, str(BIN_HARNESS_PATH), "console", str(repo)],
    )


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


def build_server_argv(uv: str, repo: Path) -> list[str]:
    """Команда запуска Сервера консоли: как у перезапуска TUI, но с зависимостями сервера
    (`h11`, `websockets`, `jinja2`) и подкомандой `console-server` (ADR 0028)."""
    with_requirements = [
        arg for pin in CONSOLE_SERVER_REQUIREMENTS for arg in ("--with", pin)
    ]
    return [
        uv,
        "run",
        "--no-project",
        "--python",
        sys.executable,
        *with_requirements,
        "python",
        str(BIN_HARNESS_PATH),
        "console-server",
        str(repo),
    ]


class ServerStartError(RuntimeError):
    """Сервер консоли не удалось запустить или дождаться; текст — причина для пользователя."""


ServerStarter = Callable[[Path, str], ServerEndpoint]


SERVER_START_TIMEOUT = 30.0
_SERVER_POLL_SECONDS = 0.1


def _serves_console(port: int) -> bool:
    """Порт отвечает как Сервер консоли (`GET /` даёт 200 «Console server»), а не просто принимает
    соединения: устаревшая запись может указывать на порт постороннего процесса."""
    request = (
        f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n"
    ).encode()
    reply = b""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5) as connection:
            connection.sendall(request)
            while len(reply) < 4096 and (chunk := connection.recv(1024)):
                reply += chunk
    except OSError:
        return False
    return reply.startswith(b"HTTP/1.1 200") and reply.rstrip().endswith(
        b"Console server"
    )


def _new_live_record(repo: Path, stale: ServerEndpoint | None) -> ServerEndpoint | None:
    """Запись, отличная от устаревшей, порт которой отвечает как Сервер консоли."""
    record = read_record(repo)
    if record is None or record == stale or not _serves_console(record.port):
        return None
    return record


def _stop(process: "subprocess.Popen[bytes]") -> None:
    """Останавливает `uv` вместе с дочерним сервером: на POSIX процесс лидер своей группы."""
    try:
        if sys.platform == "win32":
            process.terminate()
        else:
            os.killpg(process.pid, signal.SIGTERM)
    except OSError:
        pass


def _spawn_detached(argv: list[str], log: BinaryIO) -> "subprocess.Popen[bytes]":
    """Запускает процесс отдельно от команды, чтобы сервер пережил её (ADR 0028). Его вывод идёт в
    файл, а не в каналы вызывающей команды."""
    windows = sys.platform == "win32"
    creationflags = (
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "DETACHED_PROCESS", 0)
        if windows
        else 0
    )
    return subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        creationflags=creationflags,
        start_new_session=not windows,
    )


def _failure_reason(code: int, log: BinaryIO) -> str:
    log.seek(0)
    tail = log.read()[-500:].decode("utf-8", "replace").strip()
    return f"uv run завершился с кодом {code}" + (f": {tail}" if tail else "")


def start_server(
    repo: Path, uv: str, *, timeout: float = SERVER_START_TIMEOUT
) -> ServerEndpoint:
    """Возвращает работающий Сервер консоли проекта; если его нет, запускает отдельный процесс
    через `uv run --with` и ждёт новую discovery-запись. Сервер живёт дольше команды (ADR 0028)."""
    stale = read_record(repo)
    if stale is not None and _serves_console(stale.port):
        return stale

    with tempfile.TemporaryFile("w+b") as log:
        try:
            process = _spawn_detached(build_server_argv(uv, repo), log)
        except OSError as exc:
            raise ServerStartError(f"не удалось запустить uv: {exc}") from exc
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            # Sampled before the record: a process that already exited can no longer publish, so
            # a record found afterwards belongs to another console that won the project lock.
            code = process.poll()
            ready = _new_live_record(repo, stale)
            if ready is not None:
                return ready
            if code is not None:
                raise ServerStartError(_failure_reason(code, log))
            time.sleep(_SERVER_POLL_SECONDS)
        _stop(process)
        raise ServerStartError(f"сервер не ответил за {timeout:g} с")


def _default_app_runner(repo: Path) -> int:
    """Запускает приложение Textual по умолчанию для указанного репозитория."""
    from . import app as console_app

    return console_app.run(repo)


def run_console(
    repo: Path,
    extra_argv: Sequence[str] = (),
    *,
    web: bool = False,
    no_open: bool = False,
    runner: CommandRunner = default_runner,
    app_runner: Callable[[Path], int] = _default_app_runner,
    server_starter: ServerStarter = start_server,
    browser_opener: Callable[[str], object] = webbrowser.open,
) -> int:
    """Точка входа, вызываемая `cmd_console`. Реализует четыре сценария:

    1. Процесс уже перезапущен (установлена переменная окружения `RELAUNCH_ENV`): запускает реальное
       Textual-приложение внутри процесса через `app_runner`. Это единственный путь, импортирующий
       `textual`.
    2. Утилита `uv` не найдена в PATH: выводит причину и переключается на отчёт `harness health`,
       код 1.
    3. `uv` найден: `server_starter` запускает Сервер консоли проекта или находит работающий. Сбой
       запуска сервера выводит причину и отчёт health, код 1; TUI без сервера не стартует.
    4. Сервер готов. С `web` печатает URL с токеном, открывает браузер (если не `no_open`) и
       возвращает 0. Без `web` перезапускает процесс через `runner`: ненулевой код до старта TUI
       выводит отчёт health и возвращает 1, ненулевой код после старта TUI возвращается как есть.
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
            "harness console: uv не найден в PATH — сервер консоли и TUI недоступны, показан отчёт harness health",
        )
        return 1

    try:
        endpoint = server_starter(repo, uv)
    except ServerStartError as exc:
        _print_fallback(
            repo,
            f"harness console: не удалось запустить сервер консоли: {exc} — показан отчёт harness health",
        )
        return 1
    if web:
        url = f"http://127.0.0.1:{endpoint.port}/?token={endpoint.token}"
        print(url)
        if not no_open:
            browser_opener(url)
        return 0

    argv = build_relaunch_argv(uv, repo, extra_argv)
    env = dict(os.environ)
    env[RELAUNCH_ENV] = "1"
    # A process-liveness marker, not a task artifact: it lives only for this call.
    with tempfile.TemporaryDirectory(prefix="harness-console-") as marker_dir:
        marker = Path(marker_dir) / "started"
        env[STARTED_ENV] = str(marker)
        result = runner(argv, env=env)
        tui_started = marker.exists()
    if result.returncode == 0:
        return 0
    if tui_started:
        print(f"harness console завершился с кодом {result.returncode}")
        return result.returncode
    _print_fallback(
        repo,
        "harness console: не удалось запустить textual через uv "
        f"(uv run завершился с кодом {result.returncode}) — показан отчёт harness health",
    )
    return 1


def _print_fallback(repo: Path, reason: str) -> None:
    """Выводит причину сбоя и текстовый отчёт, аналогичный выводу `harness health`."""
    print(reason)
    from ..health import render as health_render
    from .data import run_health

    print(health_render.render_text(run_health(repo)), end="")
