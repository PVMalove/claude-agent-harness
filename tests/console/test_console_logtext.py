"""Coloured logs and ☑ / ☐ widgets."""

from __future__ import annotations

import asyncio

from rich.text import Text
from textual.app import App
from textual.widgets.selection_list import Selection

from harness.console import brand
from harness.console.logtext import colorize_diff, colorize_log, colorize_overview
from harness.console.widgets import CHECKED, UNCHECKED, CheckBox, CheckList


def _styles(text: Text) -> dict[str, str]:
    return {text.plain[span.start : span.end]: str(span.style) for span in text.spans}


def test_log_colours_statuses_counters_exit_codes_and_commands() -> None:
    log = colorize_log(
        "$ harness update /x\n"
        "код выхода: 1\n"
        "Итого: ok=42 warn=0 fail=2 skipped=11\n"
        "Требует исправления: 2 — см. «Как исправить»\n"
        "[FAIL] files.lock: нет\n"
        "Traceback (most recent call last):"
    )
    styles = _styles(log)
    colors = brand.STATUS_COLORS
    assert log.plain.count("\n") == 5
    assert colors["fail"] in styles["1"] and "bold" in styles["1"]
    assert colors["ok"] in styles["ok=42"]
    assert brand.PALETTE["muted"] in styles["warn=0"]
    assert colors["fail"] in styles["fail=2"]
    assert colors["fail"] in styles["[FAIL]"]
    assert colors["fail"] in styles["Traceback (most recent call last):"]


def test_a_zero_exit_code_is_green_and_the_text_is_unchanged() -> None:
    text = "код выхода: 0\nготово"
    log = colorize_log(text)
    assert log.plain == text
    assert brand.STATUS_COLORS["ok"] in _styles(log)["0"]


def test_diff_colours_additions_removals_and_hunks() -> None:
    diff = colorize_diff("--- a\n+++ b\n@@ -1 +1 @@\n-old\n+new\n same")
    styles = _styles(diff)
    assert brand.STATUS_COLORS["fail"] in styles["-old"]
    assert brand.STATUS_COLORS["ok"] in styles["+new"]
    assert brand.PALETTE["secondary"] in styles["@@ -1 +1 @@"]
    assert diff.plain.endswith(" same")


def test_overview_keys_are_accented() -> None:
    overview = colorize_overview("Скилов: 32\nбез двоеточия")
    assert brand.PALETTE["primary"] in _styles(overview)["Скилов:"]
    assert overview.plain == "Скилов: 32\nбез двоеточия"


def test_checkbox_and_checklist_draw_ticked_and_unticked_differently() -> None:
    class Host(App[None]):
        def compose(self):  # type: ignore[no-untyped-def]
            yield CheckBox("on", value=True, id="on")
            yield CheckBox("off", value=False, id="off")
            yield CheckList(
                Selection("first", "first", True), Selection("second", "second", False)
            )

    async def scenario() -> list[str]:
        app = Host()
        async with app.run_test(size=(40, 12)) as pilot:
            await pilot.pause()
            strips = app.screen._compositor.render_strips()
            return ["".join(segment.text for segment in strip) for strip in strips]

    lines = asyncio.run(scenario())
    joined = "\n".join(lines)
    assert CHECKED in joined and UNCHECKED in joined
    ticked = next(line for line in lines if "first" in line)
    unticked = next(line for line in lines if "second" in line)
    assert CHECKED in ticked and UNCHECKED in unticked
    assert next(line for line in lines if "on" in line.split()).count(CHECKED) == 1
