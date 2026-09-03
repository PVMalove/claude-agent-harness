# Agent Harness contributor instructions

This repository is the canonical public source of a portable, runtime-native coding-agent
harness. Skills remain ordinary `SKILL.md` packages, project discovery uses runtime-supported
paths, and build-time scripts only copy, compare, or validate files.

## Language and output

Use English for internal reasoning and planning; never reveal private chain-of-thought. Respond in
Russian unless the user explicitly requests another language. Start with the result and include
only necessary details, verification, risks, and blockers—without preambles, repetition, or long
logs.

## Project index

- `README.md` — overview and CLI usage; `CONTEXT.md` — project glossary.
- `harness/` — packager CLI, capability catalog, version, and target-project templates.
- `skills/vendor/` — pinned upstream snapshot; `skills/first-party/pvmalove/` — maintained skills.
- `global/` — seed template for the target project's global agent profile; `global-skills/` and
  `bin/` — global entry skills and installer; `scripts/` — registry, drift, verification, and
  clean-room checks.
- `docs/agents/` — operational workflow, Git, issues, artifacts, and worktrees; `docs/adr/` —
  architectural decisions; `docs/runtime-discovery.md` — runtime skill routes.
- `third_party/mattpocock-skills/` — upstream manifest, lock, license, and checksums;
  `.github/workflows/` — CI.
- `harness/CAPABILITIES.json` is the capability source of truth. `.harness/` and root
  `.agents/`/`.claude/` are generated or runtime state, not source trees.

## Search and skill routing

If a file is named, open it. Otherwise, use `rg` inside the selected area for symbols, errors,
terms, imports, configuration, and tests; open only matches. Read the relevant range and complete
logical unit, then expand to callers, dependencies, or tests only when needed. Do not inventory the
whole repository after scope is known. Exclude secrets, dependencies, build/dist output, caches,
generated/minified files, large logs, databases, temporary data, and unrelated media by default.

When a task matches a skill, open only its relevant `SKILL.md` through `.agents/skills` or
`.claude/skills`; use `.harness/skills/REGISTRY.md` when native project discovery is unavailable.

## Required boundaries

- Verify changing facts and tool state; do not invent facts, paths, commands, requirements, or
  results. State assumptions and ask when ambiguity materially changes the work.
- Preserve unrelated dirty changes and live worktrees. Keep credentials and secrets out of Git,
  logs, and reports. Ask before irreversible or hard-to-recover actions.
- Keep global instructions short and cross-project. Exclude personal knowledge, machine state,
  and project-specific integrations. Do not update profiles or projects from session-start hooks.
- Never edit `skills/vendor/` manually. Replace a complete pinned snapshot only; use exact upstream
  revisions and retain license and provenance files.
- For implementation or delivery, follow `docs/agents/git-workflow.md`: issue first, use the epic's
  recorded `integration/<service-or-team>` branch as the task base, create an isolated issue branch
  matching `.harness/project.json`, test before commits, use CLI-only Git and tracker operations,
  get explicit confirmation before opening a PR, and never auto-merge. Use
  `Closes #<ID>` only when the PR targets the repository's default branch, then verify the issue
  closed; for an integration PR, use `Related to #<ID>` and close the issue only after the developer
  confirms its merge. Use
  `docs/agents/artifacts.md` and `docs/agents/worktrees.md` for their respective workflows.
- Keep commit messages and PR titles/bodies project-only: never add automated-agent attribution,
  model names, session URLs, or `Co-Authored-By` trailers. The project hook and CI check enforce this.
  For `-F`/`--file` and PR `--body-file`/`--description-file`, pass a readable literal path: the hook
  checks the file's content, never its path, and rejects an unavailable or shell-variable path.
- Run QA commands from `.harness/project.json`. After changing `harness/CAPABILITIES.json` or
  anything under `skills/`, run `scripts/verify` before completion; `scripts/test-clean-room` is
  the faster local loop. On failure, inspect the immediate error, form a direct hypothesis, fix it,
  and report what was not checked.
- Treat `harness/project/project.schema.json` and `validate_project_json` in `harness/bin/harness`
  as one contract. After changing either or hand-editing `.harness/project.json`, run `harness health`
  for the target repository; keep the schema, template, validator, and relevant guide text aligned.

## Supervised subagents

In an Orca-managed session, coordinated subagents must use Orca Run → Task → Dispatch and the
supervised wait for `worker_done`; direct generic subagent APIs are invalid. Outside Orca, let the
harness deliver results as notifications; never poll, schedule wakeups, or launch a wait-only agent.

Codex only: when the user explicitly asks to launch or use subagents, run them with `gpt-5.6-luna`
and `max` reasoning effort. Do not apply this rule outside Codex.
