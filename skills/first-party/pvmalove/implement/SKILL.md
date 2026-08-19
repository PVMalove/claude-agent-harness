---
name: implement
description: "Implement a piece of work based on a spec or set of tickets."
disable-model-invocation: true
---

Implement the work described by the user in the spec or tickets.

Use /tdd where possible, at pre-agreed seams.

Run typechecking regularly, single test files regularly, and the full test suite once at the end.

Once done, use /code-review to review the work.

Commit your work to the current branch.

If this repo defines a git workflow doc (e.g. `docs/agents/git-workflow.md`), follow it to open a PR, then pause and ask the developer whether they want to review before it's considered done. Never merge the PR yourself — merging is the developer's call.

Before opening the PR, run the `qa-gate` skill if this repo has one — it's the full local check/test suite, run in isolation so its output doesn't clutter this context. Only proceed to `gh pr create` once it passes. If the git workflow doc specifies a PR body template with more structure than a short summary (e.g. numbered sections), delegate the body to the `pr-composer` subagent if this repo has one — it has its own read/diff access and fills the template properly, instead of you improvising a shorter body inline.
