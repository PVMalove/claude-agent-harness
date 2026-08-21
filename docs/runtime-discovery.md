# Runtime skill discovery

The harness stores one physical project snapshot at `.harness/skills` and exposes it through
relative links. This keeps every compatible runtime on the same files without duplicating skill
content inside the repository.

The upstream Matt Pocock source and the project snapshot remain byte-for-byte. Some upstream
frontmatter fields are runtime-specific, but current local discovery accepts them; the harness does
not normalize or delete them.

At the pinned revision, upstream marks 14 skills as explicit-only. For example, Codex can load
`$ask-matt` when the user names it, while `agents/openai.yaml` prevents the model from choosing it
implicitly. That is upstream behavior, not a discovery failure. Removing that policy would be a
fork, so this package does not do it.

| Runtime | Project discovery used by this harness | User discovery used for entry skills |
|---|---|---|
| Codex | `.agents/skills` | `~/.agents/skills` |
| Claude Code | `.claude/skills` | `~/.claude/skills` |
| Kimi Code | `.agents/skills` | `~/.agents/skills` |
| OpenCode | `.agents/skills` and `.claude/skills` | `~/.agents/skills` and `~/.claude/skills` |
| Hermes Agent | project `AGENTS.md` plus `.harness/skills/REGISTRY.md` fallback | `~/.hermes/skills` |

Codex, Claude Code, Kimi Code, and OpenCode therefore share the same checked-in project suite.
Hermes currently has no equivalent repository skill root. Every project snapshot therefore carries
`.harness/skills/REGISTRY.md`: `AGENTS.md` tells Hermes to search the compact metadata table and open
only the exact matching `SKILL.md`. This is portable progressive disclosure, but it is not native
skill advertisement. `skills.external_dirs` remains an optional machine-specific profile setting;
the portable harness never writes it silently.

## Project integrations

MCP servers, plugins, hooks, and runtime settings stay in the runtime's native project files. When
a project owns one, `.harness/integrations.json` records its relative path, SHA-256, target runtimes,
verification action, and secret environment-variable names. Secret values remain in native secure
storage or the device environment. `harness health` proves file integrity; a fresh runtime session
proves activation and behavior.

### OpenCode on a multi-runtime machine

OpenCode scans both Agent Skills and Claude-compatible roots. On a machine that runs both Codex
and Claude Code, the same skill is intentionally reachable through `.agents/skills` and
`.claude/skills`. Current OpenCode releases can report that as a duplicate and choose a source path
non-deterministically. Launch OpenCode with:

```bash
export OPENCODE_DISABLE_CLAUDE_CODE_SKILLS=1
```

This keeps `.agents/skills` as OpenCode's canonical compatibility root while Claude Code continues
to use `.claude/skills`. It changes only OpenCode discovery; it does not disable Claude skills in
Claude Code. Remove the workaround after OpenCode ships deterministic same-target deduplication.

## What enters the context

Skill discovery does not mean that every skill body enters every session. Compatible runtimes
advertise compact metadata such as the name and description, then load `SKILL.md` when the skill is
selected. Supporting references and scripts remain on disk until the selected workflow needs them.

This is why the recommended split is:

- user scope: only `start-project` plus an optional private/organization entry skill;
- project scope: the development suite and project-specific workflows;
- catalog scope: reusable workflows reached on demand through `start-project` or an overlay entry;
- personal overlay: private knowledge and personal workflows maintained outside this repository.

Large user-level collections still have a cost: their metadata competes for the runtime's skill
index and duplicate names can create ambiguous routing. Prefer project installation for workflows
that apply to a known repository.

## Primary documentation

- [Codex skills](https://developers.openai.com/codex/skills)
- [Claude Code skills](https://code.claude.com/docs/en/skills)
- [Kimi Code skills](https://www.kimi.com/code/docs/en/kimi-code-cli/customization/skills.html)
- [OpenCode skills](https://opencode.ai/docs/skills)
- [OpenCode duplicate-root issue](https://github.com/anomalyco/opencode/issues/29950)
- Hermes behavior is verified against its installed source: `~/.hermes/skills` plus optional
  `skills.external_dirs`; relative external paths resolve against `HERMES_HOME`, not the project.
