#!/usr/bin/env python3
"""Reject automated-agent attribution in commit messages and pull-request bodies."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


FORBIDDEN = re.compile(
    r"claude|claude\.ai/code/session|openai|chatgpt|gpt[-_ ]?[0-9]|copilot|gemini|codex|"
    r"co-authored[- ]by|ai[-_ ]?(agent|assistant|generated)",
    re.IGNORECASE,
)


def git_messages(commit_range: str) -> list[tuple[str, str]]:
    result = subprocess.run(
        ["git", "log", "--format=%H%x00%B%x00", "--no-merges", commit_range],
        capture_output=True,
        text=True,
        check=True,
    )
    fields = result.stdout.split("\x00")
    return list(zip(fields[0::2], fields[1::2]))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit-range")
    parser.add_argument("--pr-body-file", type=Path)
    args = parser.parse_args()

    violations: list[str] = []
    if args.commit_range:
        for commit, message in git_messages(args.commit_range):
            match = FORBIDDEN.search(message)
            if match:
                violations.append(f"commit {commit}: forbidden metadata near {match.group(0)!r}")
    if args.pr_body_file and args.pr_body_file.is_file():
        body = args.pr_body_file.read_text(encoding="utf-8")
        match = FORBIDDEN.search(body)
        if match:
            violations.append(f"PR body: forbidden metadata near {match.group(0)!r}")

    if violations:
        print("Public Git metadata check failed:", file=sys.stderr)
        for violation in violations:
            print(f"- {violation}", file=sys.stderr)
        return 1
    print("Public Git metadata check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
