# Rust-супервизор процессов и OCI-контейнеров

**Статус: ACCEPTED (2026-10-20).** Эпик #784, ADR 0025, ADR 0021. Документ переносит границу
движка контейнеров из Python в Rust runtime и задаёт модель жизненного цикла процессов на Linux
(включая Fedora с SELinux и rootless Podman) и Windows. Решения ADR 0025 о монтированиях, сети,
секретах, владельце файлов и закреплении digest остаются в силе; меняется только исполнитель.

## Контекст системы

В `integration/container-executor` встроенный `runtime_access` `docker` (`DockerExecutor`,
`container_executor.py:813`) запускает worker-а staged-последовательностью `_launch` (`:385`)
через единственную границу процесса `run_engine` (`:87`, таймаут 30 с). Статус читает
`inspect_worker` (`:447`), очистку делают `engine_objects`/`remove_objects` (`:560`, `:605`),
`workflow/container_status.reconcile` и `harness/cleanup.py`. Ограничения текущего кода:

- остановки с grace-периодом нет; живой worker без явной команды не останавливается (ADR 0025);
- `inspect_worker`, `engine_objects` и `remove_objects` вызывают `docker` даже на Podman-хосте
  (`:463`, `:570-582`, `:631-633`);
- clean-room QA в контейнере (#709) не реализован: `gate_runner._execute_commands` запускает
  команду на хосте через `subprocess.run` без таймаута и сливает stdout и stderr
  (`gate_runner.py:317-387` на `master`);
- идентичность процесса не хранится, а `create_argv` не ставит `--rm`/`--init`.

## Действующий контракт

### 1. Модель исполнения

- Один процесс `harness-runtime serve` на state-каталог репозитория. Эксклюзивность обеспечивает
  блокировка ОС на `runtime.db.lock`; второй экземпляр завершается с кодом 2 (ADR 0021).
- Асинхронное ядро Tokio. Каждое исполнение — задача-супервизор, владеющая своей строкой
  `executions`; вызовы движка — `tokio::process::Command` с таймаутом на вызов (30 с, как
  `ENGINE_TIMEOUT_SECONDS`) и без `kill_on_drop`, чтобы падение runtime не убивало исполнение.
- В Rust нет `unsafe`, кроме изолированных FFI-модулей платформы (`pidfd`, Job Objects), которые
  покрыты отдельными тестами.
- Все обращения к движку идут через трейт `Engine`. Реализации: Docker, Podman и scripted engine
  для тестов, который записывает argv, как `FakeEngine` в `test_container_executor.py`. Двоичный
  файл движка берётся из `observe()` для каждой операции, включая inspect и remove.

### 2. Идентичность и состояния

- **Процесс Linux:** `boot_id`, inode PID-namespace, `pid`, время старта из `/proc/<pid>/stat`
  (поле 22). Наблюдение идёт через `pidfd_open`, если ядро его поддерживает.
- **Процесс Windows:** `pid`, время создания процесса и имя Job Object, производное от
  `operation_id`.
- **Контейнер:** id, метки `harness.dispatch`, `harness.repository`, `harness.plan_digest`
  (ADR 0025) и новые `harness.operation`, `harness.execution`.
- PID никогда не служит доказательством сам по себе. Объект другого namespace или boot без
  доказательства смерти считается живым.
- Состояния исполнения: `launching → running → stopping → exited | lost | unknown`. `exited`
  хранит exit code, сигнал, `oom_killed` и время. `lost` — объект доказанно отсутствует без exit
  evidence. `unknown` — движок или ОС не дали ответа; из `unknown` нет автоматических действий,
  кроме повторного наблюдения.
- Терминальны только `exited` и `lost`. `unknown` не терминально: повторное наблюдение переводит
  его в `running`, `exited` или `lost`. Состояние исполнения — наблюдение; переход `DispatchState`
  по нему выбирает только Python (ADR 0021, раздел 6).

### 3. Процессы и группы

- **Linux:** дочерний процесс стартует в своей группе (`setsid`). При наличии пользовательского
  systemd он оборачивается в transient scope (`systemd-run --user --scope`) для учёта cgroup v2;
  без systemd учёт идёт по группе процессов, и это записывается фактом в receipt.
- **Windows:** именованный Job Object без `KILL_ON_JOB_CLOSE`, чтобы исполнение пережило рестарт
  runtime, и `CREATE_NEW_PROCESS_GROUP` для доставки `CTRL_BREAK`.
- `execution_deadline` — абсолютное время UTC: старт исполнения плюс `timeout_seconds` стадии
  `VerificationPlan` (ADR 0023) или срок handoff из brief для worker-а. `stop_grace_seconds` —
  поле того же плана; оба закрепляются в brief, значения по умолчанию задаёт тикет супервизора.
- **Остановка** только по явной команде или по `execution_deadline` из плана:
  - Linux: SIGTERM группе, после grace-периода SIGKILL;
  - контейнер: `<engine> stop --time <grace>`, затем `<engine> kill`;
  - Windows: `CTRL_BREAK`, затем `TerminateJobObject`.

  После остановки записываются exit evidence и частичные логи, затем освобождаются lease. Lease
  освобождается только после доказанной остановки.

### 4. Контейнеры

- **Worker (handoff):** detached, как в ADR 0025. Launch, inspect, remove и reconcile выполняет
  Rust; Python больше не формирует argv.
- **QA-команда (#709):** отдельный foreground-контейнер `--rm --init` на каждую команду
  `VerificationPlan` из образа, закреплённого в brief QA-dispatch, по плану `operations.qa`.
  Sidecar и сеть живут весь QA run. stdout и stderr сохраняются раздельно, в content-addressed
  файлы. `--init` пересылает сигналы и собирает зомби.
- **Классификация кода выхода** команды в контейнере: 125 — ошибка движка (`infrastructure`),
  126/127 — команда не запускается или не найдена (`tooling`), 128+N — сигнал (`cancelled` при
  остановке по команде, иначе `unknown`), остальные — результат команды. Exit code wrapper-а не
  подменяет exit code команды.
- **Запрещённые монтирования:** план монтирования отвергается (`policy_denied`), если путь
  совпадает с сокетом движка или лежит под ним (`/var/run/docker.sock`, `/run/docker.sock`,
  `/run/podman/podman.sock`, `$XDG_RUNTIME_DIR/podman/`, `$XDG_RUNTIME_DIR/docker.sock`), с
  каталогом runtime, сокетом или токеном ADR 0021. Проверка идёт по `realpath` после разрешения
  symlink.
- Существующие запреты ADR 0025 остаются: `--cap-drop ALL`, `no-new-privileges`, `--pull never`,
  монтирования только по плану, env только по именам.

### 5. Reconcile

Reconcile запускается при старте runtime, перед каждой операцией над исполнением и периодически
(раз в `dispatch_poll_interval_seconds`):

1. Для каждого исполнения не в терминальном состоянии наблюдаются процесс и контейнер.
2. Живой объект с совпадающей идентичностью → `running`; выход с evidence → `exited`;
   доказанное отсутствие → `lost`; нет ответа → `unknown`.
3. Объекты с меткой своего репозитория без строки `executions` выводятся как сироты в `qa
   status`/`dispatch status`. Удаляет их только явная команда `harness cleanup`; чужой репозиторий
   не затрагивается.

### 6. Платформы

| Платформа | Процессы | Контейнеры | Особенности |
|---|---|---|---|
| Fedora, SELinux enforcing, rootless Podman | группы + systemd scope | Podman, `--userns=keep-id` | `label=disable`, как в ADR 0025: SELinux-разделение для контейнера выключается, зато файлы хоста не перемаркируются. Перемаркировка `:z`/`:Z` остаётся отвергнутой. Ограничения ресурсов требуют делегирования cgroup пользователю: без него ограничение из плана — блокер, а не тихий пропуск. |
| Linux, rootful/rootless Docker | группы + systemd scope | Docker, режимы ADR 0025 | userns-remap — блокер, как в ADR 0025 |
| Ubuntu CI | группы | Docker | AppArmor userns уже настраивается в `verify.yml` |
| Windows | Job Objects | только Docker Desktop | Podman на Windows работает через VM, то есть удалённо, а удалённый Podman — блокер ADR 0025 |

## Операционные последствия

- **Approval.** Rust запускает только по сохранённому brief с совпадающими `plan_digest` и
  `runtime_images`; при изменённом образе срабатывает `CHANGED_IMAGE_REMEDY` и нужен новый
  propose. Auto-approve, флагов агента и обхода hooks нет.
- **Перенос тестов.** Матрица argv `EngineMatrixChecks` (8 комбинаций Docker/Podman × rootful/
  rootless × SELinux) экспортируется в golden-фикстуры и проверяется `cargo test` против трейта
  `Engine`. Python-тесты сохраняют seam 1: граница процесса coordinator CLI и настоящий runtime,
  подменён только scripted engine.
- **Негативные тесты:**
  - SIGKILL runtime посреди staged-запуска → reconcile удаляет частичные объекты от новых к
    старым;
  - PID reuse и другой namespace → живой объект не останавливается;
  - статус и очистка на Podman-хосте вызывают `podman`;
  - сокет движка или runtime в плане (в том числе через symlink) → `policy_denied`;
  - SIGTERM посреди QA-команды → контейнер остановлен, частичные логи сохранены, lane освобождён;
  - таймаут команды → `timeout`;
  - exit 125/126/127 классифицируются по таблице;
  - без делегирования cgroup и с лимитом в плане → блокер;
  - сироты чужого репозитория не затрагиваются.
- **Race-сценарии:** reconcile и явный `stop` одного исполнения сериализуются по строке
  `executions`; удаление объекта, который снова запущен, отвергается (как в
  `test_a_worker_that_is_running_again_is_never_stopped_by_the_removal`).
- **Живые smoke-проверки** выполняются вне `make verify`: Docker на Ubuntu в CI. Fedora с SELinux
  и rootless Podman требует self-hosted runner или VM, потому что GitHub-hosted runner её не даёт;
  до появления runner-а поддержка подтверждается только argv-матрицей, и это фиксируется в
  отчётах. Windows-контракт (named pipe, Job Objects) — отдельное лёгкое задание CI, а не полный
  `make verify`, отключённый в #753.
- **Команды проверки:** `cargo test` в `native/harness-runtime`;
  `.harness/.venv/bin/python -m pytest -q tests/orchestration/test_container_executor.py
  tests/orchestration/test_container_lifecycle.py tests/orchestration/test_runtime_access.py
  tests/core/test_cleanup.py tests/core/test_gate_runner.py` (контейнерные тесты — из
  `integration/container-executor` после её вливания); `make verify`.

## Решения реализации #805

**Принято оператором (2026-10-28).** Supervisor процессов на Linux (#805) реализует разделы 1–3 и
5 с отступлениями ниже. Контейнеры (#807), Windows Job Objects (#806) и одобрение политики hooks
при запуске (#808) этот ticket не затрагивает.

| Текст ADR | Решение #805 | Причина |
|---|---|---|
| §1: асинхронное ядро Tokio, одна задача на исполнение | Один std-поток на дочерний процесс (`Child::try_wait` со сроком исполнения) и один поток периодического reconcile | Runtime синхронный: IPC-сервер обслуживает соединение в своём потоке. `std::process::Child` не убивает процесс при drop, поэтому отсутствие `kill_on_drop` выполняется по построению. |
| §3: своя группа через `setsid` | `CommandExt::process_group(0)`: своя группа процессов без новой сессии | Безопасный Rust; `setsid` требует `unsafe` в `pre_exec`. Для остановки по сроку нужна группа процессов. |
| §3: systemd `--user --scope`, если он есть | Учёт по группе процессов; receipt всегда содержит `"accounting": "process-group"` | ADR допускает учёт по группе с отметкой в receipt. Для scope нужна пользовательская шина, которой нет в CI и в sandbox; scope — отдельная задача. |
| §2: наблюдение через `pidfd_open` | Опрос `/proc/<pid>/stat` с идентичностью по времени старта (поле 22) | Время старта отличает переиспользованный PID; pidfd не даёт кода выхода чужого процесса. |
| ADR 0021 §4: новый запуск при доказанном отсутствии | Intent без живого процесса с метками после рестарта становится `lost`, операция — `failed`; повторного запуска нет | Отсутствие процесса не отличает «не стартовал» от «уже завершился и собран». At-most-once важнее. |
| ADR 0021 §4: `attempt` из store | `attempt = 1`: счётчика попыток нет, повтор сегодня — новый dispatch | `operation_id = sha256([dispatch_id, brief_sha256, 1])` в каноническом виде digest. |

Как работает реализация:

- **Хранилище.** Схема store 4 добавляет таблицу `executions`: идентичность процесса (host,
  `boot_id`, inode PID namespace, PID, время старта), `pgid`, receipt, свидетельство выхода и
  аренда. Триггер допускает только переходы `launching → running | exited | lost | unknown`,
  `running → stopping | exited | lost | unknown`, `stopping → exited | lost | unknown` и
  `unknown → running | exited | lost`; строка в `exited` или `lost` больше не меняется. Каждый
  переход — одна транзакция: строка с CAS по ревизии, `dispatch_status.execution_state` и одна
  запись аудита `execution-<state>`. Rust не пишет `DispatchState`.
- **Запуск.** `execution.launch` читает сохранённые brief и статус dispatch. Brief внешнего
  dispatch, созданного на runtime store, закрепляет `execution` (срок
  `worker_execution_timeout_seconds`, по умолчанию 4 часа, `stop_grace_seconds` и интервал
  опроса); статус содержит `launch` с argv адаптера и рабочим каталогом. Запуск идёт в три шага:
  intent, процесс в своей группе с метками `HARNESS_OPERATION_ID` и `HARNESS_DISPATCH_ID`,
  receipt. Повтор отвечает сохранённым receipt.
- **Усыновление.** Reconcile выполняется до строки готовности runtime, перед каждой операцией
  исполнения и периодически. Intent без идентичности ищет лидера группы с меткой в
  `/proc/*/environ`: из нескольких выбирается стартовавший первым и не раньше runtime, который
  записал intent; найден — receipt с `adopted: true`, не найден — `lost`. Процесс, который
  усыновлённый runtime видит завершённым, не имеет кода выхода и тоже становится `lost`.
- **Наблюдение.** Другая загрузка, другой PID namespace или другой host дают `unknown`: такой
  процесс считается живым, его не останавливают и аренду не освобождают. Переиспользованный PID
  даёт `lost`, процесс с этим PID не получает сигнала.
- **Остановка по сроку.** При `execution_deadline` исполнение `running` переходит в `stopping`,
  группа получает SIGTERM, через `stop_grace_seconds` — SIGKILL. Свидетельство выхода (код или
  сигнал, время сигналов, размер и SHA-256 обоих логов) и освобождение аренды записываются одной
  транзакцией после доказанного выхода; неудачное наблюдение во время остановки даёт `unknown`.
  Остановку, начатую до рестарта runtime, reconcile нового runtime доводит до конца: SIGKILL через
  `stop_grace_seconds` от записанного SIGTERM, затем то же свидетельство. Лидер, завершившийся
  сам, завершает исполнение только вместе с группой: оставшийся участник держит аренду до своего
  выхода или до остановки по сроку.
- **Реакция coordinator.** `report submit` при незавершённом исполнении записывает отчёт и
  оставляет dispatch `working`. Python выбирает переход по наблюдаемому исполнению: `exited` с
  отчётом — `reported`, `exited` без отчёта — `blocked` с доменом `timeout` после остановки по
  сроку, `lost` — `blocked` с доменом `infrastructure`. Отчёт, который ждал выхода потерянного
  исполнения, остаётся в статусе как `held_report`, и `dispatch status` показывает его оператору.
- **Адаптер.** Под меткой `HARNESS_OPERATION_ID` адаптер `claude_code` запускает worker на
  переднем плане и завершается вместе с ним. Сторонний адаптер, который возвращается сразу после
  старта worker, завершает исполнение, и dispatch становится `blocked`. Worker наследует окружение
  runtime, а не coordinator.
