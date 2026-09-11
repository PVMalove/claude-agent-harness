---
name: to-tickets
description: Break a plan, spec, or conversation into a set of tracer-bullet tickets with blocking edges, and publish them to the configured tracker.
disable-model-invocation: true
---

# To Tickets

**Objective:** Break a plan, spec, or conversation into a set of **tickets** — tracer-bullet vertical slices, each declaring its blocking edges.

The issue tracker and triage label vocabulary should have been provided to you. If not, tell the user to run `/setup-matt-pocock-skills`.

## Execution in Two Phases

You must execute this skill in two distinct phases. Do NOT publish anything to the issue tracker or create local files until the user explicitly approves the ticket breakdown.

### Phase 1: Drafting & Review
1. **Gather Context:** Work from the conversation context. If passed a reference (spec path, issue number/URL), read its full body and comments.
2. **Explore the Codebase (Optional):** Use the project's domain glossary. Look for prefactoring opportunities ("Make the change easy, then make the easy change").
3. **Draft Vertical Slices:** Break the work into **tracer bullet** tickets.
    - *Vertical Slices:* Cut a narrow but COMPLETE path through every layer (schema, API, UI, tests). Must be demoable/verifiable on its own and fit in a single context window.
    - *Wide Refactors (Exception):* If a change has a massive blast radius (e.g., renaming a shared column), use **expand-contract** instead of vertical slicing. Sequence as: Expand → Migrate (in batches) → Contract.
    - *Blocking Edges:* Give each ticket its blocking edges (which other tickets must complete first).
    - *Human Time Estimate:* Estimate the rough time required for a human developer to complete this slice (e.g., "2 hours", "1 day") — for every ticket, `afk` included, not just `hitl` ones. This is a decomposition-quality signal, not a commitment: an estimate in weeks means the slice isn't tracer-bullet-sized — split it further before presenting the breakdown.
4. **STOP AND ASK (Quiz the User):** Present the proposed breakdown as a numbered list. For each ticket, show:
    - **Title:** Short descriptive name
    - **Blocked by:** Which tickets gate it
    - **Est. Time (Human):** The estimated time for a human to complete it
    - **What it delivers:** The end-to-end behavior
    - *Ask the user:* Does the granularity feel right? Are blocking edges correct? Should anything be merged/split? Is the time estimate realistic?
    - **DO NOT PROCEED TO PHASE 2 UNTIL APPROVED.**

### Phase 2: Publishing & Summarizing (After Approval)
1. **Publish to the Tracker:** The method depends on the configured tracker:
    - **Integration branch:** Read the parent epic's `## Integration Branch` section before writing
      any child ticket. Copy its exact branch name into every child ticket; if the epic has no
      integration branch, stop and report the missing prerequisite instead of inferring one.
    - **Local files:** Write one file per ticket under `.scratch/<feature-slug>/issues/<NN>-<slug>.md` (01, 02...). Use `<local-ticket-template>`. Set `**Workflow:**` to `workflow::blocked` if it has blockers, otherwise `workflow::ready`. Set `**Execution:**` to `hitl` or `afk` per your best judgment of the ticket (see `docs/agents/triage-labels.md`). Add `**Task report:** required` unless told to skip it (omit the line entirely if not required). `/implement` finds the next ticket by reading each file's `**Workflow:**` field — a purely linear chain resolves top to bottom.
    - **GitHub / Real Tracker:**
        - Publish one issue per ticket in dependency order using `gh issue create --body-file <path>`. **CRITICAL:** Do NOT use inline `--body` heredoc, as it breaks bash quoting.
        - Apply labels (see `docs/agents/triage-labels.md` for the full taxonomy): `bug`/`enhancement`, `workflow::ready` (or `workflow::blocked` if gated by another ticket in this batch), `hitl`/`afk`, and `task-report::required` unless told to skip it.
        - *Grouping:* Link every ticket to the parent epic as a **native sub-issue**. Do NOT use `epic::<slug>` labels.
        - *Local Mirror:* The epic spec already lives in its own folder under `docs/tasks/` (per `docs/agents/artifacts.md`) — rename that folder to `issue-<epic-id>-<epic-slug>/` first if it was still slug-only. Save each published ticket's issue body into that same folder, as `issue-<ID>-<slug>.md`.
        - *Frontier:* Don't trace `Blocked by` by hand to find what's takeable — query it, the same fields and mechanism as `/wayfinder`'s frontier query (`docs/agents/issue-tracker.md#wayfinding-operations`), scoped to the epic's sub-issues instead of the map's children. `/implement` runs this same query itself when handed the epic instead of a specific ticket.
        - Do NOT close or rewrite the parent epic issue, except to append a short list of the subtask numbers you created.
2. **Summarize the Batch:**
    - Read `language` from `.harness/project.json` (default `ru` if the file or field is absent) — this decides only the "What to build" column below, not the ticket titles/bodies you publish, which stay in whatever language you drafted them in.
    - If this runtime supports dispatching a sub-agent pinned to a specific model, send a single call with `model: haiku` (cheapest available, one call for the whole batch) — pass it every ticket's title and body plus the target language, asking for one concise sentence per ticket written in that language. Otherwise, write the descriptions yourself, on your own model, in the same language.
    - Output a final table compiling all data. The labels column must list all applied taxonomy tags (e.g., `enhancement`, `workflow::ready`, `afk`, `task-report::required`).

   | Ticket | What to build | Est. Time (Human) | Labels |
      |---|---|---|---|
   | <number/link/path> | <one-line summary> | <time> | <comma-separated labels> |

---

<local-ticket-template>
# <NN> — <Ticket title>

**What to build:** The end-to-end behavior this ticket makes work from the user's perspective.
**Blocked by:** The numbers/titles of the tickets that gate this one, or "None — can start immediately".
**Integration branch:** The exact branch recorded by the parent epic.

**Category:** bug / enhancement
**Workflow:** workflow::ready (or workflow::blocked)
**Execution:** hitl / afk
**Task report:** required (omit the line entirely if not required)

- [ ] Acceptance criterion 1
- [ ] Acceptance criterion 2
  </local-ticket-template>

<issue-template>
## Parent
A reference to the parent issue on the tracker (if applicable).

## Integration Branch

The exact integration branch recorded by the parent epic. Child implementation branches start
from this branch and their PRs target it.

## What to build
The end-to-end behavior this ticket makes work from the user's perspective.

## Acceptance criteria
- [ ] Criterion 1
- [ ] Criterion 2

## Blocked by
- A reference to each blocking ticket, or "None — can start immediately".
  </issue-template>

*Note for both templates: Avoid specific file paths or code snippets unless it is a vital prototype snippet (trim to decision-rich parts only).*
\n
<!--
Краткое описание (Summary): Навык для декомпозиции плана, спецификации или обсуждения на набор задач ("tracer-bullet tickets") с указанием блокирующих зависимостей и последующей их публикацией в настроенный трекер задач (в два этапа: черновик с ревью и сама публикация).

Перевод:
---
name: to-tickets
description: Разбить план, спецификацию или обсуждение на набор задач ("tracer-bullet tickets") с блокирующими связями и опубликовать их в настроенном трекере.
disable-model-invocation: true
---

# В задачи (To Tickets)

**Цель:** Разбить план, спецификацию или обсуждение на набор **задач** (tickets) — вертикальных срезов-трассировщиков, каждый из которых объявляет свои блокирующие зависимости.

Доступ к трекеру задач и словарю ярлыков сортировки должен был быть вам предоставлен. Если нет, скажите пользователю выполнить `/setup-matt-pocock-skills`.

## Выполнение в две фазы

Вы должны выполнять этот навык в две отдельные фазы. НЕ публикуйте ничего в трекер задач и не создавайте локальные файлы, пока пользователь явно не утвердит декомпозицию на задачи.

### Фаза 1: Черновик и ревью
1. **Сбор контекста:** Работайте на основе контекста обсуждения. Если передана ссылка (путь к спецификации, номер/URL задачи), прочитайте её полное тело и комментарии.
2. **Изучение кодовой базы (Необязательно):** Используйте предметный глоссарий проекта. Ищите возможности для предварительного рефакторинга ("Сделайте изменение простым, затем сделайте простое изменение").
3. **Набросок вертикальных срезов:** Разбейте работу на задачи-**трассировщики**.
    - *Вертикальные срезы:* Проложите узкий, но ПОЛНЫЙ путь через каждый слой (схема, API, UI, тесты). Он должен быть готов к демонстрации/проверке сам по себе и помещаться в одном контекстном окне.
    - *Обширные рефакторинги (Исключение):* Если изменение имеет огромный радиус поражения (например, переименование общего столбца), используйте подход **расширение-сжатие** (expand-contract) вместо вертикального нарезания. Последовательность: Расширение → Миграция (партиями) → Сжатие.
    - *Блокирующие связи:* Укажите для каждой задачи её блокирующие связи (какие другие задачи должны быть завершены первыми).
    - *Оценка времени человека:* Оцените примерное время, необходимое разработчику-человеку для завершения этого среза (например, "2 часа", "1 день") — для каждой задачи, включая `afk`, а не только `hitl`. Это сигнал качества декомпозиции, а не обязательство: оценка в неделях означает, что срез не размера "трассировщика" — разбейте его дальше, прежде чем представлять декомпозицию.
4. **ОСТАНОВИТЕСЬ И СПРОСИТЕ (Опрос пользователя):** Представьте предложенную декомпозицию в виде нумерованного списка. Для каждой задачи покажите:
    - **Название:** Краткое описательное имя
    - **Заблокировано кем (Blocked by):** Какие задачи её блокируют
    - **Оценка времени (Человек):** Ожидаемое время на выполнение человеком
    - **Что поставляется:** Поведение системы от начала до конца
    - *Спросите пользователя:* Кажется ли гранулярность правильной? Верны ли блокирующие зависимости? Стоит ли что-то объединить/разделить? Реалистична ли оценка времени?
    - **НЕ ПЕРЕХОДИТЕ К ФАЗЕ 2 ДО ПОЛУЧЕНИЯ ОДОБРЕНИЯ.**

### Фаза 2: Публикация и подведение итогов (После одобрения)
1. **Публикация в трекере:** Метод зависит от настроенного трекера:
    - **Интеграционная ветка:** Прочтите раздел `## Integration Branch` родительского эпика перед написанием
      любой дочерней задачи. Скопируйте её точное имя ветки в каждую дочернюю задачу; если у эпика нет
      интеграционной ветки, остановитесь и сообщите об отсутствии предварительного условия, вместо того чтобы придумывать его.
    - **Локальные файлы:** Создайте по одному файлу на задачу в `.scratch/<feature-slug>/issues/<NN>-<slug>.md` (01, 02...). Используйте `<local-ticket-template>`. Установите `**Workflow:**` в `workflow::blocked`, если есть блокирующие факторы, иначе `workflow::ready`. Установите `**Execution:**` в `hitl` или `afk` по вашему лучшему суждению о задаче (см. `docs/agents/triage-labels.md`). Добавьте `**Task report:** required`, если вам не сказали пропустить это (полностью пропустите эту строку, если не требуется). `/implement` находит следующую задачу, считывая поле `**Workflow:**` каждого файла — чисто линейная цепочка решается сверху вниз.
    - **GitHub / Реальный трекер:**
        - Опубликуйте по одному issue на задачу в порядке зависимости, используя `gh issue create --body-file <path>`. **КРИТИЧЕСКИ ВАЖНО:** НЕ используйте встроенный heredoc `--body`, так как это ломает экранирование в bash.
        - Примените метки (см. полную таксономию в `docs/agents/triage-labels.md`): `bug`/`enhancement`, `workflow::ready` (или `workflow::blocked`, если она заблокирована другой задачей в этой партии), `hitl`/`afk` и `task-report::required`, если вам не сказали пропустить это.
        - *Группировка:* Свяжите каждую задачу с родительским эпиком как **нативный под-issue**. НЕ используйте метки `epic::<slug>`.
        - *Локальное зеркало:* Спецификация эпика уже находится в собственной папке в `docs/tasks/` (согласно `docs/agents/artifacts.md`) — сначала переименуйте эту папку в `issue-<epic-id>-<epic-slug>/`, если она всё ещё содержала только слаг. Сохраните тело каждого опубликованного issue в эту же папку как `issue-<ID>-<slug>.md`.
        - *Фронтир (Frontier):* Не отслеживайте `Blocked by` вручную, чтобы найти, что можно взять в работу — запросите это, те же поля и механизм, что и в запросе фронтира `/wayfinder` (`docs/agents/issue-tracker.md#wayfinding-operations`), с областью видимости до под-issue эпика, а не дочерних элементов карты. `/implement` сам запускает этот же запрос, когда ему передают эпик, а не конкретную задачу.
        - НЕ закрывайте и не переписывайте родительский эпик issue, за исключением добавления в конец краткого списка номеров созданных вами подзадач.
2. **Подведение итогов по партии:**
    - Прочитайте `language` из `.harness/project.json` (по умолчанию `ru`, если файл или поле отсутствует) — это определяет только столбец "Что нужно сделать" ниже, а не заголовки/тела публикуемых задач, которые остаются на том языке, на котором вы их составили.
    - Если эта среда выполнения поддерживает диспетчеризацию подагента, привязанного к конкретной модели, отправьте один вызов с `model: haiku` (самая дешёвая из доступных, один вызов на всю партию) — передайте ему заголовок и тело каждой задачи, а также целевой язык, запросив одно лаконичное предложение на каждую задачу, написанное на этом языке. Иначе напишите описания сами, на вашей собственной модели, на том же языке.
    - Выведите итоговую таблицу, собирающую все данные. Столбец Labels должен содержать все примененные теги таксономии (например, `enhancement`, `workflow::ready`, `afk`, `task-report::required`).

   | Задача | Что нужно сделать | Оценка времени (Человек) | Метки |
      |---|---|---|---|
   | <номер/ссылка/путь> | <однострочное резюме> | <время> | <метки, разделенные запятыми> |

---

<local-ticket-template>
# <NN> — <Название задачи>

**What to build:** Поведение системы от начала до конца, которое эта задача делает рабочим с точки зрения пользователя.
**Blocked by:** Номера/названия задач, которые блокируют эту, или "None — can start immediately".
**Integration branch:** Точная ветка, записанная родительским эпиком.

**Category:** bug / enhancement
**Workflow:** workflow::ready (или workflow::blocked)
**Execution:** hitl / afk
**Task report:** required (полностью пропустите эту строку, если не требуется)

- [ ] Критерий приемки 1
- [ ] Критерий приемки 2
  </local-ticket-template>

<issue-template>
## Parent
Ссылка на родительскую задачу в трекере (если применимо).

## Integration Branch

Точная интеграционная ветка, записанная родительским эпиком. Дочерние ветки реализации начинаются
с этой ветки, и их PR направляются в неё.

## What to build
Поведение системы от начала до конца, которое эта задача делает рабочим с точки зрения пользователя.

## Acceptance criteria
- [ ] Критерий 1
- [ ] Критерий 2

## Blocked by
- Ссылка на каждую блокирующую задачу, или "None — can start immediately".
  </issue-template>

*Примечание к обоим шаблонам: Избегайте конкретных путей к файлам или фрагментов кода, если это не жизненно важный фрагмент прототипа (обрежьте только до частей, насыщенных решениями).*
-->
\n