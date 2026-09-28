# Релизы Agent Harness

## Версия и состав

Харнесс использует [SemVer](https://semver.org/lang/ru/): `MAJOR.MINOR.PATCH`. Одна и та же версия
записана в `harness/VERSION`, `pyproject.toml`, заголовке секции `CHANGELOG.md` и имени тега
`vMAJOR.MINOR.PATCH`. `harness/VERSION` служит проверяемым источником версии для CD.

GitHub workflow [release.yml](../../.github/workflows/release.yml) запускается при push в `master`
и при push тега строго вида `vMAJOR.MINOR.PATCH`. Для push в `master` тег выводится из
`harness/VERSION`; если такой тег уже существует, workflow завершается без публикации. Затем job
`preflight` за секунды проверяет равенство тега и `harness/VERSION`, наличие секции и её категорий в
`CHANGELOG.md` (`python scripts/build_release.py --tag vX.Y.Z --check` — ту же проверку можно
выполнить локально до merge). После этого вызываются все проверки
[verify.yml](../../.github/workflows/verify.yml), и только затем собирается архив. При ошибке Release
и тег не создаются. Успешный запуск создаёт тег на проверенном коммите (если его ещё нет) и
GitHub Release с двумя Assets:
`claude-agent-harness-vX.Y.Z.tar.gz` и `claude-agent-harness-vX.Y.Z.tar.gz.sha256`.
Тело Release Notes берётся из секции `[X.Y.Z]` в `CHANGELOG.md`.

Архив содержит `harness/`, `skills/`, `global/`, `global-skills/`, `bin/`, `third_party/`,
`README.md` и файлы лицензий. `docs/`, `tests/` и `.github/` в архив не входят.

## Выпуск

1. В issue-ветке измените `harness/VERSION` и `pyproject.toml` на одну версию. Добавьте в
   `CHANGELOG.md` секцию `[X.Y.Z]` только с теми категориями Keep a Changelog, в которых есть записи
   (`Added`, `Changed`, `Deprecated`, `Removed`, `Fixed`, `Security`, `Breaking Changes`); пустые
   категории и заглушки не пишутся, а `build_release.py` отклоняет категорию без записей. Выполните проверки и
   обычный процесс PR; merge делает разработчик.
2. После merge в `master` workflow `release` публикует версию автоматически: создаёт тег `vX.Y.Z`
   на merge-коммите и GitHub Release; ручной тег не требуется. Если автоматический выпуск не
   состоялся, после исправления причины тег можно опубликовать вручную на проверенном коммите:

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
