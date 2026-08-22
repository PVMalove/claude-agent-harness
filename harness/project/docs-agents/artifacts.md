### Artifacts & Scratchpads Management

* **Storage Location:** NEVER use system temporary directories (e.g., `AppData/Local/Temp`, `/tmp`) for saving specifications, scratchpads, or intermediate files.
* **Project Directory:** All intermediate task-related documents MUST be saved locally inside the project repository in the `docs/tasks/` directory (create it if it doesn't exist).
* **Pre-publish only:** `docs/tasks/` holds drafts and scratchpads *before* a spec or ticket is published to the issue tracker. Once published, the durable record lives with that tracker instead — the issue itself for GitHub/GitLab (optionally mirrored under `docs/tasks/issue-<epic-id>-<epic-slug>/` per the epic-grouping convention below), or `.scratch/<feature-slug>/spec.md` and `.scratch/<feature-slug>/issues/` for the local markdown tracker (see `.harness/skills/setup-matt-pocock-skills/issue-tracker-local.md`). Never treat `docs/tasks/` as the tracker of record.
* **Naming Convention:** Every specification or scratchpad file MUST include the tracker issue ID (if it exists) and a descriptive name in its filename.
  * *Example:* `docs/tasks/issue-45-spec-search-pagination.md`
* **Epic grouping:** If the ticket belongs to a parent epic (has a `## Parent`/`Blocked by` reference to another issue, or is linked to it as a native GitHub sub-issue — see `docs/agents/triage-labels.md`), put its file inside a folder named after the epic — `docs/tasks/issue-<epic-id>-<epic-slug>/` — instead of directly under `docs/tasks/`. The epic's own spec file lives in that same folder, so the epic and all of its subtask specs stay together. Individual filenames inside the folder keep the same naming convention as before (issue ID + descriptive name) — only the location changes, not the name.
  * *Example:* epic #26 ("search-revamp") with subtasks #27 and #28 →
    ```
    docs/tasks/issue-26-search-revamp/
      issue-26-spec-search-revamp.md
      issue-27-pagination.md
      issue-28-filters.md
    ```
  * A standalone ticket with no parent epic stays flat directly under `docs/tasks/`, as before.
* **Workflow:** During `/to-spec` or when creating a scratchpad or, creating roadmaps (`/wayfinder`), explicitly write the file to this directory (inside the epic folder if one applies). Never commit these files — `docs/tasks/` is gitignored by design; the durable record is the published tracker issue, or `docs/adr/` (via `domain-modeling`) for anything that needs a permanent paper trail in-repo.
