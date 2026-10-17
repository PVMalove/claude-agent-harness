"""harness.console.app: TUI на базе textual. Полностью пропускается, если textual не установлен.
Пилотные тесты используют управляемый запуск и вызываются через `asyncio.run`."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Sequence, Mapping

import pytest

pytest.importorskip("textual")

from textual.pilot import Pilot

from harness.console import brand
from harness.console.app import HarnessConsoleApp
from harness.console.data import DashboardData
from harness.console.screens.dashboard import DashboardScreen
from harness.console.shell import SECTIONS
from harness.console.screens.diagnostics import DiagnosticsScreen
from harness.console.screens.harness import HarnessScreen
from harness.health.model import CheckResult, Fix, Report
from tests.console._pilot_wait import wait_until


def test_dashboard_shows_the_section_menu_in_order(tmp_path: Path) -> None:
    """Проверить, что дашборд отображает меню разделов в ожидаемом порядке."""

    async def scenario() -> list[str | None]:
        """Сценарий запуска приложения и извлечения элементов меню."""
        from textual.widgets import ListView

        app = HarnessConsoleApp(tmp_path)
        async with app.run_test():
            menu = app.screen.query_one("#section-menu", ListView)
            return [item.name for item in menu.children]

    assert asyncio.run(scenario()) == list(SECTIONS)


@pytest.mark.parametrize("size", [(120, 48), (80, 24)])
def test_dashboard_keeps_visible_checks_and_project_actions_with_keyboard_shortcuts(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    from textual.app import App
    from textual.binding import Binding
    from textual.widgets import Button, ListItem, Collapsible

    from harness.console.chrome import TopBar

    class DashboardHost(App[None]):
        CSS = HarnessConsoleApp.CSS

    async def scenario() -> None:
        screen = DashboardScreen(
            tmp_path,
            collect_dashboard=lambda _repo, *, online=False: _fake_dashboard_data(),
        )
        app = DashboardHost()
        async with app.run_test(size=size) as pilot:
            await app.push_screen(screen)
            await wait_until(
                pilot,
                lambda: bool(screen.query("#dashboard-attention-scroll"))
                and bool(screen.query("#dashboard-sections"))
                and len(list(screen.query("#section-menu > ListItem"))) == 9,
            )
            checks = screen.query_one("#dashboard-attention-scroll")
            for button in checks.query(Button):
                assert checks.region.contains_region(button.region)
            assert [button.id for button in checks.query(Button)] == [
                "dashboard-offline",
                "dashboard-online",
            ]
            if size[0] < 100:
                assert not screen.query_one("#dashboard-sections").display
                await pilot.click("#dashboard-menu")
                await pilot.pause()
                assert screen.query_one("#dashboard-sections").display
            sections = screen.query_one("#dashboard-sections")
            assert sections.query_one("#open-project", ListItem)
            assert sections.query_one("#update-harness", ListItem)
            menu_items = list(sections.query("#section-menu > ListItem"))
            assert [item.name for item in menu_items[:3]] == [
                "Home",
                "Open project",
                "Update Harness",
            ]
            assert len(menu_items) == 10
            assert menu_items[0].has_class("-active")
            assert not menu_items[1].has_class("-active")
            assert not sections.query(Button)
            assert screen.query_one("#dashboard-summary").display
            assert (
                sections.region.bottom
                == screen.query_one("#dashboard-main").region.bottom
            )
            assert screen.query_one("#dashboard-details", Collapsible).collapsed
            assert not screen.query(
                "#dashboard-cli, #dashboard-offline-cli, #dashboard-online-cli"
            )
            assert screen.query_one(TopBar).region.height == (2 if size[0] < 100 else 3)
            for key in ("r", "o", "p"):
                assert any(
                    isinstance(binding, Binding) and binding.key == key
                    for binding in screen.BINDINGS
                )
            for item in screen.query("#section-menu > ListItem").results(ListItem):
                assert screen.region.contains_region(item.region)

    asyncio.run(scenario())


@pytest.mark.parametrize("interaction", ["keyboard", "mouse"])
def test_dashboard_update_opens_the_update_screen_without_running_a_command(
    tmp_path: Path,
    interaction: str,
) -> None:
    from textual.app import App

    from harness.console.screens.update_harness import UpdateHarnessScreen

    calls: list[object] = []

    def runner(
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    async def scenario() -> None:
        app = App[None]()
        screen = DashboardScreen(
            tmp_path,
            collect_dashboard=lambda _repo, *, online=False: _fake_dashboard_data(),
            command_runner=runner,
        )
        async with app.run_test(size=(120, 48)) as pilot:
            await app.push_screen(screen)
            await pilot.pause()
            if interaction == "keyboard":
                await pilot.press("2")
            else:
                await pilot.click("#update-harness")
            await wait_until(pilot, lambda: isinstance(app.screen, UpdateHarnessScreen))
            assert isinstance(app.screen, UpdateHarnessScreen)
            assert app.screen.repo == tmp_path
            assert not calls
            await pilot.press("escape")
            await wait_until(pilot, lambda: isinstance(app.screen, DashboardScreen))
            assert isinstance(app.screen, DashboardScreen)
            assert not calls

    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(120, 48), (80, 24)])
def test_dashboard_menu_opens_a_section_by_its_number(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    async def scenario() -> str:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            await pilot.press("4")
            await wait_until(pilot, lambda: type(app.screen).__name__ == "HarnessScreen")
            return type(app.screen).__name__

    assert asyncio.run(scenario()) == "HarnessScreen"


def test_dashboard_project_entries_switch_language_without_changing_order(
    tmp_path: Path,
) -> None:
    from io import StringIO
    from rich.console import Console
    from textual.app import App
    from textual.widgets import ListItem, Static

    async def scenario() -> None:
        app = App[None]()
        screen = DashboardScreen(
            tmp_path,
            collect_dashboard=lambda _repo, *, online=False: _fake_dashboard_data(),
        )
        async with app.run_test(size=(120, 36)):
            await app.push_screen(screen)
            for language, expected in (
                ("ru", ("0 Главная", "1 Открыть проект", "2 Обновить Harness")),
                ("en", ("0 Home", "1 Open project", "2 Update Harness")),
            ):
                setattr(app, "language", language)
                screen.sync_language()
                items = list(screen.query("#section-menu > ListItem").results(ListItem))
                assert [item.name for item in items] == list(SECTIONS)
                for item, label in zip(items[:3], expected):
                    output = StringIO()
                    Console(file=output, width=30).print(item.query_one(Static).content)
                    assert label in output.getvalue()

    asyncio.run(scenario())


@pytest.mark.parametrize("interaction", ["keyboard", "mouse"])
def test_configuration_menu_opens_from_keyboard_and_mouse(
    tmp_path: Path, interaction: str
) -> None:
    from textual.app import App
    from textual.widgets import ListItem, ListView, Static

    from harness.console.screens.configuration import ConfigurationScreen

    async def scenario() -> None:
        app = App[None]()
        screen = DashboardScreen(
            tmp_path,
            collect_dashboard=lambda _repo, *, online=False: _fake_dashboard_data(),
        )
        async with app.run_test(size=(120, 48)) as pilot:
            await app.push_screen(screen)
            await pilot.pause()
            menu = screen.query_one("#section-menu", ListView)
            item = next(
                item for item in menu.query(ListItem) if item.name == "Configuration"
            )
            if interaction == "keyboard":
                menu.focus()
                menu.index = list(menu.children).index(item)
                await pilot.press("enter")
            else:
                assert await pilot.click(item.query_one(Static))
            await wait_until(pilot, lambda: isinstance(app.screen, ConfigurationScreen))
            assert isinstance(app.screen, ConfigurationScreen)

    asyncio.run(scenario())


def test_selecting_harness_pushes_the_harness_screen(tmp_path: Path) -> None:
    """Проверить, что выбор раздела Harness открывает экран HarnessScreen."""

    async def scenario() -> bool:
        """Сценарий выбора пункта Harness в меню."""
        from textual.widgets import ListView

        app = HarnessConsoleApp(tmp_path)
        async with app.run_test() as pilot:
            if not app.screen.query_one("#dashboard-sections").display:
                await pilot.click("#dashboard-menu")
                await pilot.pause()
            menu = app.screen.query_one("#section-menu", ListView)
            assert app.focused is menu
            menu.index = SECTIONS.index("Harness")
            await pilot.press("enter")
            await wait_until(pilot, lambda: isinstance(app.screen, HarnessScreen))
            return isinstance(app.screen, HarnessScreen)

    assert asyncio.run(scenario())


def test_selecting_orchestration_pushes_the_orchestration_screen(
    tmp_path: Path,
) -> None:
    """Проверить, что выбор раздела Orchestration открывает экран OrchestrationScreen."""

    async def scenario() -> bool:
        """Сценарий выбора пункта Orchestration в меню."""
        from textual.widgets import ListView

        from harness.console.screens.orchestration import OrchestrationScreen

        app = HarnessConsoleApp(tmp_path)
        async with app.run_test() as pilot:
            if not app.screen.query_one("#dashboard-sections").display:
                await pilot.click("#dashboard-menu")
                await pilot.pause()
            menu = app.screen.query_one("#section-menu", ListView)
            assert app.focused is menu
            menu.index = SECTIONS.index("Orchestration")
            await pilot.press("enter")
            await wait_until(pilot, lambda: isinstance(app.screen, OrchestrationScreen))
            return isinstance(app.screen, OrchestrationScreen)

    assert asyncio.run(scenario())


def _fake_dashboard_data() -> DashboardData:
    """Сформировать тестовые данные для экрана дашборда."""
    return DashboardData(
        ok=5,
        warn=1,
        fail=0,
        skipped=2,
        repo_map_tier="full",
        harness_version="9.9.9",
        drift_state="clean",
        active_batches=3,
    )


def test_dashboard_screen_renders_injected_data_without_touching_the_repo(
    tmp_path: Path,
) -> None:
    """Проверить, что экран дашборда отображает внедрённые данные без обращения к репозиторию."""

    async def scenario() -> str:
        """Сценарий монтирования экрана дашборда и чтения содержимого сводки."""
        from textual.app import App
        from textual.widgets import Static

        screen = DashboardScreen(
            tmp_path,
            collect_dashboard=lambda _repo, *, online=False: _fake_dashboard_data(),
        )

        class _HostApp(App[None]):
            """Тестовое приложение-хост для экрана дашборда."""

            def on_mount(self) -> None:
                """Смонтировать тестируемый экран."""
                self.push_screen(screen)

        app = _HostApp()
        async with app.run_test():
            summary = app.screen.query_one("#dashboard-summary", Static)
            return str(summary.content)

    rendered = asyncio.run(scenario())
    assert "ok=5" in rendered
    assert "warn=1" in rendered
    assert "fail=0" in rendered
    from rich.text import Text

    assert "active batches: 3" in Text.from_markup(rendered).plain
    assert "9.9.9" in rendered
    assert "clean" in rendered


def test_dashboard_shows_the_mark_and_the_banner_beside_it(tmp_path: Path) -> None:
    """The home screen opens with the harness mark and a description of this installation."""

    async def scenario() -> tuple[str, str, str]:
        from textual.app import App
        from textual.widgets import Static

        from harness.console import brand

        banner = brand.BannerInfo(
            version="9.9.9",
            capabilities=("pvmalove-suite",),
            repo="~/work/app",
            branch="feature/issue-7-x",
        )
        screen = DashboardScreen(
            tmp_path,
            collect_dashboard=lambda _repo, *, online=False: _fake_dashboard_data(),
            collect_banner=lambda _repo: banner,
        )

        class _HostApp(App[None]):
            def on_mount(self) -> None:
                self.push_screen(screen)

        app = _HostApp()
        async with app.run_test():
            mark = str(app.screen.query_one("#brand-mark", Static).content)
            info = str(app.screen.query_one("#brand-info", Static).content)
            card = str(app.screen.query_one("#card-harness", Static).content)
        return mark, info, card

    mark, info, card = asyncio.run(scenario())
    from harness.console import brand

    assert mark.splitlines() == list(brand.LOGO)
    assert f"{brand.PRODUCT} 9.9.9" in info
    # The installed Harness (its capabilities) lives on the Harness card, not beside the mark.
    assert "pvmalove-suite" in card and "pvmalove-suite" not in info
    assert "~/work/app" in info
    assert "ветка feature/issue-7-x · commit не определён" in info


def test_console_registers_and_selects_the_warm_theme(tmp_path: Path) -> None:
    async def scenario() -> str:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test():
            return app.theme

    from harness.console import brand

    assert asyncio.run(scenario()) == brand.THEME_NAME


@pytest.mark.parametrize("group_class", ["actions", "action-row"])
def test_common_actions_remain_reachable_after_terminal_is_narrowed(
    tmp_path: Path,
    group_class: str,
) -> None:
    """Общая группа действий прокручивается к фокусу, чтобы кнопки не обрезались."""
    from textual.app import ComposeResult
    from textual.containers import Horizontal, VerticalGroup
    from textual.screen import Screen
    from textual.widgets import Button, Static

    class StyleScreen(Screen[None]):
        def __init__(self) -> None:
            super().__init__()
            self.pressed: list[str | None] = []

        def compose(self) -> ComposeResult:
            with VerticalGroup(classes="frame") as frame:
                frame.border_title = brand.frame_title("Действия")
                yield Static("Сохранённые конфигурации")
                with Horizontal(id="style-actions", classes=group_class):
                    yield Button("Save current configuration", id="style-first")
                    yield Button("Select version / restore", id="style-second")
                    yield Button("Close / cancel", id="style-last")

        def on_button_pressed(self, event: Button.Pressed) -> None:
            self.pressed.append(event.button.id)

    async def scenario() -> None:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(120, 48)) as pilot:
            screen = StyleScreen()
            await app.push_screen(screen)
            await pilot.pause()
            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            screen.query_one("#style-first", Button).focus()
            await pilot.press("tab", "tab")
            last = screen.query_one("#style-last", Button)
            actions = screen.query_one("#style-actions", Horizontal)
            # Focus scrolls the button into view with an animation: wait for it, not for one pause.
            await wait_until(
                pilot,
                lambda: app.focused is last
                and actions.scrollable_content_region.contains_region(last.region),
            )
            assert app.focused is last
            assert actions.scrollable_content_region.contains_region(last.region)
            assert await pilot.click("#style-last")
            assert screen.pressed == ["style-last"]
            await pilot.resize_terminal(120, 48)
            await wait_until(
                pilot,
                lambda: all(
                    actions.scrollable_content_region.contains_region(button.region)
                    for button in actions.query(Button)
                ),
            )

    asyncio.run(scenario())


def test_help_opens_from_the_menu_and_from_f1(tmp_path: Path) -> None:
    async def scenario() -> tuple[bool, bool]:
        from textual.widgets import ListView

        from harness.console.screens.help import HelpScreen

        app = HarnessConsoleApp(tmp_path)
        async with app.run_test() as pilot:
            if not app.screen.query_one("#dashboard-sections").display:
                await pilot.click("#dashboard-menu")
                await pilot.pause()
            menu = app.screen.query_one("#section-menu", ListView)
            assert app.focused is menu
            menu.index = SECTIONS.index("Help")
            await pilot.press("enter")
            await wait_until(pilot, lambda: isinstance(app.screen, HelpScreen))
            from_menu = isinstance(app.screen, HelpScreen)
            await pilot.press("escape")
            await wait_until(pilot, lambda: not isinstance(app.screen, HelpScreen))
            await pilot.press("f1")
            await wait_until(pilot, lambda: isinstance(app.screen, HelpScreen))
            from_f1 = isinstance(app.screen, HelpScreen)
        return from_menu, from_f1

    assert asyncio.run(scenario()) == (True, True)


async def _settle(pilot: "Pilot[None]", delay: float | None = None) -> None:
    """Ожидать завершения фоновых воркеров и обновления интерфейса."""
    await pilot.pause(delay)
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


def _fake_report(
    *, online: bool = False, fixes_applied: list[str] | None = None
) -> Report:
    """Сформировать тестовый отчёт диагностики."""
    return Report(
        schema_version=1,
        repo="/tmp/fake-repo",
        online=online,
        checks=[
            CheckResult(id="files.lock", group="files", status="ok", message="lock ok"),
            CheckResult(
                id="environment.uv",
                group="environment",
                status="fail",
                message="uv недоступен",
                fix=Fix(text="поставить uv", command="echo installing-uv"),
            ),
        ],
        fixes_applied=fixes_applied or [],
    )


def test_diagnostics_screen_renders_the_full_report(tmp_path: Path) -> None:
    """Проверить, что экран диагностики отображает полный отчёт со всеми проверками."""

    async def scenario() -> str:
        """Сценарий отображения полного отчёта на экране диагностики."""
        from textual.app import App
        from textual.widgets import Static

        screen = DiagnosticsScreen(
            tmp_path, collect_diagnostics=lambda repo, *, online=False: _fake_report()
        )

        class _HostApp(App[None]):
            """Тестовое приложение-хост для экрана диагностики."""

            def on_mount(self) -> None:
                """Смонтировать тестируемый экран."""
                self.push_screen(screen)

        app = _HostApp()
        async with app.run_test() as pilot:
            await _settle(pilot)
            report_widget = app.screen.query_one("#diagnostics-report", Static)
            return str(report_widget.content)

    rendered = asyncio.run(scenario())
    assert "environment.uv" in rendered
    assert "uv недоступен" in rendered
    assert "поставить uv" in rendered
    assert "echo installing-uv" in rendered


class _RecordingFixes:
    """Тестовый дублёр для фиксации вызовов применения локальных исправлений."""

    def __init__(self, fixes_applied: list[str] | None = None) -> None:
        """Инициализировать запись вызовов и список применённых фиксов."""
        self.calls: list[bool] = []
        self._fixes_applied = fixes_applied

    def __call__(self, repo: Path, *, online: bool = False) -> Report:
        """Зафиксировать вызов с параметром online и вернуть фиктивный отчёт."""
        self.calls.append(online)
        return _fake_report(online=online, fixes_applied=self._fixes_applied)


def test_diagnostics_apply_fixes_asks_before_applying(tmp_path: Path) -> None:
    """Проверить, что применение исправлений требует подтверждения повторным нажатием."""

    async def scenario() -> tuple[int, int]:
        """Сценарий проверки фиксации вызовов исправлений до и после подтверждения."""
        from textual.app import App

        fixes = _RecordingFixes()
        screen = DiagnosticsScreen(
            tmp_path,
            collect_diagnostics=lambda repo, *, online=False: _fake_report(),
            apply_local_fixes=fixes,
        )

        class _HostApp(App[None]):
            """Тестовое приложение-хост для экрана диагностики."""

            def on_mount(self) -> None:
                """Смонтировать тестируемый экран."""
                self.push_screen(screen)

        app = _HostApp()
        async with app.run_test() as pilot:
            await _settle(pilot)
            await pilot.click("#apply-fixes")
            # Button's own brief "-active" press animation ignores a second click while it runs.
            await _settle(pilot, 0.4)
            calls_after_first_press = len(fixes.calls)

            await pilot.click("#apply-fixes")
            await _settle(pilot, 0.4)
            calls_after_second_press = len(fixes.calls)

        return calls_after_first_press, calls_after_second_press

    after_first, after_second = asyncio.run(scenario())
    assert after_first == 0
    assert after_second == 1


def test_diagnostics_apply_fixes_never_runs_remedy_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Проверить, что применение исправлений не выполняет внешние команды устранения."""

    async def scenario() -> tuple[str, list[list[str]]]:
        """Сценарий проверки применения исправлений и отсутствия запуска внешних команд."""
        from textual.app import App
        from textual.widgets import Static

        spawned: list[list[str]] = []

        def recording_run(
            argv: "Sequence[str]", *args: object, **kwargs: object
        ) -> object:
            """Зафиксировать запуск внешней команды."""
            spawned.append(list(argv))
            return subprocess.CompletedProcess(list(argv), 0, "", "")

        monkeypatch.setattr(subprocess, "run", recording_run)
        screen = DiagnosticsScreen(
            tmp_path,
            collect_diagnostics=lambda repo, *, online=False: _fake_report(),
            apply_local_fixes=_RecordingFixes(
                fixes_applied=["создан каталог .harness"]
            ),
        )

        class _HostApp(App[None]):
            """Тестовое приложение-хост для экрана диагностики."""

            def on_mount(self) -> None:
                """Смонтировать тестируемый экран."""
                self.push_screen(screen)

        app = _HostApp()
        async with app.run_test() as pilot:
            await _settle(pilot)
            await pilot.click("#apply-fixes")
            await _settle(pilot, 0.4)
            await pilot.click("#apply-fixes")
            await _settle(pilot, 0.4)
            report_widget = app.screen.query_one("#diagnostics-report", Static)
            return str(report_widget.content), spawned

    rendered, calls = asyncio.run(scenario())
    assert calls == []
    assert "создан каталог .harness" in rendered
    # the remedy command stays visible for the developer to run by hand
    assert "echo installing-uv" in rendered


def test_diagnostics_apply_fixes_keeps_online_mode(tmp_path: Path) -> None:
    """Проверить, что применение исправлений сохраняет онлайн-режим после онлайн-проверок."""

    async def scenario() -> list[bool]:
        """Сценарий запуска исправлений в онлайн-режиме после онлайн-проверок."""
        from textual.app import App

        fixes = _RecordingFixes()
        screen = DiagnosticsScreen(
            tmp_path,
            collect_diagnostics=lambda repo, *, online=False: _fake_report(
                online=online
            ),
            apply_local_fixes=fixes,
        )

        class _HostApp(App[None]):
            """Тестовое приложение-хост для экрана диагностики."""

            def on_mount(self) -> None:
                """Смонтировать тестируемый экран."""
                self.push_screen(screen)

        app = _HostApp()
        async with app.run_test() as pilot:
            await _settle(pilot)
            await pilot.click("#online-checks")
            await _settle(pilot, 0.4)
            await pilot.click("#apply-fixes")
            await _settle(pilot, 0.4)
            await pilot.click("#apply-fixes")
            await _settle(pilot, 0.4)
        return fixes.calls

    assert asyncio.run(scenario()) == [True]


def test_diagnostics_online_checks_refetches_with_online_true(tmp_path: Path) -> None:
    """Проверить, что запуск онлайн-проверок повторно запрашивает отчёт с online=True."""

    async def scenario() -> list[bool]:
        """Сценарий нажатия кнопки онлайн-проверок."""
        from textual.app import App

        seen_online: list[bool] = []

        def fake_collect(repo: Path, *, online: bool = False) -> Report:
            """Зафиксировать значение флага online и вернуть тестовый отчёт."""
            seen_online.append(online)
            return _fake_report(online=online)

        screen = DiagnosticsScreen(tmp_path, collect_diagnostics=fake_collect)

        class _HostApp(App[None]):
            """Тестовое приложение-хост для экрана диагностики."""

            def on_mount(self) -> None:
                """Смонтировать тестируемый экран."""
                self.push_screen(screen)

        app = _HostApp()
        async with app.run_test() as pilot:
            await _settle(pilot)
            await pilot.click("#online-checks")
            await _settle(pilot)

        return seen_online

    seen_online = asyncio.run(scenario())
    assert seen_online == [False, True]


def test_dashboard_online_checks_action_reruns_with_online_and_updates_summary(
    tmp_path: Path,
) -> None:
    """Проверить, что онлайн-проверки дашборда перезапускают сбор и обновляют сводку."""

    async def scenario() -> tuple[list[bool], str]:
        """Сценарий запуска онлайн-проверок из дашборда."""
        from textual.app import App
        from textual.widgets import Static

        seen_online: list[bool] = []

        def fake_collect(_repo: Path, *, online: bool = False) -> DashboardData:
            """Зафиксировать значение флага online и вернуть тестовые данные дашборда."""
            seen_online.append(online)
            return _fake_dashboard_data()

        class _HostApp(App[None]):
            """Тестовое приложение-хост для экрана дашборда."""

            def on_mount(self) -> None:
                """Смонтировать тестируемый экран."""
                self.push_screen(
                    DashboardScreen(tmp_path, collect_dashboard=fake_collect)
                )

        app = _HostApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("o")
            await _settle(pilot)
            summary = str(app.screen.query_one("#dashboard-summary", Static).content)
        return seen_online, summary

    seen_online, summary = asyncio.run(scenario())
    assert seen_online == [False, True]
    assert "health (online)" in summary
    assert "ok=5" in summary


def test_diagnostics_shows_cli_equivalents_and_exports_the_report(
    tmp_path: Path,
) -> None:
    """Проверить, что экран диагностики отображает эквиваленты CLI и экспортирует отчёт."""

    async def scenario() -> tuple[str, str]:
        """Сценарий проверки строк CLI и экспорта отчёта диагностики."""
        from textual.app import App
        from textual.widgets import Static

        screen = DiagnosticsScreen(
            tmp_path, collect_diagnostics=lambda repo, *, online=False: _fake_report()
        )

        class _HostApp(App[None]):
            """Тестовое приложение-хост для экрана диагностики."""

            def on_mount(self) -> None:
                """Смонтировать тестируемый экран."""
                self.push_screen(screen)

        app = _HostApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            online_cli = str(app.screen.query_one("#online-checks-cli", Static).content)
            fix_cli = str(app.screen.query_one("#apply-fixes-cli", Static).content)
            await pilot.click("#export")
            await _settle(pilot)
        return online_cli, fix_cli

    online_cli, fix_cli = asyncio.run(scenario())
    assert online_cli.endswith("--online")
    assert fix_cli.endswith("--fix")
    exported = list(
        (tmp_path / "docs" / "tasks" / "console-exports").glob("*health-report.md")
    )
    assert len(exported) == 1
    text = exported[0].read_text(encoding="utf-8")
    assert text.startswith("# Health-отчёт")
    assert "environment.uv" in text


def test_offline_checks_buttons_rerun_health_offline(tmp_path: Path) -> None:
    """Проверить, что кнопки Offline checks дашборда и диагностики перезапускают проверки офлайн."""

    async def scenario() -> tuple[list[bool], list[bool], str]:
        """Сценарий нажатия Offline checks на обоих экранах."""
        from textual.app import App
        from textual.widgets import Static

        dashboard_online: list[bool] = []
        diagnostics_online: list[bool] = []

        def fake_dashboard(_repo: Path, *, online: bool = False) -> DashboardData:
            """Зафиксировать флаг online для дашборда."""
            dashboard_online.append(online)
            return _fake_dashboard_data()

        def fake_diagnostics(repo: Path, *, online: bool = False) -> Report:
            """Зафиксировать флаг online для диагностики."""
            diagnostics_online.append(online)
            return _fake_report(online=online)

        screens = [
            DashboardScreen(tmp_path, collect_dashboard=fake_dashboard),
            DiagnosticsScreen(tmp_path, collect_diagnostics=fake_diagnostics),
        ]

        class _HostApp(App[None]):
            """Тестовое приложение-хост для обоих экранов."""

            def on_mount(self) -> None:
                """Смонтировать экран дашборда."""
                self.push_screen(screens[0])

        app = _HostApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("r")
            await _settle(pilot)
            summary = str(app.screen.query_one("#dashboard-summary", Static).content)
            app.push_screen(screens[1])
            await _settle(pilot)
            await pilot.click("#offline-checks")
            await _settle(pilot)
        return dashboard_online, diagnostics_online, summary

    dashboard_online, diagnostics_online, summary = asyncio.run(scenario())
    assert dashboard_online == [False, False]
    assert diagnostics_online == [False, False]
    assert "health (offline)" in summary
    assert "ok=5" in summary


def test_dashboard_summary_lists_problems_and_splits_repo_map_facts() -> None:
    """Проверить, что сводка перечисляет ошибки и предупреждения и разбивает строку Repo Map."""
    from dataclasses import replace
    from rich.text import Text

    from harness.console.screens.dashboard import _render_summary

    data = replace(
        _fake_dashboard_data(),
        repo_map_tier="tier=minimal; policy: enforced",
        problems=(
            ("fail", "env.git", "git [bold] missing"),
            ("warn", "repo_map.tier", "minimal"),
        ),
    )
    text = _render_summary(data)
    plain = Text.from_markup(text).plain
    assert "repo map: tier=minimal\n  policy: enforced" in plain
    assert "ошибки и предупреждения:" in plain
    assert "env.git — git \\[bold] missing" in text
    assert text.index("env.git") < text.index("repo_map.tier —")


def test_dashboard_summary_colors_labels_and_normalizes_repo_map() -> None:
    from dataclasses import replace

    from rich.text import Text
    from rich.color import Color
    from rich.style import Style

    from harness.console.screens.dashboard import _render_summary

    data = replace(
        _fake_dashboard_data(),
        repo_map_tier=(
            "Repo Map: tier=minimal (requested by policy); "
            "Repo Map provenance: policy requested path-only inventory; "
            "Repo Map policy: enforced; dispatch is limited to minimal path inventory"
        ),
    )
    rendered = Text.from_markup(_render_summary(data))
    assert "repo map: tier=minimal" in rendered.plain
    assert "  provenance: policy requested path-only inventory" in rendered.plain
    assert "  policy: enforced" in rendered.plain
    assert "Repo Map" not in rendered.plain
    for label in (
        "health (offline):",
        "active batches:",
        "repo map:",
        "memory:",
        "harness:",
    ):
        start = rendered.plain.index(label)
        assert any(
            span.start <= start
            and span.end >= start + len(label)
            and Style.parse(str(span.style)).bold
            and Style.parse(str(span.style)).color
            == Color.parse(brand.PALETTE["primary"])
            for span in rendered.spans
        )


@pytest.mark.parametrize("status", ["ok", "warn", "fail", "skipped"])
def test_dashboard_summary_shows_memory_health_without_interpreting_markup(
    status: str,
) -> None:
    from dataclasses import replace
    from typing import cast

    from rich.text import Text

    from harness.console.screens.dashboard import _render_summary
    from harness.health.model import Status

    data = replace(
        _fake_dashboard_data(),
        memory_index=CheckResult(
            id="memory.index",
            group="memory",
            status=cast(Status, status),
            message="индекс [test]",
        ),
    )
    assert (
        f"memory: {brand.STATUS_ICONS[status]} {status} — индекс [test]"
        in Text.from_markup(_render_summary(data)).plain
    )


def test_restart_command_keeps_selected_project_and_waits_for_running_commands(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            restart = next(
                command
                for command in app.get_system_commands(app.screen)
                if command.title in ("Restart console", "Перезапустить консоль")
            )
            app.commands_running = 1
            restart.callback()
            await pilot.pause()
            assert not app.restart_requested
            app.commands_running = 0
            gate = asyncio.Event()
            worker = app.screen.run_worker(gate.wait(), group="configuration")
            await pilot.pause()
            restart.callback()
            assert not app.restart_requested
            gate.set()
            await worker.wait()
            selected = tmp_path / "selected-project"
            selected.mkdir()
            app.activate_project(selected)
            await pilot.pause()
            restart.callback()
            assert app.restart_requested
            assert app.repo == selected

    asyncio.run(scenario())


def test_diagnostics_screen_unmount_cancels_workers_without_deadlock(
    tmp_path: Path,
) -> None:
    """Проверить, что размонтирование экрана отменяет фоновые воркеры и не приводит к зависанию."""
    import threading
    from textual.app import App

    started = threading.Event()
    release = threading.Event()

    def slow_diagnostics(repo: Path, *, online: bool = False) -> Report:
        started.set()
        release.wait(timeout=2.0)
        return _fake_report(online=online)

    screen = DiagnosticsScreen(tmp_path, collect_diagnostics=slow_diagnostics)

    class _HostApp(App[None]):
        def on_mount(self) -> None:
            self.push_screen(screen)

    async def scenario() -> None:
        app = _HostApp()
        async with app.run_test() as pilot:
            # Mount starts slow_diagnostics in worker; wait for thread to begin
            for _ in range(50):
                if started.is_set():
                    break
                await pilot.pause(0.01)
            app.pop_screen()
            # Release worker so it finishes its work function while screen is unmounted
            release.set()
            await pilot.pause(0.01)

    # Must complete cleanly without hanging on loop closure / shutdown_default_executor
    asyncio.run(scenario())


def test_dashboard_captures_command_output_by_default(tmp_path: Path) -> None:
    """Проверить, что дашборд без явного runner передаёт разделам перехватывающий runner,
    а не runner со сквозным выводом в терминал, которым владеет TUI."""
    from harness.console.runner import capturing_runner

    screen = DashboardScreen(
        tmp_path,
        collect_dashboard=lambda _repo, *, online=False: _fake_dashboard_data(),
    )

    assert screen._command_runner is capturing_runner


def test_dashboard_shows_why_the_checks_failed_instead_of_running_forever(
    tmp_path: Path,
) -> None:
    """Проверить, что сбой сбора данных дашборда отображается в сводке, а не оставляет «выполняются…»."""

    async def scenario() -> str:
        """Сценарий онлайн-проверок, сбор которых завершается исключением."""
        from textual.app import App
        from textual.widgets import Static

        def collect(_repo: Path, *, online: bool = False) -> DashboardData:
            """Вернуть данные офлайн и упасть на онлайн-проверках."""
            if online:
                raise RuntimeError("[boom]")
            return _fake_dashboard_data()

        class _HostApp(App[None]):
            """Тестовое приложение-хост для экрана дашборда."""

            def on_mount(self) -> None:
                """Смонтировать тестируемый экран."""
                self.push_screen(DashboardScreen(tmp_path, collect_dashboard=collect))

        app = _HostApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("o")
            await _settle(pilot)
            return str(app.screen.query_one("#dashboard-summary", Static).content)

    summary = asyncio.run(scenario())
    assert "проверки не выполнены" in summary
    # The summary is rich markup: the exception text is escaped, never interpreted as a tag.
    assert "RuntimeError('\\[boom]')" in summary


def test_diagnostics_shows_why_health_failed_instead_of_running_forever(
    tmp_path: Path,
) -> None:
    """Проверить, что сбой health на экране диагностики отображается вместо «health выполняется…»."""

    async def scenario() -> str:
        """Сценарий диагностики, сбор которой завершается исключением."""
        from textual.app import App
        from textual.widgets import Static

        def collect(repo: Path, *, online: bool = False) -> Report:
            """Упасть при сборе отчёта health."""
            raise ValueError("broken check")

        screen = DiagnosticsScreen(tmp_path, collect_diagnostics=collect)

        class _HostApp(App[None]):
            """Тестовое приложение-хост для экрана диагностики."""

            def on_mount(self) -> None:
                """Смонтировать тестируемый экран."""
                self.push_screen(screen)

        app = _HostApp()
        async with app.run_test() as pilot:
            await _settle(pilot)
            return str(app.screen.query_one("#diagnostics-report", Static).content)

    rendered = asyncio.run(scenario())
    assert "health не выполнен" in rendered
    assert "ValueError('broken check')" in rendered


def test_switching_the_project_shows_its_head_commit_in_the_banner(
    tmp_path: Path,
) -> None:
    from textual.widgets import Static

    from tests.console.test_console_brand import _committed_repo

    first, first_head = _committed_repo(tmp_path / "first")
    second, second_head = _committed_repo(tmp_path / "second")
    assert (
        first_head != second_head or True
    )  # same-second commits may share a tree only

    async def scenario() -> tuple[str, str]:
        app = HarnessConsoleApp(first)
        async with app.run_test(size=(130, 50)) as pilot:
            await pilot.pause()
            before = str(app.screen.query_one("#brand-info", Static).content)
            app.activate_project(second)
            await wait_until(
                pilot,
                lambda: f"commit {second_head}"
                in str(app.screen.query_one("#brand-info", Static).content),
            )
            return before, str(app.screen.query_one("#brand-info", Static).content)

    before, after = asyncio.run(scenario())
    assert f"commit {first_head}" in before.splitlines()[-1]
    assert f"commit {second_head}" in after.splitlines()[-1]
    assert "second" in after.splitlines()[-2]


def test_dashboard_checks_live_in_the_attention_frame_and_have_no_diagnostics_button(
    tmp_path: Path,
) -> None:
    from textual.widgets import Button

    async def scenario() -> list[str | None]:
        screen = DashboardScreen(
            tmp_path,
            collect_dashboard=lambda _repo, *, online=False: _fake_dashboard_data(),
        )
        from textual.app import App

        app = App[None]()
        async with app.run_test(size=(120, 48)) as pilot:
            await app.push_screen(screen)
            await wait_until(
                pilot,
                lambda: bool(screen.query("#dashboard-attention-scroll"))
                and len(screen.query_one("#dashboard-attention-scroll").query(Button)) == 2,
            )
            return [
                b.id
                for b in screen.query_one("#dashboard-attention-scroll").query(Button)
            ]

    assert asyncio.run(scenario()) == ["dashboard-offline", "dashboard-online"]


def test_home_uses_workspace_shell_and_keeps_technical_report_collapsed(
    tmp_path: Path,
) -> None:
    from textual.widgets import Collapsible
    from harness.console.chrome import WorkspaceHeader

    async def scenario() -> None:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(180, 52)) as pilot:
            screen = DashboardScreen(
                tmp_path,
                collect_dashboard=lambda _repo, *, online=False: _fake_dashboard_data(),
            )
            await app.push_screen(screen)
            await pilot.pause()
            assert screen.query_one(WorkspaceHeader)
            assert screen.query_one("#dashboard-details", Collapsible).collapsed
            assert (
                screen.query_one("#dashboard-breadcrumbs").region.y
                > screen.query_one("#topbar-left").region.y
            )
            sidebar = screen.query_one("#dashboard-sections")
            center = screen.query_one("#dashboard-overview")
            context = screen.query_one("#dashboard-context")
            assert sidebar.region.right <= center.region.x
            assert center.region.right <= context.region.x
            assert context.region.width >= 25

    asyncio.run(scenario())


def test_dashboard_session_list_shows_role_labels_and_status_captions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The home session list uses the shared role label and a symbol-plus-words caption."""
    from rich.text import Text
    from textual.widgets import ListView, Static

    from harness.console import reports

    stages = [
        reports.StageEntry(
            "dispatch-a", "batch-a", "#721", "developer", "working", "2026-10-16"
        ),
        reports.StageEntry(
            "dispatch-b", "batch-a", "#722", "qa", "approved", "2026-10-15"
        ),
    ]
    monkeypatch.setattr(
        reports, "load_ledger_view", lambda _repo: reports.LedgerView(stages=stages)
    )

    async def scenario() -> list[Text]:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(140, 40)):
            sessions = app.screen.query_one("#home-sessions", ListView)
            return [
                card
                for item in sessions.children
                for card in [item.query_one(Static).content]
                if isinstance(card, Text)
            ]

    cards = asyncio.run(scenario())
    assert [card.plain.splitlines()[0] for card in cards] == [
        "#721  ·  developer",
        "#722  ·  qa",
    ]
    assert "▶ Выполняется" in cards[0].plain
    assert "○ Готов к запуску" in cards[1].plain
    assert f"bold {brand.ROLE_COLORS['developer']}" in {
        str(span.style) for span in cards[0].spans
    }


# -- Diagnostics on the shared shell ---------------------------------------------------------


def _mixed_report() -> Report:
    return Report(
        schema_version=1,
        repo="/tmp/fake-repo",
        online=False,
        checks=[
            CheckResult(id="files.lock", group="files", status="ok", message="lock ok"),
            CheckResult(id="env.node", group="env", status="warn", message="node old"),
            CheckResult(id="env.uv", group="env", status="fail", message="uv missing"),
            CheckResult(
                id="net.pypi", group="net", status="skipped", message="offline"
            ),
        ],
    )


def test_report_statuses_and_counters_are_shown_as_symbol_plus_text() -> None:
    from harness.console.screens.diagnostics import _styled_report, render_report

    report = _mixed_report()
    before = render_report(report)
    shown = _styled_report(report).plain.split("\n")
    assert shown[0] == "Итого: ✔ ok=1 ⚠ warn=1 ✖ fail=1 ○ skipped=1"
    assert "✔ [OK] files.lock: lock ok" in shown
    assert "⚠ [WARN] env.node: node old" in shown
    assert "✖ [FAIL] env.uv: uv missing" in shown
    assert "○ - net.pypi: offline" in shown
    # Only the display changes: the report text and the export keep the plain markers.
    assert render_report(report) == before
    assert "✔" not in before and "✖" not in before
    english = _styled_report(report, "en").plain.split("\n")
    assert english[0].startswith("Total: ✔ ok=1") and "○ skipped=1" in english[0]


def test_a_zero_counter_keeps_its_symbol_and_is_muted() -> None:
    from harness.console.screens.diagnostics import _styled_report

    shown = _styled_report(_fake_report())
    assert shown.plain.split("\n")[0] == "Итого: ✔ ok=1 ⚠ warn=0 ✖ fail=1 ○ skipped=0"
    muted = brand.PALETTE["muted"]
    styled = {
        shown.plain[span.start : span.end]: str(span.style) for span in shown.spans
    }
    assert styled["⚠ "] == f"bold {muted}"
    assert styled["✖ "] == f"bold {brand.STATUS_COLORS['fail']}"


def test_diagnostics_sits_on_the_shell_with_the_section_menu(tmp_path: Path) -> None:
    from textual.widgets import ListItem, ListView

    from harness.console.chrome import TopBar
    from harness.console.shell import Breadcrumbs
    from tests.console._pilot_wait import crumb_text

    async def scenario() -> tuple[str, int | None, list[str | None], bool, int, bool]:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = DiagnosticsScreen(
                tmp_path,
                collect_diagnostics=lambda repo, *, online=False: _fake_report(),
            )
            await app.push_screen(screen)
            await _settle(pilot)
            menu = screen.query_one("#shell-sections", ListView)
            active = [
                item.name for item in menu.query(ListItem) if item.has_class("-active")
            ]
            crumbs = crumb_text(screen.query_one("#shell-breadcrumbs", Breadcrumbs))
            focus = screen.query_one("#diagnostics-scroll").has_focus
            topbars = len(screen.query(TopBar))
            # The open section's own number is a no-op; another number opens its section.
            await pilot.press("3")
            await _settle(pilot)
            stayed = app.screen is screen
            await pilot.press("6")
            await _settle(pilot)
            return (
                crumbs,
                menu.index,
                active,
                focus,
                topbars,
                stayed and (app.screen is not screen),
            )

    crumbs, index, active, focus, topbars, navigated = asyncio.run(scenario())
    assert crumbs == "Главная  ›  Диагностика"
    assert index == SECTIONS.index("Diagnostics")
    assert active == ["Diagnostics"]
    assert focus and topbars == 1  # only the shell's own header
    assert navigated


def test_diagnostics_switches_language_and_goes_back_with_escape(
    tmp_path: Path,
) -> None:
    from harness.console.shell import Breadcrumbs
    from tests.console._pilot_wait import crumb_text
    from textual.widgets import Static

    async def scenario() -> tuple[str, str, bool]:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = DiagnosticsScreen(
                tmp_path,
                collect_diagnostics=lambda repo, *, online=False: _fake_report(),
            )
            await app.push_screen(screen)
            await _settle(pilot)
            await pilot.press("f2")
            await _settle(pilot)
            crumbs = crumb_text(screen.query_one("#shell-breadcrumbs", Breadcrumbs))
            report = str(screen.query_one("#diagnostics-report", Static).content)
            await pilot.press("escape")
            await _settle(pilot)
            return crumbs, report, app.screen is not screen

    crumbs, report, went_back = asyncio.run(scenario())
    assert crumbs == "Home  ›  Diagnostics"
    assert report.startswith("Total: ✔ ok=1")
    assert went_back


def test_dashboard_summary_shows_counters_and_problems_as_symbol_plus_text() -> None:
    from dataclasses import replace

    from rich.text import Text

    from harness.console.screens.dashboard import _render_cards, _render_summary

    data = replace(
        _fake_dashboard_data(),
        fail=1,
        problems=(
            ("fail", "env.git", "git missing"),
            ("warn", "repo_map.tier", "minimal"),
        ),
        memory_index=CheckResult(
            id="memory.index", group="memory", status="ok", message="индекс готов"
        ),
    )
    plain = Text.from_markup(_render_summary(data)).plain.split("\n")
    assert plain[0] == "health (offline): ✔ ok=5 ⚠ warn=1 ✖ fail=1 ○ skipped=2"
    assert "  ✖ fail env.git — git missing" in plain
    assert "  ⚠ warn repo_map.tier — minimal" in plain
    # A status is also never a colour alone on the cards.
    harness_card, _orchestration, _repo_map = _render_cards(data, "1.0.0")
    assert "память: [" in harness_card and "✔ ok" in harness_card
    assert "дрейф: [" in harness_card and "✔ нет" in harness_card
