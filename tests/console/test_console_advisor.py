from __future__ import annotations

import json
import shutil
import sys
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
        argv: list[str],
        cwd: Path,
        prompt: str,
        cancel: Event,
        timeout: float,
        **_: object,
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
        argv: list[str],
        cwd: Path,
        prompt: str,
        cancel: Event,
        timeout: float,
        **_: object,
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


def test_stderr_warnings_do_not_corrupt_structured_output(tmp_path: Path) -> None:
    from harness.console.advisor import _run

    script = tmp_path / "cli.py"
    script.write_text(
        "import sys\n"
        "sys.stderr.write('WARNING: read-only file system\\n')\n"
        "sys.stdout.write('{\"ok\": true}')\n",
        encoding="utf-8",
    )
    out = _run([sys.executable, str(script)], tmp_path, "", Event(), 10)
    assert json.loads(out) == {"ok": True}


@pytest.mark.parametrize(
    ("status", "ready"),
    [
        ("Logged in using ChatGPT", True),
        ("WARNING: aliases\nLogged in using an API key", True),
        ("Not logged in", False),
        ("not logged in; run login", False),
    ],
)
def test_codex_login_status_is_matched_by_leading_words(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str, ready: bool
) -> None:
    from harness.console import advisor

    def run(
        argv: list[str],
        cwd: Path,
        prompt: str,
        cancel: Event,
        timeout: float,
        **_: object,
    ) -> str:
        if "--help" in argv:
            return "--ignore-user-config --ignore-rules --ephemeral --output-schema --disable"
        return status

    monkeypatch.setattr(shutil, "which", lambda _: "/bin/codex")
    monkeypatch.setattr(advisor, "_run", run)
    result = advisor.availability("codex", Event())
    assert result.startswith("Готов") is ready


def test_comparison_is_validated_and_context_carries_only_non_secret_facts(
    tmp_path: Path,
) -> None:
    installed_project(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    draft = ConfigurationDraft.load(tmp_path, ("project.json",))
    field = steps_for(draft)[0]
    request = build_request(draft, field)
    assert '"project_markers": ["pyproject.toml"]' in request
    assert "comparison" in request and str(tmp_path) not in request
    good = {
        "value": "en",
        "why": "Команда",
        "impact": "Язык",
        "comparison": [{"option": "ru", "tradeoff": "Русские отчёты"}],
    }
    assert parse_response(json.dumps(good), field).comparison == (
        ("ru", "Русские отчёты"),
    )
    for bad in (
        {**good, "comparison": [{"option": "a"}]},
        {**good, "comparison": [{"option": "a", "tradeoff": "b"}] * 5},
        {**good, "comparison": [{"option": "a", "tradeoff": "token=hidden1"}]},
    ):
        with pytest.raises(AdvisorUnavailable):
            parse_response(json.dumps(bad), field)


def test_status_commands_may_report_on_stderr_only_when_requested(
    tmp_path: Path,
) -> None:
    from harness.console.advisor import _run

    script = tmp_path / "status.py"
    script.write_text(
        "import sys\nsys.stderr.write('Logged in using ChatGPT\\n')\n", encoding="utf-8"
    )
    argv = [sys.executable, str(script)]
    assert _run(argv, tmp_path, "", Event(), 10) == ""
    assert "Logged in" in _run(argv, tmp_path, "", Event(), 10, merge_stderr=True)


def test_a_response_in_a_code_fence_or_with_an_encoded_value_is_accepted(
    tmp_path: Path,
) -> None:
    installed_project(tmp_path)
    draft = ConfigurationDraft.load(tmp_path, ("project.json",))
    field = steps_for(draft)[0]
    body = '{"value": "\\"en\\"", "why": "Команда", "impact": "Язык"}'
    advice = parse_response("Вот ответ:\n```json\n" + body + "\n```", field)
    assert advice.value == "en"
    assert parse_response(body, field).value == "en"
    assert parse_response('{"value":"en","why":"x","impact":"y","note":"?"}', field)


def test_a_rejected_response_names_the_reason(tmp_path: Path) -> None:
    installed_project(tmp_path)
    draft = ConfigurationDraft.load(tmp_path, ("project.json",))
    field = steps_for(draft)[0]
    for raw, reason in (
        ("не JSON", "не в формате JSON"),
        ('{"value":"en","why":"x"}', "нет полей impact"),
        ('{"value":"invented","why":"x","impact":"y"}', "не проходит проверку поля"),
        ('{"value":"en","why":"token=secret","impact":"y"}', "похож на секрет"),
    ):
        with pytest.raises(AdvisorUnavailable, match=reason):
            parse_response(raw, field)
