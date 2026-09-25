"""Versioned delivery baseline snapshots and source-backed comparison."""

from __future__ import annotations

import json
from pathlib import Path

from harness.reporting.common import (
    BASELINE_SCHEMA_VERSION,
    CLAUDE_FIELDS,
    MISSING,
    JsonObject,
    StatsError,
    _int,
)


def _cache_tokens(
    models: JsonObject, write_field: str, read_field: str
) -> JsonObject | None:
    """Cache write/read tokens by the same rule as the input total: only when every model bucket
    carries both fields, never a partial sum presented as complete."""
    if not models or any(
        not isinstance(bucket, dict)
        or any(
            not isinstance(bucket.get(field), int)
            or isinstance(bucket.get(field), bool)
            for field in (write_field, read_field)
        )
        for bucket in models.values()
    ):
        return None
    return {
        "cache_write_tokens": sum(
            _int(bucket.get(write_field)) for bucket in models.values()
        ),
        "cache_read_tokens": sum(
            _int(bucket.get(read_field)) for bucket in models.values()
        ),
    }


def _provider_snapshot(
    usage: JsonObject,
    input_fields: tuple[str, ...],
    *,
    cache_fields: tuple[str, str] | None = None,
) -> JsonObject:
    """The comparable provider telemetry from one report, without filling absent data with zero."""
    if usage.get("status") != "ok":
        return {"status": MISSING, "reason": usage.get("reason", MISSING)}
    models = usage.get("models")
    if not isinstance(models, dict):
        return {"status": MISSING, "reason": "в отчёте нет telemetry по моделям"}
    fields = (*input_fields, "output_tokens")
    if not models or any(
        not isinstance(bucket, dict)
        or any(
            not isinstance(bucket.get(field), int)
            or isinstance(bucket.get(field), bool)
            for field in fields
        )
        for bucket in models.values()
    ):
        return {"status": MISSING, "reason": "в отчёте неполная telemetry по моделям"}
    input_tokens = sum(
        sum(_int(bucket.get(field)) for field in input_fields)
        for bucket in models.values()
    )
    output_tokens = sum(_int(bucket.get("output_tokens")) for bucket in models.values())
    snapshot = {
        "status": "ok",
        "attribution": usage.get("attribution", MISSING),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }
    if cache_fields is not None:
        cache = _cache_tokens(models, *cache_fields)
        snapshot["cache_write_tokens"] = (
            cache["cache_write_tokens"] if cache else MISSING
        )
        snapshot["cache_read_tokens"] = cache["cache_read_tokens"] if cache else MISSING
    return snapshot


def baseline_snapshot(report: JsonObject) -> JsonObject:
    """Make the small, versioned baseline contract that later reports can compare."""
    epic = report["epic"]
    return {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "generated_at": report["generated_at"],
        "repository": report["repository"],
        "epic": {"number": epic.get("number"), "title": epic.get("title", MISSING)},
        "providers": {
            "claude": _provider_snapshot(
                report["claude"],
                CLAUDE_FIELDS[:3],
                cache_fields=("cache_creation_input_tokens", "cache_read_input_tokens"),
            ),
            "codex": _provider_snapshot(
                report["codex"],
                ("input_tokens",),
                cache_fields=("cache_write_input_tokens", "cached_input_tokens"),
            ),
        },
    }


def load_baseline(path: Path) -> JsonObject:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise StatsError(
            f"не прочитать baseline: {path}: {exc}",
            remedy=f"fix the file-system error above for {path} and retry",
        ) from exc
    except ValueError as exc:
        raise StatsError(
            f"baseline не является JSON: {path}",
            remedy=f"fix the JSON syntax in {path}",
        ) from exc
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != BASELINE_SCHEMA_VERSION
    ):
        raise StatsError(
            f"baseline имеет неподдерживаемый формат: {path}",
            remedy=f"regenerate {path} with --save-baseline so it matches schema_version={BASELINE_SCHEMA_VERSION}",
        )
    if not isinstance(value.get("providers"), dict) or not isinstance(
        value.get("epic"), dict
    ):
        raise StatsError(
            f"baseline не содержит providers и epic: {path}",
            remedy=f"regenerate {path} with --save-baseline so it has providers and epic",
        )
    for provider in ("claude", "codex"):
        telemetry = value["providers"].get(provider)
        if not isinstance(telemetry, dict):
            raise StatsError(
                f"baseline не содержит telemetry {provider}: {path}",
                remedy=f"regenerate {path} with --save-baseline so it has {provider} telemetry",
            )
        if telemetry.get("status") == "ok" and any(
            not isinstance(telemetry.get(field), int)
            or isinstance(telemetry.get(field), bool)
            for field in ("input_tokens", "output_tokens", "total_tokens")
        ):
            raise StatsError(
                f"baseline содержит неполную telemetry {provider}: {path}",
                remedy=f"regenerate {path} with --save-baseline so {provider}'s token fields are complete integers",
            )
    return value


def save_baseline(snapshot: JsonObject, path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        raise StatsError(
            f"не сохранить baseline: {path}: {exc}",
            remedy=f"fix the file-system error above for {path} and retry",
        ) from exc


def _cache_delta_field(
    baseline: JsonObject, current: JsonObject, field: str
) -> str | int:
    """A cache-token delta only when both sides actually carry that field -- one or both of them
    may predate this metric or come from a provider report with incomplete cache telemetry."""
    before, after = baseline.get(field), current.get(field)
    if (
        not isinstance(before, int)
        or isinstance(before, bool)
        or not isinstance(after, int)
        or isinstance(after, bool)
    ):
        return MISSING
    return after - before


def _provider_delta(baseline: object, current: object) -> str | JsonObject:
    if not isinstance(baseline, dict) or not isinstance(current, dict):
        return MISSING
    if baseline.get("status") != "ok" or current.get("status") != "ok":
        return MISSING
    delta: JsonObject = {
        field: _int(current.get(field)) - _int(baseline.get(field))
        for field in ("input_tokens", "output_tokens", "total_tokens")
    }
    for field in ("cache_write_tokens", "cache_read_tokens"):
        delta[field] = _cache_delta_field(baseline, current, field)
    return delta


def compare_baseline(baseline: JsonObject, current: JsonObject) -> JsonObject:
    """Compare only source-backed telemetry and preserve each side's attribution evidence."""
    providers = ("claude", "codex")
    return {
        "baseline": baseline,
        "current": current,
        "delta": {
            "providers": {
                provider: _provider_delta(
                    baseline.get("providers", {}).get(provider),
                    current.get("providers", {}).get(provider),
                )
                for provider in providers
            }
        },
    }
