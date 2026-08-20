---
name: implement
description: "Implement a piece of work based on a spec or set of tickets."
disable-model-invocation: true
---

Implement the work described by the user in the spec or tickets.

If the invocation or immediate context names a specific tracker ticket (an issue number, URL, or a `.scratch/<feature>/issues/NN-*.md` path), check it before starting. If it carries `blocked-by-ticket` (see `docs/agents/triage-labels.md`), check its blockers using this repo's tracker (native GitHub/GitLab dependency links, or the `Blocked by:`/`**Blocked by:**` field — see `docs/agents/issue-tracker.md`). If every blocker is closed/resolved, remove `blocked-by-ticket` and apply `ready-for-agent` (or the equivalent `**Status:**` line for a local ticket file), then proceed. If any blocker is still open, stop and tell the user which ones — don't start the work.

If no ticket reference is given: when this repo defines a git workflow doc (e.g. `docs/agents/git-workflow.md`) with an "Issue First" rule, stop and ask the user to name an existing ticket or run `/to-spec`/`/to-tickets` first — don't start the work. Otherwise, skip this check entirely.

Use /tdd where possible, at pre-agreed seams.

Run typechecking regularly, single test files regularly, and the full test suite once at the end.

Once done, use /code-review to review the work.

Commit your work to the current branch.

If this repo defines a git workflow doc (e.g. `docs/agents/git-workflow.md`), follow it to open a PR, then pause and ask the developer whether they want to review before it's considered done. Never merge the PR yourself — merging is the developer's call.

Before opening the PR, run the `qa-gate` skill if this repo has one — it's the full local check/test suite, run in isolation so its output doesn't clutter this context. Only proceed to `gh pr create` once it passes. If the git workflow doc specifies a PR body template with more structure than a short summary (e.g. numbered sections), delegate the body to the `pr-composer` subagent if this repo has one — it has its own read/diff access and fills the template properly, instead of you improvising a shorter body inline.

If this work is tied to the tracker ticket checked above: for a GitHub/GitLab-tracked ticket, the git workflow doc's mandatory `Closes #<ID>` in the PR body already closes it on merge — nothing further to do here. For a local-markdown-tracked ticket (no PR/merge step), set its `**Status:**` line to `done` once this ticket's work is complete.
