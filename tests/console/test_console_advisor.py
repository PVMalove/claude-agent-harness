from __future__ import annotations

import json
import shutil
from pathlib import Path
from threading import Event

import pytest

from harness.console.advisor import (
    Advice,
    AdvisorUnavailable,
    advise,
    build_request,
    parse_response,
)
from harness.console.configuration import ConfigurationDraft
from harness.console.configuration_steps import steps_for
from tests.console.test_console_configuration_app import installed_project


def test_prompt_excludes_authored_commands_and_paths(tmp_path: Path) -> None:
    installed_project(tmp_path)
    draft = ConfigurationDraft.load(tmp_path, ("project.json",))
    draft.set_value("project.json", ("qa_gate_commands",), ["echo private-command"])
    request = build_request(draft, steps_for(draft)[0])
    assert "private-command" not in request
    assert str(tmp_path) not in request
    assert '"choices": ["ru", "en"]' in request


def test_cli_uses_saved_login_and_disables_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness.console import advisor

    installed_project(tmp_path)
    draft = ConfigurationDraft.load(tmp_path, ("project.json",))
    field = steps_for(draft)[0]
    seen: list[list[str]] = []

    def run(
        argv: list[str], cwd: Path, prompt: str, cancel: Event, timeout: float
    ) -> str:
        seen.append(argv)
        assert cwd != tmp_path
        if "--help" in argv:
            return "--ignore-user-config --ignore-rules --ephemeral --output-schema --disable"
        if "login" in argv:
            return "Logged in using ChatGPT"
        output = Path(argv[argv.index("--output-last-message") + 1])
        output.write_text(
            json.dumps(
                {"value": "en", "why": "Общая команда", "impact": "Английские отчёты"}
            )
        )
        return ""

    monkeypatch.setattr(shutil, "which", lambda _: "/bin/codex")
    monkeypatch.setattr(advisor, "_run", run)
    result = advise("codex", draft, field, Event())
    assert result == Advice("en", "Общая команда", "Английские отчёты")
    command = seen[-1]
    assert "--ignore-user-config" in command
    assert "--ephemeral" in command
    assert "read-only" in command
    assert "shell_tool" in command and "hooks" in command
    assert not any("auth.json" in value for value in command)


def test_missing_cli_falls_back_without_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed_project(tmp_path)
    draft = ConfigurationDraft.load(tmp_path, ("project.json",))
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(AdvisorUnavailable, match="Обычные подсказки"):
        advise("codex", draft, steps_for(draft)[0], Event())


def test_claude_structured_output_and_rejection(tmp_path: Path) -> None:
    installed_project(tmp_path)
    draft = ConfigurationDraft.load(tmp_path, ("project.json",))
    field = steps_for(draft)[0]
    response = json.dumps(
        {"structured_output": {"value": "en", "why": "Команда", "impact": "Язык"}}
    )
    assert parse_response(response, field).value == "en"
    with pytest.raises(AdvisorUnavailable):
        parse_response('{"value":"invented","why":"x","impact":"y"}', field)
    with pytest.raises(AdvisorUnavailable):
        parse_response('{"value":"en","why":"token=secret","impact":"y"}', field)


def test_claude_reuses_auth_and_never_uses_bare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness.console import advisor

    installed_project(tmp_path)
    draft = ConfigurationDraft.load(tmp_path, ("project.json",))
    seen: list[list[str]] = []

    def run(
        argv: list[str], cwd: Path, prompt: str, cancel: Event, timeout: float
    ) -> str:
        seen.append(argv)
        if "--help" in argv:
            return "--safe-mode --tools --strict-mcp-config --no-session-persistence --json-schema"
        if "auth" in argv:
            return '{"loggedIn": true}'
        assert cwd != tmp_path and timeout == 90
        return '{"structured_output":{"value":"en","why":"Команда","impact":"Язык"}}'

    monkeypatch.setattr(shutil, "which", lambda _: "/bin/claude")
    monkeypatch.setattr(advisor, "_run", run)
    assert advise("claude", draft, steps_for(draft)[0], Event()).value == "en"
    assert "--safe-mode" in seen[-1] and "--bare" not in seen[-1]
    assert seen[-1][seen[-1].index("--tools") + 1] == ""
    assert not any("login" in command or "logout" in command for command in seen)


def test_cancellation_and_output_limit_terminate_cli(tmp_path: Path) -> None:
    import sys
    from harness.console.advisor import _run

    cancel = Event()
    cancel.set()
    with pytest.raises(AdvisorUnavailable, match="отменён"):
        _run(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            tmp_path,
            "",
            cancel,
            1,
        )
    with pytest.raises(AdvisorUnavailable, match="слишком большой"):
        _run([sys.executable, "-c", "print('x' * 130000)"], tmp_path, "", Event(), 5)
    with pytest.raises(AdvisorUnavailable, match="вовремя"):
        _run(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            tmp_path,
            "",
            Event(),
            0.1,
        )
