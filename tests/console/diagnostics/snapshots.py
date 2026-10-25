"""Deterministic Diagnostics artifacts; run as a module from the issue checkout."""

from __future__ import annotations

import asyncio
import json
import subprocess
import threading
from pathlib import Path
from unittest.mock import patch
from typing import cast
from textual.screen import Screen
from textual.widgets import Button

from harness.console import brand
from harness.console import data as console_data
from harness.console.app import HarnessConsoleApp
from harness.console.screens.diagnostics import DiagnosticsScreen
from harness.health.model import CheckResult, Fix, Report
from tests.console._pilot_wait import wait_until
from tests.console._svg_snapshot import BRANCH, VERSION, _SnapshotApp

ARTIFACTS = Path(__file__).parent / "artifacts" / "svg"
STATES = ("baseline", "building", "failure", "fix-first", "fix-applied", "online")
SIZES = ((160, 52), (80, 24))
LANGUAGES = ("ru", "en")
CAPTIONS = {
    "baseline": "3 · Диагностика — широкий / узкий терминал",
    "building": "Отчёт строится",
    "failure": "Сбой health",
    "fix-first": "«Исправить», шаг 1",
    "fix-applied": "«Исправить», шаг 2",
    "online": "Онлайн-запуск",
}


def fixture_report(*, online: bool = False, applied: bool = False) -> Report:
    return Report(
        1,
        "/project",
        online,
        [
            CheckResult(
                "files.skill_snapshot",
                "files",
                "fail",
                "Скиллы отличаются от установленной версии Harness",
                Fix("Посмотрите diff перед обновлением.", "harness update ."),
            ),
            CheckResult(
                "files.integrations",
                "files",
                "fail",
                "Нужно проверить настройки интеграций",
                Fix("Проверьте блок integrations в project.json."),
            ),
            CheckResult("repo_map.config", "repo_map", "ok", "Repo Map настроена"),
            CheckResult("memory.config", "memory", "ok", "Память включена"),
            CheckResult("environment.uv", "environment", "ok", "uv доступен"),
            CheckResult("hooks.installed", "hooks", "ok", "Hooks установлены"),
            CheckResult("directories.logs", "directories", "ok", "Каталог доступен"),
            CheckResult(
                "tracker.auth",
                "tracker",
                "fail" if online else "skipped",
                "Нет доступа к трекеру" if online else "Требуется онлайн-проверка",
            ),
            CheckResult(
                "orchestration.runtime",
                "orchestration",
                "skipped",
                "Runtime не проверен",
            ),
        ],
        ["Создан каталог .harness/logs"] if applied else [],
    )


class SnapshotHost(_SnapshotApp):
    """Shared theme and actual application language/help actions, without dashboard IO."""

    action_toggle_language = HarnessConsoleApp.action_toggle_language
    action_open_help = HarnessConsoleApp.action_open_help
    action_command_palette = HarnessConsoleApp.action_command_palette


async def capture(state: str, size: tuple[int, int], language: str) -> str:
    entered = threading.Event()
    release = threading.Event()

    def collect(repo: Path, *, online: bool = False) -> Report:
        if state == "building":
            entered.set()
            assert release.wait(30), "snapshot did not release health"
        if state == "failure":
            raise ValueError("diagnostic fixture failure")
        return fixture_report(online=online)

    screen = DiagnosticsScreen(
        Path("/project"),
        collect_diagnostics=collect,
        apply_local_fixes=lambda repo, *, online=False: fixture_report(
            online=online, applied=True
        ),
    )
    with (
        patch.object(console_data, "harness_version", lambda: VERSION),
        patch.object(brand, "current_branch", lambda _repo, **_kw: BRANCH),
    ):
        app = SnapshotHost(Path("/project"), lambda repo: cast(Screen[object], screen))
        try:
            async with app.run_test(size=size) as pilot:
                if state == "building":
                    assert await asyncio.to_thread(entered.wait, 5)
                else:
                    await app.workers.wait_for_complete()
                await pilot.pause()
                if language == "en":
                    await pilot.press("f2")
                if state in ("fix-first", "fix-applied"):
                    await pilot.click("#apply-fixes")
                    if state == "fix-applied":
                        await wait_until(
                            pilot,
                            lambda: not screen.query_one(
                                "#apply-fixes", Button
                            ).has_class("-active"),
                        )
                        await pilot.click("#apply-fixes")
                        await app.workers.wait_for_complete()
                if state == "online":
                    await pilot.click("#online-checks")
                    await wait_until(pilot, lambda: bool(screen.query("#attention-2")))
                    await app.workers.wait_for_complete()
                await pilot.pause()
                return app.export_screenshot(
                    title=f"diagnostics.html · {CAPTIONS[state]} · {state} · {size[0]}x{size[1]} · {language}"
                )
        finally:
            release.set()


async def generate() -> None:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    records = []
    for state in STATES:
        for size in SIZES:
            for language in LANGUAGES:
                svg = await capture(state, size, language)
                filename = f"{state}-{size[0]}x{size[1]}-{language}.svg"
                (ARTIFACTS / filename).write_text(svg, encoding="utf-8")
                records.append(
                    {
                        "file": filename,
                        "state": state,
                        "size": list(size),
                        "language": language,
                        "source": "docs/design/console-mockups/diagnostics.html",
                        "caption": CAPTIONS[state],
                        "candidate_sha": sha,
                    }
                )
    (ARTIFACTS / "manifest.json").write_text(
        json.dumps({"artifacts": records}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Generated {len(records)} SVG artifacts: {ARTIFACTS}")


if __name__ == "__main__":
    asyncio.run(generate())
