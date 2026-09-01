from dataclasses import dataclass, field
import math
from typing import Any, Mapping


@dataclass(frozen=True)
class ProviderConfig:
    type: str
    command: str | None = None
    args: tuple[str, ...] = ()
    timeout: float | None = None


@dataclass(frozen=True)
class WorkerConfig:
    provider: str
    capabilities: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ExecutionConfig:
    preferred: tuple[str, ...] = ()


@dataclass(frozen=True)
class SkillConfig:
    requirements: frozenset[str] = frozenset()
    execution: ExecutionConfig = ExecutionConfig()


@dataclass(frozen=True)
class WorkflowConfig:
    steps: tuple[str, ...] = ()


@dataclass(frozen=True)
class RuntimeConfig:
    default_timeout: float = 600.0
    providers: Mapping[str, ProviderConfig] = field(default_factory=dict)
    workers: Mapping[str, WorkerConfig] = field(default_factory=dict)
    skills: Mapping[str, SkillConfig] = field(default_factory=dict)
    workflows: Mapping[str, WorkflowConfig] = field(default_factory=dict)


def _table(raw_config: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = raw_config.get(name, {})
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a table")
    return value


def _string_list(value: Any, location: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{location} must be a list of strings")
    return tuple(value)


def _positive_number(value: Any, location: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{location} must be a positive number")
    return float(value)


def validate_config(raw_config: Mapping[str, Any]) -> RuntimeConfig:
    if not isinstance(raw_config, Mapping):
        raise ValueError("orchestration configuration must be a table")

    runtime = _table(raw_config, "runtime")
    default_timeout = _positive_number(
        runtime.get("default_timeout", 600), "Runtime.default_timeout"
    )

    providers: dict[str, ProviderConfig] = {}
    for name, data in _table(raw_config, "providers").items():
        if not isinstance(data, Mapping):
            raise ValueError(f"Provider '{name}' must be a table")
        provider_type = data.get("type")
        if not isinstance(provider_type, str) or not provider_type:
            raise ValueError(f"Provider '{name}' is missing 'type'")
        command = data.get("command")
        if command is not None and not isinstance(command, str):
            raise ValueError(f"Provider '{name}'.command must be a string")
        providers[name] = ProviderConfig(
            type=provider_type,
            command=command,
            args=_string_list(data.get("args", []), f"Provider '{name}'.args"),
            timeout=(
                _positive_number(data["timeout"], f"Provider '{name}'.timeout")
                if "timeout" in data
                else None
            ),
        )

    workers: dict[str, WorkerConfig] = {}
    for name, data in _table(raw_config, "workers").items():
        if not isinstance(data, Mapping):
            raise ValueError(f"Worker '{name}' must be a table")
        provider = data.get("provider")
        if not isinstance(provider, str) or not provider:
            raise ValueError(f"Worker '{name}' is missing 'provider'")
        if provider not in providers:
            raise ValueError(f"Worker '{name}' references unknown provider '{provider}'")
        workers[name] = WorkerConfig(
            provider=provider,
            capabilities=frozenset(
                _string_list(data.get("capabilities", []), f"Worker '{name}'.capabilities")
            ),
        )

    skills: dict[str, SkillConfig] = {}
    for name, data in _table(raw_config, "skills").items():
        if not isinstance(data, Mapping):
            raise ValueError(f"Skill '{name}' must be a table")
        execution = data.get("execution", {})
        if not isinstance(execution, Mapping):
            raise ValueError(f"Skill '{name}'.execution must be a table")
        preferred = _string_list(execution.get("preferred", []), f"Skill '{name}'.execution.preferred")
        unknown_workers = sorted(set(preferred) - workers.keys())
        if unknown_workers:
            raise ValueError(f"Skill '{name}' prefers unknown worker '{unknown_workers[0]}'")
        skills[name] = SkillConfig(
            requirements=frozenset(_string_list(data.get("requires", []), f"Skill '{name}'.requires")),
            execution=ExecutionConfig(preferred=preferred),
        )

    workflows: dict[str, WorkflowConfig] = {}
    for name, data in _table(raw_config, "workflows").items():
        if not isinstance(data, Mapping):
            raise ValueError(f"Workflow '{name}' must be a table")
        steps = _string_list(data.get("steps", []), f"Workflow '{name}'.steps")
        unknown_skills = sorted(set(steps) - skills.keys())
        if unknown_skills:
            raise ValueError(f"Workflow '{name}' references unknown skill '{unknown_skills[0]}'")
        workflows[name] = WorkflowConfig(steps=steps)

    return RuntimeConfig(
        default_timeout=default_timeout,
        providers=providers,
        workers=workers,
        skills=skills,
        workflows=workflows,
    )
