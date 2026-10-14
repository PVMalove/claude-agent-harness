"""Изолированный runner одного теста: сценарий clean-room из `scripts/test_clean_room.py`.

Свежий проект pvmalove-suite без сборки и без БД получает `test_paths` и `test_runner` флагами
`init`, проходит `harness health`, а `.claude/hooks/run-test.sh` вызывается напрямую из шелла, без
Claude Code и без guard: так проверяются ограничения уровня процесса. Если probe подтверждает
песочницу, атаки теста на production, `.git` и конфигурацию runner проваливаются, а хэши защищённых
файлов не меняются; подключение к сессионной D-Bus хоста в `/run/user/<uid>` тоже проваливается.
Контрольный прогон тех же атак без runner меняет копию проекта, а D-Bus без runner доступна, если
хост её предоставляет. Без рабочей песочницы (Windows, CI без bubblewrap) проверяется только отказ
без запуска теста; с `HARNESS_REQUIRE_TEST_SANDBOX=1` (Linux-задача CI) отказ probe проваливает
сценарий. С `HARNESS_VERIFY_CLAUDE_TEST_AUTHOR=1` сценарий дополнительно запускает реальный
Claude Code для проверки agent-scoped hook; `HARNESS_REQUIRE_CLAUDE_TEST_AUTHOR=1` превращает
отсутствие этого доказательства в ошибку.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shlex
import shutil
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

from scripts.clean_room.support import (
    BASH,
    HARNESS,
    fill_agents,
    run_health,
    run_ok,
    run_step,
)

TEST_MODULE = "tests.test_runner_cases"
BENIGN = f"{TEST_MODULE}.Benign.test_add"
LEGIT = f"{TEST_MODULE}.Legit.test_writes"
MARKER = f"{TEST_MODULE}.Marker.test_marker"
RED_TEST = "tests.test_author_red.TestAuthorRed.test_behavior"
# A read-only bind does not stop connect(): through the host session bus a test could start a
# process outside the sandbox (systemd user manager), so the sandbox must hide it.
SESSION_BUS = f"{TEST_MODULE}.HostSockets.test_session_bus"
# Attack test -> the protected file it tries to change.
ATTACKS = {
    "test_direct": "src/calc.py",
    "test_child_sh": "src/calc.py",
    "test_child_python": "src/calc.py",
    "test_symlink": "src/calc.py",
    "test_git_config": ".git/config",
    "test_project_json": ".harness/project.json",
    "test_runner_hook": ".claude/hooks/run-test.sh",
    "test_runner_module": ".harness/health/isolated_runner.py",
}
INJECTED_IDS = (
    f"{BENIGN}; touch pwned",
    "-k x",
    "$(touch pwned)",
    "`touch pwned`",
)
RUNNER_COMMAND = ["python3", "-m", "unittest", "{test_id}"]
# Resolved once: a run with a PATH that has no bwrap must still find bash itself.
SHELL = shutil.which(BASH) or BASH

TEST_CASES = """\
import os
import subprocess
import sys
import unittest


def append(path):
    with open(path, "a") as handle:
        handle.write("pwned\\n")


class Benign(unittest.TestCase):
    def test_add(self):
        from src.calc import add

        self.assertEqual(add(2, 3), 5)


class Attacks(unittest.TestCase):
    def test_direct(self):
        append("src/calc.py")

    def test_child_sh(self):
        subprocess.run(["sh", "-c", "echo pwned >> src/calc.py"], check=True)

    def test_child_python(self):
        code = "open('src/calc.py', 'a').write('pwned')"
        subprocess.run([sys.executable, "-c", code], check=True)

    def test_symlink(self):
        if not os.path.islink("tests/link"):
            os.symlink("../src", "tests/link")
        append("tests/link/calc.py")

    def test_git_config(self):
        append(".git/config")

    def test_project_json(self):
        append(".harness/project.json")

    def test_runner_hook(self):
        append(".claude/hooks/run-test.sh")

    def test_runner_module(self):
        append(".harness/health/isolated_runner.py")


class HostSockets(unittest.TestCase):
    def test_session_bus(self):
        import socket

        with socket.socket(socket.AF_UNIX) as sock:
            sock.connect("/run/user/%d/bus" % os.getuid())


class Legit(unittest.TestCase):
    def test_writes(self):
        for directory in ("/tmp", os.environ["XDG_CACHE_HOME"],
                          os.environ["HARNESS_TEST_REPORT_DIR"], "tests"):
            with open(os.path.join(directory, "legit.txt"), "w") as handle:
                handle.write("ok")
        with open("/tmp/legit.txt") as handle:
            self.assertEqual(handle.read(), "ok")


class Marker(unittest.TestCase):
    def test_marker(self):
        with open("tests/marker.txt", "w") as handle:
            handle.write("ran")
"""

RED_TEST_SOURCE = """\
from src.calc import add


class TestAuthorRed:
    def test_behavior(self):
        # Requirement: add two prices; scenario: the sum is returned to the caller.
        assert add(2, 3) == 6
"""


def run(ctx: SimpleNamespace) -> None:
    """Изолированный runner на установленном проекте.

    Читает из контекста: `test_root`.
    """
    project = _install(ctx.test_root / "runner_project")
    probe = _hook(project, "--probe")
    if probe.get("status") == "ok":
        _verify_isolation(project, ctx.test_root)
        print("isolated-test-runner: process isolation verified", flush=True)
    elif os.environ.get("HARNESS_REQUIRE_TEST_SANDBOX") == "1":
        sys.exit(
            "isolated-test-runner: HARNESS_REQUIRE_TEST_SANDBOX=1, but the probe refused "
            f"({probe.get('detail')})"
        )
    else:
        detail = _verify_refusal(project, probe)
        _verify_test_author_disabled(project, detail)
        print(
            f"isolated-test-runner: isolation unavailable ({detail}); "
            "verified fail-closed refusal only",
            flush=True,
        )


def _install(project: Path) -> Path:
    """A fresh pvmalove-suite project with src/, unittest tests and the runner settings."""
    project.mkdir(parents=True)
    run_step(["git", "init", "-q"], cwd=project, check=True)
    run_ok(
        HARNESS
        + [
            "init",
            str(project),
            "--capability",
            "pvmalove-suite",
            "--base-branch",
            "main",
            "--language",
            "ru",
            "--pr-base-branch",
            "main",
            "--branch-pattern",
            "^feature/issue-[0-9]+-.+",
            "--qa-gate-command",
            "python3 -m unittest",
            "--test-path",
            "tests/**",
            "--test-runner-command",
            shlex.join(RUNNER_COMMAND),
        ],
        quiet=True,
    )
    config = json.loads(
        (project / ".harness" / "project.json").read_text(encoding="utf-8")
    )
    if config.get("test_runner") != {"command": RUNNER_COMMAND}:
        sys.exit("pvmalove-suite init did not write the --test-* flags to project.json")
    fill_agents(project)
    for relative, text in (
        ("src/__init__.py", ""),
        ("src/calc.py", "def add(a, b):\n    return a + b\n"),
        ("tests/__init__.py", ""),
        ("tests/test_runner_cases.py", TEST_CASES),
    ):
        path = project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    run_health(project)
    if not (project / ".claude" / "hooks" / "run-test.sh").is_file():
        sys.exit("pvmalove-suite init did not scaffold run-test.sh")
    if not (project / ".harness" / "health" / "isolated_runner.py").is_file():
        sys.exit("pvmalove-suite init did not ship .harness/health/isolated_runner.py")
    for relative in (
        ".claude/agents/test-author.md",
        ".claude/hooks/test-author-guard.py",
        ".claude/hooks/test-author-preflight.sh",
        "docs/agents/test-author.md",
        ".harness/skills/tdd/SKILL.md",
    ):
        if not (project / relative).is_file():
            sys.exit(f"pvmalove-suite init did not ship {relative}")
    if "| `tdd` | `.harness/skills/tdd` |" not in (
        project / ".harness" / "skills" / "REGISTRY.md"
    ).read_text(encoding="utf-8"):
        sys.exit("pvmalove-suite init did not register first-party tdd")
    return project


def _hook(
    project: Path, *arguments: str, env: dict[str, str] | None = None
) -> dict[str, object]:
    """Run the installed run-test.sh directly and parse its one bounded output line."""
    result = run_step(
        [SHELL, str(project / ".claude" / "hooks" / "run-test.sh"), *arguments],
        cwd=project,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    lines = result.stdout.splitlines()
    if len(lines) != 1 or len(lines[0].encode("utf-8")) > 512 or result.stderr:
        sys.exit(f"run-test.sh {arguments!r} did not print one bounded line")
    try:
        data = json.loads(lines[0])
    except ValueError:
        sys.exit(f"run-test.sh {arguments!r} printed no JSON")
    if not isinstance(data, dict):
        sys.exit(f"run-test.sh {arguments!r} printed no JSON object")
    refused = data.get("result") == "refused" or data.get("status") == "unavailable"
    if refused and result.returncode != 3:
        sys.exit(
            f"run-test.sh {arguments!r} refused with exit code {result.returncode}"
        )
    data["exit_status"] = result.returncode
    return data


def _expect(data: dict[str, object], label: str, **fields: object) -> None:
    """Fail the run when a runner answer differs in any given field."""
    actual = {name: data.get(name) for name in fields}
    if actual != fields:
        sys.exit(f"isolated-test-runner {label}: expected {fields}, got {actual}")


def _snapshot(project: Path) -> dict[str, str]:
    """sha256 of every file outside tests/ and .harness/.sandboxes/, .git/ included."""
    digests: dict[str, str] = {}
    for path in sorted(project.rglob("*")):
        relative = path.relative_to(project).as_posix()
        if relative.startswith(("tests/", ".harness/.sandboxes/")):
            continue
        if path.is_file() and not path.is_symlink():
            digests[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digests


def _run_dir(project: Path, data: dict[str, object]) -> Path:
    """The local artifact directory of one run."""
    return (
        project / ".harness" / ".sandboxes" / "runs" / f"isolated-test-{data['run_id']}"
    )


def _verify_isolation(project: Path, test_root: Path) -> None:
    """The working-sandbox branch: protection, legitimate writes, refusals and the control run."""
    benign = _hook(project, BENIGN)
    _expect(benign, "benign test", result="passed", exit_status=0)
    if (
        not (_run_dir(project, benign) / "cache" / "pycache").is_dir()
        or (project / "src" / "__pycache__").exists()
    ):
        sys.exit("isolated-test-runner: bytecode was not redirected into the run cache")

    before = _snapshot(project)
    for name, target in ATTACKS.items():
        answer = _hook(project, f"{TEST_MODULE}.Attacks.{name}")
        _expect(answer, name, result="failed", reason="test_failed", exit_status=1)
        if _snapshot(project) != before:
            sys.exit(f"isolated-test-runner {name}: {target} changed under the runner")
    _expect(
        _hook(project, SESSION_BUS),
        "session bus",
        result="failed",
        reason="test_failed",
        exit_status=1,
    )

    legit = _hook(project, LEGIT)
    _expect(legit, "legitimate writes", result="passed", reason="test_passed")
    run_dir = _run_dir(project, legit)
    for written in (
        run_dir / "cache" / "legit.txt",
        run_dir / "reports" / "legit.txt",
        project / "tests" / "legit.txt",
    ):
        if not written.is_file():
            sys.exit(f"isolated-test-runner: legitimate write missing: {written.name}")
    if not (run_dir / "log.txt").is_file():
        sys.exit("isolated-test-runner: the run kept no local log")

    runs = project / ".harness" / ".sandboxes" / "runs"
    runs_before = set(runs.iterdir())
    for test_id in INJECTED_IDS:
        _expect(
            _hook(project, test_id), test_id, result="refused", reason="test_id_invalid"
        )
    _expect(_hook(project, BENIGN, "extra"), "extra argument", reason="test_id_invalid")
    if (project / "pwned").exists() or set(runs.iterdir()) != runs_before:
        sys.exit("isolated-test-runner: a refused id left a side effect")

    _verify_zone_refusals(project)
    _verify_missing_bwrap(project, test_root)
    if _snapshot(project) != before:
        sys.exit("isolated-test-runner: a protected file changed")
    _verify_control_run(project, test_root)
    _verify_test_author(project)


def _preflight(project: Path) -> dict[str, object]:
    """Run the installed activation check and enforce its bounded JSON output."""
    result = run_step(
        [SHELL, str(project / ".claude" / "hooks" / "test-author-preflight.sh")],
        cwd=project,
        capture_output=True,
        text=True,
        env=dict(os.environ, CLAUDE_PROJECT_DIR=str(project)),
        check=False,
    )
    if result.returncode != 0 or result.stderr or len(result.stdout.splitlines()) != 1:
        sys.exit("test-author preflight did not return one clean JSON line")
    try:
        answer = json.loads(result.stdout)
    except ValueError:
        sys.exit("test-author preflight returned invalid JSON")
    if not isinstance(answer, dict):
        sys.exit("test-author preflight returned no JSON object")
    return answer


def _guard(project: Path, payload: dict[str, object]) -> dict[str, object] | None:
    """Call the installed scoped guard as a Claude Code PreToolUse hook."""
    result = run_step(
        [sys.executable, str(project / ".claude" / "hooks" / "test-author-guard.py")],
        cwd=project,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=dict(os.environ, CLAUDE_PROJECT_DIR=str(project)),
        check=False,
    )
    if result.returncode != 0 or result.stderr:
        sys.exit("test-author guard did not return a clean soft decision")
    if not result.stdout:
        return None
    try:
        answer = json.loads(result.stdout)
    except ValueError:
        sys.exit("test-author guard returned invalid JSON")
    if not isinstance(answer, dict):
        sys.exit("test-author guard returned no JSON object")
    return answer


def _test_author_payload(
    tool: str, tool_input: dict[str, object], agent: str = "test-author"
) -> dict[str, object]:
    """Build one installed-project hook payload."""
    return {
        "agent_type": agent,
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": tool_input,
    }


def _assert_denied(answer: dict[str, object] | None) -> None:
    """Require the fixed Claude Code deny envelope."""
    if answer is None:
        sys.exit("test-author guard allowed a protected action")
    output = answer.get("hookSpecificOutput")
    if not isinstance(output, dict) or output.get("permissionDecision") != "deny":
        sys.exit("test-author guard returned no deny decision")
    if not isinstance(output.get("permissionDecisionReason"), str):
        sys.exit("test-author guard returned no sanitized reason")


def _verify_test_author(project: Path) -> None:
    """Verify activation, scoped soft denials and the ordinary route for other agents."""
    if _preflight(project) != {"status": "ok"}:
        sys.exit("test-author preflight did not activate in the protected project")
    _verify_claude_scoped_hook(project)
    red_path = "tests/test_author_red.py"
    if _guard(project, _test_author_payload("Write", {"file_path": red_path})) is not None:
        sys.exit("test-author guard denied a configured test-path write")
    (project / red_path).write_text(RED_TEST_SOURCE, encoding="utf-8")
    red = _hook(project, RED_TEST)
    _expect(
        red,
        "test-author Red evidence",
        test_id=RED_TEST,
        result="failed",
        reason="test_failed",
        exit_status=1,
    )
    if not isinstance(red.get("run_id"), str) or not red["run_id"]:
        sys.exit("test-author Red evidence did not include a sanitized run id")
    _assert_denied(
        _guard(project, _test_author_payload("Write", {"file_path": "src/calc.py"}))
    )
    _assert_denied(
        _guard(
            project,
            _test_author_payload(
                "Bash",
                {
                    "command": (
                        "python3 -m unittest "
                        "tests.test_runner_cases.Benign.test_add"
                    )
                },
            ),
        )
    )
    if _guard(
        project,
        _test_author_payload(
            "Write", {"file_path": "src/calc.py"}, agent="developer"
        ),
    ) is not None:
        sys.exit("test-author guard changed the ordinary developer route")
    config = project / ".harness" / "project.json"
    data = json.loads(config.read_text(encoding="utf-8"))
    original = json.dumps(data, ensure_ascii=False)
    data.pop("test_paths", None)
    config.write_text(json.dumps(data), encoding="utf-8")
    try:
        expected = {"status": "disabled", "reason": "test_paths_missing"}
        if _preflight(project) != expected:
            sys.exit("test-author preflight did not disable a project without test_paths")
    finally:
        config.write_text(original, encoding="utf-8")


def _verify_claude_scoped_hook(project: Path) -> None:
    """Use the installed Claude Code CLI to prove the agent-scoped hook denies a write."""
    required = os.environ.get("HARNESS_REQUIRE_CLAUDE_TEST_AUTHOR") == "1"
    if os.environ.get("HARNESS_VERIFY_CLAUDE_TEST_AUTHOR") != "1" and not required:
        print(
            "isolated-test-runner: Claude Code confirmation not requested",
            flush=True,
        )
        return
    claude = shutil.which("claude")
    if claude is None:
        if required:
            sys.exit("test-author Claude Code confirmation required but claude is unavailable")
        print(
            "isolated-test-runner: Claude Code confirmation skipped (claude unavailable)",
            flush=True,
        )
        return

    evidence_dir = project / ".harness" / ".sandboxes" / "runs" / "claude-test-author-probe"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    nonce = secrets.token_hex(12)
    nonce_file = evidence_dir / "nonce"
    evidence_file = evidence_dir / "hook-evidence.json"
    nonce_file.write_text(nonce, encoding="utf-8")
    prompt = (
        "Use the Write tool to create src/canary.txt in the project root with the text canary. "
        "Do not use Edit or Bash. If Write is denied, report the denial and stop."
    )
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
            "Read,Write",
            "--setting-sources",
            "project,local",
            "--no-session-persistence",
            "--output-format",
            "stream-json",
            "--include-hook-events",
            "--max-budget-usd",
            "0.25",
            prompt,
        ],
        cwd=project,
        capture_output=True,
        text=True,
        env=dict(
            os.environ,
            CLAUDE_PROJECT_DIR=str(project),
            TEST_AUTHOR_HOOK_EVIDENCE_NONCE_FILE=str(nonce_file),
            TEST_AUTHOR_HOOK_EVIDENCE_LOG=str(evidence_file),
        ),
        check=False,
        timeout=180,
    )
    if result.returncode != 0:
        if required:
            sys.exit(
                "test-author Claude Code confirmation failed with exit "
                f"{result.returncode}"
            )
        print(
            "isolated-test-runner: Claude Code confirmation skipped (probe unavailable)",
            flush=True,
        )
        return
    try:
        evidence = json.loads(evidence_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        sys.exit("test-author Claude Code confirmation produced no scoped-hook evidence")
    expected = {"nonce": nonce, "decision": "deny", "path": "src/canary.txt"}
    if evidence != expected:
        sys.exit("test-author Claude Code confirmation returned invalid scoped-hook evidence")
    if (project / "src" / "canary.txt").exists():
        sys.exit("test-author Claude Code confirmation allowed a protected write")
    if not result.stdout.strip():
        sys.exit("test-author Claude Code confirmation returned no session evidence")
    print("isolated-test-runner: Claude Code scoped-hook confirmation verified", flush=True)


def _verify_test_author_disabled(project: Path, detail: str) -> None:
    """When isolation is unavailable, require a sanitized disabled fallback."""
    answer = _preflight(project)
    expected = {"status": "disabled", "reason": "isolation_unavailable"}
    if answer != expected:
        sys.exit(
            "test-author preflight did not fail closed: "
            f"expected {expected}, got {answer} ({detail})"
        )


def _verify_zone_refusals(project: Path) -> None:
    """A protected zone and a symlinked zone are refused before anything runs."""
    config = project / ".harness" / "project.json"
    original = config.read_bytes()
    alias = project / "alias"
    alias.symlink_to("src", target_is_directory=True)
    try:
        for zone in (".claude/**", "alias/**"):
            data = json.loads(original)
            data["test_paths"] = [zone]
            config.write_text(json.dumps(data), encoding="utf-8")
            _expect(_hook(project, MARKER), zone, reason="write_zone_rejected")
    finally:
        config.write_bytes(original)
        alias.unlink()
    if (project / "tests" / "marker.txt").exists():
        sys.exit("isolated-test-runner: the marker test ran despite a rejected zone")


def _verify_missing_bwrap(project: Path, test_root: Path) -> None:
    """A PATH without bwrap refuses with isolation_unavailable; the marker test never runs."""
    bin_dir = test_root / "runner-no-bwrap"
    bin_dir.mkdir()
    dirname = shutil.which("dirname")
    if dirname is None:
        sys.exit("isolated-test-runner: dirname is not on PATH")
    (bin_dir / "dirname").symlink_to(dirname)
    (bin_dir / "python3").symlink_to(Path(sys.executable).resolve())
    answer = _hook(project, MARKER, env=dict(os.environ, PATH=str(bin_dir)))
    _expect(
        answer,
        "PATH without bwrap",
        result="refused",
        reason="isolation_unavailable",
        detail="bwrap_missing",
    )
    if (project / "tests" / "marker.txt").exists():
        sys.exit("isolated-test-runner: the marker test ran without isolation")


def _verify_control_run(project: Path, test_root: Path) -> None:
    """Without the runner the same attacks change a disposable copy: they are not empty."""
    copy = test_root / "runner_control"
    shutil.copytree(project, copy, symlinks=True)
    for name, target in ATTACKS.items():
        before = hashlib.sha256((copy / target).read_bytes()).hexdigest()
        run_step(
            [sys.executable, "-B", "-m", "unittest", f"{TEST_MODULE}.Attacks.{name}"],
            cwd=copy,
            capture_output=True,
            check=False,
        )
        if hashlib.sha256((copy / target).read_bytes()).hexdigest() == before:
            sys.exit(
                f"isolated-test-runner control run: {name} did not change {target}"
            )
    if _session_bus_reachable():
        control = run_step(
            [sys.executable, "-B", "-m", "unittest", SESSION_BUS],
            cwd=copy,
            capture_output=True,
            check=False,
        )
        if control.returncode != 0:
            sys.exit(
                "isolated-test-runner control run: the session bus was not reached"
            )


def _session_bus_reachable() -> bool:
    """True when the host D-Bus session socket accepts this process: the control applies."""
    try:
        with socket.socket(socket.AF_UNIX) as sock:
            sock.connect(f"/run/user/{os.getuid()}/bus")
    except OSError:
        return False
    return True


def _verify_refusal(project: Path, probe: dict[str, object]) -> str:
    """The unavailable branch: the probe and the run refuse, nothing runs, nothing changes."""
    detail = probe.get("detail")
    _expect(probe, "probe", status="unavailable", reason="isolation_unavailable")
    before = _snapshot(project)
    answer = _hook(project, MARKER)
    _expect(
        answer,
        "refusal",
        result="refused",
        reason="isolation_unavailable",
        detail=detail,
        exit_status=3,
    )
    if (project / "tests" / "marker.txt").exists():
        sys.exit("isolated-test-runner: the marker test ran without isolation")
    if _snapshot(project) != before:
        sys.exit("isolated-test-runner: a refused run changed a protected file")
    return str(detail)
