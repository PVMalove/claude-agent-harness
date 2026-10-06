---
name: to-guide
description: Turn a hitl ticket or spec into a step-by-step manual implementation guide with ready-to-paste prompts for an AI IDE (Cursor, Copilot Chat). For tickets a human will code by hand, not an agent.
disable-model-invocation: true
---

# To Guide (Manual Implementation Prep)

Transform a specification or a tracer-bullet ticket into a developer guide loaded with ready-to-use prompts for AI-assisted IDEs. This is the `hitl` counterpart to `/implement` and `/fast-implement` — they take `afk` tickets and have an agent write the code; `/to-guide` takes `hitl` tickets and hands the human a navigator's checklist instead. After this skill runs, coding, code review, `qa-gate`, and the PR are entirely the human's own — this skill does not do them and is not invoked again afterward for this ticket (the guide it produces names the exact commands in its closing section).

## Process

1. **Read the source.** Fetch the ticket the user names — an issue number, URL, or a `.scratch/<feature>/issues/NN-*.md` path (see `docs/agents/issue-tracker.md`). If it carries `status::blocked`, check its blockers the same way `/implement` does — if any are still open, stop and tell the user which ones instead of drafting a guide for a ticket that isn't actually startable yet. If it carries `status::specs` instead of `status::ready`, or is missing `hitl`, tell the user and ask whether to proceed anyway — this skill expects a fully specified, human-routed ticket (see `docs/agents/triage-labels.md`).
2. **Explore the codebase.** Identify the exact files that need creating or changing to fulfill the ticket — don't guess from the ticket text alone. Use the project's domain glossary and respect any ADRs in the area you're touching.
3. **Claim it.** Assign the ticket to the maintainer (`gh issue edit <n> --add-assignee @me` on GitHub, `glab issue update <n> -R <project-url> --assignee @me` on GitLab, or the local tracker's equivalent) before any other write — the same convention `/wayfinder` and `/fast-implement` use, so a concurrent session doesn't pick the same `hitl` ticket. On GitLab, every `glab` command takes `-R <project-url>` and every `glab api` call takes `--hostname <host>` and a `projects/<project-id>/...` path; these placeholders are defined in `docs/agents/issue-tracker.md` → GitLab → Conventions.
4. **Mark it in progress.** Move the ticket from `status::ready` to `status::in-progress`: `gh issue edit <n> --remove-label status::ready --add-label status::in-progress` on GitHub, `glab issue update <n> -R <project-url> --unlabel status::ready --label status::in-progress` on GitLab (for a local-tracker ticket, set `**Workflow:** status::in-progress`).
5. **Draft the guide** using `<guide-template>` below. In its section 5, keep only the commands of this project's tracker and fill in `<project-url>`, `<host>`, `<project-id>`, `<target-branch>`, `<ID>`, `<title>` and `<slug>` with the real values (`<title>` stays in single quotes, each apostrophe written as `'\''`) — the human copies those commands without editing them; only the PR/MR number `<n>`/`<iid>` stays a placeholder until the PR/MR exists. Write the guide in the language `.harness/project.json`'s `language` field configures (default `ru` if the file or field is absent). Unlike `/to-tickets`'s summary table, this scoping is not narrow — write the whole document, prompts included, in that language: it's a local artifact for the human maintainer to read, not a ticket published to an external tracker. Save it to `docs/tasks/` per `docs/agents/artifacts.md`'s naming convention (issue ID + descriptive slug) — if the ticket belongs to an epic, into that epic's `docs/tasks/issue-<epic-id>-<epic-slug>/` folder, alongside its own ticket file.

## Rules for the prompts

- Each prompt must be explicit, self-contained, and tell the human's AI IDE *exactly* what to do — the human is going to copy-paste it verbatim, not edit it first.
- Point the IDE at existing code to imitate: "follow the pattern in `<file>`" beats a description of the pattern.
- Keep each step small enough to compile and review on its own — the whole point is narrow, verifiable chunks, same discipline as `/to-tickets`'s vertical slices.
- Ask for the test first in every prompt — this repo requires TDD (`docs/agents/git-workflow.md`) regardless of who writes the code, and every other route has to say so out loud too (`/fast-implement` via `/tdd`, `/implement` via its batch Definition of Done), so say it explicitly here instead of assuming the human's AI IDE defaults to it.

<guide-template>

# Implementation Guide: <ticket title/ID>

## 1. Context & Constraints

The architectural rules (ADRs), patterns, and boundaries that apply here — condensed from the ticket/spec and the codebase, not restated in full.

## 2. File Map

- `[Create]` path/to/new/file
- `[Update]` path/to/existing/file

## 3. Steps & Prompts

Break the implementation into small, compilable chunks. For each:

**Step N: <goal>**

```text
<a complete, ready-to-paste prompt for the human's AI IDE — cites specific existing files to follow, states the acceptance criterion it satisfies>
```

## 4. Verification

How to check this slice works — the exact test command, or a `curl`/manual step — drawn from the ticket's acceptance criteria or the spec's Testing Decisions.

## 5. When you're done

This skill doesn't review the code, commit it, run `qa-gate`, or open the PR for you — there's no single command for any of that on the `hitl` path. Once the code is written, do these yourself, in order:

1. Run `/code-review` (or ask this session to run it) — same two-axis Standards + Spec review `/implement` would run for you on an `afk` ticket. Nothing else triggers it on this path; skipping it means the diff never gets reviewed before the PR.
2. Commit your work with a Semantic Commit Message (`feat:`, `fix:`, ...) and push, per `docs/agents/git-workflow.md` — don't let finished work sit uncommitted or unpushed.
3. Run `/qa-gate` (or ask this session to run it).
4. **GitHub/GitLab-tracked ticket:** open the PR/MR into `<target-branch>` per `docs/agents/git-workflow.md`. Write its body to `.harness/.sandboxes/pr_body/pr-body-<ID>-<slug>.md` from the body template directly, or configure and run `pr-composer` manually in the coding application.
   - **Footer:** `Closes #<ID>` only when `<target-branch>` is the default branch (GitHub: `gh repo view --json defaultBranchRef --jq .defaultBranchRef.name`; GitLab: the `default_branch` field of `glab api --hostname <host> projects/<project-id>`), otherwise `Related to #<ID>`. GitLab closes an issue by `Closes #<ID>` only when the MR lands in the default branch.
   - **Open it.** GitHub: `gh pr create --base <target-branch> --title '<title>' --body-file .harness/.sandboxes/pr_body/pr-body-<ID>-<slug>.md`. GitLab: `glab mr create -R <project-url> --target-branch <target-branch> --title '<title>' --description-file .harness/.sandboxes/pr_body/pr-body-<ID>-<slug>.md --yes`; the MR is `!<iid>`, the last segment of the URL it prints.
   - **Task report:** if this ticket carries `task-report::required`, post the completion report when you open the PR/MR. Write it to `.harness/.sandboxes/pr_body/issue-comment-<ID>-<slug>.md`, then GitHub: `gh issue comment <ID> --body-file .harness/.sandboxes/pr_body/issue-comment-<ID>-<slug>.md`; GitLab: `glab api --hostname <host> projects/<project-id>/issues/<ID>/notes -F body=@.harness/.sandboxes/pr_body/issue-comment-<ID>-<slug>.md`.
   - **After you merge it:** check the merge (GitHub: `gh pr view <n> --json state,baseRefName`, `state` is `MERGED`; GitLab: `glab mr view <iid> -R <project-url> -F json`, `state` is `merged`). For `Related to #<ID>`, close the issue — GitHub: `gh issue close <ID> --reason completed`; GitLab: `glab issue close <ID> -R <project-url>`. For `Closes #<ID>`, verify that it closed — GitHub: `gh issue view <ID> --json state` (`CLOSED`); GitLab: `glab issue view <ID> -R <project-url> -F json` (`state` is `closed`; GitLab closes it asynchronously, so read it once more if it still reads `opened`) — and close it with the same command if it did not.

   **Local-markdown-tracked ticket:** there's no PR/merge step — once the above all pass, set the ticket file's `**Workflow:**` line to `done` yourself; if it carries `**Task report:** required`, fold the completion summary into that same update.

</guide-template>
<!--
# Руководство по реализации: <название/ID тикета>

## 1. Контекст и ограничения

Архитектурные правила (ADR), паттерны и границы, которые применимы здесь — кратко изложенные из тикета/спецификации и кодовой базы, а не пересказанные полностью.

## 2. Карта файлов

- `[Создать]` путь/к/новому/файлу
- `[Обновить]` путь/к/существующему/файлу

## 3. Шаги и промпты

Разбейте реализацию на небольшие, компилируемые части. Для каждой:

**Шаг N: <цель>**

```text
<полный, готовый к вставке промпт для ИИ-IDE человека — ссылается на конкретные существующие файлы для следования, формулирует критерий приемки, которому он удовлетворяет>
```

## 4. Проверка

Как проверить, что эта часть работает — точная команда тестирования или `curl`/ручной шаг — взятые из критериев приемки тикета или Тестовых Решений (Testing Decisions) спецификации.

## 5. Когда вы закончите

Этот навык не рецензирует код, не коммитит его, не запускает `qa-gate` и не открывает PR за вас — нет ни одной команды для всего этого на пути `hitl`. Как только код будет написан, выполните их самостоятельно, по порядку:

1. Запустите `/code-review` (или попросите эту сессию запустить его) — то же ревью по двум осям Стандарты + Спецификация, которое `/implement` выполнил бы за вас для `afk`-тикета. Ничто другое не запускает его на этом пути; пропуск этого шага означает, что diff (изменения) никогда не проходит ревью перед PR.
2. Закоммитьте свою работу с Семантическим Сообщением Коммита (`feat:`, `fix:`, ...) и запушьте, согласно `docs/agents/git-workflow.md` — не позволяйте законченной работе оставаться незакоммиченной или незапушенной.
3. Запустите `/qa-gate` (или попросите эту сессию запустить его).
4. **Тикет, отслеживаемый в GitHub/GitLab:** откройте PR/MR в `<target-branch>` согласно `docs/agents/git-workflow.md`. Запишите его тело в `.harness/.sandboxes/pr_body/pr-body-<ID>-<slug>.md` по шаблону тела напрямую или настройте и запустите `pr-composer` вручную в приложении для программирования.
   - **Footer:** `Closes #<ID>` только когда `<target-branch>` — ветка по умолчанию (GitHub: `gh repo view --json defaultBranchRef --jq .defaultBranchRef.name`; GitLab: поле `default_branch` из `glab api --hostname <host> projects/<project-id>`), в противном случае `Related to #<ID>`. GitLab закрывает issue по `Closes #<ID>` только когда MR попадает в ветку по умолчанию.
   - **Откройте его.** GitHub: `gh pr create --base <target-branch> --title '<title>' --body-file .harness/.sandboxes/pr_body/pr-body-<ID>-<slug>.md`. GitLab: `glab mr create -R <project-url> --target-branch <target-branch> --title '<title>' --description-file .harness/.sandboxes/pr_body/pr-body-<ID>-<slug>.md --yes`; MR — это `!<iid>`, последний сегмент URL, который печатает команда.
   - **Отчёт о завершении:** если этот тикет имеет метку `task-report::required`, опубликуйте отчёт о завершении при открытии PR/MR. Запишите его в `.harness/.sandboxes/pr_body/issue-comment-<ID>-<slug>.md`, затем GitHub: `gh issue comment <ID> --body-file .harness/.sandboxes/pr_body/issue-comment-<ID>-<slug>.md`; GitLab: `glab api --hostname <host> projects/<project-id>/issues/<ID>/notes -F body=@.harness/.sandboxes/pr_body/issue-comment-<ID>-<slug>.md`.
   - **После того как вы выполните слияние (merge):** проверьте его (GitHub: `gh pr view <n> --json state,baseRefName`, `state` равен `MERGED`; GitLab: `glab mr view <iid> -R <project-url> -F json`, `state` равен `merged`). Для `Related to #<ID>` закройте issue — GitHub: `gh issue close <ID> --reason completed`; GitLab: `glab issue close <ID> -R <project-url>`. Для `Closes #<ID>` убедитесь, что issue закрыт, — GitHub: `gh issue view <ID> --json state` (`CLOSED`); GitLab: `glab issue view <ID> -R <project-url> -F json` (`state` равен `closed`; GitLab закрывает его асинхронно, поэтому прочитайте его ещё раз, если он всё ещё `opened`), — и закройте его той же командой, если этого не произошло.

   **Тикет, отслеживаемый локально в markdown:** здесь нет шага PR/слияния — как только все вышеперечисленное будет пройдено, самостоятельно установите строку `**Workflow:**` в файле тикета на `done`; если он имеет метку `**Task report:** required`, включите сводку о завершении в то же самое обновление.

</guide-template>
-->

