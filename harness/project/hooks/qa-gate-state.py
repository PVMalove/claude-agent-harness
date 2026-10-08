#!/usr/bin/env python3
"""Сохранение свидетельств QA в том checkout, чья ветка тестируется или публикуется."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from harness.project.hooks import pr_commands
else:
    # .claude/hooks must stay prunable by uninstall: never leave __pycache__ next to it.
    sys.dont_write_bytecode = True
    try:
        import pr_commands  # sibling in the installed hooks directory (sys.path[0])
    except Exception as exc:
        # A missing or partial copy (SyntaxError, any import-time failure) must block, not
        # fail open; missing names fail later inside main(), which also exits 2.
        print(f"qa-gate: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


EVIDENCE_TIMEOUT_SECONDS = 60


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


# The flag before a shell's command string: `bash -lc '<cmd>'`, `sh -c`, `powershell -Command`.
SHELL_COMMAND_FLAG = re.compile(r"-[A-Za-z]*c|-(?i:command)")
SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh", "powershell", "pwsh"})


def wraps_qa_command(argv: list[str], qa_command: str) -> bool:
    """Получает ли оболочка из argv `qa_command` целиком строкой команды (`bash -lc '<qa>'`)."""
    for index in range(2, len(argv)):
        if argv[index] != qa_command or not SHELL_COMMAND_FLAG.fullmatch(
            argv[index - 1]
        ):
            continue
        # The shell is the nearest word before the flag that is not an option itself.
        shell = next(
            (word for word in reversed(argv[: index - 1]) if not word.startswith("-")),
            "",
        )
        if pr_commands.program(shell).lower() in SHELLS:
            return True
    return False


def runs_qa_command(command: str, qa_command: str) -> bool:
    """Выполняет ли `command` QA-команду целиком так, что успех вызова Bash означает её успех.

    QA-команда — это её simple commands подряд с теми же разделителями или одна строка команды
    оболочки (`bash -lc '<qa_command>'`, как её запускает /qa-gate). Перед ней не стоит `||`,
    после неё идут только `&&`, и она не запущена в фоне: иначе код возврата вызова не отражает
    её результат (`<qa> || true`, `<qa>; true`, `<qa> | tail`). Команда, которая лишь содержит
    текст QA-команды (`<qa_command> test_one`), и команда вне подмножества строгого лексера
    (`$(...)`, присваивание `FOO=1 <qa>`) QA-прогоном не считаются.
    """
    steps = pr_commands.strict_steps(command)
    tail = command.rstrip()
    if steps is None or (tail.endswith("&") and not tail.endswith("&&")):
        return False
    expected = pr_commands.strict_steps(qa_command) or []
    for start, (link, argv) in enumerate(steps):
        if (
            expected
            and argv == expected[0][1]
            and steps[start + 1 : start + len(expected)] == expected[1:]
        ):
            end = start + len(expected)
        elif wraps_qa_command(argv, qa_command):
            end = start + 1
        else:
            continue
        if "||" not in link and all(
            after.replace("\n", "") == "&&" for after, _ in steps[end:]
        ):
            return True
    return False


def head_of(command: str) -> str | None:
    """Ветка создаваемого PR/MR: None — ветка checkout из cwd, "" — ветку не определить."""
    if not pr_commands.CREATE_TEXT.search(command):
        return None
    heads = pr_commands.create_heads(command)
    # More than one distinct branch is ambiguous: one checkout's QA evidence cannot cover it.
    return "" if len(heads) > 1 else next(iter(heads), None)


def checkout_for(project: Path, data: dict[str, object], command: str) -> Path:
    """Определить подходящий checkout проекта на основе переданной команды и контекста."""
    available = worktrees(project)
    head = head_of(command)
    if head == "":
        raise ValueError("cannot resolve PR/MR head branch")
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


def main_worktree(project: Path) -> Path:
    """Основной worktree репозитория: Git перечисляет его первым."""
    return worktrees(project)[0][0]


def qa_gate_commands(project: Path, checkout: Path) -> list[str]:
    """Команды `qa_gate_commands` проекта; пусто, если конфигурации нет."""
    # A linked worktree lacks the gitignored .harness/: use the project root config,
    # then the main worktree's when the session itself runs in a linked worktree.
    configs = [
        path / ".harness" / "project.json"
        for path in (checkout, project, main_worktree(project))
    ]
    config = next((path for path in configs if path.is_file()), None)
    if config is None:
        return []
    commands = json.loads(config.read_text(encoding="utf-8")).get(
        "qa_gate_commands", []
    )
    return cast(list[str], commands)


def coordinator_evidence(project: Path, checkout: Path) -> bool:
    """Принял ли координатор оркестрации зелёный QA ровно для HEAD чистого checkout."""
    commands = qa_gate_commands(project, checkout)
    if not commands or git(checkout, "diff", "HEAD"):
        return False
    main_checkout = main_worktree(project)
    script = main_checkout / ".harness" / "orchestration" / "coordinator.py"
    branch = dict(worktrees(project)).get(checkout, "")
    issue = re.search(r"(?:^|/)issue-(\d+)(?:-|$)", branch)
    if issue is None or not script.is_file():
        return False
    head = git(checkout, "rev-parse", "HEAD").decode().strip()
    # The CLI checks the accept decision, the pinned candidate and the report's integrity hash.
    # It takes the ledger lock, so a busy coordinator must not hang the PR hook: give up and block.
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(script),
                "--repo",
                str(main_checkout),
                "qa",
                "evidence",
                "--ticket",
                f"#{issue.group(1)}",
                "--branch",
                branch,
                "--candidate-commit",
                head,
            ],
            cwd=main_checkout,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=EVIDENCE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    try:
        evidence = json.loads(result.stdout)
        report = evidence["qa_report"]
        passed = {
            check["command"]
            for check in report["checks_run"]
            if check["result"] == "pass"
        }
        return (
            evidence["candidate_commit"] == head
            and report["outcome"] == "completed"
            and all(command in passed for command in commands)
        )
    except (ValueError, KeyError, TypeError):
        return False


def main() -> int:
    """Точка входа хука проверки и фиксации состояния QA-gate."""
    mode = sys.argv[1]
    project = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()).resolve()
    data = {} if mode == "record" else payload()
    command = command_of(data)
    if mode == "require" and not pr_commands.CREATE_TEXT.search(command):
        return 0
    checkout = checkout_for(project, data, command)
    if mode == "mark":
        commands = qa_gate_commands(project, checkout)
        if not commands or not runs_qa_command(command, commands[-1]):
            return 0
    marker = checkout / ".claude" / ".qa-gate" / "passed"
    current = state(checkout)
    if mode == "require":
        if (
            not marker.is_file()
            or marker.read_text(encoding="utf-8").strip() != current
        ) and not coordinator_evidence(project, checkout):
            raise ValueError("сначала запусти skill qa-gate для checkout ветки PR/MR")
    else:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(current + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # any failure must block (exit 2), never fail open
        print(f"qa-gate: {exc}", file=sys.stderr)
        sys.exit(2)
