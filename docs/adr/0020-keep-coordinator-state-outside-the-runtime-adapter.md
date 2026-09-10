# Keep coordinator state outside the runtime adapter

A runtime-neutral coordinator CLI owns batch and dispatch state, immutable briefs and reports, QA leases and validation. It stores canonical JSON and renders Markdown for people under gitignored `.harness/orchestration/state/`; `orca_adapter.py` only transports an already approved dispatch, so it cannot become a scheduler or accept human decisions.
