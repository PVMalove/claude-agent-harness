from __future__ import annotations

import asyncio
from pathlib import Path

from textual.app import App

from harness.configuration import read_config_bundle
from harness.configuration_store import ConfigurationStore
from harness.console.screens.configuration_versions import ConfigurationVersionsScreen
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
            await pilot.pause()
            await pilot.click("#restore-version")
            await pilot.pause()
            assert (tmp_path / ".harness/project.json").read_bytes() == after
            await pilot.click("#confirm")
            await app.workers.wait_for_complete()
            await pilot.pause()
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


def test_versions_screen_groups_its_content_in_frames_and_names_the_storage_path(
    tmp_path: Path,
) -> None:
    from textual.widgets import Static

    installed_project(tmp_path)
    ConfigurationStore(tmp_path).save(read_config_bundle(tmp_path), "Русский")

    async def scenario() -> tuple[list[str], str, str]:
        app = _host(tmp_path)
        async with app.run_test(size=(120, 48)) as pilot:
            await pilot.pause()
            screen = app.screen
            frames = [str(f.border_title).strip() for f in screen.query(".frame")]
            path = str(screen.query_one("#versions-path", Static).content)
            diff = str(screen.query_one("#version-diff", Static).content)
            return frames, path, diff

    frames, path, diff = asyncio.run(scenario())
    assert frames == ["Хранилище", "Версии", "Различия", "Действия"]
    assert str(ConfigurationStore(tmp_path).root) in path
    assert path.endswith("config_versions")
    assert diff.startswith("Изменений нет")


def test_an_empty_store_says_so_and_offers_nothing_to_restore(tmp_path: Path) -> None:
    from textual.widgets import Button, Select, Static

    installed_project(tmp_path)

    async def scenario() -> tuple[bool, bool, str]:
        app = _host(tmp_path)
        async with app.run_test(size=(120, 48)) as pilot:
            await pilot.pause()
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
            await pilot.pause()
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
