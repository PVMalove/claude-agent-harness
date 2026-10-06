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

## Проверка приватных терминов

[scripts/check_private_terms.py](../../scripts/check_private_terms.py) не даёт опубликовать
приватные термины мейнтейнера: имена клиентов, внутренние хосты, коды тикетов. Это инструмент
только этого репозитория, в архив релиза и в целевые проекты он не попадает. Проверка
выполняется в трёх местах:

- `make verify` проверяет staged diff, непубликованные коммиты (`HEAD --not --remotes`) и имя
  текущей ветки;
- repo-local PreToolUse hook из `.claude/settings.json`
  ([scripts/hooks/check-private-terms.sh](../../scripts/hooks/check-private-terms.sh)) перед
  `git commit`, `git push` и `gh issue|pr create|edit|comment` проверяет текст команды и её
  body-файлы (`-F`/`--file`/`--body-file`), а также staged diff (при `-a` — `git diff HEAD`)
  для commit, непубликованные коммиты для push и имя ветки. При совпадении команда
  блокируется. Hook начинает работать в сессиях, начатых после того, как изменение попало в
  текущую ветку;
- job `private-terms` в [verify.yml](../../.github/workflows/verify.yml) для pull request
  проверяет сообщения и добавленные строки каждого коммита PR, head-ветку, title и body.

### Список терминов

Список берётся из непустой переменной окружения `HARNESS_PRIVATE_TERMS`. Если её нет, список
читается из файла `.private-terms.txt` в корне основного checkout. Этот файл общий для всех
linked worktree и указан в `.gitignore`. Источники не объединяются. Пустая переменная
считается незаданной. Если нет ни переменной, ни файла, проверка завершается с кодом 0 и
печатает `private-terms: skipped: no term list ...`. Список из одних комментариев тоже
считается пропуском.

Формат: UTF-8 (BOM допускается), один термин на строку, пробелы по краям отбрасываются.
Пустые строки и строки, которые начинаются с `#`, пропускаются. Комментариев в конце строки
нет: `#` может быть частью термина. Номер термина в выводе равен номеру строки в источнике.
Сравнение ищет подстроку без учёта регистра в латинице и кириллице, `ё` совпадает с `е`, а
полноширинные и разложенные символы приводятся к обычным (NFKC). Поэтому термины должны быть
характерными: короткий или числовой термин даёт ложные срабатывания. Гомоглифы, zero-width
символы и термин, разорванный переносом строки, не распознаются.

```bash
cat > .private-terms.txt <<'EOF'
# synthetic example: one term per line
Zorblax
КвазиПлюх
qx-7741
EOF
chmod 600 .private-terms.txt
git check-ignore -q .private-terms.txt && echo ignored
```

`git clean -x` или `git clean -X` удаляет этот файл, после чего проверка молча переходит к
уведомлению о пропуске.

### Секрет репозитория

Job `private-terms` читает список из секрета `HARNESS_PRIVATE_TERMS` в том же формате. Создать
или обновить его можно из файла:

```bash
gh secret set HARNESS_PRIVATE_TERMS < .private-terms.txt
```

То же самое делается в Settings → Secrets and variables → Actions → New repository secret.
Секрет передаётся только в env шага проверки. В PR из форков и Dependabot секрет пустой, поэтому
job печатает `::notice` о пропуске и завершается успешно. Runner печатает env шага в лог, а
маскирование секретов в логах GitHub учитывает регистр. Поэтому в env остаётся только сам секрет:
его значение совпадает с секретом дословно и выводится как `***`. Title, body, head-ветку и
диапазон коммитов PR шаг читает из файла события `$GITHUB_EVENT_PATH`, а сама проверка никогда не
печатает термины.

### Вывод и ручной запуск

Коды выхода: 0 — совпадений нет или проверка пропущена; 1 — есть совпадения; 2 — ошибка (в hook
код 2 блокирует команду). Каждая находка печатается как `<место>: term #N`, без самого термина
и без текста строки:

- `notes.md:3` — добавленная строка staged diff, `path:line` — строка body-файла;
- `commit <sha12>:3` — строка 3 сообщения коммита (строка 1 — subject),
  `commit <sha12> path:line` — добавленная строка этого коммита;
- `branch:1` — имя ветки, `command:4` — строка текста команды в hook;
- `staged file #k`, `commit <sha12> file #k`, `commit #j`, `body file #k` — порядковые
  метки вместо пути, sha или пути body-файла, в которых есть термин; `(name)` — термин в имени
  файла. Путь в кавычках git тоже заменяется меткой.

Метку раскрывают только локально, не в CI:
`git diff --cached --name-only --no-renames | sed -n 'kp'` для `staged file #k`,
`git show --format= --name-only --no-renames <sha> | sed -n 'kp'` для `commit <sha12> file #k` и
`git log --format=%H <range> | sed -n 'jp'` для `commit #j`; `body file #k` — k-й аргумент
`--body-file`.

```bash
python scripts/check_private_terms.py --staged --commits --branch
python scripts/check_private_terms.py --commits origin/master..HEAD --body-file pr-body.md
```

Hook нужен литеральный путь: при `cd "$DIR"` или body-файле с подстановкой shell он блокирует
команду, если список есть. Команды вне Bash tool (терминал разработчика), `gh api` и правки в веб-
интерфейсе не проверяются; для PR страховкой служит CI job.

### Известные ограничения hook

Hook видит состояние до выполнения команды и разбирает shell приблизительно. Пропущенное здесь
ловят проверка push (непубликованные коммиты) и `make verify`:

- commit проверяет staged diff на момент перед командой, поэтому файл из `git add X && git commit`
  в той же команде и pathspec-коммит `git commit -m ok X` попадут в проверку только при push или
  в `make verify`;
- имена опций сравниваются точно: сокращённую длинную опцию, например `--fil=msg.md`, git
  принимает, а hook этот файл сообщения не читает;
- команда распознаётся после присваиваний, слов `if`, `then`, `elif`, `else`, `do`, `while`,
  `until`, `!`, `{`, префиксов `timeout`, `nice`, `nohup`, `command`, `exec`, `time`, `env`, а
  также внутри `$(...)` и обратных кавычек. Не распознаются `sudo`, `bash -c`, `eval`, `xargs`,
  алиасы shell, алиасы из конфигурации git и алиас из `-c alias.*`, который ссылается на другой
  алиас.
