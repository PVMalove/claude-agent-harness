# Conflict resolver для текстового конфликта с integration target

## Контекст системы

Если integration ref ушёл вперёд и issue-ветка уже не перебазируется чисто, `integration refresh`
(ADR 0014) возвращает `state: conflict` с данными resolver-а и ничего не меняет. Раньше такой
конфликт возвращался обычному developer-у по маршруту rebase из ADR 0012: новая сессия, трата
`retry_policy.max_developer_retries` и перезапись требований, которые уже были согласованы двумя
тикетами. Нужна отдельная роль, которая разрешает конфликт, не теряя требований ни одной стороны, не
теряет сессию при вопросе к человеку и не останавливает чужие batch.

## Действующий контракт

- **Роль.** `conflict-resolver` — write-роль (`required_capabilities: conflict-resolution`). Она
  открывает skill `resolving-merge-conflicts`, сохраняет требования обеих сторон, не добавляет
  функциональность вне них, никогда не делает `--abort` и force-push и пишет только в issue-ветку
  своего batch.
- **Маршрут.** `integration resolve --record <id>` (или `--ticket` и `--branch`) ничего не пишет в
  Git. Чистый rebase отклоняется (его делает `integration refresh`); конфликт превращается в новый
  batch вида `resolver` с `next_action: resolve-conflict`. Завершённый batch тикета, его brief и
  отчёты не переписываются и не открываются заново. Дальше идёт обычный путь: `batch approve` и
  `dispatch create --role conflict-resolver` с `--propose` и подтверждением digest.
- **Brief.** Неизменяемая секция `resolver`: тикет; `sides.candidate` (DoD исходного batch) и
  `sides.target` (plan-записи тикетов из `(#N)` в subject коммитов между merge-base и target, иначе
  subject и тело коммита); SHA candidate и target; scope (конфликтные файлы и файлы обеих сторон);
  запреты; план коммита; проверки проекта; остаток бюджета; `report_staging_path`. Источник требований
  target эвристичен: он опирается на `(#N)` в subject, и без plan-записи тикета берётся текст коммита.
- **Бюджет.** Два автоматических target SHA; третий требует решения человека с `--extends-budget`.
  Цикл тратит только зафиксированный отчёт resolver-а по новому target SHA. Чистый rebase, ответ
  человека и правка на том же target цикл не тратят; правки на одном target ограничены
  `retry_policy.max_developer_retries` (новых полей конфигурации нет). Счётчик не хранится: он
  выводится из append-only событий `reports/resolver-events/` (`cycle-spent`, `same-target-fix`,
  `human-decision`, `scope-change`, `exhausted`), поэтому потеря сессии, resume и
  повторный запуск его не сбрасывают.
- **Несовместимые требования.** Resolver не угадывает: он пишет checkpoint (`blockers` — конкретное
  описание и варианты) и завершает сессию. Ответ человека — отдельное событие `human-decision`
  (кто, когда, вариант, расширение бюджета), которое записывается до
  `dispatch resume --trigger human-decision`. Тот же dispatch продолжается в новой сессии; новый
  developer не создаётся, соседние batch не затрагиваются. Изменение scope — обычное approval нового
  dispatch: исходный brief не переписывается, resume с изменившимися фактами отклоняется существующей
  проверкой drift. Потерянная runtime-сессия возобновляется через существующие checkpoint/resume или
  `batch resume`.
- **Исчерпание.** Событие `exhausted` блокирует только этот resolver batch; ветка и evidence
  сохраняются. Причина решает маршрут: несовместимость интеграции продолжает тот же resolver после
  решения человека, собственный дефект тикета (`resolver.cause: task-defect`) возвращается обычному
  developer-у. Retry resolver-отчёта без `task-defect` — маршрут `same-candidate-rerun` с
  `next_action: resolve-conflict`; новых значений в `RECOVERY_ROUTES` нет.
- **Отчёт.** Верхнеуровневое необязательное поле `resolver` (его проверяет `report submit` и
  `batch decide`): `preserved_requirements` по обеим сторонам, `human_decisions` (id событий
  этого dispatch), `target_sha`, `resolved_candidate_sha`, `cause`, точные `changed_files` и `commits` с
  записью плана. Policy auto-accept resolver-отчёт не принимает: резолюцию меняет код, который target
  не ревьюил.
- **Узкий маршрут.** После принятого отчёта запись интеграции получает refresh-запись с
  `resolver: {invoked: true}`, а risk assessment batch вида `resolver` не требует повторного review.
  QA нового candidate и CI либо local-QA новой пары обязательны: `integration status` держит
  `verification.required`, а `resolver`-evidence его не закрывает.

## Операционные последствия

- Ledger schema и `ledger migrate` не меняются: записи лежат в `reports/resolver/` и
  `reports/resolver-events/`.
- `PLANNED_TRIGGER_KINDS` получает `human-decision` без измеренного значения; остальное продолжение
  использует существующий контракт checkpoint и resume.
- Роль нельзя назначить вручную: единственный путь к ней — `integration resolve`.
- Решение трудно откатывать частично: событийная модель бюджета и `next_action: resolve-conflict`
  связаны, поэтому изменение любого из них требует нового ADR.
