from __future__ import annotations

import asyncio
from pathlib import Path

from textual.app import App

from harness.configuration import ConfigurationStore, read_config_bundle
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
