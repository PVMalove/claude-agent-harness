# Context Package, checkpoint/continuation и base-commit gate

## Контекст

После разделения orchestration core на contract, lifecycle ledger и gate-runner повторное discovery
каждой ролью расходует контекст, длинный TDD-цикл перегружает одну worker session, а локальный HEAD
может быть старее актуальной вершины integration-ветки. Discovery Pipeline дополнительно требует
передать согласованный человеком список файлов от проектирования к декомпозиции и реализации.

## Решение

1. `/grilling` ведёт `Live Artifact` с кандидатными путями, но добавляет путь только после explicit
   opt-in пользователя. `/to-spec` переносит список в `## Relevant Files (Discovery Context)`, а
   `/to-tickets` распределяет пути по тикетам и строит filtered Repo Map. Cheap advisory может только
   добавить exact dependency из этой карты; его вывод эфемерен и не имеет authority.
2. `context_builder.py` остаётся отдельным детерминированным LLM-free модулем. Для pinned
   `base_commit`/`candidate_commit` он создаёт exact diff, стартовые файлы, bounded graph, тесты,
   ADR/precedent cards, размер и hashes. Прямые локальные импорты разворачиваются на один уровень;
   неизвестные форматы получают первые 30 строк. Coordinator регистрирует результат как immutable
   ledger-owned Context Package и пока проверяет его freshness в shadow-режиме.
3. Только write-роли (`developer`, `database-migrations`, `messaging-integration`) могут записать
   non-terminal checkpoint и продолжить тот же dispatch в новой worker session. Checkpoint содержит
   только commit SHA, изменённые файлы, оставшийся DoD, проверки, risks/blockers и Context Package
   ID; история чата и traceback не переносятся. Каждая новая session заново проходит self-report и
   heartbeat. Recognized rate-limit разрешает resume автоматически; planned trigger использует
   существующий coordinator decision и adaptive project policy. Read-only роли не продолжаются.
4. `batch create` после `git fetch origin <integration_ref>` фиксирует актуальный base. Перед
   review/publish gate повторяется. Drift требует нового developer/rebase dispatch, новый commit
   получает новый risk assessment.
5. Delta-review разрешён только для test-only fix после ровно одного Warning и одного Clean:
   Standards=Clean наследуется, Warning-ось проверяется заново, а delta-review всегда является новым
   независимым dispatch. Любое production-изменение требует полного review.
6. Baseline и `delivery-stats` добавляют cache read/write tokens, worker sessions и причины restart,
   review diff scope excess и QA failure rate. Значения принимаются только из наблюдаемой telemetry,
   ledger и coordinator decisions; отсутствующие данные не заменяются оценками.

## Последствия

Context Package уменьшает повторный discovery, но не отменяет brief, approval, review или QA и в
shadow-фазе не блокирует stale dispatch. Checkpoint экономит историю длинного TDD, сохраняя один
dispatch и его authority boundary, однако требует строгой проверки неизменности восстановленных
фактов. Base gate может остановить публикацию и потребовать rebase dispatch, зато reviewer всегда
получает diff от актуальной integration-базы. Advisory остаётся безопасным utility-инструментом,
потому что не может менять ledger state или scope.

## Отклонённые альтернативы

- Полное повторное чтение репозитория каждой ролью — сохраняет blind discovery и token cost.
- Передача полного чата/traceback в новую session — переносит шум вместо полезного состояния.
- Автоматический resume для любой причины — может обойти coordinator approval.
- Обновление integration-ветки самим coordinator-ом — нарушает write-role boundary.
- Наследование review для production diff — ломает независимый риск-контроль.

## Статус и evidence

Решение реализовано в orchestration core и Discovery Pipeline. Проверяемые точки: `harness/context_builder/`,
`harness/orchestration/coordinator.py`, `harness/orchestration/advisory.py`, `tests/test_context_builder.py`,
`tests/test_advisory.py`, `scripts/test-clean-room` и `scripts/verify`.
