"""Single source of truth for the textual version `harness console` requests from `uv run
--with` (ADR 0009). `pyproject.toml`'s `[dependency-groups].dev` pins the same version so local
TUI tests run against it without a relaunch; tests/test_console_pin.py keeps the two equal."""

TEXTUAL_PIN = "6.5.0"
