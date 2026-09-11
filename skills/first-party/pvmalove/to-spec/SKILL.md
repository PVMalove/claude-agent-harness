---
name: to-spec
description: Turn the current conversation into a spec and publish it to the project issue tracker. No interview, just synthesis of what you've already discussed.
disable-model-invocation: true
---

**Objective:** Synthesize the current conversation context and codebase understanding into a final specification.
**Strict Rule:** Do NOT interview the user or start a new grilling round. If information feels missing, it means the previous grilling was incomplete; synthesize the spec using only the known facts and explicit assumptions.

The issue tracker and triage label vocabulary should have been provided to you. If not, tell the user to run `/setup-matt-pocock-skills`.

## Execution in Two Phases

You must execute this skill in two distinct phases to ensure the user agrees with the testing strategy before you write the final document.

### Phase 1: Exploration & Seam Proposal
1. **Explore the Codebase:** Use the project's domain glossary (`CONTEXT.md`) and respect any ADRs in the touched area.
2. **Define Seams:** Sketch out the seams at which the feature will be tested.
    - Prefer existing seams over new ones.
    - Use the highest seam possible.
    - Ideal state: exactly *one* seam for the whole feature.
3. **STOP AND ASK:** Present your proposed seams and one integration branch name in the form
   `integration/<service-or-team>`. Derive the slug from explicit project/domain language; never
   guess it. Ask the user to approve both. **Do not proceed to Phase 2 until the user confirms.**

### Phase 2: Drafting & Publishing (After User Approval)
1. **Draft the File:** Write the spec using the `<spec-template>` below, in its own folder under `docs/tasks/`.
    - *Naming convention:* If the issue ID is known, use it. If not, use a descriptive slug (e.g., `docs/tasks/add-user-auth/add-user-auth.md`) and rename both the folder and file later once the ID is generated — see `docs/agents/artifacts.md` for the full convention, including the epic-folder grouping.
2. **Publish to Tracker:** Publish the issue using the CLI: `gh issue create --body-file <path>`.
    - **CRITICAL:** Do NOT use an inline `--body` heredoc. Spec bodies contain characters (nested quotes, backticks, etc.) that break heredoc quoting. Always use `--body-file`.
3. **Apply Labels:** This published issue acts as the feature's **epic**. Apply the following labels (see `docs/agents/triage-labels.md` for the full taxonomy):
    - `bug` OR `enhancement`
    - `workflow::specs` (Do NOT use `workflow::ready` as it requires decomposition first).
    - `task-report::required` (unless told to skip).
    - *Note:* Do NOT create ad-hoc `epic::<slug>` labels. `/to-tickets` will handle linking sub-tasks natively later, as GitHub sub-issues — see `docs/agents/issue-tracker.md#wayfinding-operations` for the mechanism.
4. **Ensure the selected integration branch exists after the epic issue succeeds:**
    - Read `base_branch` from `.harness/project.json` (default `main`). This is the release/base
      branch from which the epic integration branch starts.
    - Fetch the base ref. Create `integration/<service-or-team>` from `origin/<base_branch>` when
      a remote exists, otherwise from the local base branch. Do not switch the current worktree;
      it may contain unrelated changes.
    - If the integration branch already exists locally or remotely, reuse it without resetting,
      force-updating, or deleting it. If publication succeeds but branch creation fails, report
      the partial state and the exact recovery action; do not recreate the epic issue.
    - Push a newly created remote branch with `git push -u origin integration/<service-or-team>`.
      For a local tracker or a repository without a remote, create the local branch and report
      that it was not pushed.

---

<spec-template>

## Problem Statement
The problem that the user is facing, from the user's perspective.

## Solution
The solution to the problem, from the user's perspective.

## User Stories
A LONG, numbered list of user stories covering all aspects of the feature.
Format: `1. As an <actor>, I want a <feature>, so that <benefit>`
*(Example: As a bank customer, I want to see my balance, so that I can make informed spending decisions).*

## Implementation Decisions
A list of modules to build/modify, interface changes, technical clarifications, architectural decisions, schema changes, API contracts, and specific interactions.
- **DO NOT** include specific file paths or generic code snippets (they outdate quickly).
- **Exception:** If a prototype produced a snippet that encodes a decision perfectly (state machine, schema, type shape), inline it and note it came from a prototype. Trim it to the decision-rich parts only.

## Testing Decisions
- Description of what makes a good test here (test external behavior, not implementation details).
- Which modules will be tested.
- Prior art (similar existing tests in the codebase).

## Out of Scope
A strict list of things that will NOT be done. This is your insurance policy against over-engineering. Be explicit about boundaries so future agents do not build more than requested.

## Integration Branch

- Branch: `integration/<service-or-team>`
- Created from: `base_branch` in `.harness/project.json`
- Child issue branches start from this integration branch and open PRs back to it.

## Further Notes
Any remaining context or constraints.

</spec-template>
\n
<!--
Краткое описание (Summary): Этот навык (to-spec) синтезирует текущее обсуждение в готовую спецификацию без дополнительных вопросов пользователю и публикует её в трекер задач проекта в виде эпика в два этапа.

Перевод:
---
name: to-spec
description: Превратить текущий разговор в спецификацию и опубликовать ее в трекере задач проекта. Никаких интервью, только синтез того, что вы уже обсудили.
disable-model-invocation: true
---

**Цель:** Синтезировать контекст текущего разговора и понимание кодовой базы в окончательную спецификацию.
**Строгое правило:** НЕ проводите интервью с пользователем и не начинайте новый раунд допроса. Если кажется, что информации не хватает, это означает, что предыдущий допрос был неполным; синтезируйте спецификацию, используя только известные факты и явные предположения.

Вам должны были предоставить трекер задач и словарь меток сортировки (triage label vocabulary). Если нет, скажите пользователю выполнить `/setup-matt-pocock-skills`.

## Выполнение в две фазы

Вы должны выполнить этот навык в две отдельные фазы, чтобы убедиться, что пользователь согласен со стратегией тестирования, прежде чем писать итоговый документ.

### Фаза 1: Исследование и предложение швов (Seam Proposal)
1. **Исследование кодовой базы (Explore the Codebase):** Используйте предметный глоссарий проекта (`CONTEXT.md`) и соблюдайте любые ADR (архитектурные решения) в затрагиваемой области.
2. **Определение швов (Define Seams):** Набросайте швы (seams), на которых будет тестироваться функциональность.
    - Отдавайте предпочтение существующим швам перед новыми.
    - Используйте максимально высокий возможный шов.
    - Идеальное состояние: ровно *один* шов для всей фичи.
3. **ОСТАНОВИТЕСЬ И СПРОСИТЕ:** Представьте ваши предложенные швы и одно имя интеграционной ветки в формате
   `integration/<service-or-team>`. Выводите слаг (slug) из явного языка проекта/предметной области; никогда
   не угадывайте его. Попросите пользователя утвердить и то, и другое. **Не переходите к Фазе 2, пока пользователь не подтвердит.**

### Фаза 2: Составление и публикация (После одобрения пользователем)
1. **Составление файла:** Напишите спецификацию, используя `<spec-template>` ниже, в ее собственной папке внутри `docs/tasks/`.
    - *Соглашение об именовании:* Если ID задачи известен, используйте его. Если нет, используйте описательный слаг (например, `docs/tasks/add-user-auth/add-user-auth.md`) и переименуйте как папку, так и файл позже, как только будет сгенерирован ID — см. `docs/agents/artifacts.md` для полного соглашения, включая группировку по папкам эпиков (epic-folder grouping).
2. **Публикация в трекер:** Опубликуйте задачу, используя CLI: `gh issue create --body-file <path>`.
    - **КРИТИЧЕСКИ ВАЖНО:** НЕ используйте встроенный heredoc `--body`. Тела спецификаций содержат символы (вложенные кавычки, обратные кавычки и т. д.), которые ломают цитирование heredoc. Всегда используйте `--body-file`.
3. **Применение меток:** Эта опубликованная задача действует как **эпик** (epic) фичи. Примените следующие метки (см. `docs/agents/triage-labels.md` для полной таксономии):
    - `bug` ИЛИ `enhancement`
    - `workflow::specs` (НЕ используйте `workflow::ready`, так как это сначала требует декомпозиции).
    - `task-report::required` (если не сказано пропустить).
    - *Примечание:* НЕ создавайте ad-hoc метки `epic::<slug>`. `/to-tickets` позже нативно обработает связывание подзадач как подзадачи GitHub — см. `docs/agents/issue-tracker.md#wayfinding-operations` для механизма.
4. **Убедитесь, что выбранная интеграционная ветка существует после успешного создания задачи-эпика:**
    - Прочитайте `base_branch` из `.harness/project.json` (по умолчанию `main`). Это релизная/базовая
      ветка, от которой начинается интеграционная ветка эпика.
    - Получите базовую ссылку (fetch base ref). Создайте `integration/<service-or-team>` от `origin/<base_branch>`, когда
      существует удаленный репозиторий, иначе — от локальной базовой ветки. Не переключайте текущее рабочее дерево (worktree);
      оно может содержать не связанные изменения.
    - Если интеграционная ветка уже существует локально или удаленно, переиспользуйте ее без сброса (resetting),
      принудительного обновления (force-updating) или удаления. Если публикация прошла успешно, но создание ветки не удалось, сообщите
      о частичном состоянии и точном действии по восстановлению; не пересоздавайте задачу-эпик.
    - Отправьте (push) вновь созданную удаленную ветку с помощью `git push -u origin integration/<service-or-team>`.
      Для локального трекера или репозитория без удаленного, создайте локальную ветку и сообщите,
      что она не была отправлена.

---

<spec-template>

## Problem Statement (Постановка проблемы)
Проблема, с которой сталкивается пользователь, с точки зрения пользователя.

## Solution (Решение)
Решение проблемы с точки зрения пользователя.

## User Stories (Пользовательские истории)
ДЛИННЫЙ, пронумерованный список пользовательских историй, охватывающий все аспекты фичи.
Формат: `1. Как <actor>, я хочу <feature>, чтобы <benefit>` (1. Как <субъект>, я хочу <функцию>, чтобы <выгода>)
*(Пример: Как клиент банка, я хочу видеть свой баланс, чтобы принимать обоснованные решения о расходах).*

## Implementation Decisions (Решения по реализации)
Список модулей для создания/изменения, изменения интерфейсов, технические уточнения, архитектурные решения, изменения схемы, контракты API и конкретные взаимодействия.
- **НЕ** включайте конкретные пути к файлам или общие фрагменты кода (они быстро устаревают).
- **Исключение:** Если прототип создал фрагмент, который идеально кодирует решение (конечный автомат, схема, форма типа), встройте его и отметьте, что он взят из прототипа. Обрежьте его только до частей, богатых решениями.

## Testing Decisions (Решения по тестированию)
- Описание того, что делает тест хорошим здесь (тестирование внешнего поведения, а не деталей реализации).
- Какие модули будут тестироваться.
- Предшествующий уровень техники / Prior art (похожие существующие тесты в кодовой базе).

## Out of Scope (Вне рамок)
Строгий список вещей, которые НЕ будут сделаны. Это ваш страховой полис от переусложнения (over-engineering). Четко определите границы, чтобы будущие агенты не создавали больше, чем запрашивалось.

## Integration Branch (Интеграционная ветка)

- Ветка: `integration/<service-or-team>`
- Создана из: `base_branch` в `.harness/project.json`
- Дочерние ветки задач начинаются из этой интеграционной ветки и открывают PR к ней.

## Further Notes (Дополнительные заметки)
Любой оставшийся контекст или ограничения.

</spec-template>
-->
\n