"""Read-only user sandbox configuration checks; never launch or configure an agent."""

from __future__ import annotations

import json
import os
import shutil
import tomllib
from pathlib import Path

from ..context import HealthContext
from ..model import CheckResult, Fix, JsonObject

_CODEX_SNIPPET = """sandbox_mode = "workspace-write"

[sandbox_workspace_write]
network_access = true"""

_CLAUDE_SNIPPET = """{
  "sandbox": {
    "enabled": true,
    "network": {
      "allowAllUnixSockets": true
    }
  }
}"""


def _config_path(variable: str, directory: str, filename: str) -> Path:
    configured = os.environ.get(variable)
    root = Path(configured).expanduser() if configured else Path.home() / directory
    return root / filename


def _read_settings(path: Path) -> JsonObject | str:
    """Return data or a safe diagnostic, never parser exceptions containing config values."""
    try:
        text = path.read_text(encoding="utf-8")
        data = tomllib.loads(text) if path.suffix == ".toml" else json.loads(text)
    except FileNotFoundError:
        return "файл отсутствует"
    except OSError:
        return "файл недоступен для чтения"
    except (ValueError, RecursionError):
        return "файл не является корректным TOML/JSON в UTF-8"
    if not isinstance(data, dict):
        return "ожидался объект настроек"
    return data


def _result(
    runtime: str, path: Path, settings: JsonObject | str, *, valid: bool, snippet: str
) -> CheckResult:
    label = "Codex" if runtime == "codex" else "Claude Code"
    message = (
        f"{label}: {settings} ({path})"
        if isinstance(settings, str)
        else f"{label}: пользовательские настройки песочницы не настроены для локальных сокетов ({path})"
    )
    return CheckResult(
        id=f"environment.{runtime}_sandbox",
        group="environment",
        status="ok" if valid else "warn",
        message=f"{label}: пользовательские настройки песочницы для локальных сокетов заданы ({path})"
        if valid
        else message,
        fix=None
        if valid
        else Fix(
            text=f"в {path} добавьте или измените настройки, сохранив остальные поля:\n"
            f"{snippet}\n"
            "Начните новую сессию агента и повторите harness health. "
            + (
                "network_access = true разрешает также внешнюю сеть."
                if runtime == "codex"
                else "allowAllUnixSockets разрешает все Unix-сокеты; правила внешних доменов не меняются. "
                "Если задано sandbox.filesystem.disabled, установите false."
            )
        ),
    )


def _uninstalled(runtime: str) -> CheckResult:
    return CheckResult(
        id=f"environment.{runtime}_sandbox",
        group="environment",
        status="skipped",
        message=f"{runtime}: утилита и пользовательский конфиг отсутствуют",
    )


def check_codex_sandbox(context: HealthContext) -> CheckResult:
    """Check the requested workspace-write and network baseline in the user's Codex config."""
    path = _config_path("CODEX_HOME", ".codex", "config.toml")
    if shutil.which("codex") is None and not path.exists():
        return _uninstalled("codex")
    settings = _read_settings(path)
    valid = False
    if isinstance(settings, dict):
        workspace = settings.get("sandbox_workspace_write")
        valid = (
            settings.get("sandbox_mode") == "workspace-write"
            and isinstance(workspace, dict)
            and workspace.get("network_access") is True
        )
    return _result("codex", path, settings, valid=valid, snippet=_CODEX_SNIPPET)


def check_claude_sandbox(context: HealthContext) -> CheckResult:
    """Check Claude's filesystem sandbox and permission to use local Unix sockets."""
    path = _config_path("CLAUDE_CONFIG_DIR", ".claude", "settings.json")
    if shutil.which("claude") is None and not path.exists():
        return _uninstalled("claude")
    settings = _read_settings(path)
    valid = False
    if isinstance(settings, dict):
        sandbox = settings.get("sandbox")
        if isinstance(sandbox, dict):
            network = sandbox.get("network")
            filesystem = sandbox.get("filesystem", {})
            valid = (
                sandbox.get("enabled") is True
                and isinstance(network, dict)
                and network.get("allowAllUnixSockets") is True
                and isinstance(filesystem, dict)
                and filesystem.get("disabled", False) is False
            )
    return _result("claude", path, settings, valid=valid, snippet=_CLAUDE_SNIPPET)
