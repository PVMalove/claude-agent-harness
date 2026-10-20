# Rust-супервизор процессов и OCI-контейнеров

**Статус: ACCEPTED (2026-10-20).** Эпик #784, ADR 0018 (`0018-container-executor.md` в
`integration/container-executor`), ADR 0021. Документ переносит границу движка контейнеров из
Python в Rust runtime и задаёт модель жизненного цикла процессов на Linux (включая Fedora с SELinux
и rootless Podman) и Windows. Решения ADR 0018 о монтированиях, сети, секретах, владельце файлов и
закреплении digest остаются в силе; меняется только исполнитель.

## Контекст системы

В `integration/container-executor` встроенный `runtime_access` `docker` (`DockerExecutor`,
`container_executor.py:813`) запускает worker-а staged-последовательностью `_launch` (`:385`)
через единственную границу процесса `run_engine` (`:87`, таймаут 30 с). Статус читает
`inspect_worker` (`:447`), очистку делают `engine_objects`/`remove_objects` (`:560`, `:605`),
`workflow/container_status.reconcile` и `harness/cleanup.py`. Ограничения текущего кода:

- остановки с grace-периодом нет; живой worker без явной команды не останавливается (ADR 0018);
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
  (ADR 0018) и новые `harness.operation`, `harness.execution`.
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
- **Остановка** только по явной команде или по `execution_deadline` из плана:
  - Linux: SIGTERM группе, после grace-периода SIGKILL;
  - контейнер: `<engine> stop --time <grace>`, затем `<engine> kill`;
  - Windows: `CTRL_BREAK`, затем `TerminateJobObject`.

  После остановки записываются exit evidence и частичные логи, затем освобождаются lease. Lease
  освобождается только после доказанной остановки.

### 4. Контейнеры

- **Worker (handoff):** detached, как в ADR 0018. Launch, inspect, remove и reconcile выполняет
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
- Существующие запреты ADR 0018 остаются: `--cap-drop ALL`, `no-new-privileges`, `--pull never`,
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
| Fedora, SELinux enforcing, rootless Podman | группы + systemd scope | Podman, `--userns=keep-id` | `label=disable`, как в ADR 0018: SELinux-разделение для контейнера выключается, зато файлы хоста не перемаркируются. Перемаркировка `:z`/`:Z` остаётся отвергнутой. Ограничения ресурсов требуют делегирования cgroup пользователю: без него ограничение из плана — блокер, а не тихий пропуск. |
| Linux, rootful/rootless Docker | группы + systemd scope | Docker, режимы ADR 0018 | userns-remap — блокер, как в ADR 0018 |
| Ubuntu CI | группы | Docker | AppArmor userns уже настраивается в `verify.yml` |
| Windows | Job Objects | только Docker Desktop | Podman на Windows работает через VM, то есть удалённо, а удалённый Podman — блокер ADR 0018 |

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
