# Переход operational state с файлового ledger на Rust SQLite WAL store

**Статус: ACCEPTED (2026-10-20).** Эпик #784 (поглотил #783), ADR 0019, ADR 0026. До cutover
operational state ведут ADR 0003 и тексты issue. ADR снимает противоречие
между исходными критериями #774/#777/#778/#779 (развитие файлового ledger, «формат записей не
меняется», «миграция вне scope») и целевым требованием #784: Rust SQLite WAL — единственный writer
operational state после одного cutover, который выходит в релизе 2.0.0.

## Контекст системы

Сейчас operational state целиком принадлежит Python `LifecycleLedger`
(`harness/orchestration/ledger/lifecycle.py`):

- корень состояния `.harness/orchestration/state` (`core/constants.py:15`), указатель `ledger.json`
  ровно с полями `{version, generation, selected_at}` (`lifecycle.py:880-913`), версии
  `SUPPORTED_LEDGER_VERSIONS = (1, 2, 3)` (`lifecycle.py:37`); записи лежат в
  `generations/generation-<hex>/{batches,dispatches,dispatch-status,risk-assessments,qa-lane,audit,...}`;
- запись — `write_immutable` с `open("x")` (`:1230-1252`) или `replace` через `_atomic_write`
  (tmp-файл и `os.replace`, `:1339-1350`); `fsync` в ledger нет;
- audit — отдельный файл `audit/audit-<uuid4>.json` на каждое событие с собственным
  `record_sha256` (`_append_audit`, `:1660-1671`). **Цепочки хэшей нет:** порядок не задан,
  удаление файла не обнаруживается, `_validate_audit` (`:1838-1848`) проверяет только целостность
  каждой записи и вызывается из `records_root()` (`:926-946`) при каждом чтении — O(N) на команду;
- один глобальный замок: каталог `.coordinator.lock` с `owner.json` (`lock`, `:651-669`),
  flock-guard `.coordinator.lock.release` берётся только при снятии (`_guard_byte`, `:148-163`);
  вердикты `owner-alive`, `owner-dead`, `owner-in-other-environment`, `owner-unknown-stale`,
  `owner-unknown-recent`, `owner-on-another-host` (`ledger_admin.py:130-166`), таймаут
  `LEDGER_LOCK_STALE_SECONDS = 3_600` (`core/constants.py:196`);
- QA-lane — `qa-lane/lease.json`, очередь `qa-lane/queue/<seq>-<id>.json` (`qa_lane.py:181-198`);
  #780 (только в `integration/orchestration-ledger`, 7cef1d7) добавил ожидание замка до 30 с при
  откате и правило «снимается только lease, совпадающий со взятым»;
- лимит параллельности в ledger не хранится: `concurrency_budget` — только поле контракта
  (`contract.py:43`);
- CLI coordinator возвращает 0 и JSON в stdout при успехе, 2 и `ERROR:`/`REMEDY:` в stderr для
  любой `HarnessError` (`harness/errors.py:33-39`, `coordinator.py:211-236`), 1 — для
  непредвиденного исключения; `ledger_busy` — поле успешного ответа `dispatch status`
  (`workflow/delivery.py:565-575`), а не код ошибки.

В репозитории нет ни Rust-кода (`Cargo.toml`, `native/`), ни SQLite в orchestration (единственный
`sqlite3` — `scripts/memory_vector_worker.py`).

## Действующий контракт

### 1. Эпохи authority и единственный writer

Operational state живёт ровно в одной эпохе:

| Эпоха | Writer | Читатели | Признак |
|---|---|---|---|
| `file-ledger` (сейчас → cutover) | Python `LifecycleLedger` установки 1.x | Python 1.x | `ledger.json.version ∈ {1,2,3}` |
| `runtime-store` (после cutover) | Rust runtime (`native/harness-runtime`, crate `state`) | Python только через IPC `runtime_client.py` | `ledger.json.version = 4` + `state/AUTHORITY.json` |

Одновременной записи в оба хранилища нет ни в какой момент, включая миграцию и откат. Python
никогда не открывает `runtime.db` на запись; Rust не пишет в `generations/` кроме явной операции
отката (раздел 5), выполняемой под legacy-замком при остановленном store.

`version = 4` — fence против старых установок harness: их `LifecycleLedger` отвергает указатель
с версией вне `(1, 2, 3)` и завершается с кодом 2, поэтому устаревший процесс не может стать
вторым writer-ом.

Релиз 2.0.0 работает только в эпохе `runtime-store`: Python-writer файлового ledger из него
удалён, а operational state читается и пишется только через `runtime_client`. При
`version ∈ {1,2,3}` coordinator 2.0.0 завершается с кодом 2 и remedy `harness-runtime migrate`.
Файловый ledger в 2.0.0 нужен только как источник импорта и формат отката; в обоих случаях его
читает и пишет Rust (раздел 5).

### 2. Разделение ответственности

- **Python** (policy-слой, остаётся после cutover): семантика переходов batch/dispatch, approval,
  risk, rewind, решение о публикации, `runtime_access` (resolve/verify/pin плана), отчёты, CLI.
  Python формирует переход и передаёт его Rust вместе с `expected_revision`.
- **Rust** (единственный writer store): схема, транзакции, CAS по `revision`, fencing-токены
  lease, привязка операции к `operation_id`, проверка approval-digest против сохранённой записи,
  структурные инварианты (CHECK-ограничения), audit-цепочка, процессы и OCI. Rust не решает,
  допустим ли переход по policy, и не принимает approval/publish-решений.
- Отказ Rust IPC никогда не приводит к записи в обход: `runtime_client` возвращает
  `HarnessError` (код выхода 2) с remedy; fallback-writer на Python запрещён.

### 3. Транзакции, конкурентность и порядок блокировок (закрывает требование ADR из #779)

- SQLite в режиме WAL, `synchronous=FULL`, `foreign_keys=ON`, один процесс-writer (Rust runtime),
  `busy_timeout` ≤ 5 с. Транзакция — `BEGIN IMMEDIATE … COMMIT` длительностью в миллисекунды;
  внутри транзакции запрещены subprocess, сеть, ожидание контейнера, проверка gate и полная
  проверка audit.
- Каждая строка batch, dispatch и lease несёт `revision INTEGER`. Запись принимает
  `expected_revision`; несовпадение — типизированная ошибка `revision_conflict` (retryable: Python
  перечитывает и заново применяет policy). Записи в разные batch не конфликтуют логически, а
  физическая сериализация SQLite короче любой команды CLI.
- Одна транзакция затрагивает не больше одного batch и, при необходимости, singleton-строки
  в фиксированном порядке: `batch → qa_lane → generation`. Логические замки между транзакциями не
  удерживаются, поэтому взаимная блокировка невозможна; правило проверяется тестом, который
  запускает обе стороны в противоположном порядке.
- Чтение (`dispatch status`, `batch auto-report`, `batch decision-packet`, `qa status`) — снимок
  WAL без writer-блокировки; читатель никогда не видит половину перехода.
- QA-lane — singleton-строка `qa_lane(id=1, owner_kind, owner_id, fencing_token, expires_at)`.
  Допуск, постановка в очередь и снятие — по одной транзакции; `fencing_token` монотонно растёт.
  Снятие и откат требуют совпадения `(owner, fencing_token)`, что обобщает правило #780: lease
  другого владельца или нового запуска того же dispatch не снимается.
- Версия схемы store — `PRAGMA user_version`. `serve` при старте под эксклюзивной блокировкой
  применяет только прямые миграции схемы, каждую одной транзакцией с audit-записью
  `schema-migrate`. Runtime, который встречает версию новее своей, завершается с кодом 2 и не
  пишет. Понижение схемы не поддерживается: путь назад — только `rollback --to-legacy`.
- Ошибка ожидания: `store_busy` (retryable, после `busy_timeout`) отличается от
  `revision_conflict` и от `runtime_unavailable`; все три отображаются в код выхода 2 с разными
  `REMEDY`.

### 4. Audit и целостность

- Новый audit — append-only таблица `audit(seq INTEGER PRIMARY KEY, at, action, details_json,
  record_sha256, prev_chain_sha256, chain_sha256)`, где `chain_sha256 = sha256(prev_chain_sha256 ||
  record_sha256)`. Удаление, вставка и перестановка записи ломают цепочку.
- Инкрементальная проверка (замена кэша #778 в эпохе store): Rust хранит
  `audit_checkpoint(seq, chain_sha256)` и при обычной команде проверяет только записи после
  checkpoint. `ledger validate --full` пересчитывает цепочку от genesis и сверяет её с якорем
  импорта (ниже). Кэш не является источником истины: удалённый checkpoint означает полную проверку.
- **Legacy audit не переписывается.** Миграция импортирует каждый файл `audit/*.json` и
  `lock-audit/*.json` побайтно в таблицу `legacy_audit(path, bytes, sha256, record_sha256_valid)`
  и строит `import_manifest` — отсортированный по пути список `(path, sha256)` с итоговым
  `manifest_sha256`. Первая запись новой цепочки — `legacy-import` с этим `manifest_sha256`;
  манифест также сохраняется файлом рядом со снимком (раздел 5). Так история становится
  проверяемой на полноту с момента импорта, хотя до импорта цепочки не было; удаление legacy-файла
  до миграции этим не обнаруживается, что фиксируется как известное ограничение.
- Модель угроз совпадает с текущей: обнаруживается частичная или случайная подмена записей;
  противник с правом записи во весь state-каталог и в снимок может переписать всё — это вне scope,
  как и сейчас.

### 5. Миграция, cutover и откат

Команда `harness-runtime migrate --repo <repo>` выполняет cutover одной операцией:

1. **Предусловия** (нарушение — код 2, ничего не изменено): нет живого QA-lease и записей
   очереди QA; нет dispatch в `sent`/`working` с живым worker-ом; `ledger validate` проходит;
   Rust store отсутствует или в состоянии `importing` от прерванной попытки.
2. **Захват legacy-замка** по существующему протоколу (`owner.json` с `operation:
   runtime-cutover`) плюс удержание flock на файле внутри `.coordinator.lock/` на всё время
   операции (критерии #777, реализация в Rust-мигратор). Живой
   владелец не снимается; мёртвый снимается только по вердикту `owner-dead` с записью в
   `lock-audit`.
3. **Снимок**: копия state-каталога в `.harness/orchestration/state-snapshots/<utc>/` и
   `snapshot-manifest.json` (sha256 каждого файла), `fsync` файлов и каталога.
4. **Импорт** в `state/runtime.db.importing`: выбранное поколение → таблицы; остальные
   поколения → `legacy_generation` только для чтения; legacy audit → раздел 4. Исторически
   невалидные состояния чинит детерминированный repair с audit-записью `migration-repair`
   (`before_sha256`, `after_sha256`, правило). Первое правило — #781: batch с
   `risk_reassessment_required = true` без `risk_reassessment_candidate` получает
   `risk_reassessment_pending_next = true`.
5. **Сверка паритета** выполняется в Rust, без запуска `serve` и Python: для каждой записи
   выбранного поколения мигратор сравнивает sha256 канонического JSON legacy-записи и проекции
   импортированных строк обратно в форму записи. Расхождение вне полей из repair-записей — код 4.
   Паритет ответов Python CLI (`dispatch status`, `batch auto-report`, `batch decision-packet`,
   `qa status`) проверяется не здесь, а golden-тестом в CI: фикстура state-каталога 1.x даёт те же
   ответы CLI 1.x до импорта и CLI 2.0.0 после импорта.
6. **Точка фиксации**: `rename(runtime.db.importing → runtime.db)`, `fsync` каталога, запись
   `state/AUTHORITY.json` (`authority`, `store_id`, `manifest_sha256`, `snapshot`, `cutover_at`),
   затем замена `ledger.json` на `version: 4` через `os.replace`. Запись указателя — commit point.
7. Снятие legacy-замка.

**Идемпотентность и сбои.** Сбой до шага 6 оставляет authority у файлового ledger; повторный
`migrate` удаляет `runtime.db.importing` и начинает заново. Повтор после шага 6 при совпадающем
`manifest_sha256` возвращает 0 и `already_cut_over: true`. Если legacy-каталог изменился после
снимка (нарушение замка), `migrate` останавливается с кодом 4 и не фиксирует cutover.

Коды выхода `harness-runtime migrate|rollback`: 0 — выполнено или уже выполнено; 2 — отказ по
предусловию, состояние не изменено; 3 — прервано после начала, частичный store отброшен,
authority прежняя; 4 — нарушение инварианта, требуется оператор (legacy изменён во время
операции, расхождение паритета).

**Откат** (`harness-runtime rollback --to-legacy`, human approval с `--approved-by`,
`--approved-at`, `--reason`, как у `release-lock --force`; `policy:`-approver отклоняется):

- требует остановленных исполнений и пустой QA-lane в store;
- Rust экспортирует store в **новое** legacy-поколение `generation-<hex>` формата v3; история
  исполнения, которой нет в формате v3 (receipts, fencing), уходит неизменяемым архивом
  `state/runtime-export/<store_id>.tar` с sha256 в audit-записи `runtime-rollback`;
- Rust пишет поколение и проверяет его тем же правилом паритета, что и импорт (шаг 5), затем
  возвращает указатель на `version: 3` и новое поколение; `AUTHORITY.json` получает
  `authority: file-ledger` и `rolled_back_from`; `runtime.db` переводится в read-only и
  сохраняется для разбора. Второго writer-а формата v3 в 2.0.0 нет: Python-writer удалён
  (раздел 1);
- после отката coordinator 2.0.0 отказывает с кодом 2, и оператор возвращает последний релиз 1.x.
  Его `ledger validate` по новому поколению — обязательный шаг процедуры отката; провал означает
  восстановление снимка (ниже);
- аварийный откат восстановлением снимка теряет состояние после cutover, поэтому требует
  отдельного approval владельца и audit-записи `snapshot-restore`.

Порядок шагов `rollback --to-legacy` (нарушение на шагах 1–5 — код 2, state-каталог и `runtime.db`
не изменены):

1. **Approval**: `--approved-by`, `--approved-at` и `--reason` не пустые (иначе
   `malformed_request`); approver с префиксом `policy:` отклоняется (`policy_denied`);
   `--approved-at` разбирается как время RFC 3339 и не лежит в будущем.
2. **Замок экземпляра `serve`** (`ipc::hold_instance_lock`, тот же `take_lock`, что у `serve`):
   занятый замок — код 2, сначала нужно остановить `serve`.
3. **Legacy-замок** по протоколу шага 2 миграции, `owner.json` с `operation: runtime-rollback`.
4. **Схема store**: `schema::upgrade`; `user_version` новее runtime — код 2 без записи. Откат
   никогда не понижает `user_version`.
5. **Предусловия в одном снимке чтения**: нет строки в `qa_lane` и `qa_queue`, нет исполнения в
   `launching`, `running`, `stopping` или `unknown` (`lease_held`). Dispatch в `dispatched` или
   `working` с остановленным исполнением или без него экспортируется как есть: тест DoD требует
   поколение с batch в каждом `DispatchState`.
6. **Архив и поколение**: детерминированный ustar `state/runtime-export/<store_id>.tar`
   (участники по алфавиту, mtime 0, uid/gid 0, mode 0444; `store_meta.json`, `audit.jsonl` — вся
   цепочка store, `operations.jsonl`, `revisions.jsonl` (`path`, `revision`, `execution_state`),
   `qa_lane.json` при наличии строки, `MANIFEST.json` с sha256 каждого участника). Файл пишется под
   временным именем, сбрасывается на диск, переименовывается и становится read-only. Файл с
   другими байтами, который оставила незавершённая попытка, заменяется (см. «Повторы»). Затем
   новое `generation-<hex>` и его сверка `check_export` по правилу шага 5 миграции; единственный
   лишний файл — v3 audit-запись `runtime-rollback`.
7. **Audit-запись store** `runtime-rollback` (`generation`, `export`, `export_sha256`,
   `approved_by`, `approved_at`, `reason`), checkpoint WAL, `runtime.db` переводится в read-only и
   не удаляется.
8. **`AUTHORITY.json`**: `authority: file-ledger`, `rolled_back_from: <store_id>`, `generation`,
   `export`, `export_sha256`, `rolled_back_at` и поля cutover.
9. **`ledger.json`** заменяется на `{version: 3, generation: <новое>, selected_at}` — commit point.

**Повторы.** Повтор после шага 9 возвращает код 0 и `already_rolled_back`. Повтор, который застал
шаг 8 выполненным, а указатель ещё на `version: 4`, выполняет только шаг 9. Повтор, который застал
audit-запись шага 7, продолжает с шага 8 по поколению и архиву из этой записи. Сбой до шага 7
удаляет записанное поколение, и authority остаётся у store. Архив, который незавершённая попытка
установила до commit point, не назван ни одной audit-записью `runtime-rollback`: это отброшенное
состояние, и store мог измениться после сбоя. Повтор заменяет такой архив архивом текущего store и
не завершается кодом 4. Архив, который назван audit-записью шага 7, в том числе архив
зафиксированного отката, неизменяем: повтор только сверяет его sha256 с записью, а расхождение —
код 4.

### 6. Поставка и работа до cutover

Integration-ветки поставляются цепочкой: дочерние PR → `integration/orchestration-ledger` и
`integration/container-executor` → integration-ветка Harness Next
`integration/harness-runtime-reliability` → `master` одним релизом **2.0.0** (major: указатель
`version: 4`, обязательный Rust runtime). Ни одна из этих integration-веток не попадает в `master`
раньше Harness Next. Поэтому изменения файлового ledger из `integration/orchestration-ledger` дойдут
до пользователей только вместе со store, который их заменяет, и в 2.0.0 файловый ledger служит лишь
источником миграции и форматом отката. Rust-компонент не получает временной Python-реализации, а
Python-доработки файлового ledger, которые store заменяет, не реализуются:

| Issue | Решение |
|---|---|
| #776 / PR #780 | Уже в `integration/orchestration-ledger`; правило «снимается только свой lease» переходит в fencing QA-lane (раздел 3). |
| #777 | **Superseded-by-Harness-Next** как Python-реализация. Критерии переходят на store и миграцию: мёртвый владелец lease снимается без оператора по идентичности процесса (раздел 7), живой — никогда, каждое автоматическое снятие попадает в audit; legacy-замок, оставленный процессом 1.x, при `migrate` получает прежние вердикты (`ledger_admin.py:130-166`). |
| #778 | **Superseded-by-Harness-Next** как Python-кэш. Критерии переходят на store: инкрементальная проверка по checkpoint audit-цепочки, `validate --full` без кэша, удалённый checkpoint → полная проверка, замер до и после (раздел 4). |
| #779 | **Superseded-by-Harness-Next**: замки на batch в файловом ledger — новая архитектура конкурентности, которую store заменяет целиком. Критерии (параллельные batch, согласованное чтение, последовательный QA-lane, совместимость CLI, ADR порядка блокировок) переходят на store; требование ADR выполняет раздел 3. |
| #781 | Исправляется в Python `workflow/recovery.py` и `workflow/risk.py`: это policy, которая остаётся на Python после cutover, поэтому исправление не временное и может выйти в `master` до 2.0.0 (у задачи нет эпика). Store добавляет CHECK-ограничение и repair при миграции (раздел 5). |

Доработки файлового ledger и Python store из `integration/orchestration-system-fields` (#699,
#701, #703, #712, #728, #735) тоже Superseded-by-Harness-Next; разбор по коммитам — ADR 0026,
раздел 1.

**Обновление до 2.0.0.** `harness update` до 2.0.0 сначала проверяет предусловия `migrate`
(шаг 1) и при отказе не устанавливает 2.0.0. Затем он устанавливает файлы и запускает
`harness-runtime migrate`. Если migrate прервался (код 3 или 4), authority остаётся у файлового
ledger, coordinator 2.0.0 отказывает с кодом 2, а remedy — повтор `migrate` или возврат на 1.x.

Если до 2.0.0 зависшие замки файлового ledger станут недопустимыми для работы, исправление
выпускается отдельным hotfix-тикетом от `master` (1.x), а не через `integration/orchestration-ledger`.
До тех пор действуют `ledger lock-status` и `release-lock --force` (#759).

### 7. Идентичность процесса и lease

Lease и execution receipt в store хранят `boot_id`, inode PID-namespace, `pid`, время старта
процесса (`/proc/<pid>/stat`, поле 22), а для контейнера — id и метки
`harness.dispatch`/`harness.repository`/`harness.plan_digest`. Один PID никогда не служит
доказательством; процесс из другого namespace или boot без доказательства смерти не снимается
(как вердикт `owner-in-other-environment`). Повтор операции с тем же `operation_id` после
потерянного ответа возвращает сохранённый receipt и не запускает исполнение повторно.

## Операционные последствия

- **Approval.** Native approval, immutable brief, `plan_digest`, `runtime_images` и
  `runtime_access_sha256` сохраняют смысл; миграция переносит их побайтно, а Rust сверяет digest
  по сохранённой записи, а не по флагу запроса. Агент не может выполнить `migrate`/`rollback`:
  обе команды требуют human approval; `policy:`-approver отклоняется.
- **CLI.** Команды, флаги, JSON-ответы и коды выхода 0/2 coordinator остаются прежними в обеих
  эпохах; `ledger_busy` в `dispatch status` после cutover означает `store_busy`.
- **Негативные тесты (обязательны до cutover):** SIGKILL `migrate` на каждом шаге 1–7 с
  последующим повтором; legacy-запись во время импорта → код 4; старая установка harness
  (версии 1–3) после cutover → код 2 без записи; Python-запись при `version = 4` в обход
  `runtime_client` → отказ; подмена, удаление и перестановка строки audit с восстановленным
  содержимым → `validate --full` падает; удалённый checkpoint → полная проверка без ошибки;
  повтор `operation_id` после потерянного ответа → тот же receipt; два coordinator пишут в разные
  batch одновременно → оба коммита; конфликт `expected_revision` в одном batch → ровно один
  победитель и `revision_conflict` у второго; снятие QA-lease с чужим `fencing_token` → отказ;
  PID reuse и другой PID-namespace → lease не снимается; откат с живым исполнением → код 2;
  batch #781 с `required=true` без кандидата → repair и последующий `risk assess` снимает флаг;
  поколение v3, записанное откатом, проходит `ledger validate` последнего релиза 1.x на фикстурах
  с batch в каждом `DispatchState`; store с `user_version` новее runtime → код 2 без записи.
- **Race-сценарии:** Python-процесс, прочитавший `version = 3` до cutover, после захвата
  legacy-замка перечитывает указатель и отказывает при `version = 4`; два `migrate` одновременно —
  второй получает занятый замок (код 2); `rollback` во время QA-допуска — сериализуются
  транзакцией singleton-строки.
- **Команды проверки:** `make verify` (`qa_gate_commands`); точечно
  `.harness/.venv/bin/python -m pytest -q tests/orchestration/test_ledger.py
  tests/orchestration/test_ledger_lock_environment.py tests/orchestration/test_qa_lane.py
  tests/orchestration/test_recovery.py`; для Rust — `cargo fmt --check`,
  `cargo clippy --all-targets -- -D warnings`, `cargo test` в `native/harness-runtime`;
  rehearsal — `harness-runtime migrate` на копии state-каталога этого репозитория с замером
  `dispatch status`/`dispatch heartbeat` p50/p95/p99 до и после.
- **Решения владельца (2026-10-20):** канонический эпик — #784, #783 закрыт как его дубликат;
  integration-ветка Harness Next — существующая `integration/harness-runtime-reliability`
  (`integration/harness-next-runtime` не создаётся); `integration/container-executor` и
  `integration/orchestration-ledger` сначала вливаются в неё, затем всё выходит в `master` как
  2.0.0, поэтому Python-исполнитель #704–#708 в `master` отдельно не попадает, а его граница движка
  (`container_executor.run_engine`) переходит к Rust до 2.0.0; #709, #777, #778 и #779 стоят в
  `status::blocked` до появления тикетов Rust-фундамента и store.
- **Откат на 1.x:** `rollback --to-legacy` экспортирует состояние в формат v3, поэтому после него
  state-каталог снова читает установка 1.x; её `ledger validate` завершает откат. Перед откатом
  оператор останавливает `serve` (иначе код 2 на замке экземпляра) и дожидается пустой QA-lane;
  архив `runtime-export/<store_id>.tar` и read-only `runtime.db` остаются для разбора, а sha256
  архива записан в audit-записи `runtime-rollback` и в `AUTHORITY.json`.
- **Golden-тест паритета CLI** (обязателен до cutover, `tests/orchestration/test_cli_parity_1x.py`
  в job `verify`): state-каталог пишет код релиза 1.5.1 (коммит `3e4dee33`, извлекается из истории
  Git). Решение владельца (2026-10-27) об объёме фикстур: фикстура содержит состояния, которые
  допускает шаг 1 `migrate`. Dispatch в `dispatched` или `working`, QA-lease и записи очереди QA
  шаг 1 отклоняет: их покрывают негативные тесты `migrate`, а batch в каждом `DispatchState`
  покрывает тест отката (`ledger validate` 1.5.1 по поколению отката).
- **Правило сравнения golden-теста** (решение владельца, 2026-10-27): тест сравнивает весь ответ
  CLI 1.5.1 до импорта с ответом CLI 2.0.0 после импорта в каноническом JSON. Исключаются только
  поля, которые audit-запись `migration-repair` называет для своей записи (правило → поля
  сверяется с `repair.rs`), и закрытый список полей, добавленных в CLI после 1.5.1; они удаляются
  только из ответа 2.0.0:
  - `dispatch status`: `dispatches[*].adapter_receipt`, `dispatches[*].worker`,
    `dispatches[*].startup.deadline_exceeded`, `dispatches[*].startup.failure`,
    `dispatches[*].startup.observation`, `dispatches[*].startup.startup_elapsed_seconds`,
    `dispatches[*].startup.startup_timeout_seconds`;
  - `batch decision-packet`: `route_preview.retry.failure`.

  Всего в списке восемь полей. Значение `silent_seconds` есть в обоих ответах и зависит от
  часов, поэтому маскируется, а ключ сравнивается. Адрес отчёта в `batch decision-packet`
  сравнивается по имени файла.
  Любое другое добавленное, отсутствующее или изменённое поле — провал теста; расширение списка —
  новое решение владельца.
