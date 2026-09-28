"""harness.console.app: TUI на базе textual. Полностью пропускается, если textual не установлен.
Пилотные тесты используют управляемый запуск и вызываются через `asyncio.run`."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Sequence

import pytest

pytest.importorskip("textual")

from textual.pilot import Pilot

from harness.console.app import HarnessConsoleApp
from harness.console.data import DashboardData
from harness.console.screens.dashboard import SECTIONS, DashboardScreen
from harness.console.screens.diagnostics import DiagnosticsScreen
from harness.console.screens.harness import HarnessScreen
from harness.health.model import CheckResult, Fix, Report


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


def test_selecting_harness_pushes_the_harness_screen(tmp_path: Path) -> None:
    """Проверить, что выбор раздела Harness открывает экран HarnessScreen."""

    async def scenario() -> bool:
        """Сценарий выбора пункта Harness в меню."""
        from textual.widgets import ListView

        app = HarnessConsoleApp(tmp_path)
        async with app.run_test() as pilot:
            menu = app.screen.query_one("#section-menu", ListView)
            menu.index = SECTIONS.index("Harness")
            await pilot.press("enter")
            await pilot.pause()
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
            menu = app.screen.query_one("#section-menu", ListView)
            menu.index = SECTIONS.index("Orchestration")
            await pilot.press("enter")
            await pilot.pause()
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
    assert "active batches: 3" in rendered
    assert "9.9.9" in rendered
    assert "clean" in rendered


def test_dashboard_shows_the_mark_and_the_banner_beside_it(tmp_path: Path) -> None:
    """The home screen opens with the harness mark and a description of this installation."""

    async def scenario() -> tuple[str, str]:
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
        return mark, info

    mark, info = asyncio.run(scenario())
    from harness.console import brand

    assert mark.splitlines() == list(brand.LOGO)
    assert f"{brand.PRODUCT} 9.9.9" in info
    assert "pvmalove-suite" in info
    assert "~/work/app" in info
    assert "ветка feature/issue-7-x" in info


def test_console_registers_and_selects_the_warm_theme(tmp_path: Path) -> None:
    async def scenario() -> str:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test():
            return app.theme

    from harness.console import brand

    assert asyncio.run(scenario()) == brand.THEME_NAME


def test_help_opens_from_the_menu_and_from_f1(tmp_path: Path) -> None:
    async def scenario() -> tuple[bool, bool]:
        from textual.widgets import ListView

        from harness.console.screens.help import HelpScreen

        app = HarnessConsoleApp(tmp_path)
        async with app.run_test() as pilot:
            menu = app.screen.query_one("#section-menu", ListView)
            menu.index = SECTIONS.index("Help")
            await pilot.press("enter")
            await pilot.pause()
            from_menu = isinstance(app.screen, HelpScreen)
            await pilot.press("escape")
            await pilot.pause()
            await pilot.press("f1")
            await pilot.pause()
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


def test_dashboard_online_checks_action_reruns_with_online_and_shows_the_cli(
    tmp_path: Path,
) -> None:
    """Проверить, что онлайн-проверки дашборда перезапускают сбор и выводят команду CLI."""

    async def scenario() -> tuple[list[bool], str, str]:
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
            await pilot.click("#dashboard-online")
            await _settle(pilot)
            summary = str(app.screen.query_one("#dashboard-summary", Static).content)
            cli = str(app.screen.query_one("#dashboard-online-cli", Static).content)
        return seen_online, summary, cli

    seen_online, summary, cli = asyncio.run(scenario())
    assert seen_online == [False, True]
    assert "health (online)" in summary
    assert "harness health" in cli and "--online" in cli


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
            await pilot.click("#dashboard-offline")
            await _settle(pilot)
            cli = str(app.screen.query_one("#dashboard-offline-cli", Static).content)
            app.push_screen(screens[1])
            await _settle(pilot)
            await pilot.click("#offline-checks")
            await _settle(pilot)
        return dashboard_online, diagnostics_online, cli

    dashboard_online, diagnostics_online, cli = asyncio.run(scenario())
    assert dashboard_online == [False, False]
    assert diagnostics_online == [False, False]
    assert cli.endswith(f"health {tmp_path}")


def test_dashboard_summary_lists_problems_and_splits_repo_map_facts() -> None:
    """Проверить, что сводка перечисляет ошибки и предупреждения и разбивает строку Repo Map."""
    from dataclasses import replace

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
    assert "repo map: tier=minimal\n  policy: enforced" in text
    assert "ошибки и предупреждения:" in text
    assert "env.git — git \\[bold] missing" in text
    assert text.index("env.git") < text.index("repo_map.tier —")
