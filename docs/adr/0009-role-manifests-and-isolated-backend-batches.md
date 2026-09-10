# Role manifests и изолированные backend batches

## Контекст системы

Backend-работа несколькими ролями требует наблюдаемых границ записи и исключения конфликтующих
изменений.

## Действующий контракт

Каждая роль имеет переносимый Markdown manifest в `harness/orchestration/roles/`. Проектная
`.harness/orchestration.json` сопоставляет роль provider profiles, обязательные model/effort,
budgets, зоны и verification commands. `developer`, `database-migrations` и `messaging-integration` записывают
только в объявленные зоны; `architect`, `qa` и `code-review` работают read-only.

Batch владеет одной issue-веткой и worktree. Одновременные batches не пересекают service, bounded
context или infrastructure zone. Внутри batch handoff последовательны, а одновременно активен
только один writer. Тяжёлые integration и quality checks используют одну serialised lane.

## Операционные последствия

Coordinator проверяет границы роли, зоны, writer и lane до dispatch. API contracts, migrations,
messaging/outbox, transactions, authorization/security и concurrency/retry требуют code-review.
