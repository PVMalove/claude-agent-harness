"""Pilot tests follow the wizard, including cancellation and explicit application."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from threading import Event

import pytest

from textual.app import App
from textual.widgets import Button, RadioButton, RadioSet, Static, TextArea

from harness.console.configuration import PACKAGE
from harness.console.configuration import ConfigurationDraft
from harness.console.configuration_steps import ConfigurationStep
from harness.console.advisor import Advice, AdvisorUnavailable
from harness.console.screens.configuration import ConfigurationScreen


def installed_project(repo: Path) -> bytes:
    root = repo / ".harness"
    root.mkdir()
    (root / "project.schema.json").write_bytes(
        (PACKAGE / "project/project.schema.json").read_bytes()
    )
    raw = json.dumps(
        {
            "language": "ru",
            "base_branch": "main",
            "branch_pattern": "^feature/issue-[0-9]+-.+",
            "qa_gate_commands": ["pytest"],
            "tracker": {"type": "local"},
        }
    ).encode()
    (root / "project.json").write_bytes(raw)
    return raw


class Host(App[None]):
    def __init__(self, repo: Path) -> None:
        super().__init__()
        self.repo = repo

    def on_mount(self) -> None:
        self.push_screen(ConfigurationScreen(self.repo))


def test_wizard_choice_changes_only_a_draft_until_apply(tmp_path: Path) -> None:
    before = installed_project(tmp_path)

    async def scenario() -> None:
        app = Host(tmp_path)
        async with app.run_test(size=(120, 48)) as pilot:
            await pilot.click("#configure-project")
            await pilot.pause()
            app.screen.query_one("#choice-1", RadioButton).value = True
            await pilot.click("#next-step")
            await pilot.pause()
            assert (tmp_path / ".harness/project.json").read_bytes() == before
            await pilot.click("#review-config")
            await pilot.pause()
            await pilot.click("#apply-config")
            await pilot.pause()
            assert (tmp_path / ".harness/project.json").read_bytes() == before
            await pilot.click("#confirm")
            await app.workers.wait_for_complete()
            await pilot.pause()
            active = json.loads((tmp_path / ".harness/project.json").read_bytes())
            assert active["language"] == "en"

    asyncio.run(scenario())


def test_advisor_is_explicit_and_only_changes_the_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness.console.screens import configuration

    before = installed_project(tmp_path)
    requests: list[str] = []

    def fake(
        provider: str,
        draft: ConfigurationDraft,
        field: ConfigurationStep,
        cancel: Event,
    ) -> Advice:
        requests.append(provider)
        return Advice("en", "Общая команда", "Английские отчёты")

    monkeypatch.setattr(configuration, "advise", fake)

    async def scenario() -> None:
        app = Host(tmp_path)
        async with app.run_test(size=(120, 48)) as pilot:
            await pilot.click("#configure-project")
            await pilot.pause()
            assert requests == []
            app.screen.query_one("#advisor-provider", RadioSet).query(RadioButton)[
                1
            ].value = True
            app.screen.query_one("#ask-advisor", Button).scroll_visible(animate=False)
            await pilot.pause()
            await pilot.click("#ask-advisor")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert requests == ["codex"]
            assert app.screen.query_one("#field-value", TextArea).text == '"ru"'
            app.screen.query_one("#use-advisor", Button).scroll_visible(animate=False)
            await pilot.pause()
            await pilot.click("#use-advisor")
            await pilot.pause()
            assert app.screen.query_one("#field-value", TextArea).text == '"en"'
            assert (tmp_path / ".harness/project.json").read_bytes() == before

    asyncio.run(scenario())


def test_cli_failure_keeps_manual_wizard_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness.console.screens import configuration

    installed_project(tmp_path)

    def unavailable(
        provider: str,
        draft: ConfigurationDraft,
        field: ConfigurationStep,
        cancel: Event,
    ) -> Advice:
        raise AdvisorUnavailable(
            "CLI не найден. Обычные подсказки мастера остаются доступны."
        )

    monkeypatch.setattr(configuration, "advise", unavailable)

    async def scenario() -> None:
        app = Host(tmp_path)
        async with app.run_test(size=(120, 48)) as pilot:
            await pilot.click("#configure-project")
            await pilot.pause()
            app.screen.query_one("#advisor-provider", RadioSet).query(RadioButton)[
                1
            ].value = True
            app.screen.query_one("#ask-advisor", Button).scroll_visible(animate=False)
            await pilot.pause()
            await pilot.click("#ask-advisor")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert "Обычные подсказки" in str(
                app.screen.query_one("#advisor-output", Static).render()
            )
            await pilot.click("#review-config")
            await pilot.pause()
            assert not app.screen.query_one("#apply-config", Button).disabled

    asyncio.run(scenario())


def test_removing_optional_object_then_jumping_to_its_former_child_keeps_it_removed(
    tmp_path: Path,
) -> None:
    from textual.widgets import Checkbox, Select

    installed_project(tmp_path)
    path = tmp_path / ".harness/project.json"
    document = json.loads(path.read_bytes())
    document["memory_policy"] = {"source_types": ["adr"], "top_k": 5}
    before = json.dumps(document).encode()
    path.write_bytes(before)

    async def scenario() -> None:
        app = Host(tmp_path)
        async with app.run_test(size=(120, 48)) as pilot:
            await pilot.click("#configure-project")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, ConfigurationScreen)
            parent = next(
                i for i, s in enumerate(screen.steps) if s.path == ("memory_policy",)
            )
            child = next(
                i
                for i, s in enumerate(screen.steps)
                if s.path == ("memory_policy", "top_k")
            )
            screen.query_one("#step-jump", Select).value = parent
            await pilot.pause()
            screen.query_one("#include-field", Checkbox).value = False
            screen.query_one("#step-jump", Select).value = child
            await pilot.pause()
            assert screen.draft is not None
            assert "memory_policy" not in screen.draft.documents["project.json"]
            assert screen.steps[screen.index].path == ("memory_policy",)
            assert not any(
                len(s.path) > 1 and s.path[0] == "memory_policy" for s in screen.steps
            )
            await pilot.click("#review-config")
            await pilot.pause()
            assert screen.phase == "review"
            assert "memory_policy" not in screen.draft.documents["project.json"]
            assert path.read_bytes() == before

    asyncio.run(scenario())


def test_dashboard_refreshes_after_applied_configuration_is_closed(
    tmp_path: Path,
) -> None:
    from textual.widgets import ListView
    from harness.console.data import DashboardData
    from harness.console.screens.dashboard import DashboardScreen, SECTIONS

    installed_project(tmp_path)
    collections: list[bool] = []

    def collect(repo: Path, *, online: bool = False) -> DashboardData:
        collections.append(online)
        language = json.loads((repo / ".harness/project.json").read_bytes())["language"]
        return DashboardData(
            ok=5 if language == "ru" else 6,
            warn=0,
            fail=0,
            skipped=0,
            repo_map_tier="minimal",
            harness_version="test",
            drift_state="clean",
            active_batches=None,
        )

    class DashboardHost(App[None]):
        def on_mount(self) -> None:
            self.push_screen(DashboardScreen(tmp_path, collect_dashboard=collect))

    async def scenario() -> None:
        app = DashboardHost()
        async with app.run_test(size=(120, 48)) as pilot:
            menu = app.screen.query_one("#section-menu", ListView)
            menu.index = SECTIONS.index("Настройка конфигурации")
            await pilot.press("enter")
            await pilot.pause()
            await pilot.click("#configure-project")
            await pilot.pause()
            app.screen.query_one("#choice-1", RadioButton).value = True
            await pilot.click("#review-config")
            await pilot.pause()
            await pilot.click("#apply-config")
            await pilot.pause()
            await pilot.click("#confirm")
            await app.workers.wait_for_complete()
            await pilot.pause()
            # Successful apply reloads the draft; closing it returns to the dashboard.
            await pilot.click("#cancel-config")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert isinstance(app.screen, DashboardScreen)
            assert "ok=6" in str(
                app.screen.query_one("#dashboard-summary", Static).content
            )
            assert collections == [False, False]

    asyncio.run(scenario())
