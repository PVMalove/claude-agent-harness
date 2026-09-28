# Релизы Agent Harness

## Версия и состав

Харнесс использует [SemVer](https://semver.org/lang/ru/): `MAJOR.MINOR.PATCH`. Одна и та же версия
записана в `harness/VERSION`, `pyproject.toml`, заголовке секции `CHANGELOG.md` и имени тега
`vMAJOR.MINOR.PATCH`. `harness/VERSION` служит проверяемым источником версии для CD.

GitHub workflow [release.yml](../../.github/workflows/release.yml) запускается при push тега,
вызывает все проверки [verify.yml](../../.github/workflows/verify.yml), затем проверяет равенство
тега и `harness/VERSION`, наличие секции и трёх категорий в `CHANGELOG.md`. При ошибке Release
не создаётся. Успешный запуск создаёт GitHub Release с двумя Assets:
`claude-agent-harness-vX.Y.Z.tar.gz` и `claude-agent-harness-vX.Y.Z.tar.gz.sha256`.
Тело Release Notes берётся из секции `[X.Y.Z]` в `CHANGELOG.md`.

Архив содержит `harness/`, `skills/`, `global/`, `global-skills/`, `bin/`, `third_party/`,
`README.md` и файлы лицензий. `docs/`, `tests/` и `.github/` в архив не входят.

## Выпуск

1. В issue-ветке измените `harness/VERSION` и `pyproject.toml` на одну версию. Добавьте в
   `CHANGELOG.md` секцию `[X.Y.Z]` с `Added`, `Fixed` и `Breaking Changes`. Выполните проверки и
   обычный процесс PR; merge делает разработчик.
2. После попадания проверенного коммита в `master` разработчик создаёт на нём тег и публикует его:

   ```bash
   git switch master
   git pull --ff-only origin master
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```

3. Дождитесь успешного `release` workflow. Скачайте Assets на странице
   [GitHub Releases](https://github.com/PVMalove/claude-agent-harness/releases) или через CLI:

   ```bash
   gh release download vX.Y.Z --pattern 'claude-agent-harness-vX.Y.Z.tar.gz*'
   sha256sum --check claude-agent-harness-vX.Y.Z.tar.gz.sha256
   ```

   В PowerShell сверьте хеш из `.sha256` с выводом
   `Get-FileHash .\claude-agent-harness-vX.Y.Z.tar.gz -Algorithm SHA256`.

Для отдельного проверенного parser bundle существует ручной
[release-parser-bundle.yml](../../.github/workflows/release-parser-bundle.yml). Его запускают после
успешного основного релиза с `release_tag` на тот же коммит. Он проверяет закреплённые wheels,
собирает и аудирует bundle и прикладывает `parser-bundle.tar.gz` к существующему Release. Основной
архив устанавливается независимо от этого дополнительного Asset; Repo Map при отсутствии parser
bundle возвращает уровень `minimal`.

CD релизов реализован только для GitHub. Устанавливаемый workflow целевого проекта может
пользоваться `glab` для тикетов и merge requests; GitLab release pipeline в этом репозитории нет.
