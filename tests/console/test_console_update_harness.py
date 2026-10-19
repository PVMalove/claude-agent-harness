"""The Update Harness screen: versions, drift, diff, mode and the confirmed run."""

from __future__ import annotations

import asyncio
import subprocess
import threading
from pathlib import Path
from typing import Mapping, Sequence

import re

import pytest
from rich.text import Text
from textual.app import App
from textual.screen import Screen
from textual.widgets import Button, ListView, RadioButton, SelectionList, Static

from tests.console._pilot_wait import crumb_text, press, wait_until
from harness.console import stats as console_stats
from harness.console.app import HarnessConsoleApp
from harness.console.shell import (
    SECTIONS,
    Breadcrumbs,
    ConsoleShell,
    Fold,
)
from harness.console.catalog import seed_update_entry
from harness.console.screens.harness import CommandFormScreen
from harness.console.screens.update_harness import (
    SEED_MODE,
    UpdateHarnessScreen,
    render_drift,
    render_versions,
)

PAYLOAD: dict[str, object] = {
    "state": "drift",
    "package_changed": [".harness/a.py", ".harness/b.py"],
    "added": [".harness/new.py"],
    "missing": [],
    "retired": [],
    "local_changed": [".harness/local.py"],
    "conflict": [".harness/conflict.py"],
    "seed_differing": [".claude/rules/one.md", ".claude/rules/two.md"],
}


def _state(*, installed: bool = True) -> console_stats.HarnessState:
    if not installed:
        return console_stats.HarnessState(False, "", (), 0, 0, "", False)
    return console_stats.HarnessState(
        True, "1.3.2", ("backend-orchestration",), 32, 219, "2026-10-11 11:00 UTC", True
    )


class _Runner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> "subprocess.CompletedProcess[str]":
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(list(argv), 0, "обновлено\n", "")


def _screen(
    tmp_path: Path,
    runner: _Runner,
    *,
    installed: bool = True,
    payload: dict[str, object] | None = None,
) -> UpdateHarnessScreen:
    return UpdateHarnessScreen(
        tmp_path,
        command_runner=runner,
        load_state=lambda _repo: _state(installed=installed),
        load_drift=lambda _repo: PAYLOAD if payload is None else payload,
        load_diff=lambda _repo,
        relative: f"--- проект/{relative}\n+++ после обновления/{relative}\n-old\n+new\n",
    )


def _card_title(card: object) -> str:
    """A block's title as drawn: a card's border title, a fold's title row without its summary."""
    if isinstance(card, Fold):
        return Text.from_markup(card.title).plain.split("  ")[0].strip().upper()
    return str(card.border_title).strip()  # type: ignore[attr-defined]


def _plain(widget: object) -> str:
    return str(widget.content)  # type: ignore[attr-defined]


def _console(screen: UpdateHarnessScreen, repo: Path) -> HarnessConsoleApp:
    """The real console app, with the screen pushed over its home: the menu, language and footer work."""
    app = HarnessConsoleApp(repo)
    app.call_later(app.push_screen, screen)
    return app


async def _ready(
    pilot: object, app: App[None], loaded: bool = True
) -> UpdateHarnessScreen:
    await wait_until(pilot, lambda: isinstance(app.screen, UpdateHarnessScreen))  # type: ignore[arg-type]
    screen = app.screen
    assert isinstance(screen, UpdateHarnessScreen)
    if loaded:
        await app.workers.wait_for_complete()
        await wait_until(pilot, lambda: screen._payload_ready)  # type: ignore[arg-type]
        await pilot.pause()  # type: ignore[attr-defined]
    return screen


def _host(screen: UpdateHarnessScreen) -> App[None]:
    class Host(App[None]):
        def on_mount(self) -> None:
            self.push_screen(screen)

    return Host()


def test_versions_and_drift_are_rendered_with_counters() -> None:
    versions = render_versions(_state(), "1.4.0")
    assert "1.3.2" in versions.plain and "1.4.0" in versions.plain
    assert "Доступно обновление" in versions.plain
    assert "Версия актуальна" in render_versions(_state(), "1.3.2").plain
    assert "не установлен" in render_versions(_state(installed=False), "1.4.0").plain
    drift = render_drift(PAYLOAD)
    assert "обновятся" in drift.plain and "в конфликте" in drift.plain
    assert "Расхождений нет" in render_drift({"state": "clean"}).plain
    assert "Not installed" not in render_drift(None, language="en").plain
    assert "Could not check drift" in render_drift(None, language="en").plain


def test_the_screen_shows_every_block_and_the_drift_counters(tmp_path: Path) -> None:
    async def scenario() -> tuple[list[str], str, int]:
        app = _host(_screen(tmp_path, _Runner()))
        async with app.run_test(size=(120, 60)) as pilot:
            await wait_until(
                pilot,
                lambda: bool(app.screen.query(".section-card"))
                and bool(app.screen.query("#diff-files")),
            )
            screen = app.screen
            titles = [_card_title(card) for card in screen.query(".section-card")]
            drift = str(screen.query_one("#update-drift", Static).content)
            files = len(screen.query_one("#diff-files", ListView))
            return titles, drift, files

    titles, drift, files = asyncio.run(scenario())
    assert {"ВЕРСИИ", "РЕЖИМ ОБНОВЛЕНИЯ", "ПЛАН", "РЕЗУЛЬТАТ"} <= {
        title.split("  ")[0].strip() for title in titles
    }
    assert any(title.upper().startswith("DIFF") for title in titles)
    assert "обновятся" in drift
    # Every drift field plus the seed files that differ from the template, once each.
    assert files == 2 + 1 + 1 + 1 + 2


def test_update_asks_for_the_same_confirmation_and_runs_only_after_it(
    tmp_path: Path,
) -> None:
    runner = _Runner()

    async def scenario() -> tuple[bool, str, str]:
        app = _host(_screen(tmp_path, runner))
        async with app.run_test(size=(120, 60)) as pilot:
            await pilot.pause()
            await press(pilot, "#update-run")
            await wait_until(pilot, lambda: isinstance(app.screen, CommandFormScreen))
            assert isinstance(app.screen, CommandFormScreen)
            assert app.screen.entry.key == "update"
            assert not runner.calls
            await pilot.press("escape")
            await wait_until(pilot, lambda: isinstance(app.screen, UpdateHarnessScreen))
            assert isinstance(app.screen, UpdateHarnessScreen)
            assert not runner.calls
            await press(pilot, "#update-run")
            await wait_until(pilot, lambda: isinstance(app.screen, CommandFormScreen))
            await press(pilot, "#run")
            await wait_until(pilot, lambda: isinstance(app.screen, UpdateHarnessScreen))
            await app.workers.wait_for_complete()
            await pilot.pause()
            output = str(app.screen.query_one("#update-output", Static).content)
            return bool(runner.calls), output, type(app.screen).__name__

    ran, output, screen_name = asyncio.run(scenario())
    assert ran and screen_name == "UpdateHarnessScreen"
    assert "код выхода: 0" in output and "обновлено" in output
    assert runner.calls[0][-1] == str(tmp_path) and "update" in runner.calls[0]


def test_nothing_runs_when_harness_is_not_installed(tmp_path: Path) -> None:
    runner = _Runner()

    async def scenario() -> tuple[bool, str]:
        app = _host(_screen(tmp_path, runner, installed=False))
        async with app.run_test(size=(120, 60)) as pilot:
            await pilot.pause()
            return (
                app.screen.query_one("#update-run", Button).disabled,
                str(app.screen.query_one("#update-drift", Static).content),
            )

    disabled, drift = asyncio.run(scenario())
    assert disabled and "сравнивать нечего" in drift
    assert not runner.calls


def test_seed_mode_updates_only_the_ticked_files(tmp_path: Path) -> None:
    runner = _Runner()

    async def scenario() -> None:
        app = _host(_screen(tmp_path, runner))
        async with app.run_test(size=(120, 60)) as pilot:
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, UpdateHarnessScreen)
            assert not screen.query_one("#seed-frame").display
            screen.query_one("#update-mode").query(RadioButton).last(
                RadioButton
            ).value = True
            await pilot.pause()
            assert screen._mode() == SEED_MODE
            assert screen.query_one("#seed-frame").display
            files = screen.query_one("#seed-files", SelectionList)
            assert list(files.selected) == []
            assert screen.query_one("#update-run", Button).disabled
            await press(pilot, "#seed-all")
            await wait_until(
                pilot,
                lambda: sorted(files.selected) == PAYLOAD["seed_differing"],
            )
            assert sorted(files.selected) == PAYLOAD["seed_differing"]
            await press(pilot, "#seed-none")
            await wait_until(pilot, lambda: not files.selected)
            assert not files.selected
            files.select(".claude/rules/two.md")
            await wait_until(
                pilot,
                lambda: not screen.query_one("#update-run", Button).disabled,
            )
            assert not screen.query_one("#update-run", Button).disabled
            await press(pilot, "#update-run")
            await wait_until(pilot, lambda: isinstance(app.screen, CommandFormScreen))
            assert isinstance(app.screen, CommandFormScreen)
            assert app.screen.entry.cli_argv(tmp_path)[3:] == [
                "--force-seed-file",
                ".claude/rules/two.md",
            ]
            await press(pilot, "#run")
            await wait_until(pilot, lambda: isinstance(app.screen, UpdateHarnessScreen))
            await app.workers.wait_for_complete()

    asyncio.run(scenario())
    assert runner.calls
    assert runner.calls[0][-2:] == ["--force-seed-file", ".claude/rules/two.md"]


def test_seed_update_entry_builds_one_flag_per_file(tmp_path: Path) -> None:
    entry = seed_update_entry(["a.md", "b/c.md"])
    assert entry.cli_argv(tmp_path) == [
        "harness",
        "update",
        str(tmp_path),
        "--force-seed-file",
        "a.md",
        "--force-seed-file",
        "b/c.md",
    ]
    assert entry.needs_confirmation


def test_the_diff_block_shows_the_diff_of_the_highlighted_file(tmp_path: Path) -> None:
    async def scenario() -> tuple[str, str]:
        app = _host(_screen(tmp_path, _Runner()))
        async with app.run_test(size=(120, 60)) as pilot:
            await app.workers.wait_for_complete()
            await wait_until(
                pilot,
                lambda: bool(app.screen.query("#diff-files"))
                and app.screen.query_one("#diff-files", ListView).highlighted_child
                is not None
                and bool(app.screen.query("#diff-view")),
            )
            listing = app.screen.query_one("#diff-files", ListView)
            first = listing.highlighted_child
            assert first is not None and first.name
            return first.name, str(app.screen.query_one("#diff-view", Static).content)

    name, view = asyncio.run(scenario())
    assert name == "conflict.py" or name.endswith("conflict.py")
    assert "+new" in view and "-old" in view


def test_the_screen_follows_the_language(tmp_path: Path) -> None:
    async def scenario() -> tuple[str, str, str]:
        app = _host(_screen(tmp_path, _Runner()))
        async with app.run_test(size=(120, 60)) as pilot:
            await pilot.pause()
            app.language = "en"  # type: ignore[attr-defined]
            from harness.console import translate

            translate.retranslate(app.screen, "en")
            await pilot.pause()
            return (
                str(app.screen.query_one("#update-versions", Static).content),
                str(app.screen.query_one("#update-run", Button).label),
                str(app.screen.query_one("#update-drift", Static).content),
            )

    versions, run, drift = asyncio.run(scenario())
    assert "Update available" in versions and "Installed" in versions
    assert run == "Update"
    assert "will be updated" in drift


def test_update_harness_stands_on_the_shared_frame(tmp_path: Path) -> None:
    from harness.console.chrome import TopBar, WorkspaceHeader

    async def scenario() -> None:
        app = _console(_screen(tmp_path, _Runner()), tmp_path)
        async with app.run_test(size=(160, 52)) as pilot:
            screen = await _ready(pilot, app)
            assert isinstance(screen, ConsoleShell)
            assert screen.SECTION == "Update Harness"
            assert [type(bar) for bar in screen.query(TopBar)] == [WorkspaceHeader]
            assert screen.query_one(
                "#shell-sections", ListView
            ).index == SECTIONS.index("Update Harness")
            await wait_until(
                pilot, lambda: bool(screen.query_one(Breadcrumbs).query(Static))
            )
            crumbs = crumb_text(screen.query_one(Breadcrumbs))
            assert crumbs.startswith("Главная") and crumbs.endswith("Обновить Harness")
            assert (
                _plain(screen.query_one("#update-project"))
                == "Проект: " + tmp_path.name
            )
            assert _plain(screen.query_one("#update-title")) == "Обновить Harness"
            strip = " ".join(_plain(w) for w in screen.query(".context-strip Static"))
            assert "Команда обновления — та же, что в CLI: harness update" in strip

    asyncio.run(scenario())


def test_the_blocks_come_in_the_mockup_order_at_160x52(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = _console(_screen(tmp_path, _Runner()), tmp_path)
        async with app.run_test(size=(160, 52)) as pilot:
            screen = await _ready(pilot, app)
            region = {
                name: screen.query_one(f"#{name}").region
                for name in (
                    "versions-frame",
                    "drift-frame",
                    "mode-frame",
                    "plan-frame",
                    "diff-frame",
                    "output-frame",
                )
            }
            assert region["versions-frame"].y == region["mode-frame"].y
            assert region["versions-frame"].x < region["mode-frame"].x
            assert region["drift-frame"].y > region["versions-frame"].y
            assert region["drift-frame"].x == region["versions-frame"].x
            assert region["plan-frame"].y > region["mode-frame"].y
            assert region["plan-frame"].x == region["mode-frame"].x
            assert region["diff-frame"].y >= max(
                region["drift-frame"].bottom, region["plan-frame"].bottom
            )
            assert region["output-frame"].y > region["diff-frame"].y
            assert _card_title(screen.query_one("#drift-frame")).startswith(
                "ЧТО ИЗМЕНИТСЯ В УПРАВЛЯЕМЫХ ФАЙЛАХ"
            )
            assert (
                "ИЗ HARNESS DIFF"
                in _card_title(screen.query_one("#drift-frame")).upper()
            )
            for name in ("mode-frame", "diff-frame", "output-frame"):
                assert not screen.query_one(f"#{name}", Fold).collapsed
            assert [
                str(b.label)
                for b in screen.query("#plan-frame Button.narrow-only").results(Button)
            ] == ["Проверить заново"]
            assert screen.query_one("#update-recheck").display
            assert not screen.query_one("#update-recheck-narrow").display

    asyncio.run(scenario())


def test_narrow_folds_mode_diff_and_result_and_moves_the_recheck_button(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        app = _console(_screen(tmp_path, _Runner()), tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            screen = await _ready(pilot, app)
            for name in ("mode-frame", "diff-frame", "output-frame"):
                assert screen.query_one(f"#{name}", Fold).collapsed, name
            assert not screen.query_one("#update-recheck").display
            assert screen.query_one("#update-recheck-narrow").display
            assert not screen.query_one("#update-subtitle").display
            versions = _plain(screen.query_one("#update-versions"))
            assert "Установлено" in versions and "Доступно" in versions
            assert "Состав" in versions and "Скилов" not in versions
            assert "актуальна" in _card_title(
                screen.query_one("#versions-frame")
            ).lower() or (
                "доступно обновление"
                in _card_title(screen.query_one("#versions-frame")).lower()
            )
            summaries = {
                name: Text.from_markup(screen.query_one(f"#{name}", Fold).title).plain
                for name in ("mode-frame", "diff-frame", "output-frame")
            }
            assert "Обычное" in summaries["mode-frame"]
            assert (
                "7 файлов" in summaries["diff-frame"]
                and "1 конфликт" in summaries["diff-frame"]
            )
            assert "пока пусто" in summaries["output-frame"]

    asyncio.run(scenario())


def test_footer_keys_equal_the_mockup_sets(tmp_path: Path) -> None:
    async def keys(size: tuple[int, int]) -> set[str]:
        app = _console(_screen(tmp_path, _Runner()), tmp_path)
        async with app.run_test(size=size) as pilot:
            screen = await _ready(pilot, app)
            return {
                key
                for key, active in screen.active_bindings.items()
                if active.binding.show and key != app.COMMAND_PALETTE_BINDING
            }

    assert asyncio.run(keys((160, 52))) == {"escape", "f1", "f2"}
    assert asyncio.run(keys((80, 24))) == {"escape", "f2"}


def test_counters_and_file_tags_carry_symbols() -> None:
    drift = render_drift(PAYLOAD).plain.splitlines()
    assert drift == [
        "   2 ✔ обновятся",
        "   1 ✔ добавятся",
        "   1 ⚠ с локальными правками (сохранятся)",
        "   1 ✖ в конфликте (нужна перезапись)",
    ]
    narrow = render_drift(PAYLOAD, narrow=True).plain.splitlines()
    assert narrow[-2:] == ["   1 ⚠ ваши правки сохранятся", "   1 ✖ конфликт"]
    assert (
        render_drift(PAYLOAD, language="en").plain.splitlines()[0]
        == "   2 ✔ will be updated"
    )


def test_file_tags_in_the_diff_list_are_a_symbol_and_a_word(tmp_path: Path) -> None:
    async def scenario() -> list[str]:
        app = _console(_screen(tmp_path, _Runner()), tmp_path)
        async with app.run_test(size=(160, 52)) as pilot:
            screen = await _ready(pilot, app)
            return [
                _plain(item.query_one(Static))
                for item in screen.query_one("#diff-files", ListView).children
            ]

    rows = asyncio.run(scenario())
    assert re.match(r"✖ конфликт +\.harness/conflict\.py", rows[0])
    assert any(re.match(r"✔ обновится +\.harness/a\.py", row) for row in rows)
    assert any(re.match(r"⚠ ваши правки +\.harness/local\.py", row) for row in rows)
    assert any(re.match(r"⚠ seed +\.claude/rules/one\.md", row) for row in rows)


def test_the_versions_verdict_is_a_symbol_and_a_word() -> None:
    current = render_versions(_state(), "1.3.2").plain
    assert "✔ Версия актуальна" in current
    assert "Состав" in current and "Скилов" in current and "Lock обновлён" in current
    assert "⚠ Доступно обновление" in render_versions(_state(), "1.4.0").plain
    narrow = render_versions(_state(), "1.3.2", narrow=True).plain
    assert "Установлено" in narrow and "Состав" in narrow and "Скилов" not in narrow
    missing = render_versions(_state(installed=False), "1.4.0").plain.splitlines()
    assert missing == [
        "✖ Харнесс не установлен: нет читаемого .harness/harness.lock",
        "Установите его через «Открыть проект».",
    ]


@pytest.mark.parametrize(
    ("conflicts", "size", "expected"),
    [
        (1, (160, 52), "⚠ Обычное обновление остановится: 1 конфликт"),
        (2, (160, 52), "⚠ Обычное обновление остановится: 2 конфликта"),
        (5, (160, 52), "⚠ Обычное обновление остановится: 5 конфликтов"),
        (1, (80, 24), "⚠ Остановится: 1 конфликт"),
    ],
)
def test_the_plan_warns_that_a_normal_update_stops_on_conflicts(
    tmp_path: Path, conflicts: int, size: tuple[int, int], expected: str
) -> None:
    payload = {**PAYLOAD, "conflict": [f".harness/c{i}.md" for i in range(conflicts)]}

    async def scenario() -> tuple[str, bool, bool, bool]:
        app = _console(_screen(tmp_path, _Runner(), payload=payload), tmp_path)
        async with app.run_test(size=size) as pilot:
            screen = await _ready(pilot, app)
            warning = screen.query_one("#update-warning", Static)
            shown, text = warning.display, _plain(warning)
            screen.query_one("#update-mode").query(RadioButton)[1].value = True
            await pilot.pause()
            forced = warning.display
            screen._payload = {**payload, "conflict": []}
            screen.query_one("#update-mode").query(RadioButton)[0].value = True
            await pilot.pause()
            return text, shown, forced, warning.display

    text, shown, forced, without = asyncio.run(scenario())
    assert text.splitlines()[0] == expected
    assert shown and not forced and not without


def test_the_selected_mode_names_its_consequence(tmp_path: Path) -> None:
    async def scenario() -> list[tuple[str, str]]:
        app = _console(_screen(tmp_path, _Runner()), tmp_path)
        async with app.run_test(size=(160, 52)) as pilot:
            screen = await _ready(pilot, app)
            seen = []
            radios = list(screen.query_one("#update-mode").query(RadioButton))
            assert [str(r.label) for r in radios] == [
                "Обычное",
                "С перезаписью управляемых файлов",
                "С перезаписью выбранных seed-файлов",
            ]
            for radio in radios:
                radio.value = True
                await pilot.pause()
                seen.append(
                    (
                        _plain(screen.query_one("#mode-caption")),
                        Text.from_markup(
                            screen.query_one("#mode-frame", Fold).title
                        ).plain,
                    )
                )
            return seen

    normal, force, seed = asyncio.run(scenario())
    assert normal[0] == "Локальные правки сохраняются. Остановится на конфликтах."
    assert force[0].startswith("⚠ Заменит и конфликтные файлы версией пакета")
    assert seed[0] == "Только отмеченные seed-файлы; остальное не меняется."
    assert "Обычное" in normal[1] and "С перезаписью управляемых файлов" in force[1]


def test_every_disabled_action_says_why(tmp_path: Path) -> None:
    async def scenario() -> dict[str, str]:
        shown: dict[str, str] = {}
        app = _console(_screen(tmp_path, _Runner(), installed=False), tmp_path)
        async with app.run_test(size=(160, 52)) as pilot:
            screen = await _ready(pilot, app, loaded=False)
            await wait_until(
                pilot, lambda: _plain(screen.query_one("#plan-reason")) != ""
            )
            shown["not installed"] = _plain(screen.query_one("#plan-reason"))
            shown["not installed tooltip"] = str(
                screen.query_one("#update-run", Button).tooltip
            )
        app = _console(_screen(tmp_path, _Runner()), tmp_path)
        async with app.run_test(size=(160, 52)) as pilot:
            screen = await _ready(pilot, app)
            screen.query_one("#update-mode").query(RadioButton)[2].value = True
            await wait_until(
                pilot, lambda: _plain(screen.query_one("#plan-reason")) != ""
            )
            shown["seed"] = _plain(screen.query_one("#plan-reason"))
            screen._set_busy(True)
            await pilot.pause()
            shown["busy plan"] = _plain(screen.query_one("#plan-reason"))
            shown["busy drift"] = _plain(screen.query_one("#drift-reason"))
            shown["busy seed"] = _plain(screen.query_one("#seed-reason"))
            assert all(
                screen.query_one(f"#{name}", Button).disabled
                for name in ("update-run", "update-recheck", "seed-all", "seed-none")
            )
            screen._set_busy(False)
            await pilot.pause()
            shown["idle drift"] = _plain(screen.query_one("#drift-reason"))
        return shown

    shown = asyncio.run(scenario())
    assert shown["not installed"] == "«Обновить»: Harness не установлен"
    assert shown["not installed tooltip"] == "Harness не установлен"
    assert shown["seed"] == "«Обновить»: Отметьте хотя бы один файл"
    assert shown["busy plan"] == "«Обновить»: обновление выполняется"
    assert shown["busy drift"] == "«Проверить заново»: обновление выполняется"
    assert "«Выбрать все»: обновление выполняется" in shown["busy seed"]
    assert shown["idle drift"] == ""


def test_esc_the_menu_and_the_home_crumb_wait_while_the_update_runs(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[list[str], list[object]]:
        closed: list[object] = []
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = _screen(tmp_path, _Runner())
            await app.push_screen(screen, closed.append)
            await _ready(pilot, app)
            notes: list[str] = []
            screen.notify = lambda message, **_: notes.append(message)  # type: ignore[method-assign]
            screen.busy = True
            for key in ("escape", "1", "9"):
                await pilot.press(key)
                await pilot.pause()
                assert app.screen is screen
            await screen.navigate_crumb(screen.crumbs()[0])
            assert app.screen is screen and not closed
            screen.busy = False
            await pilot.press("escape")
            await wait_until(pilot, lambda: app.screen is not screen)
            return notes, closed

    notes, closed = asyncio.run(scenario())
    assert notes == ["Дождитесь окончания обновления"] * 4
    assert closed == [None]


def test_the_home_key_and_the_home_crumb_close_the_screen_through_its_callback(
    tmp_path: Path,
) -> None:
    """Leaving to Home any way runs the opener's callback (it refreshes Home), as Esc does."""
    from harness.console.screens.dashboard import DashboardScreen

    async def scenario() -> list[list[object]]:
        results: list[list[object]] = []
        for leave in ("key", "crumb"):
            closed: list[object] = []
            app = HarnessConsoleApp(tmp_path)
            async with app.run_test(size=(120, 40)) as pilot:
                screen = _screen(tmp_path, _Runner())
                await app.push_screen(screen, closed.append)
                await _ready(pilot, app)
                if leave == "key":
                    await pilot.press("0")
                else:
                    await screen.navigate_crumb(screen.crumbs()[0])
                await wait_until(pilot, lambda: isinstance(app.screen, DashboardScreen))
                await pilot.pause()
                results.append(closed)
        return results

    assert asyncio.run(scenario()) == [[None], [None]]


def test_drift_and_not_installed_states_are_a_symbol_and_a_text(tmp_path: Path) -> None:
    async def scenario() -> dict[str, str]:
        seen: dict[str, str] = {}
        app = _console(_screen(tmp_path, _Runner()), tmp_path)
        async with app.run_test(size=(160, 52)) as pilot:
            screen = await _ready(pilot, app, loaded=False)
            screen._payload_ready = False
            screen._render_state()
            seen["loading"] = _plain(screen.query_one("#update-drift"))
        for name, payload in (("failed", None), ("clean", {"state": "clean"})):
            app = _console(_screen_with(tmp_path, payload), tmp_path)
            async with app.run_test(size=(160, 52)) as pilot:
                inner = await _ready(pilot, app)
                seen[name] = _plain(inner.query_one("#update-drift"))
        app = _console(_screen(tmp_path, _Runner(), installed=False), tmp_path)
        async with app.run_test(size=(160, 52)) as pilot:
            inner = await _ready(pilot, app, loaded=False)
            seen["not installed"] = _plain(inner.query_one("#update-versions"))
        return seen

    seen = asyncio.run(scenario())
    assert seen["loading"].splitlines() == [
        "○ проверяется…",
        "Счётчики и список файлов появятся после проверки.",
    ]
    assert seen["failed"].splitlines() == [
        "✖ Не удалось проверить дрейф",
        "Нажмите «Проверить заново». Если повторяется — «Диагностика» (3).",
    ]
    assert seen["clean"].startswith("✔ Расхождений нет")
    assert seen["not installed"].startswith(
        "✖ Харнесс не установлен: нет читаемого .harness/harness.lock"
    )


class _Gate:
    """A runner that waits for the test, so the screen can be looked at while the update runs."""

    def __init__(self, code: int) -> None:
        self.code = code
        self.release = threading.Event()

    def __call__(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> "subprocess.CompletedProcess[str]":
        self.release.wait(30)
        return subprocess.CompletedProcess(list(argv), self.code, "", "")


@pytest.mark.parametrize(
    ("code", "summary"), [(0, "✔ Harness обновлён"), (1, "✖ ошибка")]
)
def test_the_result_block_names_the_run_state_with_a_symbol(
    tmp_path: Path, code: int, summary: str
) -> None:
    gate = _Gate(code)

    async def scenario() -> tuple[str, str, str]:
        def result() -> str:
            return Text.from_markup(screen.query_one("#output-frame", Fold).title).plain

        screen = _screen(tmp_path, gate)  # type: ignore[arg-type]
        app = _console(screen, tmp_path)
        async with app.run_test(size=(160, 52)) as pilot:
            await _ready(pilot, app)
            before = result()
            screen._run(screen._entry())
            await pilot.pause()
            running = _plain(screen.query_one("#update-output")).splitlines()[-1]
            during = result()
            gate.release.set()
            await app.workers.wait_for_complete()
            await wait_until(pilot, lambda: not screen.busy)
            return before + "|" + during, running, result()

    states, running, after = asyncio.run(scenario())
    assert "○ пока пусто" in states and "▶ выполняется" in states
    assert running == "▶ Выполняется…"
    assert summary in after


def _screen_with(
    tmp_path: Path, payload: Mapping[str, object] | None
) -> UpdateHarnessScreen:
    return UpdateHarnessScreen(
        tmp_path,
        command_runner=_Runner(),
        load_state=lambda _repo: _state(),
        load_drift=lambda _repo: None if payload is None else dict(payload),
        load_diff=lambda _repo, relative: "",
    )


def test_focus_starts_on_the_update_button_and_tab_visits_the_visible_controls_after_it(
    tmp_path: Path,
) -> None:
    async def scenario(size: tuple[int, int]) -> tuple[list[str], list[str], bool]:
        app = _console(_screen(tmp_path, _Runner()), tmp_path)
        async with app.run_test(size=size) as pilot:
            screen = await _ready(pilot, app)
            await wait_until(pilot, lambda: screen.focused is not None)
            page = screen.query_one("#update-page")
            controls = [w for w in screen.focus_chain if page in w.ancestors]
            first = screen.focused
            assert first is not None and first.id == "update-run"
            after = controls[controls.index(first) + 1 :]
            seen = []
            for _ in after:
                await pilot.press("tab")
                await pilot.pause()
                assert screen.focused is not None
                assert all(node.display for node in screen.focused.ancestors_with_self)
                seen.append(screen.focused)
            assert seen == after  # the page's own controls come first, in order
            await pilot.press("tab")
            await pilot.pause()
            left = page not in (screen.focused.ancestors if screen.focused else ())
            return (
                [str(w.id or type(w).__name__) for w in controls],
                [str(w.id or type(w).__name__) for w in seen],
                left,
            )

    narrow, seen, left = asyncio.run(scenario((80, 24)))
    assert "update-recheck-narrow" in narrow and "update-recheck" not in narrow
    assert "update-mode" not in narrow  # folded away at 80x24
    assert seen[0] == "update-recheck-narrow" and left
    wide, seen, left = asyncio.run(scenario((160, 52)))
    assert "update-recheck" in wide and "update-recheck-narrow" not in wide
    assert "update-mode" in wide and left


def test_long_values_wrap_or_elide_and_never_widen_the_page(tmp_path: Path) -> None:
    repo = tmp_path / ("very-long-project-name-" * 5).rstrip("-")
    repo.mkdir()
    long_seed = "docs/" + "a-very-long-seed-file-name-" * 6 + "end.md"
    long_diff = ".harness/" + "deeply/nested/" * 8 + "file-with-a-long-name.md"
    payload = {
        **PAYLOAD,
        "seed_differing": [long_seed, "AGENTS.md"],
        "conflict": [long_diff],
    }

    async def scenario() -> list[str]:
        from tests.console.test_console_layout import layout_problems

        app = _console(_screen(repo, _Runner(), payload=payload), repo)
        async with app.run_test(size=(80, 24)) as pilot:
            screen = await _ready(pilot, app)
            assert screen.query_one("#update-project", Static).region.width <= 80
            screen.query_one("#update-mode").query(RadioButton)[2].value = True
            await pilot.pause()
            for name in ("mode-frame", "diff-frame", "output-frame"):
                screen.query_one(f"#{name}", Fold).collapsed = False
            await pilot.pause()
            await pilot.pause()
            # The diff text itself scrolls sideways (a diff line is never wrapped); the page does not.
            page = screen.query_one("#update-page")
            assert page.virtual_size.width <= page.size.width
            return [
                problem
                for problem in layout_problems(cast_screen(screen))
                if not problem.startswith("Static#diff-view ")
            ]

    def cast_screen(screen: UpdateHarnessScreen) -> Screen[None]:
        return screen

    assert asyncio.run(scenario()) == []


def test_update_harness_follows_the_language_on_the_shared_frame(
    tmp_path: Path,
) -> None:
    async def scenario() -> dict[str, str]:
        app = _console(_screen(tmp_path, _Runner()), tmp_path)
        async with app.run_test(size=(160, 52)) as pilot:
            screen = await _ready(pilot, app)
            app.action_toggle_language()
            await wait_until(
                pilot,
                lambda: str(screen.query_one("#update-run", Button).label) == "Update",
            )
            await pilot.pause()
            footer = {
                key: str(active.binding.description)
                for key, active in screen.active_bindings.items()
                if active.binding.show
            }
            return {
                "title": _plain(screen.query_one("#update-title")),
                "versions": _card_title(screen.query_one("#versions-frame")),
                "warning": _plain(screen.query_one("#update-warning")),
                "caption": _plain(screen.query_one("#mode-caption")),
                "tag": _plain(screen.query_one("#diff-files ListItem Static")),
                "drift": _plain(screen.query_one("#update-drift")).splitlines()[0],
                "result": Text.from_markup(
                    screen.query_one("#output-frame", Fold).title
                ).plain,
                "back": footer["escape"],
                "recheck": str(screen.query_one("#update-recheck", Button).label),
            }

    shown = asyncio.run(scenario())
    assert shown["title"] == "Update Harness"
    assert shown["versions"].startswith("VERSIONS")
    assert shown["warning"].splitlines()[0] == "⚠ A normal update will stop: 1 conflict"
    assert shown["caption"] == "Local changes are kept. It stops on conflicts."
    assert shown["tag"].startswith("✖ conflict")
    assert shown["drift"] == "   2 ✔ will be updated"
    assert "○ empty so far" in shown["result"]
    assert shown["back"] == "Back" and shown["recheck"] == "Check again"
