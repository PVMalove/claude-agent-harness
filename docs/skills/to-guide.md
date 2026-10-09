# To Guide — В руководство

## 1. Название и локализация

- **Английское название:** `To Guide`
- **Русский перевод:**

```text
В руководство
```

## 2. Полный перевод текста скила

````text
---
name: to-guide
description: Преобразуйте тикет hitl или спецификацию в пошаговое руководство по ручной реализации с готовыми к вставке промптами для AI IDE (Cursor, Copilot Chat). Для тикетов, которые человек будет писать вручную, а не агент.
disable-model-invocation: true
---

# В руководство (подготовка ручной реализации)

Преобразуйте спецификацию или tracer-bullet тикет в руководство для разработчика с готовыми к использованию промптами для IDE с AI-помощником. Это соответствующий `hitl` маршрут для `/implement` и `/fast-implement`: они берут тикеты `afk`, и код пишет агент; `/to-guide` берёт тикеты `hitl` и вместо этого передаёт человеку чек-лист навигатора. После работы этого скила кодирование, code review, `qa-gate` и PR полностью остаются задачей человека — скил их не выполняет и не вызывается по этому тикету вновь (созданное руководство называет точные команды в заключительном разделе).

## Процесс

1. **Прочитайте источник.** Получите названный пользователем тикет — номер issue, URL или путь `.scratch/<feature>/issues/NN-*.md` (см. `docs/agents/issue-tracker.md`). Если у него есть `status::blocked`, проверьте блокеры так же, как это делает `/implement`: если хоть один ещё открыт, остановитесь и скажите пользователю, какие именно, вместо подготовки руководства для тикета, который в действительности ещё нельзя начать. Если у тикета `status::specs` вместо `status::ready` либо отсутствует `hitl`, сообщите это пользователю и спросите, продолжать ли всё равно — этот скил ожидает полностью специфицированный тикет, направленный человеку (см. `docs/agents/triage-labels.md`).
2. **Исследуйте кодовую базу.** Определите точные файлы, которые нужно создать или изменить для выполнения тикета, — не гадайте только по тексту тикета. Используйте глоссарий домена проекта и соблюдайте все ADR в затрагиваемой области.
3. **Заберите тикет.** Назначьте тикет сопровождающему (`gh issue edit <n> --add-assignee @me` в GitHub, `glab issue update <n> -R <project-url> --assignee @me` в GitLab либо эквивалент локального трекера) до любой другой записи — та же конвенция, которую используют `/wayfinder` и `/fast-implement`, чтобы параллельная сессия не выбрала тот же тикет `hitl`. В GitLab каждая команда `glab` получает `-R <project-url>`, а каждый вызов `glab api` пишется как `GITLAB_HOST=<host> glab api projects/<project-id>/...`; эти плейсхолдеры определены в `docs/agents/issue-tracker.md` → GitLab → Conventions.
4. **Пометьте его как выполняемый.** У тикета ровно одна метка `status::*` (см. `docs/agents/triage-labels.md`), поэтому замените текущую — `status::ready`, `status::blocked`, блокеры которой шаг 1 нашёл закрытыми, или `status::specs`, с которой пользователь согласился продолжить, — а не добавляйте вторую:
   - **GitHub:** `gh issue edit <n> --remove-label status::ready --remove-label status::blocked --remove-label status::specs --add-label status::in-progress`, затем проверьте через `gh issue view <n> --json labels --jq '[.labels[].name]'`, что `status::in-progress` — единственная метка `status::*`.
   - **GitLab:** `glab issue update <n> -R <project-url> --unlabel status::ready,status::blocked,status::specs --label status::in-progress`, затем проверьте через `glab issue view <n> -R <project-url> -F json`, что `status::in-progress` — единственная метка `status::*` в его `labels`.
   - **Локальный трекер:** установите строку `**Workflow:**` файла в `status::in-progress`.
   - Если запись метки не удалась, остановитесь и сообщите об этом.
5. **Подготовьте руководство** по `<guide-template>` ниже. В его разделе 5 оставьте только команды трекера этого проекта и подставьте реальные значения `<project-url>`, `<host>`, `<project-id>`, `<target-branch>`, `<ID>`, `<title>` и `<slug>` (`<title>` остаётся в одинарных кавычках, каждый апостроф записывается как `'\''` или как `''`, если shell человека — PowerShell) — человек копирует эти команды без правки; только номер PR/MR `<n>`/`<iid>` остаётся плейсхолдером, пока PR/MR не создан. Пишите руководство на языке, заданном полем `language` в `.harness/project.json` (по умолчанию `ru`, если файла или поля нет). В отличие от сводной таблицы `/to-tickets`, эта область не узкая — пишите весь документ, включая промпты, на этом языке: это локальный артефакт для чтения сопровождающим-человеком, а не тикет, опубликованный во внешнем трекере. Сохраните его в `docs/tasks/` по соглашению об именовании из `docs/agents/artifacts.md` (ID тикета + описательный slug) — если тикет принадлежит эпику, в папку этого эпика `docs/tasks/issue-<epic-id>-<epic-slug>/`, рядом с собственным файлом тикета.

## Правила для промптов

- Каждый промпт должен быть явным, самодостаточным и говорить AI IDE человека *точно*, что делать, — человек скопирует-вставит его дословно, не редактируя сначала.
- Указывайте IDE на существующий код для подражания: «следуй шаблону в `<file>`» лучше описания шаблона.
- Делайте каждый шаг достаточно малым, чтобы его можно было скомпилировать и проверить самостоятельно, — весь смысл в узких, проверяемых кусках, та же дисциплина, что у вертикальных срезов `/to-tickets`.
- В каждом промпте сначала просите тест — этот репозиторий требует TDD (`docs/agents/git-workflow.md`) независимо от того, кто пишет код, и каждый другой маршрут также должен говорить это вслух (`/fast-implement` через `/tdd`, `/implement` через Definition of Done своего batch), поэтому укажите это явно вместо предположения о настройках по умолчанию AI IDE человека.

<guide-template>

# Руководство по реализации: <название/ID тикета>

## 1. Контекст и ограничения

Архитектурные правила (ADR), шаблоны и границы, применимые здесь, — сжато из тикета/спецификации и кодовой базы, без полного повторения.

## 2. Карта файлов

- `[Создать]` path/to/new/file
- `[Обновить]` path/to/existing/file

## 3. Шаги и промпты

Разбейте реализацию на небольшие компилируемые части. Для каждой:

**Шаг N: <цель>**

```text
<полный, готовый к вставке промпт для AI IDE человека — ссылается на конкретные существующие файлы для следования, указывает выполняемый критерий приёмки>
```

## 4. Проверка

Как проверить, что этот срез работает: точная команда теста либо шаг `curl`/ручной шаг, взятый из критериев приёмки тикета или Testing Decisions спецификации.

## 5. Когда вы закончите

Этот скил не проверяет код, не создаёт commit, не запускает `qa-gate` и не открывает PR за вас — на маршруте `hitl` нет единой команды для всего этого. Когда код написан, сделайте это сами, по порядку:

1. Запустите `/code-review` (либо попросите эту сессию его запустить) — такой же двухосевой review Standards + Spec, который `/implement` выполнил бы для тикета `afk`. Ничто другое не запускает его на этом маршруте; пропуск означает, что diff не будет проверен перед PR.
2. Создайте commit своей работы с Semantic Commit Message (`feat:`, `fix:`, ...) и отправьте его по правилам `docs/agents/git-workflow.md` — не оставляйте завершённую работу без commit или push.
3. Запустите `/qa-gate` (либо попросите эту сессию его запустить).
4. **Тикет в GitHub/GitLab:** откройте PR/MR в `<target-branch>` по `docs/agents/git-workflow.md`. Запишите его body в `.harness/.sandboxes/pr_body/pr-body-<ID>-<slug>.md` по шаблону body напрямую либо вручную настройте и запустите `pr-composer` в coding application.
   - **Footer:** `Closes #<ID>` только когда `<target-branch>` — default branch (GitHub: `gh repo view --json defaultBranchRef --jq .defaultBranchRef.name`; GitLab: поле `default_branch` из `GITLAB_HOST=<host> glab api projects/<project-id>`), иначе `Related to #<ID>`. GitLab закрывает issue по `Closes #<ID>` только когда MR попадает в default branch.
   - **Откройте его.** GitHub: `gh pr create --base <target-branch> --title '<title>' --body-file .harness/.sandboxes/pr_body/pr-body-<ID>-<slug>.md`. GitLab: `glab mr create -R <project-url> --target-branch <target-branch> --title '<title>' --description-file .harness/.sandboxes/pr_body/pr-body-<ID>-<slug>.md --yes`; MR — это `!<iid>`, последний сегмент URL, который печатает команда.
   - **Completion report:** если у тикета есть `task-report::required`, опубликуйте completion report при открытии PR/MR. Запишите его в `.harness/.sandboxes/pr_body/issue-comment-<ID>-<slug>.md`, затем GitHub: `gh issue comment <ID> --body-file .harness/.sandboxes/pr_body/issue-comment-<ID>-<slug>.md`; GitLab: `GITLAB_HOST=<host> glab api projects/<project-id>/issues/<ID>/notes -F body=@.harness/.sandboxes/pr_body/issue-comment-<ID>-<slug>.md`.
   - **После вашего merge:** проверьте его (GitHub: `gh pr view <n> --json state,baseRefName`, `state` равен `MERGED`, а `baseRefName` — `<target-branch>`; GitLab: `glab mr view <iid> -R <project-url> -F json`, `state` равен `merged`, а `target_branch` — `<target-branch>`). Для `Related to #<ID>` закройте issue — GitHub: `gh issue close <ID> --reason completed`; GitLab: `glab issue close <ID> -R <project-url>`. Для `Closes #<ID>` проверьте, что issue закрыт, — GitHub: `gh issue view <ID> --json state` (`CLOSED`); GitLab: `glab issue view <ID> -R <project-url> -F json` (`state` равен `closed`; GitLab закрывает его асинхронно, поэтому прочитайте его ещё раз, если он всё ещё `opened`), — и закройте его той же командой, если этого не произошло.

   **Тикет в локальном Markdown-трекере:** шага PR/merge нет — когда всё выше пройдено, самостоятельно установите строку `**Workflow:**` файла тикета в `done`; если у него есть `**Task report:** required`, включите completion summary в то же обновление.

</guide-template>
````

## 3. Контракты

- **Вход (Input/Brief):** тикет `hitl` или спецификация, критерии приёмки, состояние блокировок и репозиторные соглашения.
- **Выход (Output/Report):** локальное пошаговое руководство в `docs/tasks/` с картой файлов, TDD-промптами, проверками и ручным маршрутом до PR.

## Источник

[SKILL.md](../../skills/first-party/pvmalove/to-guide/SKILL.md)
