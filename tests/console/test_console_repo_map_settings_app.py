"""The Repo Map screen's Settings tab: tier, language parsers and project memory."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Mapping, Sequence

import pytest

from rich.color import Color
from textual import events
from textual.app import App
from textual.pilot import Pilot
from textual.widgets import Button, RadioButton, SelectionList, Static, TabbedContent

from harness.console import brand
from harness.console.app import HarnessConsoleApp
from harness.console.screens.configuration import ConfigurationScreen
from harness.console.screens.repo_map import RepoMapScreen
from harness.repo_map.bundle_fetch import ParserStatus
from tests.console.test_console_configuration_app import (
    _orchestration_project,
    installed_project,
)

NO_BUNDLE = ParserStatus(
    Path("/registry"), None, None, (), "offline parser bundle unavailable"
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
        return subprocess.CompletedProcess(list(argv), 0, '{"status": "built"}\n', "")


def _git_project(repo: Path, *, orchestration: bool = True) -> Path:
    (_orchestration_project if orchestration else installed_project)(repo)
    (repo / "a.py").write_text("x = 1\n")
    (repo / "b.go").write_text("package b\n")
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    return repo


class _Host(App[None]):
    def __init__(self, repo: Path, runner: _Runner, status: ParserStatus) -> None:
        super().__init__()
        self.repo, self.runner, self.status = repo, runner, status

    def on_mount(self) -> None:
        self.push_screen(
            RepoMapScreen(
                self.repo,
                command_runner=self.runner,
                find_head=lambda _repo: None,
                load_parsers=lambda _repo: self.status,
            )
        )


async def _open(app: App[None], pilot: Pilot[None]) -> None:
    await pilot.pause()
    app.screen.query_one("#repo-map-tabs", TabbedContent).active = "tab-settings"
    await _settle(pilot)


async def _settle(pilot: Pilot[None]) -> None:
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()
    await pilot.pause()


def _text(app: App[None], selector: str) -> str:
    return str(app.screen.query_one(selector, Static).content)


def test_the_settings_tab_shows_tier_parsers_and_memory(tmp_path: Path) -> None:
    repo = _git_project(tmp_path)

    async def scenario() -> None:
        app = _Host(repo, _Runner(), NO_BUNDLE)
        async with app.run_test(size=(120, 48)) as pilot:
            await _open(app, pilot)
            assert "full" in _text(app, "#tier-status")
            assert "не установлен" in _text(app, "#parser-status") or (
                "парсер не установлен" in _text(app, "#parser-status")
            )
            options = app.screen.query_one("#parser-languages", SelectionList)
            # The languages the project has and no parser covers yet start ticked.
            assert sorted(options.selected) == ["go", "python"]
            assert "выключена" in _text(app, "#memory-status")

    asyncio.run(scenario())


def test_the_tier_is_switched_after_confirmation(tmp_path: Path) -> None:
    repo = _git_project(tmp_path)

    async def scenario() -> None:
        app = _Host(repo, _Runner(), NO_BUNDLE)
        async with app.run_test(size=(120, 48)) as pilot:
            await _open(app, pilot)
            app.screen.query_one("#tier-minimal", RadioButton).value = True
            await pilot.click("#tier-apply")
            await pilot.pause()
            before = (repo / ".harness/orchestration.json").read_bytes()
            await pilot.click("#cancel")
            await _settle(pilot)
            assert (repo / ".harness/orchestration.json").read_bytes() == before
            await pilot.click("#tier-apply")
            await pilot.pause()
            await pilot.click("#confirm")
            await _settle(pilot)
            written = json.loads((repo / ".harness/orchestration.json").read_bytes())
            assert written["repo_map_policy"]["tier"] == "minimal"
            assert "minimal" in _text(app, "#tier-status")
            assert "Режим записан" in _text(app, "#settings-output")

    asyncio.run(scenario())


def test_the_tier_cannot_be_switched_without_orchestration(tmp_path: Path) -> None:
    repo = _git_project(tmp_path, orchestration=False)

    async def scenario() -> None:
        app = _Host(repo, _Runner(), NO_BUNDLE)
        async with app.run_test(size=(120, 48)) as pilot:
            await _open(app, pilot)
            assert app.screen.query_one("#tier-apply", Button).disabled
            assert "backend-orchestration" in _text(app, "#tier-status")

    asyncio.run(scenario())


def test_installing_parsers_runs_the_cli_command_after_confirmation(
    tmp_path: Path,
) -> None:
    repo = _git_project(tmp_path)
    runner = _Runner()

    async def scenario() -> None:
        app = _Host(repo, runner, NO_BUNDLE)
        async with app.run_test(size=(120, 48)) as pilot:
            await _open(app, pilot)
            await pilot.click("#parser-install")
            await pilot.pause()
            assert runner.calls == []  # the confirmation form comes first
            await pilot.click("#run")
            await _settle(pilot)
            assert runner.calls
            argv = runner.calls[0]
            assert argv[2:4] == ["parsers", "install"]
            assert (
                argv[argv.index("--language") + 1 :]
                == [
                    "go",
                    "--language",
                    "python",
                ]
                or argv.count("--language") == 2
            )
            assert "код выхода: 0" in _text(app, "#settings-output")

    asyncio.run(scenario())


def test_memory_is_enabled_then_built_when_it_is_off(tmp_path: Path) -> None:
    repo = _git_project(tmp_path)
    runner = _Runner()

    async def scenario() -> None:
        app = _Host(repo, runner, NO_BUNDLE)
        async with app.run_test(size=(120, 48)) as pilot:
            await _open(app, pilot)
            await pilot.click("#memory-init")
            await pilot.pause()
            assert runner.calls == []
            await pilot.click("#confirm")
            await _settle(pilot)
            await _settle(pilot)
            config = json.loads((repo / ".harness/project.json").read_bytes())
            assert config["memory"] == {"enabled": True}
            assert runner.calls[0][2:4] == ["memory", "build"]

    asyncio.run(scenario())


def test_memory_sync_and_rebuild_run_their_commands(tmp_path: Path) -> None:
    repo = _git_project(tmp_path)
    runner = _Runner()

    async def scenario() -> None:
        app = _Host(repo, runner, NO_BUNDLE)
        async with app.run_test(size=(120, 48)) as pilot:
            await _open(app, pilot)
            await pilot.click("#memory-sync")
            await _settle(pilot)
            await pilot.click("#memory-rebuild")
            await _settle(pilot)
            assert [call[2:4] for call in runner.calls] == [
                ["memory", "sync"],
                ["memory", "rebuild"],
            ]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "button, field",
    [("#parser-settings", "repo_map_policy"), ("#memory-settings", "memory")],
)
def test_the_wizard_opens_on_the_policy_it_was_asked_for(
    tmp_path: Path, button: str, field: str
) -> None:
    repo = _git_project(tmp_path)

    async def scenario() -> None:
        app = _Host(repo, _Runner(), NO_BUNDLE)
        async with app.run_test(size=(120, 48)) as pilot:
            await _open(app, pilot)
            await pilot.click(button)
            await _settle(pilot)
            screen = app.screen
            assert isinstance(screen, ConfigurationScreen)
            for _ in range(40):  # a long step list takes a moment to build
                if screen.steps and screen.steps[screen.index].path[0] == field:
                    break
                await pilot.pause(0.25)
            assert screen.steps[screen.index].path[0] == field

    asyncio.run(scenario())


def test_the_pane_follows_the_console_language(tmp_path: Path) -> None:
    repo = _git_project(tmp_path)

    async def scenario() -> None:
        app = HarnessConsoleApp(repo)
        async with app.run_test(size=(120, 48)) as pilot:
            await app.push_screen(
                RepoMapScreen(
                    repo,
                    command_runner=_Runner(),
                    find_head=lambda _repo: None,
                    load_parsers=lambda _repo: NO_BUNDLE,
                )
            )
            await _settle(pilot)
            app.screen.query_one(
                "#repo-map-tabs", TabbedContent
            ).active = "tab-settings"
            await _settle(pilot)
            await pilot.press("f2")
            await _settle(pilot)
            assert (
                str(app.screen.query_one("#tier-apply", Button).label) == "Apply mode"
            )
            assert _text(app, "#tier-status").startswith("In the project policy:")
            assert "Memory:" in _text(app, "#memory-status")
            assert events  # keep the import used by type checkers

    asyncio.run(scenario())


def test_an_installed_parser_is_ticked_locked_and_grey(tmp_path: Path) -> None:
    repo = _git_project(tmp_path)
    runner = _Runner()
    ready = ParserStatus(
        Path("/registry"), "cp314-linux_x86_64", "local-cache", ("python",), None
    )

    async def scenario() -> None:
        app = _Host(repo, runner, ready)
        async with app.run_test(size=(120, 48)) as pilot:
            await _open(app, pilot)
            options = app.screen.query_one("#parser-languages", SelectionList)
            python, go = options.get_option_at_index(0), options.get_option_at_index(3)
            assert python.disabled and "python" in options.selected
            # Go is in the project and not installed: ticked, free to change.
            assert not go.disabled and "go" in options.selected
            # The locked row cannot be toggled by keyboard.
            options.highlighted = 0
            options.action_select()
            await pilot.pause()
            assert "python" in options.selected
            # Its glyph is the muted grey, not the accent colour of a ticked item.
            first = list(options.render_line(0))[0]
            assert first.text.strip() == "☑"
            assert first.style is not None and first.style.color is not None
            assert (
                first.style.color.triplet == Color.parse(brand.PALETTE["muted"]).triplet
            )
            # Installing ignores the locked row.
            options.deselect("go")
            await pilot.click("#parser-install")
            await pilot.pause()
            assert "Отметьте язык" in _text(app, "#settings-output")
            assert runner.calls == []

    asyncio.run(scenario())


def test_the_diagnostics_tab_runs_the_checks_when_it_opens(tmp_path: Path) -> None:
    repo = _git_project(tmp_path)
    ready = ParserStatus(
        Path("/registry"), "cp314-linux_x86_64", "local-cache", ("python",), None
    )

    async def scenario() -> None:
        app = _Host(repo, _Runner(), ready)
        async with app.run_test(size=(120, 48)) as pilot:
            await pilot.pause()
            assert not app.screen.query("#open-settings")  # one way in: the tab
            app.screen.query_one(
                "#repo-map-tabs", TabbedContent
            ).active = "tab-diagnostics"
            await _settle(pilot)
            text = _text(app, "#map-checks")
            assert "Python: парсер установлен" in text
            assert "Go: парсер не установлен" in text
            assert "Память проекта выключена" in text
            await pilot.click("#run-checks")
            await _settle(pilot)
            assert "Bundle парсеров готов" in _text(app, "#map-checks")

    asyncio.run(scenario())


def test_the_pane_says_why_the_map_is_minimal_although_the_policy_is_full(
    tmp_path: Path,
) -> None:
    repo = _git_project(tmp_path)

    async def scenario() -> None:
        app = _Host(repo, _Runner(), NO_BUNDLE)
        async with app.run_test(size=(120, 48)) as pilot:
            await _open(app, pilot)
            status = _text(app, "#tier-status")
            assert "В политике проекта: full" in status
            assert "Фактически карта строится в minimal" in status
            # Applying the mode the policy already has explains instead of doing nothing.
            await pilot.click("#tier-apply")
            await _settle(pilot)
            output = _text(app, "#settings-output")
            assert "В политике уже full" in output
            assert "Установите парсеры" in output

    asyncio.run(scenario())


def test_memory_with_empty_allow_paths_is_fixed_then_built(tmp_path: Path) -> None:
    repo = _git_project(tmp_path)
    (repo / "docs/adr").mkdir(parents=True)
    (repo / "docs/adr/0001-x.md").write_text("# X\n")
    path = repo / ".harness/project.json"
    document = json.loads(path.read_bytes())
    document["memory"] = {"enabled": True}
    document["memory_policy"] = {
        "source_types": ["adr"],
        "allow_paths": [],
        "redact_rules": [],
        "min_similarity": 0.0,
        "top_k": 5,
        "max_tokens": 1000,
    }
    path.write_text(json.dumps(document))
    runner = _Runner()

    async def scenario() -> None:
        app = _Host(repo, runner, NO_BUNDLE)
        async with app.run_test(size=(120, 48)) as pilot:
            await _open(app, pilot)
            assert "не читает источники" in _text(app, "#memory-status")
            await pilot.click("#memory-init")
            await pilot.pause()
            assert "allow_paths" in str(
                app.screen.query_one("#confirm-text Static", Static).content
            )
            await pilot.click("#confirm")
            await _settle(pilot)
            await _settle(pilot)
            policy = json.loads(path.read_bytes())["memory_policy"]
            assert policy["allow_paths"] == ["docs/adr/**/*.md"]
            assert runner.calls[0][2:4] == ["memory", "build"]

    asyncio.run(scenario())
