#!/usr/bin/env python3
"""Cheap, non-role advisory helpers: file ranking, log summarizing, and rough risk hints.

This module runs outside the brief/report/self-report/heartbeat dispatch contract. It imports
nothing from ledger.py, contract.py, or coordinator.py, writes no state, and creates no batch,
dispatch, or ledger record. Every call recomputes its output from the inputs given; nothing here
is persisted or reused as evidence for another call. The coordinator/contract validation path
must never read this module's output as authorization to create a dispatch, lower risk, accept
QA, or change scope — the ledger-owned `coordinator.py risk assess` command remains the sole
authority on mandatory review."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def rank_files(paths: list[str], keywords: list[str]) -> list[dict[str, object]]:
    """Rough relevance ranking by keyword hits in each path. Advisory only."""
    normalized_keywords = [keyword.casefold() for keyword in keywords if keyword]
    scored = [
        (sum(1 for keyword in normalized_keywords if keyword in path.casefold()), path) for path in paths
    ]
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [{"path": path, "score": score} for score, path in scored]


_LOG_MARKER = re.compile(r"(?i)\b(error|fail(?:ed|ure)?|exception|traceback|warning)\b")


def summarize_log(text: str, *, max_lines: int = 20) -> dict[str, object]:
    """Rough, cheap log summary: line count plus the lines most likely to matter. Advisory only —
    a completion report or QA finding still cites the full sanitized artifact as evidence."""
    lines = text.splitlines()
    flagged = [line for line in lines if _LOG_MARKER.search(line)]
    return {
        "line_count": len(lines),
        "head": lines[:max_lines],
        "tail": lines[-max_lines:],
        "flagged": flagged[:max_lines],
    }


def classify_risk(text: str, known_triggers: list[str]) -> list[str]:
    """Rough, non-authoritative risk-trigger hint from free text (e.g. DoD plus changed file
    names). Advisory only — it never writes a risk_assessment record."""
    normalized = text.casefold()
    hits = []
    for trigger in known_triggers:
        words = [word for word in re.split(r"[^a-z0-9]+", trigger.casefold()) if word]
        if trigger.casefold() in normalized or any(re.search(rf"\b{re.escape(word)}\b", normalized) for word in words):
            hits.append(trigger)
    return hits


def _emit(kind: str, output: object) -> None:
    print(json.dumps({"advisory": True, "kind": kind, "output": output}, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ephemeral, non-role advisory helpers. Output is never persisted or accepted "
        "as dispatch authorization."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    rank = commands.add_parser("rank-files", help="Rough keyword relevance ranking for a list of paths.")
    rank.add_argument("--keyword", action="append", default=[])
    rank.add_argument("path", nargs="+")

    summarize = commands.add_parser("summarize-log", help="Rough summary of a log file.")
    summarize.add_argument("--file", required=True)
    summarize.add_argument("--max-lines", type=int, default=20)

    classify = commands.add_parser("classify-risk", help="Rough risk-trigger hint from free text.")
    classify.add_argument("--text", required=True)
    classify.add_argument("--known-trigger", action="append", required=True)

    args = parser.parse_args(argv)

    if args.command == "rank-files":
        _emit("rank-files", rank_files(args.path, args.keyword))
    elif args.command == "summarize-log":
        text = Path(args.file).read_text(encoding="utf-8")
        _emit("summarize-log", summarize_log(text, max_lines=args.max_lines))
    elif args.command == "classify-risk":
        _emit("classify-risk", classify_risk(args.text, args.known_trigger))
    return 0


if __name__ == "__main__":
    sys.exit(main())
