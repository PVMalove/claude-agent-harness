"""External worker adapters exercised only with dry runs and fake runtimes."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def dispatch(tmp_path: Path) -> tuple[Path, Path, dict[str, object]]:
    worker = tmp_path / "worker"
    worker.mkdir()
    subprocess.run(
        ["git", "init", "-b", "feature/issue-715-adapters", str(worker)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(worker),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--allow-empty",
            "-m",
            "init",
        ],
        check=True,
        capture_output=True,
    )
    sha = subprocess.check_output(
        ["git", "-C", str(worker), "rev-parse", "HEAD"], text=True
    ).strip()
    repo = tmp_path / "main"
    incoming = repo / ".harness/.sandboxes/scratch/inbox"
    incoming.mkdir(parents=True)
    brief: dict[str, object] = {
        "dispatch_id": "dispatch-test",
        "role": "developer",
        "purpose": "work",
        "resolved_transport": "external",
        "resolved_model": "test-model",
        "resolved_effort": "high",
        "allowed_tools": ["Read", "Bash", "Edit"],
        "access": "write",
        "report_staging_path": str(incoming / "dispatch-test.json"),
        "context_package_id": "context-package-test",
        "liveness": {"heartbeat_every_seconds": 300},
        "worktree": str(worker),
        "snapshot_commit": sha,
        "branch": "feature/issue-715-adapters",
    }
    return repo, worker, brief


def invoke(
    dispatch: tuple[Path, Path, dict[str, object]],
    *args: str,
    runtime: str = "claude",
    changes: dict[str, object] | None = None,
) -> subprocess.CompletedProcess[str]:
    repo, worker, brief = dispatch
    payload = dict(brief)
    payload.update(changes or {})
    path = repo / "brief.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / f"harness/adapters/{runtime}_worker.py"),
            "dispatch",
            "--repo",
            str(repo),
            "--brief",
            str(path),
            *args,
        ],
        cwd=worker,
        text=True,
        capture_output=True,
        timeout=15,
    )


def test_claude_dry_run_uses_brief_and_only_incoming_report_directory(
    dispatch: tuple[Path, Path, dict[str, object]],
) -> None:
    repo, worker, brief = dispatch
    extra = repo / "retry.txt"
    extra.write_text("Close retry finding finding-1", encoding="utf-8")
    result = invoke(
        dispatch,
        "--dry-run",
        "--claude-bin",
        "/fake/claude",
        "--extra-prompt-file",
        str(extra),
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    command = data["command"]
    assert command[0:2] == ["/fake/claude", "-p"]
    assert command[command.index("--model") + 1] == "test-model"
    assert command[command.index("--effort") + 1] == "high"
    assert command[command.index("--allowedTools") + 1] == "Read,Bash,Edit"
    assert command[command.index("--add-dir") + 1] == str(
        Path(str(brief["report_staging_path"])).parent
    )
    assert data["cwd"] == str(worker)
    for text in (
        "dispatch self-report",
        "dispatch heartbeat",
        "report submit",
        "context-package-test",
        "Close retry finding finding-1",
        "ledger_busy",
    ):
        assert text in data["prompt"]


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"role": "qa"}, "role qa"),
        ({"purpose": "publish"}, "purpose publish"),
        ({"resolved_transport": "in-process"}, "external"),
        ({"allowed_tools": None}, "allowed_tools"),
        ({"allowed_tools": []}, "allowed_tools"),
    ],
)
def test_claude_rejects_unsupported_briefs_before_launch(
    dispatch: tuple[Path, Path, dict[str, object]],
    changes: dict[str, object],
    reason: str,
) -> None:
    result = invoke(dispatch, "--dry-run", changes=changes)
    assert result.returncode == 1
    assert reason in result.stderr
    assert not (dispatch[0] / ".harness/.sandboxes/scratch/worker-logs").exists()


def fake_runtime(repo: Path, body: str) -> Path:
    binary = repo / "fake-runtime"
    binary.write_text(f"#!{sys.executable}\n" + body, encoding="utf-8")
    binary.chmod(0o755)
    return binary


@pytest.mark.parametrize("code", [0, 7])
def test_claude_reports_any_early_exit_with_log_tail(
    dispatch: tuple[Path, Path, dict[str, object]], code: int
) -> None:
    binary = fake_runtime(
        dispatch[0],
        f"import sys\nsys.stdin.read()\nprint('fake startup failure', flush=True)\nsys.exit({code})\n",
    )
    result = invoke(dispatch, "--claude-bin", str(binary))
    assert result.returncode == 1
    assert f"exited with {code}" in result.stderr
    assert "fake startup failure" in result.stderr


def test_claude_starts_detached_with_stdin_prompt_in_worker_checkout(
    dispatch: tuple[Path, Path, dict[str, object]],
) -> None:
    repo, worker, _ = dispatch
    evidence = repo / "started.json"
    body = f"import json, os, sys, time\nfrom pathlib import Path\nprompt = sys.stdin.read()\nPath({str(evidence)!r}).write_text(json.dumps(dict(pid=os.getpid(), session=os.getsid(0), cwd=os.getcwd(), prompt=prompt, args=sys.argv[1:])))\ntime.sleep(30)\n"
    binary = fake_runtime(repo, body)
    try:
        started = time.monotonic()
        result = invoke(dispatch, "--claude-bin", str(binary))
        assert result.returncode == 0, result.stderr
        assert time.monotonic() - started < 10
        data = json.loads(evidence.read_text())
        assert data["pid"] == data["session"]
        assert data["cwd"] == str(worker)
        assert "dispatch self-report" in data["prompt"]
        assert "--model" in data["args"]
    finally:
        if evidence.exists():
            os.kill(json.loads(evidence.read_text())["pid"], 15)


def test_claude_dispatch_help_lists_entrypoint_arguments(
    dispatch: tuple[Path, Path, dict[str, object]],
) -> None:
    result = invoke(dispatch, "--help")
    assert result.returncode == 0
    assert "--repo" in result.stdout and "--brief" in result.stdout
    assert "--extra-prompt-file" in result.stdout and "--claude-bin" in result.stdout
