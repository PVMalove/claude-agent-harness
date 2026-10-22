"""Adapter updates require an explicit, fresh console preview and confirmation."""

from pathlib import Path
from typing import cast
from textual.screen import Screen

from textual.app import App

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
            status = " ".join(
                str(line.content)
                for line in app.screen.query("#adapter-status Static").results(Static)
            )
            assert "claude_worker" in status and "codex_worker" in status
            assert "не установлен" in status
            assert not any((project / ".harness/adapters").glob("*.py"))
            await press(pilot, "#preview-adapters")
            await wait_until(
                pilot, lambda: isinstance(app.screen, ConfirmConfiguration)
            )
            assert isinstance(app.screen, ConfirmConfiguration)
            assert ".harness/adapters/claude_worker.py" in app.screen.text
            await press(pilot, "#cancel")
            await wait_until(
                pilot, lambda: not isinstance(app.screen, ConfirmConfiguration)
            )
            assert not any((project / ".harness/adapters").glob("*.py"))
            await press(pilot, "#preview-adapters")
            await wait_until(
                pilot, lambda: isinstance(app.screen, ConfirmConfiguration)
            )
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


def _fake_preview(
    adapters: list[dict[str, str | None]],
    *,
    text: str = "Обновить адаптеры runtime?\nВозможности: pvmalove-suite",
    blocked: bool = False,
    reason: str = "",
) -> object:
    from harness.console.configuration import RuntimeAdapterPreview

    return RuntimeAdapterPreview(
        adapters,  # type: ignore[arg-type]
        ["pvmalove-suite"],
        "fingerprint",
        text,
        blocked,
        reason,
    )


def _configuration_host(repo: Path) -> App[None]:
    from harness.console.screens.configuration import ConfigurationScreen

    class Host(App[None]):
        def on_mount(self) -> None:
            self.push_screen(ConfigurationScreen(repo))

    return Host()


def test_every_adapter_is_a_block_with_a_status_symbol_a_path_and_the_problem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    from textual.widgets import Static
    from harness.console.screens import configuration

    preview = _fake_preview(
        [
            {
                "path": ".harness/adapters/claude_worker.py",
                "status": "current",
                "problem": None,
            },
            {
                "path": ".harness/adapters/codex_worker.py",
                "status": "outdated",
                "problem": "locally changed adapter",
            },
            {
                "path": ".harness/adapters/other_worker.py",
                "status": "missing",
                "problem": None,
            },
        ]
    )
    monkeypatch.setattr(configuration, "preview_runtime_adapters", lambda repo: preview)

    async def scenario() -> list[str]:
        app = _configuration_host(tmp_path)
        async with app.run_test(size=(160, 52)) as pilot:
            await wait_until(pilot, lambda: bool(app.screen.query(".adapter-block")))
            blocks = list(app.screen.query(".adapter-block"))
            return [
                " | ".join(
                    str(line.content) for line in block.query(Static).results(Static)
                )
                for block in blocks
            ]

    claude, codex, other = asyncio.run(scenario())
    assert claude.startswith(
        "claude_worker | ✔ актуален | .harness/adapters/claude_worker.py"
    )
    assert "⚠ требует обновления" in codex and codex.endswith(
        "⚠ locally changed adapter"
    )
    assert other.startswith("other_worker | ○ не установлен")


def test_an_unavailable_snapshot_replaces_the_adapter_blocks_with_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    from textual.widgets import Static
    from harness.console.screens import configuration

    def failing(repo: Path) -> object:
        raise ValueError("Сначала установите харнесс проекта")

    monkeypatch.setattr(configuration, "preview_runtime_adapters", failing)

    async def scenario() -> tuple[str, int, int]:
        app = _configuration_host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await wait_until(pilot, lambda: bool(app.screen.query("#adapter-error")))
            line = str(app.screen.query_one("#adapter-error", Static).content)
            return (
                line,
                len(app.screen.query(".adapter-block")),
                len(app.screen.query("#preview-adapters")),
            )

    assert asyncio.run(scenario()) == ("✖ Сначала установите харнесс проекта", 0, 0)


def test_the_confirmation_has_a_title_a_plan_block_and_the_reason_confirm_is_off(
    tmp_path: Path,
) -> None:
    import asyncio
    from textual.app import App
    from textual.widgets import Button, Static
    from harness.console.screens.configuration import ConfirmConfiguration

    class Host(App[None]):
        def on_mount(self) -> None:
            self.push_screen(
                ConfirmConfiguration(
                    "Обновить адаптеры runtime?\n"
                    "Возможности: pvmalove-suite\n"
                    "⚠ Capability backend-orchestration не выбрана. Выберите состав явно в CLI:",
                    enabled=False,
                    reason="⚠ Capability backend-orchestration не выбрана",
                )
            )

    async def scenario() -> tuple[str, str, bool, str, str]:
        app = Host()
        async with app.run_test(size=(80, 24)) as pilot:
            await wait_until(pilot, lambda: bool(app.screen.query("#confirm-title")))
            screen = app.screen
            confirm = screen.query_one("#confirm", Button)
            return (
                str(screen.query_one("#confirm-title", Static).content),
                str(screen.query_one("#confirm-text Static", Static).content),
                confirm.disabled,
                str(screen.query_one("#confirm-reason", Static).content),
                str(confirm.tooltip),
            )

    title, plan, disabled, reason, tooltip = asyncio.run(scenario())
    assert title == "Обновить адаптеры runtime?"
    assert plan.startswith("Возможности: pvmalove-suite")
    assert disabled
    assert reason == "«Подтвердить»: ⚠ Capability backend-orchestration не выбрана"
    assert tooltip == "⚠ Capability backend-orchestration не выбрана"


def test_the_plan_names_changes_with_words_and_the_blocking_line_with_a_warning(
    project: Path,
) -> None:
    import json
    from harness.console.configuration import preview_runtime_adapters

    preview = preview_runtime_adapters(project)
    assert "✔ добавится  .harness/adapters/claude_worker.py" in preview.text or (
        "⚠ восстановится  .harness/adapters/claude_worker.py" in preview.text
    )
    assert "package_changed:" not in preview.text and "added:" not in preview.text
    assert not preview.blocked and preview.reason == ""
    lock = project / ".harness/harness.lock"
    payload = json.loads(lock.read_text())
    payload["capabilities"] = ["project-foundation"]
    lock.write_text(json.dumps(payload))
    blocked = preview_runtime_adapters(project)
    assert blocked.blocked
    assert blocked.reason == "⚠ Capability backend-orchestration не выбрана"
    assert (
        "⚠ Capability backend-orchestration не выбрана. Выберите состав" in blocked.text
    )
    assert "--capability backend-orchestration" in blocked.text


def test_a_long_adapter_path_wraps_and_stays_inside_the_screen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    from harness.console.screens import configuration
    from tests.console.test_console_layout import layout_problems

    long_path = ".harness/adapters/" + "very_long_adapter_name_" * 6 + "worker.py"
    preview = _fake_preview(
        [{"path": long_path, "status": "outdated", "problem": "p " * 40}]
    )
    monkeypatch.setattr(configuration, "preview_runtime_adapters", lambda repo: preview)

    async def scenario() -> list[str]:
        app = _configuration_host(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await wait_until(pilot, lambda: bool(app.screen.query(".adapter-block")))
            await pilot.pause()
            return layout_problems(cast(Screen[None], app.screen))

    assert asyncio.run(scenario()) == []
