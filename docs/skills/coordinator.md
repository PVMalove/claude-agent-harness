# Coordinator — Координатор

Источник: [`harness/orchestration/playbook.md`](../../harness/orchestration/playbook.md). Для статусов и файлов доказательств также см. [`docs/skills/playbook.md`](playbook.md).

## 1. Название и локализация

- **English:** Coordinator
- **Русский (1:1):**

```text
Координатор
```

## 2. Перевод контракта роли из playbook

```text
Coordinator владеет lifecycle batch, одобрением dispatch, изменениями scope и решением принять completion report. Манифест роли является авторитетным источником для режима роли, границы записи, обязательного доказательства и risk triggers. Конфигурация проекта разрешает профиль провайдера, модель, fallback, зону, бюджет параллелизма и команды верификации; она не может ослабить контракт манифеста.

Каждый batch имеет один тикет, одну issue-ветку и один изолированный worktree. Coordinator записывает разрешённые профиль провайдера и модель в dispatch brief. Учётные данные никогда не входят в brief, конфигурацию проекта или reports.

Coordinator записывает ровно одно текущее состояние для каждого batch: `planned`, `awaiting-approval`, `active`, `completed`, `blocked` или `failed`. Роль может сообщить о прогрессе или blocker, но не может перевести собственный batch в другое состояние или молча расширить brief. `reported` — конечный исход одного dispatch роли, однако он остаётся ожидающим решения координатора; batch возвращается в `awaiting-approval` до принятия, блокировки, ошибки или создания нового dispatch.
```

## 3. Полное описание

Координатор управляет жизненным циклом одного backend-batch в `/implement`: фиксирует тикет, явный scope записи (`--allowed-path`), ветку и worktree; формирует неизменяемый brief; ждёт явного человеческого approve перед каждым dispatch; принимает или отклоняет отчёты ролей. Он не пишет продуктовый код и не выбирает провайдера или модель: это разрешается конфигурацией проекта и фиксируется в brief.

Запускается после выбора тикета или при изменении его области. Сначала проверяет блокировки, бюджет параллелизма, дубль незавершённой работы, единственного активного writer и очередь тяжёлого QA; пересечение файлов с другим batch запуск не блокирует. Затем последовательно направляет Architect, write-роль, Code Review и независимый QA. Новый факт, тайм-аут, несовпадение модели, недостаток доказательств или риск вне brief завершают текущий dispatch и требуют нового решения, а не правки уже отправленного brief.

## 4. Контракты

- **Вход (Input/Brief):** тикет и критерии приёмки; явные allowed paths batch; issue-ветка и изолированный worktree; запреты; команды проверки; доступный бюджет параллелизма и явное approve человека.
- **Выход (Output/Report):** состояние batch (`completed`, `blocked` или `failed`), журнал решений и принятые completion reports. Для завершения — все обязательные доказательства, SHA write-ролей и следующий безопасный шаг.

## Integration accounting после publish

Завершённый batch не переоткрывается и не переписывается. После accepted publish coordinator
фиксирует связь тикета, issue-ветки, source batch, опубликованного candidate SHA и target SHA
отдельной неизменяемой integration-записью:

- `integration prepare --ticket T --branch B [--batch ID] [--candidate-commit SHA]` выбирает
  единственный завершённый batch с accepted publish, сверяет SHA по принятому отчёту и по remote,
  берёт accepted green QA того же SHA и идемпотентно пишет запись; другой batch не подставляется,
  каждый отказ содержит remedy.
- `integration status` — наблюдение без записи и без dispatch: `current`, `stale` (integration ref
  ушёл вперёд, нужен новый check пары) или `unavailable`. Старое QA на новую пару не переносится.
- `integration refresh` — подготовка PR при сдвиге integration base: при неизменном target rebase не
  запускается, иначе собственная issue-ветка batch перебазируется на точный SHA target и публикуется
  через `--force-with-lease`. Грязный worktree, чужие коммиты на remote и protected-ветки отклоняются;
  конфликт возвращает `state: conflict` с данными resolver. Rebase пишет immutable
  `IntegrationRefreshRecord`; старое QA остаётся историей, новую пару подтверждают CI или local-QA,
  повторный review не нужен (ADR 0014).
- `integration resolve` — текстовый конфликт с integration target: чистый rebase отклоняется, иначе
  создаётся resolver batch и brief роли `conflict-resolver` (ADR 0015). Два автоматических target
  SHA, третий требует решения человека (`integration resolver-event --kind human-decision`); та же
  сессия продолжается через checkpoint и `dispatch resume --trigger human-decision`. Принятая
  резолюция идёт узким маршрутом без повторного review, но с новыми QA и CI либо local-QA пары.
- `integration local-qa --record <id> --ci-condition absent|unavailable|unusable --reason <текст>` —
  запасной полный локальный QA актуализированного candidate (#536) в изолированном checkout через
  общую очередь QA; пишет `verified` evidence только для текущей пары, провал сохраняет как finding,
  недоступность инфраструктуры — отдельный результат с явным `--retry` и лимитом.
- `integration link-evidence --kind ci|local-qa|resolver` — единственный путь привязать будущие
  результаты CI, local-QA и resolver; каждая привязка — отдельная запись со своей парой SHA и
  `verification: unverified`. Сами эти маршруты операция не запускает.

- `integration next --ticket T --branch B [--pull-request N]` — read-only шаг продолжения PR
  (ADR 0017): `unavailable`, `resolver-open`, `refresh`, `route-failure`, `human-decision`,
  `confirm-pr`, `verify` или `handoff`. Провал проверки обновлённой пары идёт тому же resolver в
  пределах бюджета (`integration resolve` принимает такой провал, `resolver.trigger:
  verification-failure`), провал исходной пары — обычному developer через новый batch того же тикета и ветки (`batch create`, затем
  обычный конвейер и `integration prepare --batch <новый batch>`); операционные сбои провалом кода
  не считаются. Результат `collect-ci` несёт подсказку `next` (`wait` либо `local-qa`).

Записи лежат в `reports/integration*` существующего каталога `reports`: схема ledger и
`ledger migrate` не меняются. Подробности — `harness/docs/backend-orchestration.md`.

## Память в Context Package

Схема v3 отдельно сохраняет утверждённые `goal` и DoD, ADR-карточки и секцию `memory`.
Goal передаётся локально через `batch create --goal`; сборка не обращается к трекеру или сети.
Пакет один раз выполняет read-only поиск по goal + DoD в уже существующем индексе главного
checkout. Указатели содержат ограниченные заголовок, статус, тип источника, путь, hash исходных
байтов, дату, superseded_by и сгенерированную причину включения. Текст источников, lessons,
findings и исходный JSON отчётов в пакет не включаются. Исторические источники требуют проверки;
completion report сохраняет статус «не подтверждено человеком».

`--no-memory` поддержан в `dispatch propose`, `dispatch create` и `context-package register`;
он фиксирует режим `bypass`. `memory.enabled=false` фиксирует `disabled`. Оба режима обходят
поиск и имеют приоритет над порогом. Без индекса пакет получает пустые указатели и
`index_missing`; сборка не создаёт и не обновляет кэш. Пока векторного backend нет,
`min_similarity > 0` даёт `vector_threshold_unavailable` до открытия SQLite. Нулевой порог
сохраняет BM25/path ranking, без нормирования BM25 в cosine score. Это gate сборки Package;
существующий standalone FTS search сохраняет прежний контракт.

Builder выбирает целые указатели в исходном порядке, не более `top_k` и не более
`min(top_k, 2)` на каждый валидированный тип источника, включая `completion_report`.
Незаполненные слоты одного типа не передаются другому. Секция памяти целиком, включая
`diagnostic`, identity и inclusion_reason, учитывается в `memory_policy.max_tokens` и в
остатке общего token/byte budget. Слишком большой указатель пропускается без расхода слота;
основной кодовый контекст и ADR-карточки сохраняются. Если не помещается даже обязательная
пустая секция v3 в memory token ceiling или общий бюджет, возвращается ограниченная ошибка
бюджета; её overhead не бесплатен.

Reuse требует совпадения pinned base/candidate, goal + DoD, режима и версии selection policy.
Появление или обновление индекса не заменяет замороженный пакет. Legacy v2 проверяется по
исходным полям и integrity hash без перезаписи и не переиспользуется как v3.
Freshness читает ограниченные исходные байты в main checkout, проверяет выбранную generation,
allowlist и eligibility проекции. Изменение, удаление, недоступность или отзыв источника делают
пакет stale; изменение только индекса этого не делает. Проверка freshness не открывает SQLite
и не выполняет search/refresh; действующий admission gate для stale package сохраняется.

## 5. Архитектурная схема

![Контракт скила: вход, работа, результат](../diagrams/previews/skill-contract-fill.workflow.png)

## Источник

[coordinator.py](../../harness/orchestration/coordinator.py)
