# Triage Labels

This repo does **not** use the upstream `mattpocock/skills` canonical five-role vocabulary (`needs-triage` / `needs-info` / `ready-for-agent` / `ready-for-human` / `wontfix`) as literal labels. Instead it uses a namespaced, multi-axis taxonomy. `triage/SKILL.md` has been customized to speak natively in these terms — this file is the reference, not a translation table.

If a skill you're reading still talks in the old canonical vocabulary (some upstream skill prose does), translate with this cheat-sheet:

| Canonical concept              | This repo                          |
| ------------------------------- | ----------------------------------- |
| `needs-triage`                  | unlabeled (no dedicated label)      |
| `needs-info`                    | `workflow::blocked` + triage notes comment |
| `ready-for-agent`               | `workflow::ready` + `afk`           |
| `ready-for-human`                | `workflow::ready` + `hitl`          |
| `wontfix`                        | `out-of-scope`                      |
| category (`bug` / `enhancement`) | no label — `type::*` covers the work-kind axis instead; bug-vs-feature is a prose judgment call for the `.out-of-scope/` write, not a GitHub label |

## The taxonomy

Every triaged issue or PR carries exactly one label from each of the first three axes.

### 1. Execution mode — who does the work

| Label  | Color                 | Meaning                                                |
| ------ | ---------------------- | ------------------------------------------------------- |
| `hitl` | yellow `#fbca04`       | Human-in-the-loop — needs review, testing, or approval from you |
| `afk`  | light blue `#54c1e8`   | Away-from-keyboard — an agent can complete it alone      |

### 2. Type (`type::*`) — what kind of session this is

| Label                  | Meaning                                             |
| ----------------------- | ---------------------------------------------------- |
| `type::spec`            | Specification drafting                              |
| `type::research`        | Codebase or infrastructure investigation             |
| `type::prototype`       | Throwaway code to test a hypothesis                  |
| `type::grilling`        | Requirements-gathering / requirements-boundary session |
| `type::implementation`  | Writing production code                              |
| `type::map`             | A Wayfinder epic-mapping ticket                      |

### 3. Workflow state (`workflow::*`) — where it sits in the pipeline

| Label                    | Color            | Meaning                                                        |
| ------------------------- | ----------------- | ----------------------------------------------------------------- |
| `workflow::ready`         | green `#0e8a16`   | Fully specified, ready to be picked up                          |
| `workflow::in-progress`   | blue `#1d76db`    | Currently being worked on in an active session                 |
| `workflow::blocked`       | red `#b60205`     | Blocked by a dependency, or waiting on more info from you       |

### Context labels — applied when relevant

| Label                     | Color             | Meaning                                                                 |
| -------------------------- | ------------------ | -------------------------------------------------------------------------- |
| `epic::<name>`              | (per-epic, created ad hoc) | This ticket belongs to a larger effort, e.g. `epic::search-revamp`. **Not** the same namespace as Wayfinder's `wayfinder:<type>` — don't conflate them. |
| `task-report::required`     | gray `#5319e7`     | Agent must post a completion report on close. Applied by default to every triaged ticket unless you say to skip it. |
| `out-of-scope`              | gray `#c2c2c2`     | This project's `wontfix` — the request was explicitly rejected. Applied at close time; see `.out-of-scope/` handling in `triage/OUT-OF-SCOPE.md`. |

`wayfinder:map` and `wayfinder:<type>` (`research`/`prototype`/`grilling`/`task`) belong to the `wayfinder` skill's own namespace — see [issue-tracker.md](./issue-tracker.md#wayfinding-operations). They coexist with the taxonomy above (a Wayfinder ticket also carries its `hitl`/`afk` + `workflow::*` labels) but are managed by `/wayfinder`, not `/triage`.

## State machine

An unlabeled issue is implicitly "needs triage" — there's no dedicated label for that state.

1. Triage analyzes the issue: determine `type::*` and `hitl`/`afk`.
2. Place it in `workflow::ready` (nothing blocking it) or `workflow::blocked` (a dependency, or missing info from you — either way, post triage notes).
3. `workflow::blocked` → `workflow::ready` once the blocker clears or you reply.
4. `workflow::ready` → `workflow::in-progress` when a session (agent or you) picks it up.
5. Rejected at any point → apply `out-of-scope`, drop the `workflow::*` label, close.

Apply `task-report::required` alongside any `workflow::ready` outcome by default. Apply `epic::<name>` if the ticket belongs to a larger effort.

## Creating new `epic::*` labels

`epic::*` labels are created ad hoc, one per epic, the first time a ticket needs one — not pre-created. Use a short kebab-case name after the `::` (e.g. `epic::search-revamp`): `gh label create "epic::search-revamp" --color <pick one, any unused hue> --description "Search revamp epic"`.

## Adapting this taxonomy per project

This is the taxonomy `pvmalove-suite` ships by default — it's a design choice (namespaced multi-axis labels), not a hardcoded requirement. Edit this file directly to rename axes or add project-specific context labels; `triage/SKILL.md` reads its label vocabulary from here, not from its own body.
