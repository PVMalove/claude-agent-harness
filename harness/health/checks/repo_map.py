"""Group 'repo_map': the Repo Map dispatch-tier check migrated from `harness/bin/harness`'s old
`cmd_health` (`repo_map_health`). Detection logic is unchanged; `harness/bin/harness` now imports
it from here (single definition, see checks/files.py's module docstring for why)."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from harness.repo_map import parser_bundle

from ..context import HealthContext
from ..model import CheckResult, Fix, Status
from .files import ORCHESTRATION_CONFIG_REL

REPO_MAP_REL = Path(".harness/repo_map/repo_map.py")
REPO_MAP_REGISTRY_REL = Path(
    ".harness/.sandboxes/cache/repo_map/parser_bundle/registry"
)

_REMEDY_PREFIX = "КАК ИСПРАВИТЬ: "


def repo_map_health(repo: Path) -> list[str]:
    """Describe the Repo Map dispatch capability without invoking its parser.

    Health must remain safe on an air-gapped machine: in particular, it must not
    call the Repo Map CLI because that CLI may install an already-present bundle.
    This deliberately inspects only policy and bundle metadata.
    """
    if not (repo / REPO_MAP_REL).is_file():
        return []

    policy: dict[str, object] = {}
    has_policy = False
    policy_path = repo / ORCHESTRATION_CONFIG_REL
    if policy_path.is_file():
        try:
            decoded = json.loads(policy_path.read_text(encoding="utf-8"))
            if isinstance(decoded, dict) and isinstance(
                decoded.get("repo_map_policy"), dict
            ):
                policy = decoded["repo_map_policy"]
                has_policy = True
        except (OSError, json.JSONDecodeError):
            # The normal health validation reports malformed orchestration config.
            pass

    requested_tier = policy.get("tier", "full")
    policy_mode = "enforced" if has_policy else "portable"
    if requested_tier == "minimal":
        return [
            "Repo Map: tier=minimal (requested by policy)",
            "Repo Map provenance: policy requested path-only inventory",
            "Repo Map policy: enforced; dispatch is limited to minimal path inventory",
        ]

    configured_paths = policy.get("parser_bundle_registry_paths", [])
    registry_paths = (
        tuple(item for item in configured_paths if isinstance(item, str))
        if isinstance(configured_paths, list)
        else ()
    )
    dispatch_policy = "dispatch may use full parser-backed Repo Map and degrades to minimal when the bundle is unavailable"
    timeout = policy.get("parser_bundle_timeout_seconds", 30)
    located = parser_bundle.locate_bundle(
        repo=repo,
        registry_paths=registry_paths,
        python_executable=sys.executable,
        timeout_seconds=timeout if isinstance(timeout, int) and timeout > 0 else 30,
    )
    degradation_reason: str | None
    remedy = (
        "КАК ИСПРАВИТЬ: установите offline parser bundle в "
        f"{REPO_MAP_REGISTRY_REL.as_posix()} и повторите harness health"
    )
    if isinstance(located, str):
        degradation_reason = located
    else:
        degradation_reason = parser_bundle.check_bundle(located)
        if degradation_reason is None and shutil.which("uv") is None:
            degradation_reason = "uv executable unavailable"
        if degradation_reason is None and not parser_bundle.supports_real_sources(
            located.lock
        ):
            degradation_reason = "parser bundle has no supported grammars"
            remedy = (
                "КАК ИСПРАВИТЬ: registry содержит bundle без поддерживаемых грамматик "
                "(вероятно, тестовый stub); замените его bundle из scripts/build_parser_bundle.py "
                "и повторите harness health"
            )
    if degradation_reason is not None:
        return [
            f"Repo Map: tier=minimal ({degradation_reason})",
            f"Repo Map provenance: {degradation_reason}",
            f"Repo Map policy: {policy_mode}; {dispatch_policy}",
            "ПРЕДУПРЕЖДЕНИЕ: Repo Map работает в ограниченном режиме; команда health не загружает зависимости",
            remedy,
        ]
    assert not isinstance(located, str)
    lock = located.lock
    grammars = ", ".join(
        f"{grammar.name}@{grammar.version} abi={grammar.abi} sha256={grammar.sha256}"
        for grammar in lock.grammars
    )
    provenance = (
        f"source={located.bundle_source}, python_tag={located.python_tag}, "
        f"platform_tag={located.platform_tag}, "
        f"lock_sha256={lock.raw_sha256}, script_hash={lock.script_sha256}, "
        f"core_version={lock.core_version}, core_abi_range={lock.core_abi_range}, grammars=[{grammars}]"
    )
    return [
        "Repo Map: tier=full (offline parser bundle detected)",
        f"Repo Map provenance: {provenance}",
        f"Repo Map policy: {policy_mode}; {dispatch_policy}",
    ]


def check_tier(context: HealthContext) -> CheckResult:
    lines = repo_map_health(context.repo)
    if not lines:
        return CheckResult(
            id="repo_map.tier",
            group="repo_map",
            status="skipped",
            message="Repo Map не установлен в проект",
        )
    status: Status = "ok" if lines[0].startswith("Repo Map: tier=full") else "warn"
    message = "; ".join(lines[:3])
    fix = None
    if status == "warn":
        for line in lines:
            if line.startswith(_REMEDY_PREFIX):
                fix = Fix(text=line.removeprefix(_REMEDY_PREFIX))
                break
    return CheckResult(
        id="repo_map.tier", group="repo_map", status=status, message=message, fix=fix
    )
