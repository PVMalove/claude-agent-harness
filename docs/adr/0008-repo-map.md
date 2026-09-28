# Repo Map и проверенный parser bundle

## Контекст системы

Context Package нужен ограниченный обзор кода по закреплённому commit, одинаковый для
повторного запуска и независимый от незакоммиченных файлов. Синтаксический разбор нескольких
языков использует бинарные грамматики, которые должны исполняться вне основного процесса и
не изменять зависимости целевого проекта.

## Действующий контракт

`harness/repo_map/` строит карту по pinned commit, нормализованным seeds, token budget,
project-owned `repo_map_policy` и provenance парсера. `git_source.py` читает tracked пути и blob
через Git; policy до сериализации ограничивает allow/deny/redact paths и symbols, число и размер
файлов, длину полей, время Git-вызовов и бюджет. Файлы ранжируются детерминированно по seeds и
связям. Content-addressed cache проверяется при чтении и не является источником истины.

Карта имеет два уровня:

| Уровень | Содержимое |
|---|---|
| `full` | Разрешённые пути, parser-backed сигнатуры, import и name-reference связи с provenance. |
| `minimal` | Разрешённый Path inventory и явная причина отсутствия семантического разбора. |

Полный режим использует tree-sitter worker в отдельном процессе для Python (`.py`), TypeScript
(`.ts`), TSX (`.tsx`), JavaScript (`.js`, `.jsx`), Go (`.go`), Java (`.java`) и C# (`.cs`).
Python тоже требует bundle; stdlib `ast` не заменяет его. Import edges разрешаются для Python
и статических относительных импортов TS/JS; bare packages, aliases и динамические импорты
не выводятся. Для Go, Java и C# import edges не строятся без анализа структуры проекта.
Сопоставления def/ref по имени являются приближёнными и не представляют call graph.
Неподдержанные, бинарные и слишком большие файлы остаются в списке путей без сигнатур.
Комментарии и тела функций в карту не включаются.

`repo_map.py` зависит от протокола `ParserBackend`. `parser_bundle.py` находит один bundle для
ключа кэша и разбора, проверяет lock и SHA-256 wheels и устанавливает грамматики офлайн через
`uv` под межпроцессной блокировкой. Через границу worker проходит типизированный JSON, а
`context_builder` валидирует `repo_map.schema.json` и применяет ту же policy к diff и другим
сырым выдержкам. Вывод включает quality tier, причину деградации и parser provenance.

Релизный набор закрепляет `tree-sitter` и грамматики Python, TypeScript/TSX, JavaScript,
Go, Java и C#. Матрица `scripts/release_parser_bundle.py` охватывает Python 3.12–3.14
на `win_amd64`, `linux_x86_64` и `macos_arm64`; манифест wheels хранит SHA-256.
`verify.yml` собирает проверенный CI bundle и отдельно проверяет worker и языковые тесты.
Ручной `release-parser-bundle.yml` собирает, аудирует и прикладывает parser bundle к
существующему GitHub Release.

## Операционные последствия

При отсутствии bundle, неверном хеше или ошибке worker Repo Map сообщает `minimal` и причину
без сетевой установки во время dispatch. Проект может задать минимально допустимый tier по
роли; coordinator проверяет его при `dispatch propose` и `create`. Новые грамматики,
пины, пары платформ или policy проходят проверки lock, offline-установки, worker, JSON-схемы,
provenance и clean-room пути. Текст этой карты не считается telemetry модели.
