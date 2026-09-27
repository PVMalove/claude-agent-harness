"""textual screens for the harness console. Every module here imports `textual` at module load
time - only harness.console.app imports this package, and only from the already-relaunched
`uv run --with textual==<pin>` subprocess (see harness.console.launcher.run_console)."""
