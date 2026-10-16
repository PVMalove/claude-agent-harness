"""Pilot tests follow the wizard, including cancellation and explicit application."""

from __future__ import annotations

import asyncio
import shutil
from typing import cast
import json
from pathlib import Path
from threading import Event

import pytest

from textual.app import App
from textual.pilot import Pilot
from textual.widgets import Button, RadioButton, RadioSet, Static, TextArea

from harness.configuration_schema import PACKAGE
from harness.console.configuration import ConfigurationDraft
from harness.console.configuration_steps import ConfigurationStep
from harness.console.advisor import Advice, AdvisorUnavailable
from harness.console.screens.configuration import ConfigurationScreen

from tests.console._pilot_wait import press, wait_until


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


def test_configuration_choice_groups_editing_and_saved_versions(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = Host(tmp_path)
        async with app.run_test(size=(120, 36)):
            editing = app.screen.query_one("#configuration-editing")
            versions = app.screen.query_one("#configuration-versions")
            assert editing.query_one("#configure-project", Button)
            assert editing.query_one("#configure-orchestration", Button)
            assert editing.query_one("#configure-both", Button)
            assert versions.query_one("#version-description")
            assert versions.query_one("#save-current-version", Button)
            assert versions.query_one("#open-versions", Button)

    asyncio.run(scenario())


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
            await wait_until(pilot, lambda: screen.query("#include-field"))
            screen.query_one("#include-field", Checkbox).value = False
            screen.query_one("#step-jump", Select).value = child
            await wait_until(
                pilot, lambda: screen.steps[screen.index].path == ("memory_policy",)
            )
            assert screen.draft is not None
            assert "memory_policy" not in screen.draft.documents["project.json"]
            assert screen.steps[screen.index].path == ("memory_policy",)
            assert not any(
                len(s.path) > 1 and s.path[0] == "memory_policy" for s in screen.steps
            )
            await press(pilot, "#review-config")
            assert screen.phase == "review"
            assert "memory_policy" not in screen.draft.documents["project.json"]
            assert path.read_bytes() == before

    asyncio.run(scenario())


def test_dashboard_refreshes_after_applied_configuration_is_closed(
    tmp_path: Path,
) -> None:
    from textual.widgets import ListView
    from harness.console.data import DashboardData
    from harness.console.screens.dashboard import DashboardScreen
    from harness.console.shell import SECTIONS

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
            menu.index = SECTIONS.index("Configuration")
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
            await pilot.press("escape")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert isinstance(app.screen, DashboardScreen)
            assert "ok=6" in str(
                app.screen.query_one("#dashboard-summary", Static).content
            )
            assert collections == [False, False]

    asyncio.run(scenario())


def _array_choices(screen: object) -> list[object]:
    from textual.widgets import Checkbox

    return [
        box
        for box in screen.query(Checkbox)  # type: ignore[attr-defined]
        if (box.id or "").startswith("array-choice-")
    ]


def _write_project(repo: Path, **extra: object) -> None:
    path = repo / ".harness/project.json"
    data = json.loads(path.read_bytes())
    data.update(extra)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_back_keeps_answers_and_missing_capability_is_explained(
    tmp_path: Path,
) -> None:
    installed_project(tmp_path)

    async def scenario() -> None:
        app = Host(tmp_path)
        async with app.run_test(size=(120, 48)) as pilot:
            await pilot.click("#configure-orchestration")
            await pilot.pause()
            error = str(app.screen.query_one("#config-error", Static).render())
            assert "backend-orchestration" in error
            await pilot.click("#configure-project")
            await pilot.pause()
            app.screen.query_one("#choice-1", RadioButton).value = True
            await pilot.click("#next-step")
            await pilot.pause()
            await pilot.click("#previous-step")
            await pilot.pause()
            assert app.screen.query_one("#field-value", TextArea).text == '"en"'

    asyncio.run(scenario())


def test_choices_beyond_four_are_paged_and_the_recommendation_is_marked(
    tmp_path: Path,
) -> None:
    installed_project(tmp_path)
    _write_project(tmp_path, memory_policy={"source_types": ["adr"]})

    async def scenario() -> None:
        app = Host(tmp_path)
        async with app.run_test(size=(120, 48)) as pilot:
            await pilot.click("#configure-project")
            await pilot.pause()
            screen = app.screen
            steps = screen.steps  # type: ignore[attr-defined]
            screen.index = next(  # type: ignore[attr-defined]
                i
                for i, s in enumerate(steps)
                if s.path == ("memory_policy", "source_types")
            )
            await screen.recompose()
            await pilot.pause()
            assert len(_array_choices(screen)) == 4
            await pilot.click("#choice-next-page")
            await pilot.pause()
            assert len(_array_choices(screen)) == 2
            screen.index = 0  # type: ignore[attr-defined]
            screen.choice_page = 0  # type: ignore[attr-defined]
            await screen.recompose()
            await pilot.pause()
            labels = [str(b.label) for b in screen.query(RadioButton)]
            assert any(label.startswith("★ ") for label in labels)

    asyncio.run(scenario())


def test_accepting_recommendation_updates_array_checkboxes_and_keeps_later_edits(
    tmp_path: Path,
) -> None:
    from textual.widgets import Checkbox

    installed_project(tmp_path)
    _write_project(tmp_path, memory_policy={"source_types": ["adr", "ledger"]})
    before = (tmp_path / ".harness/project.json").read_bytes()

    async def scenario() -> None:
        app = Host(tmp_path)
        async with app.run_test(size=(120, 60)) as pilot:
            await press(pilot, "#configure-project")
            await wait_until(pilot, lambda: bool(app.screen.query("#step-jump")))
            screen = cast(ConfigurationScreen, app.screen)
            await _open_step(
                pilot, screen, ("memory_policy", "source_types"), "#array-choice-0"
            )
            screen.query_one("#array-choice-0", Checkbox).value = False
            screen.query_one("#array-choice-1", Checkbox).value = True
            await pilot.pause()
            await press(pilot, "#use-recommendation")
            await wait_until(
                pilot,
                lambda: screen.query_one("#array-choice-0", Checkbox).value
                and not screen.query_one("#array-choice-1", Checkbox).value,
            )
            assert screen.query_one("#array-choice-0", Checkbox).value is True
            assert screen.query_one("#array-choice-1", Checkbox).value is False
            assert json.loads(screen.query_one("#field-value", TextArea).text) == [
                "adr",
                "ledger",
            ]
            await press(pilot, "#use-recommendation")
            await press(pilot, "#choice-next-page")
            # The page index changes before recompose finishes; wait for the new labels.
            await wait_until(
                pilot,
                lambda: any(
                    str(box.label).startswith("ledger —")
                    for box in screen.query("#array-choice-1").results(Checkbox)
                ),
            )
            assert screen.query_one("#array-choice-1", Checkbox).value is True
            screen.query_one("#array-choice-0", Checkbox).value = True
            await pilot.pause()
            await press(pilot, "#next-step")
            await wait_until(
                pilot,
                lambda: screen.steps[screen.index].path
                != ("memory_policy", "source_types")
                and _step_is_rendered(screen, screen.steps[screen.index].path),
            )
            await _open_step(
                pilot, screen, ("memory_policy", "source_types"), "#array-choice-0"
            )
            assert json.loads(screen.query_one("#field-value", TextArea).text) == [
                "adr",
                "ledger",
                "completion_report",
            ]
            assert screen.query_one("#array-choice-0", Checkbox).value is True
            assert (tmp_path / ".harness/project.json").read_bytes() == before

    asyncio.run(scenario())


def test_accepting_false_recommendation_clears_boolean_without_applying_config(
    tmp_path: Path,
) -> None:
    from textual.widgets import Checkbox

    installed_project(tmp_path)
    _write_project(tmp_path, memory={"enabled": False})
    before = (tmp_path / ".harness/project.json").read_bytes()

    async def scenario() -> None:
        app = Host(tmp_path)
        async with app.run_test(size=(120, 60)) as pilot:
            await press(pilot, "#configure-project")
            await wait_until(pilot, lambda: bool(app.screen.query("#step-jump")))
            screen = cast(ConfigurationScreen, app.screen)
            await _open_step(pilot, screen, ("memory", "enabled"), "#field-boolean")
            screen.query_one("#field-boolean", Checkbox).value = True
            await press(pilot, "#use-recommendation")
            await wait_until(
                pilot, lambda: not screen.query_one("#field-boolean", Checkbox).value
            )
            assert screen.query_one("#field-boolean", Checkbox).value is False
            await press(pilot, "#next-step")
            await wait_until(
                pilot,
                lambda: screen.steps[screen.index].path != ("memory", "enabled")
                and _step_is_rendered(screen, screen.steps[screen.index].path),
            )
            await _open_step(pilot, screen, ("memory", "enabled"), "#field-boolean")
            assert screen.query_one("#field-boolean", Checkbox).value is False
            assert (tmp_path / ".harness/project.json").read_bytes() == before

    asyncio.run(scenario())


def test_external_change_is_reloaded_without_losing_the_draft(tmp_path: Path) -> None:
    installed_project(tmp_path)

    async def scenario() -> None:
        app = Host(tmp_path)
        async with app.run_test(size=(120, 48)) as pilot:
            await press(pilot, "#configure-project")
            await wait_until(pilot, lambda: bool(app.screen.query("#choice-1")))
            app.screen.query_one("#choice-1", RadioButton).value = True
            await press(pilot, "#review-config")
            await wait_until(pilot, lambda: bool(app.screen.query("#apply-config")))
            _write_project(tmp_path, base_branch="release")
            await press(pilot, "#apply-config")
            await press(pilot, "#confirm")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert (
                json.loads((tmp_path / ".harness/project.json").read_bytes())[
                    "language"
                ]
                == "ru"
            )
            assert app.screen.query_one("#apply-config", Button).disabled
            await press(pilot, "#reload-external")
            await press(pilot, "#apply-config")
            await press(pilot, "#confirm")
            await app.workers.wait_for_complete()
            await pilot.pause()
            active = json.loads((tmp_path / ".harness/project.json").read_bytes())
            assert (active["language"], active["base_branch"]) == ("en", "release")

    asyncio.run(scenario())


def test_current_set_can_be_saved_as_a_version_from_the_menu(tmp_path: Path) -> None:
    from harness.configuration_store import ConfigurationStore

    before = installed_project(tmp_path)

    async def scenario() -> None:
        app = Host(tmp_path)
        async with app.run_test(size=(120, 48)) as pilot:
            await pilot.click("#save-current-version")
            await app.workers.wait_for_complete()
            await pilot.pause()

    asyncio.run(scenario())
    store = ConfigurationStore(tmp_path)
    assert len(store.versions()) == 1
    assert (tmp_path / ".harness/project.json").read_bytes() == before
    # The saved version sits inside the project's own store, under its own directory.
    assert store.root == (tmp_path / ".harness/config_versions").resolve()
    assert [p.name for p in store.root.iterdir() if p.is_dir()] == [
        store.versions()[0].id
    ]


def test_errors_link_to_steps_and_cli_status_is_shown_without_a_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness.console.screens import configuration

    installed_project(tmp_path)
    monkeypatch.setattr(
        configuration,
        "availability",
        lambda provider, cancel: "Готов к запросу"
        if provider == "codex"
        else "CLI не найден.",
    )

    async def scenario() -> None:
        app = Host(tmp_path)
        async with app.run_test(size=(120, 48)) as pilot:
            await pilot.click("#configure-project")
            await app.workers.wait_for_complete()
            await pilot.pause()
            buttons = list(
                app.screen.query_one("#advisor-provider", RadioSet).query(RadioButton)
            )
            assert not buttons[1].disabled and buttons[2].disabled
            assert "CLI не найден" in str(buttons[2].label)
            await pilot.click("#review-config")
            await pilot.pause()
            app.screen.draft.set_value("project.json", ("language",), "xx")  # type: ignore[attr-defined]
            await app.screen.recompose()
            await pilot.pause()
            assert len(app.screen.query("#problem-jump")) == 1

    asyncio.run(scenario())


def test_every_phase_keeps_its_content_in_frames(tmp_path: Path) -> None:
    from textual.widgets import Static

    installed_project(tmp_path)

    async def scenario() -> dict[str, list[str]]:
        phases: dict[str, list[str]] = {}
        app = Host(tmp_path)
        async with app.run_test(size=(120, 60)) as pilot:
            await pilot.pause()

            def titles() -> list[str]:
                return [
                    str(frame.border_title).strip()
                    for frame in app.screen.query(".frame")
                    if frame.border_title and frame.display
                ]

            phases["choose"] = titles()
            await pilot.click("#configure-project")
            await pilot.pause()
            phases["editing"] = titles()
            error = app.screen.query_one("#config-error", Static)
            assert not error.display  # an empty message takes no room
            await pilot.click("#review-config")
            await pilot.pause()
            phases["review"] = titles()
        return phases

    phases = asyncio.run(scenario())
    assert phases["choose"] == [
        "Цель",
        "Редактирование конфигурации",
        "Сохранённые версии",
    ]
    assert phases["editing"] == ["Цель", "Шаг", "Параметр", "ИИ-подсказки", "Действия"]
    assert phases["review"] == [
        "Цель",
        "Проверка",
        "Различия",
        "Итоговые файлы",
        "Версия",
        "Действия",
    ]


def test_a_message_appears_in_the_actions_frame_and_hides_again(tmp_path: Path) -> None:
    from textual.widgets import Static

    installed_project(tmp_path)

    async def scenario() -> tuple[bool, bool, bool]:
        app = Host(tmp_path)
        async with app.run_test(size=(120, 60)) as pilot:
            await pilot.pause()
            frame = app.screen.query_one("#configuration-actions")
            hidden_at_start = not frame.display
            cast(ConfigurationScreen, app.screen)._set_message("Ошибка поля")
            await pilot.pause()
            shown = frame.display
            assert (
                str(app.screen.query_one("#config-error", Static).content)
                == "Ошибка поля"
            )
            cast(ConfigurationScreen, app.screen)._set_message("")
            await pilot.pause()
            return hidden_at_start, shown, not frame.display

    assert asyncio.run(scenario()) == (True, True, True)


def test_the_confirmation_dialog_is_framed_and_cancel_changes_nothing(
    tmp_path: Path,
) -> None:
    from harness.console.screens.configuration import ConfirmConfiguration

    before = installed_project(tmp_path)
    answers: list[bool | None] = []

    class ConfirmHost(App[None]):
        def on_mount(self) -> None:
            self.push_screen(ConfirmConfiguration("Перезаписать?"), answers.append)

    async def scenario() -> str:
        app = ConfirmHost()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            title = str(app.screen.query_one("Vertical").border_title).strip()
            await pilot.click("#cancel")
            await pilot.pause()
            return title

    assert asyncio.run(scenario()) == "Подтверждение"
    assert answers == [False]
    assert (tmp_path / ".harness/project.json").read_bytes() == before


def test_a_block_is_a_section_page_with_its_fields_on_the_next_steps(
    tmp_path: Path,
) -> None:
    from textual.widgets import Checkbox

    installed_project(tmp_path)
    path = tmp_path / ".harness/project.json"
    document = json.loads(path.read_bytes())
    document["memory_policy"] = {"source_types": ["adr"], "top_k": 5}
    path.write_text(json.dumps(document))

    async def scenario() -> None:
        app = Host(tmp_path)
        async with app.run_test(size=(120, 48)) as pilot:
            await press(pilot, "#configure-project")
            await wait_until(pilot, lambda: bool(app.screen.query("#step-jump")))
            screen = app.screen
            assert isinstance(screen, ConfigurationScreen)
            await _open_step(pilot, screen, ("memory_policy",), ".section-rows")
            # No JSON editor of the whole block: its fields follow as steps of their own.
            assert not screen.query("#field-value")
            assert not screen.query("#use-recommendation")
            assert screen.query_one("#include-field", Checkbox).value is True
            rows = [
                str(s.content)
                for s in screen.query(".row-ok, .row-muted, .row-warn").results(Static)
            ]
            assert any(r.startswith("✓ top_k = 5") for r in rows)
            assert any(r.startswith("✓ source_types") for r in rows)
            await press(pilot, "#next-step")
            await wait_until(pilot, lambda: len(screen.steps[screen.index].path) == 2)
            assert screen.steps[screen.index].path[0] == "memory_policy"

    asyncio.run(scenario())


def test_a_section_added_with_the_checkbox_gets_its_field_steps(
    tmp_path: Path,
) -> None:
    from textual.widgets import Checkbox

    installed_project(tmp_path)

    async def scenario() -> None:
        app = Host(tmp_path)
        async with app.run_test(size=(120, 48)) as pilot:
            await press(pilot, "#configure-project")
            await wait_until(pilot, lambda: bool(app.screen.query("#step-jump")))
            screen = app.screen
            assert isinstance(screen, ConfigurationScreen)
            assert not any(
                s.path[:1] == ("memory_policy",) and len(s.path) > 1
                for s in screen.steps
            )
            await _open_step(pilot, screen, ("memory_policy",), ".section-rows")
            assert screen.query_one("#include-field", Checkbox).value is False
            screen.query_one("#include-field", Checkbox).value = True
            await press(pilot, "#next-step")
            await wait_until(pilot, lambda: len(screen.steps[screen.index].path) == 2)
            assert screen.draft is not None
            assert "memory_policy" in screen.draft.documents["project.json"]

    asyncio.run(scenario())


def _orchestration_project(repo: Path) -> None:
    from harness.configuration_schema import PACKAGE

    installed_project(repo)
    root = repo / ".harness/orchestration"
    shutil.copytree(PACKAGE / "orchestration/roles", root / "roles")
    (root / "orchestration.schema.json").write_bytes(
        (PACKAGE / "orchestration/orchestration.schema.json").read_bytes()
    )
    (repo / ".harness/orchestration.json").write_bytes(
        (PACKAGE / "orchestration.example.json").read_bytes()
    )


def _step_is_rendered(screen: ConfigurationScreen, path: tuple[str, ...]) -> bool:
    return any(
        str(header.content).endswith(" → " + ".".join(path))
        for header in screen.query("#configuration-step > Static").results(Static)
    )


async def _open_step(
    pilot: Pilot[None],
    screen: ConfigurationScreen,
    path: tuple[str, ...],
    ready: str,
) -> None:
    """Jump to the step at `path` and wait until its page (recognised by `ready`) is built."""
    from textual.widgets import Select

    index = next(i for i, s in enumerate(screen.steps) if s.path == path)
    screen.query_one("#step-jump", Select).value = index
    await wait_until(
        pilot,
        lambda: screen.steps[screen.index].path == path
        and _step_is_rendered(screen, path)
        and bool(screen.query(ready)),
    )


def test_entries_of_a_map_are_added_removed_and_opened_through_the_menu(
    tmp_path: Path,
) -> None:
    from textual.widgets import Input, ListView

    _orchestration_project(tmp_path)

    async def scenario() -> None:
        app = Host(tmp_path)
        async with app.run_test(size=(120, 48)) as pilot:
            await press(pilot, "#configure-orchestration")
            await app.workers.wait_for_complete()
            await wait_until(pilot, lambda: bool(app.screen.query("#step-jump")))
            screen = app.screen
            assert isinstance(screen, ConfigurationScreen) and screen.draft is not None
            draft = screen.draft
            await _open_step(pilot, screen, ("assignment_plans",), "#menu-new")
            # The menu is the default way; the JSON text has its own radio option.
            assert not screen.query("#field-value")
            assert screen.query_one("#menu-items", ListView)
            plans = draft.documents["orchestration.json"]["assignment_plans"]
            assert isinstance(plans, dict) and "reviewer" not in plans
            screen.query_one("#menu-new", Input).value = "reviewer"
            await press(pilot, "#menu-add")
            # The new entry opens at once, so its own fields are the next thing to fill.
            await wait_until(
                pilot,
                lambda: screen.steps[screen.index].path
                == ("assignment_plans", "reviewer"),
            )
            plans = draft.documents["orchestration.json"]["assignment_plans"]
            assert isinstance(plans, dict)
            assert plans["reviewer"] == plans["architect"]  # copied from the template
            await _open_step(pilot, screen, ("assignment_plans",), "#menu-new")
            screen.query_one("#menu-new", Input).value = "reviewer"
            await press(pilot, "#menu-add")
            await wait_until(
                pilot,
                lambda: "уже есть" in str(screen.query_one("#config-error").render()),
            )
            menu = screen.query_one("#menu-items", ListView)
            menu.index = [str(i.name) for i in menu.children].index("reviewer")
            await press(pilot, "#menu-remove")
            await wait_until(
                pilot,
                lambda: "reviewer"
                not in cast(
                    dict[str, object],
                    draft.documents["orchestration.json"]["assignment_plans"],
                ),
            )
            await _open_step(pilot, screen, ("assignment_plans",), "#menu-new")
            await press(pilot, "#menu-open")
            await wait_until(
                pilot,
                lambda: screen.steps[screen.index].path
                == ("assignment_plans", "architect"),
            )

    asyncio.run(scenario())


def test_the_menu_and_the_json_text_edit_the_same_list(tmp_path: Path) -> None:
    from textual.widgets import Input, ListView, TextArea

    installed_project(tmp_path)

    async def scenario() -> None:
        app = Host(tmp_path)
        async with app.run_test(size=(120, 48)) as pilot:
            await press(pilot, "#configure-project")
            await wait_until(pilot, lambda: bool(app.screen.query("#step-jump")))
            screen = app.screen
            assert isinstance(screen, ConfigurationScreen) and screen.draft is not None
            draft = screen.draft
            await _open_step(pilot, screen, ("qa_gate_commands",), "#menu-new")
            screen.query_one("#menu-new", Input).value = "make lint"
            await press(pilot, "#menu-add")
            await wait_until(
                pilot,
                lambda: draft.documents["project.json"]["qa_gate_commands"]
                == ["pytest", "make lint"]
                and any(
                    len(menu.children) == 2
                    for menu in screen.query("#menu-items").results(ListView)
                ),
            )
            assert not screen.query("Button#mode-menu, Button#mode-text")
            assert screen.query_one("#mode-menu", RadioButton).value
            screen.query_one("#configuration-mode", RadioSet).focus()
            await pilot.press("right", "space")
            await wait_until(
                pilot,
                lambda: any(
                    area.text == json.dumps(["pytest", "make lint"], indent=2)
                    for area in screen.query("#field-value").results(TextArea)
                ),
            )
            assert screen.query_one("#mode-text", RadioButton).value
            assert not screen.query_one("#mode-menu", RadioButton).value
            area = screen.query_one("#field-value", TextArea)
            assert json.loads(area.text) == ["pytest", "make lint"]
            area.load_text("{broken JSON")
            screen.query_one("#mode-menu", RadioButton).value = True
            # Invalid JSON rebuilds the text page; the previous editor is not readiness.
            await wait_until(
                pilot,
                lambda: any(
                    editor is not area and editor.text == "{broken JSON"
                    for editor in screen.query("#field-value").results(TextArea)
                ),
            )
            assert screen.text_mode
            assert screen.query_one("#mode-text", RadioButton).value
            assert not screen.query_one("#mode-menu", RadioButton).value
            area = screen.query_one("#field-value", TextArea)
            assert area.text == "{broken JSON"
            area.load_text('["pytest"]')
            screen.query_one("#mode-menu", RadioButton).value = True
            await wait_until(pilot, lambda: bool(screen.query("#menu-items")))
            assert draft.documents["project.json"]["qa_gate_commands"] == ["pytest"]
            assert len(screen.query_one("#menu-items", ListView).children) == 1
            await press(pilot, "#menu-remove")
            await wait_until(
                pilot,
                lambda: draft.documents["project.json"]["qa_gate_commands"] == []
                and not screen.query("#menu-items"),
            )

    asyncio.run(scenario())
