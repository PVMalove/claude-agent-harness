### Artifacts & Scratchpads Management

## Local runtime storage

Generated runtime data belongs under the main checkout's `.harness/.sandboxes/`, shared by its linked
worktrees. The layout is `runs/` for disposable environments (e.g. `runs/tests/` for verification runs,
`runs/qa/` for QA checkouts, `runs/drift/` for upstream checks), `cache/` for rebuildable caches
(e.g. `cache/repo_map/`), `logs/` for test and execution logs, `reports/` for generated reports,
`worktrees/` for managed issue checkouts, `scratch/` for role inboxes (`scratch/inbox/`), and
`pr_body/` for one-shot publication files. `.harness/orchestration/state/` is the durable ledger and is never
part of cleanup.

Use `harness cleanup <repo> --mode soft` to preview removal of expired temporary runs (`runs/` >24h),
scratch files (`scratch/`, `pr_body/`), expired logs (`logs/` >24h), and legacy top-level directories outside
`.sandboxes/` (`.cache`, `test-logs`, `tmp`, `reports`, `scratch`). Add `--apply` to execute it. `--mode hard`
also previews clearing rebuildable caches (`cache/`), reports (`reports/`), and old clean managed
worktrees (`worktrees/`). Execute hard cleanup with `--apply --confirm HARD`. Both modes default to a
24-hour minimum age for temporary data and worktrees; adjust with `--min-age-hours`. Active runs,
ledger-referenced worktrees, dirty trees, and branches with commits absent from `origin` are preserved.
Remote branches are never deleted.

System-wide architecture and the boundary between source documents and local evidence are described
in [harness-guide.md](../../.harness/docs/harness-guide.md). This guide defines only task artifacts and scratchpads.

* **Storage Location:** Save specifications, scratchpads, and intermediate files inside the project repository, not in system temporary directories (`AppData/Local/Temp`, `/tmp`): `block-scratch-outside-docs-tasks.sh` rejects writes there.
* **Project Directory:** Save intermediate task documents in `docs/tasks/` (create it if it doesn't exist).
* **PR bodies:** A PR body or comment — including a `task-report::required` completion report posted as an issue comment — is one-shot publication metadata, not a task artifact. Save it only under `.harness/.sandboxes/pr_body/`, never in `docs/tasks/`; delete it after the `gh`/`glab` command succeeds and keep it when the command fails so it can be retried.
  * **Filename is enforced, not just the directory:** `block-scratch-outside-docs-tasks.sh` only allows a file under `.harness/.sandboxes/pr_body/` when its *basename* contains `pr-body`, `pr-comment`, or `issue-comment` (case-insensitive) — e.g. `.harness/.sandboxes/pr_body/pr-body-<issue>-<slug>.md` or `.harness/.sandboxes/pr_body/issue-comment-<issue>-<slug>.md`. A differently-named file in that same, otherwise-correct directory (e.g. `report-<issue>-completion.md`, `notes.md`) is rejected by the hook exactly like a generic system-temp path — the fix is renaming the file, not changing the directory.
* **Pre-publish only:** `docs/tasks/` holds drafts and scratchpads *before* a spec or ticket is published to the issue tracker — never treat it as the tracker of record. Once published, the durable record lives with that tracker instead: the issue itself for GitHub/GitLab, or `.scratch/<feature-slug>/spec.md` and `.scratch/<feature-slug>/issues/` for the local markdown tracker (see `docs/agents/issue-tracker.md`) — which doesn't use `docs/tasks/` at all.
* **Naming Convention:** Every specification or scratchpad MUST include the tracker issue ID (if it exists) and a descriptive name — this names both the file and the folder that contains it (see below). If the ID isn't known yet, use a descriptive slug and rename both once it's generated.
  * *Example:* `issue-45-search-pagination`.
* **One folder per spec:** Every spec or ticket — standalone or part of an epic — gets its own folder directly under `docs/tasks/`, never a bare file. The spec/ticket file itself lives at the root of that folder, alongside two reserved subfolders: `tickets/` (one file per child ticket — only when this is an epic with children, see below) and `artifacts/` (the Live Artifact from `/grilling`: the list of Discovery Context paths approved during that session; `/to-spec` copies the same list into the spec's own `## Relevant Files (Discovery Context)` section).
  * *Example (standalone):* `docs/tasks/issue-45-search-pagination/issue-45-search-pagination.md`, plus `docs/tasks/issue-45-search-pagination/artifacts/discovery-context.md` if `/grilling` ran first.
  * *Epic grouping:* If the ticket belongs to a parent epic (has a `## Parent`/`Blocked by` reference to another issue, or is linked to it as a native GitHub sub-issue — see `docs/agents/triage-labels.md`), the folder is named after the epic instead — `issue-<epic-id>-<epic-slug>/` — and holds the epic's own spec file at its root, plus every subtask's body under `tickets/` (one file per ticket, not flat alongside the spec). Individual filenames inside keep the normal naming convention; only the folder groups them.
    *Example:* epic #26 ("search-revamp") with subtasks #27 and #28:
    ```
    docs/tasks/issue-26-search-revamp/
      issue-26-spec-search-revamp.md
      tickets/
        issue-27-pagination.md
        issue-28-filters.md
      artifacts/
        discovery-context.md
    ```
* **Workflow:** During `/to-spec` or when creating a scratchpad, explicitly write the file to its own folder under `docs/tasks/` (inside the epic folder if one applies). Never commit these files — `docs/tasks/` is gitignored by design. This is not a scratch draft under deletion, though: unlike the one-shot PR/comment body under `.harness/.sandboxes/pr_body/` (deleted right after publication succeeds), the contents of `docs/tasks/` are a permanent local archive — no skill or tool deletes them after a spec or ticket is published. If something needs a permanent, *committed* trail in the repository itself, that belongs in `docs/adr/` via `domain-modeling`, not here.
