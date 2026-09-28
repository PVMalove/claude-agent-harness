# Руководства и агенты

Операционные руководства исходного репозитория и русские описания проектных агентов. Руководства,
кроме `releases.md`, совпадают с шаблонами `harness/project/docs-agents/`, которые `harness init`
разворачивает в `docs/agents/` целевого проекта; этот `README.md` и описания агентов в целевой
проект не копируются.

## Руководства

| Документ | Содержимое |
| --- | --- |
| [harness-guide.md](./harness-guide.md) | Установка, команды CLI, `health`, `console`, скилы и hooks. |
| [git-workflow.md](./git-workflow.md) | Ветки, коммиты, PR и закрытие тикетов. |
| [issue-tracker.md](./issue-tracker.md) | Работа с трекером issues. |
| [triage-labels.md](./triage-labels.md) | Метки триажа и их цвета. |
| [artifacts.md](./artifacts.md) | Временные артефакты и их размещение. |
| [worktrees.md](./worktrees.md) | Параллельная работа в Git worktree. |
| [backend-orchestration.md](./backend-orchestration.md) | Backend-оркестрация: конфиг, coordinator и lifecycle. |
| [releases.md](./releases.md) | Выпуск версии и GitHub Release. |

## Агенты

Проектные субагенты из `harness/project/agents/`. Каждое описание содержит название и
локализацию, полный перевод манифеста, контракты и схему.

| Агент | Назначение |
| --- | --- |
| [code-review-standards](./code-review-standards.md) | Ось Standards скила `code-review`: соответствие стандартам репозитория и «запахи» Фаулера. |
| [code-review-spec](./code-review-spec.md) | Ось Spec скила `code-review`: полнота реализации и выход за рамки задачи. |
| [pr-composer](./pr-composer.md) | Заполняет тело PR по шаблону проекта и возвращает путь к файлу. |
