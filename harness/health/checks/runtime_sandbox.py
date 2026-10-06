"""Read-only user sandbox configuration checks; never launch or configure an agent."""

from __future__ import annotations

import json
import os
import shutil
import tomllib
from collections.abc import Callable
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


def _expand(value: str) -> Path:
    """Expand a leading `~` against the same home directory the config lookup uses."""
    if value == "~" or value.startswith(("~/", "~\\")):
        return Path.home() / value[2:]
    return Path(value)


def _uv_cache_dir() -> Path:
    """The directory `uv` writes its cache to: UV_CACHE_DIR, XDG_CACHE_HOME/uv or ~/.cache/uv."""
    configured = os.environ.get("UV_CACHE_DIR")
    if configured:
        return _expand(configured)
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = _expand(xdg) if xdg else Path.home() / ".cache"
    return base / "uv"


def _covers(entries: object, cache: Path) -> bool:
    """True when the cache directory is one of the absolute paths or lies under one."""
    if not isinstance(entries, list):
        return False
    target = Path(os.path.abspath(cache))
    for entry in entries:
        if not isinstance(entry, str):
            continue
        root = Path(os.path.normpath(_expand(entry)))
        if root.is_absolute() and (target == root or root in target.parents):
            return True
    return False


def _claude_cache_verdict(settings: JsonObject, cache: Path) -> str:
    sandbox = settings.get("sandbox")
    if not isinstance(sandbox, dict) or sandbox.get("enabled") is not True:
        return "unrestricted"
    filesystem = sandbox.get("filesystem", {})
    if not isinstance(filesystem, dict):
        return "missing"
    if filesystem.get("disabled", False) is True:
        return "unrestricted"
    return "writable" if _covers(filesystem.get("allowWrite"), cache) else "missing"


def _codex_cache_verdict(settings: JsonObject, cache: Path) -> str:
    if settings.get("sandbox_mode") != "workspace-write":
        return "unrestricted"
    workspace = settings.get("sandbox_workspace_write")
    roots = workspace.get("writable_roots") if isinstance(workspace, dict) else None
    return "writable" if _covers(roots, cache) else "missing"


def _uv_cache_result(
    runtime: str, path: Path, settings: JsonObject | str, cache: Path, verdict: str
) -> CheckResult:
    label = "Codex" if runtime == "codex" else "Claude Code"
    check_id = f"environment.{runtime}_uv_cache"
    if verdict != "missing" and isinstance(settings, dict):
        message = (
            f"{label}: кеш uv {cache} доступен для записи из песочницы ({path})"
            if verdict == "writable"
            else f"{label}: песочница не ограничивает запись, кеш uv {cache} доступен ({path})"
        )
        return CheckResult(
            id=check_id, group="environment", status="ok", message=message
        )
    snippet = (
        f'[sandbox_workspace_write]\nwritable_roots = ["{cache}"]'
        if runtime == "codex"
        else json.dumps(
            {"sandbox": {"filesystem": {"allowWrite": [str(cache)]}}}, indent=2
        )
    )
    return CheckResult(
        id=check_id,
        group="environment",
        status="warn",
        message=f"{label}: {settings} ({path})"
        if isinstance(settings, str)
        else f"{label}: кеш uv {cache} не разрешён для записи из песочницы ({path})",
        fix=Fix(
            text=f"в {path} добавьте путь в список, сохранив остальные поля и уже заданные пути:\n"
            f"{snippet}\n"
            "Начните новую сессию агента и повторите harness health. Без этого `uv sync` "
            "(например, в `make verify`) падает с «Read-only file system». Разрешение "
            "действует на все команды песочницы, не только на uv."
        ),
    )


def _uv_cache_check(
    runtime: str, path: Path, judge: Callable[[JsonObject, Path], str]
) -> CheckResult:
    if shutil.which(runtime) is None and not path.exists():
        return CheckResult(
            id=f"environment.{runtime}_uv_cache",
            group="environment",
            status="skipped",
            message=f"{runtime}: утилита и пользовательский конфиг отсутствуют",
        )
    cache = _uv_cache_dir()
    settings = _read_settings(path)
    verdict = judge(settings, cache) if isinstance(settings, dict) else "missing"
    return _uv_cache_result(runtime, path, settings, cache, verdict)


def check_codex_uv_cache(context: HealthContext) -> CheckResult:
    """Check that the Codex workspace-write sandbox may write the `uv` cache."""
    path = _config_path("CODEX_HOME", ".codex", "config.toml")
    return _uv_cache_check("codex", path, _codex_cache_verdict)


def check_claude_uv_cache(context: HealthContext) -> CheckResult:
    """Check that the Claude Code filesystem sandbox may write the `uv` cache."""
    path = _config_path("CLAUDE_CONFIG_DIR", ".claude", "settings.json")
    return _uv_cache_check("claude", path, _claude_cache_verdict)
