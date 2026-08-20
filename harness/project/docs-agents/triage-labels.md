# Triage Labels

The skills speak in terms of five canonical triage roles. This file maps those roles to the actual label strings used in this repo's issue tracker.

| Label in mattpocock/skills | Label in our tracker | Meaning                                  |
| --------------------------- | ---------------------- | ------------------------------------------ |
| `needs-triage`               | `needs-triage`          | Maintainer needs to evaluate this issue  |
| `needs-info`                  | `needs-info`            | Waiting on reporter for more information |
| `ready-for-agent`             | `ready-for-agent`       | Fully specified, ready for an AFK agent  |
| `ready-for-human`              | `ready-for-human`       | Requires human implementation            |
| `wontfix`                      | `wontfix`               | Will not be actioned                     |

When a skill mentions a role (e.g. "apply the AFK-ready triage label"), use the corresponding label string from this table. Edit the right-hand column to match whatever vocabulary you actually use.

## Context labels — not managed by `triage`

These two labels sit outside the five canonical roles above. `triage/SKILL.md` (vendor, unmodified) doesn't know about either of them — nothing about the canonical roles prevents them coexisting. They're applied by `to-spec`/`to-tickets` and, for the second one, cleared by `implement`.

| Label               | Color                     | Applied by                  | Meaning                                                                                                                                                                                    |
| --------------------- | --------------------------- | ------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `epic::<slug>`        | per-epic, created ad hoc     | `/to-spec`, `/to-tickets`      | This issue is a feature epic (`/to-spec`), or a subtask decomposed from one (`/to-tickets`) — the same slug on every subtask so the whole group stays queryable.                          |
| `blocked-by-ticket`   | red `#b60205`               | `/to-tickets`, cleared by `/implement` | Applied instead of `ready-for-agent` when `/to-tickets` creates a ticket whose blockers (from the same decomposition) aren't closed yet. `/implement` checks the blockers before starting a ticket that carries this label — clears it and applies `ready-for-agent` once they're all closed, otherwise stops and names the open ones. |

### Creating new `epic::*` labels

`epic::*` labels are created ad hoc, one per epic, the first time a ticket needs one — not pre-created. Use a short kebab-case name after the `::` (e.g. `epic::search-revamp`): `gh label create "epic::search-revamp" --color <pick one, any unused hue> --description "Search revamp epic"`.

### Local markdown tracker

A local-markdown-tracked ticket (`.scratch/<feature>/issues/NN-*.md`) has no GitHub/GitLab labels — the same values (`ready-for-agent`, `blocked-by-ticket`, plus a terminal `done`) go in that file's `**Status:**` line instead. `/implement` sets `**Status:** done` when it finishes a ticket, mirroring the `Closes #<ID>` auto-close a GitHub/GitLab ticket gets on merge (see `docs/agents/git-workflow.md`).
