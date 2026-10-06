"""Public health diagnostics of user sandbox settings, without changing machine state."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from harness.health import registry
from harness.health.model import CheckResult
from harness.health.render import render_text


@pytest.fixture
def runtime_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Outside the project: a home inside it would make every home cache "inside the project".
    home = tmp_path.with_name(f"{tmp_path.name}-user")
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    original_which = shutil.which
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: f"/tools/{name}"
        if name in {"codex", "claude"}
        else original_which(name),
    )
    return home


def _checks(repo: Path, *, fix: bool = False) -> dict[str, CheckResult]:
    return {check.id: check for check in registry.run(repo, fix=fix).checks}


@pytest.mark.parametrize(
    ("runtime", "config", "snippet"),
    [
        ("codex", ".codex/config.toml", "network_access = true"),
        ("claude", ".claude/settings.json", '"allowAllUnixSockets": true'),
    ],
)
def test_missing_user_config_warns_with_path_and_steps_without_creating_it(
    tmp_path: Path, runtime_home: Path, runtime: str, config: str, snippet: str
) -> None:
    result = _checks(tmp_path)[f"environment.{runtime}_sandbox"]
    assert result.status == "warn"
    assert result.fix is not None
    assert str(runtime_home / config) in result.fix.text
    assert snippet in result.fix.text
    assert "новую сессию" in result.fix.text
    assert not (runtime_home / config).exists()


def test_valid_settings_are_ok_and_health_fix_preserves_user_files(
    tmp_path: Path, runtime_home: Path
) -> None:
    codex = runtime_home / ".codex/config.toml"
    claude = runtime_home / ".claude/settings.json"
    codex.parent.mkdir()
    claude.parent.mkdir()
    codex.write_text(
        'sandbox_mode = "workspace-write"\n[sandbox_workspace_write]\nnetwork_access = true\n',
        encoding="utf-8",
    )
    claude.write_text(
        json.dumps(
            {"sandbox": {"enabled": True, "network": {"allowAllUnixSockets": True}}}
        ),
        encoding="utf-8",
    )
    before = {path: path.read_bytes() for path in (codex, claude)}

    results = _checks(tmp_path, fix=True)

    for runtime in ("codex", "claude"):
        result = results[f"environment.{runtime}_sandbox"]
        assert result.status == "ok"
        assert result.fix is None
    assert {path: path.read_bytes() for path in before} == before


@pytest.mark.parametrize(
    "contents",
    [
        "",
        'sandbox_mode = "read-only"\n[sandbox_workspace_write]\nnetwork_access = true\n',
        'sandbox_mode = "danger-full-access"\n',
        'sandbox_mode = "workspace-write"\n',
        'sandbox_mode = "workspace-write"\n[sandbox_workspace_write]\nnetwork_access = false\n',
        'sandbox_mode = "workspace-write"\n[sandbox_workspace_write]\nnetwork_access = "true"\n',
    ],
)
def test_codex_missing_or_wrong_settings_warn(
    tmp_path: Path, runtime_home: Path, contents: str
) -> None:
    path = runtime_home / ".codex/config.toml"
    path.parent.mkdir()
    path.write_text(contents, encoding="utf-8")
    assert _checks(tmp_path)["environment.codex_sandbox"].status == "warn"


@pytest.mark.parametrize(
    "sandbox",
    [
        {},
        {"enabled": False, "network": {"allowAllUnixSockets": True}},
        {"enabled": True},
        {"enabled": True, "network": {"allowAllUnixSockets": False}},
        {"enabled": True, "network": {"allowAllUnixSockets": "true"}},
        {
            "enabled": True,
            "filesystem": {"disabled": True},
            "network": {"allowAllUnixSockets": True},
        },
    ],
)
def test_claude_missing_or_wrong_settings_warn(
    tmp_path: Path, runtime_home: Path, sandbox: dict[str, object]
) -> None:
    path = runtime_home / ".claude/settings.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"sandbox": sandbox}), encoding="utf-8")
    assert _checks(tmp_path)["environment.claude_sandbox"].status == "warn"


@pytest.mark.parametrize(
    ("runtime", "config", "contents"),
    [
        ("codex", ".codex/config.toml", b'api_key = "SECRET"\nsandbox_mode = [broken'),
        ("codex", ".codex/config.toml", b"\xffSECRET"),
        ("claude", ".claude/settings.json", b'{"api_key": "SECRET", broken}'),
        ("claude", ".claude/settings.json", b'["SECRET"]'),
        ("claude", ".claude/settings.json", b"\xffSECRET"),
    ],
)
def test_invalid_config_warns_without_disclosing_contents(
    tmp_path: Path, runtime_home: Path, runtime: str, config: str, contents: bytes
) -> None:
    path = runtime_home / config
    path.parent.mkdir()
    path.write_bytes(contents)
    report = registry.run(tmp_path)
    result = next(
        check for check in report.checks if check.id == f"environment.{runtime}_sandbox"
    )
    assert result.status == "warn"
    assert result.fix is not None
    assert "SECRET" not in render_text(report)


def test_config_directories_respect_runtime_environment_overrides(
    tmp_path: Path, runtime_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for runtime, variable in (("codex", "CODEX_HOME"), ("claude", "CLAUDE_CONFIG_DIR")):
        directory = tmp_path / f"custom-{runtime}"
        monkeypatch.setenv(variable, str(directory))
        result = _checks(tmp_path)[f"environment.{runtime}_sandbox"]
        assert result.fix is not None
        assert str(directory) in result.fix.text
        assert str(runtime_home) not in result.fix.text


@pytest.mark.parametrize(
    ("runtime", "config"),
    [("codex", ".codex/config.toml"), ("claude", ".claude/settings.json")],
)
def test_unreadable_config_warns_without_disclosing_exception(
    tmp_path: Path,
    runtime_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime: str,
    config: str,
) -> None:
    path = runtime_home / config
    original_read = Path.read_text

    def read_text(self: Path, *args: object, **kwargs: object) -> str:
        if self == path:
            raise PermissionError("SECRET")
        return original_read(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", read_text)
    report = registry.run(tmp_path)
    result = next(
        check for check in report.checks if check.id == f"environment.{runtime}_sandbox"
    )
    assert result.status == "warn"
    assert "недоступен для чтения" in result.message
    assert result.fix is not None
    assert "SECRET" not in render_text(report)


def test_uninstalled_runtimes_without_config_are_skipped(
    tmp_path: Path, runtime_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    results = _checks(tmp_path)
    assert results["environment.codex_sandbox"].status == "skipped"
    assert results["environment.claude_sandbox"].status == "skipped"


_CACHE_IDS = {
    "codex": "environment.codex_uv_cache",
    "claude": "environment.claude_uv_cache",
}


def _write_codex(home: Path, text: str) -> Path:
    path = home / ".codex/config.toml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _write_claude(home: Path, settings: object) -> Path:
    path = home / ".claude/settings.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(settings), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("runtime", "config", "snippet"),
    [
        ("codex", ".codex/config.toml", "writable_roots"),
        ("claude", ".claude/settings.json", '"allowWrite"'),
    ],
)
def test_missing_config_warns_that_the_uv_cache_is_not_writable(
    tmp_path: Path, runtime_home: Path, runtime: str, config: str, snippet: str
) -> None:
    result = _checks(tmp_path)[_CACHE_IDS[runtime]]
    assert result.status == "warn"
    assert result.fix is not None
    assert str(runtime_home / config) in result.fix.text
    assert str(runtime_home / ".cache/uv") in result.fix.text
    assert snippet in result.fix.text
    assert "новую сессию" in result.fix.text
    assert not (runtime_home / config).exists()


def test_uv_cache_listed_or_covered_by_a_parent_is_ok_and_files_are_preserved(
    tmp_path: Path, runtime_home: Path
) -> None:
    cache = runtime_home / ".cache/uv"
    codex = _write_codex(
        runtime_home,
        'sandbox_mode = "workspace-write"\n[sandbox_workspace_write]\n'
        f'writable_roots = ["{cache.parent}"]\n',
    )
    claude = _write_claude(
        runtime_home,
        {"sandbox": {"enabled": True, "filesystem": {"allowWrite": ["~/.cache/uv"]}}},
    )
    before = {path: path.read_bytes() for path in (codex, claude)}

    results = _checks(tmp_path, fix=True)

    assert results[_CACHE_IDS["codex"]].status == "ok"
    assert results[_CACHE_IDS["claude"]].status == "ok"
    assert {path: path.read_bytes() for path in before} == before


@pytest.mark.parametrize(
    "allow_write", [[], ["~/.cache/uv-other"], ["relative/uv"], [42], "~/.cache/uv"]
)
def test_claude_uv_cache_not_covered_by_allow_write_warns(
    tmp_path: Path, runtime_home: Path, allow_write: object
) -> None:
    _write_claude(
        runtime_home,
        {"sandbox": {"enabled": True, "filesystem": {"allowWrite": allow_write}}},
    )
    assert _checks(tmp_path)[_CACHE_IDS["claude"]].status == "warn"


@pytest.mark.parametrize(
    "settings",
    [
        {"sandbox": {"enabled": False}},
        {"sandbox": {"enabled": True, "filesystem": {"disabled": True}}},
        {},
    ],
)
def test_claude_without_a_write_restriction_is_ok(
    tmp_path: Path, runtime_home: Path, settings: object
) -> None:
    _write_claude(runtime_home, settings)
    result = _checks(tmp_path)[_CACHE_IDS["claude"]]
    assert result.status == "ok"
    assert "не ограничивает" in result.message


@pytest.mark.parametrize(
    ("config", "status"),
    [
        ('sandbox_mode = "danger-full-access"\n', "ok"),
        ('sandbox_mode = "workspace-write"\n', "warn"),
        (
            'sandbox_mode = "workspace-write"\n[sandbox_workspace_write]\n'
            'writable_roots = ["/elsewhere"]\n',
            "warn",
        ),
    ],
)
def test_codex_uv_cache_depends_on_sandbox_mode_and_writable_roots(
    tmp_path: Path, runtime_home: Path, config: str, status: str
) -> None:
    _write_codex(runtime_home, config)
    assert _checks(tmp_path)[_CACHE_IDS["codex"]].status == status


def test_uv_cache_directory_follows_uv_environment_variables(
    tmp_path: Path, runtime_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CACHE_HOME", str(xdg))
    result = _checks(repo)[_CACHE_IDS["claude"]]
    assert result.fix is not None
    assert str(xdg / "uv") in result.fix.text

    explicit = tmp_path / "explicit"
    monkeypatch.setenv("UV_CACHE_DIR", str(explicit))
    result = _checks(repo)[_CACHE_IDS["claude"]]
    assert result.fix is not None
    assert str(explicit) in result.fix.text
    assert str(xdg / "uv") not in result.fix.text

    _write_claude(
        runtime_home,
        {"sandbox": {"enabled": True, "filesystem": {"allowWrite": [str(explicit)]}}},
    )
    assert _checks(repo)[_CACHE_IDS["claude"]].status == "ok"


@pytest.mark.parametrize(
    ("runtime", "config", "contents"),
    [
        ("codex", ".codex/config.toml", b'api_key = "SECRET"\nsandbox_mode = [broken'),
        ("claude", ".claude/settings.json", b'{"api_key": "SECRET", broken}'),
        ("claude", ".claude/settings.json", b"\xffSECRET"),
    ],
)
def test_uv_cache_check_warns_on_invalid_config_without_disclosing_contents(
    tmp_path: Path, runtime_home: Path, runtime: str, config: str, contents: bytes
) -> None:
    path = runtime_home / config
    path.parent.mkdir()
    path.write_bytes(contents)
    report = registry.run(tmp_path)
    result = next(c for c in report.checks if c.id == _CACHE_IDS[runtime])
    assert result.status == "warn"
    assert "SECRET" not in render_text(report)


def test_uv_cache_check_is_skipped_for_uninstalled_runtimes(
    tmp_path: Path, runtime_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    results = _checks(tmp_path)
    assert results[_CACHE_IDS["codex"]].status == "skipped"
    assert results[_CACHE_IDS["claude"]].status == "skipped"


_PYPROJECT_DEFAULT = '[tool.uv]\ncache-dir = ".harness/.sandboxes/cache/uv"\n'


@pytest.mark.parametrize("runtime", ["codex", "claude"])
def test_uv_cache_inside_the_project_is_ok_without_any_sandbox_config(
    tmp_path: Path, runtime_home: Path, monkeypatch: pytest.MonkeyPatch, runtime: str
) -> None:
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "cache" / "uv"))
    result = _checks(tmp_path)[_CACHE_IDS[runtime]]
    assert result.status == "ok"
    assert "внутри проекта" in result.message
    assert result.fix is None


def test_a_relative_uv_cache_dir_is_resolved_against_the_project(
    tmp_path: Path, runtime_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UV_CACHE_DIR", ".harness/cache/uv")
    assert _checks(tmp_path)[_CACHE_IDS["claude"]].status == "ok"
    monkeypatch.setenv("UV_CACHE_DIR", "../outside/uv")
    assert _checks(tmp_path)[_CACHE_IDS["claude"]].status == "warn"


def test_uv_cache_outside_the_project_still_needs_a_write_permission(
    tmp_path: Path, runtime_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "elsewhere" / "uv"))
    assert _checks(repo)[_CACHE_IDS["codex"]].status == "warn"


def test_a_pyproject_cache_dir_inside_the_project_is_ok(
    tmp_path: Path, runtime_home: Path
) -> None:
    (tmp_path / "pyproject.toml").write_text(_PYPROJECT_DEFAULT, encoding="utf-8")
    for runtime in ("codex", "claude"):
        result = _checks(tmp_path)[_CACHE_IDS[runtime]]
        assert result.status == "ok"
        assert "внутри проекта" in result.message


def test_a_pyproject_without_a_cache_dir_keeps_the_home_cache_requirement(
    tmp_path: Path, runtime_home: Path
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.uv]\npackage = false\n", encoding="utf-8"
    )
    assert _checks(tmp_path)[_CACHE_IDS["claude"]].status == "warn"


def test_a_user_uv_cache_dir_wins_over_the_pyproject_cache_dir(
    tmp_path: Path, runtime_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "pyproject.toml").write_text(_PYPROJECT_DEFAULT, encoding="utf-8")
    monkeypatch.setenv("UV_CACHE_DIR", str(runtime_home / ".cache" / "uv"))
    assert _checks(tmp_path)[_CACHE_IDS["claude"]].status == "warn"
