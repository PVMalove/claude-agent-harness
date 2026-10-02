# Changelog

Изменения выпусков записываются в формате [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/) и версионируются по SemVer.

## [1.2.0] - 2026-10-02

### Added

- Память проекта (RAG): вычисляемый SQLite FTS5 кэш «как это решали раньше» на основе локальных источников (ADR, глоссарий, архив задач, Completion reports, QA findings и Lifecycle ledger).
- Команды `harness memory`: `build` (построение кэша), `search` (поиск с ранжированием BM25), `rebuild` (полная пересборка), `sync` (санитизированный snapshot тикетов и PR из трекера) и `eval` (офлайн-оценка качества).
- Замороженные указатели памяти в Context Package: подбор релевантных карточек по цели и DoD тикета с метаданными (заголовок, статус, путь, hash) без включения недоверенного текста источников; поддержка флага `--no-memory`, квот по типам источников и лимита токенов внутри бюджета пакета.
- Доказательная база памяти в Completion report: поля `used_memory` (использованные источники) и `lessons` (извлечённые уроки).
- Диагностика памяти проекта в `harness health`: проверка доступности SQLite FTS5, конфигурации `memory_policy`, свежести индекса и snapshot трекера.
- Общий индекс для linked worktree: чтение локального индекса основного checkout без повторной индексации.
- Офлайн golden-гейт качества поиска на корпусе тикетов с замером recall@k и уровня шума.
- Документация и руководство по памяти проекта (`harness/docs/project-memory.md`) и процедуре обновления существующих проектов в `harness/docs/harness-guide.md`.

### Changed

- Конфигурация проекта `.harness/project.json` поддерживает опциональные секции `memory` и `memory_policy` (по умолчанию память выключена, существующие проекты продолжают работать без opt-in).

### Fixed

- Предотвращено зависание завершения цикла asyncio в тестах консоли и добавлена ограниченная диагностика Windows verify.

## [1.1.0] - 2026-09-28

### Added

- `harness uninstall <repo>`: полное удаление харнесса из проекта — `.harness/`, discovery-ссылок, seed-файлов, `AGENTS.md`, `CLAUDE.md` и строк харнесса в `.gitignore`. Без `--apply` выводится план; `--apply --confirm UNINSTALL` применяет его, предварительно копируя изменённые проектом файлы и проектные данные `.harness/` в `.harness-uninstall-backup/<время>/`. При активных batch оркестрации удаление отклоняется.
- `harness console`, раздел Harness: план и применение удаления харнесса (ввод `UNINSTALL`).

## [1.0.1] - 2026-09-28

### Added

- `harness console`, раздел Harness: команды `update --force-managed-files` и `update --force-seed-files`, предпросмотр hard-очистки; команды очистки soft/hard (план и применение) сгруппированы после команд установки.

### Changed

- `harness console`: фон задан только у экрана — остальные элементы прозрачны и разделены рамками с заголовками (верхняя полоса со значком меню `☰`, «Вывод» команд, Diagnostics, Reports, отчёт, хронология, QA-логи, Help, Repo Map); выбранный элемент списков, дерева и вкладок и кнопка под курсором выделяются цветом, без заливки.
- `harness console`, Dashboard: в «Состоянии» перечислены ошибки и предупреждения health, факты Repo Map выводятся по одному на строку; панель прокручивается внутри рамки.
- `harness console`: кнопка «Offline checks» на Dashboard и в Diagnostics повторно запускает офлайн-проверки.

### Fixed

- `harness console`: в невысоком окне панели Harness и Orchestration больше не наезжают друг на друга — списки состояний и команд прокручиваются внутри своих рамок.

## [1.0.0] - 2026-09-28

### Added

- Первый стабильный выпуск переносимого Agent Harness с capability-снимками, CLI установки, оркестрацией и Repo Map.
- GitHub Release с архивом установки, SHA-256 и заметками из этого раздела; публикуется автоматически после merge в `master`, если тега версии из `harness/VERSION` ещё нет.
- Управляемый пример `.harness/orchestration.example.json` для `backend-orchestration`: обновляется при `init`, `adopt` и `update`; `init` один раз создаёт из него `.harness/orchestration.json`.
- Справочник оркестрации `harness/orchestration/README.md`: устройство coordinator и все поля `orchestration.json` с дефолтами.
- README: каталог всех скилов с описаниями на русском, инструкции `harness health` и `harness console`.
- Русские описания проектных агентов в `docs/agents/` с полным переводом манифестов и `docs/agents/README.md`.
- `harness console`: тёплая тема в палитре терракоты и янтаря, знак харнесса с описанием установки на главном экране, раскраска статусов в Diagnostics и раздел `Help` (также `F1`).
- `harness console`: в разделе Harness — состояние установки и использование пайплайна, в Orchestration — статистика пайплайна и история batch с фильтром по состоянию и переходом к хронологии.
- Переписан справочник `harness-guide.md`: навигация по задачам, таблицы-шпаргалки, примеры команд и вывода, восемь новых диаграмм Archify (выбор команды установки, `/to-spec`, `/wayfinder`, `/fast-implement`, `/tdd`, метки `status::*`, два сквозных примера).
- Все 23 диаграммы в `docs/diagrams/` проходят `archify finalize` (validate, deliver, check, browser-check); превью перегенерированы, картинки справочника поставляются рядом с ним в `harness/docs/diagrams/`, а `scripts/verify.py` сверяет их с `docs/diagrams/previews/`.

### Changed

- Установщик глобального слоя переименован в `bin/install-global.py`: запуск `python3 bin/install-global.py ...`.
- Справочник харнесса и руководство по backend-оркестрации перенесены из `docs/agents/` в управляемый снимок `.harness/docs/{harness-guide,backend-orchestration}.md` (источник — `harness/docs/`) и обновляются командой `harness update`; прежние копии в `docs/agents/` целевого проекта больше не используются и удаляются вручную.
