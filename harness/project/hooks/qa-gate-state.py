#!/usr/bin/env python3
"""Keep QA evidence in the checkout whose branch is tested or published."""

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import cast


def git(checkout: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    return subprocess.run(
        ["git", "-C", str(checkout), *args],
        input=input_bytes,
        capture_output=True,
        check=True,
    ).stdout


def worktrees(project: Path) -> list[tuple[Path, str]]:
    entries: list[tuple[Path, str]] = []
    path: Path | None = None
    branch = ""
    for line in git(project, "worktree", "list", "--porcelain").decode().splitlines() + [""]:
        if line.startswith("worktree "):
            path = Path(line[9:]).resolve()
        elif line.startswith("branch refs/heads/"):
            branch = line[len("branch refs/heads/"):]
        elif not line:
            if path is not None:
                entries.append((path, branch))
            path, branch = None, ""
    return entries


def payload() -> dict[str, object]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("hook input is not a JSON object")
    return cast(dict[str, object], parsed)


def command_of(data: dict[str, object]) -> str:
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return ""
    command = tool_input.get("command")
    return command if isinstance(command, str) else ""


def head_of(command: str) -> str | None:
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    for index, word in enumerate(words):
        if word == "--head" and index + 1 < len(words):
            return words[index + 1].rsplit(":", 1)[-1]
        if word.startswith("--head="):
            return word[7:].rsplit(":", 1)[-1]
    return None


def checkout_for(project: Path, data: dict[str, object], command: str) -> Path:
    available = worktrees(project)
    head = head_of(command)
    if "--head" in command and not head:
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
        matches = [path for path, _ in available if path == requested or path in requested.parents]
        if matches:
            return max(matches, key=lambda path: len(path.parts))
    raise ValueError("command cwd is not a checkout of this project")


def state(checkout: Path) -> str:
    head = git(checkout, "rev-parse", "HEAD").decode().strip()
    diff = git(checkout, "diff", "HEAD")
    digest = git(checkout, "hash-object", "--stdin", input_bytes=diff).decode().strip()
    return f"{head}:{digest}"


def main() -> int:
    mode = sys.argv[1]
    project = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()).resolve()
    data = {} if mode == "record" else payload()
    command = command_of(data)
    if mode == "require" and not re.search(r"\bgh\s+pr\s+create\b", command):
        return 0
    checkout = checkout_for(project, data, command)
    if mode == "mark":
        config = checkout / ".harness" / "project.json"
        commands = json.loads(config.read_text(encoding="utf-8")).get("qa_gate_commands", [])
        if not commands or commands[-1] not in command:
            return 0
    marker = checkout / ".claude" / ".qa-gate" / "passed"
    current = state(checkout)
    if mode == "require":
        if not marker.is_file() or marker.read_text(encoding="utf-8").strip() != current:
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
