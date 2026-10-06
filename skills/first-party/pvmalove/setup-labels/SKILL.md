---
name: setup-labels
description: Create or update this repo's GitHub or GitLab labels (status::*, hitl/afk, task-report::required, resolution::wontfix, wayfinder:*) to match docs/agents/triage-labels.md. Run once per repo before first use of triage, to-spec, to-tickets, implement, to-guide, or wayfinder.
disable-model-invocation: true
---

# Setup Labels

`gh issue edit --add-label`/`gh issue create --label` fail on a label that doesn't exist yet — `gh` never auto-creates one. GitLab does the opposite: a `--label` that names a missing label silently creates it with GitLab's default color, so labelling a ticket before this skill runs leaves a wrongly colored label behind. This skill creates every label this repo's triage taxonomy needs, with its canonical color, so run it before the first `/to-spec` or `/to-tickets`.

## Process

1. **Read the tables.** Pull every `Label` / `Color` row from `docs/agents/triage-labels.md` — the taxonomy tables (category, execution mode, workflow state, context labels) and the Wayfinder addendum. Name and hex color only, skip the `Applied by` column; skip `Meaning` too on GitHub, while on GitLab keep its first sentence as the label description. If the file doesn't exist, tell the user to run `harness update`, which seeds it from the harness templates, and stop.
2. **Show the plan.** List every label about to be created or updated, with its color. Confirm with the maintainer before touching the tracker — this mutates shared repo state, same discipline as any other tracker-mutating step in this repo (see `docs/agents/issue-tracker.md`).
3. **Apply.**
    - **GitHub:** For each label, run `gh label create "<name>" --color "<hex>" --force`. `--force` makes this idempotent — it updates the color of a label that already exists instead of erroring, and touches nothing else about it (issues already carrying it are unaffected).
    - **GitLab:** the CLI has no `--force` for label creation, so compare against the existing labels first. `<host>`, `<project-url>` and `<project-id>` are defined in `docs/agents/issue-tracker.md` → GitLab → Conventions.
        - Read the existing labels once: `glab api --hostname <host> --paginate projects/<project-id>/labels` — the paginated API, because the CLI's own label listing returns a single page. Each entry carries `id`, `name` and `color`.
        - Missing label: `glab label create -R <project-url> -n '<name>' -c '<hex>' -d '<description>'`. `<description>` is the first sentence of the `Meaning` column as plain text, with its backticks and quotes dropped: inside double quotes the shell would run a backticked word as a command, and a single-quoted value can't contain a quote.
        - Existing label with another color (compare hex case-insensitively): `glab label edit -R <project-url> --label-id <id> -c '<hex>'`, with `<id>` from the listing. Only the color changes; issues already carrying the label are unaffected.
        - Existing label with the same color: no-op.
4. **Report.** One line per label: created, updated (color changed), or already correct (no-op). Don't re-print the full plan from step 2.
