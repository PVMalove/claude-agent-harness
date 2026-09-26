"""Общие помощники сценариев clean-room: команды harness, запуск процессов и проверка hooks."""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HARNESS = [sys.executable, str(ROOT / "harness" / "bin" / "harness")]
INSTALL_GLOBAL = [sys.executable, str(ROOT / "bin" / "install-global")]


def _find_bash() -> str:
    """Путь к рабочему bash.

    На Windows голый `bash` может найти заглушку WSL в System32, которая без настроенного
    дистрибутива не работает, раньше bash из Git. Поэтому bash ищется от каталога установки Git;
    на остальных платформах достаточно `bash`.
    """
    if sys.platform != "win32":
        return "bash"
    git_exe = shutil.which("git")
    if git_exe:
        # git.exe lives at varying depths under the Git install root (cmd/, bin/, or
        # mingw64/bin/, depending on which one PATH finds first) - walk up rather than
        # assume a fixed depth, and stop at the first real bash.exe found.
        for ancestor in Path(git_exe).resolve().parents:
            for candidate in (
                ancestor / "bin" / "bash.exe",
                ancestor / "usr" / "bin" / "bash.exe",
            ):
                if candidate.is_file():
                    return str(candidate)
    return "bash"


BASH = _find_bash()


def run_ok(cmd, quiet=False, quiet_all=False):
    """Запустить команду, которая обязана пройти; при сбое завершиться её кодом выхода, как `set -e`."""
    stdout = subprocess.DEVNULL if (quiet or quiet_all) else None
    stderr = subprocess.DEVNULL if quiet_all else None
    result = subprocess.run(cmd, stdout=stdout, stderr=stderr, check=False)
    if result.returncode != 0:
        sys.exit(result.returncode)


def run_fails(cmd, quiet=False, quiet_all=False) -> bool:
    """Запустить команду, которая должна упасть; вернуть `True`, если она упала."""
    stdout = subprocess.DEVNULL if (quiet or quiet_all) else None
    stderr = subprocess.DEVNULL if quiet_all else None
    result = subprocess.run(cmd, stdout=stdout, stderr=stderr, check=False)
    return result.returncode != 0


def fail_output(cmd) -> str:
    """Запустить команду, которая обязана упасть, и вернуть её stdout и stderr вместе."""
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode == 0:
        sys.exit("command unexpectedly succeeded: " + " ".join(map(str, cmd)))
    return result.stdout + result.stderr


def capture(cmd) -> str:
    """Запустить команду, которая обязана пройти, и вернуть её stdout."""
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout


def commit_map_for(brief: dict, commit_sha: str) -> list[dict]:
    """Обязательный `commit_map` отчёта developer для кандидата из одного коммита."""
    return [
        {"commit_sha": commit_sha, "plan_entry_id": entry["id"]}
        for entry in brief.get("commit_plan", [])
    ]


def fill_agents(repo: Path):
    """Заменить незаполненные маркеры `{{...}}` в AGENTS.md проекта на `N/A`."""
    agents = repo / "AGENTS.md"
    agents.write_text(
        re.sub(r"{{[^{}\n]+}}", "N/A", agents.read_text(encoding="utf-8")),
        encoding="utf-8",
    )


def count_skill_files(skills_dir: Path) -> int:
    """Число файлов SKILL.md под каталогом скиллов."""
    return sum(1 for p in skills_dir.rglob("SKILL.md") if p.is_file())


def run_hook(
    hook: Path, project_dir: Path, command: str, *, raw_payload=None, env_overrides=None
):
    """Передать PreToolUse(Bash) hook тот же JSON, что отправляет Claude Code, с `CLAUDE_PROJECT_DIR`.

    Так поведение hook проверяется напрямую, а не выводится из файлов, созданных harness.
    """
    payload = (
        raw_payload
        if raw_payload is not None
        else json.dumps({"tool_input": {"command": command}})
    )
    env = dict(os.environ, CLAUDE_PROJECT_DIR=str(project_dir))
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [BASH, str(hook)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
        check=False,
    )
