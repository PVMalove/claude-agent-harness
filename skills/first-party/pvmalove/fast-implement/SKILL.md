---
name: fast-implement
description: "Implement a piece of work in a single session, without the coordinator pipeline's approval gates."
disable-model-invocation: true
---

# Fast implement

**Objective:** Implement the work described by the user in the spec or tickets, in this session, with
no architect step, no independent QA role, and no coordinator approval gates.

This is the short path. Use it for a ticket whose direction is not in question and whose blast radius
is small. For anything else use `/implement`, which runs the gated architect → developer →
code-review → QA pipeline.

## Execution in Three Phases

A strict pipeline, resolved in order: **Pre-flight** (confirm the ticket is actually startable, and by this agent) → **Coding** (TDD, tests, an explicitly approved review, commit, and push) → **PR & Wrap-up** (offer the separate `/to-pull-requests` command). Only the developer can select `/to-pull-requests`.

### Phase 1: Pre-flight

1. **Resolve the ticket.**
    - **A specific ticket is named** (an issue number, URL, or a `.scratch/<feature>/issues/NN-*.md` path): use it.
        - **Fail fast on `hitl`:** if it carries `hitl` (see `docs/agents/triage-labels.md`), stop immediately and tell the user to run `/to-guide` instead — don't check anything else on this ticket, this agent only implements `afk` work.
    - **An epic is named** (carries `workflow::specs`, or is otherwise the parent of a decomposition) rather than a specific ticket: pick the next ticket yourself instead of asking. Never auto-pick a `hitl` ticket — that execution mode always routes through `/to-guide`, not this agent.
        - **GitHub/GitLab:** run the same frontier query `/wayfinder` uses (`docs/agents/issue-tracker.md#wayfinding-operations`), scoped to the epic's sub-issues and filtered to `afk` — open, unblocked, unclaimed, first in decomposition order. Claim the chosen ticket (`gh issue edit <n> --add-assignee @me` on GitHub, `glab issue update <n> --assignee @me` on GitLab) before any other write, the same way `/wayfinder` claims a ticket, so a concurrent session (e.g. a parallel worktree) doesn't pick the same one. If the filtered frontier is empty, stop and tell the user why: if open, unblocked, unclaimed tickets remain but all are `hitl`, say so and point at `/to-guide`; otherwise explain that nothing is unblocked yet, or everything is already claimed.
        - **Local tracker:** read each `.scratch/<feature>/issues/NN-*.md` file in filename order — a purely linear chain — and take the first one that is both `**Workflow:** workflow::ready` and `**Execution:** afk`. If tickets remain but every `workflow::ready` one is `hitl`, say so — naming them — and point at `/to-guide` instead of picking one.
        - Once chosen this way, treat the ticket exactly like one named explicitly for the rest of this process.
    - **Nothing is named:** when this repo defines a git workflow doc (e.g. `docs/agents/git-workflow.md`) with an "Issue First" rule, stop and ask the user to name an existing ticket or run `/to-spec`/`/to-tickets` first — don't start the work. Otherwise, skip this check entirely.
2. **Check blockers**, on the resolved ticket. If it carries `workflow::blocked` (see `docs/agents/triage-labels.md`), check its blockers using this repo's tracker (native GitHub/GitLab dependency links, or the `Blocked by:`/`**Blocked by:**` field — see `docs/agents/issue-tracker.md`).
    - Any blocker still open → stop and tell the user which ones. Don't start the work.
    - All blockers closed/resolved → clear the block before proceeding:
        - **GitHub/GitLab:** replace the `workflow::blocked` label with `workflow::ready`.
        - **Local tracker:** set the file's `**Workflow:**` line to `workflow::ready`.
3. **Mark it in progress**, once you actually start work:
    - **GitHub/GitLab:** set the `workflow::in-progress` label.
    - **Local tracker:** set the file's `**Workflow:**` line to `workflow::in-progress`.
4. **Git pre-flight, before editing files:**
    - Resolve the exact integration branch from the ticket's `## Integration Branch` section or,
      for a child ticket that omits it, from its parent epic. An absent value is a blocker; do not
      infer a branch from memory, the current checkout, or a service name.
    - The current branch must match `branch_pattern` and be an issue branch. If it does not,
      fetch the integration branch and create `feature/issue-<ID>-<slug>` from it before coding.
      Never commit or push directly to the project base branch or an `integration/*` branch.

### Phase 2: Coding

1. Use `/tdd` where possible, at pre-agreed seams.
2. Run typechecking regularly, single test files regularly, and the full test suite once at the end.
3. Ask the developer: “Провести code review?” Stop for their answer.
    - **Yes:** run `/code-review`. It launches the Standards and Spec subagents through the coding application's manually configured mechanism, waits for both reports, and returns its separate `## Standards` and `## Spec` report to this primary session. Address any requested changes, then repeat the relevant tests before continuing.
    - **No:** record that the developer declined review and continue.
4. Ask the developer for explicit permission to commit and push. Stop for their answer.
5. After approval, verify that the current branch still matches `branch_pattern` and is neither `base_branch` nor `integration/*`; commit the completed work with a Semantic Commit Message and push it to the current issue branch. Report the commit and push result to the developer.

### Phase 3: PR & Wrap-up

After a successful push, offer `/to-pull-requests <ticket>` as the next command. Do not invoke it automatically, open a PR, run `qa-gate`, or close the ticket in this skill.
\n
<!--
Краткое описание (Summary): Навык `fast-implement` предназначен для быстрой реализации задач за один сеанс без промежуточных этапов согласования, проверок архитектора и тестировщика. Состоит из трех этапов: подготовка (Pre-flight), кодирование (Coding) и предложение создания пулл-реквеста (PR & Wrap-up).

Перевод:
---
name: fast-implement
description: "Реализовать часть работы за один сеанс, без этапов утверждения конвейера координатора."
disable-model-invocation: true
---

# Быстрая реализация (Fast implement)

**Цель:** Реализовать работу, описанную пользователем в спецификации или тикетах, в этом сеансе, без
этапа архитектора, без независимой роли QA и без этапов утверждения координатором.

Это короткий путь. Используйте его для тикета, направление которого не вызывает сомнений, а радиус поражения
мал. Для всего остального используйте `/implement`, который запускает конвейер с проверками: архитектор → разработчик →
code-review → QA.

## Выполнение в три этапа

Строгий конвейер, выполняемый по порядку: **Подготовка** (подтверждение того, что тикет действительно можно начать, и именно этим агентом) → **Кодирование** (TDD, тесты, явно утвержденное ревью, коммит и пуш) → **PR и завершение** (предложение отдельной команды `/to-pull-requests`). Только разработчик может выбрать `/to-pull-requests`.

### Этап 1: Подготовка (Pre-flight)

1. **Разрешение тикета.**
    - **Указан конкретный тикет** (номер проблемы, URL или путь `.scratch/<feature>/issues/NN-*.md`): используйте его.
        - **Быстрый отказ при `hitl`:** если он содержит `hitl` (см. `docs/agents/triage-labels.md`), немедленно остановитесь и скажите пользователю запустить `/to-guide` вместо этого — не проверяйте ничего другого в этом тикете, этот агент реализует только работу `afk`.
    - **Указан эпик** (содержит `workflow::specs` или иным образом является родителем декомпозиции), а не конкретный тикет: выберите следующий тикет самостоятельно вместо того, чтобы спрашивать. Никогда не выбирайте автоматически тикет `hitl` — этот режим выполнения всегда маршрутизируется через `/to-guide`, а не через этого агента.
        - **GitHub/GitLab:** выполните тот же пограничный запрос, который использует `/wayfinder` (`docs/agents/issue-tracker.md#wayfinding-operations`), ограниченный подзадачами эпика и отфильтрованный по `afk` — открытые, незаблокированные, неназначенные, первые в порядке декомпозиции. Заберите выбранный тикет (`gh issue edit <n> --add-assignee @me` на GitHub, `glab issue update <n> --assignee @me` на GitLab) перед любой другой записью, так же, как `/wayfinder` забирает тикет, чтобы параллельный сеанс (например, параллельное рабочее дерево) не выбрал тот же самый. Если отфильтрованная граница пуста, остановитесь и скажите пользователю почему: если открытые, незаблокированные, неназначенные тикеты остались, но все они `hitl`, скажите об этом и укажите на `/to-guide`; в противном случае объясните, что еще ничего не разблокировано, или все уже назначено.
        - **Локальный трекер:** прочитайте каждый файл `.scratch/<feature>/issues/NN-*.md` в порядке имени файла — чисто линейная цепочка — и возьмите первый, который является одновременно `**Workflow:** workflow::ready` и `**Execution:** afk`. Если тикеты остались, но каждый `workflow::ready` является `hitl`, скажите об этом — назвав их — и укажите на `/to-guide` вместо того, чтобы выбирать один.
        - После такого выбора обращайтесь с тикетом точно так же, как с тем, который был явно назван для остальной части этого процесса.
    - **Ничего не названо:** когда в этом репозитории определен документ о рабочем процессе git (например, `docs/agents/git-workflow.md`) с правилом "Issue First" (Сначала проблема), остановитесь и попросите пользователя назвать существующий тикет или запустить сначала `/to-spec`/`/to-tickets` — не начинайте работу. В противном случае полностью пропустите эту проверку.
2. **Проверка блокировщиков** на выбранном тикете. Если он содержит `workflow::blocked` (см. `docs/agents/triage-labels.md`), проверьте его блокировщики, используя трекер этого репозитория (встроенные ссылки зависимостей GitHub/GitLab, или поле `Blocked by:`/`**Blocked by:**` — см. `docs/agents/issue-tracker.md`).
    - Любой блокировщик все еще открыт → остановитесь и скажите пользователю, какие именно. Не начинайте работу.
    - Все блокировщики закрыты/разрешены → снимите блокировку перед продолжением:
        - **GitHub/GitLab:** замените метку `workflow::blocked` на `workflow::ready`.
        - **Локальный трекер:** установите строку `**Workflow:**` файла в `workflow::ready`.
3. **Отметьте как находящийся в работе**, как только вы действительно начнете работу:
    - **GitHub/GitLab:** установите метку `workflow::in-progress`.
    - **Локальный трекер:** установите строку `**Workflow:**` файла в `workflow::in-progress`.
4. **Предварительная проверка Git перед редактированием файлов:**
    - Определите точную ветку интеграции из раздела `## Integration Branch` (Ветка интеграции) тикета или,
      для дочернего тикета, в котором он опущен, из его родительского эпика. Отсутствующее значение является блокировщиком; не
      выводите ветку из памяти, текущего checkout или имени службы.
    - Текущая ветка должна соответствовать `branch_pattern` и быть веткой проблемы (issue branch). Если это не так,
      извлеките ветку интеграции и создайте `feature/issue-<ID>-<slug>` из нее перед кодированием.
      Никогда не делайте коммиты и не пушьте напрямую в базовую ветку проекта или в ветку `integration/*`.

### Этап 2: Кодирование (Coding)

1. Используйте `/tdd` где это возможно, на заранее согласованных стыках.
2. Регулярно запускайте проверку типов, регулярно запускайте отдельные файлы тестов и один раз в конце — полный набор тестов.
3. Спросите разработчика: "Провести code review?" Остановитесь, ожидая его ответа.
    - **Да:** запустите `/code-review`. Это запустит субагентов Standards и Spec через настроенный вручную механизм приложения для кодирования, дождется обоих отчетов и вернет свой отдельный отчет `## Standards` и `## Spec` в этот основной сеанс. Устраните любые запрошенные изменения, затем повторите соответствующие тесты перед продолжением.
    - **Нет:** запишите, что разработчик отказался от ревью, и продолжайте.
4. Запросите у разработчика явное разрешение на коммит и пуш. Остановитесь, ожидая его ответа.
5. После утверждения убедитесь, что текущая ветка по-прежнему соответствует `branch_pattern` и не является ни `base_branch`, ни `integration/*`; закоммитьте завершенную работу с использованием семантического сообщения коммита (Semantic Commit Message) и запушьте ее в текущую ветку проблемы. Сообщите разработчику о результате коммита и пуша.

### Этап 3: PR и завершение (PR & Wrap-up)

После успешного пуша предложите `/to-pull-requests <ticket>` в качестве следующей команды. Не вызывайте ее автоматически, не открывайте PR, не запускайте `qa-gate` и не закрывайте тикет в этом навыке.
-->
\n