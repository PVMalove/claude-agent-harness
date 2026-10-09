"""Branch-name hooks и трекер проекта: сценарий clean-room из `scripts/test_clean_room.py`."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from scripts.clean_room.support import run_hook, run_step

BRANCH = "release/rel-7-x"
BRANCH_PATTERN = "^release/rel-[0-9]+-.+"
GITLAB_ORIGIN = "https://gitlab.example.test:8443/group/sub/project.git"
GITHUB_ORIGIN = "https://github.com/acme/widgets.git"
TRACKER_CLIS = ("gh", "glab")

# Answers modelled on real gh 2.x and glab 1.120 output. GitLab answers 404 both for a missing
# issue and for a project with issues disabled; only the project's issues_access_level tells them
# apart.
FAKE_TRACKER = """\
import json, os, sys

tool, args = sys.argv[1], sys.argv[2:]
with open(os.environ["FAKE_TRACKER_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps([tool, *args]) + "\\n")
mode = os.environ["FAKE_TRACKER_MODE"]
no_project = "   ERROR\\n  404 {message: 404 Project Not Found}.\\n"
if args[:2] == ["repo", "view"]:
    if mode == "project":
        sys.stderr.write(no_project)
        sys.exit(1)
    level = "disabled" if mode == "disabled" else "enabled"
    print(json.dumps({"issues_access_level": level}))
    sys.exit(0)
if mode == "ok":
    sys.exit(0)
if mode == "unknown":
    sys.stderr.write("unexpected tracker failure\\n")
    sys.exit(1)
if tool == "gh":
    sys.stderr.write({
        "missing": "GraphQL: Could not resolve to an issue or pull request with the number of 7. (repository.issue)",
        "disabled": "the 'acme/widgets' repository has disabled issues",
        "auth": "To get started with GitHub CLI, please run:  gh auth login",
        "project": "GraphQL: Could not resolve to a Repository with the name 'acme/widgets'. (repository)",
    }[mode] + "\\n")
    sys.exit(4 if mode == "auth" else 1)
sys.stderr.write({
    "missing": "   ERROR\\n  404 Not Found.\\n",
    "disabled": "   ERROR\\n  404 Not Found.\\n",
    "auth": "   ERROR\\n  Get https://h/api/v4/projects/g%2Fp/issues/7: 401 {message: 401 Unauthorized}.\\n",
    "project": no_project,
}[mode])
sys.exit(1)
"""


def _fake_tracker_bin(test_root: Path) -> Path:
    """Каталог с fake `gh` и `glab`, которые пишут argv в FAKE_TRACKER_LOG."""
    bin_dir = test_root / "fake-tracker-bin"
    bin_dir.mkdir()
    script = bin_dir / "fake_tracker.py"
    script.write_text(FAKE_TRACKER, encoding="utf-8")
    for tool in TRACKER_CLIS:
        if sys.platform == "win32":
            (bin_dir / f"{tool}.cmd").write_text(
                f'@"{sys.executable}" "{script}" {tool} %*\r\n', encoding="utf-8"
            )
        else:
            launcher = bin_dir / tool
            launcher.write_text(
                f'#!/bin/sh\nexec "{sys.executable}" "{script}" {tool} "$@"\n',
                encoding="utf-8",
            )
            launcher.chmod(0o755)
    return bin_dir


def _path_without_tracker_cli(test_root: Path) -> str:
    """PATH, в котором не находятся настоящие `gh`/`glab`.

    Каталог с ними заменяется теневым каталогом из симлинков на всё остальное: на Linux-раннере
    `gh` лежит в /usr/bin рядом с git и coreutils. На Windows `gh` стоит в собственном каталоге,
    и его достаточно убрать из PATH.
    """
    entries = []
    for index, entry in enumerate(os.environ.get("PATH", "").split(os.pathsep)):
        if not entry or not any(
            shutil.which(tool, path=entry) for tool in TRACKER_CLIS
        ):
            entries.append(entry)
            continue
        if sys.platform == "win32":
            continue
        shadow = test_root / "path-without-tracker-cli" / str(index)
        shadow.mkdir(parents=True)
        for item in Path(entry).iterdir():
            if item.name not in TRACKER_CLIS:
                (shadow / item.name).symlink_to(item)
        entries.append(str(shadow))
    return os.pathsep.join(entries)


def run(ctx: SimpleNamespace) -> None:
    """Branch-name hooks находят issue через резолвер трекера проекта (#442).

    Читает из контекста: `pv_project`, `test_root`.
    """
    pv_project = ctx.pv_project
    test_root = ctx.test_root
    hooks = pv_project / ".claude" / "hooks"
    project_json = pv_project / ".harness" / "project.json"
    saved_project_json = project_json.read_text(encoding="utf-8")
    log = test_root / "fake-tracker.log"
    no_cli_path = _path_without_tracker_cli(test_root)
    fake_path = str(_fake_tracker_bin(test_root)) + os.pathsep + no_cli_path

    def write_project_json(**fields: object) -> None:
        data = json.loads(saved_project_json)
        data.update(branch_pattern=BRANCH_PATTERN, **fields)
        project_json.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def set_origin(url: str) -> None:
        run_step(
            ["git", "remote", "remove", "origin"], cwd=pv_project, capture_output=True
        )
        run_step(["git", "remote", "add", "origin", url], cwd=pv_project, check=True)

    def run_branch_hooks(
        mode: str = "ok", path: str = fake_path
    ) -> list[tuple[str, subprocess.CompletedProcess[str], list[list[str]]]]:
        """Запустить оба hooks на BRANCH; для каждого вернуть имя, результат и argv вызовов CLI."""
        runs = []
        for name, payload in (
            ("check-branch-name.sh", {"command": f"git checkout -b {BRANCH}"}),
            ("check-worktree-branch-name.sh", {"name": BRANCH}),
        ):
            log.unlink(missing_ok=True)
            result = run_hook(
                hooks / name,
                pv_project,
                "",
                raw_payload=json.dumps({"tool_input": payload}),
                env_overrides={
                    "PATH": path,
                    "FAKE_TRACKER_LOG": str(log),
                    "FAKE_TRACKER_MODE": mode,
                },
            )
            calls = (
                [json.loads(line) for line in log.read_text("utf-8").splitlines()]
                if log.exists()
                else []
            )
            runs.append((name, result, calls))
        return runs

    def expect_call(expected: list[str], why: str) -> None:
        for name, result, calls in run_branch_hooks():
            if result.returncode != 0 or not calls or calls[0] != expected:
                sys.exit(
                    f"{name} did not run {expected} ({why}): rc={result.returncode}, "
                    f"calls={calls}, stderr={result.stderr}"
                )

    def expect_refusal(mode: str, marker: str) -> tuple[str, ...]:
        """stderr каждого hook (в порядке `run_branch_hooks`) для отказа с `marker`."""
        messages = []
        for name, result, _calls in run_branch_hooks(mode):
            if result.returncode != 2 or marker not in result.stderr:
                sys.exit(
                    f"{name} gave no '{marker}' message for tracker answer {mode}: "
                    f"rc={result.returncode}, stderr={result.stderr}"
                )
            messages.append(result.stderr)
        return tuple(messages)

    try:
        write_project_json()
        # A self-hosted GitLab origin with a port and subgroups is addressed as a whole URL.
        set_origin(GITLAB_ORIGIN)
        gitlab_view = ["glab", "issue", "view", "7", "-R"]
        expect_call(
            gitlab_view + ["https://gitlab.example.test:8443/group/sub/project"],
            "origin with a port and subgroups",
        )

        # Missing issue, disabled issues and missing auth each get their own message; so do an
        # inaccessible project and an unrecognised failure.
        messages = {
            mode: expect_refusal(mode, marker)
            for mode, marker in (
                ("missing", "не найдена"),
                ("disabled", "отключены"),
                ("auth", "auth login"),
                ("project", "недоступен"),
                ("unknown", "запусти команду вручную"),
            )
        }
        # Each hook on its own gives a distinct message per tracker answer.
        for position in range(2):
            if len({pair[position] for pair in messages.values()}) != len(messages):
                sys.exit(f"branch hook messages are not distinct: {messages}")

        # Without the tracker CLI the issue check is skipped, as before.
        for name, result, _calls in run_branch_hooks("missing", no_cli_path):
            if result.returncode != 0:
                sys.exit(f"{name} did not skip without glab: {result.stderr}")

        # The explicit tracker field wins over an origin on another host.
        write_project_json(
            tracker={
                "type": "gitlab",
                "host": "tracker.example.test",
                "project": "team/app",
            }
        )
        expect_call(
            gitlab_view + ["https://tracker.example.test/team/app"], "tracker field"
        )
        write_project_json()

        # GitHub is addressed with gh issue view -R host/owner/repo.
        set_origin(GITHUB_ORIGIN)
        expect_call(
            ["gh", "issue", "view", "7", "-R", "github.com/acme/widgets"], "GitHub"
        )
        expect_refusal("missing", "не найдена")
    finally:
        run_step(
            ["git", "remote", "remove", "origin"], cwd=pv_project, capture_output=True
        )
        project_json.write_text(saved_project_json, encoding="utf-8")
