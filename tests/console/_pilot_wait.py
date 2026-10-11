"""Ожидание состояния интерфейса в pilot-тестах: на Windows одной `pilot.pause()` не хватает,
чтобы экран успел смонтироваться, поэтому тест ждёт условие, а не фиксированное число пауз."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from textual.pilot import Pilot

WAIT_SECONDS = 10.0
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
                f"UI did not reach the expected state in {WAIT_SECONDS}s"
            )
        await asyncio.sleep(POLL_SECONDS)
