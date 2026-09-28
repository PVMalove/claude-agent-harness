"""Единый источник истины для версии textual, запрашиваемой `harness console` через `uv run
--with` (ADR 0009). Секция `[dependency-groups].dev` в `pyproject.toml` фиксирует ту же версию,
чтобы локальные TUI-тесты выполнялись с ней без перезапуска; tests/test_console_pin.py проверяет их равенство."""

TEXTUAL_PIN = "6.5.0"
