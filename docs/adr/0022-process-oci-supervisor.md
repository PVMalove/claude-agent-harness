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
  receipt. Повтор отвечает сохранённым receipt. Только повторяемый отказ runtime советует
  повторить `dispatch send`; неповторяемый (`policy_denied`, `idempotency_conflict`,
  `malformed_request` и другие) показывает remedy runtime, dispatch остаётся `dispatched`.
- **Усыновление.** Reconcile выполняется до строки готовности runtime, перед каждой операцией
  исполнения и периодически. Intent без идентичности ищет лидера группы с меткой в
  `/proc/*/environ`: из нескольких выбирается стартовавший первым и не раньше runtime, который
  записал intent; найден — receipt с `adopted: true`, не найден — `lost`. Усыновлённый
  процесс, который runtime видит завершённым, не имеет кода выхода и становится `lost`, но
  только при пустой группе: пока жив участник группы, исполнение остаётся `running`, держит
  аренду и останавливается по сроку.
- **Наблюдение.** Другая загрузка, другой PID namespace или другой host дают `unknown`: такой
  процесс считается живым, его не останавливают и аренду не освобождают. Переиспользованный PID
  даёт `lost`, процесс с этим PID не получает сигнала.
- **Остановка по сроку.** При `execution_deadline` группа исполнения `running` получает SIGTERM,
  затем исполнение переходит в `stopping` со временем этого сигнала; через `stop_grace_seconds` —
  SIGKILL. Строка не утверждает сигнал, который не отправлен: если runtime погиб между сигналом и
  записью, исполнение остаётся `running` после срока, и следующий runtime останавливает его
  заново. Свидетельство выхода (код или сигнал, время сигналов, размер и SHA-256 обоих логов) и
  освобождение аренды записываются одной транзакцией после доказанного выхода; неудачное
  наблюдение во время остановки даёт `unknown`. Остановку, начатую до рестарта runtime, reconcile
  нового runtime доводит до конца: пока grace не истёк, повторяет SIGTERM, через
  `stop_grace_seconds` от записанного SIGTERM — SIGKILL, затем то же свидетельство. Лидер,
  завершившийся сам, завершает исполнение только вместе с группой: оставшийся участник держит
  аренду до своего выхода или до остановки по сроку. Номер группы не больше 1 сигнала не
  получает: такая остановка даёт `unknown`. Если store отказывает в записи исхода навсегда или
  дольше ограниченного числа повторов, строка становится `unknown` с удержанной арендой, а
  причина пишется в stderr runtime и в запись аудита.
- **Реакция coordinator.** `report submit` при незавершённом исполнении записывает отчёт и
  оставляет dispatch `working`. Python выбирает переход по наблюдаемому исполнению: `exited` с
  отчётом — `reported`, `exited` без отчёта — `blocked` с доменом `timeout` после остановки по
  сроку, `lost` — `blocked` с доменом `infrastructure`. Отчёт, который ждал выхода потерянного
  исполнения, остаётся в статусе как `held_report`, и `dispatch status` показывает его оператору.
  Если запись ждавшего отчёта после выхода отказана, отчёт остаётся ожидающим, а
  `dispatch status --dispatch` показывает причину как `reaction_error`.
- **Адаптер.** Под меткой `HARNESS_OPERATION_ID` адаптер `claude_code` запускает worker на
  переднем плане и завершается вместе с ним. Сторонний адаптер, который возвращается сразу после
  старта worker, завершает исполнение, и dispatch становится `blocked`. Worker наследует окружение
  runtime, а не coordinator.

## Решения реализации #807

**Принято оператором (2026-10-28).** Контейнерный worker (#807) реализует раздел 4 для worker-а
с отступлениями ниже. QA-контейнер (#709), Windows Job Objects (#806), одобрение политики hooks
при запуске (#808) и CI smoke Docker с базовым образом worker-а (#710) этот ticket не затрагивает.

| Текст ADR | Решение #807 | Причина |
|---|---|---|
| ADR 0025: значения `environment` берутся из окружения coordinator | Значения берутся из окружения `harness-runtime serve`; в контейнер по-прежнему передаётся только имя (`--env NAME`) | Контейнер запускает runtime, а значение не проходит через IPC, store и evidence. |
| ADR 0025: worker, sidecar и сеть удаляются при терминальном `DispatchState` | Объекты удаляются при выходе исполнения, когда логи сохранены, а выход с кодом записан в `executions`; незавершённое удаление повторяет reconcile | Rust не пишет `DispatchState` (ADR 0021, один писатель), а выход исполнения — его собственное наблюдение. |
| ADR 0021 §4, ADR 0025: сбой запуска удаляет объекты, dispatch можно отправить снова | Неудачный staged launch — at-most-once: объекты откатываются от новых к старым, исполнение `lost`, операция `failed`, dispatch `blocked`; повторного запуска нет | Как в #805: отсутствие объекта не отличает «не создан» от «уже удалён», at-most-once важнее. |
| ADR 0025: исполнитель работает в любой эпохе ledger и на любой платформе | Только эпоха runtime store и только Linux; на файловом ledger и вне Linux `dispatch preflight` блокирует его с remedy (`harness-runtime migrate`, затем `serve`) | Rust запускает только по сохранённому brief; Windows — #806. |
| §1: асинхронное ядро Tokio | Синхронный `std::process`: команда движка опрашивается `try_wait` до 30 секунд, затем убивается | Так же, как в #805: runtime синхронный, IPC обслуживает соединение в своём потоке. |

Как работает реализация:

- **Крейт `engine`.** `Kind` (`docker`, `podman`), трейт `Engine` с реализациями `Docker`,
  `Podman` и `Scripted` (ответы по префиксу argv и запись каждого argv для тестов) — единственная
  граница процесса движка. Построители argv и разбор `info`, id образа и `inspect` перенесены из
  Python 1:1; единственная дельта — метки `harness.operation` и `harness.execution` после
  `harness.plan_digest`. Восемь строк `EngineMatrixChecks` (Docker и Podman, rootful и rootless,
  с SELinux и без) экспортированы в `crates/engine/tests/fixtures/engine_matrix.json` и проходят
  через `Scripted`.
- **Хранилище.** Схема store 5 пересобирает `executions` с полями `kind` (`process`,
  `container`), `engine` и `container_id`: исполнение-контейнер может быть `running` с id
  контейнера вместо PID. `serve` повышает схему 4 до 5 при старте; сборка со схемой 4 store
  схемы 5 не открывает.
- **Политика монтирования.** Корень плана отвергается `policy_denied`, если его `realpath` равен
  защищённому пути, лежит под ним или содержит его, независимо от существования защищённого пути.
  Защищены сокеты движков (`/var/run/docker.sock`, `/run/docker.sock`,
  `/run/podman/podman.sock`, `podman` и `docker.sock` в `$XDG_RUNTIME_DIR` и в
  `/run/user/<uid>`, `/tmp/podman-run-<uid>`, Unix-путь из `DOCKER_HOST` и `CONTAINER_HOST`),
  собственные каталог, сокет, токен и замок runtime и все известные каталоги runtime этого uid
  (`$XDG_RUNTIME_DIR/harness`, `/run/user/<uid>/harness`, `/tmp/harness-<uid>`). Корень, который
  не разрешается, корень с символами `,"\n\r` и пересечение с `/harness-home` тоже отвергаются.
  Проверка идёт в `container.observe` и в шаге 1 `execution.launch` до любого вызова движка и
  до записи строки.
- **Запуск.** `execution.launch` выбирает контейнер, если `runtime_access.runtime_extension`
  сохранённого brief — `docker`. Шаг 1 ничего не пишет: `git_common` плана равен общему
  git-каталогу runtime, политика монтирования, имена `environment` есть в окружении runtime,
  read-only observe движка; id образа, отличный от `runtime_images`, — `approval_mismatch` с
  remedy о новом propose. Шаг 2 — intent с запланированными объектами, шаг 3 — staged launch
  (сеть, sidecar, worker), шаг 4 — receipt с `container_id` и `engine`.
- **Наблюдение и выход.** Reconcile, усыновление незавершённого запуска по меткам, остановка по
  сроку (`stop --time <grace>`, затем `kill`, `stopped_by: "deadline"`), сохранение логов в
  `executions/<id>/stdout.log` и `stderr.log` и удаление объектов всегда идут движком из строки
  `executions`, поэтому на хосте с Podman ни одна команда не уходит в `docker`.
- **Протокол 1.3.** `container.observe {plan}`, `container.objects {engines}` и
  `container.remove {objects}` есть в `hello` только при подключённом store на Linux, как
  `execution.*`. Метка репозитория — собственный канонический общий git-каталог runtime, а не
  поле запроса. `container.remove` заново проверяет каждый объект и отказывает объекту другого
  репозитория, нетерминального исполнения и снова запущенному worker-у; порядок — worker,
  sidecar, сеть.
- **Volumes.** Исполнитель не создаёт named volumes (ADR 0025), а runtime не перечисляет и не
  удаляет volumes ни одной командой. Анонимные volumes из `VOLUME` образа остаются за `rm` без
  `-v`.
- **Python.** `container_executor.py` проверяет план, спрашивает `container.observe` и сравнивает
  закреплённые digest; `run_engine`, argv движка и `workflow/container_status.py` удалены.
  `dispatch send` применяет план supervised docker-dispatch без передачи провайдеру и запускает
  его через `execution.launch`; `dispatch status` и `dispatch wait` показывают исполнение runtime
  и движок не спрашивают. `harness cleanup` перечисляет и удаляет объекты только через
  `container.objects` и `container.remove` с движками из `access_policy`.
- **Каталог runtime.** Без `XDG_RUNTIME_DIR` каталог runtime — `/tmp/harness-<uid>` вне любого
  репозитория (ADR 0021, раздел 2), поэтому обязательные корни плана его не содержат и запуск не
  отвергается из-за runtime.

## Решения реализации #810

**Принято оператором (2026-10-28).** Контейнерный clean-room QA (#810) реализует раздел 3 и
раздел 4 для команд QA с отступлениями ниже. Windows Job Objects (#806), одобрение политики hooks
(#808) и CI smoke Docker (#710) этот ticket не затрагивает; detached worker-контейнер #807 не
меняется.

| Текст ADR | Решение #810 | Причина |
|---|---|---|
| ADR 0023 §5: повтор после `infrastructure` или `environment_preparation` — новый `attempt` | Для контейнерного QA так и есть: отчёта нет, есть запись `qa-lane/attempts`, `qa run` повторяется как attempt N+1. Исторический хост-QA сохраняет отчёт `blocked` и policy-retry новым dispatch | Объединение путей ломает поведение исторического QA (DoD 11). |
| ADR 0021 §4: `attempt` хранится в строке dispatch | `attempt` — поле тела `dispatch-status`: его пишет Python через `state.write` с CAS, Rust только читает (нет поля — attempt 1) | Один писатель dispatch-status и без новой колонки схемы store. |
| ADR 0025: ресурсы плана монтируются с их доступом | Сужение: `run_root` (каталог clean-room под `shared_storage`) на запись, `git_common` только на чтение, `cache` плана со своим доступом; `artifacts`, issue worktree и весь `shared_storage` не монтируются | Код кандидата не пишет ledger, `runtime.db` и refs. Шаг QA, которому нужна запись вне `run_root`, — повод пересмотреть решение, а не расширить монтирование молча. |
| §3: срок команды берётся из `timeout_seconds` стадии `VerificationPlan` | Новый ключ `execution_policy.qa_command_timeout_seconds` (по умолчанию 3600) закрепляется в brief `qa_execution` | QA исполняет `verification_commands`, у которых нет стадий плана. |
| `stage-evidence.v1`: результат команды — `pass`, `fail` или `not_run` | Домен окончания хранится в `qa_stages[].domain` и в exit evidence runtime; в stage evidence команда с доменом — `not_run` с реальным кодом выхода | В схеме stage evidence нет поля домена. |
| §3: QA-команды исполняются в контейнере | Только если `access_policy.operations.qa.container` задан; проект с одним `defaults.container` остаётся на хост-QA | Исторический QA не меняется без явной настройки. |
| §1: асинхронное ядро Tokio | Синхронный `std::process` и поток прогона; осиротевший после рестарта runtime QA-прогон становится `lost` и не возобновляется | Как в #805 и #807. |

Как работает реализация:

- **Классификация.** Crate `qa` проверяет срок команды первым: команда, остановленная по сроку
  и не завершившаяся кодом 0, — `timeout`, даже без кода выхода. Дальше: нет кода или 125 —
  `infrastructure`, 126 и 127 — `tooling`, 129–255 — `cancelled` после `qa.stop` и `unknown`
  иначе; любой другой код — собственный результат команды. `stopped_by` прогона — `command`
  после `qa.stop` и `deadline`, если хотя бы одна команда дошла до своего срока.
- **`qa.stop`.** Запрос `{dispatch_id, attempt}` не требует fencing token и не пишет строку
  `operations`: он только ставит флаг остановки потока прогона или запускает reconcile. Флаг
  появляется вместе с квитанцией `qa.run`; прогон без квитанции остановку не хранит.
- **Python.** `qa run` для brief с `qa_execution` готовит clean-room checkout на хосте (Git вне
  контейнеров), пишет `attempt` при переходе в `working`, вызывает `qa.run` с fencing token lane и
  опрашивает `execution.observe`. Потоки читаются из `executions/<id>/commands`, сверяются по
  sha256 и санитизируются. Исходы: `tooling`, `timeout`, `unknown` — отчёт `blocked` с доменом;
  `infrastructure`, `environment_preparation`, `cancelled` — без отчёта, повтор `qa run` — новая
  попытка без траты бюджета developer-а. Число таких попыток не ограничено, как у нынешних
  транзиентных сбоев `qa run`.
- **Сигналы и страховочный срок.** SIGINT и SIGTERM, пришедшие во время вызова `qa.run`,
  coordinator откладывает до квитанции: брошенный вызов оставил бы runtime прогон без владельца.
  После квитанции SIGTERM поднимает исключение с кодом 143, SIGINT — `KeyboardInterrupt`;
  страховочный срок coordinator-а — `execution_deadline` квитанции плюс grace и 60 секунд.
  Coordinator шлёт `qa.stop`, ждёт конца прогона (grace плюс 60 секунд), записывает частичное
  evidence в `qa-lane/attempts`, освобождает lane и поднимает сигнал снова.
  Восстановление владельца очищенного или снятого lease останавливает его живой прогон до
  возврата dispatch в `approved`.
- **Эпоха store.** Артефакты QA пишутся в единице store команды, а записи стадий сверяются с
  текстом артефакта, который принял runtime; схема brief принимает `qa_execution` только рядом с
  `runtime_images`.
- **Проверки.** Fedora, SELinux и rootless Podman подтверждены argv-матрицей и scripted-движком;
  живой smoke движка — #710.

## Решения реализации #806

**Принято оператором (2026-10-29).** Supervisor процессов на Windows (#806) реализует разделы 2,
3 и 5 для Job Objects с отступлениями ниже. Контейнеры на Windows (Docker Desktop), одобрение
политики hooks при запуске (#808) и полный `verify-windows` (#753) этот ticket не затрагивает;
поведение на Linux не меняется.

| Текст ADR | Решение #806 | Причина |
|---|---|---|
| §3: `CREATE_NEW_PROCESS_GROUP` для доставки `CTRL_BREAK` | Исполнение получает ещё и свою скрытую консоль (`CREATE_NO_WINDOW`); `CTRL_BREAK` шлёт короткий helper `harness-runtime ctrl-break` без консоли (`DETACHED_PROCESS`), который подключается к консоли живого участника job | `GenerateConsoleCtrlEvent` доходит только до процессов той же консоли. Общая консоль с runtime отдала бы исполнению Ctrl+Break оператора и закрытие окна терминала, а `AttachConsole` в самом runtime сломал бы его stderr. |
| §3: `CTRL_BREAK`, затем `TerminateJobObject`; #805: отказ SIGTERM даёт `unknown` | Недоставленный `CTRL_BREAK` — факт `ctrl_break` (`delivered`, `no-member`, `failed` с `ctrl_break_error`), остановка продолжается `TerminateJobObject` с кодом 137; доказательство — пустой job и ушедший лидер; `term_sent_at` — время попытки | `TerminateJobObject` останавливает всех участников job и без `CTRL_BREAK`, а пустой job доказывает остановку. |
| #805: усыновлённый процесс без кода выхода становится `lost` | Лидер, которого runtime держит открытым handle с того момента, когда он был жив, при пустом job даёт `exited` с кодом выхода и `source: process-handle` | Windows хранит код выхода, пока открыт handle процесса. |
| §2: имя Job Object входит в идентичность | Имя хранится в `launch.job` intent и в receipt, а не в колонках идентичности; схема store 5 не меняется; `pgid` — pid лидера | Имя выводится из `operation_id`, поэтому новая колонка не нужна. |
| §5: доказанное отсутствие — `lost` | Лидер ушёл (нет процесса, другое время создания или выход без удерживаемого handle) и имени job нет — `lost` с фактом `job: name-absent`, хотя участники могли пережить лидера | Временный объект ядра живёт по имени, только пока открыт хотя бы один handle. Без runtime и лидера имя пропадает, и job больше не найти. |
| ADR 0021 §4, #805: intent усыновляется по метке окружения | Intent усыновляет самый ранний живой участник job по имени из intent; его потоки возобновляются | Окружение чужого процесса Windows отдаёт только через недокументированный разбор PEB, а участник job — заведомо процесс исполнения: breakaway запрещён. Runtime мог умереть до возобновления подвешенного лидера. |
| §1: асинхронное ядро Tokio | Синхронные std-потоки, как в #805 | Runtime синхронный. |
| §6: на Windows только Docker Desktop | Контейнеры и QA-прогоны на Windows не обслуживаются: `hello` перечисляет только `execution.launch` и `execution.observe`, `container.*` и `qa.*` дают `unknown_operation`, brief с `docker` отвергается `policy_denied` до intent | Контейнеры на Windows — отдельная задача. |

Как работает реализация:

- **Граница кода.** Общий цикл запуска, reconcile и остановки (`launch.rs`, `stop.rs`) один для
  Linux и Windows; платформенный слой за cfg-границей: Linux — `linux.rs` и `process.rs`, Windows
  — `windows.rs`. Весь `unsafe` лежит в модуле `windows` крейта `platform-ffi` и его дочернем
  модуле `job`. Таблица наблюдения и имя job (`supervisor::job`) не делают системных вызовов и
  проверяются тестами и на Linux.
- **Запуск.** Шаг 2 идёт строго по порядку: `Job::create` с DACL только владельца
  (`D:P(A;;GA;;;<SID>)`) и без лимитов, то есть без `KILL_ON_JOB_CLOSE` и breakaway; уже
  существующее имя — отказ, чужой job не открывается как свой. Затем журналы с DACL только
  владельца, процесс с `CREATE_SUSPENDED | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW` и метками,
  `AssignProcessToJobObject`, копия handle job в лидере только с правом `JOB_OBJECT_QUERY` и
  возобновление потоков. Сбой между запуском и возобновлением завершает подвешенный процесс:
  исполнение `lost`, операция `failed` с `engine_unavailable`, at-most-once, как в #805.
- **Имя и права.** Имя — `Global\harness-runtime-execution-<operation_id>`: его находит runtime
  из любой сессии того же пользователя. Runtime, который открывает job по имени, берёт права
  `JOB_OBJECT_QUERY | JOB_OBJECT_TERMINATE`, helper — только `JOB_OBJECT_QUERY`. Копия handle в
  лидере держит имя живым, пока жив лидер.
- **Идентичность.** Receipt пишет pid, время создания процесса (FILETIME) и `pgid`, равный pid,
  `"accounting": "job-object"` и имя job. Процесс с записанным pid, но с другим временем
  создания исполнением не считается и сигнала не получает: `CTRL_BREAK` уходит только на консоль
  проверенного участника job, а `TerminateJobObject` касается только участников job.
- **Наблюдение.** Reconcile открывает job по имени из intent и смотрит на лидера по pid и
  времени создания. Лидер жив и в job — `running`; жив вне job или при пустом job — `unknown`;
  лидер ушёл, а участники живы — `running` до их выхода или до срока; пустой job — `exited` с
  кодом удерживаемого handle или `lost`; ошибка ОС — `unknown` с удержанной арендой. Живого
  усыновлённого лидера runtime держит открытым handle до терминального состояния строки.
- **Остановка по сроку.** Runtime запускает helper `harness-runtime ctrl-break --job --group
  --pid --created` и ждёт его до 10 секунд. Helper игнорирует консольные события, открывает job,
  выбирает проверенного лидера или первого живого участника, подключается к его консоли и шлёт
  `CTRL_BREAK_EVENT` записанной группе. Коды выхода helper: 0 — доставлено, 3 — нет живого
  участника, 1 — не доставлено. Через `stop_grace_seconds` оставшихся участников завершает
  `TerminateJobObject`; если job не опустел за 30 секунд, исход `unknown`. Затем одна транзакция
  пишет `exited` с exit evidence (код выхода как беззнаковый DWORD, `signal: null`, источник,
  время шагов, `ctrl_break`, размер и SHA-256 журналов) и освобождает аренду. Остановку, начатую
  до рестарта, новый runtime доводит до конца: повторяет `CTRL_BREAK`, пока идёт grace, затем
  `TerminateJobObject`.
- **Проверки.** Задание CI `runtime-ipc-windows` запускает контрактные тесты `platform-ffi`
  (лимиты и DACL job, жизнь имени по handle, возобновление, участие потомков, код выхода 259 из
  handle), `supervisor` (запуск в job, идентичность, отказ ОС, чужое имя, отказ контейнера) и
  `harness-runtime --test windows_supervisor`: `CTRL_BREAK` и `TerminateJobObject`, исполнение,
  которое пережило убитый runtime, код выхода после рестарта, завершение начатой остановки,
  усыновление без второго запуска, `lost` после intent и процесс с подделанной идентичностью,
  который сигнала не получает. Не доказаны: Python seam 1 на Windows, рестарт в другой сессии
  или под другим пользователем, захват имени чужим пользователем, настоящий pid reuse ОС, runtime
  во внешнем job с `KILL_ON_JOB_CLOSE`, реальный worker под `CTRL_BREAK` и Windows ARM. Окно в
  микросекунды между `CreateProcess` и `AssignProcessToJobObject` при гибели runtime оставляет
  подвешенный процесс вне job: `PROC_THREAD_ATTRIBUTE_JOB_LIST` на стабильном std недоступен.

## Решения реализации #909

**Принято оператором (2026-10-30).** Исправление #909 уточняет раздел 3 и решения #805 для
Linux: группа процессов, в которой остались только зомби, не держит аренду, а остановка по сроку
не бросает своего ребёнка без `wait`. Supervisor на Windows (#806) получает только общую функцию
платформенного слоя, и поведение Job Object не меняется.

| Текст ADR | Решение #909 | Причина |
|---|---|---|
| #805: участник группы жив, пока группу достигает `killpg(pgid, 0)` | `killpg` остаётся быстрым доказательством пустоты (ESRCH). Если он отвечает успехом или EPERM, решает `process::members`: два прохода по `/proc` подряд, участники выбираются по полю 5 (`pgrp`) `/proc/<pid>/stat`; группа пуста, только если ни один проход не нашёл живого участника | `killpg` успешен для группы из одного несобранного зомби, поэтому исполнение оставалось `running` или `unknown` с удержанной арендой до рестарта runtime. |
| Задача #909: зомби — процесс в состоянии `Z` или `X` по `/proc/<pid>/stat` | Участник считается зомби, только если все его потоки в `/proc/<pid>/task` в состоянии `Z` или `X` | Процесс, главный поток которого вызвал `pthread_exit`, показывает `Z` в `/proc/<pid>/stat`, пока другой поток работает. Правило по одному `stat` освободило бы аренду работающего процесса. |
| Задача #909: группа из одних зомби пуста | Пустоту доказывает только procfs PID namespace самого runtime (`/proc/self` называет pid runtime), смонтированный без `hidepid` (решает последнее монтирование `proc` в `/proc`; значения `0` и `off` не скрывают процессы), и только проходы без ошибок чтения. Иначе группа из одних зомби считается живой, как до #909 | При `hidepid` процессы других пользователей скрыты, а procfs чужого namespace показывает другие процессы, поэтому отсутствие живого участника не доказано. Ответ без доказательства не становится ошибкой: `wait_for` считает ошибку `members_alive` отсутствием выжившего участника и записал бы `exited` без доказательства. |
| #805: отказ записи `running → stopping` после SIGTERM | Запись `stopping` идёт по правилам `Supervisor::record`: повторяемый отказ повторяется в пределах `RECORD_ATTEMPTS` × `RECORD_RETRY`, отказ навсегда оставляет строку `unknown` с фактом `unrecorded: stopping` и удержанной арендой, и SIGKILL не уходит | Из `unknown` разрешено только повторное наблюдение (раздел 2). Раньше остановка завершалась молча, наблюдатель бросал `Child` без `wait`, лидер оставался зомби, а строка — `running`. |
| §3 и #805: exit evidence — статус ожидания ребёнка runtime | После отказа навсегда записи `stopping` статус подобранного ребёнка не записывается: reconcile доводит строку до `lost`, а не до `exited` | Запись `unknown → exited` из потока подбора гонялась бы с reconcile и с остановкой усыновлённого процесса и разошлась бы с Windows. |

Как работает реализация:

- **Проход по `/proc`.** Процесс или поток, который исчез во время прохода (чтение даёт
  `NotFound` или ESRCH), пропускается. Любая другая ошибка чтения или строка `stat`, которую
  нельзя разобрать, лишает проход доказательства. Второй проход видит участника, который во время
  первого породил ребёнка с меньшим pid (после перехода счётчика pid) и вышел. Доверие к `/proc`
  проверяется только перед ответом «нет живого участника»; путь с живым участником его не читает.
- **Безопасность.** Группа с живым участником держит аренду, пока все участники не выйдут или пока
  остановка не будет доказана. Правило переиспользованного номера группы не меняется: живой
  процесс на pid лидера доказывает, что группа исполнения ушла. Ядро освобождает номер, только
  когда ни одна задача не ссылается на него как на PID, PGID или SID, поэтому зомби-участник
  резервирует номер группы. Pid сам по себе ничего не доказывает: `identity::probe` отвечает
  `Absent` для зомби по одному pid, а пустоту группы доказывают только ESRCH или доверенный
  двойной проход. Правило группы применяется только после вердикта `owner-dead` или
  `owner-pid-reused` при совпавших host, boot и PID namespace; процесс другого boot или PID
  namespace без доказательства смерти по-прежнему даёт `unknown`.
- **Усыновлённый лидер-зомби.** `observe` в `linux.rs` не меняется: лидер-зомби даёт
  `owner-dead`, группа пуста, строка становится `lost`, и аренда освобождается в той же
  транзакции. Строку `stopping` остановка доводит до `exited` с `stop-observed`. Зомби остаётся в
  таблице процессов у своего родителя, потому что чужого ребёнка runtime подобрать не может.
- **Подбор своего ребёнка.** Новая функция платформенного слоя `os::reap` на Linux запускает
  поток, который вызывает `wait()`, если `try_wait` ещё не дал статус; на Windows она закрывает
  handles, как раньше делал конец `wait_for`. `wait_for` вызывает её, когда остановка закончилась
  без доказательства выхода и когда статус ожидания нельзя прочитать. Лидер, завершённый SIGTERM,
  подбирается, и reconcile переводит строку `unknown → lost`. Лидера, который проигнорировал
  SIGTERM, reconcile возвращает в `running`, после срока остановка усыновлённого процесса шлёт
  SIGTERM, пишет `stopping` и шлёт SIGKILL, поток подбора собирает лидера, а остановка доказывает
  выход как `stop-observed`.
- **Остаточный риск и стоимость.** Процесс предкового PID namespace, который сам вошёл в группу,
  достижим для `killpg`, но невидим в `/proc` runtime. Проход читает `stat` до первого живого
  участника, а `wait_for` повторяет его каждые 50 мс, пока живой участник переживает лидера.
- **Проверки.** Unit-тесты `supervisor`: `a_group_whose_only_member_is_a_zombie_is_empty` (на
  382cb2a ответ `Ok(true)`), `a_live_member_holds_the_group_of_a_zombie_leader` и разбор
  `hidepid`. Интеграционный тест `supervisor/tests/stop.rs`
  `a_stopping_record_refused_for_good_leaves_the_execution_unknown_and_reaps_the_child`: триггер
  SQL отказывает в `stopping` навсегда, строка становится `unknown`, лидер подобран, затем `lost`;
  на 382cb2a строка остаётся `running`. Seam-тест на реальном бинаре
  `test_a_refused_stopping_record_after_the_sigterm_is_retried_and_the_child_is_reaped` ставит
  debug-hook `execution.stop:stopping` (`HARNESS_RUNTIME_TEST_PAUSE_AT` и
  `HARNESS_RUNTIME_TEST_FAIL_AT`): первая запись получает повторяемый отказ, повтор пишет
  `stopping`, исход — `exited` с `wait-status` и сигналом 15. Hook существует только вместе с
  исправлением, поэтому на 382cb2a seam-тест падает на ожидании паузы; с hook, но без исправления
  исполнение заканчивается `lost` вместо `exited`. Не доказаны: проход на настоящем монтировании с
  `hidepid` и на procfs чужого namespace (проверен только разбор опций) и `os::reap` на Windows,
  который собирается только в Windows CI.
