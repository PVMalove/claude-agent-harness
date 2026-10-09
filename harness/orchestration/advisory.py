#!/usr/bin/env python3
"""Легковесные консультативные функции вне ролевой модели: ранжирование файлов, агрегация логов и подсказки по рискам.

Этот модуль работает вне контракта диспетчеризации (brief/report/self-report/heartbeat).
Он ничего не импортирует из ledger.py, contract.py или coordinator.py, не сохраняет состояние
и не создаёт записей batch, dispatch или ledger. Каждый вызов пересчитывает результат исключительно
на основе переданных входных данных; ничего из этого модуля не сохраняется и не используется в качестве
подтверждения для других вызовов. Контур валидации координатора/контракта никогда не должен трактовать
вывод этого модуля как авторизацию на создание dispatch, снижение риска, приёмку QA или изменение
области видимости — команда `coordinator.py risk assess` в ledger остаётся единственным авторитетным
источником для обязательного ревью.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def rank_files(paths: list[str], keywords: list[str]) -> list[dict[str, object]]:
    """Приблизительное ранжирование релевантности по вхождениям ключевых слов в каждый путь. Исключительно консультативно."""
    normalized_keywords = [keyword.casefold() for keyword in keywords if keyword]
    scored = [
        (sum(1 for keyword in normalized_keywords if keyword in path.casefold()), path)
        for path in paths
    ]
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [{"path": path, "score": score} for score, path in scored]


_LOG_MARKER = re.compile(r"(?i)\b(error|fail(?:ed|ure)?|exception|traceback|warning)\b")


def summarize_log(text: str, *, max_lines: int = 20) -> dict[str, object]:
    """Приблизительная легковесная сводка лога: число строк и наиболее важные строки. Исключительно консультативно —
    отчёт о завершении или замечание QA по-прежнему требуют ссылки на полный очищенный артефакт в качестве доказательства."""
    lines = text.splitlines()
    flagged = [line for line in lines if _LOG_MARKER.search(line)]
    # A non-positive limit selects nothing; lines[-0:] would otherwise return the whole log.
    limit = max(max_lines, 0)
    return {
        "line_count": len(lines),
        "head": lines[:limit],
        "tail": lines[-limit:] if limit else [],
        "flagged": flagged[:limit],
    }


def classify_risk(text: str, known_triggers: list[str]) -> list[str]:
    """Приблизительная неавторитетная подсказка триггеров риска по произвольному тексту (например, DoD и имена файлов).
    Исключительно консультативно — никогда не создаёт запись risk_assessment."""
    normalized = text.casefold()
    hits = []
    for trigger in known_triggers:
        words = [word for word in re.split(r"[^a-z0-9]+", trigger.casefold()) if word]
        if trigger.casefold() in normalized or any(
            re.search(rf"\b{re.escape(word)}\b", normalized) for word in words
        ):
            hits.append(trigger)
    return hits


def _emit(kind: str, output: object) -> None:
    """Вывести результат консультативного вызова в формате JSON."""
    print(
        json.dumps({"advisory": True, "kind": kind, "output": output}, sort_keys=True)
    )


def main(argv: list[str] | None = None) -> int:
    """Точка входа CLI для вспомогательных консультативных команд."""
    parser = argparse.ArgumentParser(
        description="Ephemeral, non-role advisory helpers. Output is never persisted or accepted "
        "as dispatch authorization."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    rank = commands.add_parser(
        "rank-files", help="Rough keyword relevance ranking for a list of paths."
    )
    rank.add_argument("--keyword", action="append", default=[])
    rank.add_argument("path", nargs="+")

    summarize = commands.add_parser(
        "summarize-log", help="Rough summary of a log file."
    )
    summarize.add_argument("--file", required=True)
    summarize.add_argument("--max-lines", type=int, default=20)

    classify = commands.add_parser(
        "classify-risk", help="Rough risk-trigger hint from free text."
    )
    classify.add_argument("--text", required=True)
    classify.add_argument("--known-trigger", action="append", required=True)

    args = parser.parse_args(argv)

    if args.command == "rank-files":
        _emit("rank-files", rank_files(args.path, args.keyword))
    elif args.command == "summarize-log":
        try:
            # A gate log may hold bytes that are not UTF-8; the summary is advisory only.
            text = Path(args.file).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            parser.error(f"cannot read --file {args.file}: {exc}")
        _emit("summarize-log", summarize_log(text, max_lines=args.max_lines))
    elif args.command == "classify-risk":
        _emit("classify-risk", classify_risk(args.text, args.known_trigger))
    return 0


if __name__ == "__main__":
    sys.exit(main())
