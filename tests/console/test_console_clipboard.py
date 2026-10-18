"""The console reads and writes the system clipboard, so Ctrl+C / Ctrl+V reach other programs."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path
from typing import Sequence

import pytest
from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Input, TextArea

from harness.console import clipboard
from harness.console.app import HarnessConsoleApp


class _Tool:
    """A fake clipboard tool: `wl-paste` returns `content`, `wl-copy` stores its input."""

    def __init__(self, content: bytes = b"", code: int = 0) -> None:
        self.content = content
        self.code = code
        self.calls: list[list[str]] = []

    def __call__(
        self, argv: Sequence[str], **kwargs: object
    ) -> "subprocess.CompletedProcess[bytes]":
        self.calls.append(list(argv))
        data = kwargs.get("input")
        if isinstance(data, bytes):
            self.content = data
        return subprocess.CompletedProcess(
            list(argv), self.code, self.content if argv[0] == "wl-paste" else b"", b""
        )


@pytest.fixture
def wayland(monkeypatch: pytest.MonkeyPatch) -> _Tool:
    monkeypatch.setenv("HARNESS_CONSOLE_SYSTEM_CLIPBOARD", "1")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: "/usr/bin/" + name if name.startswith("wl-") else None,
    )
    tool = _Tool(b"from outside")
    monkeypatch.setattr(subprocess, "run", tool)
    return tool


def test_reads_and_writes_through_the_platform_tool(wayland: _Tool) -> None:
    assert clipboard.read_system() == "from outside"
    assert clipboard.write_system("копия") is True
    assert wayland.content == "копия".encode()
    assert clipboard.read_system() == "копия"


def test_an_empty_or_failing_clipboard_means_none(wayland: _Tool) -> None:
    wayland.code = 1  # wl-paste: "Nothing is copied"
    assert clipboard.read_system() is None
    wayland.code, wayland.content = 0, b""
    assert clipboard.read_system() is None


def test_no_tool_means_no_system_clipboard(
    wayland: _Tool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert clipboard.read_system() is None
    assert clipboard.write_system("x") is False


def test_the_switch_turns_the_system_clipboard_off(
    wayland: _Tool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESS_CONSOLE_SYSTEM_CLIPBOARD", "0")
    assert clipboard.read_system() is None
    assert wayland.calls == []


class _Editors(Screen[None]):
    def compose(self) -> ComposeResult:
        yield Input("", id="line")
        yield TextArea("", id="area")


def test_ctrl_v_pastes_the_system_clipboard_and_ctrl_c_copies_to_it(
    wayland: _Tool, tmp_path: Path
) -> None:
    async def scenario() -> tuple[str, str, bytes]:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test() as pilot:
            await app.push_screen(_Editors())
            await pilot.pause()
            area = app.screen.query_one("#area", TextArea)
            area.focus()
            await pilot.press("ctrl+v")
            await pilot.pause()
            pasted = area.text
            area.select_all()
            wayland.content = b"overwritten later"
            await pilot.press("ctrl+c")
            await pilot.pause()
            return pasted, area.text, wayland.content

    pasted, text, stored = asyncio.run(scenario())
    assert pasted == "from outside"
    assert text == "from outside"
    assert stored == b"from outside"
