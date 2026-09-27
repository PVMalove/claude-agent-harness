"""The one way a health check runs an external tool (git, uv, gh/glab, ...)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def run_tool(
    argv: list[str],
    *,
    timeout: float,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str] | None:
    """Run a tool non-interactively; None when it cannot be started or does not finish in time.

    Never raises: a missing tool or a stalled call becomes a `warn`/`fail` CheckResult, not an
    exception. stdin is closed and git never prompts for credentials, so a check can only time
    out, never wait on the keyboard."""
    child_env = dict(os.environ if env is None else env)
    child_env.setdefault("GIT_TERMINAL_PROMPT", "0")
    try:
        return subprocess.run(
            argv,
            cwd=cwd,
            env=child_env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
