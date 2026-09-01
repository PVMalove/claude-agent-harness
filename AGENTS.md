# Agent Harness contributor instructions

This repository is the canonical public source for the portable harness. Keep runtime behavior
native: skills remain ordinary `SKILL.md` packages, project discovery uses runtime-supported paths,
and build-time scripts only copy, compare, or validate files.

- Never modify files under `skills/vendor/`; replace a complete pinned snapshot instead.
- Keep global instructions short and valid across unrelated projects.
- Keep credentials, personal knowledge, machine state, and project-specific integrations out.
- Preserve unrelated changes. After touching `harness/CAPABILITIES.json` or anything under
  `skills/`, verify with `scripts/verify` (or `scripts/test-clean-room` alone for a faster local
  loop) against the disposable repos it creates, before considering the change done.
- Use exact upstream revisions and retain license and provenance files.
- Do not perform automatic profile or project updates from session-start hooks.
- When a first-party skill or agent coordinates sub-agents in an Orca-managed session, use Orca
  orchestration: create/bind the Run, create Tasks and Dispatches, wait for `worker_done` via
  Orca's supervised wait, then continue the coordinator workflow with the delivered results.
  Direct generic sub-agent APIs bypass Orca's lifecycle and are not valid for supervised work.
  Outside Orca, let the harness deliver each result as a notification; never poll, schedule a
  wakeup (even as a "fallback heartbeat" or for /loop mode), or launch another agent whose only
  job is to wait on the ones already running.
