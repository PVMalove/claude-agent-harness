"""Hardening tests for the terminal confirmation of a human approval (``approval``)."""

from __future__ import annotations

import io

import pytest

from harness.orchestration.core.utils import CoordinatorError
from harness.orchestration.workflow import approval


def _terminal(
    monkeypatch: pytest.MonkeyPatch, answer: str, *, sink_fails: bool = False
) -> list[io.StringIO]:
    """Replace the terminal with in-memory handles; return every handle the prompt opened."""
    opened: list[io.StringIO] = []

    def fake_open(path: str, mode: str, encoding: str) -> io.StringIO:
        if mode == "w" and sink_fails:
            raise OSError(f"cannot open {path} for writing")
        handle = io.StringIO(answer if mode == "r" else "")
        opened.append(handle)
        return handle

    monkeypatch.setattr(approval, "open", fake_open, raising=False)
    return opened


def test_terminal_without_sink_closes_the_opened_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened = _terminal(monkeypatch, "approve\n", sink_fails=True)

    with pytest.raises(CoordinatorError) as caught:
        approval._confirm_on_terminal("operator")

    assert "terminal" in caught.value.remedy
    assert len(opened) == 1
    assert opened[0].closed


@pytest.mark.parametrize(
    ("answer", "confirmed"), [("approve\n", True), ("no\n", False)]
)
def test_terminal_prompt_closes_both_handles(
    monkeypatch: pytest.MonkeyPatch, answer: str, confirmed: bool
) -> None:
    opened = _terminal(monkeypatch, answer)

    if confirmed:
        approval._confirm_on_terminal("operator", "a" * 64)
    else:
        with pytest.raises(CoordinatorError) as caught:
            approval._confirm_on_terminal("operator", "a" * 64)
        assert "interactive terminal" in caught.value.remedy

    assert [handle.closed for handle in opened] == [True, True]
