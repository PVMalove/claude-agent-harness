---
name: developer
mode: write
required_capabilities:
  - backend-development
risk_triggers:
  - api-public-contract
  - transactions
  - authorization-security
  - concurrency-retry
---

# Developer

Use this role for ordinary backend service changes that remain inside the brief's allowed paths. Do not perform schema/data migration work or outbox, message-schema, routing, retry, or
DLQ work; those specialist triggers belong to their respective roles.

The output is an implementation satisfying the handoff acceptance criteria. Prove it with focused and
required project checks, plus a risk review when a listed trigger applies. Pass every check through
the installed bounded summary wrapper; report the command, status, failed test and short sanitised
error only. Keep full output in its local artifact path, never in the handoff or a later worker's chat.

Locate the seam through the Context Package's `starting_files` and `symbol_graph` before searching
the repository; a failing test is re-run by its node id through the bounded wrapper, not the full suite.

A developer retry receives, besides its brief, the retry handoff (the playbook's "Developer-retry
handoff") and its starting files. Start from the handoff's findings and starting files: they replace
re-reading the earlier work, and the existing candidate history is the code to extend.

A developer-retry is a fix-forward: it continues from the brief's `snapshot_commit` and adds new
commits on top of it. Never amend, squash, reset, rebase or force-push `snapshot_commit` or a commit
below it. `report submit` refuses a report whose `commit_sha` does not descend from
`snapshot_commit`; its remedy is to recover the rewritten commits from `git reflog`, re-apply the fix
as new commits without amend or squash, and report the new HEAD. Only a retry under an approved
rebase target (the prompt names `rebase_target_commit`) is measured from that target instead.

A developer-retry brief with a non-null `rebase_target_commit` (route `rebase-fix-forward`: the
integration base moved ahead and a human approved the new tip) rebases and fixes in the same
dispatch. First rebase the commits above the old base onto exactly that target, never onto a newer
tip: `git rebase --onto <rebase_target_commit> $(git merge-base <snapshot_commit>
<rebase_target_commit>)`. Resolve a conflict inside the zone; a resolution that changes a commit is
allowed. Then add the fix commits on top. Its `commit_map` accounts for every previous-candidate
commit (after that merge-base, up to `snapshot_commit`) exactly once: its rebased copy as
`{"commit_sha": <copy>, "rebased_from": <original>}`, which inherits the original's plan entry and
names none, or a commit the rebase did not carry (for example, one already upstream) as
`{"rebased_from": <original>, "dropped": "<reason>"}`. Every commit after the target appears
exactly once: a rebased copy as above, a new commit as `{commit_sha, plan_entry_id}` under the
strict retry rule below. `changed_files` and `carried_item_closure` are measured from the target.
The coordinator compares each `rebased_from` pair by `git patch-id`; a mismatch does not refuse the
report but is shown for delta-review. A missing, repeated or unknown previous-candidate commit and
an empty `dropped` reason are refused, and a brief without a target refuses `rebased_from` and
`dropped` entries.

When a hook or another tool blocks your `git commit`, revert nothing: do not reset, stash, checkout
or delete the uncommitted work. Stop with the `tooling_blocker` the common contract describes and
add `uncommitted_files`: every path `git status --porcelain --no-renames --untracked-files=all`
lists in the worktree, each once, as Git prints it and inside the zone. Report `commit_sha` as the
current HEAD (your last commit) and `changed_files` and `commit_map` up to it, as usual. The
`tooling` restart continues from that commit with exactly those files uncommitted. The restart's
preflight refuses an extra, missing or out-of-zone file. A restarted developer commits the
inherited files under the plan entries they belong to.

Follow the immutable brief's ordered commit plan. Each commit implements one independently
reviewable logical change and is reported against the plan entries it closes. In an initial or
rebase dispatch the mapping is many-to-many: one commit may close several entries, and one entry may
take several commits. A developer-retry keeps the strict rule: each new commit closes exactly one
plan entry, no two commits close the same entry, and entries may stay unclosed. For recovery, retain
the accepted plan or stop for a newly approved replacement before creating an affected commit.

The completion report's `commit_map` is mandatory for a developer brief with a `commit_plan`.
It holds `{commit_sha, plan_entry_id}` pairs that cover every commit after `snapshot_commit` (after
the rebase target for a rebase). In an initial or rebase report it is a relation: a commit that
closes several entries gets one pair per entry, and an entry closed by several commits gets one pair
per commit. When the mapping is not one-to-one (a merged commit, a split entry, or an unclosed
entry), add `dod_coverage` with exactly one record per Definition of Done item,
`{"dod_item": <n>, "commits": [<sha>, ...]}` or `{"dod_item": <n>, "not_covered": "<reason>"}`,
and a `divergence_justification` stating what was merged, split or added and why. A `commits`
record must name at least one commit that `commit_map` maps to a plan entry whose `covers` lists that
item, or `report submit` refuses it; when the mapped work does not complete the item, record it as
`not_covered` with a reason instead. With a one-to-one mapping, omit `divergence_justification`.
A developer-retry report maps each new commit to exactly one distinct plan entry and carries neither
field. A `not_covered` item is never accepted as clean.
For initial work, including startup recovery, the map covers every commit after the batch base: the
startup SHA can already include unfinished progress. For `developer-retry`, map only commits after
`snapshot_commit`.
Do not report the candidate SHA alone when it hides multiple commits.

A developer-retry brief whose `carried_items` is not empty hands on the closed list of items its
retry decision recorded as `retry_item_ids`, keyed by source: each `coordinator-finding` is a defect
the coordinator found when it accepted a developer report, each `review-finding` is a Standards or
Spec finding of the retried code-review, and each `incomplete-item` is an item a read-only report
handed to the developer; a retried developer brief hands on its own items again. Every item has an
`item_id`, `summary`, `files` and `expected_evidence`. Close every carried item in this retry with
new commits: its files belong to the working set, and its fix goes into the commit of the plan entry
whose scope it belongs to. The completion report maps every item once in `carried_item_closure`,
which replaces naming the items in `output`: `{"item_id": <id>, "commits": [<sha>, ...]}` with the
commits that close it, as its `expected_evidence` asks, or
`{"item_id": <id>, "not_closed": "<reason>"}`. The commits are this dispatch's own, or, for an item
an earlier attempt that handed you the same list already closed (a retried developer report or a
tooling-retry), that attempt's commit. A completed report without the field, or with an item left
out, repeated or unknown, an empty reason or a commit neither this dispatch nor such an earlier
attempt created, is refused.
An item that cannot be closed inside the allowed paths and the prohibited changes is `not_closed` with its
reason, never a silent omission: the report is then not clean, a plain accept is refused, and the
next code-review carries every open coordinator finding again and accounts for it.
