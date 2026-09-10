# Pin clean-room QA to a candidate commit in one repository lane

Developer dispatches create a candidate commit; review, QA and publish briefs name that exact SHA and refuse a mismatched checkout. Full QA runs in a clean worktree under one portable repository-level quality-gate lock, so concurrent tickets cannot contaminate test resources while unrelated repositories remain independent.
