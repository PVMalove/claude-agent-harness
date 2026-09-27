"""Windows checks of the group 'environment' (#344): LongPathsEnabled, repository path headroom,
pytest-of-<user>, symlink creation and the `bash` hooks run through. Process cases run
`harness health --json`; the Windows-only ones are skipped elsewhere. Decision logic is also
covered in-process on every OS by pretending to be Windows and stubbing the machine probes."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

import pytest

from harness.health.checks import files, windows
from harness.health.context import HealthContext
from harness.health.model import CheckResult

HARNESS = Path(__file__).resolve().parents[1] / "harness" / "bin" / "harness.py"
REAL_GIT = shutil.which("git")
WINDOWS_CHECK_IDS = (
    "environment.long_paths",
    "environment.path_length",
    "environment.pytest_temp",
    "environment.symlinks",
    "environment.hook_bash",
)
only_windows = pytest.mark.skipif(os.name != "nt", reason="Windows-only check")


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    assert REAL_GIT is not None
    subprocess.run([REAL_GIT, "init", "-q"], cwd=path, check=True)
    return path


def _health(
    repo: Path, extra_env: dict[str, str] | None = None
) -> dict[str, dict[str, object]]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in ("PYTHONIOENCODING", "GIT_DIR", "GIT_WORK_TREE")
    }
    env["PYTHONUTF8"] = "1"
    for key, value in (extra_env or {}).items():
        # Windows environment names are case-insensitive: drop the inherited spelling first.
        for inherited in [name for name in env if name.casefold() == key.casefold()]:
            del env[inherited]
        env[key] = value
    result = subprocess.run(
        [sys.executable, str(HARNESS), "health", str(repo), "--json"],
        env=env,
        capture_output=True,
        check=False,
    )
    data = json.loads(result.stdout.decode("utf-8"))
    return {check["id"]: check for check in data["checks"]}


def _fix(check: dict[str, object]) -> dict[str, object]:
    fix = check["fix"]
    assert isinstance(fix, dict)
    return fix


# --- through the process -----------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="the non-Windows branch")
def test_windows_checks_are_skipped_outside_windows(tmp_path: Path) -> None:
    checks = _health(_repo(tmp_path / "repo"))

    for check_id in WINDOWS_CHECK_IDS:
        assert checks[check_id]["status"] == "skipped", check_id
        assert checks[check_id]["group"] == "environment"
        assert checks[check_id]["fix"] is None


@only_windows
def test_windows_checks_report_the_real_machine(tmp_path: Path) -> None:
    checks = _health(_repo(tmp_path / "repo"))

    for check_id in WINDOWS_CHECK_IDS:
        assert checks[check_id]["status"] != "skipped", check_id
    long_paths = checks["environment.long_paths"]
    if long_paths["status"] == "warn":
        assert "LongPathsEnabled" in str(_fix(long_paths)["command"])
    symlinks = checks["environment.symlinks"]
    if symlinks["status"] != "ok":
        assert "AllowDevelopmentWithoutDevLicense" in str(_fix(symlinks)["command"])


@only_windows
def test_deep_repository_path_warns(tmp_path: Path) -> None:
    deep = tmp_path
    while windows.path_headroom(str(deep.resolve())) >= windows.MIN_PATH_HEADROOM:
        deep = deep / ("d" * 20)
    checks = _health(_repo(deep))

    path_length = checks["environment.path_length"]
    assert path_length["status"] == "warn"
    assert path_length["fix"] is not None


@only_windows
def test_own_pytest_temp_root_is_ok(tmp_path: Path) -> None:
    temproot = tmp_path / "temproot"
    (temproot / f"pytest-of-{windows._current_user()}").mkdir(parents=True)

    checks = _health(_repo(tmp_path / "repo"), {"PYTEST_DEBUG_TEMPROOT": str(temproot)})

    pytest_temp = checks["environment.pytest_temp"]
    assert pytest_temp["status"] == "ok", pytest_temp
    assert "записываем" in str(pytest_temp["message"])


def _fake_exe(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


@only_windows
def test_bash_on_path_before_system32_is_ok(tmp_path: Path) -> None:
    bash = _fake_exe(tmp_path / "bin" / "bash.exe")
    system32 = Path(os.environ["SystemRoot"]) / "System32"

    checks = _health(
        _repo(tmp_path / "repo"),
        {"PATH": os.pathsep.join([str(bash.parent), str(system32)])},
    )

    hook_bash = checks["environment.hook_bash"]
    assert hook_bash["status"] == "ok"
    assert str(bash).casefold() in str(hook_bash["message"]).casefold()


def _with_program_files(env: dict[str, str], program_files: Path) -> None:
    # A 64-bit Windows process gets ProgramFiles re-derived from ProgramW6432 at startup, so an
    # override of ProgramFiles alone never reaches the child: set both, as on a real machine.
    env["ProgramFiles"] = str(program_files)
    env["ProgramW6432"] = str(program_files)


def _no_git_install(tmp_path: Path) -> dict[str, str]:
    empty = tmp_path / "empty"
    empty.mkdir(exist_ok=True)
    return {
        variable: str(empty)
        for variable in (
            "ProgramW6432",
            "ProgramFiles",
            "ProgramFiles(x86)",
            "LOCALAPPDATA",
        )
    }


@only_windows
def test_missing_bash_warns_and_points_to_git_bash(tmp_path: Path) -> None:
    program_files = tmp_path / "pf"
    git_bash = _fake_exe(program_files / "Git" / "bin" / "bash.exe")
    env = _no_git_install(tmp_path)
    _with_program_files(env, program_files)
    env["PATH"] = str(tmp_path / "empty")

    checks = _health(_repo(tmp_path / "repo"), env)

    hook_bash = checks["environment.hook_bash"]
    assert hook_bash["status"] == "warn"
    assert f"Git Bash найден: {git_bash}" in str(hook_bash["message"])
    assert str(git_bash.parent) in str(_fix(hook_bash)["command"])


@only_windows
@pytest.mark.parametrize("git_bash_installed", [True, False])
def test_wsl_bash_stub_on_path_fails(tmp_path: Path, git_bash_installed: bool) -> None:
    system32 = Path(os.environ["SystemRoot"]) / "System32"
    if not (system32 / "bash.exe").is_file():
        pytest.skip("no WSL bash.exe stub in System32 on this machine")
    env = _no_git_install(tmp_path)
    if git_bash_installed:
        program_files = tmp_path / "pf"
        _fake_exe(program_files / "Git" / "bin" / "bash.exe")
        _with_program_files(env, program_files)
    env["PATH"] = str(system32)

    checks = _health(_repo(tmp_path / "repo"), env)

    hook_bash = checks["environment.hook_bash"]
    assert hook_bash["status"] == "fail"
    message = str(hook_bash["message"])
    assert "WSL" in message
    if git_bash_installed:
        assert "Git Bash найден" in message
        assert _fix(hook_bash)["command"] is not None
    else:
        assert "Git Bash не найден" in message
        assert "git-scm.com" in str(_fix(hook_bash)["text"])


# --- decision logic on every OS ----------------------------------------------------------------


@pytest.fixture
def as_windows(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setattr(windows, "_is_windows", lambda: True)
    return monkeypatch


def _context(repo: Path) -> HealthContext:
    return HealthContext(repo=repo, lock=None, online=False)


@pytest.mark.parametrize(
    "check",
    [
        windows.check_long_paths,
        windows.check_path_length,
        windows.check_pytest_temp,
        windows.check_symlinks,
        windows.check_hook_bash,
    ],
)
def test_every_check_is_skipped_when_not_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    check: Callable[[HealthContext], CheckResult],
) -> None:
    monkeypatch.setattr(windows, "_is_windows", lambda: False)

    assert check(_context(tmp_path)).status == "skipped"


@pytest.mark.parametrize(("value", "status"), [(1, "ok"), (0, "warn"), (None, "warn")])
def test_long_paths(
    tmp_path: Path, as_windows: pytest.MonkeyPatch, value: int | None, status: str
) -> None:
    as_windows.setattr(windows, "_long_paths_enabled", lambda: value)

    result = windows.check_long_paths(_context(tmp_path))

    assert result.status == status
    if status == "warn":
        assert result.fix is not None and result.fix.command is not None
        assert "LongPathsEnabled -Value 1" in result.fix.command


def test_path_headroom_counts_separator_and_nul() -> None:
    assert windows.path_headroom("C:\\" + "a" * 97) == 158
    assert windows.path_headroom("C:\\src\\repo\\") == 247


def test_path_length_warns_below_minimum_headroom(
    tmp_path: Path, as_windows: pytest.MonkeyPatch
) -> None:
    as_windows.setattr(windows, "MIN_PATH_HEADROOM", 10_000)

    result = windows.check_path_length(_context(tmp_path))

    assert result.status == "warn"
    assert result.fix is not None
    assert str(len(str(tmp_path.resolve()))) in result.message


def test_path_length_ok_with_enough_headroom(
    tmp_path: Path, as_windows: pytest.MonkeyPatch
) -> None:
    as_windows.setattr(windows, "MIN_PATH_HEADROOM", 0)

    assert windows.check_path_length(_context(tmp_path)).status == "ok"


def test_pytest_temp_root_matches_pytest_naming() -> None:
    assert windows.pytest_temp_root("T", "alice") == Path("T") / "pytest-of-alice"
    assert windows.pytest_temp_root("T", "") == Path("T") / "pytest-of-unknown"


@pytest.mark.parametrize(
    ("owner", "user", "expected"),
    [
        (windows.Owner("HOST\\alice", True), "alice", False),
        (windows.Owner("HOST\\Alice", True), "alice", False),
        (windows.Owner("alice", True), "alice", False),
        (windows.Owner("HOST\\bob", True), "alice", True),
        (windows.Owner("BUILTIN\\Administrators", False), "alice", False),
    ],
)
def test_is_other_user(owner: windows.Owner, user: str, expected: bool) -> None:
    assert windows.is_other_user(owner, user) is expected


def _pytest_temp(
    monkeypatch: pytest.MonkeyPatch,
    temproot: Path,
    *,
    owner: windows.Owner | None = windows.Owner("HOST\\alice", True),
    writable: bool = True,
) -> CheckResult:
    monkeypatch.setattr(windows, "_current_user", lambda: "alice")
    monkeypatch.setattr(windows, "_temproot", lambda: str(temproot))
    monkeypatch.setattr(windows, "_path_owner", lambda _path: owner)
    monkeypatch.setattr(windows, "_is_writable", lambda _path: writable)
    return windows.check_pytest_temp(_context(temproot))


def test_pytest_temp_owned_by_another_user_warns_with_owner(
    tmp_path: Path, as_windows: pytest.MonkeyPatch
) -> None:
    (tmp_path / "pytest-of-alice").mkdir()

    result = _pytest_temp(as_windows, tmp_path, owner=windows.Owner("HOST\\bob", True))

    assert result.status == "warn"
    assert "HOST\\bob" in result.message
    assert result.fix is not None and result.fix.command is not None
    assert "pytest-of-alice" in result.fix.command


def test_unwritable_pytest_temp_warns_with_owner(
    tmp_path: Path, as_windows: pytest.MonkeyPatch
) -> None:
    (tmp_path / "pytest-of-alice").mkdir()

    result = _pytest_temp(as_windows, tmp_path, writable=False)

    assert result.status == "warn"
    assert "не записываем" in result.message
    assert "HOST\\alice" in result.message


def test_writable_pytest_temp_owned_by_a_group_is_ok(
    tmp_path: Path, as_windows: pytest.MonkeyPatch
) -> None:
    (tmp_path / "pytest-of-alice").mkdir()
    administrators = windows.Owner("BUILTIN\\Administrators", False)

    result = _pytest_temp(as_windows, tmp_path, owner=administrators)

    assert result.status == "ok"
    assert "BUILTIN\\Administrators" in result.message


def test_own_writable_pytest_temp_is_ok(
    tmp_path: Path, as_windows: pytest.MonkeyPatch
) -> None:
    (tmp_path / "pytest-of-alice").mkdir()

    result = _pytest_temp(as_windows, tmp_path)

    assert result.status == "ok"
    assert "HOST\\alice" in result.message


@pytest.mark.parametrize(("writable", "status"), [(True, "ok"), (False, "warn")])
def test_absent_pytest_temp_depends_on_temproot(
    tmp_path: Path, as_windows: pytest.MonkeyPatch, writable: bool, status: str
) -> None:
    assert _pytest_temp(as_windows, tmp_path, writable=writable).status == status


def test_pytest_temp_that_is_a_file_warns(
    tmp_path: Path, as_windows: pytest.MonkeyPatch
) -> None:
    (tmp_path / "pytest-of-alice").write_text("", encoding="utf-8")

    assert _pytest_temp(as_windows, tmp_path).status == "warn"


def test_is_writable_probe_leaves_nothing_behind(tmp_path: Path) -> None:
    assert windows._is_writable(tmp_path) is True
    assert list(tmp_path.iterdir()) == []
    assert windows._is_writable(tmp_path / "missing") is False


def test_symlink_probe_succeeds_where_symlinks_work() -> None:
    if os.name == "nt":
        pytest.skip("depends on Developer Mode on Windows")
    assert windows._symlink_error() is None


def _link_discovery(repo: Path) -> None:
    (repo / ".harness" / "skills").mkdir(parents=True)
    for relative, target in files.DISCOVERY_LINKS.items():
        link = repo / relative
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(files.native_link_target(target))


@pytest.mark.skipif(os.name == "nt", reason="creates real symlinks")
@pytest.mark.parametrize(
    ("links_ok", "probe_error", "status"),
    [
        (True, None, "ok"),
        (True, "A required privilege is not held by the client", "warn"),
        (False, "A required privilege is not held by the client", "fail"),
    ],
)
def test_symlinks(
    tmp_path: Path,
    as_windows: pytest.MonkeyPatch,
    links_ok: bool,
    probe_error: str | None,
    status: str,
) -> None:
    if links_ok:
        _link_discovery(tmp_path)
    as_windows.setattr(windows, "_symlink_error", lambda: probe_error)

    result = windows.check_symlinks(_context(tmp_path))

    assert result.status == status
    if status == "ok":
        return
    assert result.fix is not None and result.fix.command is not None
    assert "AllowDevelopmentWithoutDevLicense" in result.fix.command
    assert probe_error is not None and probe_error in result.message
    if status == "fail":
        assert ".claude/skills" in result.message
        assert "причина" in result.message


@pytest.mark.parametrize(
    ("bash", "expected"),
    [
        ("C:\\Windows\\System32\\bash.exe", True),
        ("c:\\windows\\system32\\BASH.EXE", True),
        ("C:\\Windows\\Sysnative\\bash.exe", True),
        ("C:\\Windows\\System32\\..\\System32\\bash.exe", True),
        ("C:\\Program Files\\Git\\bin\\bash.exe", False),
        ("D:\\Windows\\System32\\bash.exe", False),
    ],
)
def test_is_wsl_stub(bash: str, expected: bool) -> None:
    assert windows.is_wsl_stub(bash, "C:\\Windows") is expected


def test_git_bash_candidates_start_next_to_git() -> None:
    environ = {
        "ProgramFiles": "C:\\Program Files",
        "ProgramW6432": "C:\\Program Files",
        "LOCALAPPDATA": "C:\\Users\\alice\\AppData\\Local",
    }

    candidates = windows.git_bash_candidates("D:\\Tools\\Git\\cmd\\git.exe", environ)

    assert candidates[0] == "D:\\Tools\\Git\\bin\\bash.exe"
    assert "C:\\Program Files\\Git\\bin\\bash.exe" in candidates
    assert (
        "C:\\Users\\alice\\AppData\\Local\\Programs\\Git\\bin\\bash.exe" in candidates
    )
    assert len(candidates) == len({candidate.casefold() for candidate in candidates})


def test_git_bash_candidates_from_mingw_git() -> None:
    candidates = windows.git_bash_candidates("C:\\Git\\mingw64\\bin\\git.exe", {})

    assert "C:\\Git\\bin\\bash.exe" in candidates


@only_windows
def test_find_git_bash_under_program_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git_bash = _fake_exe(tmp_path / "pf" / "Git" / "bin" / "bash.exe")
    for variable, value in _no_git_install(tmp_path).items():
        monkeypatch.setenv(variable, value)
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "pf"))
    monkeypatch.setattr(shutil, "which", lambda name: None)

    assert windows._find_git_bash() == str(git_bash)


def _hook_bash(
    monkeypatch: pytest.MonkeyPatch, bash: str | None, git_bash: str | None
) -> CheckResult:
    monkeypatch.setattr(shutil, "which", lambda name: bash if name == "bash" else None)
    monkeypatch.setattr(windows, "_system_root", lambda: "C:\\Windows")
    monkeypatch.setattr(windows, "_find_git_bash", lambda: git_bash)
    return windows.check_hook_bash(_context(Path(".")))


GIT_BASH = "C:\\Program Files\\Git\\bin\\bash.exe"


def test_hook_bash_ok_for_git_bash(as_windows: pytest.MonkeyPatch) -> None:
    result = _hook_bash(as_windows, GIT_BASH, GIT_BASH)

    assert result.status == "ok"
    assert GIT_BASH in result.message


@pytest.mark.parametrize("git_bash", [GIT_BASH, None])
def test_hook_bash_fails_on_wsl_stub(
    as_windows: pytest.MonkeyPatch, git_bash: str | None
) -> None:
    result = _hook_bash(as_windows, "C:\\Windows\\System32\\bash.exe", git_bash)

    assert result.status == "fail"
    assert result.fix is not None
    if git_bash is None:
        assert "Git Bash не найден" in result.message
        assert result.fix.command is None
    else:
        assert f"Git Bash найден: {git_bash}" in result.message
        assert result.fix.command is not None
        assert "C:\\Program Files\\Git\\bin;" in result.fix.command


def test_hook_bash_warns_when_bash_is_missing(as_windows: pytest.MonkeyPatch) -> None:
    result = _hook_bash(as_windows, None, GIT_BASH)

    assert result.status == "warn"
    assert "bash не найден в PATH" in result.message
