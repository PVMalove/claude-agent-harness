"""One status vocabulary for the whole console: every status is a symbol plus text, never colour alone.

Six kinds carry the symbol and the palette colour; a `Status` adds its own RU/EN text, so a screen
renders a session state, a pending permission or a process state the same way. Stdlib-only, like
the rest of the textual-free seam."""

from __future__ import annotations

from dataclasses import dataclass

from . import brand

# kind -> (single-cell symbol, palette key)
KINDS = {
    "accepted": ("✔", "success"),
    "running": ("▶", "success"),
    "paused": ("Ⅱ", "muted"),
    "not_started": ("○", "muted"),
    "error": ("✖", "error"),
    "warning": ("⚠", "warning"),
}


@dataclass(frozen=True)
class Status:
    kind: str
    ru: str
    en: str

    @property
    def symbol(self) -> str:
        return KINDS[self.kind][0]

    @property
    def color(self) -> str:
        return brand.PALETTE[KINDS[self.kind][1]]

    def text(self, language: str) -> str:
        return self.en if language == "en" else self.ru

    def render(self, language: str) -> str:
        """The symbol and the text in `language`."""
        return f"{self.symbol} {self.text(language)}"


_SESSION = {
    "working": Status("running", "Выполняется", "Running"),
    "dispatched": Status("paused", "Запуск", "Starting"),
    "approved": Status("not_started", "Готов к запуску", "Ready to start"),
    "reported": Status("accepted", "Отчёт получен", "Report received"),
    "blocked": Status("error", "Работа заблокирована", "Work blocked"),
    "failed": Status("error", "Ошибка", "Error"),
    "cancelled": Status("paused", "Отменён", "Cancelled"),
    "abandoned": Status("paused", "Завершён", "Finished"),
}
_UNCONFIRMED = Status("warning", "Нет подтверждения", "Unconfirmed")

PERMISSION_PENDING = Status("warning", "Ждёт разрешения", "Permission pending")

_PROCESS = {
    "absent": Status("warning", "Процесс не найден", "Process not found"),
    "stopped": Status("paused", "Процесс завершён", "Process exited"),
}
_OUTCOME_UNKNOWN = Status("warning", "Исход неизвестен", "Outcome unknown")


def session_status(state: str) -> Status:
    """The status of a session in ledger state `state`; an unknown state is unconfirmed."""
    return _SESSION.get(state, _UNCONFIRMED)


def process_status(worker: str) -> Status:
    """The status of the worker process of a session that should still be running."""
    return _PROCESS.get(worker, _OUTCOME_UNKNOWN)
