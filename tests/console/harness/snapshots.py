"""Capture included harness.html states in RU/EN at both accepted viewport sizes.

Run from the candidate checkout:
  env -u NO_COLOR .harness/.venv/bin/python -m tests.console.harness.snapshots --output <artifact-root>
The output root must be outside the tracked source tree, under the main checkout's runtime logs.
Fixtures are confined to tests; no production fallback supplies these facts.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
from pathlib import Path
from threading import Event
from typing import Mapping, Sequence, cast
from unittest.mock import patch

from textual.pilot import Pilot
from textual.screen import Screen
from textual.widgets import Button, Input, ListView, Static

from harness.console import brand, data, i18n, translate
from harness.console.catalog import HARNESS_COMMANDS
from harness.console.reports import LedgerView
from harness.console.screens.harness import CommandFormScreen, HarnessScreen
from harness.console.shell import Fold
from tests.console._svg_snapshot import VERSION, SIZES, _SnapshotApp, until

LANGUAGES = ("ru", "en")
WIDE = "4 · Harness — широкий терминал (≈160×52), компактно"
NARROW = "4 · Harness — узкий терминал (≈80×24)"
FORM = "Форма подтверждения"
STATES_CAPTION = "Состояния «Вывода» и списка"
# Every implemented state has four actual captures, never a placeholder image.
STATES = (
    "baseline",
    "reversible-detail",
    "external-risk",
    "deletion-risk",
    "group-checks",
    "group-skills",
    "group-repo-map",
    "group-cleanup",
    "group-ledger",
    "filter-reversible",
    "filter-confirmation",
    "filter-empty",
    "confirmation",
    "worktree-empty",
    "worktree-valid",
    "reset-empty",
    "reset-wrong",
    "reset-valid",
    "hard",
    "hard-valid",
    "uninstall",
    "uninstall-valid",
    "parser-bundle",
    "running",
    "exit-zero",
    "exit-nonzero",
    "os-error",
    "unexpected-error",
    "unavailable-command",
    "ledger-unavailable",
    "version-mismatch",
    "not-installed",
    "drift-loading",
    "drift-clean",
    "drift-detected",
    "drift-unknown",
)
MOCKUP_STATES = {
    "running": "Команда выполняется",
    "exit-zero": "Команда завершилась",
    "exit-nonzero": "Команда упала",
    "unavailable-command": "Команда недоступна",
    "ledger-unavailable": "Ledger недоступен",
}
EXCLUDED = (
    {
        "state": "Статистика считается",
        "reason": "Расчёт размеров временных файлов вне #743",
        "follow_up": "https://github.com/PVMalove/claude-agent-harness/issues/853",
    },
    {
        "state": "Нечего очищать",
        "reason": "План и размеры временных файлов вне #743",
        "follow_up": "https://github.com/PVMalove/claude-agent-harness/issues/853",
    },
    {
        "state": "Git недоступен",
        "reason": "Панель фактов worktree вне #743",
        "follow_up": "https://github.com/PVMalove/claude-agent-harness/issues/854",
    },
    {
        "state": "Можно удалить",
        "reason": "Удаление worktree из строки с фактами вне #743",
        "follow_up": "https://github.com/PVMalove/claude-agent-harness/issues/854",
    },
    {
        "state": "Есть незакоммиченные изменения",
        "reason": "Dirty counts и защиты удаления worktree вне #743",
        "follow_up": "https://github.com/PVMalove/claude-agent-harness/issues/854",
    },
    {
        "state": "Очистка временных файлов",
        "reason": "Сводка размеров в подтверждении очистки вне #743",
        "follow_up": "https://github.com/PVMalove/claude-agent-harness/issues/853",
    },
    {
        "state": "Вывод по мере поступления",
        "reason": "capturing_runner возвращает stdout/stderr после завершения",
        "follow_up": "https://github.com/PVMalove/claude-agent-harness/issues/855",
    },
)
DIFFERENCES = (
    "Панели размеров cleanup и фактов worktree, расширенные подтверждения удаления и streaming исключены; связаны с отдельными задачами.",
    "Ячейки терминала заменяют браузерные радиусы, шрифты, отступы и плашки: рамки Textual и цветные подписи сохраняют иерархию.",
    "Счётчик команд зависит от available(repo), поэтому 20 видны только при наличии всех скриптов; в целевом проекте недоступные команды скрыты.",
    "В 80×24 версия, состав и подключение показаны компактно; скиллы, managed files и дата lock остаются в широком состоянии и tooltip, как полный текст состояния.",
    "При запуске в узком режиме описание сворачивается, чтобы список и код выхода оставались в viewport; его можно раскрыть снова.",
    "Полный stdout затем полный stderr сохраняются раздельно: исходный порядок их чередования capturing_runner не предоставляет.",
    "Фактические имя проекта, версия, ветка, lock mtime и ledger отличаются от демонстрационных значений HTML; фикстуры используются только для доказательств.",
)


class EvidenceApp(_SnapshotApp):
    def action_toggle_language(self) -> None:
        self.language = i18n.next_language(self.language)
        for screen in self.screen_stack:
            translate.retranslate(screen, self.language)
        translate.translate_bindings(self, self.language)
        self.refresh_bindings()


class Runner:
    def __init__(self, state: str) -> None:
        self.state = state
        self.calls: list[list[str]] = []
        self.started, self.release = Event(), Event()

    def __call__(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        self.started.set()
        if self.state == "running":
            if not self.release.wait(20):
                raise TimeoutError("evidence runner was not released")
        if self.state == "os-error":
            raise OSError("fixture executable unavailable")
        if self.state == "unexpected-error":
            raise ValueError("fixture unexpected failure")
        return subprocess.CompletedProcess(
            list(argv),
            7 if self.state == "exit-nonzero" else 0,
            "STDOUT-FIRST\n" + "output line\n" * 310 + "STDOUT-LAST\n",
            "STDERR-FIRST\n" + "error line\n" * 310 + "STDERR-LAST\n",
        )


def prepare_repo(repo: Path, state: str) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    for relative in (
        "scripts/verify.py",
        "scripts/build_parser_bundle.py",
        ".harness/repo_map/repo_map.py",
        ".harness/orchestration/coordinator.py",
    ):
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# test fixture only\n", encoding="utf-8")
    lock = repo / ".harness/harness.lock"
    if state == "not-installed":
        lock.unlink(missing_ok=True)
    else:
        lock.write_text(
            json.dumps(
                {
                    "package_version": "0.0.1"
                    if state == "version-mismatch"
                    else VERSION,
                    "capabilities": ["pvmalove-suite"],
                    "files": {"AGENTS.md": {}},
                }
            ),
            encoding="utf-8",
        )
        os.utime(lock, (1791969120, 1791969120))


async def reveal(
    screen: HarnessScreen, pilot: Pilot[None], key: str, *, detail: bool = True
) -> None:
    screen.select_command(key)
    menu = screen.query_one("#command-menu", ListView)
    # Group replacement runs in a worker. Wait for its selection before expanding details,
    # otherwise the arriving Highlighted message may collapse them again.
    await until(
        pilot,
        lambda: menu.highlighted_child is not None
        and menu.highlighted_child.name == key
        and screen._selected_key == key,
    )
    menu.focus()
    if detail:
        screen.query_one("#detail-fold", Fold).collapsed = False
    # Expanding the narrow detail shrinks the list, then its after-refresh callback scrolls
    # the selected row. The compositor updates its region on the following refresh.
    await until(
        pilot,
        lambda: (not detail or screen.query_one("#detail-fold", Fold).region.height > 1)
        and menu.highlighted_child is not None
        and menu.content_region.y
        <= menu.highlighted_child.region.y
        < menu.content_region.bottom,
    )
    await pilot.pause()
    selected = menu.highlighted_child
    assert selected is not None and selected.name == key
    assert menu.content_region.y <= selected.region.y < menu.content_region.bottom


async def drive(
    state: str, screen: HarnessScreen, pilot: Pilot[None], runner: Runner
) -> None:
    keys = {
        "external-risk": "health-online-fix",
        "deletion-risk": "cleanup-apply",
        "confirmation": "health-online-fix",
        "worktree-empty": "worktree-remove",
        "worktree-valid": "worktree-remove",
        "hard": "cleanup-hard-apply",
        "hard-valid": "cleanup-hard-apply",
        "uninstall": "uninstall-apply",
        "uninstall-valid": "uninstall-apply",
        "parser-bundle": "parser-bundle",
        "group-skills": "registry",
        "group-repo-map": "repo-map",
        "group-cleanup": "cleanup",
        "group-ledger": "ledger-migrate",
    }
    if state.startswith("reset-"):
        keys[state] = "ledger-reset"
    if state in keys:
        await reveal(screen, pilot, keys[state])
    elif state == "reversible-detail":
        await reveal(screen, pilot, "diff")
    if state in ("filter-reversible", "filter-confirmation", "filter-empty"):
        screen.query_one(
            "#filter-confirmation" if state == "filter-empty" else "#" + state, Button
        ).press()
        await pilot.pause()
    if state in (
        "confirmation",
        "worktree-empty",
        "worktree-valid",
        "reset-empty",
        "reset-wrong",
        "reset-valid",
        "hard",
        "hard-valid",
        "uninstall",
        "uninstall-valid",
        "parser-bundle",
    ):
        screen.open_command(keys[state])
        await pilot.pause()
        form = pilot.app.screen
        assert isinstance(form, CommandFormScreen)
        if state == "worktree-valid":
            form.query_one(
                "#input-worktree", Input
            ).value = "/workspace/issue-743-example"
        elif state in ("reset-wrong", "reset-valid"):
            form.query_one("#typed-confirmation", Input).value = (
                "reset" if state == "reset-wrong" else "RESET"
            )
        elif state in ("hard-valid", "uninstall-valid"):
            form.query_one("#typed-confirmation", Input).value = (
                "HARD" if state == "hard-valid" else "UNINSTALL"
            )
        elif state == "parser-bundle":
            form.query_one("#input-wheelhouse", Input).value = "wheels"
        await pilot.pause()
    elif state in (
        "running",
        "exit-zero",
        "exit-nonzero",
        "os-error",
        "unexpected-error",
    ):
        screen.open_command("diff")
        await until(pilot, lambda: runner.started.is_set())
        if state != "running":
            await pilot.app.workers.wait_for_complete()
            await until(
                pilot,
                lambda: "exit" in str(screen.query_one("#run-status", Static).content)
                or "fixture" in str(screen.query_one("#run-status", Static).content),
            )
    elif state == "unavailable-command":
        screen.open_command("not-an-available-command")
    await pilot.pause()


async def capture(
    state: str, size: tuple[int, int], language: str, repo: Path
) -> tuple[str, str]:
    assert state in STATES
    prepare_repo(repo, state)
    runner = Runner(state)
    drift_started, drift_release = Event(), Event()

    def load_drift(_: Path) -> str:
        drift_started.set()
        if state == "drift-loading":
            if not drift_release.wait(20):
                raise TimeoutError("evidence drift was not released")
        return {"drift-detected": "drift", "drift-unknown": "unknown"}.get(
            state, "clean"
        )

    def make_screen(path: Path) -> Screen[object]:
        catalog = (
            [entry for entry in HARNESS_COMMANDS if entry.key == "diff"]
            if state == "filter-empty"
            else HARNESS_COMMANDS
        )
        return cast(
            Screen[object],
            HarnessScreen(
                path,
                command_runner=runner,
                catalog=catalog,
                load_drift=load_drift,
                load_view=lambda _: LedgerView(
                    unavailable="fixture ledger locked"
                    if state == "ledger-unavailable"
                    else None
                ),
            ),
        )

    with (
        patch.object(data, "harness_version", lambda: VERSION),
        patch.object(
            brand,
            "current_branch",
            lambda _repo, **_kw: "feature/issue-743-harness-mockup",
        ),
    ):
        app = EvidenceApp(repo, make_screen)
        app.language = language
        async with app.run_test(size=size) as pilot:
            try:
                screen = cast(HarnessScreen, app.screen)
                await until(pilot, lambda: drift_started.is_set())
                if state != "drift-loading":
                    await app.workers.wait_for_complete()
                await drive(state, screen, pilot, runner)
                caption = (
                    FORM
                    if isinstance(app.screen, CommandFormScreen)
                    else STATES_CAPTION
                    if state in MOCKUP_STATES
                    else WIDE
                    if size[0] == 160
                    else NARROW
                )
                title = f"harness.html · {caption} · {MOCKUP_STATES.get(state, state)} · {size[0]}x{size[1]} · {language}"
                svg = app.export_screenshot(title=title)
                terminal = "\n".join(
                    strip.text for strip in app.screen._compositor.render_strips()
                )
                return svg, terminal
            finally:
                runner.release.set()
                drift_release.set()
                await app.workers.wait_for_complete()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    candidate = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    common = Path(
        subprocess.check_output(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            text=True,
        ).strip()
    ).parent
    allowed = common / ".harness/.sandboxes/logs/issue-743-harness"
    output = args.output.resolve()
    if not output.is_relative_to(allowed):
        parser.error(f"--output must be under {allowed}")
    (output / "svg").mkdir(parents=True, exist_ok=True)
    manifest: dict[str, object] = {
        "candidate_sha": candidate,
        "source": "docs/design/console-mockups/harness.html",
        "captures": [],
        "excluded": EXCLUDED,
        "differences": DIFFERENCES,
    }
    captures = []
    for state in STATES:
        for language in LANGUAGES:
            for size in SIZES:
                svg, terminal = asyncio.run(
                    capture(
                        state,
                        size,
                        language,
                        output
                        / "fixtures"
                        / f"{state}-{language}-{size[0]}x{size[1]}"
                        / "claude-agent-harness",
                    )
                )
                relative = f"svg/{state}-{language}-{size[0]}x{size[1]}.svg"
                (output / relative).write_text(svg, encoding="utf-8")
                (output / relative.replace(".svg", ".txt")).write_text(
                    terminal, encoding="utf-8"
                )
                captures.append(
                    {
                        "case": state,
                        "caption": FORM
                        if any(word in state for word in ("reset-", "worktree-"))
                        or state
                        in (
                            "confirmation",
                            "hard",
                            "hard-valid",
                            "uninstall",
                            "uninstall-valid",
                            "parser-bundle",
                        )
                        else WIDE
                        if size[0] == 160
                        else NARROW,
                        "state": MOCKUP_STATES.get(state, state),
                        "size": list(size),
                        "language": language,
                        "candidate_sha": candidate,
                        "path": relative,
                    }
                )
                print(f"captured {state} {language} {size[0]}x{size[1]}", flush=True)
    manifest["captures"] = captures
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"manifest: {output / 'manifest.json'} ({len(captures)} SVGs)")


if __name__ == "__main__":
    main()
