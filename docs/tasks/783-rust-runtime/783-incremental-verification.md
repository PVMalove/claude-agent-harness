# Spec #783.B — Incremental Verification and Failure-Scoped Retry
Status: proposed. Parent epic #783; ADR 0019.
## Problem
Full make verify takes over 20 minutes in user sessions, repeated across Developer, Review, QA and retries. scripts/verify.py defaults to disabled pytest cacheprovider; --failed-first is --ff, NOT failed-only. scripts/test_clean_room.py shares sequential scenario context. #756 #757 #758 and #666 are closed; extend, do not duplicate.
## Verification modes
Focused Developer: Git diff + repo_map impacted tests + Context Package related_tests; mandatory lint/type checks; widen conservatively if dependencies unknown. Freeze approved commands in brief.
Failed-only Developer: exact pytest nodeids from JUnit or per-worktree pytest --lf --lfnf=none. Diagnostic proof only; does not mark other tests passed.
Independent Review: focused checks run by reviewer independently, never developer self-report.
Full clean-room QA: entire approved gate and sequential E2E on exact final candidate SHA, mandatory before publish. New SHA invalidates full QA. Same SHA infrastructure retry must preserve approval and not burn developer budget.
## Evidence
Per command ID, phase, SHA, plan/image digest, environment fingerprint, nodeids, pass/fail/not_run, exit, durations, stdout/stderr sanitized refs and checksums. Never call unexecuted commands pass. Unknown errors must remain unknown.
## Cache
Developer pytest lastfailed and mypy caches persist per worktree, interpreter and dependency fingerprint. No cross-worktree mixing. A previously green cache entry never authorizes release. Clean-room shared-context scenarios are not independently rerunnable without refactoring.
## Code interfaces
scripts/verify.py, scripts/verification/process.py, harness/repo_map, harness/orchestration/{core/config.py,workflow/batch.py,workflow/dispatch.py,contract.py,qa_lane.py}, harness/gate_runner/gate_runner.py, .github/workflows/verify.yml.
## Acceptance
- A known failing nodeid reruns without the entire suite; after fix focused impacted suite required.
- No silent inheritance of full gate to configured Developer/Review; existing profiles remain compatible.
- Independent review and final full QA preserve semantics and immutable approval.
- Test changed SHA, changed dependency, missing nodeids, stale cache, partial output and offline uv.
- Measure full-suite invocation count and overall implement wall time before/after; no guessed benchmark.