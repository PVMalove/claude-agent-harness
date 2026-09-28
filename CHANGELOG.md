# Changelog

Изменения выпусков записываются в формате [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/) и версионируются по SemVer.

## [Unreleased]

### Added

- `harness console`, раздел Harness: команда `update --force-seed-files` и предпросмотр hard-очистки; команды очистки soft/hard (план и применение) сгруппированы после команд установки.

### Changed

- `harness console`: верхняя полоса в залитой рамке со значком меню `☰`; вывод команд в рамке «Вывод»; выбранный элемент списков, дерева и вкладок выделяется цветом без заливки фона; на экране Repo Map состояние, файлы, связи, сводка, хабы и диагностики показаны в рамках с заголовками.

### Fixed

- `harness console`: в невысоком окне панели Harness и Orchestration больше не наезжают друг на друга — списки состояний и команд прокручиваются внутри своих рамок.

## [1.0.0] - 2026-09-28

### Added

- Первый стабильный выпуск переносимого Agent Harness с capability-снимками, CLI установки, оркестрацией и Repo Map.
- GitHub Release с архивом установки, SHA-256 и заметками из этого раздела.
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
