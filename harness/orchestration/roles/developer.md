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

Use this role for ordinary backend service changes that remain inside the declared service or bounded
context zone. Do not perform schema/data migration work or outbox, message-schema, routing, retry, or
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
Do not report the candidate SHA alone when it hides multiple commits.

A developer-retry brief whose `carried_items` is not empty hands the retry obligations an earlier
decision recorded, keyed by source: each `coordinator-finding` is a defect the coordinator found when
it accepted a developer report, and each `review-finding` is a Standards or Spec finding of the
retried code-review; every item has an `item_id`, `summary`, `files` and `expected_evidence`. Close
every carried item in this retry: its files belong to the working set, and its fix goes into the
commit of the plan entry whose scope it belongs to. In the completion report's `output`, name each
`item_id` with the evidence that closes it (commit SHA, `file:line`, test name), as its
`expected_evidence` asks. An item that cannot be closed inside the zone and the prohibited changes
is a blocker, never a silent omission: the next code-review carries every open coordinator finding
again and accounts for it.
