# Git workflow: feature branch + PR

### 1. Fundamental Constraints & Tooling
* **Zero Direct Commits:** Any direct commits to the main branch (`master`/`main`) or the current working branch are strictly prohibited unless it is an isolated feature branch.
* **CLI Only:** Rely exclusively on `git` and your tracker's CLI — GitHub CLI (`gh`) or GitLab CLI (`glab`) — for repository and task operations.
* **Body via File, Not Inline:** Any multiline body passed to `gh`/`glab` (`issue create`, `pr create`, `pr comment`, or equivalents) MUST go through `--body-file <path>`, never inline `--body "..."` or a shell heredoc — nested quotes, backticks, and PowerShell's escaping rules all break it unpredictably. For an issue or spec, the path is the already-written draft file (see [issue-tracker.md](./issue-tracker.md)). For a PR body or a comment, write it to a temp file first, then delete that temp file once the command succeeds.
* **Issue First:** No development begins without a registered ticket. All implementation tasks MUST be created beforehand using `gh issue create` (`glab issue create` on GitLab). For the local markdown tracker, creating the ticket file under `.scratch/<feature-slug>/issues/` satisfies this instead — see [issue-tracker.md](./issue-tracker.md); that tracker also skips steps 6–7 below entirely (see `implement/SKILL.md` Phase 3 — no PR/merge step).
* **Zero Auto-Merge:** The agent must never merge a pull request itself (`gh pr merge`/`glab mr merge` or equivalent). Merging into `master`/`main` is exclusively a manual action performed by the developer, after they confirm in the Human QA & Merge step below. This is unconditional regardless of the ticket's `hitl`/`afk` execution mode (see `docs/agents/triage-labels.md`) — `afk` means an agent can implement the work unattended, never that it may ship unattended.
* **PR Confirmation Required:** The agent must not run `gh pr create`/`glab mr create` without first getting the developer's explicit go-ahead that the branch is ready to become a PR. This is a separate, earlier checkpoint than Human QA & Merge below (which covers review *after* the PR already exists) — finishing implementation, tests, and code-review does NOT by itself imply consent to open the PR. Like Zero Auto-Merge above, this gate does not relax for `afk`-labeled tickets.

### 2. Workflow Sequence

1. **Initialization (Branching):**
   An isolated branch is created for each task, from an up-to-date `base_branch` — never from whatever branch happens to already be checked out.
   * **Format:** must match `branch_pattern` in `.harness/project.json` (default: `feature/issue-<ID>-<short-slug>`, where `<ID>` is the tracker issue number and `<short-slug>` is a short task description — transliterated per whatever convention this project's `.harness/project.json` documents, words separated by hyphens or underscores).
   * **Command:** `git checkout <base_branch> && git pull && git checkout -b feature/issue-<ID>-<slug>`, where `<base_branch>` is `base_branch` in `.harness/project.json` (default `main`).
2. **Post-branch Push:**
   * Immediately after creating the branch, push it to GitHub so it exists remotely: `git push -u origin feature/issue-<ID>-<slug>`.
3. **Implementation & Quality Assurance (TDD):**
   * Code MUST be written in strict accordance with the **TDD** (Test-Driven Development) methodology.
   * Local testing is mandatory before committing any changes.
4. **Committing Changes:**
   * Commits are made only to the current feature branch.
   * Commit messages **MUST** follow the **Semantic Commit Messages** standard (e.g., `feat: ...`, `fix: ...`, `refactor: ...`).
5. **Continuous Push:**
   * Push commits to GitHub both while implementing the task and after addressing code-review feedback: `git push origin feature/issue-<ID>-<slug>`. Never leave finished commits sitting only in the local repo.
6. **Integration (Pull Request):** *(local markdown tracker: skip this step and step 7 — see "Issue First" above.)*
   * **Confirm before opening:** before creating the PR, explicitly ask the developer whether the branch is ready to be opened as a pull request. Do not run `gh pr create` just because implementation, tests, and code-review are done — wait for an explicit go-ahead. Silence, or the mere fact that the task is otherwise complete, does not count as consent.
   * Once the developer confirms, run the `qa-gate` skill (see [issue-tracker.md](./issue-tracker.md)'s "When a skill says…" conventions for how tickets are referenced) and only proceed once it passes.
   * If the PR body template in [§3](#3-pr-body-template) has more structure than a short summary, delegate the body to the `pr-composer` subagent instead of improvising it inline — give it a temp file path to write to, then pass that path to `--body-file` below and delete the temp file once the PR is created.
   * PR creation is performed via CLI: `gh pr create --body-file <path>` (`glab mr create` on GitLab) — see §1 ("Body via File, Not Inline"); never inline `--body`.
   * **Mandatory Requirement:** The pull request body MUST contain the phrase `Closes #<ID>` to automatically link and close the original ticket upon successful merge.
   * **Mandatory Requirement:** The pull request body MUST follow the template in [§3 PR Body Template](#3-pr-body-template) below.
7. **Human QA & Merge:**
   * Once the PR is open, explicitly ask the developer whether they want to review the change themselves before it's considered ready — do not assume silence means approval.
   * **If the developer confirms:** tell them the PR is ready and stop there. Do not merge it — merging is always a manual action the developer performs themselves.
   * **If the developer requests changes or clarifications:** address them with new commits on the same branch (repeat steps 3–5: implement, commit, push), then ask again. Repeat until the developer confirms.

### 3. PR Body Template

Read `language` from `.harness/project.json` (default `ru`) to pick which template below applies.

**`language: ru`** — every `gh pr create --body` MUST start with the following HTML comment verbatim (invisible when GitHub renders the PR, but a checklist for the author and a navigator for the reviewer), followed by the seven sections it describes, filled in for the actual change:

```html
<!--
Этот закомментированный блок в теле Pull Request служит чек-листом для автора и навигатором для ревьюера. Каждый пункт должен давать чёткое понимание контекста изменений.

1. Итог
Краткая выжимка того, какая конечная цель достигнута этим пулл-реквестом. Читая только этот пункт, ревьюер должен понять суть PR без погружения в код.

2. Затронутые части проекта
Названия модулей, слоёв архитектуры, сервисов или баз данных, которые были изменены.

3. Бизнес-логика
Какие бизнес-правила добавлены, изменены или удалены. Как теперь должна вести себя система с точки зрения бизнеса.

4. Что изменено
Техническое описание реализации — использованные паттерны, добавленные классы/интерфейсы, изменения в сигнатурах.

5. Проверка
Как именно тестировался функционал (unit, интеграционные, e2e, ручная проверка) — используй результат `qa-gate`, если он запускался в этой сессии.

6. Не проверено и риски
Краевые случаи, не покрытые тестами, потенциальные проблемы производительности, оставленные "костыли".

7. Интеграция
Что нужно сделать при выкладке в другие окружения — миграции БД, новые переменные окружения, зависимости от других PR.
-->
```

```markdown
## Итог

## Затронутые части проекта

## Бизнес-логика

## Что изменено

## Проверка

## Не проверено и риски

## Интеграция

Closes #<ID>
```

**`language: en`** — the same checklist and seven sections, in English:

```html
<!--
This commented block in the Pull Request body serves as a checklist for the author and a navigator for the reviewer. Each item should give a clear picture of the change's context.

1. Summary
A short summary of the end goal this pull request achieves. Reading only this point, the reviewer should understand the gist of the PR without diving into the code.

2. Affected parts of the project
The modules, architectural layers, services, or databases that were changed.

3. Business logic
Which business rules were added, changed, or removed. How the system should now behave from a business perspective.

4. What changed
A technical description of the implementation — patterns used, classes/interfaces added, signature changes.

5. Verification
How the functionality was tested (unit, integration, e2e, manual) — use the `qa-gate` result if one ran this session.

6. Not verified & risks
Edge cases not covered by tests, potential performance issues, remaining workarounds.

7. Integration
What needs to happen when deploying to other environments — DB migrations, new env vars, dependencies on other PRs.
-->
```

```markdown
## Summary

## Affected parts of the project

## Business logic

## What changed

## Verification

## Not verified & risks

## Integration

Closes #<ID>
```
