# Граница Rust runtime: authority, IPC и идемпотентность

**Статус: PROPOSED (2026-10-20).** Эпик #784, ADR 0019, ADR 0020. Документ фиксирует, кто владеет
каждой сущностью, как Python обращается к Rust runtime и почему повтор после потерянного ответа не
исполняет операцию дважды. Хранилище, миграцию и audit описывает ADR 0020, процессы и OCI — ADR
0022, проверки — ADR 0023, профили hooks — ADR 0024.

## Контекст системы

По ADR 0003 ядро `harness/orchestration/` на Python владеет batch, dispatch, approval, immutable
audit, очередью QA и решением о публикации. Approval связывает переход через `transition_digest`
по полям `TRANSITION_FIELDS` (`operational_guards.py:18-32`): batch, роли, `candidate_sha`,
`base_sha`, `review_scope`, `verification_commands`, `context_package_id`, `required_gates`.
Необязательные привязки (`OPTIONAL_TRANSITION_FIELDS`, `:36-43`) и `runtime_access_sha256`
(`:45`) добавляют план доступа, recovery-событие и повтор инфраструктуры; `retry_idempotency_key`
(`:158`) уже делает повтор developer-retry идемпотентным. `allowed_tools` попадает в brief
(`workflow/dispatch.py:1282`), но в transition digest не входит.

Контейнерный исполнитель ADR 0018 живёт в Python (`container_executor.py` в
`integration/container-executor`) и вызывает движок через `subprocess.run`. Процесс coordinator
выполняет и решение, и запись, и запуск, поэтому SIGKILL между запуском и записью оставляет
контейнер без записи о нём, а повтор команды не знает, что запуск уже был.

## Действующий контракт

### 1. Матрица authority

Каждой сущностью владеет ровно один writer. Решение и запись разделены: Python принимает решение
по policy, Rust сохраняет его транзакцией и сам исполняет только то, что разрешает сохранённое
решение.

| Сущность | Решение принимает | Пишет | Читает | Проверка Rust при записи |
|---|---|---|---|---|
| Lifecycle batch (`state`, `next_action`, `recovery_events`, risk-флаги) | Python `workflow/` | Rust store | Python через IPC | CAS по `revision`, CHECK-инварианты (ADR 0020) |
| Brief dispatch и его approval | Python (`contract.py`, `approvals`, human approval) | Rust store, неизменяемо | оба | digest brief, `transition_digest`, наличие approval |
| Отчёт роли | Python (`_validate_report`) | Rust store, неизменяемо | Python | привязка к dispatch и `brief_sha256` |
| `ApprovedAccessPlan` (`runtime_access`) | Python (`resolve_plan`, `pin_images`) | Rust store в составе brief | Rust при исполнении | `plan_digest`, `runtime_images` против сохранённого brief |
| `VerificationPlan` (ADR 0023) | Python (ChangeImpact) | Rust store в составе brief | Rust при исполнении | digest плана против brief |
| Execution receipt и outcome | Rust | Rust store | Python | идентичность процесса, метки OCI |
| Lease QA-lane и lease исполнения | Python просит допуск | Rust store | оба | `fencing_token`, срок, владелец |
| Audit-цепочка | — | Rust, в той же транзакции, что и изменение | оба | `prev_chain_sha256` |
| Evidence (логи, JUnit, манифест) | — | Rust, content-addressed файлы и строки манифеста | оба | sha256 содержимого |
| Effective hook policy (ADR 0024) | Python resolver | Rust store в составе brief | Rust и hook-адаптеры | `hook_policy_sha256` |
| Git-операции и publish | Python coordinator | Git | — | вне контейнера, как в ADR 0018 |
| Проектный конфиг | человек | файлы репозитория | Python | Rust получает только digest в плане |

Rust не решает, допустим ли переход по policy, не создаёт approval и не выбирает следующую роль.
Python не пишет в store и не запускает процессы и контейнеры исполнения. Запрещённых fallback нет:
недоступный runtime даёт ошибку, а не запись в обход.

### 2. Транспорт и аутентификация

- **Linux и macOS:** Unix domain socket `$XDG_RUNTIME_DIR/harness/<repo-id>.sock`, где
  `<repo-id>` — первые 16 hex sha256 абсолютного пути общего git-каталога; без
  `XDG_RUNTIME_DIR` — `<state root>/runtime/` с проверкой длины пути (не больше 100 байт), иначе
  отказ запуска. Каталог `0700`, сокет `0600`.
- **Windows:** named pipe `\\.\pipe\harness-runtime-<repo-id>` с DACL только на SID текущего
  пользователя и `PIPE_REJECT_REMOTE_CLIENTS`.
- **Peer-проверка:** Linux — `SO_PEERCRED`, uid клиента равен uid runtime; macOS —
  `getpeereid`; Windows — `GetNamedPipeClientProcessId` и SID токена клиента. Несовпадение —
  `unauthorized_peer`, соединение закрывается.
- **Capability-токен:** при старте runtime пишет случайный 256-битный токен в
  `<socket dir>/token` (`0600`); клиент предъявляет его в `hello`. Ни сокет, ни токен, ни каталог
  runtime не монтируются в контейнеры: ADR 0022 отвергает такие пути в плане монтирования.
- **Модель угроз.** Процесс того же пользователя на хосте может прочитать токен, так же как сейчас
  может переписать файлы ledger; регресса нет. Контейнерный worker доступа к runtime не имеет — это
  усиление по сравнению с текущим состоянием. Сетевого доступа к runtime нет ни в одном режиме.

### 3. Протокол

- Кадр: длина `u32` big-endian и JSON UTF-8; максимум 16 MiB; один запрос — один ответ. Длинные
  ожидания — отдельная операция `watch` с `deadline`, без потоковых ответов.
- Рукопожатие: `hello {protocol: {min, max}, client_version, repo_root, token}` → `{protocol,
  server_version, store_id, features}`. Совместимы версии с общим major; minor добавляет только
  необязательные поля. Сервер отвергает неизвестные поля запроса (`malformed_request`); клиент
  игнорирует неизвестные поля ответа. Несовместимая версия — `protocol_unsupported` с remedy
  обновить harness или runtime.
- Конверт запроса: `protocol_version`, `operation_id`, `operation`, `deadline` (UTC, мс),
  необязательные `batch_id`, `dispatch_id`, `candidate_sha`, `plan_digest`, `expected_revision`,
  `fencing_token` и `payload`.
- Ответ: `operation_id`, `status: ok|error`, `revision`, `result` или `error {code, domain,
  retryable, message, remedy, facts}`.
- Каталог кодов: `protocol_unsupported`, `unauthorized_peer`, `malformed_request`,
  `unknown_operation`, `approval_mismatch`, `revision_conflict`, `store_busy`, `lease_held`,
  `fencing_mismatch`, `idempotency_conflict`, `not_found`, `invariant_violation`,
  `deadline_exceeded`, `engine_unavailable`, `policy_denied`, `internal`. Клиентская ошибка
  `runtime_unavailable` означает, что runtime не отвечает. `domain` для исполнения берётся из
  таксономии ADR 0019: `environment_preparation`, `infrastructure`, `policy`, `tooling`,
  `code_test`, `timeout`, `cancelled`, `unknown`.

### 4. Идемпотентность и потерянный ACK

- `operation_id` — детерминированный ключ намерения, а не случайный id вызова CLI. Для исполнения
  он равен `sha256(dispatch_id, brief_sha256, attempt)`, для мутации lifecycle —
  `sha256(batch_id, expected_revision, operation, payload_digest)`. Поэтому повтор той же команды
  после SIGKILL coordinator-а получает тот же ключ.
- Таблица `operations(operation_id PK, operation, request_digest, state, result_json,
  created_at, completed_at)`. Чистая мутация store записывает строку операции и эффект в одной
  транзакции. Повтор с тем же `operation_id` и `request_digest` возвращает сохранённый результат;
  тот же `operation_id` с другим digest — `idempotency_conflict`.
- Исполнение с внешним эффектом идёт в три шага:
  1. транзакция: `operations.state = intent` и `executions.state = launching` с детерминированными
     именем и метками (`harness.operation=<operation_id>` рядом с метками ADR 0018);
  2. запуск процесса или контейнера;
  3. транзакция: receipt и `operations.state = committed`.
- Повтор или перезапуск runtime при `intent` сначала выполняет reconcile (ADR 0022): найденный по
  меткам или идентичности объект усыновляется, и receipt записывается без нового запуска. Новый
  запуск допускается только при доказанном отсутствии объекта: движок ответил «нет такого
  объекта». Ответ «неизвестно» оставляет исполнение в `unknown` и возвращает retryable
  `engine_unavailable`.
- Уникальность на уровне домена дублирует ключ: `UNIQUE(dispatch_id, attempt)` для исполнений и
  `UNIQUE(qa_lane.id)` для QA-lease. Второй `dispatch send` того же dispatch возвращает
  существующий receipt.
- `runtime_client` повторяет запрос с тем же `operation_id` при обрыве транспорта до `deadline`,
  с экспоненциальной задержкой от 100 мс до 5 с, и никогда не порождает новый ключ для того же
  намерения.

### 5. Коды выхода

- CLI coordinator сохраняет контракт `harness/errors.py:33-39`: 0 и JSON в stdout при успехе, 2 и
  `ERROR:`/`REMEDY:` в stderr для любой ошибки IPC. Код ошибки протокола добавляется в первую
  строку `REMEDY:` как `[<code>]`, чтобы скрипты могли его распознать без смены формата.
- Процесс `harness-runtime serve`: 0 — штатная остановка; 2 — отказ запуска (authority не
  `runtime-store`, другой экземпляр держит store, путь сокета слишком длинный); 4 — нарушение
  целостности store при старте, сервис не обслуживает запросы до вмешательства оператора. Коды
  `migrate`/`rollback` заданы в ADR 0020.

## Операционные последствия

- **Approval.** Rust сверяет `transition_digest`, `plan_digest`, `runtime_images` и
  `hook_policy_sha256` с сохранённым brief, а не с флагами запроса. Флаг «approved» от клиента не
  принимается. Каноническая форма digest общая для Python и Rust и проверяется контрактным тестом
  на golden-векторах.
- **Негативные тесты (seam 1 — граница процесса coordinator CLI с настоящим runtime):**
  - чужой uid и неверный токен → `unauthorized_peer`;
  - несовместимый major → `protocol_unsupported`, код 2;
  - неизвестное поле запроса → `malformed_request`;
  - `approval_mismatch` при подменённом `plan_digest`;
  - failpoint «commit, затем обрыв ответа» на `dispatch send` → повтор возвращает тот же receipt,
    движок видит ровно один `create`;
  - SIGKILL coordinator-а после шага 1 → повтор усыновляет объект;
  - SIGKILL runtime между шагами 2 и 3 → reconcile после рестарта записывает receipt без второго
    запуска;
  - ответ движка «неизвестно» → `unknown`, без запуска;
  - тот же `operation_id` с другим payload → `idempotency_conflict`.
- **Race-сценарии:** два coordinator шлют одно намерение одновременно → один `intent`, второй
  получает тот же результат после commit; `revision_conflict` при встречных мутациях одного batch;
  `fencing_mismatch` при снятии чужого lease.
- **Команды проверки:** `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings`,
  `cargo test` в `native/harness-runtime`;
  `.harness/.venv/bin/python -m pytest -q tests/orchestration/test_runtime_client.py` (новый),
  `make verify`.
