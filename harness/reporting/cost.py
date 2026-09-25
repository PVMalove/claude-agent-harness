"""Rate card validation and source-backed model cost calculations."""

from __future__ import annotations

import json
from pathlib import Path

from harness.reporting.common import MISSING, JsonObject, StatsError


def _rate(card: object, field: str) -> float:
    if not isinstance(card, dict):
        return 0.0
    try:
        return float(card.get(field, 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _is_priced(card: object) -> bool:
    """A model is priced only when it carries a rate above zero.

    The shipped template lists models at 0.0 so the shape is obvious. Treating those zeros as real
    prices is what turns an unfilled card into a confident '0.00'.
    """
    return _rate(card, "input") > 0 or _rate(card, "output") > 0


def load_rates(path: Path | None) -> JsonObject:
    if path is None or not path.is_file():
        return {
            "status": MISSING,
            "reason": f"тариф не настроен: нет файла {path}",
            "models": {},
        }
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise StatsError(
            f"rate card is not valid JSON: {path}",
            remedy=f"fix the JSON syntax in {path}",
        ) from exc
    if not isinstance(value, dict) or not isinstance(value.get("models"), dict):
        raise StatsError(
            "rate card must be an object with a models object",
            remedy=f"set {path} to a JSON object with a top-level 'models' object",
        )
    if not any(_is_priced(card) for card in value["models"].values()):
        # An untouched template is not a rate card: every price is 0.0. Reporting its total as a
        # real 0.00 would be the tool asserting a number nobody gave it.
        return {
            "status": MISSING,
            "reason": f"тариф не заполнен — все ставки нулевые: {path}",
            "models": {},
        }
    value.setdefault("status", "ok")
    return value


def estimate_cost(
    claude: JsonObject, codex: JsonObject, rates: JsonObject
) -> JsonObject:
    """Cost by the supplied rate card only. No prices are built into this tool."""
    if rates.get("status") != "ok":
        return {"status": MISSING, "reason": rates.get("reason", "тариф не настроен")}
    table = rates["models"]
    per_model: list[JsonObject] = []
    total = 0.0
    uncached_total = 0.0
    unpriced = []

    def price(
        model: str, fresh: int, cache_write: int, cache_read: int, output: int
    ) -> None:
        nonlocal total, uncached_total
        card = table.get(model)
        if not _is_priced(card):
            # Includes a model left at the template's 0.0: absent from the card and priced at zero
            # are the same statement — nobody said what this model costs.
            unpriced.append(model)
            return
        rate_in = _rate(card, "input") / 1_000_000
        rate_out = _rate(card, "output") / 1_000_000
        write_multiplier = float(card.get("cache_write_multiplier", 1.25))
        read_multiplier = float(card.get("cache_read_multiplier", 0.1))
        cost = (
            fresh * rate_in
            + cache_write * rate_in * write_multiplier
            + cache_read * rate_in * read_multiplier
            + output * rate_out
        )
        uncached = (fresh + cache_write + cache_read) * rate_in + output * rate_out
        total += cost
        uncached_total += uncached
        per_model.append(
            {
                "model": model,
                "cost": round(cost, 6),
                "uncached_cost": round(uncached, 6),
            }
        )

    if claude.get("status") == "ok":
        for model, bucket in claude["models"].items():
            price(
                model,
                bucket["input_tokens"],
                bucket["cache_creation_input_tokens"],
                bucket["cache_read_input_tokens"],
                bucket["output_tokens"],
            )
    if codex.get("status") == "ok":
        for model, bucket in codex["models"].items():
            price(
                model,
                bucket["input_tokens"] - bucket["cached_input_tokens"],
                bucket["cache_write_input_tokens"],
                bucket["cached_input_tokens"],
                bucket["output_tokens"],
            )

    per_model.sort(key=lambda item: item["cost"], reverse=True)
    return {
        "status": "ok",
        "currency": rates.get("currency", "USD"),
        "rates_effective": rates.get("effective_date", MISSING),
        "rates_source": rates.get("source", MISSING),
        "per_model": per_model,
        "total": round(total, 6),
        "uncached_total": round(uncached_total, 6),
        "cache_saving": round(uncached_total - total, 6),
        "unpriced_models": sorted(set(unpriced)),
    }
