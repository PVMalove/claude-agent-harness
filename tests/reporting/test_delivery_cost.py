"""Characterize source-backed cost calculation before separating it from I/O."""

from harness.reporting.delivery_stats import estimate_cost


def test_estimate_cost_uses_supplied_rates_and_cache_multipliers() -> None:
    claude = {
        "status": "ok",
        "models": {
            "sonnet": {
                "input_tokens": 100,
                "cache_creation_input_tokens": 400,
                "cache_read_input_tokens": 900,
                "output_tokens": 250,
            },
            "unknown": {
                "input_tokens": 20,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "output_tokens": 0,
            },
        },
    }
    rates = {
        "status": "ok",
        "currency": "USD",
        "effective_date": "2026-09-25",
        "models": {
            "sonnet": {
                "input": 1,
                "output": 10,
                "cache_write_multiplier": 2,
                "cache_read_multiplier": 0.5,
            }
        },
    }

    result = estimate_cost(claude, {"status": "нет данных"}, rates)

    assert result["total"] == 0.00385
    assert result["uncached_total"] == 0.0039
    assert result["cache_saving"] == 0.00005
    assert result["unpriced_models"] == ["unknown"]
