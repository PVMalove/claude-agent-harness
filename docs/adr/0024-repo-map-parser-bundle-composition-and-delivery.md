# Repo Map: состав offline parser bundle и формат его поставки

Статус: proposed (принимается после одобрения PR по #267).

## Контекст системы

[ADR 0023](0023-repo-map-tree-sitter-uv-pep723-with-degradation.md) ввёл релизный offline parser
bundle для языков, которые нельзя разобрать встроенным `ast`, но оставил без проверки состав bundle,
наличие Windows-wheels, типизацию под mypy strict, правила SBOM/CVE и формат поставки. Это решение
фиксирует результат проверки по #267 (spike 2026-09-21: Windows 11, Python 3.14.7 и 3.12.13, uv 0.12.1,
mypy 2.3.1, pip-audit 2.10.1, cyclonedx-py 7.4.0). Полный режим осуществим; допущения ADR 0023 о
составе и типах подтверждены, о ядре — уточнены (привязка к минорной версии Python).

## Действующий контракт

- **Состав и пины** (меняются только релизом харнесса): `tree-sitter==0.26.0`,
  `tree-sitter-typescript==0.23.2` (языки `language_typescript` и `language_tsx`),
  `tree-sitter-javascript==0.25.0`, `tree-sitter-go==0.25.0`, `tree-sitter-java==0.23.5`,
  `tree-sitter-c-sharp==0.23.5`, `tree-sitter-python==0.25.0` (добавлен #290: Python разбирается только
  tree-sitter; abi3-wheels `cp310-abi3` есть для всей матрицы и `win_arm64`). Все лицензии — MIT. Хеши в ADR не копируются; при расхождении версий или хешей источник истины — lock релиза, ADR фиксирует политику и стартовые пины. Lock и wheelhouse создаёт релизный процесс (#277 — первым языком), они поставляются как release-asset, а не коммитятся в репозиторий.
- **Wheels.** Только бинарные wheels (`--only-binary=:all:`), сборка из sdist запрещена. Windows
  `win_amd64` и `win_arm64` есть у всех семи пакетов. Ядро собрано отдельным wheel на каждую минорную
  версию Python (`cp312`–`cp314`, не abi3); грамматики — abi3 и не зависят от версии Python. Поэтому
  bundle — матрица, а не один артефакт: Python 3.12, 3.13, 3.14 × `win_amd64`, `linux_x86_64` (manylinux),
  `macos_arm64`. Расширение матрицы — релизное решение (wheels на PyPI для `win_arm64`, `linux_aarch64`, `macos_x86_64` есть).
- **ABI.** Диапазон ядра 0.26.0 — 13–15; грамматики: typescript/tsx 14, java 14, javascript 15,
  go 15, c-sharp 15, python 15. Конфликта нет. Версия ABI каждой грамматики и диапазон ядра входят в provenance (вместе с hash скрипта и версией token estimator из ADR 0023 — их добавляет #272).
- **Формат поставки — вариант A.** Wheelhouse-каталог на каждую пару «Python × платформа» и общий lock
  с хешами (`uv pip compile --generate-hashes`), поставляемые как release-asset харнесса. Загрузчик
  выбирает каталог по интерпретатору, который запускает разбор, и платформе; сверяет sha256 каждого
  wheel с lock и при отсутствии каталога для пары или несовпадении хеша деградирует в `minimal`
  без сети. Wheels распаковываются в изолированный каталог bundle вне целевого проекта (`uv pip install
  --offline --no-config --no-index --find-links --require-hashes --only-binary :all: --target`; pip не
  используется, отсутствие `uv` — причина деградации `uv executable unavailable`); момент (при установке
  харнесса или при первом запуске) и место распаковки определяет #272. Это относится к bundle, а не к пакету
  харнесса: «пакет без pip-установки» из ADR 0018 сохраняется. Харнесс использует собственное
  окружение `.harness/.venv` и группу `dev` из `pyproject.toml` (`uv sync --locked`); `.venv`, `requirements.txt`
  и правка `pyproject.toml` целевого проекта по-прежнему запрещены.
  Внутренний registry (вариант B) — разрешённый источник для enterprise-профиля с тем же lock; vendoring
  wheels в репозиторий (вариант C) отвергнут.
- **Typed-граница** ([ADR 0020](0020-mypy-strict-disallow-any-explicit.md)). Во всех семи пакетах есть
  `py.typed` и `.pyi`; код разбора проходит `mypy --strict --disallow-any-explicit` (`Node.text` —
  `bytes | None`, проверка на `None` обязательна). Без установленного bundle те же импорты дают
  `import-not-found`, поэтому модуль с `import tree_sitter*` не входит в `files` основного mypy-прогона
  (в CI ставится группа `dev` через `uv sync`); он проверяется отдельным mypy-прогоном в изолированной CI-задаче с
  установленным bundle. Типы tree-sitter не пересекают границу процесса: `context_builder` видит
  только типизированный JSON-контракт.
- **SBOM.** Релиз формирует CycloneDX 1.6 (`cyclonedx-py`) по lock/установленному bundle. Инструмент
  не записывает хеши компонентов, поэтому релизный шаг добавляет sha256 каждого wheel матрицы из lock (исключая sdist); SBOM входит
  в release-asset рядом с wheelhouse.
- **CVE-проверка релиза.** `pip-audit -r <lock> --require-hashes --disable-pip` с пустым
  `--cache-dir`; недоступность сети или сервиса — провал релиза (fail-closed), найденная уязвимость без
  явного исключения — тоже провал; исключения записываются в PR релиза с обоснованием. Результат входит в release-asset.

## Операционные последствия

- **Проверено spike:** wheels и их sha256 по PyPI; установка `--only-binary` на 3.14 и 3.12 без сборки из sdist; разбор
  TS, TSX, JS, Go, Java, C# без ошибок; `mypy strict` с установленным и без установленного bundle; offline
  установка из wheelhouse с `--require-hashes` (только Python 3.12, `win_amd64`); отклонение подменённого wheel; SBOM и `pip-audit` онлайн;
  `pip-audit` без сети с пустым кэшем завершается кодом 1.
- **Проверено #290 (2026-09-23):** `tree-sitter==0.26.0` + `tree-sitter-python==0.25.0` на Python 3.14,
  `win_amd64`: офлайн-установка `uv pip install --target`, разбор Python (сигнатуры, импорты, def/ref,
  ERROR-узлы), отдельный `mypy --strict --disallow-any-explicit` worker'а через `--python-executable`
  (при `MYPYPATH` mypy проверяет сами `.pyi` tree-sitter и падает на их explicit `Any`).
- **CI до release-asset (#290).** Пока релиз не публикует wheelhouse, задача `repo-map-bundle` берёт
  wheels пары `cp312` × `linux_x86_64` по закоммиченному `.github/parser-bundle-wheels.txt` (URL PyPI +
  sha256), отвергает несовпадение хеша и собирает bundle `scripts/build_parser_bundle.py`. Это
  отступление от «хеши не в репозитории» ограничено CI-входом; lock bundle по-прежнему строится
  при сборке.
- **Не проверено:** Python 3.13 (на машине нет — wheels на PyPI есть, запуск не проверен); установка на
  Linux и macOS (проверены только наличие wheels и, для `linux`, разрешение lock); `win_arm64`;
  `osv-scanner`; поведение `--target`-установки и подпроцесса с `PYTHONPATH` на этих платформах; стоимость
  cold/warm cache (замер в #268/#280). `pip-audit` без сети, но с непустым дефолтным кэшем вернул
  «нет уязвимостей» с кодом 0, поэтому релиз обязан использовать пустой `--cache-dir`.
- **#272 (загрузчик):** выбор wheelhouse по интерпретатору, запускающему подпроцесс, и платформе;
  отсутствие каталога для пары — причина деградации; provenance включает ABI грамматик, диапазон ядра и
  хеши wheels; проверка хеша не зависит от сети.
- **#277 (TS/JS, первый язык):** изолированная CI-задача ставит wheelhouse из lock офлайн, гоняет
  bundle-путь и отдельный mypy-прогон парсер-модуля; при недоступном артефакте — падает; выпускает SBOM
  и результат `pip-audit`. Для TypeScript загружаются два языка: `typescript` и `tsx`.
- **#279 (Go, Java, C#):** без изменений по объёму; новые грамматики уже входят в пины и lock.
- Обновление любого пина, версии Python или платформы матрицы — релиз харнесса с новым lock, SBOM и
  результатом CVE-проверки.

## Considered Options

- Вариант B — внутренний registry как единственный источник — сервера в проекте нет; остаётся
  допустимым источником для enterprise при том же lock.
- Вариант C — wheels в репозитории по образцу `skills/vendor/` — раздувает git на матрицу каталогов и
  противоречит духу ADR 0018.
- `tree-sitter-language-pack` (один abi3-wheel ~2,2 МБ) — не проверялся, другая provenance и заведомо шире
  нужных четырёх языков; расширяет supply-chain perimeter.
- `tree-sitter-languages` — wheels только до cp312, не подходит для `requires-python >=3.12`.
