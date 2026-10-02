"""Read-only diagnostics for the optional shared project-memory cache."""

from __future__ import annotations

import re
import sqlite3

from harness.memory.index import LOCK_TIMEOUT, SCHEMA_VERSION, context as memory_context

from ..context import HealthContext
from ..model import CheckResult, Fix


def check_index(context: HealthContext) -> CheckResult:
    """Report optional memory state without creating or rebuilding the cache."""
    canonical, path, policy = memory_context(context.repo)
    if not policy.active:
        return CheckResult(
            id="memory.index",
            group="memory",
            status="skipped",
            message="память выключена или список источников пуст",
        )
    remedy = Fix(
        "Пересоберите индекс из главного checkout.",
        context.harness_command("memory", "rebuild", str(canonical)),
    )
    if not path.is_file():
        return CheckResult(
            id="memory.index",
            group="memory",
            status="warn",
            message="индекс отсутствует",
            fix=remedy,
        )
    try:
        connection = sqlite3.connect(
            path.as_uri() + "?mode=ro", uri=True, timeout=LOCK_TIMEOUT
        )
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            manifest = dict(connection.execute("SELECT key,value FROM manifest"))
            if (
                manifest.get("schema_version") != SCHEMA_VERSION
                or manifest.get("policy_fingerprint") != policy.fingerprint
            ):
                return CheckResult(
                    id="memory.index",
                    group="memory",
                    status="warn",
                    message="индекс несовместим со схемой или политикой памяти",
                    fix=remedy,
                )
            schema = connection.execute(
                "SELECT sql FROM sqlite_schema WHERE type='table' AND name='search_text'"
            ).fetchone()
            if (
                schema is None
                or re.match(
                    r"CREATE\s+VIRTUAL\s+TABLE\s+.+?\s+USING\s+fts5\s*\(",
                    schema[0],
                    flags=re.IGNORECASE | re.DOTALL,
                )
                is None
            ):
                raise ValueError("memory index requires an FTS5 table")
            count = connection.execute("SELECT count(*) FROM documents").fetchone()[0]
            fts_count = connection.execute(
                "SELECT count(*) FROM search_text"
            ).fetchone()[0]
            matched_count = connection.execute(
                "SELECT count(*) FROM documents d JOIN search_text ON d.id=search_text.rowid"
            ).fetchone()[0]
            if (
                fts_count != count
                or matched_count != count
                or connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]
            ):
                raise ValueError("memory index integrity mismatch")
            size = path.stat().st_size
        finally:
            connection.close()
    except (sqlite3.Error, OSError, ValueError):
        return CheckResult(
            id="memory.index",
            group="memory",
            status="warn",
            message="индекс недоступен или повреждён; проверьте поддержку FTS5",
            fix=remedy,
        )
    return CheckResult(
        id="memory.index",
        group="memory",
        status="ok",
        message=f"индекс валиден (FTS5); размер: {size} байт; источников: {count}",
    )


def check_model(context: HealthContext) -> CheckResult:
    """Describe the FTS5 slice; vector model delivery belongs to the later slice."""
    _, _, policy = memory_context(context.repo)
    message = (
        "модель не скачана; FTS5 работает без модели (векторный срез ещё не подключён)"
        if policy.active
        else "модель не требуется: память выключена или список источников пуст"
    )
    return CheckResult(
        id="memory.model", group="memory", status="skipped", message=message
    )
