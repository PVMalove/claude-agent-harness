---
name: to-tickets
description: Break a plan, spec, or conversation into a set of tracer-bullet tickets with blocking edges, and publish them to the configured tracker.
disable-model-invocation: true
---

# To Tickets

**Objective:** Break a plan, spec, or conversation into a set of **tickets** — tracer-bullet vertical slices, each declaring its blocking edges.

The issue tracker and triage label vocabulary should have been provided to you. If not, tell the user to run `/setup-matt-pocock-skills`.

## Execution in Two Phases

You must execute this skill in two distinct phases. Do NOT publish anything to the issue tracker or create local files until the user explicitly approves the ticket breakdown.

### Phase 1: Drafting & Review
1. **Gather Context:** Work from the conversation context. If passed a reference (spec path, issue number/URL), read its full body and comments.
2. **Explore the Codebase (Optional):** Use the project's domain glossary. Look for prefactoring opportunities ("Make the change easy, then make the easy change").
3. **Draft Vertical Slices:** Break the work into **tracer bullet** tickets.
    - *Vertical Slices:* Cut a narrow but COMPLETE path through every layer (schema, API, UI, tests). Must be demoable/verifiable on its own and fit in a single context window.
    - *Wide Refactors (Exception):* If a change has a massive blast radius (e.g., renaming a shared column), use **expand-contract** instead of vertical slicing. Sequence as: Expand → Migrate (in batches) → Contract.
    - *Blocking Edges:* Give each ticket its blocking edges (which other tickets must complete first).
    - *Human Time Estimate:* Estimate the rough time required for a human developer to complete this slice (e.g., "2 hours", "1 day") — for every ticket, `afk` included, not just `hitl` ones. This is a decomposition-quality signal, not a commitment: an estimate in weeks means the slice isn't tracer-bullet-sized — split it further before presenting the breakdown.
4. **STOP AND ASK (Quiz the User):** Present the proposed breakdown as a numbered list. For each ticket, show:
    - **Title:** Short descriptive name
    - **Blocked by:** Which tickets gate it
    - **Est. Time (Human):** The estimated time for a human to complete it
    - **What it delivers:** The end-to-end behavior
    - *Ask the user:* Does the granularity feel right? Are blocking edges correct? Should anything be merged/split? Is the time estimate realistic?
    - **DO NOT PROCEED TO PHASE 2 UNTIL APPROVED.**

### Phase 2: Publishing & Summarizing (After Approval)
1. **Publish to the Tracker:** The method depends on the configured tracker:
    - **Local files:** Write one file per ticket under `.scratch/<feature-slug>/issues/<NN>-<slug>.md` (01, 02...). Use `<local-ticket-template>`. Set `**Workflow:**` to `workflow::blocked` if it has blockers, otherwise `workflow::ready`. Set `**Execution:**` to `hitl` or `afk` per your best judgment of the ticket (see `docs/agents/triage-labels.md`). Add `**Task report:** required` unless told to skip it (omit the line entirely if not required). `/implement` finds the next ticket by reading each file's `**Workflow:**` field — a purely linear chain resolves top to bottom.
    - **GitHub / Real Tracker:**
        - Publish one issue per ticket in dependency order using `gh issue create --body-file <path>`. **CRITICAL:** Do NOT use inline `--body` heredoc, as it breaks bash quoting.
        - Apply labels (see `docs/agents/triage-labels.md` for the full taxonomy): `bug`/`enhancement`, `workflow::ready` (or `workflow::blocked` if gated by another ticket in this batch), `hitl`/`afk`, and `task-report::required` unless told to skip it.
        - *Grouping:* Link every ticket to the parent epic as a **native sub-issue**. Do NOT use `epic::<slug>` labels.
        - *Local Mirror:* The epic spec already lives in its own folder under `docs/tasks/` (per `docs/agents/artifacts.md`) — rename that folder to `issue-<epic-id>-<epic-slug>/` first if it was still slug-only. Save each published ticket's issue body into that same folder, as `issue-<ID>-<slug>.md`.
        - *Frontier:* Don't trace `Blocked by` by hand to find what's takeable — query it, the same fields and mechanism as `/wayfinder`'s frontier query (`docs/agents/issue-tracker.md#wayfinding-operations`), scoped to the epic's sub-issues instead of the map's children. `/implement` runs this same query itself when handed the epic instead of a specific ticket.
        - Do NOT close or rewrite the parent epic issue, except to append a short list of the subtask numbers you created.
2. **Summarize the Batch:**
    - Read `language` from `.harness/project.json` (default `ru` if the file or field is absent) — this decides only the "What to build" column below, not the ticket titles/bodies you publish, which stay in whatever language you drafted them in.
    - If this runtime supports dispatching a sub-agent pinned to a specific model, send a single call with `model: haiku` (cheapest available, one call for the whole batch) — pass it every ticket's title and body plus the target language, asking for one concise sentence per ticket written in that language. Otherwise, write the descriptions yourself, on your own model, in the same language.
    - Output a final table compiling all data. The labels column must list all applied taxonomy tags (e.g., `enhancement`, `workflow::ready`, `afk`, `task-report::required`).

   | Ticket | What to build | Est. Time (Human) | Labels |
      |---|---|---|---|
   | <number/link/path> | <one-line summary> | <time> | <comma-separated labels> |

---

<local-ticket-template>
# <NN> — <Ticket title>

**What to build:** The end-to-end behavior this ticket makes work from the user's perspective.
**Blocked by:** The numbers/titles of the tickets that gate this one, or "None — can start immediately".

**Category:** bug / enhancement
**Workflow:** workflow::ready (or workflow::blocked)
**Execution:** hitl / afk
**Task report:** required (omit the line entirely if not required)

- [ ] Acceptance criterion 1
- [ ] Acceptance criterion 2
  </local-ticket-template>

<issue-template>
## Parent
A reference to the parent issue on the tracker (if applicable).

## What to build
The end-to-end behavior this ticket makes work from the user's perspective.

## Acceptance criteria
- [ ] Criterion 1
- [ ] Criterion 2

## Blocked by
- A reference to each blocking ticket, or "None — can start immediately".
  </issue-template>

*Note for both templates: Avoid specific file paths or code snippets unless it is a vital prototype snippet (trim to decision-rich parts only).*