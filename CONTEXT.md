# claude-agent-harness

Глоссарий терминов проекта, который разворачивает переносимый набор скиллов и правил для
coding agents. Операционные процессы и архитектурные решения находятся в `docs/agents/` и
`docs/adr/`, а не в этом файле.

## Термины

**Харнесс проекта** (project harness):
Установленный в целевой репозиторий набор инструкций, snapshot скиллов, discovery-ссылок,
реестра и lock-файлов, работающий независимо от исходного репозитория харнесса.
_Avoid_: инсталляция, харнесс без уточнения «проекта».

**Сидируемые файлы** (seed files):
Проектные файлы, которые CLI добавляет при отсутствии и после этого передаёт во владение целевому
проекту: инструкции, документы, hooks, rules, subagents и project config.
_Avoid_: шаблонные артефакты, foundation-файлы.

**Капабилити** (capability):
Именованный состав скиллов, описанный в `harness/CAPABILITIES.json`; состав может наследовать,
заменять или добавлять скиллы.
_Avoid_: пакет, набор скиллов.

В текущем составе `pvmalove-suite` 10 скиллов переопределены в `skills/first-party/pvmalove/`: `to-spec`, `to-tickets`, `implement`, `ask-matt`, `code-review`, `grilling`, `grill-me`, `grill-with-docs`, `triage`, `wayfinder`; доп. скиллы: `qa-gate`, `to-guide`, `setup-labels`, `run-workflow`.

**Vendor-скилл**:
Скилл из байт-в-байт snapshot закреплённого upstream-источника в `skills/vendor/`, связанный с
его revision, license и provenance.
_Avoid_: ванильный скилл, оригинальный скилл.

**First-party-скилл**:
Скилл, поддерживаемый в этом репозитории под `skills/first-party/`, включая новый скилл или
полную замену одноимённого vendor-скилла.
_Avoid_: кастомный скилл, если речь не о замене vendor-скилла.

**Переопределение капабилити** (capability override):
Механизм `extends`/`overrides`/`additions`, которым capability меняет источник унаследованного
скилла или добавляет новый без дублирования полного списка; подробный состав задаёт
`harness/CAPABILITIES.json`.
_Avoid_: патч, патчинг.

**Snapshot харнесса**:
Набор managed-файлов скиллов в `.harness/skills/`, зафиксированный в `.harness/harness.lock`.
_Avoid_: исходники скиллов — они находятся в `skills/` этого репозитория.

**Дрейф** (drift):
Неподтверждённое расхождение snapshot-файлов целевого проекта с ожидаемым содержимым в lock.
_Avoid_: рассинхронизация, устаревание.

**Проектный конфиг** (`.harness/project.json`):
Источник проектных значений для `qa-gate`, `pr-composer`, `code-review`, `to-guide` и branch
hooks: `language`, `base_branch`, `branch_pattern` и `qa_gate_commands`; необязателен только
`$schema`. Форма описана в `harness/project/project.schema.json`, а `harness health` применяет тот
же строгий контракт и отклоняет неизвестные поля.
_Avoid_: конфигурация проекта, settings.

**Интеграционная ветка эпика** (`integration/<service-or-team>`):
Ветка, которую `/to-spec` создаёт от `base_branch` после публикации эпика. `/to-tickets` переносит
её в дочерние тикеты; issue-ветки создаются от неё, а их PR направляются обратно в неё. Такой PR
ссылается на тикет через `Related to #<ID>`, но не закрывает его: после подтверждённого merge
тикет закрывают через CLI трекера. `Closes #<ID>` применяется только для PR в default branch;
после merge состояние тикета проверяют, поскольку GitLab может отключить или изменить closing pattern.
_Avoid_: project-level base branch, ветка реализации отдельного тикета.

**Проверка публичных метаданных** (`block-public-attribution.sh`):
Локальный PreToolUse hook, который разбирает только `tool_input.command` и проверяет содержимое
commit message, PR/MR title/body и явно переданного literal-файла сообщения; путь к этому файлу не
является метаданными. Неразбираемый payload, незакрытая кавычка или недоступный/переменный путь
блокируют публикацию; CI повторяет проверку уже опубликованных Git-метаданных.
_Avoid_: проверка всей команды, проверка пути body-файла.

**Реестр скиллов проекта** (`.harness/skills/REGISTRY.md`):
Компактный каталог имён, путей и описаний скиллов, используемый как fallback-маршрут для runtime
без native project skill root.
_Avoid_: `skills/` — это исходный каталог этого репозитория.

**Provenance-лок проекта** (`.harness/overlays/project-local.lock`):
Фиксация sha256-файлов project-owned скиллов, не покрытых выбранной capability.
_Avoid_: overlay-лок без уточнения «проекта».

**Инвентарь интеграций** (`.harness/integrations.json`):
Описание native runtime-конфигов с их путями, sha256, runtime-целями, verify-действиями и именами
секретных env-переменных; значения секретов сюда не входят.
_Avoid_: проектный конфиг, секреты.

**Skill discovery roots**:
`.agents/skills` и `.claude/skills` — runtime-ссылки на `.harness/skills`; для Hermes Agent fallback
маршрутом служат `AGENTS.md` и `.harness/skills/REGISTRY.md`.
