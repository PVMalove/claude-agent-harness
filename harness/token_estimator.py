"""Shared conservative payload size contract for portable resources."""

TOKEN_ESTIMATOR_VERSION = "utf8-bytes-per-2-v1"


def estimate_tokens(text: str) -> int:
    """Return a deterministic conservative estimate without a provider tokenizer.

    Two UTF-8 bytes per token leaves room for code, identifiers and non-ASCII text. It is a
    payload budget bound, not a provider billing estimate.
    """
    return estimate_tokens_for_bytes(len(text.encode("utf-8")))


def estimate_tokens_for_bytes(byte_count: int) -> int:
    """Return the same estimate as `estimate_tokens` for a UTF-8 payload of `byte_count` bytes."""
    return (byte_count + 1) // 2
