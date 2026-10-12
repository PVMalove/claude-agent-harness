"""Console tests never probe the real Codex/Claude CLIs unless a test opts in."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def offline_advisor_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    from harness.console.screens import configuration

    monkeypatch.setattr(
        configuration, "availability", lambda provider, cancel: "Готов к запросу (тест)"
    )


@pytest.fixture(autouse=True)
def private_clipboard(monkeypatch: pytest.MonkeyPatch) -> None:
    """A test never reads or overwrites the developer's real clipboard."""
    monkeypatch.setenv("HARNESS_CONSOLE_SYSTEM_CLIPBOARD", "0")
