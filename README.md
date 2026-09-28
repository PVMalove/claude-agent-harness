# Agent Harness

[![CI](https://github.com/PVMalove/claude-agent-harness/actions/workflows/verify.yml/badge.svg)](https://github.com/PVMalove/claude-agent-harness/actions/workflows/verify.yml)
[![Release](https://img.shields.io/github/v/release/PVMalove/claude-agent-harness)](https://github.com/PVMalove/claude-agent-harness/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

**v1.0.0** — переносимый харнесс для coding agents. Он разрешает выбранную capability из
закреплённых skills и ресурсов, устанавливает проверяемый снимок в целевой проект и запускает
его через нативные механизмы Claude Code и Codex. Совместимость v1.0.0 проверена для
этих двух runtime.
Термины определены в [CONTEXT.md](./CONTEXT.md), подробные инструкции — в [docs/](./docs/README.md).

## Архитектура

`harness/CAPABILITIES.json` задаёт `project-foundation`, `mattpocock-suite`, `pvmalove-suite` и
опциональную `backend-orchestration`. `harness init` разрешает выбранные skills из закреплённого
`skills/vendor/` и `skills/first-party/`, копирует их вместе с ресурсами в `.harness/` проекта
и записывает lock. Корневой `docs/` и внутренние документы принадлежат только этому исходному
репозиторию: установщик их не переносит. Проектные руководства, hooks, агенты и конфигурация
устанавливаются из отдельных шаблонов `harness/project/`.

В `pvmalove-suite` переопределены в `skills/first-party/pvmalove/`: `to-spec`, `to-tickets`, `implement`, `ask-matt`, `code-review`, `grilling`, `grill-me`, `grill-with-docs`, `triage`, `wayfinder`; доп. скиллы: `qa-gate`, `to-guide`, `setup-labels`, `to-pull-requests`, `fast-implement`, `delivery-stats`.

[![Граница исходного харнесса и целевого проекта](./docs/diagrams/previews/harness-topology.architecture.png)](./docs/diagrams/harness-topology.architecture.html)

`/implement` ведёт один тикет через architect, developer, независимое review, clean-room QA и
publish. Coordinator закрепляет Context Package и candidate SHA, проверяет свежесть базы и
привязывает approval к digest конкретного перехода. PR требует отдельного подтверждения;
merge выполняет разработчик.
После проверки `/to-pull-requests` готовит отдельный PR по правилам целевого проекта.

[![Конвейер implement](./docs/diagrams/previews/implement-pipeline.workflow.png)](./docs/diagrams/implement-pipeline.workflow.html)

Backend batches работают в отдельных worktrees; роли обмениваются неизменяемыми briefs и
reports. Tree-sitter parser запускается в отдельном worker-процессе, а временные файлы
хранятся под `.harness/.sandboxes/`: inbox ролей — в `scratch/`, тела PR и комментарии —
в `pr_body/`.

[![Изоляция процессов и временных файлов](./docs/diagrams/previews/process-isolation.architecture.png)](./docs/diagrams/process-isolation.architecture.html)

Действующие контракты — в [ADR](./docs/adr/), операции — в
[руководстве](./docs/agents/harness-guide.md) и
[правилах Git](./docs/agents/git-workflow.md).

## Быстрый старт и установка

Нужны Python 3.12 или новее и Git. Глобальный слой ставится один раз для выбранных runtime;
`--runtime` можно повторять:

```bash
python3 bin/install-global --target-home "$HOME" --runtime codex --runtime claude
```

В PowerShell:

```powershell
python bin\install-global --target-home $HOME --runtime codex --runtime claude
```

После этого `start-project` помогает создать новый проект, а `integrate-project` — включить
харнесс в существующий. Для прямой установки в Git-репозиторий:

```bash
python3 harness/bin/harness.py init /path/to/repository --capability pvmalove-suite \
  --project-type software --stack python --base-branch main --language ru \
  --qa-gate-command "make test"
python3 harness/bin/harness.py health /path/to/repository
```

В PowerShell путь и команды задаются так же:

```powershell
python harness\bin\harness.py init C:\path\to\repository --capability pvmalove-suite `
  --project-type software --stack python --base-branch main --language ru `
  --qa-gate-command "python -m pytest"
python harness\bin\harness.py health C:\path\to\repository
```

Без `--capability` устанавливается доменно-нейтральная `project-foundation`. Для полного
закреплённого upstream-набора выберите `mattpocock-suite`; `backend-orchestration` добавляет
координатор и роли поверх `pvmalove-suite`. `harness diff` показывает изменения управляемого
снимка, `harness update` обновляет его с сохранением локальных правок. Команды и варианты
параметров приведены в [руководстве](./docs/agents/harness-guide.md). В целевой проект
попадают только выбранные ресурсы `harness/`, skills и шаблоны `harness/project/`;
корневой `docs/` служит документацией этого репозитория.
Шаблоны проектных руководств при `pvmalove-suite` и `backend-orchestration` разворачиваются
из `harness/project/docs-agents/` как `docs/agents/{artifacts,backend-orchestration,git-workflow,harness-guide,issue-tracker,triage-labels,worktrees}.md`.

## Релизная политика

Версии следуют SemVer. После изменения `harness/VERSION`, `pyproject.toml` и секции
`CHANGELOG.md` разработчик публикует тег `vMAJOR.MINOR.PATCH` на проверенном коммите.
[GitHub CD](./.github/workflows/release.yml) запускает проверки `verify.yml`, сверяет тег с
версией, создаёт архив установки и SHA-256, затем публикует GitHub Release. Release Notes берутся
из секции версии в [CHANGELOG.md](./CHANGELOG.md); её отсутствие прерывает выпуск.
Архив и checksum доступны в [GitHub Releases](https://github.com/PVMalove/claude-agent-harness/releases).
Проверка после скачивания: `sha256sum --check claude-agent-harness-vX.Y.Z.tar.gz.sha256`.
Дополнительный parser bundle прикладывает ручной
[`release-parser-bundle.yml`](./.github/workflows/release-parser-bundle.yml) к уже созданному
Release того же коммита. Полная процедура описана в [releases.md](./docs/agents/releases.md).
В целевых проектах GitLab поддерживается через `glab` в workflow тикетов и merge requests;
релизный CD этого репозитория работает на GitHub.

## Структура проекта

| Путь | Назначение |
|---|---|
| `harness/` | CLI, каталог capability, runtime-модули и шаблоны целевого проекта |
| `skills/` | Закреплённый vendor-снимок и собственные skills |
| `global/`, `global-skills/`, `bin/` | Глобальный профиль, стартовые skills и установщик |
| `scripts/` | Сборка, проверка и clean-room сценарии исходного репозитория |
| `docs/` | ADR, руководства, русские описания и Archify-диаграммы исходного репозитория |
| `third_party/` | Provenance, lock и лицензии upstream |
| `.github/` | CI, CD и проверки upstream |

Лицензия проекта — [MIT](./LICENSE). Лицензия закреплённого upstream-снимка находится в
[`third_party/mattpocock-skills/LICENSE`](./third_party/mattpocock-skills/LICENSE).
