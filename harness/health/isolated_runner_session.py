"""Private database lifecycle inside the selected test's sandbox; stdlib only.

Stdout is a private result channel. Every configured child sends output to stderr (the capped
run log), closes other descriptors, and cannot reopen the non-dumpable helper's descriptors.
The caller's bwrap PID namespace and outer timeout bound the entire process tree.
"""

from __future__ import annotations

import ctypes
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Mapping, Sequence, cast

READY_SECONDS = 15.0


def _stop(process: subprocess.Popen[bytes]) -> None:
    """Stop a foreground server and its group, even if readiness or the test failed."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            continue
        # A server can exit while a descendant keeps its group alive.
        if sig == signal.SIGTERM:
            continue
        break


def _start(command: Sequence[str], env: Mapping[str, str]) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=sys.stderr,
        stderr=sys.stderr,
        env=env,
        close_fds=True,
        start_new_session=True,
    )


def _execute(
    command: Sequence[str], env: Mapping[str, str], timeout: float | None = None
) -> int:
    child = _start(command, env)
    try:
        return child.wait(timeout=timeout)
    finally:
        _stop(child)


def run(database: dict[str, object], command: Sequence[str]) -> tuple[bool, int]:
    """Return database availability and the selected test's exit status."""
    # PR_SET_DUMPABLE=0 prevents child access via /proc/<helper>/fd and ptrace. No capability
    # or host service is needed. Without this boundary the result channel is not trustworthy.
    if ctypes.CDLL(None, use_errno=True).prctl(4, 0, 0, 0, 0) != 0:
        return False, 3
    server: subprocess.Popen[bytes] | None = None
    with tempfile.TemporaryDirectory(prefix="harness-db-", dir="/tmp") as directory:
        data, sockets = Path(directory) / "data", Path(directory) / "socket"
        data.mkdir(mode=0o700)
        sockets.mkdir(mode=0o700)
        env = dict(
            os.environ,
            HARNESS_TEST_DATABASE_DIR=str(data),
            HARNESS_TEST_DATABASE_SOCKET_DIR=str(sockets),
        )
        try:
            server = _start(cast(list[str], database["command"]), env)
            deadline = time.monotonic() + READY_SECONDS
            ready = False
            while server.poll() is None and time.monotonic() < deadline:
                try:
                    ready = (
                        _execute(
                            cast(list[str], database["ready_command"]),
                            env,
                            min(1.0, max(0.01, deadline - time.monotonic())),
                        )
                        == 0
                    )
                except subprocess.TimeoutExpired:
                    pass
                if ready:
                    break
                time.sleep(0.05)
            if not ready or server.poll() is not None:
                return False, 3
            return True, _execute(command, env)
        except (OSError, ValueError):
            return False, 3
        finally:
            if server is not None:
                _stop(server)


def main() -> int:
    available = False
    code = 3
    try:
        database, command = json.loads(sys.argv[1]), json.loads(sys.argv[2])
        available, code = run(database, command)
    finally:
        print(
            json.dumps(
                {"database": "ready" if available else "unavailable"},
                separators=(",", ":"),
            ),
            flush=True,
        )
    return code if code >= 0 else 128 - code


if __name__ == "__main__":
    sys.exit(main())
