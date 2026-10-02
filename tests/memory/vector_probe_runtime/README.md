# Isolated vector experiment (#431)

Production FTS5 and the project's dev environment do not depend on this runtime.
`uv.lock` pins every transitive dependency for Python 3.12; model and extension URLs,
sizes, SHA-256, licenses and inference settings are in `../vector_probe.lock.json`.
Only Linux x86-64 and Windows AMD64 are supported by this experiment. WSL must be
tested inside a real x86-64 WSL distribution, separately from ordinary Linux CI.

From the repository root, in bash (Git Bash on Windows):

```bash
export UV_PROJECT_ENVIRONMENT="$PWD/.harness/.sandboxes/runs/issue-431-vector/venv"
export HARNESS_VECTOR_PROBE_ARTIFACTS="$PWD/.harness/.sandboxes/runs/issue-431-vector/artifacts"
export HARNESS_VECTOR_PROBE_REQUIRED=1
uv sync --locked --project tests/memory/vector_probe_runtime
python scripts/memory_vector_probe.py --artifacts "$HARNESS_VECTOR_PROBE_ARTIFACTS"
uv run --locked --project tests/memory/vector_probe_runtime python -m pytest -q tests/memory/test_vector_probe.py tests/memory/test_vector_probe_integration.py
uv run --locked --project tests/memory/vector_probe_runtime python scripts/memory_vector_worker.py --artifacts "$HARNESS_VECTOR_PROBE_ARTIFACTS" --output .harness/.sandboxes/reports/issue-431-vector-comparison.json
```

The first two preparation commands may use the network. The worker verifies model
bytes and installed sqlite-vec native bytes against the downloaded pinned wheel,
then rejects socket connections during inference and evaluation. No hosted embedder
is used. Missing artifacts/runtime, hash drift, an unsupported platform, a failed
SQL probe or changed golden data/corpus fail the mandatory experiment. Outside this
explicit environment only the real integration test skips, keeping native libraries
optional for FTS5. That skip provides no platform evidence.

The worker recreates the hash-pinned corpus in a disposable Git repository below
`.harness/.sandboxes/runs/issue-431-vector/`, verifies the entire FTS5 baseline report,
then evaluates vector-only and all declared equal-weight RRF threshold candidates.
Document input is sanitized title plus body, truncated to 512 tokens including the
`passage: ` prefix. Queries use `query: `. No chunks are used. Mean pooling applies
the attention mask, then L2 normalization. sqlite-vec uses cosine; vector filtering
precedes RRF (offset 60), with path tie-breaks. This truncation is a limitation of
this candidate, not a conclusion about all vector retrieval approaches.

Reports are deterministic within one runtime and include per-ticket rankings,
recall and noise. Platform/runtime metadata may differ across machines. The threshold
grid is in-sample; it does not prove independent generalization. RU/EN smoke verifies
execution, not English retrieval quality. `recommendation: deferred` is intentional:
the worker does not possess Windows + WSL + CI evidence and never makes the
maintainer's final decision or closes follow-up issues.

The temporary `memory-vector-probe` workflow runs the same real experiment on
Linux and Windows when this issue branch is pushed. Its artifacts contain reports,
not weights. Record WSL command/output and OS details in the local task evidence,
then publish the relevant results in epic #420 after the maintainer's decision.
