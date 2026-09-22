# Repo Map: offline parser bundle, изолированное Python-окружение и policy-управляемая деградация

Статус: accepted.

## Контекст системы

Агенты роли `architect`/`developer`/`code-review` и сессия `/grilling` находят нужные файлы через
`rg` и чтение, а Context Package раскрывает только Python-импорты на один уровень через regex и
stdlib `ast` (`harness/context_builder/context_builder.py`); для остальных языков берутся первые
30 строк. Обзора всего репозитория за несколько тысяч токенов нет. Целевые монолиты написаны не
только на Python. Харнесс может использовать сторонние Python-библиотеки во всех своих Python-путях;
прямые зависимости перечислены в `requirements-dev.txt`, а `uv.lock` фиксирует их разрешённый граф.
[ADR 0018](0018-harness-as-importable-package-without-pip-install.md) по-прежнему запрещает
упаковывать сам харнесс в wheel. Сравнение с Aider/Cursor: Repo Map строится парсером, а не regex.

## Действующий контракт

- **Repo Map** — чистая функция `(pinned commit, normalized seeds, budget, parser provenance,
  ignore policy, token-estimator version)`. Представление не хранится в ledger. Локальный
  content-addressed cache по этому ключу неавторитетен, проверяется хешем и очищается без
  lifecycle-эффекта. В поставке #266 это standalone CLI; переход Context Package на этот ресурс и
  удаление его regex-графа с `_extract_python_signatures` выполняются отдельной миграцией.
- **Ранжирование** без LLM: без seeds — по in-degree, с seeds — по BFS-дистанции, ничья решается по
  пути; отсечение по токен-бюджету. Текущий `ast-only` режим строит сильные import-рёбра с `kind` и
  `confidence`. Полный режим добавляет сопоставление def/ref по имени (приближение, не call graph:
  разрешение типов вне scope): unique-name-ref среднее, ambiguous-name-ref низкое и не ранжирует
  файл самостоятельно.
- **Поставка парсеров** — Python всегда разбирается встроенным `ast`; tree-sitter нужен только для
  TS/JS, Go, Java и C#. Dispatch и `context_builder` никогда не разрешают зависимости через сеть.
  Полный режим получает проверенный parser bundle из lock+hash артефактов, локального cache или
  разрешённого внутреннего registry. Bundle релиза формирует SBOM и проходит CVE-проверку.
  Харнесс использует полноценное Python-окружение со сторонними библиотеками из
  `requirements-dev.txt`; оно создаётся только в `.harness/.venv` командой `make bootstrap`.
  Разрешение и установка зависимостей происходят при bootstrap, а не во время Dispatch или Context
  Package: эти пути не обращаются к сети и не создают `.venv`, `requirements.txt` или `uv add` в
  корне целевого проекта.
- **Уровни качества и data policy:** `full` содержит parser-backed сигнатуры и связи поддержанных
  языков; `reduced` — Python `ast` и path-only сведения прочих; `minimal` — только
  policy-approved Path inventory. До сериализации применяются project-owned allowlist/denylist,
  path/symbol redaction и пределы длины; комментарии и тела функций не включаются. По умолчанию
  поведение portable. Enterprise-ограничения задаёт `repo_map_policy` в `orchestration.json`;
  отдельной сущности «профиль» нет. Policy применяет allowlist/denylist/redaction и лимиты числа
  файлов, размера blob, времени Git-вызова и token budget до сериализации.
- **Provenance и health:** Context Package получает совместимое структурированное
  `parser_provenance`: версии и хеши bundle/грамматик, ABI, hash скрипта, token-estimator version,
  quality tier и причина деградации. `harness health` показывает уровень и offline remedy, не
  скачивает зависимости и сообщает применимую policy.
- **Граница capability.** Модуль — новый ресурс `pvmalove-suite`. Поставка #266 предоставляет
  standalone CLI; последующая интеграция `/grilling`, `/to-tickets` и `context_builder` вызывает
  его подпроцессом, а не импортирует. Встроенный `ast` собирает Python всегда; установленный bundle
  разбирает остальные языки с ограничением времени и размера вывода. При отсутствии bundle CLI
  возвращает валидный деградированный JSON без сетевого вызова. Скрипт читает pinned commit через
  git, а не рабочее дерево. Бюджет токенов: константа по умолчанию, флаг `--max-tokens` и верхняя
  граница из `.harness/orchestration.json`. В output попадают hash policy, применённые лимиты и
  структурированные диагностики неразбираемых или слишком больших policy-approved файлов.
- **Типизация** ([ADR 0020](0020-mypy-strict-disallow-any-explicit.md)): типы tree-sitter не
  пересекают границу процесса. Интеграция `context_builder` валидирует типизированный JSON-контракт
  скрипта. JSON Schema поставляется рядом с CLI. Python fallback, policy, redaction и лимиты имеют
  контрактные тесты; bundle-путь тестируется в изолированной CI-задаче с проверенным offline
  артефактом.

Состав bundle, пины, матрица wheels, формат поставки, правила SBOM/CVE и typed-граница уточнены
[ADR 0024](0024-repo-map-parser-bundle-composition-and-delivery.md).

Уточняет ADR 0018: пакет остаётся без установки в wheel и `[build-system]`, но харнесс может
использовать сторонние библиотеки из собственного `.harness/.venv`; полная карта получает релизный
offline parser bundle, а не runtime-зависимость через `uv run`. Уточняет
[ADR 0016](0016-context-package-checkpoint-continuation-and-base-commit-gate.md): Context Package
получает `parser_provenance` и quality tier, а граф символов строится из Repo Map.

## Considered Options

- Tree-sitter как обязательная online-зависимость с fail-fast — один путь кода, но ломает Context
  Package в проектах без установки и расширяет supply-chain perimeter на каждый dispatch.
- `harness parsers install` с настраиваемой командой по стеку — `uv add {packages}` правит
  `pyproject.toml` и lock целевого проекта, хотя зависимости харнесса должны оставаться в его
  собственном manifest и `.harness/.venv`; в `project.json` нет `stack`, нужны новые поля и
  синхронная правка схемы, валидатора, шаблона и guide.
- universal-ctags/ast-grep — не Python-зависимость, но нужен бинарь в PATH, а ctags почти не даёт
  ссылок.
- Только stdlib (`ast` + regex) — нулевые зависимости, но качество для не-Python языков низкое.
- Настоящий call graph через LSP — разрешение типов; тяжёлая инфраструктура и недетерминизм.

## Операционные последствия

- Полная карта требует установленного проверенного bundle, а не `uv`; portable runtime безопасно
  возвращает `reduced`/`minimal`, enterprise policy принимает или ограничивает dispatch.
- Окружение харнесса — только `.harness/.venv`; корневая `.venv` не создаётся и не используется.
- Пины, hashes, SBOM и CVE-статус меняются только осознанным релизом харнесса и входят в provenance.
- Польза измеряется на фиксированном наборе задач, одинаковых моделях и commit. Считаются все
  prompt input tokens (включая карту, повторные чтения, retry и failed sessions), latency, cache
  hit rate, median и p95. Acceptance gate: не менее 25% снижения median discovery input tokens без
  статистически заметного ухудшения completion rate, post-integration defects или файлов вне scope;
  иначе фича остаётся opt-in. В отчёт идут только агрегаты, без путей и кода.
