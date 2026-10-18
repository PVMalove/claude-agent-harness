"""Adapter updates require an explicit, fresh console preview and confirmation."""

from pathlib import Path

import pytest

from tests.console._pilot_wait import press, wait_until
from tests.core.test_uninstall import git, harness


@pytest.fixture
def project(tmp_path: Path) -> Path:
    repo = tmp_path / "project"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    result = harness("init", str(repo), "--capability", "backend-orchestration")
    assert result.returncode == 0, result.stderr
    for path in (repo / ".harness/adapters").glob("*.py"):
        path.unlink()
    return repo


def test_console_adapter_install_requires_preview_confirmation(project: Path) -> None:
    from harness.console.configuration import (
        preview_runtime_adapters,
        apply_runtime_adapters,
    )

    preview = preview_runtime_adapters(project)
    assert {item["status"] for item in preview.adapters} == {"missing"}
    assert {Path(item["path"]).name for item in preview.adapters} == {
        "claude_worker.py",
        "codex_worker.py",
    }
    assert not any((project / ".harness/adapters").glob("*.py"))
    with pytest.raises(ValueError, match="подтверж"):
        apply_runtime_adapters(project, preview)
    assert not any((project / ".harness/adapters").glob("*.py"))
    apply_runtime_adapters(project, preview, confirmed=True)
    assert {item["status"] for item in preview_runtime_adapters(project).adapters} == {
        "current"
    }


def test_console_rejects_stale_preview_and_preserves_local_adapter(
    project: Path,
) -> None:
    from harness.console.configuration import (
        preview_runtime_adapters,
        apply_runtime_adapters,
    )

    preview = preview_runtime_adapters(project)
    local = project / ".harness/adapters/claude_worker.py"
    local.write_text("project adapter\n")
    with pytest.raises(ValueError, match="План изменился"):
        apply_runtime_adapters(project, preview, confirmed=True)
    current = preview_runtime_adapters(project)
    assert current.blocked
    assert (
        next(
            item
            for item in current.adapters
            if item["path"].endswith("claude_worker.py")
        )["status"]
        == "outdated"
    )
    with pytest.raises(ValueError, match="локальные"):
        apply_runtime_adapters(project, current, confirmed=True)
    assert local.read_text() == "project adapter\n"


def test_console_does_not_enable_missing_capability(project: Path) -> None:
    import json
    from harness.console.configuration import (
        preview_runtime_adapters,
        apply_runtime_adapters,
    )

    lock = project / ".harness/harness.lock"
    payload = json.loads(lock.read_text())
    payload["capabilities"] = ["project-foundation"]
    lock.write_text(json.dumps(payload))
    before = lock.read_bytes()
    preview = preview_runtime_adapters(project)
    assert preview.blocked
    assert "--capability backend-orchestration" in preview.text
    with pytest.raises(ValueError):
        apply_runtime_adapters(project, preview, confirmed=True)
    assert lock.read_bytes() == before


def test_settings_screen_shows_status_and_requires_modal_confirmation(
    project: Path,
) -> None:
    from textual.app import App
    from textual.widgets import Static
    from harness.console.screens.configuration import (
        ConfigurationScreen,
        ConfirmConfiguration,
    )

    class SettingsApp(App[None]):
        language = "ru"

        def on_mount(self) -> None:
            self.push_screen(ConfigurationScreen(project))

    import asyncio

    async def exercise() -> None:
        app = SettingsApp()
        async with app.run_test(size=(140, 70)) as pilot:
            await pilot.pause()
            status = str(app.screen.query_one("#adapter-status", Static).content)
            assert "claude_worker" in status and "codex_worker" in status
            assert "не установлен" in status
            assert not any((project / ".harness/adapters").glob("*.py"))
            await press(pilot, "#preview-adapters")
            await wait_until(pilot, lambda: isinstance(app.screen, ConfirmConfiguration))
            assert isinstance(app.screen, ConfirmConfiguration)
            assert ".harness/adapters/claude_worker.py" in app.screen.text
            await press(pilot, "#cancel")
            await wait_until(
                pilot, lambda: not isinstance(app.screen, ConfirmConfiguration)
            )
            assert not any((project / ".harness/adapters").glob("*.py"))
            await press(pilot, "#preview-adapters")
            await wait_until(pilot, lambda: isinstance(app.screen, ConfirmConfiguration))
            await press(pilot, "#confirm")
            await wait_until(
                pilot,
                lambda: (project / ".harness/adapters/claude_worker.py").is_file()
                and (project / ".harness/adapters/codex_worker.py").is_file(),
            )
            assert (project / ".harness/adapters/claude_worker.py").is_file()
            assert (project / ".harness/adapters/codex_worker.py").is_file()

    asyncio.run(exercise())


def test_console_updates_outdated_managed_adapter_after_confirmation(
    project: Path,
) -> None:
    import hashlib
    import json
    from harness.console.configuration import (
        preview_runtime_adapters,
        apply_runtime_adapters,
    )

    path = project / ".harness/adapters/claude_worker.py"
    path.write_text("old managed adapter\n")
    lock_path = project / ".harness/harness.lock"
    lock = json.loads(lock_path.read_text())
    lock["files"][".harness/adapters/claude_worker.py"] = hashlib.sha256(
        path.read_bytes()
    ).hexdigest()
    lock_path.write_text(json.dumps(lock))
    preview = preview_runtime_adapters(project)
    assert (
        next(
            item
            for item in preview.adapters
            if item["path"].endswith("claude_worker.py")
        )["status"]
        == "outdated"
    )
    assert not preview.blocked
    apply_runtime_adapters(project, preview, confirmed=True)
    assert {item["status"] for item in preview_runtime_adapters(project).adapters} == {
        "current"
    }
