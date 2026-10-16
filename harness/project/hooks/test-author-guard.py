#!/usr/bin/env python3
"""Scoped Claude Code guard for the optional test-author subagent.

The hook is installed in the test-author agent frontmatter, not in the project-wide settings.
It returns a soft PreToolUse denial for unsafe calls and never changes orchestration state.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import shutil
import stat
import sys
from pathlib import Path

sys.dont_write_bytecode = True

TEST_ID_PATTERN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:/\[\]-]{0,199}")
TEST_PATH_PATTERN = re.compile(
    r"^(?![/\\])(?![A-Za-z]:)(?!(?:.*[/\\])?\.\.(?:[/\\]|$)).+$"
)
TEST_ID_PLACEHOLDER = "{test_id}"
PROTECTED_TOP_LEVEL = frozenset({".git", ".harness", ".claude", ".agents", ".codex"})
GLOB_CHARACTERS = frozenset("*?[]{}\\")
TARGET_AGENT = "test-author"
AGENT_RELATIVE = Path(".claude/agents/test-author.md")
RUNNER_RELATIVE = Path(".claude/hooks/run-test.sh")
SHELL_META = frozenset(";|&<>$`\\\n\r")


class GuardRefusal(Exception):
    """A fixed, sanitized reason for refusing a test-author action."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def project_dir() -> Path:
    """Return the configured project root without trusting a tool-provided path."""
    value = os.environ.get("CLAUDE_PROJECT_DIR")
    if not value:
        value = str(Path.cwd())
    return Path(value).resolve()


def _validate_agent(repo: Path) -> None:
    """Require the installed Claude Code agent to keep its scoped hook enabled."""
    agent = repo / AGENT_RELATIVE
    try:
        if agent.is_symlink() or not agent.is_file():
            raise GuardRefusal("agent_missing")
        text = agent.read_text(encoding="utf-8").replace("\r\n", "\n")
    except GuardRefusal:
        raise
    except (OSError, UnicodeError):
        raise GuardRefusal("scoped_hook_unsupported") from None

    if not text.startswith("---\n"):
        raise GuardRefusal("scoped_hook_unsupported")
    end = text.find("\n---", 4)
    if end < 0:
        raise GuardRefusal("scoped_hook_unsupported")
    frontmatter = text[4:end].splitlines()
    required_lines = (
        "name: test-author",
        "tools: Read, Write, Edit, Bash, Grep, Glob",
        "hooks:",
        "  PreToolUse:",
        '    - matcher: "Write|Edit|Bash"',
        "      hooks:",
        "        - type: command",
        '          command: "python3 \\"${CLAUDE_PROJECT_DIR}/.claude/hooks/test-author-guard.py\\""',
    )
    if not all(line in frontmatter for line in required_lines):
        raise GuardRefusal("scoped_hook_unsupported")


def _trusted_bash_paths() -> frozenset[Path]:
    """Return canonical system Bash paths that are safe for the wrapper form."""
    paths: set[Path] = set()
    for value in ("/bin/bash", "/usr/bin/bash"):
        try:
            path = Path(value).resolve(strict=True)
            mode = path.stat()
        except (OSError, RuntimeError):
            continue
        if (
            stat.S_ISREG(mode.st_mode)
            and os.access(path, os.X_OK)
            and not mode.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            paths.add(path)
    return frozenset(paths)


def _trusted_bash(value: str) -> bool:
    """Accept only a canonical, non-writable system Bash executable."""
    if value == "bash":
        candidate_name = shutil.which("bash")
    elif Path(value).is_absolute():
        candidate_name = value
    else:
        return False
    if not candidate_name:
        return False
    try:
        candidate = Path(candidate_name).resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    return candidate in _trusted_bash_paths()


def _config(repo: Path) -> tuple[list[str], list[str]]:
    """Read and validate the two optional settings needed by the guard and runner."""
    try:
        data = json.loads((repo / ".harness/project.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        raise GuardRefusal("config_invalid") from None
    if not isinstance(data, dict):
        raise GuardRefusal("config_invalid")

    if "test_paths" not in data:
        raise GuardRefusal("test_paths_missing")
    paths = data["test_paths"]
    if (
        not isinstance(paths, list)
        or not paths
        or not all(
            isinstance(item, str) and TEST_PATH_PATTERN.fullmatch(item)
            for item in paths
        )
        or len(set(paths)) != len(paths)
    ):
        raise GuardRefusal("config_invalid")

    if "test_runner" not in data:
        raise GuardRefusal("test_runner_missing")
    runner = data["test_runner"]
    if (
        not isinstance(runner, dict)
        or not {"command"} <= set(runner)
        or set(runner) - {"command", "build", "database"}
    ):
        raise GuardRefusal("config_invalid")
    _optional_settings(runner)
    command = runner["command"]
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(item, str) and item for item in command)
        or command.count(TEST_ID_PLACEHOLDER) != 1
        or command[0] == TEST_ID_PLACEHOLDER
    ):
        raise GuardRefusal("config_invalid")
    return list(paths), list(command)


def _optional_settings(runner: dict) -> None:
    """Keep optional groups closed; values never appear in a refusal."""
    for field, keys in (
        ("build", {"command", "artifact_paths"}),
        ("database", {"engine", "test_ids", "command", "ready_command"}),
    ):
        if field not in runner:
            continue
        settings = runner[field]
        if not isinstance(settings, dict) or set(settings) != keys:
            raise GuardRefusal("config_invalid")
        for name in (
            ("command", "ready_command") if field == "database" else ("command",)
        ):
            argv = settings[name]
            if (
                not isinstance(argv, list)
                or not argv
                or not all(
                    isinstance(item, str)
                    and item
                    and "\x00" not in item
                    and "{test_id}" not in item
                    for item in argv
                )
            ):
                raise GuardRefusal("config_invalid")
        name = "artifact_paths" if field == "build" else "test_ids"
        items = settings[name]
        if (
            not isinstance(items, list)
            or not items
            or not all(
                isinstance(item, str) and item.strip() and "\x00" not in item
                for item in items
            )
            or len(set(items)) != len(items)
        ):
            raise GuardRefusal("config_invalid")
        if field == "database" and not (
            isinstance(settings["engine"], str)
            and re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", settings["engine"])
        ):
            raise GuardRefusal("config_invalid")


def _write_zones(repo: Path, paths: list[str]) -> None:
    """Check that every configured glob can be mounted by the protected runner."""
    zones: list[Path] = []
    for pattern in paths:
        if not pattern.endswith("/**"):
            raise GuardRefusal("write_zone_rejected")
        parts = pattern[:-3].split("/")
        if (
            not parts
            or any(
                part in {"", ".", ".."} or GLOB_CHARACTERS.intersection(part)
                for part in parts
            )
            or parts[0].lower() in PROTECTED_TOP_LEVEL
        ):
            raise GuardRefusal("write_zone_rejected")
        zone = repo.joinpath(*parts)
        try:
            cursor = repo
            for part in parts:
                cursor /= part
                if cursor.is_symlink():
                    raise GuardRefusal("write_zone_rejected")
            if not zone.is_dir():
                raise GuardRefusal("write_zone_rejected")
            if zone.resolve() != zone or _has_linked_file(zone):
                raise GuardRefusal("write_zone_rejected")
        except (OSError, RuntimeError, ValueError):
            raise GuardRefusal("write_zone_rejected") from None
        zones.append(zone)
    if len(set(zones)) != len(zones) or any(
        left != right and left in right.parents for left in zones for right in zones
    ):
        raise GuardRefusal("write_zone_rejected")


def _has_linked_file(zone: Path) -> bool:
    """Reject a regular file with a second hard link, matching the runner's mount checks."""
    try:
        for directory, _, files in os.walk(zone):
            for name in files:
                if os.lstat(os.path.join(directory, name)).st_nlink > 1:
                    return True
    except (OSError, ValueError):
        return True
    return False


def _activation_config(repo: Path) -> tuple[list[str], list[str]]:
    """Return settings only when the runner can mount all configured test zones."""
    _validate_agent(repo)
    paths, command = _config(repo)
    _write_zones(repo, paths)
    return paths, command


def _relative_target(repo: Path, value: object) -> str:
    """Resolve a Write/Edit target and return its project-relative POSIX name."""
    if not isinstance(value, str) or not value or "\x00" in value:
        raise GuardRefusal("write_path_invalid")
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = repo / candidate
    try:
        resolved = candidate.resolve(strict=False)
        relative = resolved.relative_to(repo).as_posix()
    except (OSError, RuntimeError, ValueError):
        raise GuardRefusal("write_path_invalid") from None
    if not relative or relative.split("/", 1)[0].lower() in PROTECTED_TOP_LEVEL:
        raise GuardRefusal("write_outside_test_paths")
    return relative


def _write_allowed(repo: Path, value: object, paths: list[str]) -> bool:
    """Allow only a normalized target matched by one configured glob."""
    relative = _relative_target(repo, value)
    normalized = [pattern.replace("\\", "/") for pattern in paths]
    return any(fnmatch.fnmatchcase(relative, pattern) for pattern in normalized)


def _valid_test_id(value: object) -> bool:
    """Keep the runner argument to one bounded test identifier."""
    return isinstance(value, str) and TEST_ID_PATTERN.fullmatch(value) is not None


def _runner_allowed(repo: Path, value: object) -> str | None:
    """Return a refusal reason unless the command is exactly the installed runner plus one id."""
    if not isinstance(value, str) or not value or SHELL_META.intersection(value):
        return "runner_only"
    try:
        words = shlex.split(value, posix=True)
    except ValueError:
        return "runner_only"
    if len(words) == 2:
        runner_word, test_id = words
    elif len(words) == 3 and _trusted_bash(words[0]):
        runner_word, test_id = words[1:]
    else:
        return "runner_only"
    if not _valid_test_id(test_id):
        return "test_id_invalid"
    runner = Path(runner_word)
    if not runner.is_absolute():
        runner = repo / runner
    expected = repo / RUNNER_RELATIVE
    try:
        if (
            runner.resolve(strict=False) != expected.resolve()
            or expected.is_symlink()
            or not expected.is_file()
            or not os.access(expected, os.X_OK)
        ):
            return "runner_only"
    except (OSError, RuntimeError):
        return "runner_only"
    return None


def _deny(reason: str) -> None:
    """Emit the Claude Code soft-deny envelope with no user-controlled values."""
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": f"test-author: {reason}",
                }
            },
            separators=(",", ":"),
        )
    )


def _validate_config(repo: Path) -> int:
    """Small internal probe used by test-author-preflight.sh."""
    try:
        _activation_config(repo)
    except GuardRefusal as refusal:
        print(
            json.dumps(
                {"status": "disabled", "reason": refusal.reason}, separators=(",", ":")
            )
        )
    else:
        print('{"status":"ok"}')
    return 0


def main() -> int:
    """Handle a Claude Code payload or the preflight-only config probe."""
    repo = project_dir()
    if sys.argv[1:] == ["--validate-config"]:
        try:
            return _validate_config(repo)
        except Exception:
            print('{"status":"disabled","reason":"config_invalid"}')
            return 0

    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError, RecursionError):
        _deny("malformed_payload")
        return 0
    if not isinstance(payload, dict):
        _deny("malformed_payload")
        return 0
    agent_type = payload.get("agent_type")
    if agent_type is not None and agent_type != TARGET_AGENT:
        return 0
    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    if not isinstance(tool, str) or not isinstance(tool_input, dict):
        _deny("malformed_payload")
        return 0

    try:
        paths, _ = _activation_config(repo)
        if tool in {"Write", "Edit"}:
            if not _write_allowed(repo, tool_input.get("file_path"), paths):
                _deny("write_outside_test_paths")
            return 0
        if tool == "Bash":
            reason = _runner_allowed(repo, tool_input.get("command"))
            if reason is not None:
                _deny(reason)
            return 0
        _deny("tool_not_allowed")
    except GuardRefusal as refusal:
        _deny(refusal.reason)
    except Exception:
        _deny("guard_unavailable")
    return 0


if __name__ == "__main__":
    sys.exit(main())
