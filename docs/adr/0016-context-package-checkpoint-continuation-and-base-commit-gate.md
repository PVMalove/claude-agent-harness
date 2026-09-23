# Ограничение повторного discovery и разрастания TDD-истории: Context Package, checkpoint и base-commit gate

## Контекст системы

Каждая роль (architect, developer, code-review) дублирует discovery заново, потому что между ними
нет переиспользуемого артефакта: architect читает 24+ файла, developer и reviewer частично повторяют
то же самое. Developer растягивает весь TDD-цикл (например, 11 red-green-refactor commit) в одну
worker session, из-за чего traceback и логи неудачных попыток накапливаются в истории линейно.
`base_commit` batch фиксируется как локальный HEAD без сверки с текущим `origin/<integration_ref>`
(`coordinator.py:993`), из-за чего устаревшая база один раз утроила diff ревью (17→47 файлов, тикет
#371) и заставила reviewer повторно обрабатывать посторонний шум. Эти три независимых источника
вместе объясняют, почему 91% backend-сессий превышают 150k токенов контекста.

## Действующий контракт

Вводится новое разделение: **dispatch** остаётся coordinator-owned единицей approval, как и раньше,
но теперь может пережить несколько **worker session** — только для write-ролей с итеративным TDD
(`developer`, `database-migrations`, `messaging-integration`). Каждая worker session обязана заново
пройти model self-report и heartbeat; ни один существующий terminal outcome (`completed`/`blocked`/
`failed`) не меняется — переход между session фиксируется как non-terminal **checkpoint** (commit
SHA, изменённые файлы, оставшийся DoD, пройденные проверки, краткие риски/блокеры, ссылка на Context
Package), а не completion report. Read-only роли (`architect`, `qa`, `code-review`) никогда не
растягивают dispatch на несколько worker session.

**Context Package** становится новым ledger-owned immutable артефактом (по образцу существующего
`risk_assessment`), детерминированно построенным отдельным sibling-модулем (в духе `gate_runner.py`,
без участия LLM) до dispatch. Содержит: цель и DoD, integration base и candidate SHA, точный diff,
5–10 стартовых файлов с причиной включения, ограниченный граф символов/зависимостей, связанные
тесты, карточки релевантных ADR/прецедентов, hash каждого включённого файла. `coordinator.py`
проверяет его свежесть перед каждым новым dispatch (push-модель) и отказывает в создании brief, если
пакет устарел; роль, столкнувшись с недостаточным пакетом, эскалирует как blocker вместо чтения
репозитория вслепую.

**Base-commit gate**: `batch create` обязан выполнить `git fetch origin <integration_ref>` перед
фиксацией `base_commit` (сегодня фиксируется локальный HEAD без fetch). Дополнительная обязательная
проверка свежести базы вставляется непосредственно перед созданием review/publish dispatch, а не на
каждом developer dispatch approval. Устаревшая база блокирует не batch целиком, а конкретно
review/publish, и разрешается только через формальный developer dispatch (rebase, остаётся внутри
write-role границы) — его результат — новый `candidate_commit`, обязанный заново пройти risk
assessment по уже действующему правилу.

**Continuation** (переход между worker session внутри одного dispatch) авторизуется по-разному в
зависимости от причины: 429 — coordinator автоматически перезапускает из checkpoint без нового
human-решения (recovery, не решение об эффективности); плановая compaction (лимит контекста, N
TDD-циклов, большой failure log, завершённый вертикальный срез) — через уже существующий в
`playbook.md` механизм `coordinator decision`, без новой ledger-схемы для самого факта авторизации.
Continuation никогда не меняет scope, DoD, risks или dependencies; при изменении — текущий dispatch
закрывается, новый открывается через обычный approval.

Delta-review после Warning разрешён только для test-only diff, закрывающего конкретный finding, и
всегда идёт как новый независимый dispatch (Context Package и отчёт review №1 передаются как
evidence, но worker session первого review никогда не переиспользуется) — независимость review как
адверсариальной проверки не приносится в жертву экономии токенов.

Дешёвая advisory-модель (ранжирование файлов, сводка логов, риск-классификация) остаётся non-role
deterministic tool call вне brief/report-контракта: её вывод эфемерен, не версионируется, не входит
в Context Package и не может сам разрешить dispatch, понизить риск, принять QA или изменить scope.

## Операционные последствия

Появление worker session как понятия, отдельного от dispatch, и новая ledger-схема для checkpoint и
Context Package — это lifecycle-изменение и требует migration/evidence tests ledger по правилу
ADR-0015. Context Package обязан существовать раньше checkpoint/continuation: checkpoint ссылается
на него по id и hash. Классификация 429 против переполнения контекста зависит от ещё не
спроектированного runtime/provider-сигнала — гибридная continuation-политика безопасна именно
потому, что не полагается на точную причину сбоя, а не потому что причина уже известна. Фактическая
выгода (cache hit rate, сокращение повторного discovery, снижение max context size) — гипотезы,
проверяемые новой per-worker-session телеметрией (cache read/write tokens, число TDD-циклов, причина
compaction, доля файлов вне task scope), а не следствие самого факта внедрения.

Внедрение идёт двумя волнами. Немедленно, без пилота: base-commit gate (исправляет доказанный
дефект, чисто детерминированная проверка) и delta-review restriction (только сужает разрешённое,
откат — уже действующее поведение). Только после пилота: Context Package (первая фаза — shadow-режим
без ограничения ролей) и checkpoint/continuation (пилот на малом числе long-TDD batch, сравнение
post-integration defect rate между continuation- и single-session-batch), в указанном порядке.

## Рассмотренные и отклонённые варианты

- **Каждый checkpoint закрывает dispatch и открывает новый** (без понятия worker session). Отклонено:
  каждый плановый checkpoint снова требовал бы полного нового approval-цикла, что возвращает часть
  human-latency, ради устранения которой предложен механизм.
- **Context Package как внешний артефакт вне ledger**, проверяемый отдельным CLI без lifecycle.
  Отклонено: не согласуется с узкой ownership-границей ADR-0015 — ledger владеет versioned lifecycle
  и immutable audit, а не отдельный внешний модуль.
- **`orca_adapter` получает ответственность за компоновку provider-native prompt** (переупорядочивание
  контента под кэш-стратегию провайдера). Отклонено: расширяет заявленную ADR-0015 узкую границу
  adapter ("только переводит already-approved brief, не определяет форму").
- **Rebase при устаревшем base выполняет coordinator/человек напрямую через git CLI**, минуя dispatch.
  Отклонено: создаёт исключение из инварианта "write role" — production-ветку меняет только
  write-role dispatch.
- **Delta-review переиспользует ту же worker session reviewer-а.** Отклонено: конфликтует с духом
  независимого review — тот же "человек" менее вероятно оспорит собственный прошлый вердикт.
- **Checkpoint/continuation обобщается на все роли, включая read-only.** Отклонено: текущего
  конкретного кейса для read-only роли нет (delta-review уже решён как всегда новый dispatch), а
  обобщение создало бы неиспользуемую сложность инварианта.
- **Отдельная новая ledger-запись "continuation approval"** вместо переиспользования "coordinator
  decision". Отклонено: continuation-триггер — это ровно тот "новый факт после dispatch", который
  playbook.md уже описывает; отдельная схема добавила бы параллельную сущность без новой семантики.

## Обновление (issue #274): граф символов и parser_provenance из Repo Map

`build_context_package` больше не строит граф импортов и не извлекает Python-сигнатуры сам (удалены
regex-based import graph и AST-based signature extraction); он получает их из Repo Map CLI —
sibling-модуля `harness/repo_map/repo_map.py`, — вызывая его подпроцессом через `sys.executable` и
валидируя типизированный JSON-контракт (`harness/repo_map/repo_map.schema.json`). Через границу
процесса пересекает только JSON; tree-sitter-типы никогда её не пересекают.

`allow_paths`/`deny_paths`/`redact_paths` — это решения на уровне целого пути, которые Repo Map уже
закладывает в список `files` этого JSON (запрещённый или redact-путь просто никогда там не
появляется), поэтому любое другое сырое чтение через `git diff`/`git show`, которое делает сам
`build_context_package` — diff и fallback-excerpt зависимости — дополнительно ограничено тем же
срезом `files`, а не полным набором изменённых файлов (issue #274, review-finding: `package.diff`
изначально строился нефильтрованным `git diff` по всем изменённым файлам, независимо от `files`).

`symbol_graph["<path>"]["imports"]`/`["imported_by"]` заполняются только рёбрами `kind == "import"`;
рёбра `unique-name-ref`/`ambiguous-name-ref` образуют новые аддитивные ключи `["references"]`/
`["referenced_by"]` (пустые списки, когда Repo Map работает на minimal-tier без grammar-бандла).

Context Package получил структурированный `parser_provenance` (tier, parser, degradation_reason,
token_estimator_version, plюс вложенный `parser_provenance` самого Repo Map — идентичность и версии
tree-sitter-бандла, hash скрипта, версия token-estimator) и `schema_version` (2); прежнее строковое
поле `parser` сохраняется как legacy-зеркало top-level `parser` Repo Map ("path-only"/"bundle") ещё
одну версию схемы. Отсутствие `schema_version` в уже существующей записи трактуется как версия 1.
