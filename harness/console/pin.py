"""Единый источник истины для версии textual, запрашиваемой `harness console` через `uv run
--with` (ADR 0009). Секция `[dependency-groups].dev` в `pyproject.toml` фиксирует ту же версию,
чтобы локальные TUI-тесты выполнялись с ней без перезапуска; tests/console/test_console_pin.py проверяет их равенство."""

TEXTUAL_PIN = "6.5.0"

# Сервер консоли и веб-клиент (ADR 0028): тот же механизм `uv run --with`. Dev-группа повторяет эти версии,
# а tests/console/test_console_pin.py проверяет их равенство.
H11_PIN = "0.16.0"
WEBSOCKETS_PIN = "17.3"
JINJA2_PIN = "3.1.6"
CONSOLE_SERVER_REQUIREMENTS = (
    f"h11=={H11_PIN}",
    f"websockets=={WEBSOCKETS_PIN}",
    f"jinja2=={JINJA2_PIN}",
)
