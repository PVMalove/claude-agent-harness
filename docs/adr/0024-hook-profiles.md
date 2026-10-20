# Профили runtime hooks: minimal, standard, strict

**Статус: ACCEPTED (2026-10-20).** Эпик #784, ADR 0021, ADR 0023, ADR 0026. Профиль управляет
поведенческой политикой hooks: предупреждениями, повторными проверками и fail-closed на границах
высокого риска. Он не управляет правами sandbox, native approval и изоляцией ОС, и ни один профиль
не ослабляет критические guard-ы.

## Контекст системы

Сейчас (`master` 3556b16):

- Hooks поставляются только для одного coding runtime: шаблон `harness/project/settings.local.json.tmpl`
  устанавливается в `.claude/settings.local.json`, скрипты — в `.claude/hooks/` (`SEED_FOLDERS`,
  `harness/uninstall.py:39-44`). Шаблона для `.codex/hooks.json` нет; путь лишь учитывается в
  `validate_integrations` (`harness/health/project_files.py:458`).
- Установка (`harness init|adopt|update`, `scaffold_pvmalove_extras`, `harness/bin/harness.py:870`)
  копирует файл, только если его нет. Отличающийся файл остаётся как есть и попадает в
  предупреждение. Файл настроек заменяется целиком или не заменяется вовсе, поэтому пользователь
  со своим hook-ом не получает новых managed hooks.
- `harness health` проверяет только наличие bash на Windows (`check_hook_bash`); присутствие,
  исполняемость и подключение hooks не проверяются.
- Ключей профиля, уровня строгости или режима отказа нет ни в одной схеме. Fail-closed —
  соглашение кода: блокирующие hooks при внутренней ошибке завершаются с кодом 2
  (`qa-gate-state.py:292-296`, `block-direct-master.sh:5-6`).
- Digest политики hooks в brief нет; `allowed_tools` в brief есть, но в transition digest не
  входит.

Текущие hooks:

| Класс | Hooks |
|---|---|
| Критические, блокируют | `block-public-attribution`, `block-direct-master`, `block-pr-merge`, `check-branch-name`, `check-worktree-branch-name`, `block-dangerous-git`, `block-scratch-outside-docs-tasks`, `require-qa-gate`, `test-author-guard` |
| Поведенческие | `require-bounded-check` (не даёт запускать полный набор в обход `test_summary.py`) |
| Учётные, не блокируют | `count-skill-usage`, `mark-qa-gate-passed` |

## Действующий контракт

### 1. Конфигурация

- `.harness/project.json` получает необязательный ключ `hook_policy`:
  `{profile: minimal|standard|strict, escalations: [{paths, roles, profile}]}`. Схема
  `harness/project/project.schema.json` и `validate_project_json` меняются вместе, как требует
  AGENTS.md.
- Без ключа действует `standard`. Его блокирующий набор равен сегодняшнему, поэтому существующие
  проекты не меняют поведения без явной миграции. Новый проект через `start-project` и
  `integrate-project` получает `standard` явно.
- `escalations` только повышают профиль для путей или ролей; понижение отвергается валидатором.
  Порядок: базовый профиль → самая строгая подходящая эскалация.
- Источник профиля — только `hook_policy` в `.harness/project.json`. Переменные окружения
  (`HOOK_PROFILE` и подобные), параметры инструментов и текст брифа или отчёта игнорируются.
- Изменение `hook_policy`, `.claude/settings*.json`, `.codex/hooks.json` и каталогов hooks
  инструментом агента блокирует критический guard `protect-hook-policy` во всех профилях. Такие
  изменения делает человек.
  - Для `.harness/project.json` guard сравнивает ключ, а не файл: `Write`/`Edit` разрешены, если
    разобранный `hook_policy` до и после правки совпадает. Неразбираемый JSON до или после правки —
    блок.
  - Запись в `.harness/project.json` через shell (`sed -i`, перенаправление, `tee` и подобные)
    блокируется целиком, потому что её результат нельзя вычислить до исполнения.

### 2. Профили

| Поведение | `minimal` | `standard` | `strict` |
|---|---|---|---|
| Критические guard-ы | да | да | да |
| `require-bounded-check` | предупреждение | блокирует | блокирует |
| Уведомление ChangeImpact при первом касании рискованного пути | нет | предупреждение | предупреждение |
| Предупреждение о неотчитанных targeted-проверках | нет | да | да |
| Повторная проверка чувствительных путей при каждом касании | нет | нет | да |
| Fail-closed на границах высокого риска | нет | нет | да |
| Дедупликация повторных уведомлений | да | да | да |

- Чувствительные пути для `strict`: манифесты зависимостей и lock-файлы, `.github/**`, миграции
  схемы, auth, права доступа, сами hooks и `.harness/project.json`.
- Границы высокого риска для `strict`: `dispatch propose` review и QA, `qa run`, publish. На этих
  границах `strict` требует, чтобы `ImpactSet` (ADR 0023) не был `unknown` для чувствительных путей
  или чтобы план был `full`. Иначе — отказ с remedy.
- `strict` никогда не блокирует каждый вызов инструмента и не запускает `make verify` на каждую
  правку: планирование проверок принадлежит только `VerificationPlan`.
- Команды восстановления не блокируются ни одним профилем: `ledger lock-status`,
  `qa clear-stale-lease`, `batch rewind`, `dispatch cancel`, `release-lock --force` (последняя
  по-прежнему требует human approval).

### 2a. Матрица hooks

Hooks ставятся в проект, поэтому действуют для всех ролей (developer, review, qa, coordinator),
если в строке не указано иное. Read-only роли (review, qa) не пишут файлы, поэтому hooks на
`Write`/`Edit` у них не срабатывают; запрет записи им даёт `allowed_tools` brief, а не hook. «Блок»
— код 2, «предупр.» — код 0 с `decision: warn`.

| hook_id | Событие | Класс | `minimal` | `standard` | `strict` | Внутренняя ошибка | Проверка вне hook |
|---|---|---|---|---|---|---|---|
| `block-public-attribution` | Bash: commit, PR | критический | блок | блок | блок | блок | CI `check_public_metadata.py` |
| `block-direct-master` | Bash: commit, push | критический | блок | блок | блок | блок | `dispatch publish` пушит только ветку batch |
| `block-pr-merge` | Bash: merge PR | критический | блок | блок | блок | блок | нет |
| `check-branch-name` | Bash: создание ветки | критический | блок | блок | блок | блок | `batch create` по `branch_pattern` |
| `check-worktree-branch-name` | EnterWorktree | критический | блок | блок | блок | блок | `batch create` по `branch_pattern` |
| `block-dangerous-git` | Bash: разрушающий git | критический | блок | блок | блок | блок | нет |
| `block-scratch-outside-docs-tasks` | Write, Edit | критический | блок | блок | блок | блок | нет |
| `require-qa-gate` | Bash: создание PR | критический | блок | блок | блок | блок | `qa_evidence_verify` |
| `test-author-guard` | Write, Edit, Bash субагента test-author (роль developer) | критический | блок | блок | блок | блок | нет |
| `protect-hook-policy` (новый) | Write, Edit, Bash | критический | блок | блок | блок | блок | `hook_policy_sha256` в brief |
| `require-bounded-check` | Bash: полный прогон | поведенческий | предупр. | блок | блок | разрешает | `VerificationPlan` |
| `impact-first-touch` (новый) | Write, Edit рискованного пути | поведенческий | нет | предупр. | предупр. | разрешает | ChangeImpact-gate (ADR 0023) |
| `targeted-check-reminder` (новый) | завершение хода роли | поведенческий | нет | предупр. | предупр. | разрешает | сверка `checks_run` |
| `sensitive-path-recheck` (новый) | Write, Edit чувствительного пути | поведенческий | нет | нет | предупр. и evidence | разрешает | граница `strict` ниже |
| `count-skill-usage` | Skill | учётный | да | да | да | разрешает | — |
| `mark-qa-gate-passed` | PostToolUse Bash | учётный | да | да | да | разрешает | — |

- Вспомогательные скрипты (`record-qa-gate-pass`, `run-test`, `test-author-preflight`,
  `qa-gate-state.py`, `direct_commits.py`, `pr_commands.py`, `tracker-issue.py`) вызываются
  hooks или skills и сами событием не подключены, поэтому в матрицу не входят.
- Новые hook_id реализуют поведение из таблицы раздела 2; `protect-hook-policy` — решение
  владельца D8 (ADR 0026).
- Fail-closed на границах высокого риска `strict` — не hook, а проверка coordinator и Rust при
  `dispatch propose` review и QA, `qa run` и publish (раздел 2).
- Каждый runtime получает одни и те же скрипты. Если у runtime нет события строки, `harness health`
  (`hooks.policy`) сообщает это. Для критического hook-а защиту тогда даёт только «Проверка вне
  hook»; при значении «нет» это предупреждение health, а не тихий пропуск.
- Новые hook_id входят в реестр `systemFields.v1`; ответ hook-а с неизвестным `hook_id` или
  `reason_code` — ошибка контракта.

### 3. Исполнение и привязка к approval

- Python resolver (`harness health` и установка) вычисляет effective policy: профиль, эскалации,
  набор критических guard-ов, версии hooks. Результат пишется в сгенерированный
  `.harness/hook-policy.json`, его digest — `hook_policy_sha256`.
- `hook_policy_sha256` входит в `OPTIONAL_TRANSITION_FIELDS`, а `allowed_tools` добавляется в
  transition digest. Поэтому изменение конфига после approval не меняет запущенный dispatch и его
  QA-evidence.
- Для операций, которые исполняет Rust runtime (запуск и QA), авторитетную проверку делает модуль
  политики Rust по сохранённому `hook_policy_sha256` (ADR 0021). Push и merge решает Python; Rust
  только отвечает `qa_evidence_verify` (ADR 0021, раздел 7) и не принимает решения о публикации.
  Hook-скрипты coding runtime — адаптеры той же таблицы политики и не единственный уровень защиты.
- Адаптер для второго coding runtime (`.codex/hooks.json`) ставится из того же набора скриптов, и
  его критические guard-ы равны первому. Если hooks этого runtime недоступны, критическую
  защиту дают только проверки Rust runtime и coordinator, и `harness health` сообщает это как
  предупреждение.
- Ответ hook — JSON `{hook_id, profile, decision: allow|warn|block, reason_code, remedy,
  evidence_ref}`.
  - Коды выхода: 0 — `allow`/`warn`, 2 — `block`.
  - Критический hook при внутренней ошибке блокирует (2). Поведенческий и учётный при внутренней
    ошибке разрешают и пишут диагностику; они никогда не блокируют coordinator.
  - Дедупликация — по `(hook_id, событие, digest файла или diff, operation_id)`.
- Пути проверяются после `realpath`, с учётом symlink и hardlink, чтобы обходной путь не менял
  решение.

### 4. Установка и обновление

- `harness update` сливает файл настроек: managed-записи распознаются по пути команды
  `.claude/hooks/<имя шаблона>` и заменяются, остальные записи пользователя сохраняются.
  Конфликт (managed hook изменён пользователем) — предупреждение с перечнем, без перезаписи.
- Существующие флаги `--force-seed-file` и `--force-seed-files` сохраняют смысл.
- Повторный `update` идемпотентен и выдаёт отчёт о миграции и проверке.
- `harness health` получает проверку `hooks.policy`: effective profile и его источник,
  эскалации, присутствие, исполняемость и подключение каждого критического hook-а, расхождение с
  шаблоном.

## Операционные последствия

- **Approval.** Профиль не создаёт и не снимает native approval. Аварийный выход из `strict`
  требует аудируемого действия владельца: изменения `hook_policy` человеком и нового propose.
- **Негативные тесты (seam 2 — точка входа hook с JSON на stdin, `harness init|update|health`):**
  - матрица 3 профиля × 4 роли (developer, review, qa, coordinator);
  - критический guard не отключается в `minimal`;
  - `HOOK_PROFILE=minimal` в окружении игнорируется;
  - понижение через `escalations` отвергается;
  - правка `hook_policy` инструментом агента блокируется, а правка другого ключа
    `.harness/project.json` через `Edit` разрешена; shell-запись в этот файл блокируется;
  - symlink на защищённый путь блокируется;
  - повторный вызов того же события даёт одно уведомление;
  - изменение конфига после approval не меняет решение по запущенному dispatch;
  - `strict` не блокирует команды восстановления и не зацикливается на первом касании;
  - внутренняя ошибка поведенческого hook-а разрешает, а критического — блокирует;
  - legacy-проект без ключа ведёт себя как сейчас (сверка с `scripts/clean_room/project_hooks.py`);
  - `update` сохраняет пользовательский hook и не дублирует managed.
- **Замеры:** число вызовов hooks, дубликатов предупреждений и блокировок, время до первого
  полезного теста и время цикла developer-а по профилям. Профиль не увеличивает число полных
  прогонов.
- **Команды проверки:** `.harness/.venv/bin/python -m pytest -q tests/health
  tests/core/test_harness_cli_hardening.py tests/core/test_install_project_test_author.py
  tests/core/test_runtime_adapters.py`, `.harness/.venv/bin/python scripts/test_clean_room.py`,
  `harness health`, `make verify`.
