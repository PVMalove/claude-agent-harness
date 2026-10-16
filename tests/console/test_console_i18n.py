"""Russian / English switching: fixed strings translate, data and domain text stay as they are."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from harness.console import i18n
from harness.console.app import HarnessConsoleApp
from harness.console.help_text import HELP_MARKDOWN, HELP_MARKDOWN_EN, help_markdown
from harness.console.i18n_strings import EN, EN_PHRASES
from tests.console._pilot_wait import wait_until


def test_tr_translates_known_strings_and_leaves_the_rest() -> None:
    assert i18n.tr("en", "Назад") == "Back"
    assert i18n.tr("ru", "Назад") == "Назад"
    assert (
        i18n.tr("en", "Главная › Настройка › Версии")
        == "Home › Configuration › Versions"
    )
    assert i18n.tr("en", "Требует внимания · 2") == "Needs attention · 2"
    assert i18n.tr("en", "Цель: /srv/app") == "Target: /srv/app"
    assert (
        i18n.tr("en", "любой пользовательский текст") == "любой пользовательский текст"
    )
    # Labels written in English become Russian when the language is Russian.
    assert i18n.tr("ru", "Both files") == "Оба файла"
    assert i18n.tr("en", "Both files") == "Both files"


def test_tr_text_uses_whole_words_so_data_is_not_garbled() -> None:
    translated = i18n.tr_text("en", "в снэпшоте скиллов есть расхождения")
    assert translated == "в снэпшоте скиллов есть расхождения"
    assert i18n.tr_text("en", "память: ok, пропущено 11") == "memory: ok, skipped 11"
    assert i18n.tr_text("ru", "память: ok") == "память: ok"


def test_every_english_text_is_distinct_work_not_a_copy() -> None:
    for table in (EN, EN_PHRASES):
        for russian, english in table.items():
            assert russian.strip() and english.strip()
    # The Russian side of a phrase never contains Latin-only text that would match code.
    assert all(re.search("[А-Яа-я]", russian) for russian in EN_PHRASES)


def test_the_english_guide_has_the_same_sections() -> None:
    assert help_markdown("ru") == HELP_MARKDOWN
    assert help_markdown("en") == HELP_MARKDOWN_EN
    assert HELP_MARKDOWN.count("\n## ") == HELP_MARKDOWN_EN.count("\n## ")
    assert not re.search("[А-Яа-я]", HELP_MARKDOWN_EN)


def _dump(app: HarnessConsoleApp) -> str:
    strips = app.screen._compositor.render_strips()
    return "\n".join("".join(segment.text for segment in strip) for strip in strips)


def test_f2_switches_the_dashboard_and_back_without_drift(tmp_path: Path) -> None:
    async def scenario() -> tuple[str, str, str]:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(130, 50)) as pilot:
            # The dashboard fills in asynchronously; a slow machine must not be compared half-built.
            await app.workers.wait_for_complete()
            # Footer bindings recompose independently of workers and the Pilot idle barrier.
            await wait_until(pilot, lambda: "r Офлайн" in _dump(app).splitlines()[-1])
            before = _dump(app)
            await pilot.press("f2")
            await wait_until(pilot, lambda: "r Offline" in _dump(app).splitlines()[-1])
            english = _dump(app)
            await pilot.press("f2")
            await wait_until(pilot, lambda: "r Офлайн" in _dump(app).splitlines()[-1])
            return before, english, _dump(app)

    before, english, after = asyncio.run(scenario())
    assert after == before
    assert "Main menu" in english and "Workspace" in english
    assert "Detailed technical report" in english
    assert "Open project" in english and "Диагностика" not in english


def test_f2_translates_screens_that_are_already_open_below(tmp_path: Path) -> None:
    async def scenario() -> tuple[str, str]:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(130, 50)) as pilot:
            await pilot.pause()
            await pilot.press("9")  # Help
            await pilot.pause()
            await pilot.press("f2")
            await pilot.pause()
            help_screen = _dump(app)
            await pilot.press("escape")
            await pilot.pause()
            return help_screen, _dump(app)

    help_screen, dashboard = asyncio.run(scenario())
    assert "Console guide" in help_screen and "Main menu" in dashboard


def test_command_menus_translate_groups_titles_and_footer(tmp_path: Path) -> None:
    async def scenario() -> str:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(130, 50)) as pilot:
            await pilot.pause()
            await pilot.press("f2")
            await pilot.press("4")  # Harness
            await pilot.pause()
            await pilot.pause()
            return _dump(app)

    screen = asyncio.run(scenario())
    assert "INSTALL & UPDATE" in screen
    assert "Install Harness" in screen and "Update Harness" in screen
    assert "Back" in screen and "Copy CLI" in screen


@pytest.mark.parametrize("language", ["ru", "en"])
def test_the_default_language_comes_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, language: str
) -> None:
    monkeypatch.setenv(i18n.LANGUAGE_ENV, language)
    assert i18n.default_language() == language
    monkeypatch.setenv(i18n.LANGUAGE_ENV, "xx")
    assert i18n.default_language() == "ru"
