#!/usr/bin/env python3
"""Сохранение свидетельств QA в том checkout, чья ветка тестируется или публикуется."""

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import cast


def git(checkout: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    """Выполнить команду Git в указанном checkout и вернуть stdout в виде байтов."""
    return subprocess.run(
        ["git", "-C", str(checkout), *args],
        input=input_bytes,
        capture_output=True,
        check=True,
    ).stdout


def worktrees(project: Path) -> list[tuple[Path, str]]:
    """Получить список всех привязанных worktree репозитория и их веток."""
    entries: list[tuple[Path, str]] = []
    path: Path | None = None
    branch = ""
    for line in git(
        project, "worktree", "list", "--porcelain"
    ).decode().splitlines() + [""]:
        if line.startswith("worktree "):
            path = Path(line[9:]).resolve()
        elif line.startswith("branch refs/heads/"):
            branch = line[len("branch refs/heads/") :]
        elif not line:
            if path is not None:
                entries.append((path, branch))
            path, branch = None, ""
    return entries


def payload() -> dict[str, object]:
    """Прочитать и распарсить входную JSON-полезную нагрузку хука из sys.stdin."""
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("hook input is not a JSON object")
    return cast(dict[str, object], parsed)


def command_of(data: dict[str, object]) -> str:
    """Извлечь выполняемую команду из полезной нагрузки вызова инструмента."""
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return ""
    command = tool_input.get("command")
    return command if isinstance(command, str) else ""


PR_CREATE = re.compile(r"\bgh\s+pr\s+create\b")


def head_of(command: str) -> str | None:
    """Извлечь ветку из токена --head команды gh pr create; "" — флаг есть, ветки нет."""
    if not PR_CREATE.search(command):
        return None
    try:
        words = shlex.split(command)
    except ValueError:
        return "" if "--head" in command else None
    for index, word in enumerate(words):
        if word == "--head":
            return words[index + 1].rsplit(":", 1)[-1] if index + 1 < len(words) else ""
        if word.startswith("--head="):
            return word[7:].rsplit(":", 1)[-1]
    return None


def checkout_for(project: Path, data: dict[str, object], command: str) -> Path:
    """Определить подходящий checkout проекта на основе переданной команды и контекста."""
    available = worktrees(project)
    head = head_of(command)
    if head == "":
        raise ValueError("cannot resolve PR head branch")
    if head:
        matches = [path for path, branch in available if branch == head]
        if len(matches) != 1:
            raise ValueError(f"branch {head!r} has no unique linked checkout")
        return matches[0]
    tool_input = data.get("tool_input")
    tool_cwd = tool_input.get("cwd") if isinstance(tool_input, dict) else None
    payload_cwd = data.get("cwd")
    cwd = payload_cwd if isinstance(payload_cwd, str) else tool_cwd
    # Hooks may omit cwd: prefer the hook's own checkout, then CLAUDE_PROJECT_DIR.
    candidates = [Path(cwd)] if isinstance(cwd, str) else [Path(os.getcwd()), project]
    for candidate in candidates:
        requested = candidate.resolve()
        matches = [
            path
            for path, _ in available
            if path == requested or path in requested.parents
        ]
        if matches:
            return max(matches, key=lambda path: len(path.parts))
    raise ValueError("command cwd is not a checkout of this project")


def state(checkout: Path) -> str:
    """Вычислить текущий хэш состояния checkout (HEAD и незакоммиченный diff)."""
    head = git(checkout, "rev-parse", "HEAD").decode().strip()
    diff = git(checkout, "diff", "HEAD")
    digest = git(checkout, "hash-object", "--stdin", input_bytes=diff).decode().strip()
    return f"{head}:{digest}"


def main() -> int:
    """Точка входа хука проверки и фиксации состояния QA-gate."""
    mode = sys.argv[1]
    project = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()).resolve()
    data = {} if mode == "record" else payload()
    command = command_of(data)
    if mode == "require" and not PR_CREATE.search(command):
        return 0
    checkout = checkout_for(project, data, command)
    if mode == "mark":
        config = checkout / ".harness" / "project.json"
        commands = json.loads(config.read_text(encoding="utf-8")).get(
            "qa_gate_commands", []
        )
        if not commands or commands[-1] not in command:
            return 0
    marker = checkout / ".claude" / ".qa-gate" / "passed"
    current = state(checkout)
    if mode == "require":
        if (
            not marker.is_file()
            or marker.read_text(encoding="utf-8").strip() != current
        ):
            raise ValueError("сначала запусти skill qa-gate для checkout ветки PR")
    else:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(current + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"qa-gate: {exc}", file=sys.stderr)
        sys.exit(2)
