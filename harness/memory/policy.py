"""Shared strict memory policy contract for health and runtime consumers."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath

MAX_REGEX_LENGTH = 512


@dataclass(frozen=True)
class Policy:
    """Deny-by-default source selection and pointer limits."""

    enabled: bool = False
    source_types: tuple[str, ...] = ()
    allow_paths: tuple[str, ...] = ()
    redact_rules: tuple[str, ...] = ()
    min_similarity: float = 0.0
    top_k: int = 5
    max_tokens: int = 1000

    @property
    def fingerprint(self) -> str:
        """Fingerprint every policy field, including dormant vector threshold."""
        payload = json.dumps(asdict(self), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @property
    def active(self) -> bool:
        """No implicit allowlists even when memory is enabled."""
        return self.enabled and bool(self.source_types and self.allow_paths)


def parse_policy(config: dict[str, object]) -> Policy:
    """Validate both optional config sections; raise a bounded field diagnostic."""
    enabled = False
    if "memory" in config:
        memory = config["memory"]
        if not isinstance(memory, dict) or set(memory) != {"enabled"}:
            raise ValueError("memory must contain only enabled")
        if not isinstance(memory["enabled"], bool):
            raise ValueError("memory.enabled must be boolean")
        enabled = memory["enabled"]
    if "memory_policy" not in config:
        return Policy(enabled=enabled)
    value = config["memory_policy"]
    keys = {
        "source_types",
        "allow_paths",
        "redact_rules",
        "min_similarity",
        "top_k",
        "max_tokens",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(
            "memory_policy must contain exactly " + ", ".join(sorted(keys))
        )
    lists: dict[str, tuple[str, ...]] = {}
    for key in ("source_types", "allow_paths", "redact_rules"):
        items = value[key]
        if not isinstance(items, list) or not all(
            isinstance(item, str) for item in items
        ):
            raise ValueError(f"memory_policy.{key} must be a list of strings")
        lists[key] = tuple(items)
    if any(item not in {"adr", "glossary"} for item in lists["source_types"]):
        raise ValueError("memory_policy.source_types supports only adr and glossary")
    for item in lists["allow_paths"]:
        if (
            not item
            or item.startswith("/")
            or "\\" in item
            or ":" in item
            or any(part in {"", ".", ".."} for part in item.split("/"))
            or PurePosixPath(item).is_absolute()
        ):
            raise ValueError(
                "memory_policy.allow_paths must be relative POSIX patterns without traversal"
            )
    for rule in lists["redact_rules"]:
        if not rule or len(rule) > MAX_REGEX_LENGTH:
            raise ValueError(
                "memory_policy.redact_rules must contain 1..512 character regexes"
            )
        try:
            re.compile(rule)
        except re.error:
            raise ValueError(
                "memory_policy.redact_rules contains an invalid regex"
            ) from None
    threshold = value["min_similarity"]
    if (
        isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or not 0 <= threshold <= 1
        or not math.isfinite(threshold)
    ):
        raise ValueError("memory_policy.min_similarity must be a finite number in 0..1")
    limits: dict[str, int] = {}
    for key in ("top_k", "max_tokens"):
        number = value[key]
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise ValueError(f"memory_policy.{key} must be an integer >= 1")
        limits[key] = number
    return Policy(
        enabled=enabled,
        source_types=lists["source_types"],
        allow_paths=lists["allow_paths"],
        redact_rules=lists["redact_rules"],
        min_similarity=float(threshold),
        top_k=limits["top_k"],
        max_tokens=limits["max_tokens"],
    )
