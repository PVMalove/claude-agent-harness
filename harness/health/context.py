"""Общий контекст одного запуска, передаваемый в каждую функцию проверки здоровья."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .model import JsonObject
from .project_files import BACKEND_ORCHESTRATION_CAPABILITY

_BROKEN_LOCK_MESSAGE = "не проверено: .harness/harness.lock повреждён (см. files.lock)"


def shell_join(argv: list[str]) -> str:
    """Сформировать командную строку для копирования и вставки в шелл текущей платформы."""
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


@dataclass(frozen=True)
class HealthContext:
    """Контекст запуска `harness health`, исключающий повторное чтение .harness/harness.lock каждой проверкой.

    Параметр `snapshot_diff` необязателен и по умолчанию равен None: это единственная функция
    обнаружения, которая не может быть перенесена в данный stdlib-пакет (она заново вычисляет
    ожидаемое содержимое пакетов из `CAPABILITIES.json` и дерева исходников `harness/`, которые
    не поставляются в установленный проект). Только канонический CLI `harness health`
    (cmd_health в `harness/bin/harness.py`) передаёт сюда уже загруженную функцию `snapshot_diff`;
    автономный установленный пакет `harness/health/` оставляет её равной None, и проверка
    `files.check_skill_snapshot` возвращает статус 'skipped' вместо ошибки импорта.
    """

    repo: Path
    lock: JsonObject | None
    online: bool
    snapshot_diff: Callable[[Path], JsonObject] | None = None
    # stdout's encoding as the caller saw it, before any in-process reconfiguration (the canonical
    # CLI forces UTF-8 on startup, which would otherwise hide a non-UTF-8 console from
    # environment.check_output_encoding). None means "read sys.stdout at check time".
    output_encoding: str | None = None
    # Why .harness/harness.lock exists but could not be used (unreadable, not JSON, not an object);
    # `lock` is None then too, so lock-dependent checks skip while files.check_lock reports `fail`.
    lock_error: str | None = None
    # How to invoke the harness CLI in a printed remedy. The canonical CLI passes its own interpreter
    # and script path so the command runs as printed; a shipped, standalone package keeps the
    # documented `harness` alias.
    harness_cli: tuple[str, ...] = ("harness",)

    def no_lock_message(self) -> str:
        """Причина пропуска проверки, зависящей от lock-файла: lock отсутствует либо повреждён."""
        return _BROKEN_LOCK_MESSAGE if self.lock_error else "нет .harness/harness.lock"

    def orchestration_enabled(self) -> bool:
        """Выбрана ли в lock-файле возможность backend-orchestration."""
        return self.lock is not None and BACKEND_ORCHESTRATION_CAPABILITY in (
            self.lock.get("capabilities") or []
        )

    def no_orchestration_message(self) -> str:
        """Причина пропуска проверки backend-orchestration (при повреждённом lock возвращает ошибку lock-файла)."""
        if self.lock_error:
            return _BROKEN_LOCK_MESSAGE
        return "backend-orchestration capability не выбрана"

    def harness_command(self, *args: str) -> str:
        """Сформировать команду исправления через CLI харнесса с переданными аргументами."""
        return shell_join([*self.harness_cli, *args])

    def coordinator_command(self, *args: str) -> str:
        """Сформировать команду исправления через координатор репозитория, запускаемую из любого каталога."""
        script = self.repo / ".harness" / "orchestration" / "coordinator.py"
        return shell_join(
            [sys.executable, str(script), "--repo", str(self.repo), *args]
        )
