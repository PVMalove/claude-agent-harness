### Artifacts & Scratchpads Management

System-wide architecture and the boundary between source documents and local evidence are described
in [current-state.md](./current-state.md). This guide defines only task artifacts and scratchpads.

* **Storage Location:** NEVER use system temporary directories (e.g., `AppData/Local/Temp`, `/tmp`) for saving specifications, scratchpads, or intermediate files.
* **Project Directory:** All intermediate task-related documents MUST be saved locally inside the project repository in the `docs/tasks/` directory (create it if it doesn't exist).
* **PR bodies:** A PR body or comment is one-shot publication metadata, not a task artifact. Save it only under `.harness/scratch/tmp/` (for example, `.harness/scratch/tmp/pr-body-<issue>-<slug>.md`), never in `docs/tasks/`; delete it after the `gh`/`glab` command succeeds and keep it when the command fails so it can be retried.
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
* **Workflow:** During `/to-spec` or when creating a scratchpad or, creating roadmaps (`/wayfinder`), explicitly write the file to its own folder under `docs/tasks/` (inside the epic folder if one applies). Never commit these files — `docs/tasks/` is gitignored by design. This is not a scratch draft under deletion, though: unlike the one-shot PR/comment body under `.harness/scratch/tmp/` (deleted right after publication succeeds), the contents of `docs/tasks/` are a permanent local archive — no skill or tool deletes them after a spec or ticket is published. If something needs a permanent, *committed* trail in the repository itself, that belongs in `docs/adr/` via `domain-modeling`, not here.
