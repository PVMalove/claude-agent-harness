"""Ожидание состояния интерфейса в pilot-тестах: на Windows одной `pilot.pause()` не хватает,
чтобы экран успел смонтироваться, поэтому тест ждёт условие, а не фиксированное число пауз."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from textual.pilot import Pilot
from textual.widgets import Button, Static

from harness.console.crumbs import SEPARATOR
from harness.console.shell import Breadcrumbs

WAIT_SECONDS = 30.0
POLL_SECONDS = 0.05


async def wait_until[T](pilot: Pilot[T], ready: Callable[[], object]) -> None:
    """Ждать, пока `ready()` вернёт истинное значение; по истечении срока — `AssertionError`."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + WAIT_SECONDS
    while True:
        await pilot.pause()
        if ready():
            return
        if loop.time() >= deadline:
            raise AssertionError(
                f"UI did not reach the expected state in {WAIT_SECONDS}s "
                f"(screen: {type(pilot.app.screen).__name__})"
            )
        await asyncio.sleep(POLL_SECONDS)


async def press[T](pilot: Pilot[T], selector: str) -> None:
    """Дождаться включённой кнопки на текущем экране и нажать её без позиционного клика.

    `pilot.click` целится в координаты: на Windows длинный путь временного каталога переносит
    строки заголовка, кнопка уходит из окна или под другой виджет, и клик не доходит до неё.
    `Button.press()` отправляет то же событие `Pressed` без проверки попадания, но на
    отключённой кнопке молча ничего не делает, поэтому сначала ждём, пока экран её включит."""
    await wait_until(
        pilot,
        lambda: any(
            not button.disabled
            for button in pilot.app.screen.query(selector).results(Button)
        ),
    )
    pilot.app.screen.query_one(selector, Button).press()
    await pilot.pause()


def crumb_text(crumbs: Breadcrumbs) -> str:
    """Путь, как он нарисован: сегменты хлебных крошек через `SEPARATOR`, свёрнутые — как «…»."""
    parts = []
    for widget in crumbs.query(Static):
        text = str(widget.content)
        parts.append(
            SEPARATOR if widget.has_class("crumb-sep") and text.strip() == "›" else text
        )
    return "".join(parts)
