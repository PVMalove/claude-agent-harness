from dataclasses import dataclass, field
import math
from typing import Any, Mapping
from ...domain.policy import DelegationPolicy, DelegationRule, RetryPolicy


@dataclass(frozen=True)
class ProviderConfig:
    type: str
    command: str | None = None
    args: tuple[str, ...] = ()
    timeout: float | None = None
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)


@dataclass(frozen=True)
class WorkerConfig:
    provider: str
    capabilities: frozenset[str] = frozenset()
    priority: int = 0
    health: str = "healthy"


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
    parallel: bool = False


@dataclass(frozen=True)
class RuntimeConfig:
    default_timeout: float = 600.0
    max_parallel: int = 8
    max_depth: int | None = None
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    providers: Mapping[str, ProviderConfig] = field(default_factory=dict)
    workers: Mapping[str, WorkerConfig] = field(default_factory=dict)
    skills: Mapping[str, SkillConfig] = field(default_factory=dict)
    workflows: Mapping[str, WorkflowConfig] = field(default_factory=dict)
    delegation: Mapping[str, DelegationPolicy] = field(default_factory=dict)


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


def _positive_integer(value: Any, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{location} must be a positive integer")
    return value


def _non_negative_integer(value: Any, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{location} must be a non-negative integer")
    return value


def _health(value: Any, location: str) -> str:
    if value not in {"healthy", "unhealthy"}:
        raise ValueError(f"{location} must be 'healthy' or 'unhealthy'")
    return value


def _priority(value: Any, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{location} must be an integer")
    return value


def _retry_policy(value: Any, location: str, default: RetryPolicy | None = None) -> RetryPolicy:
    if value is None:
        return default or RetryPolicy()
    if isinstance(value, int) and not isinstance(value, bool):
        return RetryPolicy(max_attempts=_positive_integer(value, f"{location}.max_attempts"))
    if not isinstance(value, Mapping):
        raise ValueError(f"{location} must be a positive integer or table")
    if "max_retries" in value and "max_attempts" not in value and "attempts" not in value:
        max_retries = _non_negative_integer(value["max_retries"], f"{location}.max_retries")
        max_attempts = max_retries + 1
    else:
        max_attempts = _positive_integer(
            value.get("max_attempts", value.get("attempts", 1)),
            f"{location}.max_attempts",
        )
    backoff = value.get("backoff", "none")
    if backoff not in {"none", "exponential", "linear"}:
        raise ValueError(f"{location}.backoff must be none, exponential, or linear")
    return RetryPolicy(max_attempts=max_attempts, backoff=backoff)


def validate_config(raw_config: Mapping[str, Any]) -> RuntimeConfig:
    if not isinstance(raw_config, Mapping):
        raise ValueError("orchestration configuration must be a table")

    runtime = _table(raw_config, "runtime")
    default_timeout = _positive_number(
        runtime.get("default_timeout", 600), "Runtime.default_timeout"
    )
    max_parallel = _positive_integer(runtime.get("max_parallel", 8), "Runtime.max_parallel")
    max_depth_value = runtime.get("max_depth")
    max_depth = (
        _non_negative_integer(max_depth_value, "Runtime.max_depth")
        if max_depth_value is not None
        else None
    )
    runtime_retry_value = runtime.get("retry", runtime.get("retry_policy"))
    if runtime_retry_value is None and "retry_attempts" in runtime:
        runtime_retry_value = runtime["retry_attempts"]
    runtime_retry = _retry_policy(runtime_retry_value, "Runtime.retry")

    policies = _table(raw_config, "policies")
    limits = _table(policies, "limits")
    if "max_parallel" in limits:
        max_parallel = _positive_integer(limits["max_parallel"], "Policies.limits.max_parallel")
    if "max_depth" in limits:
        max_depth = _non_negative_integer(limits["max_depth"], "Policies.limits.max_depth")
    if "retry" in limits or "retry_policy" in limits or "max_retries" in limits:
        runtime_retry = _retry_policy(
            limits.get(
                "retry",
                limits.get("retry_policy", {"max_retries": limits["max_retries"]}),
            ),
            "Policies.limits.retry",
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
            retry_policy=_retry_policy(
                data.get(
                    "retry",
                    data.get("retry_policy", data.get("retry_attempts")),
                ),
                f"Provider '{name}'.retry",
                default=runtime_retry,
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
            priority=_priority(data.get("priority", 0), f"Worker '{name}'.priority"),
            health=_health(data.get("health", "healthy"), f"Worker '{name}'.health"),
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
        parallel = data.get("parallel", False)
        if not isinstance(parallel, bool):
            raise ValueError(f"Workflow '{name}'.parallel must be a boolean")
        unknown_skills = sorted(set(steps) - skills.keys())
        if unknown_skills:
            raise ValueError(f"Workflow '{name}' references unknown skill '{unknown_skills[0]}'")
        workflows[name] = WorkflowConfig(steps=steps, parallel=parallel)

    delegation: dict[str, DelegationPolicy] = {}
    for caller, data in _table(policies, "delegation").items():
        if not isinstance(data, Mapping):
            raise ValueError(f"Delegation policy for '{caller}' must be a table")
        raw_rules = data.get("allow", [])
        if not isinstance(raw_rules, list):
            raise ValueError(f"Delegation policy for '{caller}'.allow must be a list")
        rules: list[DelegationRule] = []
        for index, raw_rule in enumerate(raw_rules):
            location = f"Delegation policy for '{caller}'.allow[{index}]"
            if not isinstance(raw_rule, Mapping):
                raise ValueError(f"{location} must be a table")
            worker = raw_rule.get("worker")
            if not isinstance(worker, str) or not worker:
                raise ValueError(f"{location}.worker must be a non-empty string")
            if worker not in workers:
                raise ValueError(f"{location} references unknown worker '{worker}'")
            skills_value = raw_rule.get("skills", [])
            rule_skills = set(_string_list(skills_value, f"{location}.skills"))
            unknown_skills = sorted(
                skill for skill in rule_skills if skill != "*" and skill not in skills
            )
            if unknown_skills:
                raise ValueError(f"{location} references unknown skill '{unknown_skills[0]}'")
            rules.append(DelegationRule(worker=worker, skills=rule_skills))
        delegation[caller] = DelegationPolicy(allow=rules)

    return RuntimeConfig(
        default_timeout=default_timeout,
        max_parallel=max_parallel,
        max_depth=max_depth,
        retry_policy=runtime_retry,
        providers=providers,
        workers=workers,
        skills=skills,
        workflows=workflows,
        delegation=delegation,
    )
