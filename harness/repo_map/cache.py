"""Content-addressed кэш результатов Repo Map.

Кэш только ускоряет повторный запуск: ключ включает каждый вход, влияющий на сериализованную карту,
запись защищена SHA-256 и заново проверяется по схеме, а любая ошибка чтения или записи означает
промах. Удаление кэша не меняет результат.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path

from harness.repo_map.contract import validation_error
from harness.repo_map.policy import RepoMapPolicy
from harness.token_estimator import TOKEN_ESTIMATOR_VERSION

_PACKAGE_DIR = Path(__file__).resolve().parent
# Every file of the Repo Map package that can change a serialized map: its code and output schema.
_SOURCE_PATTERNS = ("*.py", "repo_map.schema.json")


@lru_cache(maxsize=1)
def parser_identity() -> str:
    """SHA-256 исходников пакета Repo Map и схемы; вычисляется один раз на процесс.

    Хешируется каждый модуль пакета, поэтому новый модуль автоматически входит в ключ кэша.
    """
    digest = hashlib.sha256()
    sources = sorted(
        {path for pattern in _SOURCE_PATTERNS for path in _PACKAGE_DIR.glob(pattern)}
    )
    for path in sources:
        digest.update(path.name.encode("utf-8") + b"\0" + path.read_bytes())
    return digest.hexdigest()


def cache_key(
    pinned: str,
    seeds: list[str],
    max_tokens: int,
    policy: RepoMapPolicy,
    backend_identity: str | None,
) -> str:
    """Вычислить ключ кэша по всем входам, влияющим на сериализованную карту.

    `backend_identity` — идентичность парсера полного режима (выбранный bundle) или `None` для
    уровня `minimal`.
    """
    identity = {
        "commit": pinned,
        "seeds": seeds,
        "max_tokens": max_tokens,
        "policy": asdict(policy),
        "parser_identity": parser_identity(),
        "bundle_identity": backend_identity,
        "token_estimator_version": TOKEN_ESTIMATOR_VERSION,
    }
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def read_cache(cache_dir: Path, key: str, pinned: str) -> str | None:
    """Вернуть запись, соответствующую схеме, запрошенному ключу и коммиту."""
    try:
        envelope = json.loads((cache_dir / f"{key}.json").read_text(encoding="utf-8"))
        if not isinstance(envelope, dict):
            return None
        payload = envelope.get("payload")
        digest = envelope.get("sha256")
        if envelope.get("key") != key or not isinstance(payload, str) or not isinstance(digest, str):
            return None
        if hashlib.sha256(payload.encode("utf-8")).hexdigest() != digest:
            return None
        parsed: object = json.loads(payload)
        if not isinstance(parsed, dict) or parsed.get("commit") != pinned:
            return None
        if validation_error(parsed) is not None:
            return None
        return payload
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def write_cache(cache_dir: Path, key: str, payload: str) -> None:
    """Атомарно записать результат в кэш по возможности; кэш никогда не становится авторитетным."""
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        envelope = json.dumps(
            {
                "key": key,
                "sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                "payload": payload,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        descriptor, temporary = tempfile.mkstemp(dir=cache_dir, prefix=f".{key}.", suffix=".tmp")
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(envelope)
        Path(temporary).replace(cache_dir / f"{key}.json")
    except OSError:
        return
