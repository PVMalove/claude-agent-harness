"""Общий консервативный контракт оценки размера полезной нагрузки для переносимых ресурсов."""

from __future__ import annotations

TOKEN_ESTIMATOR_VERSION = "utf8-bytes-per-2-v1"


def estimate_tokens(text: str) -> int:
    """Вернуть детерминированную консервативную оценку токенов без токенизатора провайдера.

    Два байта UTF-8 на токен оставляют запас для кода, идентификаторов и символов non-ASCII.
    Это ограничение бюджета полезной нагрузки, а не оценка биллинга провайдера.
    """
    return estimate_tokens_for_bytes(len(text.encode("utf-8")))


def estimate_tokens_for_bytes(byte_count: int) -> int:
    """Вернуть ту же оценку, что и `estimate_tokens`, для полезной нагрузки размером `byte_count` байт в UTF-8."""
    return (byte_count + 1) // 2
