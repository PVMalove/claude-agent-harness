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
from textual.widgets import Input, ListView, Static, Tree

from _console_repo_map_fixture import HUB, build_repo_map_fixture, map_payload
from harness.console import repo_map as console_repo_map
from harness.console.data import DashboardData
from harness.console.screens.dashboard import SECTIONS, DashboardScreen
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
            collect_dashboard=lambda _repo: DashboardData(
                0, 0, 0, 0, "-", "0", "-", None
            ),
            command_runner=_RecordingRunner(),
        )

        class _DashboardHost(App[None]):
            def on_mount(self) -> None:
                self.push_screen(screen)

        app = _DashboardHost()
        async with app.run_test() as pilot:
            menu = app.screen.query_one("#section-menu", ListView)
            menu.focus()
            menu.index = SECTIONS.index("Repo Map")
            await pilot.press("enter")
            await pilot.pause()
            return isinstance(app.screen, RepoMapScreen)

    assert asyncio.run(scenario())


def test_cached_head_map_is_shown_at_once_without_running_the_cli(
    tmp_path: Path,
) -> None:
    commit = build_repo_map_fixture(tmp_path)
    runner = _RecordingRunner()

    async def scenario() -> tuple[str, str, str, str]:
        app = _RepoMapHost(tmp_path, runner)
        async with app.run_test() as pilot:
            await pilot.pause()
            return (
                _static_text(app, "#repo-map-status"),
                _static_text(app, "#map-summary"),
                _static_text(app, "#map-hubs"),
                _static_text(app, "#map-diagnostics"),
            )

    status, summary, hubs, diagnostics = asyncio.run(scenario())
    assert runner.calls == []
    assert "из кэша" in status
    assert "tier: full (parser: bundle)" in summary
    assert "причина деградации: parser bundle applied" in summary
    assert f"commit: {commit}" in summary
    assert "файлов: 4" in summary and "рёбер: 4" in summary
    assert "токенов (оценка): 512" in summary
    assert "bundle_source: local-cache" in summary
    assert "grammar: python@0.23.6" in summary
    assert hubs.splitlines()[0].split() == ["2", HUB]
    assert diagnostics == "syntax_error: pkg/broken.py"


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
    assert "broken.py [syntax_error]" in full
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
        "исходящие · unique-name-ref · medium: pkg/cli.py",
        "входящие · import · high: pkg/cli.py",
        "входящие · unique-name-ref · medium: pkg/cli.py",
        "входящие · ambiguous-name-ref · low: pkg/broken.py",
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
            await pilot.pause()
            is_confirm = isinstance(app.screen, BuildConfirmScreen)
            warning = _static_text(app, "#build-warning")
            await pilot.click("#cancel-build")
            await _settle(pilot)
            return status, is_confirm, warning, _static_text(app, "#map-summary")

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
            await pilot.pause()
            await pilot.click("#confirm-build")
            await _settle(pilot)
            return (
                _static_text(app, "#repo-map-status"),
                _static_text(app, "#map-summary"),
                _tree_labels(app),
            )

    status, summary, labels = asyncio.run(scenario())
    assert runner.calls == [console_repo_map.build_argv(tmp_path, commit)]
    assert "построена" in status
    assert f"commit: {commit}" in summary
    assert "источник: построена" in summary
    assert "core.py [ok]" in labels


def test_failed_build_reports_why(tmp_path: Path) -> None:
    build_repo_map_fixture(tmp_path, cached=False)
    runner = _RecordingRunner(returncode=2)

    async def scenario() -> str:
        app = _RepoMapHost(tmp_path, runner)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.click("#build-map")
            await pilot.pause()
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
