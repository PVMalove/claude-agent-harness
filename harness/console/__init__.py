"""Harness console: an interactive TUI over the same public facts `harness health` and the
backend-orchestration ledger already expose.

Everything up to and including `data.py` and `runner.py` is stdlib-only and importable even when
`textual` is not installed - the offline/no-`uv` fallback path (see `launcher.py`) never imports
`textual`. Only `app.py` and `screens/*.py` import it, and only after `launcher.run_console` has
already decided the process is running inside the relaunched `uv run --with textual==<pin>`
subprocess. See docs/adr/0025-harness-console-textual-via-uv-with-stdlib-fallback.md.
"""
