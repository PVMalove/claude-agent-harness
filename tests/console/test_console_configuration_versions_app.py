from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

from textual.app import App

from harness.configuration import read_config_bundle
from harness.configuration_store import ConfigurationStore
from harness.console.screens.configuration_versions import ConfigurationVersionsScreen
from tests.console._pilot_wait import press, wait_until
from tests.console.test_console_configuration_app import installed_project


def test_restore_requires_confirmation_and_retains_current_version(
    tmp_path: Path,
) -> None:
    before = installed_project(tmp_path)
    store = ConfigurationStore(tmp_path)
    saved = store.save(read_config_bundle(tmp_path), "Русский")
    after = before.replace(b'"ru"', b'"en"')
    (tmp_path / ".harness/project.json").write_bytes(after)

    class Host(App[None]):
        def on_mount(self) -> None:
            self.push_screen(ConfigurationVersionsScreen(tmp_path))

    async def scenario() -> None:
        app = Host()
        async with app.run_test(size=(120, 48)) as pilot:
            await wait_until(pilot, lambda: bool(app.screen.query("#restore-version")))
            await press(pilot, "#restore-version")
            await wait_until(pilot, lambda: bool(app.screen.query("#confirm")))
            assert (tmp_path / ".harness/project.json").read_bytes() == after
            await press(pilot, "#confirm")
            await app.workers.wait_for_complete()
            await wait_until(
                pilot,
                lambda: (tmp_path / ".harness/project.json").read_bytes() == before,
            )
            assert (tmp_path / ".harness/project.json").read_bytes() == before
            backups = [v for v in store.versions() if v.id != saved.id]
            assert len(backups) == 1
            assert store.load(backups[0].id)["project.json"] == after

    asyncio.run(scenario())


def _host(repo: Path) -> App[None]:
    class Host(App[None]):
        def on_mount(self) -> None:
            self.push_screen(ConfigurationVersionsScreen(repo))

    return Host()


def test_versions_screen_groups_its_content_in_cards_and_names_the_storage_path(
    tmp_path: Path,
) -> None:
    from textual.widgets import Static

    installed_project(tmp_path)
    ConfigurationStore(tmp_path).save(read_config_bundle(tmp_path), "Русский")

    async def scenario() -> tuple[list[str], str, str]:
        app = _host(tmp_path)
        async with app.run_test(size=(120, 48)) as pilot:
            await wait_until(
                pilot,
                lambda: bool(app.screen.query(".section-card"))
                and bool(app.screen.query("#versions-path"))
                and bool(app.screen.query("#version-diff")),
            )
            screen = app.screen
            frames = [
                str(f.border_title).strip() for f in screen.query(".section-card")
            ]
            path = str(screen.query_one("#versions-path", Static).content)
            diff = str(screen.query_one("#version-diff", Static).content)
            return frames, path, diff

    frames, path, diff = asyncio.run(scenario())
    assert frames == ["ХРАНИЛИЩЕ", "ВЕРСИИ", "РАЗЛИЧИЯ", "ДЕЙСТВИЯ"]
    assert str(ConfigurationStore(tmp_path).root) in path
    assert path.endswith("config_versions")
    assert diff.startswith("Изменений нет")


def test_an_empty_store_says_so_and_offers_nothing_to_restore(tmp_path: Path) -> None:
    from textual.widgets import Button, Select, Static

    installed_project(tmp_path)

    async def scenario() -> tuple[bool, bool, str]:
        app = _host(tmp_path)
        async with app.run_test(size=(120, 48)) as pilot:
            await wait_until(
                pilot,
                lambda: bool(app.screen.query("#restore-version"))
                and bool(app.screen.query("#configuration-version"))
                and bool(app.screen.query("#versions-empty")),
            )
            screen = app.screen
            return (
                screen.query_one("#restore-version", Button).disabled,
                screen.query_one("#configuration-version", Select).disabled,
                str(screen.query_one("#versions-empty", Static).content),
            )

    restore_disabled, select_disabled, empty = asyncio.run(scenario())
    assert restore_disabled and select_disabled
    assert "Сохранённых версий нет" in empty


def test_a_storage_path_with_brackets_is_shown_as_plain_text(tmp_path: Path) -> None:
    from textual.widgets import Static

    repo = tmp_path / "my [project] [bold]"
    repo.mkdir()
    installed_project(repo)

    async def scenario() -> str:
        app = _host(repo)
        async with app.run_test(size=(120, 48)) as pilot:
            await wait_until(pilot, lambda: bool(app.screen.query("#versions-path")))
            return str(app.screen.query_one("#versions-path", Static).content)

    shown = asyncio.run(scenario())
    assert "my [project] [bold]" in shown and shown.count("config_versions") == 1


def test_the_save_messages_name_the_storage(tmp_path: Path) -> None:
    from harness.console.screens.configuration import ConfigurationScreen

    installed_project(tmp_path)
    screen = ConfigurationScreen(tmp_path)
    message = screen._save_current("Русский")
    assert message.startswith("Текущая конфигурация сохранена как версия: ")
    assert f"Хранилище: {ConfigurationStore(tmp_path).root}" in message


def test_versions_stand_on_the_shared_frame_and_wait_for_a_running_restore(
    tmp_path: Path,
) -> None:
    from harness.console.chrome import TopBar, WorkspaceHeader
    from harness.console.shell import ConsoleShell

    installed_project(tmp_path)

    async def scenario() -> None:
        app = _host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await wait_until(pilot, lambda: bool(app.screen.query("#restore-version")))
            screen = cast(ConfigurationVersionsScreen, app.screen)
            assert isinstance(screen, ConsoleShell)
            assert screen.SECTION == "Configuration" and not screen.IS_SECTION_ROOT
            assert [type(bar) for bar in screen.query(TopBar)] == [WorkspaceHeader]
            notes: list[str] = []
            screen.notify = lambda message, **_: notes.append(message)  # type: ignore[method-assign]
            screen.busy = True
            for key in ("escape", "9"):
                await pilot.press(key)
                await pilot.pause()
                assert app.screen is screen
            await screen.navigate_crumb(screen.crumbs()[0])
            assert app.screen is screen
            assert notes == ["Дождитесь окончания отката"] * 3
            screen.busy = False

    asyncio.run(scenario())


def test_restore_says_why_it_is_off_and_goes_on_with_a_version_chosen(
    tmp_path: Path,
) -> None:
    from textual.widgets import Button, Static

    installed_project(tmp_path)

    async def scenario() -> tuple[str, str]:
        app = _host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await wait_until(pilot, lambda: bool(app.screen.query("#restore-reason")))
            note = app.screen.query_one("#restore-reason", Static)
            await wait_until(pilot, lambda: str(note.content) != "")
            empty = str(note.content)
            assert app.screen.query_one("#restore-version", Button).disabled
            return empty, str(app.screen.query_one("#restore-version", Button).tooltip)

    empty, tooltip = asyncio.run(scenario())
    assert empty == "«Восстановить версию»: сохранённых версий нет"
    assert tooltip == "сохранённых версий нет"
