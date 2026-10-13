"""Изолированный runner одного теста (docs/hooks/run-test.md, docs/adr/0014).

`.claude/hooks/run-test.sh <test-id>` запускает один выбранный тест командой `test_runner` из
`.harness/project.json` внутри bubblewrap: checkout, `.git` и конфигурация runner доступны тесту
и его потомкам только на чтение, сокеты сервисов хоста в `/run` скрыты, запись разрешена в
каталоги `test_paths`, в `/tmp` (tmpfs) и в свежие `cache/` и `reports/` запуска. Тест никогда
не запускается без песочницы, подтверждённой probe: любой провал до запуска даёт отказ. Вывод —
одна JSON-строка не длиннее 512 байт без путей, вывода теста и окружения; полный лог остаётся
локальным артефактом запуска.

Модуль использует только stdlib и поставляется в `.harness/health/` целевого проекта; shim
`run-test.sh` загружает его через `python3 -I -B`, не исполняя ничего из зон записи.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import IO, BinaryIO, Callable, Iterable, Mapping, Protocol, Sequence

from harness.storage import storage_path

from .project_files import (
    TEST_ID_PLACEHOLDER,
    test_paths_problems,
    test_runner_problems,
)

PROJECT_JSON_REL = Path(".harness") / "project.json"
# A pytest node id or a unittest dotted id: no leading '-', whitespace or shell metacharacter.
TEST_ID_PATTERN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:/\[\]-]{0,199}")
# The only supported zone form is '<literal directory>/**'.
ZONE_SUFFIX = "/**"
GLOB_CHARACTERS = frozenset("*?[]{}\\")
# Top-level directories that hold the repository, the harness and runtime configuration.
PROTECTED_TOP_LEVEL = frozenset({".git", ".harness", ".claude", ".agents", ".codex"})
# The sandbox replaces these with its own /tmp, /run, /dev and /proc: a checkout below them is
# invisible.
MASKED_ROOTS = ("/tmp", "/run", "/dev", "/proc")
# Host service sockets live in /run (/var/run links there): the D-Bus buses and the systemd user
# manager in /run/user/<uid>, container engines. A read-only bind does not stop connect(), and such
# a service starts processes outside the sandbox, so the sandbox mounts an empty tmpfs over /run
# and the probe requires both paths to be empty.
HIDDEN_ROOTS = ("/run", "/var/run")
ENV_ALLOWLIST = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ")
TIMEOUT_SECONDS = 300.0
PROBE_TIMEOUT_SECONDS = 60.0
LOG_LIMIT_BYTES = 4 * 1024 * 1024
LOG_TRUNCATED = b"\n[run-test: log truncated at 4 MiB]\n"
OUTPUT_LIMIT_BYTES = 512
# The `--probe` answer when the runner itself fails; run-test.sh prints the same line.
PROBE_RUNNER_UNAVAILABLE = (
    '{"status":"unavailable","reason":"runner_unavailable","detail":null}'
)
# Readers of a finished process: a pipe still open after this long is abandoned.
DRAIN_SECONDS = 5.0
STATUS_LIMIT_BYTES = 64 * 1024
READ_CHUNK = 65536

EXIT_PASSED = 0
EXIT_FAILED = 1
EXIT_REFUSED = 3

ISOLATION_UNAVAILABLE = "isolation_unavailable"
REFUSAL_REASONS = frozenset(
    {
        "runner_unavailable",
        "test_id_invalid",
        "config_invalid",
        "test_paths_missing",
        "test_runner_missing",
        "write_zone_rejected",
        ISOLATION_UNAVAILABLE,
    }
)
ISOLATION_DETAILS = frozenset(
    {"platform", "bwrap_missing", "checkout_masked", "sandbox_failed", "probe_failed"}
)

# Runs inside the sandbox with HIDDEN_ROOTS and two paths: creating the first path in the checkout
# root must fail with EROFS, every hidden root must be empty or absent, and a write to the second
# path in the run cache must pass. The runner checks the files from outside and removes them.
PROBE_SCRIPT = (
    "import errno, os, sys\n"
    "*hidden, blocked, allowed = sys.argv[1:]\n"
    "try:\n"
    "    os.close(os.open(blocked, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))\n"
    "except OSError as error:\n"
    "    denied = error.errno == errno.EROFS\n"
    "else:\n"
    "    denied = False\n"
    "empty = not any(os.path.isdir(root) and os.listdir(root) for root in hidden)\n"
    "with open(allowed, 'w') as handle:\n"
    "    handle.write('ok')\n"
    "sys.exit(0 if denied and empty else 1)\n"
)


class Refused(Exception):
    """The runner refuses before the test runs; `detail` only explains isolation_unavailable."""

    def __init__(self, reason: str, detail: str | None = None) -> None:
        super().__init__(reason)
        assert reason in REFUSAL_REASONS
        assert (detail in ISOLATION_DETAILS) == (reason == ISOLATION_UNAVAILABLE)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class Outcome:
    """One runner answer before rendering."""

    test_id: str | None
    result: str
    reason: str
    detail: str | None = None
    exit_code: int | None = None
    run_id: str | None = None

    @property
    def status(self) -> int:
        """Process exit status: 0 passed, 1 failed or timeout, 3 refused."""
        if self.result == "passed":
            return EXIT_PASSED
        if self.result == "refused":
            return EXIT_REFUSED
        return EXIT_FAILED


@dataclass(frozen=True)
class Launch:
    """What one sandboxed process run produced."""

    # bwrap reported the child pid: the sandbox was set up and the command was started in it.
    started: bool
    exit_code: int | None
    timed_out: bool = False


class Launcher(Protocol):
    """Runs the argv that `build` returns for the status fd and streams its output to `log`."""

    def __call__(
        self,
        build: Callable[[int], list[str]],
        *,
        env: Mapping[str, str],
        log: Path,
        timeout: float,
    ) -> Launch: ...


def valid_test_id(value: object) -> bool:
    """True for an id the runner may pass to the test command as one argument."""
    return isinstance(value, str) and TEST_ID_PATTERN.fullmatch(value) is not None


def render(outcome: Outcome) -> str:
    """The one sanitized output line: fixed keys, at most OUTPUT_LIMIT_BYTES bytes."""
    line = json.dumps(
        {
            "test_id": outcome.test_id if valid_test_id(outcome.test_id) else None,
            "result": outcome.result,
            "reason": outcome.reason,
            "detail": outcome.detail,
            "exit_code": outcome.exit_code,
            "run_id": outcome.run_id,
        },
        separators=(",", ":"),
    )
    assert len(line.encode("utf-8")) <= OUTPUT_LIMIT_BYTES
    return line


def render_probe(detail: str | None) -> str:
    """The `--probe` line: ok, or unavailable with the isolation detail."""
    if detail is None:
        return json.dumps({"status": "ok"}, separators=(",", ":"))
    return json.dumps(
        {"status": "unavailable", "reason": ISOLATION_UNAVAILABLE, "detail": detail},
        separators=(",", ":"),
    )


def load_settings(repo: Path) -> tuple[list[str], list[str]]:
    """`test_paths` and `test_runner.command` from the project config, checked by the health
    validators; any other state is a refusal."""
    try:
        data = json.loads((repo / PROJECT_JSON_REL).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise Refused("test_paths_missing") from None
    except (OSError, ValueError, RecursionError):
        raise Refused("config_invalid") from None
    if not isinstance(data, dict):
        raise Refused("config_invalid")
    if "test_paths" not in data:
        raise Refused("test_paths_missing")
    if test_paths_problems(data["test_paths"]):
        raise Refused("config_invalid")
    if "test_runner" not in data:
        raise Refused("test_runner_missing")
    if test_runner_problems(data["test_runner"]):
        raise Refused("config_invalid")
    return list(data["test_paths"]), list(data["test_runner"]["command"])


def write_zones(repo: Path, globs: Iterable[str]) -> list[Path]:
    """Directories the test may write, one per `test_paths` glob; `repo` must be resolved.

    Only '<literal directory>/**' outside the protected top-level directories is accepted. The
    directory must exist, no component of its path may be a symlink, no zone may contain another,
    and no regular file in it may have a second hard link: each of these would give the test a
    writable alias of the checkout.
    """
    zones = [_zone(repo, glob) for glob in globs]
    for zone in zones:
        if any(other != zone and other in zone.parents for other in zones):
            raise Refused("write_zone_rejected")
    if len(set(zones)) != len(zones):
        raise Refused("write_zone_rejected")
    return zones


def _zone(repo: Path, glob: str) -> Path:
    """The directory of one '<literal directory>/**' glob; any other form is refused."""
    if not glob.endswith(ZONE_SUFFIX):
        raise Refused("write_zone_rejected")
    parts = glob[: -len(ZONE_SUFFIX)].split("/")
    if any(
        part in ("", ".", "..") or GLOB_CHARACTERS.intersection(part) for part in parts
    ):
        raise Refused("write_zone_rejected")
    if parts[0].lower() in PROTECTED_TOP_LEVEL:
        raise Refused("write_zone_rejected")
    zone = repo
    for part in parts:
        zone = zone / part
        try:
            mode = os.lstat(zone).st_mode
        except OSError:
            raise Refused("write_zone_rejected") from None
        if not stat.S_ISDIR(mode):
            raise Refused("write_zone_rejected")
    if Path(os.path.realpath(zone)) != zone or _has_linked_file(zone):
        raise Refused("write_zone_rejected")
    return zone


def _has_linked_file(zone: Path) -> bool:
    """True when a regular file below `zone` has more than one hard link (or cannot be read)."""

    def failed(error: OSError) -> None:
        raise error

    try:
        for directory, _, files in os.walk(zone, onerror=failed):
            for name in files:
                info = os.lstat(os.path.join(directory, name))
                if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                    return True
    except OSError:
        return True
    return False


def _masked(path: Path) -> bool:
    """True when the sandbox hides `path` behind its own /tmp, /run, /dev or /proc."""
    text = path.as_posix()
    return any(text == root or text.startswith(root + "/") for root in MASKED_ROOTS)


def sandbox_argv(
    bwrap: str,
    repo: Path,
    binds: Sequence[Path],
    command: Sequence[str],
    status_fd: int,
) -> list[str]:
    """The bubblewrap argv of ADR 0014: a read-only root, private /dev, /proc and /tmp, an empty
    /run without host sockets, writable `binds`, every namespace unshared and no capabilities;
    `command` runs without a shell."""
    argv = [
        bwrap,
        "--ro-bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--tmpfs",
        "/tmp",
        "--tmpfs",
        "/run",
    ]
    for path in binds:
        argv += ["--bind", str(path), str(path)]
    argv += [
        "--chdir",
        str(repo),
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--cap-drop",
        "ALL",
        "--json-status-fd",
        str(status_fd),
        "--",
        *command,
    ]
    return argv


def command_for(command: Sequence[str], test_id: str) -> list[str]:
    """The configured argv with its one `{test_id}` element replaced by the id."""
    return [test_id if part == TEST_ID_PLACEHOLDER else part for part in command]


def sandbox_environment(
    cache: Path, reports: Path, environ: Mapping[str, str]
) -> dict[str, str]:
    """The test's whole environment: allowlisted locale and PATH plus the run's own locations."""
    env = {name: environ[name] for name in ENV_ALLOWLIST if name in environ}
    env.update(
        HOME="/tmp",
        TMPDIR="/tmp",
        XDG_CACHE_HOME=str(cache),
        PYTHONPYCACHEPREFIX=str(cache / "pycache"),
        HARNESS_TEST_CACHE_DIR=str(cache),
        HARNESS_TEST_REPORT_DIR=str(reports),
    )
    return env


def launch(
    build: Callable[[int], list[str]],
    *,
    env: Mapping[str, str],
    log: Path,
    timeout: float,
) -> Launch:
    """Run the argv `build(status_fd)` in a new session, append its capped output to `log` and
    kill its process group on timeout. `started` needs a `child-pid` record on the status fd."""
    try:
        sink = open(log, "ab")
    except OSError:
        return Launch(started=False, exit_code=None)
    read_fd, write_fd = os.pipe()
    status = bytearray()
    try:
        with sink:
            try:
                process = subprocess.Popen(
                    build(write_fd),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    env=dict(env),
                    pass_fds=(write_fd,),
                    start_new_session=True,
                )
            except (OSError, ValueError):
                return Launch(started=False, exit_code=None)
            finally:
                os.close(write_fd)
            assert process.stdout is not None
            readers = [
                threading.Thread(
                    target=_copy_capped, args=(process.stdout, sink), daemon=True
                ),
                threading.Thread(
                    target=_read_status, args=(read_fd, status), daemon=True
                ),
            ]
            for reader in readers:
                reader.start()
            timed_out = False
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_group(process)
                process.wait()
            for reader in readers:
                reader.join(DRAIN_SECONDS)
            process.stdout.close()
    finally:
        os.close(read_fd)
    return Launch(
        started=_child_started(bytes(status)),
        exit_code=None if timed_out else process.returncode,
        timed_out=timed_out,
    )


def _copy_capped(source: IO[bytes], sink: BinaryIO) -> None:
    """Stream `source` into `sink` until the log holds LOG_LIMIT_BYTES, then drain it."""
    truncated = False
    # A reader abandoned after DRAIN_SECONDS meets closed files: it stops quietly.
    try:
        written = sink.tell()
        while chunk := os.read(source.fileno(), READ_CHUNK):
            if written < LOG_LIMIT_BYTES:
                part = chunk[: LOG_LIMIT_BYTES - written]
                sink.write(part)
                written += len(part)
                if len(part) == len(chunk):
                    continue
            if not truncated:
                sink.write(LOG_TRUNCATED)
                truncated = True
        sink.flush()
    except (OSError, ValueError):
        return


def _read_status(fd: int, status: bytearray) -> None:
    """Collect what bwrap writes on its status fd, up to STATUS_LIMIT_BYTES."""
    try:
        while chunk := os.read(fd, READ_CHUNK):
            status.extend(chunk[: max(0, STATUS_LIMIT_BYTES - len(status))])
    except OSError:
        return


def _child_started(status: bytes) -> bool:
    """True when one JSON document on the status fd carries `child-pid`."""
    for line in status.decode("utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and "child-pid" in record:
            return True
    return False


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    """Kill the session the process leads; --die-with-parent ends the sandboxed tree."""
    if sys.platform == "win32":
        process.kill()
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        process.kill()


def new_run_id() -> str:
    """A fresh hex run id."""
    return secrets.token_hex(8)


def _require_isolation(
    repo: Path, platform: str, which: Callable[[str], str | None]
) -> str:
    """The bwrap path when this host can host the sandbox at all."""
    if platform != "linux":
        raise Refused(ISOLATION_UNAVAILABLE, "platform")
    bwrap = which("bwrap")
    if not bwrap:
        raise Refused(ISOLATION_UNAVAILABLE, "bwrap_missing")
    if _masked(repo):
        raise Refused(ISOLATION_UNAVAILABLE, "checkout_masked")
    return bwrap


def _make_run_dir(repo: Path, run_id: str) -> Path:
    """Create `.harness/.sandboxes/runs/isolated-test-<run_id>/` with empty cache/ and reports/."""
    try:
        run_dir = storage_path(repo, "runs", f"isolated-test-{run_id}")
        if _masked(run_dir.resolve()):
            raise Refused(ISOLATION_UNAVAILABLE, "checkout_masked")
        for name in ("cache", "reports"):
            (run_dir / name).mkdir(parents=True)
        return run_dir.resolve()
    except (OSError, ValueError):
        raise Refused(ISOLATION_UNAVAILABLE, "sandbox_failed") from None


def _probe(
    *,
    bwrap: str,
    repo: Path,
    binds: Sequence[Path],
    env: Mapping[str, str],
    run_dir: Path,
    launcher: Launcher,
    run_id: str,
) -> None:
    """Prove with the test's own mounts that the checkout is read-only, the host sockets are
    hidden and the cache is writable."""
    blocked = repo / f".harness-isolation-probe-{run_id}"
    allowed = run_dir / "cache" / "isolation-probe"
    if os.path.lexists(blocked):
        raise Refused(ISOLATION_UNAVAILABLE, "probe_failed")
    command = [
        sys.executable,
        "-I",
        "-B",
        "-c",
        PROBE_SCRIPT,
        *HIDDEN_ROOTS,
        str(blocked),
        str(allowed),
    ]
    try:
        result = launcher(
            lambda fd: sandbox_argv(bwrap, repo, binds, command, fd),
            env=env,
            log=run_dir / "log.txt",
            timeout=PROBE_TIMEOUT_SECONDS,
        )
        isolated = (
            result.exit_code == 0
            and not os.path.lexists(blocked)
            and _read_marker(allowed) == "ok"
        )
    finally:
        blocked.unlink(missing_ok=True)
        allowed.unlink(missing_ok=True)
    if not result.started:
        raise Refused(ISOLATION_UNAVAILABLE, "sandbox_failed")
    if not isolated:
        raise Refused(ISOLATION_UNAVAILABLE, "probe_failed")


def _read_marker(path: Path) -> str | None:
    """The probe marker text, or None when the probe did not write it."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None


def probe(
    repo: Path,
    *,
    launcher: Launcher = launch,
    platform: str = sys.platform,
    which: Callable[[str], str | None] = shutil.which,
    environ: Mapping[str, str] = os.environ,
) -> str | None:
    """`--probe`: None when the sandbox works here, else the isolation detail. Needs no
    `test_paths`; the probe run directory is removed afterwards."""
    repo = repo.resolve()
    run_id = new_run_id()
    run_dir: Path | None = None
    try:
        bwrap = _require_isolation(repo, platform, which)
        run_dir = _make_run_dir(repo, run_id)
        cache, reports = run_dir / "cache", run_dir / "reports"
        _probe(
            bwrap=bwrap,
            repo=repo,
            binds=[cache, reports],
            env=sandbox_environment(cache, reports, environ),
            run_dir=run_dir,
            launcher=launcher,
            run_id=run_id,
        )
    except Refused as refusal:
        return refusal.detail
    finally:
        if run_dir is not None:
            shutil.rmtree(run_dir, ignore_errors=True)
    return None


def run_test(
    repo: Path,
    test_id: str,
    *,
    launcher: Launcher = launch,
    platform: str = sys.platform,
    which: Callable[[str], str | None] = shutil.which,
    environ: Mapping[str, str] = os.environ,
) -> Outcome:
    """Run one test in the sandbox; every check before the test fails closed with a refusal.

    Order: id, settings, zones, host, run directory, probe, test.
    """
    if not valid_test_id(test_id):
        return Outcome(None, "refused", "test_id_invalid")
    repo = repo.resolve()
    run_id: str | None = None
    try:
        globs, command = load_settings(repo)
        zones = write_zones(repo, globs)
        bwrap = _require_isolation(repo, platform, which)
        run_id = new_run_id()
        run_dir = _make_run_dir(repo, run_id)
        cache, reports = run_dir / "cache", run_dir / "reports"
        binds = [*zones, cache, reports]
        env = sandbox_environment(cache, reports, environ)
        _probe(
            bwrap=bwrap,
            repo=repo,
            binds=binds,
            env=env,
            run_dir=run_dir,
            launcher=launcher,
            run_id=run_id,
        )
        argv = command_for(command, test_id)
        result = launcher(
            lambda fd: sandbox_argv(bwrap, repo, binds, argv, fd),
            env=env,
            log=run_dir / "log.txt",
            timeout=TIMEOUT_SECONDS,
        )
        if not result.started:
            raise Refused(ISOLATION_UNAVAILABLE, "sandbox_failed")
    except Refused as refusal:
        return Outcome(
            test_id, "refused", refusal.reason, refusal.detail, run_id=run_id
        )
    if result.timed_out:
        return Outcome(test_id, "timeout", "timeout", run_id=run_id)
    if result.exit_code == 0:
        return Outcome(test_id, "passed", "test_passed", exit_code=0, run_id=run_id)
    return Outcome(
        test_id, "failed", "test_failed", exit_code=result.exit_code, run_id=run_id
    )


def main(argv: Sequence[str]) -> int:
    """Shim entry: `argv[0]` is the repository, the rest is `<test-id>` or `--probe`.

    An unexpected runner error is reported as runner_unavailable, never as a traceback.
    """
    arguments = list(argv[1:])
    if arguments == ["--probe"]:
        try:
            detail = probe(Path(argv[0]))
        except Exception:
            print(PROBE_RUNNER_UNAVAILABLE)
            return EXIT_REFUSED
        print(render_probe(detail))
        return EXIT_PASSED if detail is None else EXIT_REFUSED
    if len(arguments) != 1:
        outcome = Outcome(None, "refused", "test_id_invalid")
    else:
        try:
            outcome = run_test(Path(argv[0]), arguments[0])
        except Exception:
            outcome = Outcome(None, "refused", "runner_unavailable")
    print(render(outcome))
    return outcome.status
