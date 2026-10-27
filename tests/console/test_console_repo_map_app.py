"""Pilot tests for the console's Repo Map section on a real repository with a cached schema v1 map
(tests/_console_repo_map_fixture.py). Skipped when textual is not installed, like
tests/test_console_app.py; driven with `asyncio.run` for the same reason."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Mapping, Sequence

import pytest

pytest.importorskip("textual")

from textual.app import App
from textual.pilot import Pilot
from textual.widgets import Button, Input, ListView, Static, TabbedContent, Tree

from tests.console._pilot_wait import wait_until
from tests.console._console_repo_map_fixture import (
    HUB,
    build_repo_map_fixture,
    map_payload,
)
from harness.console import repo_map as console_repo_map
from harness.console.data import DashboardData
from harness.console.screens.dashboard import DashboardScreen
from harness.console.shell import SECTIONS, ConsoleShell, Fold
from harness.console.screens.repo_map import BuildConfirmScreen, RepoMapScreen


class _RecordingRunner:
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.calls: list[list[str]] = []

    def __call__(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> "subprocess.CompletedProcess[str]":
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(
            list(argv), self.returncode, self.stdout, "boom" if self.returncode else ""
        )


class _RepoMapHost(App[None]):
    def __init__(self, repo: Path, runner: _RecordingRunner) -> None:
        super().__init__()
        self.repo = repo
        self.runner = runner

    def on_mount(self) -> None:
        self.push_screen(RepoMapScreen(self.repo, command_runner=self.runner))


def _static_text(app: App[None], selector: str) -> str:
    return str(app.screen.query_one(selector, Static).content)


def _screen_text(app: App[None], selector: str) -> str:
    """All the text the widgets under `selector` show."""
    root = app.screen.query_one(selector)
    return "\n".join(str(widget.content) for widget in root.query(Static))


def _tree_labels(app: App[None]) -> list[str]:
    tree: Tree[str] = app.screen.query_one("#file-tree", Tree)
    labels: list[str] = []
    pending = list(tree.root.children)
    while pending:
        node = pending.pop(0)
        labels.append(str(node.label))
        pending.extend(node.children)
    return labels


async def _settle(pilot: Pilot[None]) -> None:
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


def test_dashboard_repo_map_section_opens_the_repo_map_screen(tmp_path: Path) -> None:
    build_repo_map_fixture(tmp_path)

    async def scenario() -> bool:
        screen = DashboardScreen(
            tmp_path,
            collect_dashboard=lambda _repo, *, online=False: DashboardData(
                0, 0, 0, 0, "-", "0", "-", None
            ),
            command_runner=_RecordingRunner(),
        )

        class _DashboardHost(App[None]):
            def on_mount(self) -> None:
                self.push_screen(screen)

        app = _DashboardHost()
        async with app.run_test() as pilot:
            await pilot.click("#dashboard-menu")
            menu = app.screen.query_one("#section-menu", ListView)
            menu.focus()
            menu.index = SECTIONS.index("Repo Map")
            await pilot.press("enter")
            await wait_until(pilot, lambda: isinstance(app.screen, RepoMapScreen))
            return isinstance(app.screen, RepoMapScreen)

    assert asyncio.run(scenario())


def test_cached_head_map_is_shown_at_once_without_running_the_cli(
    tmp_path: Path,
) -> None:
    commit = build_repo_map_fixture(tmp_path)
    runner = _RecordingRunner()

    async def scenario() -> tuple[str, str, str, str, str]:
        app = _RepoMapHost(tmp_path, runner)
        async with app.run_test() as pilot:
            await pilot.pause()
            return (
                _static_text(app, "#repo-map-status"),
                _screen_text(app, "#map-summary"),
                _static_text(app, "#map-provenance"),
                _static_text(app, "#map-hubs"),
                _static_text(app, "#map-diagnostics"),
            )

    status, summary, provenance, hubs, diagnostics = asyncio.run(scenario())
    assert runner.calls == []
    assert status == f"✔ карта HEAD {commit[:12]} из кэша · full"
    for label, value in (
        ("tier", "full (parser: bundle)"),
        ("Деградация", "parser bundle applied"),
        ("commit", commit),
        ("Файлов", "4"),
        ("Рёбер", "4"),
        ("Токенов (оценка)", "512"),
        ("Источник", "кэш"),
    ):
        assert f"{label}\n{value}" in summary
    assert "bundle_source: local-cache" in provenance
    assert "grammar: python@0.23.6" in provenance
    assert hubs.splitlines()[0].split() == ["2", HUB]
    assert diagnostics == "⚠ syntax_error: pkg/broken.py"


def test_tree_shows_signatures_and_parser_status_and_search_filters(
    tmp_path: Path,
) -> None:
    build_repo_map_fixture(tmp_path)

    async def scenario() -> tuple[list[str], list[str], str]:
        app = _RepoMapHost(tmp_path, _RecordingRunner())
        async with app.run_test() as pilot:
            await pilot.pause()
            full = _tree_labels(app)
            search = app.screen.query_one("#symbol-search", Input)
            search.value = "load_config"
            await pilot.pause()
            return full, _tree_labels(app), _static_text(app, "#search-status")

    full, filtered, search_status = asyncio.run(scenario())
    assert "pkg/" in full
    assert "core.py [ok]" in full
    assert "broken.py ✖ [syntax_error]" in full
    assert "def load_config(path)" in full
    assert "def main(argv)" in full
    assert filtered == [
        "pkg/",
        "core.py [ok]",
        "def load_config(path)",
        "class Settings",
    ]
    assert search_status == "найдено файлов: 1 из 4"


def test_selecting_a_file_shows_its_relations(tmp_path: Path) -> None:
    build_repo_map_fixture(tmp_path)

    async def scenario() -> str:
        app = _RepoMapHost(tmp_path, _RecordingRunner())
        async with app.run_test() as pilot:
            await pilot.pause()
            tree: Tree[str] = app.screen.query_one("#file-tree", Tree)
            pkg = tree.root.children[0]
            core = next(node for node in pkg.children if node.data == HUB)
            tree.select_node(core)
            await pilot.pause()
            return _static_text(app, "#file-relations")

    relations = asyncio.run(scenario())
    assert relations.splitlines() == [
        HUB,
        "исходящие · unique-name-ref · medium · 1",
        "  pkg/cli.py",
        "входящие · import · high · 1",
        "  pkg/cli.py",
        "входящие · unique-name-ref · medium · 1",
        "  pkg/cli.py",
        "входящие · ambiguous-name-ref · low · 1",
        "  pkg/broken.py",
    ]


def test_build_without_cache_warns_and_cancel_runs_nothing(tmp_path: Path) -> None:
    build_repo_map_fixture(tmp_path, cached=False)
    runner = _RecordingRunner()

    async def scenario() -> tuple[str, bool, str, str]:
        app = _RepoMapHost(tmp_path, runner)
        async with app.run_test() as pilot:
            await pilot.pause()
            status = _static_text(app, "#repo-map-status")
            await pilot.click("#build-map")
            await wait_until(pilot, lambda: isinstance(app.screen, BuildConfirmScreen))
            is_confirm = isinstance(app.screen, BuildConfirmScreen)
            warning = _static_text(app, "#build-warning")
            await pilot.click("#cancel-build")
            await _settle(pilot)
            return status, is_confirm, warning, _static_text(app, "#map-summary-empty")

    status, is_confirm, warning, summary = asyncio.run(scenario())
    assert "кэша карты для HEAD" in status
    assert is_confirm
    assert "parser bundle" in warning
    assert runner.calls == []
    assert summary == "карты нет"


def test_confirmed_build_runs_the_cli_and_shows_the_map(tmp_path: Path) -> None:
    commit = build_repo_map_fixture(tmp_path, cached=False)
    runner = _RecordingRunner(json.dumps(map_payload(commit)))

    async def scenario() -> tuple[str, str, list[str]]:
        app = _RepoMapHost(tmp_path, runner)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.click("#build-map")
            await wait_until(pilot, lambda: isinstance(app.screen, BuildConfirmScreen))
            await pilot.click("#confirm-build")
            await _settle(pilot)
            return (
                _static_text(app, "#repo-map-status"),
                _screen_text(app, "#map-summary"),
                _tree_labels(app),
            )

    status, summary, labels = asyncio.run(scenario())
    assert runner.calls == [console_repo_map.build_argv(tmp_path, commit)]
    assert status == f"✔ карта HEAD {commit[:12]} построена"
    assert f"commit\n{commit}" in summary
    assert "Источник\nпостроена" in summary
    assert "core.py [ok]" in labels


def test_failed_build_reports_why(tmp_path: Path) -> None:
    build_repo_map_fixture(tmp_path, cached=False)
    runner = _RecordingRunner(returncode=2)

    async def scenario() -> str:
        app = _RepoMapHost(tmp_path, runner)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.click("#build-map")
            await wait_until(pilot, lambda: isinstance(app.screen, BuildConfirmScreen))
            await pilot.click("#confirm-build")
            await _settle(pilot)
            return _static_text(app, "#repo-map-status")

    status = asyncio.run(scenario())
    assert "карта не построена" in status
    assert "кодом 2" in status and "boom" in status


def test_export_writes_markdown_and_json(tmp_path: Path) -> None:
    commit = build_repo_map_fixture(tmp_path)

    async def scenario() -> list[Path]:
        app = _RepoMapHost(tmp_path, _RecordingRunner())
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.click("#export-markdown")
            await pilot.pause(0.4)
            await pilot.click("#export-json")
            await pilot.pause()
        return sorted((tmp_path / "docs" / "tasks" / "console-exports").iterdir())

    markdown, exported_json = sorted(
        asyncio.run(scenario()), key=lambda p: p.suffix != ".md"
    )
    assert markdown.name.endswith(f"-repo-map-{commit[:12]}.md")
    text = markdown.read_text("utf-8")
    assert text.startswith(f"# Repo Map {commit[:12]}\n")
    assert f"- **Commit:** `{commit}`" in text
    assert "pkg/cli.py -> pkg/core.py (import, high)" in text
    assert exported_json.name.endswith(f"-repo-map-{commit[:12]}.json")
    assert json.loads(exported_json.read_text("utf-8")) == map_payload(commit)


def test_export_without_a_map_writes_nothing(tmp_path: Path) -> None:
    build_repo_map_fixture(tmp_path, cached=False)

    async def scenario() -> None:
        app = _RepoMapHost(tmp_path, _RecordingRunner())
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.click("#export-markdown")
            await pilot.pause(0.4)
            await pilot.click("#export-json")
            await pilot.pause()

    asyncio.run(scenario())
    assert not (tmp_path / "docs").exists()


class _RaisingRunner(_RecordingRunner):
    def __call__(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> "subprocess.CompletedProcess[str]":
        self.calls.append(list(argv))
        raise ValueError("embedded null byte")


def test_a_build_that_raises_reports_why_instead_of_building_forever(
    tmp_path: Path,
) -> None:
    build_repo_map_fixture(tmp_path, cached=False)
    runner = _RaisingRunner()

    async def scenario() -> str:
        app = _RepoMapHost(tmp_path, runner)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.click("#build-map")
            await wait_until(pilot, lambda: isinstance(app.screen, BuildConfirmScreen))
            await pilot.click("#confirm-build")
            await _settle(pilot)
            return _static_text(app, "#repo-map-status")

    status = asyncio.run(scenario())
    assert len(runner.calls) == 1
    assert status.startswith("✖ карта не построена: сбой построения:")
    assert "ValueError('embedded null byte')" in status


def _footer_keys(app: App[None]) -> dict[str, str]:
    shown = {}
    for key, active in app.screen.active_bindings.items():
        binding = active.binding
        if binding.show and binding.description:
            shown[binding.key_display or key] = binding.description
    return shown


def test_repo_map_stands_on_the_shared_frame_with_the_mockup_footer(
    tmp_path: Path,
) -> None:
    build_repo_map_fixture(tmp_path)

    async def scenario() -> tuple[bool, str, str, dict[str, str], dict[str, str]]:
        app = _RepoMapHost(tmp_path, _RecordingRunner())
        async with app.run_test(size=(160, 52)) as pilot:
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, ConsoleShell)
            wide = _footer_keys(app)
            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            return True, screen.SECTION or "", screen.CRUMBS, wide, _footer_keys(app)

    shell, section, crumbs, wide, narrow = asyncio.run(scenario())
    assert shell and section == "Repo Map" and crumbs == "Главная › Repo Map"
    assert wide == {
        "escape": "Назад",
        "b": "Собрать",
        "e": "Markdown",
        "j": "JSON",
    }
    assert narrow == {"escape": "Назад", "b": "Собрать"}


def test_repo_map_header_buttons_and_tabs_shorten_on_a_narrow_terminal(
    tmp_path: Path,
) -> None:
    build_repo_map_fixture(tmp_path)

    async def scenario() -> tuple[list[str], list[str], list[str], list[str], bool]:
        app = _RepoMapHost(tmp_path, _RecordingRunner())
        async with app.run_test(size=(160, 52)) as pilot:
            await pilot.pause()

            def buttons() -> list[str]:
                return [
                    str(app.screen.query_one(f"#{name}", Button).label)
                    for name in ("build-map", "export-markdown", "export-json")
                ]

            def tabs() -> list[str]:
                bar = app.screen.query_one("#repo-map-tabs", TabbedContent)
                return [
                    str(bar.get_tab(pane).label)
                    for pane in (
                        "tab-summary",
                        "tab-files",
                        "tab-hubs",
                        "tab-diagnostics",
                        "tab-settings",
                    )
                ]

            wide = (buttons(), tabs())
            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            narrow = (buttons(), tabs())
            fold = app.screen.query_one("#relations-fold", Fold)
            return wide[0], wide[1], narrow[0], narrow[1], fold.collapsed

    wide_buttons, wide_tabs, narrow_buttons, narrow_tabs, folded = asyncio.run(
        scenario()
    )
    assert wide_buttons == [
        "b Собрать карту",
        "e Экспорт в Markdown",
        "j Экспорт в JSON",
    ]
    assert wide_tabs == ["Сводка", "Файлы", "Хабы", "Диагностики", "Настройки"]
    assert narrow_buttons == ["b Собрать", "e MD", "j JSON"]
    assert narrow_tabs == ["Свод", "Файлы", "Хабы", "Диагн.", "Настр."]
    assert folded


def test_the_context_strip_follows_the_tab(tmp_path: Path) -> None:
    commit = build_repo_map_fixture(tmp_path)

    async def scenario() -> tuple[str, str, str, str]:
        app = _RepoMapHost(tmp_path, _RecordingRunner())
        async with app.run_test(size=(160, 52)) as pilot:
            await pilot.pause()
            files = (
                _static_text(app, "#repo-map-context"),
                _static_text(app, "#repo-map-note"),
            )
            app.screen.query_one(
                "#repo-map-tabs", TabbedContent
            ).active = "tab-settings"
            await pilot.pause()
            return (
                *files,
                _static_text(app, "#repo-map-context"),
                _static_text(app, "#repo-map-note"),
            )

    context, note, settings_context, settings_note = asyncio.run(scenario())
    assert context == f"Проект: {tmp_path.name} · HEAD {commit[:12]}"
    assert note == "Данные карты из кэша; сеть не используется"
    assert settings_context == (
        f"Проект: {tmp_path.name} · политика: orchestration.json → repo_map_policy"
    )
    assert (
        settings_note == "Установка и сборка запускают команду из формы подтверждения"
    )


def test_the_status_line_has_a_symbol_for_every_state(tmp_path: Path) -> None:
    build_repo_map_fixture(tmp_path, cached=False)

    class _NoGitHost(App[None]):
        def on_mount(self) -> None:
            self.push_screen(RepoMapScreen(tmp_path, find_head=lambda _repo: None))

    async def scenario() -> tuple[str, str, str, bool]:
        app = _RepoMapHost(tmp_path, _RecordingRunner())
        async with app.run_test(size=(160, 52)) as pilot:
            await pilot.pause()
            no_cache = _static_text(app, "#repo-map-status")
            app.screen._set_status("строится карта HEAD abc…", "running")  # type: ignore[attr-defined]
            building = _static_text(app, "#repo-map-status")
        no_git = _NoGitHost()
        async with no_git.run_test(size=(160, 52)) as pilot:
            await pilot.pause()
            return (
                no_cache,
                building,
                _static_text(no_git, "#repo-map-status"),
                no_git.screen.query_one("#build-map", Button).disabled,
            )

    no_cache, building, no_git, build_disabled = asyncio.run(scenario())
    assert no_cache.startswith("○ кэша карты для HEAD ")
    assert building == "▶ строится карта HEAD abc…"
    assert no_git.startswith("✖ HEAD не найден")
    assert build_disabled


def test_a_long_path_in_the_relations_stays_whole_by_wrapping(tmp_path: Path) -> None:
    build_repo_map_fixture(tmp_path)

    async def scenario() -> tuple[int, int, str]:
        app = _RepoMapHost(tmp_path, _RecordingRunner())
        async with app.run_test(size=(60, 40)) as pilot:
            await pilot.pause()
            app.screen.query_one("#repo-map-tabs", TabbedContent).active = "tab-files"
            await pilot.pause()
            tree: Tree[str] = app.screen.query_one("#file-tree", Tree)
            core = next(
                node for node in tree.root.children[0].children if node.data == HUB
            )
            tree.select_node(core)
            app.screen.query_one("#relations-fold", Fold).collapsed = False
            await pilot.pause()
            relations = app.screen.query_one("#file-relations", Static)
            return (
                relations.region.right,
                app.screen.size.width,
                _static_text(app, "#file-relations"),
            )

    right, width, text = asyncio.run(scenario())
    assert right <= width
    assert "pkg/cli.py" in text


def test_the_build_confirmation_keeps_its_ids_and_warns_in_the_warning_colour(
    tmp_path: Path,
) -> None:
    from harness.console import brand
    from harness.console.app import HarnessConsoleApp

    build_repo_map_fixture(tmp_path, cached=False)

    async def scenario() -> str:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(160, 52)) as pilot:
            await app.push_screen(RepoMapScreen(tmp_path))
            await pilot.pause()
            await pilot.click("#build-map")
            await pilot.pause()
            assert isinstance(app.screen, BuildConfirmScreen)
            warning = app.screen.query_one("#build-warning", Static)
            return warning.styles.color.hex

    shown = asyncio.run(scenario()).lstrip("#")
    wanted = brand.PALETTE["warning"].lstrip("#")
    # The theme blends the colour a little when it resolves it: allow a rounding step.
    assert all(
        abs(int(shown[i : i + 2], 16) - int(wanted[i : i + 2], 16)) <= 2
        for i in (0, 2, 4)
    )


def test_repo_map_follows_the_console_language(tmp_path: Path) -> None:
    build_repo_map_fixture(tmp_path)

    async def scenario() -> tuple[list[str], dict[str, str], str, str, str]:
        app = _RepoMapHost(tmp_path, _RecordingRunner())
        async with app.run_test(size=(160, 52)) as pilot:
            await pilot.pause()
            app.language = "en"  # type: ignore[attr-defined]
            app.screen.translate_now()  # type: ignore[attr-defined]
            await pilot.pause()
            bar = app.screen.query_one("#repo-map-tabs", TabbedContent)
            return (
                [
                    str(app.screen.query_one(f"#{name}", Button).label)
                    for name in _BUTTON_IDS
                ],
                _footer_keys(app),
                _static_text(app, "#repo-map-status"),
                str(bar.get_tab("tab-summary").label),
                _static_text(app, "#repo-map-note"),
            )

    buttons, footer, status, tab, note = asyncio.run(scenario())
    assert buttons == ["b Build map", "e Export to Markdown", "j Export to JSON"]
    assert footer["b"] == "Build"
    assert (
        status.startswith("✔ map HEAD") or "from cache" in status or "cache" in status
    )
    assert tab == "Summary"
    assert note.startswith("Map data comes from the cache")


_BUTTON_IDS = ("build-map", "export-markdown", "export-json")
