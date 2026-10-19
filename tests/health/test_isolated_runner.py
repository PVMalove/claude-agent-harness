"""Тесты изолированного runner одного теста (harness/health/isolated_runner.py, issue #553).

Ветки отказа проверяются через подменный launcher, реальная песочница — отдельными тестами,
которые пропускаются, только если probe на этом хосте недоступен. Репозитории-фикстуры живут в
хранилище `runs/tests`, а не в `tmp_path`: pytest по умолчанию держит его под `/tmp`, который
песочница закрывает своим tmpfs.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence, cast

import pytest

from harness.health import isolated_runner as runner
from harness.storage import storage_path

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "harness" / "project" / "hooks" / "run-test.sh"
ORIGINAL = "def add(a, b):\n    return a + b\n"

# The fixture test command: tests/attack.py does what its single argument (the test id) names.
ATTACK = """\
import os, subprocess, sys, time

TARGETS = {
    "direct": "src/calc.py",
    "git_config": ".git/config",
    "project_json": ".harness/project.json",
    "hook": ".claude/hooks/run-test.sh",
    "module": ".harness/health/isolated_runner.py",
}
action = sys.argv[1]
if action in TARGETS:
    with open(TARGETS[action], "a") as handle:
        handle.write("pwned\\n")
elif action == "child_sh":
    subprocess.run(["sh", "-c", "echo pwned >> src/calc.py"], check=True)
elif action == "child_py":
    code = "open('src/calc.py', 'a').write('pwned')"
    subprocess.run([sys.executable, "-c", code], check=True)
elif action == "symlink":
    os.symlink("../src", "tests/link")
    with open("tests/link/calc.py", "a") as handle:
        handle.write("pwned\\n")
elif action == "hardlink":
    os.link("src/calc.py", "tests/calc_link.py")
elif action == "legit":
    for directory in ("/tmp", os.environ["XDG_CACHE_HOME"],
                      os.environ["HARNESS_TEST_REPORT_DIR"], "tests"):
        with open(os.path.join(directory, "legit.txt"), "w") as handle:
            handle.write("ok")
    assert open("/tmp/legit.txt").read() == "ok"
elif action == "hidden_roots":
    sys.exit(1 if any(os.path.isdir(p) and os.listdir(p) for p in ("/run", "/var/run")) else 0)
elif action == "session_bus":
    import socket
    with socket.socket(socket.AF_UNIX) as sock:
        sock.connect("/run/user/%d/bus" % os.getuid())
elif action == "environment":
    sys.exit(1 if "RUNNER_SECRET" in os.environ else 0)
elif action == "sleep":
    time.sleep(120)
elif action == "fail":
    sys.exit(5)
"""


def _settings(test_paths: object, command: object) -> dict[str, object]:
    return {
        "language": "ru",
        "base_branch": "main",
        "branch_pattern": "^feature/.+",
        "qa_gate_commands": [],
        "test_paths": test_paths,
        "test_runner": {"command": command},
    }


def _write_settings(repo: Path, data: object) -> None:
    (repo / ".harness" / "project.json").write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def project() -> Iterator[Path]:
    """A project without a build: src/, tests/, runner configuration and a fake .git/."""
    base = storage_path(ROOT, "runs", "tests")
    base.mkdir(parents=True, exist_ok=True)
    repo = Path(tempfile.mkdtemp(prefix="irun-", dir=base)).resolve()
    for directory in ("src", "tests", ".git", ".harness/health", ".claude/hooks"):
        (repo / directory).mkdir(parents=True)
    (repo / "src" / "calc.py").write_text(ORIGINAL, encoding="utf-8")
    (repo / "tests" / "attack.py").write_text(ATTACK, encoding="utf-8")
    (repo / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    shutil.copyfile(HOOK, repo / ".claude" / "hooks" / "run-test.sh")
    shutil.copyfile(
        Path(runner.__file__), repo / ".harness" / "health" / "isolated_runner.py"
    )
    shutil.copyfile(
        Path(runner.__file__).with_name("isolated_runner_session.py"),
        repo / ".harness/health/isolated_runner_session.py",
    )
    _write_settings(
        repo,
        _settings(["tests/**"], [sys.executable, "-B", "tests/attack.py", "{test_id}"]),
    )
    yield repo
    shutil.rmtree(repo, ignore_errors=True)


def _snapshot(repo: Path) -> dict[str, str]:
    """sha256 of every file outside tests/ and .harness/.sandboxes/."""
    digests: dict[str, str] = {}
    for path in sorted(repo.rglob("*")):
        relative = path.relative_to(repo).as_posix()
        if (
            relative.startswith(("tests/", ".harness/.sandboxes/"))
            or not path.is_file()
        ):
            continue
        digests[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digests


@dataclass
class FakeLauncher:
    """Records each launch; the probe writes its cache marker unless `probe_marker` is off."""

    results: list[runner.Launch]
    probe_marker: bool = True
    calls: list[list[str]] = field(default_factory=list)
    envs: list[Mapping[str, str]] = field(default_factory=list)

    def __call__(
        self,
        build: Callable[[int], list[str]],
        *,
        env: Mapping[str, str],
        log: Path,
        timeout: float,
        session: bool = False,
    ) -> runner.Launch:
        argv = build(7)
        self.calls.append(argv)
        self.envs.append(env)
        if runner.PROBE_SCRIPT in argv and self.probe_marker:
            Path(argv[-1]).write_text("ok", encoding="utf-8")
        return self.results[len(self.calls) - 1]


def _run(
    repo: Path,
    test_id: str,
    launcher: FakeLauncher,
    *,
    platform: str = "linux",
    bwrap: str | None = "/usr/bin/bwrap",
) -> runner.Outcome:
    return runner.run_test(
        repo,
        test_id,
        launcher=launcher,
        platform=platform,
        which=lambda name: bwrap,
        environ={"PATH": "/usr/bin", "RUNNER_SECRET": "x"},
    )


STARTED = runner.Launch(started=True, exit_code=0)


def _binds(argv: list[str]) -> list[str]:
    """The writable mount sources of a bwrap argv."""
    return [argv[index + 1] for index, part in enumerate(argv) if part == "--bind"]


# --- test ids -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "test_id",
    [
        "tests.test_calc.CalcTest.test_add",
        "tests/test_calc.py::test_add",
        "tests/test_calc.py::TestCalc::test_add[1-2]",
        "_private",
        "a" * 200,
    ],
)
def test_valid_test_ids(test_id: str) -> None:
    assert runner.valid_test_id(test_id)


@pytest.mark.parametrize(
    "test_id",
    [
        "",
        "-k",
        "--help",
        "a;b",
        "$(touch pwned)",
        "`touch pwned`",
        "a\nb",
        "a b",
        " a",
        "/abs/test.py",
        "a" * 201,
        "tests.test_calc; touch pwned",
    ],
)
def test_invalid_test_id_is_refused_before_anything(
    project: Path, test_id: str
) -> None:
    launcher = FakeLauncher([])
    outcome = _run(project, test_id, launcher)
    assert (outcome.result, outcome.reason, outcome.test_id) == (
        "refused",
        "test_id_invalid",
        None,
    )
    assert outcome.status == runner.EXIT_REFUSED
    assert launcher.calls == []
    assert not (project / ".harness" / ".sandboxes").exists()


# --- settings -----------------------------------------------------------------------------


def _refusal(repo: Path) -> tuple[str, str | None]:
    with pytest.raises(runner.Refused) as caught:
        runner.load_settings(repo)
    return caught.value.reason, caught.value.detail


def test_load_settings(project: Path) -> None:
    settings = runner.load_settings(project)
    assert settings.test_paths == ["tests/**"]
    assert settings.command[-1] == "{test_id}"


def test_missing_project_json_means_no_test_paths(project: Path) -> None:
    (project / ".harness" / "project.json").unlink()
    assert _refusal(project) == ("test_paths_missing", None)


@pytest.mark.parametrize("text", ["{", "[]", '"x"'])
def test_unreadable_project_json_is_config_invalid(project: Path, text: str) -> None:
    (project / ".harness" / "project.json").write_text(text, encoding="utf-8")
    assert _refusal(project) == ("config_invalid", None)


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        ({"test_runner": {"command": ["x", "{test_id}"]}}, "test_paths_missing"),
        ({"test_paths": ["tests/**"]}, "test_runner_missing"),
        (
            {"test_paths": [], "test_runner": {"command": ["x", "{test_id}"]}},
            "config_invalid",
        ),
        (
            {"test_paths": ["../x/**"], "test_runner": {"command": ["x", "{test_id}"]}},
            "config_invalid",
        ),
        (
            {"test_paths": ["tests/**"], "test_runner": {"command": ["x"]}},
            "config_invalid",
        ),
        (
            {
                "test_paths": ["tests/**"],
                "test_runner": {"command": ["{test_id}", "x"]},
            },
            "config_invalid",
        ),
    ],
)
def test_settings_refusals(project: Path, data: dict[str, object], reason: str) -> None:
    _write_settings(project, data)
    assert _refusal(project) == (reason, None)
    outcome = _run(project, "direct", FakeLauncher([]))
    assert (outcome.result, outcome.reason, outcome.run_id) == ("refused", reason, None)


# --- write zones --------------------------------------------------------------------------


def test_write_zone_is_the_literal_directory(project: Path) -> None:
    (project / "tests" / "unit").mkdir()
    (project / "spec").mkdir()
    assert runner.write_zones(project, ["tests/unit/**", "spec/**"]) == [
        project / "tests" / "unit",
        project / "spec",
    ]


@pytest.mark.parametrize(
    "glob",
    [
        "**",
        "**/test_*.py",
        "tests/*",
        "tests",
        "tests/",
        "/**",
        "./tests/**",
        "tests/./unit/**",
        "tests//**",
        "tests/../src/**",
        "te*sts/**",
        "tests\\unit/**",
        ".harness/**",
        ".git/**",
        ".claude/**",
        ".Claude/**",
        ".agents/**",
        ".codex/**",
        "missing/**",
        "src/calc.py/**",
    ],
)
def test_rejected_zone_forms(project: Path, glob: str) -> None:
    with pytest.raises(runner.Refused) as caught:
        runner.write_zones(project, [glob])
    assert caught.value.reason == "write_zone_rejected"


def test_symlinked_zone_is_rejected(project: Path) -> None:
    (project / "alias").symlink_to(project / "src", target_is_directory=True)
    (project / "tests" / "link").symlink_to(project / "src", target_is_directory=True)
    (project / "linked").symlink_to(project / "tests", target_is_directory=True)
    (project / "tests" / "unit").mkdir()
    for glob in ("alias/**", "tests/link/**", "linked/unit/**"):
        with pytest.raises(runner.Refused):
            runner.write_zones(project, [glob])


def test_hard_linked_file_in_zone_is_rejected(project: Path) -> None:
    os.link(project / "src" / "calc.py", project / "tests" / "calc_alias.py")
    with pytest.raises(runner.Refused) as caught:
        runner.write_zones(project, ["tests/**"])
    assert caught.value.reason == "write_zone_rejected"


def test_symlink_inside_zone_is_allowed(project: Path) -> None:
    (project / "tests" / "link").symlink_to(project / "src", target_is_directory=True)
    assert runner.write_zones(project, ["tests/**"]) == [project / "tests"]


@pytest.mark.parametrize(
    "globs",
    [["tests/**", "tests/unit/**"], ["tests/unit/**", "tests/**"], ["tests/**"] * 2],
)
def test_overlapping_zones_are_rejected(project: Path, globs: list[str]) -> None:
    (project / "tests" / "unit").mkdir()
    with pytest.raises(runner.Refused):
        runner.write_zones(project, globs)


def test_unreadable_zone_is_rejected(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(
        *args: object, **kwargs: object
    ) -> Iterator[tuple[str, list[str], list[str]]]:
        raise OSError("unreadable")

    monkeypatch.setattr(os, "walk", broken)
    with pytest.raises(runner.Refused):
        runner.write_zones(project, ["tests/**"])


def test_rejected_zone_refuses_the_run(project: Path) -> None:
    _write_settings(project, _settings([".claude/**"], ["x", "{test_id}"]))
    launcher = FakeLauncher([])
    outcome = _run(project, "direct", launcher)
    assert (outcome.result, outcome.reason) == ("refused", "write_zone_rejected")
    assert launcher.calls == []


# --- argv, environment, rendering -----------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="bwrap runs only on Linux")
def test_sandbox_argv_is_the_adr_flag_set() -> None:
    argv = runner.sandbox_argv(
        "/usr/bin/bwrap",
        Path("/repo"),
        [Path("/repo/tests"), Path("/cache")],
        ["python3", "-m", "unittest", "a;b c"],
        9,
    )
    assert argv == [
        "/usr/bin/bwrap",
        "--ro-bind", "/", "/",
        "--dev", "/dev",
        "--proc", "/proc",
        "--tmpfs", "/tmp",
        "--tmpfs", "/run",
        "--bind", "/repo/tests", "/repo/tests",
        "--bind", "/cache", "/cache",
        "--chdir", "/repo",
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--cap-drop", "ALL",
        "--json-status-fd", "9",
        "--",
        "python3", "-m", "unittest", "a;b c",
    ]  # fmt: skip


def test_command_for_replaces_only_the_placeholder_element() -> None:
    command = ["pytest", "x{test_id}", "{test_id}", "-q"]
    assert runner.command_for(command, "t::a") == ["pytest", "x{test_id}", "t::a", "-q"]


@pytest.mark.skipif(sys.platform == "win32", reason="bwrap runs only on Linux")
def test_sandbox_environment_is_an_allowlist() -> None:
    env = runner.sandbox_environment(
        Path("/run/cache"),
        Path("/run/reports"),
        {"PATH": "/bin", "LANG": "C.UTF-8", "GITHUB_TOKEN": "x", "PYTHONPATH": "/evil"},
    )
    assert env == {
        "PATH": "/bin",
        "LANG": "C.UTF-8",
        "HOME": "/tmp",
        "TMPDIR": "/tmp",
        "XDG_CACHE_HOME": "/run/cache",
        "PYTHONPYCACHEPREFIX": "/run/cache/pycache",
        "HARNESS_TEST_CACHE_DIR": "/run/cache",
        "HARNESS_TEST_REPORT_DIR": "/run/reports",
    }


def test_render_has_fixed_keys_and_bound() -> None:
    outcome = runner.Outcome(
        "a" * 200, "failed", "test_failed", exit_code=-9, run_id="f" * 16
    )
    line = runner.render(outcome)
    assert list(json.loads(line)) == [
        "test_id",
        "result",
        "reason",
        "detail",
        "exit_code",
        "run_id",
    ]
    assert len(line.encode()) <= runner.OUTPUT_LIMIT_BYTES
    assert "\n" not in line


def test_render_never_echoes_an_invalid_id() -> None:
    line = runner.render(runner.Outcome("$(touch pwned)", "refused", "test_id_invalid"))
    assert json.loads(line)["test_id"] is None
    assert "pwned" not in line


def test_render_probe() -> None:
    assert json.loads(runner.render_probe(None)) == {"status": "ok"}
    assert json.loads(runner.render_probe("bwrap_missing")) == {
        "status": "unavailable",
        "reason": "isolation_unavailable",
        "detail": "bwrap_missing",
    }


def test_refusal_detail_only_explains_isolation() -> None:
    with pytest.raises(AssertionError):
        runner.Refused("config_invalid", "platform")
    with pytest.raises(AssertionError):
        runner.Refused("isolation_unavailable")


# --- fail closed before the test --------------------------------------------------------------


@pytest.mark.parametrize(
    ("platform", "bwrap", "detail"),
    [
        ("win32", "/usr/bin/bwrap", "platform"),
        ("darwin", None, "platform"),
        ("linux", None, "bwrap_missing"),
    ],
)
def test_host_without_isolation_never_launches(
    project: Path, platform: str, bwrap: str | None, detail: str
) -> None:
    launcher = FakeLauncher([])
    outcome = _run(project, "direct", launcher, platform=platform, bwrap=bwrap)
    assert (outcome.result, outcome.reason, outcome.detail) == (
        "refused",
        "isolation_unavailable",
        detail,
    )
    assert launcher.calls == []


def test_masked_checkout_never_launches(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "MASKED_ROOTS", (project.parent.as_posix(),))
    launcher = FakeLauncher([])
    outcome = _run(project, "direct", launcher)
    assert (outcome.reason, outcome.detail) == (
        "isolation_unavailable",
        "checkout_masked",
    )
    assert launcher.calls == []


def test_masked_paths() -> None:
    assert runner._masked(Path("/tmp")) and runner._masked(Path("/tmp/repo"))
    assert runner._masked(Path("/proc/self/cwd"))
    assert runner._masked(Path("/dev"))
    assert runner._masked(Path("/run/user/1000/repo"))
    assert not runner._masked(Path("/tmpfoo/repo"))
    assert not runner._masked(Path("/runner/repo"))
    assert not runner._masked(Path("/home/user/repo"))


def test_masked_run_directory_never_launches(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = project / ".harness" / ".sandboxes"
    monkeypatch.setattr(runner, "MASKED_ROOTS", (runs.as_posix(),))
    outcome = _run(project, "direct", FakeLauncher([]))
    assert (outcome.reason, outcome.detail) == (
        "isolation_unavailable",
        "checkout_masked",
    )


def test_run_directory_failure_never_launches(project: Path) -> None:
    (project / ".harness" / ".sandboxes").write_text(
        "not a directory", encoding="utf-8"
    )
    launcher = FakeLauncher([])
    outcome = _run(project, "direct", launcher)
    assert (outcome.reason, outcome.detail) == (
        "isolation_unavailable",
        "sandbox_failed",
    )
    assert launcher.calls == []


@pytest.mark.parametrize(
    ("probe", "marker", "detail"),
    [
        (runner.Launch(started=False, exit_code=1), True, "sandbox_failed"),
        (runner.Launch(started=True, exit_code=1), True, "probe_failed"),
        (runner.Launch(started=True, exit_code=0), False, "probe_failed"),
        (
            runner.Launch(started=True, exit_code=None, timed_out=True),
            True,
            "probe_failed",
        ),
    ],
)
def test_failed_probe_never_runs_the_test(
    project: Path, probe: runner.Launch, marker: bool, detail: str
) -> None:
    launcher = FakeLauncher([probe], probe_marker=marker)
    outcome = _run(project, "direct", launcher)
    assert (outcome.result, outcome.reason, outcome.detail) == (
        "refused",
        "isolation_unavailable",
        detail,
    )
    assert len(launcher.calls) == 1
    assert outcome.run_id is not None
    assert not list(project.glob(".harness-isolation-probe-*"))


def test_probe_file_left_in_checkout_fails_the_probe(project: Path) -> None:
    def leaky(
        build: Callable[[int], list[str]],
        *,
        env: Mapping[str, str],
        log: Path,
        timeout: float,
        session: bool = False,
    ) -> runner.Launch:
        argv = build(7)
        Path(argv[-2]).write_text("escaped", encoding="utf-8")
        Path(argv[-1]).write_text("ok", encoding="utf-8")
        return STARTED

    outcome = runner.run_test(
        project, "direct", launcher=leaky, platform="linux", which=lambda name: "bwrap"
    )
    assert (outcome.reason, outcome.detail) == ("isolation_unavailable", "probe_failed")
    assert not list(project.glob(".harness-isolation-probe-*"))


def test_existing_probe_path_fails_the_probe(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "new_run_id", lambda: "0" * 16)
    blocker = project / f".harness-isolation-probe-{'0' * 16}"
    blocker.write_text("user file", encoding="utf-8")
    outcome = _run(project, "direct", FakeLauncher([]))
    assert outcome.detail == "probe_failed"
    assert blocker.read_text(encoding="utf-8") == "user file"


def test_test_without_child_pid_is_refused(project: Path) -> None:
    launcher = FakeLauncher([STARTED, runner.Launch(started=False, exit_code=1)])
    outcome = _run(project, "direct", launcher)
    assert (outcome.result, outcome.reason, outcome.detail) == (
        "refused",
        "isolation_unavailable",
        "sandbox_failed",
    )
    assert outcome.run_id is not None


# --- launched test -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("launch", "result", "reason", "exit_code", "status"),
    [
        (runner.Launch(True, 0), "passed", "test_passed", 0, 0),
        (runner.Launch(True, 5), "failed", "test_failed", 5, 1),
        (runner.Launch(True, None, timed_out=True), "timeout", "timeout", None, 1),
    ],
)
def test_test_result(
    project: Path,
    launch: runner.Launch,
    result: str,
    reason: str,
    exit_code: int | None,
    status: int,
) -> None:
    launcher = FakeLauncher([STARTED, launch])
    outcome = _run(project, "tests/test_x.py::test_y", launcher)
    assert (outcome.result, outcome.reason, outcome.exit_code, outcome.status) == (
        result,
        reason,
        exit_code,
        status,
    )
    assert outcome.test_id == "tests/test_x.py::test_y"
    assert outcome.detail is None


def test_probe_and_test_share_mounts_and_environment(project: Path) -> None:
    launcher = FakeLauncher([STARTED, STARTED])
    outcome = _run(project, "tests/test_x.py::test_y", launcher)
    assert outcome.run_id is not None
    run_dir = storage_path(project, "runs", f"isolated-test-{outcome.run_id}").resolve()
    probe_argv, test_argv = launcher.calls
    binds = [str(project / "tests"), str(run_dir / "cache"), str(run_dir / "reports")]
    for argv in (probe_argv, test_argv):
        assert _binds(argv) == binds
        assert argv[argv.index("--chdir") + 1] == str(project)
    assert test_argv[test_argv.index("--") + 1 :] == [
        sys.executable,
        "-B",
        "tests/attack.py",
        "tests/test_x.py::test_y",
    ]
    assert launcher.envs[0] == launcher.envs[1]
    assert "RUNNER_SECRET" not in launcher.envs[1]
    assert launcher.envs[1]["HARNESS_TEST_REPORT_DIR"] == str(run_dir / "reports")
    assert (run_dir / "cache").is_dir() and (run_dir / "reports").is_dir()


def test_probe_function(project: Path) -> None:
    launcher = FakeLauncher([STARTED])
    assert (
        runner.probe(
            project, launcher=launcher, platform="linux", which=lambda n: "bwrap"
        )
        is None
    )
    assert [Path(bind).name for bind in _binds(launcher.calls[0])] == [
        "tests",
        "cache",
        "reports",
    ]
    assert not list((project / ".harness" / ".sandboxes" / "runs").iterdir())
    assert runner.probe(project, launcher=launcher, platform="win32") == "platform"


# --- main and the shim -------------------------------------------------------------------------


def test_main_refuses_extra_arguments(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert runner.main([str(project), "direct", "-k"]) == runner.EXIT_REFUSED
    assert runner.main([str(project)]) == runner.EXIT_REFUSED
    lines = capsys.readouterr().out.splitlines()
    assert [json.loads(line)["reason"] for line in lines] == ["test_id_invalid"] * 2


def test_main_reports_a_runner_crash(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def crash(*args: object, **kwargs: object) -> None:
        raise RuntimeError("bug")

    monkeypatch.setattr(runner, "run_test", crash)
    monkeypatch.setattr(runner, "probe", crash)
    assert runner.main([str(project), "direct"]) == runner.EXIT_REFUSED
    assert runner.main([str(project), "--probe"]) == runner.EXIT_REFUSED
    first, second = capsys.readouterr().out.splitlines()
    assert json.loads(first)["reason"] == "runner_unavailable"
    assert second == runner.PROBE_RUNNER_UNAVAILABLE


def test_main_probe_unavailable(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(runner, "probe", lambda repo: "bwrap_missing")
    assert runner.main([str(project), "--probe"]) == runner.EXIT_REFUSED
    assert json.loads(capsys.readouterr().out)["detail"] == "bwrap_missing"


def _shim(repo: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(repo / ".claude" / "hooks" / "run-test.sh"), *arguments],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.mark.skipif(sys.platform == "win32", reason="the shim is checked on POSIX bash")
def test_shim_without_runner_module_refuses(project: Path) -> None:
    shutil.rmtree(project / ".harness" / "health")
    result = _shim(project, "direct")
    assert (result.returncode, result.stderr) == (3, "")
    assert json.loads(result.stdout) == {
        "test_id": None,
        "result": "refused",
        "reason": "runner_unavailable",
        "detail": None,
        "exit_code": None,
        "run_id": None,
    }
    probe = _shim(project, "--probe")
    assert probe.returncode == 3
    assert probe.stdout.strip() == runner.PROBE_RUNNER_UNAVAILABLE


@pytest.mark.skipif(sys.platform == "win32", reason="the shim is checked on POSIX bash")
def test_shim_loads_the_installed_runner(project: Path) -> None:
    harness = project / ".harness"
    shutil.copytree(
        Path(runner.__file__).parent,
        harness / "health",
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    for name in ("storage.py", "json_types.py", "configuration.py"):
        shutil.copyfile(ROOT / "harness" / name, harness / name)
    result = _shim(project, "a;b")
    assert (result.returncode, result.stderr) == (3, "")
    assert json.loads(result.stdout)["reason"] == "test_id_invalid"
    assert not list(harness.rglob("__pycache__"))


# --- the real launcher on a plain command ---------------------------------------------------------


def _plain(code: str) -> Callable[[int], list[str]]:
    """A command that gets the status fd number as its argument, without bwrap."""
    return lambda fd: [sys.executable, "-c", code, str(fd)]


REPORT_CHILD = "import os, sys; os.write(int(sys.argv[1]), b'{\"child-pid\": 2}\\n'); "


@pytest.mark.skipif(sys.platform == "win32", reason="pass_fds is POSIX-only")
def test_launch_streams_output_and_status(project: Path) -> None:
    log = project / "log.txt"
    result = runner.launch(
        _plain(REPORT_CHILD + "print('hello'); sys.exit(4)"),
        env={},
        log=log,
        timeout=30,
    )
    assert result == runner.Launch(started=True, exit_code=4)
    assert log.read_text(encoding="utf-8").strip() == "hello"


@pytest.mark.skipif(sys.platform == "win32", reason="pass_fds is POSIX-only")
def test_launch_without_child_pid_is_not_started(project: Path) -> None:
    result = runner.launch(
        _plain("import os, sys; os.write(int(sys.argv[1]), b'not json\\n{}\\n')"),
        env={},
        log=project / "log.txt",
        timeout=30,
    )
    assert result == runner.Launch(started=False, exit_code=0)


@pytest.mark.skipif(sys.platform == "win32", reason="pass_fds is POSIX-only")
def test_launch_caps_the_log(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "LOG_LIMIT_BYTES", 100)
    log = project / "log.txt"
    runner.launch(
        _plain(REPORT_CHILD + "sys.stdout.write('x' * 5000)"),
        env={},
        log=log,
        timeout=30,
    )
    assert log.read_bytes() == b"x" * 100 + runner.LOG_TRUNCATED


@pytest.mark.skipif(sys.platform == "win32", reason="pass_fds is POSIX-only")
def test_launch_kills_on_timeout(project: Path) -> None:
    result = runner.launch(
        _plain(REPORT_CHILD + "import time; time.sleep(60)"),
        env={},
        log=project / "log.txt",
        timeout=0.5,
    )
    assert result == runner.Launch(started=True, exit_code=None, timed_out=True)


@pytest.mark.skipif(sys.platform == "win32", reason="pass_fds is POSIX-only")
def test_launch_failures_are_not_started(project: Path) -> None:
    missing = runner.launch(
        lambda fd: [str(project / "no-such-program")],
        env={},
        log=project / "log.txt",
        timeout=30,
    )
    no_log = runner.launch(
        _plain(REPORT_CHILD), env={}, log=project / "missing" / "log.txt", timeout=30
    )
    assert missing == no_log == runner.Launch(started=False, exit_code=None)


# --- the real sandbox ---------------------------------------------------------------------------


@functools.cache
def _sandbox_detail() -> str | None:
    """The probe detail on this host; None when the real sandbox works."""
    base = storage_path(ROOT, "runs", "tests")
    base.mkdir(parents=True, exist_ok=True)
    repo = Path(tempfile.mkdtemp(prefix="iprobe-", dir=base))
    try:
        return runner.probe(repo)
    finally:
        shutil.rmtree(repo, ignore_errors=True)


@pytest.fixture
def sandboxed(project: Path) -> Path:
    detail = _sandbox_detail()
    if detail is not None:
        pytest.skip(f"isolation unavailable on this host: {detail}")
    return project


@pytest.mark.parametrize(
    "attack",
    [
        "direct",
        "child_sh",
        "child_py",
        "symlink",
        "hardlink",
        "git_config",
        "project_json",
        "hook",
        "module",
    ],
)
def test_sandbox_refuses_protected_writes(sandboxed: Path, attack: str) -> None:
    before = _snapshot(sandboxed)
    outcome = runner.run_test(sandboxed, attack)
    assert (outcome.result, outcome.reason) == ("failed", "test_failed")
    assert _snapshot(sandboxed) == before
    assert (sandboxed / "src" / "calc.py").read_text(encoding="utf-8") == ORIGINAL


def test_sandbox_allows_legitimate_writes(sandboxed: Path) -> None:
    outcome = runner.run_test(sandboxed, "legit")
    assert (outcome.result, outcome.exit_code) == ("passed", 0)
    assert outcome.run_id is not None
    run_dir = storage_path(sandboxed, "runs", f"isolated-test-{outcome.run_id}")
    for written in (
        run_dir / "cache" / "legit.txt",
        run_dir / "reports" / "legit.txt",
        sandboxed / "tests" / "legit.txt",
    ):
        assert written.read_text(encoding="utf-8") == "ok"
    assert (run_dir / "log.txt").is_file()
    assert not list(sandboxed.glob(".harness-isolation-probe-*"))


def test_sandbox_passes_only_the_allowlisted_environment(sandboxed: Path) -> None:
    outcome = runner.run_test(
        sandboxed, "environment", environ=dict(os.environ, RUNNER_SECRET="x")
    )
    assert outcome.result == "passed"


def test_sandbox_reports_exit_code_and_timeout(
    sandboxed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert runner.run_test(sandboxed, "fail").exit_code == 5
    monkeypatch.setattr(runner, "TIMEOUT_SECONDS", 1.0)
    outcome = runner.run_test(sandboxed, "sleep")
    assert (outcome.result, outcome.reason, outcome.status) == ("timeout", "timeout", 1)


def test_sandbox_main_prints_one_line(
    sandboxed: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert runner.main([str(sandboxed), "--probe"]) == runner.EXIT_PASSED
    assert runner.main([str(sandboxed), "direct"]) == runner.EXIT_FAILED
    probe_line, run_line = capsys.readouterr().out.splitlines()
    assert json.loads(probe_line) == {"status": "ok"}
    assert str(sandboxed) not in run_line
    assert json.loads(run_line)["result"] == "failed"


def test_sandbox_hides_host_service_sockets(sandboxed: Path) -> None:
    outcome = runner.run_test(sandboxed, "hidden_roots")
    assert (outcome.result, outcome.reason) == ("passed", "test_passed")


def _session_bus_reachable() -> bool:
    """True when this process reaches the host D-Bus session socket: the control of the attack."""
    if sys.platform == "win32":
        return False
    try:
        with socket.socket(socket.AF_UNIX) as sock:
            sock.connect(f"/run/user/{os.getuid()}/bus")
    except OSError:
        return False
    return True


def test_sandbox_refuses_the_session_bus(sandboxed: Path) -> None:
    if not _session_bus_reachable():
        pytest.skip("no reachable D-Bus session socket on this host")
    outcome = runner.run_test(sandboxed, "session_bus")
    assert (outcome.result, outcome.reason) == ("failed", "test_failed")


def test_probe_fails_when_host_sockets_are_visible(
    sandboxed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not os.listdir("/run"):
        pytest.skip("the host /run is empty")
    adr_argv = runner.sandbox_argv

    def without_run(*args: object, **kwargs: object) -> list[str]:
        argv = adr_argv(*args, **kwargs)  # type: ignore[arg-type]
        index = argv.index("/run")
        return argv[: index - 1] + argv[index + 1 :]

    monkeypatch.setattr(runner, "sandbox_argv", without_run)
    assert runner.probe(sandboxed) == "probe_failed"
    assert not list(sandboxed.glob(".harness-isolation-probe-*"))


def test_build_precedes_test_with_private_artifacts(project: Path) -> None:
    (project / "artifacts").mkdir()
    data = _settings(["tests/**"], ["test", "{test_id}"])
    cast(dict[str, object], data["test_runner"])["build"] = {
        "command": ["compiler", "src/calc.py", "artifacts/out"],
        "artifact_paths": ["artifacts/**"],
    }
    _write_settings(project, data)
    launcher = FakeLauncher([STARTED] * 4)
    outcome = _run(project, "legit", launcher)
    assert outcome.result == "passed"
    assert len(launcher.calls) == 4
    preparation, selected = launcher.calls[1], launcher.calls[3]
    assert preparation[-3:] == ["compiler", "src/calc.py", "artifacts/out"]
    assert str(project / "tests") not in _binds(preparation)
    target = str(project / "artifacts")
    offset = preparation.index(target)
    backing = preparation[offset - 1]
    assert preparation[offset - 2] == "--bind" and backing != target
    offset = selected.index(target)
    assert selected[offset - 2 : offset] == ["--ro-bind", backing]
    assert list((project / "artifacts").iterdir()) == []


@pytest.mark.parametrize(
    "built,reason",
    [
        (runner.Launch(True, 7), "build_failed"),
        (runner.Launch(True, None, True), "build_timeout"),
        (runner.Launch(False, 1), "isolation_unavailable"),
    ],
)
def test_failed_preparation_never_starts_selected_test(
    project: Path, built: runner.Launch, reason: str
) -> None:
    (project / "artifacts").mkdir()
    data = _settings(["tests/**"], ["test", "{test_id}"])
    cast(dict[str, object], data["test_runner"])["build"] = {
        "command": ["compiler"],
        "artifact_paths": ["artifacts/**"],
    }
    _write_settings(project, data)
    launcher = FakeLauncher([STARTED, built])
    outcome = _run(project, "legit", launcher)
    assert outcome.result == "refused" and outcome.reason == reason
    assert len(launcher.calls) == 2


def test_preparation_probe_checks_its_artifact_mount(
    sandboxed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (sandboxed / "artifacts").mkdir()
    data = _settings(["tests/**"], ["test", "{test_id}"])
    cast(dict[str, object], data["test_runner"])["build"] = {
        "command": ["compiler"],
        "artifact_paths": ["artifacts/**"],
    }
    _write_settings(sandboxed, data)
    original = runner.sandbox_argv

    def wrong_mount(
        bwrap: str,
        repo: Path,
        binds: Sequence[Path | runner.Mount],
        command: Sequence[str],
        status_fd: int,
    ) -> list[str]:
        argv = original(bwrap, repo, binds, command, status_fd)
        target = str(sandboxed / "artifacts")
        if target in argv:
            argv[argv.index(target) - 2] = "--ro-bind"
        return argv

    monkeypatch.setattr(runner, "sandbox_argv", wrong_mount)
    outcome = runner.run_test(sandboxed, "legit")
    assert (outcome.reason, outcome.detail) == ("isolation_unavailable", "probe_failed")


@pytest.mark.parametrize(
    "target",
    [
        "src/**",
        ".harness/**",
        "tests/**",
        "tests/nested/**",
        "artifacts/nested/**",
        "alias/**",
    ],
)
def test_unsafe_artifact_targets_refuse_without_launch(
    project: Path, target: str
) -> None:
    (project / "artifacts").mkdir()
    if target == "artifacts/nested/**":
        (project / "artifacts/nested").mkdir()
    (project / "tests/nested").mkdir()
    (project / "alias").symlink_to(project / "artifacts", target_is_directory=True)
    data = _settings(["tests/**"], ["test", "{test_id}"])
    cast(dict[str, object], data["test_runner"])["build"] = {
        "command": ["compiler"],
        "artifact_paths": ["artifacts/**", target],
    }
    _write_settings(project, data)
    before = _snapshot(project)
    launcher = FakeLauncher([])
    assert _run(project, "legit", launcher).reason == "write_zone_rejected"
    assert launcher.calls == [] and _snapshot(project) == before


@pytest.mark.parametrize(
    "attack", ["direct", "child_py", "symlink", "project_json", "module"]
)
def test_preparation_cannot_write_protected_files(sandboxed: Path, attack: str) -> None:
    (sandboxed / "artifacts").mkdir()
    data = _settings(["tests/**"], [sys.executable, "tests/attack.py", "{test_id}"])
    cast(dict[str, object], data["test_runner"])["build"] = {
        "command": [sys.executable, "tests/attack.py", attack],
        "artifact_paths": ["artifacts/**"],
    }
    _write_settings(sandboxed, data)
    before = _snapshot(sandboxed)
    outcome = runner.run_test(sandboxed, "legit")
    assert outcome.reason == "build_failed"
    assert _snapshot(sandboxed) == before


@pytest.mark.parametrize(
    "test_id,uses_database", [("sql.constraints", True), ("domain.add", False)]
)
def test_only_matching_selected_tests_start_database(
    project: Path, test_id: str, uses_database: bool
) -> None:
    data = _settings(["tests/**"], ["test", "{test_id}"])
    cast(dict[str, object], data["test_runner"])["database"] = {
        "engine": "postgresql",
        "test_ids": ["sql.*"],
        "command": ["foreground-db"],
        "ready_command": ["ready"],
    }
    _write_settings(project, data)
    launcher = FakeLauncher([STARTED, STARTED])
    assert _run(project, test_id, launcher).result == "passed"
    selected = launcher.calls[-1]
    assert (
        any(part.endswith("isolated_runner_session.py") for part in selected)
        is uses_database
    )
    assert "RUNNER_SECRET" not in launcher.envs[-1]


def _fake_database_settings(project: Path, server: str, selected: str) -> None:
    data = _settings(["tests/**"], [sys.executable, "-c", selected, "{test_id}"])
    cast(dict[str, object], data["test_runner"])["database"] = {
        "engine": "postgresql",
        "test_ids": ["sql.*"],
        "command": [sys.executable, "-c", server],
        "ready_command": [
            sys.executable,
            "-c",
            "import os,sys; sys.exit(not os.path.exists(os.environ['HARNESS_TEST_DATABASE_DIR']+'/ready'))",
        ],
    }
    _write_settings(project, data)


FAKE_DATABASE = "import os,time; open(os.environ['HARNESS_TEST_DATABASE_DIR']+'/ready','w').close(); time.sleep(120)"


def test_private_database_lifecycle_and_result_channel(sandboxed: Path) -> None:
    selected = """import os,sys
from pathlib import Path
from typing import Sequence, cast
assert os.environ['TMPDIR'] == SANDBOX_TMP
assert os.environ['HARNESS_TEST_DATABASE_DIR'].startswith(os.environ['TMPDIR'] + '/harness-db-')
assert os.path.isdir(os.environ['HARNESS_TEST_DATABASE_SOCKET_DIR'])
try:
    open('/proc/%d/fd/1' % os.getppid(), 'w')
except PermissionError:
    pass
else:
    raise AssertionError('helper result channel exposed')
print('{"database":"unavailable"}')
Path(os.environ['HARNESS_TEST_REPORT_DIR']+'/dbdir').write_text(os.environ['HARNESS_TEST_DATABASE_DIR'])
sys.exit(3)
""".replace("SANDBOX_TMP", repr(runner.SANDBOX_TMP))
    _fake_database_settings(sandboxed, FAKE_DATABASE, selected)
    first = runner.run_test(sandboxed, "sql.constraint")
    assert (first.result, first.reason, first.exit_code) == ("failed", "test_failed", 3)
    second = runner.run_test(sandboxed, "sql.constraint")
    directories = [
        storage_path(sandboxed, "runs", f"isolated-test-{item.run_id}")
        / "reports/dbdir"
        for item in (first, second)
    ]
    assert directories[0].read_text() != directories[1].read_text()


def test_database_start_failure_refuses_before_selected_test(sandboxed: Path) -> None:
    _fake_database_settings(
        sandboxed, "raise SystemExit(4)", "open('tests/should-not-run','w').close()"
    )
    outcome = runner.run_test(sandboxed, "sql.constraint")
    assert outcome.reason == "database_unavailable" and outcome.result == "refused"
    assert not (sandboxed / "tests/should-not-run").exists()


def test_probe_refuses_unsafe_artifact_settings_without_running_commands(
    project: Path,
) -> None:
    (project / "artifacts").mkdir()
    (project / "artifacts/source.py").write_text("protected")
    data = _settings(["tests/**"], ["test", "{test_id}"])
    cast(dict[str, object], data["test_runner"])["build"] = {
        "command": ["compiler"],
        "artifact_paths": ["artifacts/**"],
    }
    _write_settings(project, data)
    launcher = FakeLauncher([STARTED])
    assert runner.probe(project, launcher=launcher) == "write_zone_rejected"
    assert launcher.calls == []


def test_probe_checks_both_phases_without_build_or_database_start(
    project: Path,
) -> None:
    (project / "artifacts").mkdir()
    data = _settings(["tests/**"], ["never-test", "{test_id}"])
    cast(dict[str, object], data["test_runner"])["build"] = {
        "command": ["never-build"],
        "artifact_paths": ["artifacts/**"],
    }
    cast(dict[str, object], data["test_runner"])["database"] = {
        "engine": "postgresql",
        "test_ids": ["*"],
        "command": ["never-db"],
        "ready_command": ["never-ready"],
    }
    _write_settings(project, data)
    launcher = FakeLauncher([STARTED, STARTED])
    assert (
        runner.probe(
            project, launcher=launcher, platform="linux", which=lambda n: "bwrap"
        )
        is None
    )
    assert len(launcher.calls) == 2
    assert all(runner.PROBE_SCRIPT in command for command in launcher.calls)
    assert not any("never-" in part for command in launcher.calls for part in command)


def test_probe_sanitizes_configuration_refusal(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = _settings(["tests/**"], ["test", "{test_id}"])
    cast(dict[str, object], data["test_runner"])["database"] = {"secret": "not-logged"}
    _write_settings(project, data)
    assert runner.main([str(project), "--probe"]) == 3
    assert json.loads(capsys.readouterr().out) == {
        "status": "unavailable",
        "reason": "config_invalid",
        "detail": None,
    }
