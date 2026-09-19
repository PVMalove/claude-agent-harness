"""Shared error base class for the harness's Python modules.

Every harness-specific exception carries a `.message` (what went wrong) and a required, keyword-only
`.remedy` (a concrete corrective action) instead of a bare human-readable string, so a caller can act
on a failure without reading the source. See docs/adr/0021-shared-harness-errors-base-class-with-remedy.md.
"""

from __future__ import annotations

import sys


class HarnessError(Exception):
    """Base class for every harness-specific error. Subclasses add no behaviour of their own."""

    def __init__(self, message: str, *, remedy: str) -> None:
        super().__init__(message)
        self.message = message
        self.remedy = remedy


INTERNAL_INVARIANT_REMEDY = "internal invariant violated -- file a bug report with this traceback"


def print_and_exit(exc: HarnessError) -> int:
    """Print a HarnessError's message and remedy to stderr and return the CLI exit code.

    Returns 2 rather than calling `sys.exit` itself, so each entrypoint's own
    `if __name__ == "__main__": raise SystemExit(main())`-style contract is untouched.
    """
    print(f"ERROR: {exc.message}\nREMEDY: {exc.remedy}", file=sys.stderr)
    return 2
