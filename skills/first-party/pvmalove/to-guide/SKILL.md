---
name: to-guide
description: Turn a hitl ticket or spec into a step-by-step manual implementation guide with ready-to-paste prompts for an AI IDE (Cursor, Copilot Chat). For tickets a human will code by hand, not an agent.
disable-model-invocation: true
---

# To Guide (Manual Implementation Prep)

Transform a specification or a tracer-bullet ticket into a developer guide loaded with ready-to-use prompts for AI-assisted IDEs. This is the `hitl` counterpart to `/implement` — `/implement` takes `afk` tickets and writes the code itself; `/to-guide` takes `hitl` tickets and hands the human a navigator's checklist instead. After this skill runs, coding, code review, `qa-gate`, and the PR are entirely the human's own — this skill does not do them and is not invoked again afterward for this ticket (the guide it produces names the exact commands in its closing section).

## Process

1. **Read the source.** Fetch the ticket the user names — an issue number, URL, or a `.scratch/<feature>/issues/NN-*.md` path (see `docs/agents/issue-tracker.md`). If it carries `workflow::blocked`, check its blockers the same way `/implement` does — if any are still open, stop and tell the user which ones instead of drafting a guide for a ticket that isn't actually startable yet. If it carries `workflow::specs` instead of `workflow::ready`, or is missing `hitl`, tell the user and ask whether to proceed anyway — this skill expects a fully specified, human-routed ticket (see `docs/agents/triage-labels.md`).
2. **Explore the codebase.** Identify the exact files that need creating or changing to fulfill the ticket — don't guess from the ticket text alone. Use the project's domain glossary and respect any ADRs in the area you're touching.
3. **Claim it.** Assign the ticket to the maintainer (`gh issue edit <n> --add-assignee @me`, or the local tracker's equivalent) before any other write — the same convention `/wayfinder` and `/implement` use, so a concurrent session doesn't pick the same `hitl` ticket.
4. **Mark it in progress.** Move the ticket from `workflow::ready` to `workflow::in-progress` (for a local-tracker ticket, set `**Workflow:** workflow::in-progress`).
5. **Draft the guide** using `<guide-template>` below, in the language `.harness/project.json`'s `language` field configures (default `ru` if the file or field is absent). Unlike `/to-tickets`'s summary table, this scoping is not narrow — write the whole document, prompts included, in that language: it's a local artifact for the human maintainer to read, not a ticket published to an external tracker. Save it to `docs/tasks/` per `docs/agents/artifacts.md`'s naming convention (issue ID + descriptive slug) — if the ticket belongs to an epic, into that epic's `docs/tasks/issue-<epic-id>-<epic-slug>/` folder, alongside its own ticket file.

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
4. **GitHub/GitLab-tracked ticket:** open the PR/MR per `docs/agents/git-workflow.md` (`gh pr create` on GitHub, `glab mr create` on GitLab; choose `Closes #<ID>` only for the default-branch target and verify closure after merge, otherwise use `Related to #<ID>` and explicitly close the issue after the confirmed merge). Use the body template directly, or configure and run `pr-composer` manually in the coding application. If this ticket carries `task-report::required`, post the completion report when you open the PR.
   **Local-markdown-tracked ticket:** there's no PR/merge step — once the above all pass, set the ticket file's `**Workflow:**` line to `done` yourself; if it carries `**Task report:** required`, fold the completion summary into that same update.

</guide-template>


<!--
Краткое описание (Summary): Этот навык преобразует задачи для ручной реализации (hitl) в подробное руководство для разработчика с готовыми промптами для ИИ-IDE (таких как Cursor или Copilot). Он не пишет код сам, а подготавливает пошаговый план, определяет необходимые файлы, указывает на архитектурные правила и требует соблюдения TDD, оставляя написание кода, ревью и создание PR на человека.

Перевод:
---
name: to-guide
description: Превращает hitl-тикет или спецификацию в пошаговое руководство по ручной реализации с готовыми промптами для ИИ-IDE (Cursor, Copilot Chat). Для тикетов, которые человек будет программировать вручную, а не агент.
disable-model-invocation: true
---

# В руководство (Подготовка к ручной реализации)

Преобразует спецификацию или исследовательский (tracer-bullet) тикет в руководство для разработчика, наполненное готовыми к использованию промптами для IDE с поддержкой ИИ. Это `hitl`-аналог `/implement` — `/implement` берет `afk`-тикеты и пишет код самостоятельно; `/to-guide` берет `hitl`-тикеты и передает человеку контрольный список навигатора. После работы этого навыка написание кода, код-ревью, `qa-gate` и создание PR полностью ложатся на плечи человека — этот навык не выполняет их и больше не вызывается для этого тикета (руководство, которое он создает, указывает точные команды в своем заключительном разделе).

## Процесс

1. **Прочтите источник.** Получите тикет, указанный пользователем — номер проблемы, URL или путь `.scratch/<feature>/issues/NN-*.md` (см. `docs/agents/issue-tracker.md`). Если он имеет метку `workflow::blocked`, проверьте его блокираторы так же, как это делает `/implement` — если какие-либо из них все еще открыты, остановитесь и сообщите пользователю, какие именно, вместо того чтобы составлять руководство для тикета, к выполнению которого на самом деле еще нельзя приступить. Если он имеет метку `workflow::specs` вместо `workflow::ready` или отсутствует `hitl`, сообщите об этом пользователю и спросите, следует ли все равно продолжить — этот навык ожидает полностью специфицированный тикет, направленный на человека (см. `docs/agents/triage-labels.md`).
2. **Исследуйте кодовую базу.** Определите точные файлы, которые нужно создать или изменить для выполнения тикета — не угадывайте только по тексту тикета. Используйте предметный глоссарий проекта и соблюдайте любые ADR (архитектурные решения) в той области, которую вы затрагиваете.
3. **Возьмите в работу.** Назначьте тикет мейнтейнеру (`gh issue edit <n> --add-assignee @me` или эквивалент локального трекера) перед любой другой записью — это та же конвенция, которую используют `/wayfinder` и `/implement`, чтобы параллельная сессия не выбрала тот же `hitl`-тикет.
4. **Отметьте как выполняемый.** Переместите тикет из `workflow::ready` в `workflow::in-progress` (для тикета локального трекера установите `**Workflow:** workflow::in-progress`).
5. **Составьте руководство**, используя `<guide-template>` ниже, на языке, который настроен в поле `language` файла `.harness/project.json` (по умолчанию `ru`, если файл или поле отсутствуют). В отличие от сводной таблицы `/to-tickets`, эта область применения не является узкой — напишите весь документ, включая промпты, на этом языке: это локальный артефакт для чтения человеком-мейнтейнером, а не тикет, публикуемый во внешнем трекере. Сохраните его в `docs/tasks/` в соответствии с соглашением об именовании в `docs/agents/artifacts.md` (ID проблемы + описательный слаг) — если тикет принадлежит к эпику (epic), в папку этого эпика `docs/tasks/issue-<epic-id>-<epic-slug>/`, рядом с самим файлом тикета.

## Правила для промптов

- Каждый промпт должен быть явным, самодостаточным и говорить ИИ IDE человека *точно*, что делать — человек будет копировать и вставлять его дословно, а не редактировать сначала.
- Укажите IDE на существующий код для подражания: «следуйте паттерну в `<file>`» лучше, чем описание паттерна.
- Делайте каждый шаг достаточно маленьким, чтобы он компилировался и рецензировался сам по себе — весь смысл в узких, проверяемых частях, та же дисциплина, что и вертикальные срезы `/to-tickets`.
- В каждом промпте сначала запрашивайте тест — этот репозиторий требует TDD (разработку через тестирование) (`docs/agents/git-workflow.md`) независимо от того, кто пишет код, и каждый другой путь тоже должен об этом говорить вслух (`/fast-implement` через `/tdd`, `/implement` через свое пакетное Определение Готовности - Definition of Done), поэтому скажите об этом явно здесь, вместо того чтобы предполагать, что ИИ IDE человека делает это по умолчанию.

<guide-template>

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
4. **Тикет, отслеживаемый в GitHub/GitLab:** откройте PR/MR согласно `docs/agents/git-workflow.md` (`gh pr create` на GitHub, `glab mr create` на GitLab; выбирайте `Closes #<ID>` только для целевой ветки по умолчанию и проверяйте закрытие после слияния (merge), в противном случае используйте `Related to #<ID>` и явно закройте issue (проблему) после подтвержденного слияния). Используйте шаблон тела напрямую или настройте и запустите `pr-composer` вручную в приложении для программирования. Если этот тикет имеет метку `task-report::required`, опубликуйте отчет о завершении при открытии PR.
   **Тикет, отслеживаемый локально в markdown:** здесь нет шага PR/слияния — как только все вышеперечисленное будет пройдено, самостоятельно установите строку `**Workflow:**` в файле тикета на `done`; если он имеет метку `**Task report:** required`, включите сводку о завершении в то же самое обновление.

</guide-template>
-->

