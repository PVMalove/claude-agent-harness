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
    home = tmp_path / "user"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
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
