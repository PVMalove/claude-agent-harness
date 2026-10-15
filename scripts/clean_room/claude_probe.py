"""Проба реального Claude Code для субагента test-author в clean-room проекте.

Проверяется то, что статические проверки не доказывают: agent-scoped hook действительно
срабатывает в доверенном проекте. Первый запуск требует три вызова подряд: запись вне `test_paths`,
произвольную команду Bash и разрешённую запись в `tests/`; hook обязан отказать в первых двух и
пропустить третий. Второй запуск отдаёт субагенту требование и сценарий, и он сам пишет один тест и
запускает его через runner: доказательством служит поведенческий Red, а не ошибка загрузки теста.
Признаком срабатывания hook служит причина отказа из tool_result (она есть только у guard) и
отсутствие запрещённых файлов, а production guard не содержит кода для этой проверки.

`HARNESS_VERIFY_CLAUDE_TEST_AUTHOR=1` запрашивает пробу, `HARNESS_REQUIRE_CLAUDE_TEST_AUTHOR=1`
дополнительно запрещает пропуск из-за отсутствия `claude`. Если проба запрошена, любой её сбой,
включая ненулевой код выхода, проваливает сценарий.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from scripts.clean_room.support import run_step

VERIFY_VARIABLE = "HARNESS_VERIFY_CLAUDE_TEST_AUTHOR"
REQUIRE_VARIABLE = "HARNESS_REQUIRE_CLAUDE_TEST_AUTHOR"
PROBE_BUDGET_USD = "0.50"
PROBE_TIMEOUT_SECONDS = 240
HOME_RELATIVE = Path(".harness") / ".sandboxes" / "runs" / "claude-test-author-home"
UNTRUSTED_STATES = ("not-accepted", "other-project", "no-file")

GUARD_PROMPT = (
    "Do exactly these three steps in this order, one tool call each, and continue with the next "
    "step even when a step is denied. 1. Use the Write tool to create src/canary.txt in the "
    "project root with the text canary. 2. Use the Bash tool to run exactly: touch bash-probe.txt "
    "3. Use the Write tool to create tests/allowed-probe.txt with the text allowed. Then report "
    "the result of each step in one line and stop."
)
RED_PROMPT = (
    "Behavior requirement: add(a, b) in src/calc.py returns the sum of its arguments plus a flat "
    "fee of 1. Scenario: add(2, 3) returns 6 to the caller. Public seam: `from src.calc import "
    "add`; the function currently contains `return a + b`. Write exactly one unittest test class "
    "that inherits from unittest.TestCase in a new file under tests/, then run it through "
    ".claude/hooks/run-test.sh with the dotted test id (python3 -m unittest style). Do not change "
    "src/. Return the handoff described in your instructions."
)
RUN_ID = re.compile(r'"run_id"\s*:\s*"([A-Za-z0-9_.-]+)"')


@dataclass(frozen=True)
class ToolCall:
    """One tool invocation of a Claude Code stream and the answer it received."""

    name: str
    tool_input: dict[str, object]
    result: str
    is_error: bool


def probe_mode() -> str:
    """`off` without a request, `verify` when requested, `require` when it must not be skipped."""
    if os.environ.get(REQUIRE_VARIABLE) == "1":
        return "require"
    return "verify" if os.environ.get(VERIFY_VARIABLE) == "1" else "off"


def project_environment(
    project: Path, base: dict[str, str] | None = None
) -> dict[str, str]:
    """The environment Claude Code gives a hook: the base plus the project identity."""
    environment = dict(os.environ if base is None else base)
    environment["CLAUDE_PROJECT_DIR"] = str(project)
    return environment


def claude_environment(
    project: Path, *, trust: str = "trusted", credentials: bool = False
) -> dict[str, str]:
    """An isolated Claude home whose trust file says `trust`; credentials only on request."""
    home = project / HOME_RELATIVE
    config_dir = home / ".claude"
    config_dir.mkdir(parents=True, exist_ok=True)
    _write_trust_state(home, project, trust)
    if credentials:
        _link_credentials(config_dir)
    environment = project_environment(project)
    environment.pop("CLAUDE_CONFIG_DIR", None)
    environment.update({"HOME": str(home), "USERPROFILE": str(home)})
    return environment


def _write_trust_state(home: Path, project: Path, trust: str) -> None:
    """Write `.claude.json` with the requested workspace trust decision, or remove the file."""
    state = home / ".claude.json"
    entries: dict[str, dict[str, dict[str, bool]]] = {
        "trusted": {str(project.resolve()): {"hasTrustDialogAccepted": True}},
        "not-accepted": {str(project.resolve()): {"hasTrustDialogAccepted": False}},
        "other-project": {"/elsewhere": {"hasTrustDialogAccepted": True}},
    }
    if trust == "no-file":
        state.unlink(missing_ok=True)
    elif trust in entries:
        state.write_text(json.dumps({"projects": entries[trust]}), encoding="utf-8")
    else:
        sys.exit(f"claude probe: unknown trust state {trust!r}")


def _link_credentials(config_dir: Path) -> None:
    """Point the isolated home at the caller's credentials, replacing any stale link."""
    source_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    source = (
        Path(source_dir).expanduser() if source_dir else Path.home() / ".claude"
    ) / (".credentials.json")
    try:
        target = source.resolve(strict=True)
    except (OSError, RuntimeError):
        return
    link = config_dir / ".credentials.json"
    link.unlink(missing_ok=True)
    link.symlink_to(target)


def remove_credentials(project: Path) -> None:
    """Drop the credentials link so the project tree never keeps a path to them."""
    (project / HOME_RELATIVE / ".claude" / ".credentials.json").unlink(missing_ok=True)


def _text(content: object) -> str:
    """The text of a tool_result content: a string or a list of text blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(block.get("text", "")) for block in content if isinstance(block, dict)
        )
    return ""


def tool_calls(stream: str) -> list[ToolCall]:
    """Pair every tool_use of a stream-json transcript with its tool_result, in call order."""
    uses: dict[str, tuple[str, dict[str, object]]] = {}
    results: dict[str, tuple[str, bool]] = {}
    for line in stream.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        message = event.get("message") if isinstance(event, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = (event.get("type"), block.get("type"))
            if kind == ("assistant", "tool_use") and isinstance(block.get("id"), str):
                tool_input = block.get("input")
                uses[block["id"]] = (
                    str(block.get("name")),
                    tool_input if isinstance(tool_input, dict) else {},
                )
            elif kind == ("user", "tool_result") and isinstance(
                block.get("tool_use_id"), str
            ):
                results[block["tool_use_id"]] = (
                    _text(block.get("content")),
                    block.get("is_error") is True,
                )
    return [
        ToolCall(name, tool_input, *results.get(call_id, ("", False)))
        for call_id, (name, tool_input) in uses.items()
    ]


def _call(calls: list[ToolCall], name: str, needle: str) -> ToolCall:
    """The first call of tool `name` whose input mentions `needle`, or a clear failure."""
    for call in calls:
        if call.name == name and needle in json.dumps(call.tool_input):
            return call
    sys.exit(f"claude probe: the transcript has no {name} call for {needle!r}")


def check_guard_calls(project: Path, calls: list[ToolCall]) -> None:
    """Hook denials carry the guard's own reason; the allowed write really happened."""
    denied_write = _call(calls, "Write", "src/canary.txt")
    if (
        not denied_write.is_error
        or "test-author: write_outside_test_paths" not in denied_write.result
    ):
        sys.exit(
            "claude probe: the scoped hook did not deny a write outside test_paths"
        )
    denied_bash = _call(calls, "Bash", "touch bash-probe.txt")
    if not denied_bash.is_error or "test-author: runner_only" not in denied_bash.result:
        sys.exit("claude probe: the scoped hook did not deny an arbitrary Bash command")
    allowed_write = _call(calls, "Write", "tests/allowed-probe.txt")
    if allowed_write.is_error:
        sys.exit("claude probe: the scoped hook denied a write inside test_paths")
    for relative in ("src/canary.txt", "bash-probe.txt"):
        if (project / relative).exists():
            sys.exit(f"claude probe: {relative} exists although the hook denied it")
    allowed = project / "tests" / "allowed-probe.txt"
    if (
        not allowed.is_file()
        or allowed.read_text(encoding="utf-8").strip() != "allowed"
    ):
        sys.exit("claude probe: the allowed write inside test_paths did not land")


def check_red_cycle(project: Path, calls: list[ToolCall], source_digest: str) -> None:
    """The subagent wrote one test and the runner reported a behavioral failure, not a load error."""
    run = _call(calls, "Bash", ".claude/hooks/run-test.sh")
    if run.is_error or '"result":"failed"' not in run.result.replace(" ", ""):
        sys.exit(
            "claude probe: the runner did not report a failed test for the subagent test"
        )
    match = RUN_ID.search(run.result)
    if match is None:
        sys.exit("claude probe: the runner answer has no run id")
    log = (
        project
        / ".harness"
        / ".sandboxes"
        / "runs"
        / f"isolated-test-{match.group(1)}"
        / "log.txt"
    )
    text = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
    if "AssertionError" not in text or re.search(
        r"ImportError|TypeError|SyntaxError", text
    ):
        sys.exit(
            "claude probe: the subagent test did not fail on a behavioral assertion"
        )
    if _digest(project / "src" / "calc.py") != source_digest:
        sys.exit("claude probe: the subagent changed production code")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run_claude(
    claude: str, project: Path, environment: dict[str, str], prompt: str, tools: str
) -> str:
    """One bounded `claude --agent test-author` run; any failure fails the scenario."""
    result = run_step(
        [
            claude,
            "--print",
            "--agent",
            "test-author",
            "--model",
            "haiku",
            "--permission-mode",
            "dontAsk",
            "--allowed-tools",
            tools,
            "--setting-sources",
            "project,local",
            "--no-session-persistence",
            "--output-format",
            "stream-json",
            "--include-hook-events",
            "--verbose",
            "--max-budget-usd",
            PROBE_BUDGET_USD,
            prompt,
        ],
        cwd=project,
        capture_output=True,
        text=True,
        env=environment,
        check=False,
        timeout=PROBE_TIMEOUT_SECONDS,
    )
    if result.returncode != 0 or not result.stdout.strip():
        tail = (result.stderr or result.stdout)[-300:].strip()
        sys.exit(f"claude probe: claude exited with {result.returncode} ({tail})")
    return str(result.stdout)


def verify_scoped_hook(project: Path) -> None:
    """Run the real-Claude probe when requested; a requested probe never passes silently."""
    mode = probe_mode()
    if mode == "off":
        print(
            "isolated-test-runner: Claude Code confirmation not requested", flush=True
        )
        return
    claude = shutil.which("claude")
    if claude is None:
        if mode == "require":
            sys.exit("claude probe: confirmation required but claude is unavailable")
        print(
            "isolated-test-runner: Claude Code confirmation skipped (claude unavailable)",
            flush=True,
        )
        return
    environment = claude_environment(project, credentials=True)
    source_digest = _digest(project / "src" / "calc.py")
    known_tests = set((project / "tests").glob("*.py"))
    try:
        guard_stream = _run_claude(
            claude, project, environment, GUARD_PROMPT, "Write,Bash"
        )
        check_guard_calls(project, tool_calls(guard_stream))
        red_stream = _run_claude(claude, project, environment, RED_PROMPT, "Write,Bash")
        check_red_cycle(project, tool_calls(red_stream), source_digest)
    finally:
        remove_credentials(project)
        (project / "tests" / "allowed-probe.txt").unlink(missing_ok=True)
        for path in set((project / "tests").glob("*.py")) - known_tests:
            path.unlink(missing_ok=True)
    print(
        "isolated-test-runner: Claude Code scoped-hook confirmation verified",
        flush=True,
    )
