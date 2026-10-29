# Встроенный контейнерный исполнитель

**Реализация в Rust runtime (#807).** Argv движка, запуск, наблюдение, остановку и удаление
worker-а выполняет `harness-runtime` (ADR 0022); Python проверяет план и закреплённые digest.
Четыре отступления от этого документа, принятые оператором (значения `environment` из окружения
runtime, удаление объектов при выходе исполнения, at-most-once staged launch, только эпоха
runtime store на Linux), записаны в ADR 0022, раздел «Решения реализации #807». Контейнерный
QA (#810) монтирует план суженно: каталог clean-room на запись, `git_common` только на чтение и
`cache` плана; это и другие его решения записаны в ADR 0022, раздел «Решения реализации #810».
Базовый образ, guide и smoke с настоящим Docker (#710) описывает раздел «Решения реализации #710»
ниже.

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
  `--userns=keep-id`, rootless Docker → worker с явным `--user 0:0` (UID 0 внутри отображается на
  пользователя хоста независимо от `USER` образа; sidecar остаётся без флага пользователя и
  работает от `USER` своего образа). При SELinux enforcing — `label=disable`, без
  перемаркировки файлов хоста. Движок и режим определяет `observe()` одним запросом
  `<engine> info`; удалённый Podman, Docker с userns-remap и ответ, не совпадающий с выбранным
  движком, блокируют запуск до старта worker-а. `HARNESS_EXECUTOR=docker` и имя расширения `docker`
  не меняются для всех движков. `safe.directory` и HOME задаются через env и tmpfs, поэтому образ
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
  проверена только матрицей ответов движка (argv), но не настоящим smoke, и остаётся непроверенной
  на живом движке.

## Решения реализации #710

**Принято оператором (2026-10-29).** Ticket #710 поставляет базовый образ worker-а, руководство по
настройке и smoke с настоящим Docker; Python и Rust runtime он не меняет.

| Текст ADR | Решение #710 | Причина |
|---|---|---|
| Флаги агента, включая отключение внутреннего sandbox, задаёт проектный адаптер | Пример адаптера в guide передаёт только allowlist из `brief.allowed_tools`, сохраняет native approval и не отключает sandbox агента; отключение остаётся решением проекта вне примера | Ни один пример не содержит auto-approve и обхода безопасности. |
| Smoke с настоящим Docker исключён из `make verify` | Маркер `docker_smoke` отбрасывает `addopts` в `pyproject.toml` для любого прогона pytest по умолчанию (`make verify`, `make test`, coverage, diff-coverage); CI job `docker-smoke` выбирает его явно через `-m docker_smoke`, а модуль smoke внесён в `EXCLUDED_FROM_GATE` diff-coverage | Один источник исключения: машина разработчика с Docker не запускает smoke случайно, а gate не считает отброшенные строки непокрытыми. |
| Образ собирает пользователь командой из guide | Guide поставляется только с `backend-orchestration` как `.harness/docs/container-executor.md` из `harness/docs/`, на английском, как соседние контракты агентов; корневой `docs/` в целевой проект не копируется | Guide читают агенты и пользователи целевого проекта. |

Как работает реализация:

- **`Dockerfile.sandbox`.** Debian stable slim с тегом по дате, `git`, `curl`, `jq`,
  `ca-certificates`, `python3` с `venv`, `uv` закреплённой версии и Node.js с `npm`. Образ не
  объявляет `ENTRYPOINT`, `CMD`, `USER`, `VOLUME` и `ENV` и не содержит секретов: точку входа,
  пользователя, HOME и окружение задаёт runtime. Заголовок даёт команды `docker build` и
  `podman build` и пример `FROM`; `Dockerfile.proxy` получил строку `podman build`. Оба файла —
  payload `.harness/container/` с контролем дрейфа.
- **Smoke.** `tests/orchestration/test_docker_smoke.py` гоняет публичные команды coordinator против
  настоящих `harness-runtime`, Git worktree, store и `docker`. Путь worker-а: `dispatch propose`,
  `create` и `send` с исполняемым shell-адаптером в монтировании artifacts; адаптер пишет evidence,
  а хост проверяет владельца файла (uid и gid хоста), HTTPS и HTTP к `github.com` через sidecar,
  `403` для `example.com` и отсутствие прямого выхода. Путь QA: `qa run` с gate-командами
  проверки пользователя и `exit 3` даёт отчёт `failed` и stage evidence с кодом 3. Оба пути
  проверяют, что объектов с меткой dispatch не осталось. Без движка или образов smoke
  пропускается, а `HARNESS_REQUIRE_DOCKER_SMOKE=1` превращает пропуск в провал.
- **CI job `docker-smoke`.** Job собирает `harness-runtime`, базовый образ из
  `Dockerfile.sandbox`, проектный образ worker-а `FROM` него и proxy-образ, затем запускает
  `pytest -m docker_smoke` с `HARNESS_REQUIRE_RUNTIME=1` и `HARNESS_REQUIRE_DOCKER_SMOKE=1` и
  отдаёт отчёт `junit-docker-smoke` в dashboard. Smoke зависит от внешней сети (`github.com`,
  `example.com`, реестры базовых образов).
- **Граница проверки.** Настоящий smoke покрывает только rootful Docker на runner GitHub Actions.
  Podman (rootful и rootless), rootless Docker и SELinux остаются проверенными лишь argv-матрицей и
  scripted-движком.
