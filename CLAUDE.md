# Claude Code

@AGENTS.md

Claude Code-specific rules are loaded from `.claude/rules/`. The canonical repository contract is
in `AGENTS.md`; this file remains only a runtime entry point. For project configuration, use
`harness/project/project.schema.json`; for PR targeting and ticket-closure verification, use
`docs/agents/git-workflow.md` section 1 for branch and PR rules and section 2 for the workflow;
for installation and CLI commands, use `harness --help`; for skill commands, open the skill's
`SKILL.md`; for metadata-hook behavior, use the "Project-Only Metadata" rule in
`docs/agents/git-workflow.md` section 1.
