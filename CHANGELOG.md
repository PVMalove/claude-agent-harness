# Changelog

Изменения выпусков записываются в формате [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/) и версионируются по SemVer.

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

### Changed

- Установщик глобального слоя переименован в `bin/install-global.py`: запуск `python3 bin/install-global.py ...`.
- Справочник харнесса и руководство по backend-оркестрации перенесены из `docs/agents/` в управляемый снимок `.harness/docs/{harness-guide,backend-orchestration}.md` (источник — `harness/docs/`) и обновляются командой `harness update`; прежние копии в `docs/agents/` целевого проекта больше не используются и удаляются вручную.
