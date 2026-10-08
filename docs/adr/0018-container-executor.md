# Встроенный контейнерный исполнитель

## Контекст системы

`access_policy` (#615, #630) описывает режим доступа, сеть и файловые ресурсы worker-а, а
`extensions.runtime_access` должен применить этот план к реальному запуску. Поставляемой реализации
не было, и любой план, отличный от `inherit`, блокировал `dispatch send`. Внутренний sandbox агента
при этом давал EPERM на записи вне checkout (например, кэш `uv`), а снять его в текущей сессии
нельзя без ослабления native approval. Изоляцию даёт отдельный контейнер на каждый dispatch, при
условии что он исполняет тот же утверждённый план, а не расширяет его.

## Действующий контракт

- Ядро поставляет одну native-реализацию `runtime_access` под именем `docker`: контейнерный
  исполнитель (Docker или Podman, режим движка определяется в `observe()`). Остальные среды
  по-прежнему подключаются как project-owned `module:factory`.
- Исполнитель запускает только external dispatch: он оборачивает команду external-адаптера и не
  знает конкретного агента. Флаги агента (отключение внутреннего sandbox, allowlist из
  `brief.allowed_tools`) задаёт проектный адаптер. In-process транспорт с этим исполнителем даёт
  блокер.
- Native approval сохраняется: исполнитель не включает auto-approve и не подтверждает действия за
  человека. Изоляция контейнера не заменяет dispatch approval.
- Монтируются только ресурсы плана, по тем же абсолютным путям: `read` → read-only, `write` →
  read-write. Это сохраняет `gitdir` linked worktree и аргументы `--repo`/`--brief`; `/workspace`
  и перезапись путей отклонены. Тёплые кэши — только `cache`-ресурсы плана; named volumes и
  постоянный контейнер отклонены, чтобы весь доступ оставался видимым в плане.
- `network.hosts` исполняется через egress-proxy allowlist: worker подключён к internal-сети без
  прямого выхода, а proxy — sidecar-контейнер того же dispatch из отдельного proxy-образа харнесса.
- Секреты передаются только по именам переменных из компонента `environment` плана (`-e NAME`);
  значения не попадают в конфиг, brief, argv и слои образа. Auth-файлы не монтируются.
- Владелец файлов совпадает с пользователем хоста: rootful → `--user uid:gid`, rootless Podman →
  `--userns=keep-id`, rootless Docker → UID 0 внутри. При SELinux enforcing — `label=disable`, без
  перемаркировки файлов хоста. `safe.directory` и HOME задаются через env и tmpfs, поэтому образ
  worker-а может быть любым; поставляемый `Dockerfile.sandbox` — лишь типовая база.
- Конфиг: `extensions.runtime_access: "docker"` и компоненты `container`/`environment` в
  `access_policy` с теми же overrides ролей и операций. Отдельного CLI-флага нет: доступ
  фиксируется при approval, а не при send.
- В конфиге указывается тег образа. Resolver остаётся без проб; `dispatch propose` вызывает
  `observe()` и записывает digest worker- и proxy-образа в brief рядом с планом — в transition
  digest, но не в `plan_digest`. Handoff запускает образы по digest; отсутствующий или
  пересобранный образ — блокер и новый propose.
- Handoff detached: `launch_id` — id контейнера. Все объекты получают метки dispatch и репозитория;
  coordinator снимает exit code и удаляет их при terminal-состоянии dispatch, `harness cleanup`
  убирает сирот. Живой worker без явной команды не останавливается.
- Clean-room QA использует тот же исполнитель синхронно: каждая gate-команда — отдельный
  foreground-контейнер (`--rm --init`) из образа, закреплённого в brief QA-dispatch, с планом
  `operations.qa`. Exit code и потоки команды становятся её evidence; сигнал или таймаут
  coordinator-а останавливают контейнер с grace-периодом. Sidecar-proxy и сеть живут весь QA run.

## Операционные последствия

- Dockerfile-ы и proxy поставляются в payload `.harness/container/`; образ собирает пользователь
  командой из guide, харнесс образы не собирает и не скачивает.
- Хост LLM API должен быть явно перечислен в `network.hosts`; ssh-remote через proxy не работает.
- Тесты идут через публичный путь coordinator с подменённым исполнителем команд движка; smoke с
  настоящим Docker выполняется в CI и исключён из `make verify`. Поддержка Podman и rootless Docker
  остаётся непроверенной, пока для них нет smoke.
