## 1. Название и локализация
- **Английское название:** hook_response.py (hook verdict envelope)
- **Русский перевод / Суть:**

```text
Конверт ответа управляемых hooks: JSON-вердикт, код возврата по решению и профиль политики hooks проекта.
```
## 2. Полное описание
Каждый управляемый hook событий (`block-dangerous-git`, `block-direct-master`, `block-pr-merge`, `block-public-attribution`, `require-qa-gate`, `protect-hook-policy`, `check-branch-name`, `check-worktree-branch-name`, `require-bounded-check`, `block-scratch-outside-docs-tasks`, `count-skill-usage`, `mark-qa-gate-passed`) в начале сценария перезапускает себя через `hook_response.py run <hook_id> <bash> <сценарий>`. Модуль читает stdin вызова, запускает исходный сценарий с тем же stdin, передаёт его stdout и stderr без изменений и последней строкой stderr печатает вердикт:

```json
{"hook_id":"block-pr-merge","profile":"standard","decision":"block","reason_code":"pr-merge","remedy":"…","evidence_ref":".claude/hooks/block-pr-merge.sh"}
```

- **Причина блокировки.** `reason_code` блокировки — код правила hook. Сценарий с несколькими правилами (`protect-hook-policy`) называет причину префиксом `[reason_code] ` в начале stderr; незарегистрированный префикс игнорируется.
- **Решение и код возврата.** `allow` и `warn` — код `0`, `block` — код `2`. Вердикт идёт в stderr, а не в stdout: Claude Code разбирает stdout при коде `0` как собственный JSON-протокол, и значения `allow`/`warn` в нём не определены.
- **Код исходного сценария.** `0` — `allow` с `reason_code` `ok`; `2` — блокировка с `reason_code` правила этого hook; любой другой код, сбой запуска или завершение по сигналу — внутренняя ошибка.
- **Критичность (ADR 0024).** Критичные hooks (`block-public-attribution`, `block-direct-master`, `block-pr-merge`, `protect-hook-policy`, `check-branch-name`, `check-worktree-branch-name`, `block-dangerous-git`, `block-scratch-outside-docs-tasks`, `require-qa-gate`) при внутренней ошибке блокируют (`block`, `internal-error`, код `2`), в том числе при сбое самого конверта. Поведенческий `require-bounded-check` и учётные `count-skill-usage`, `mark-qa-gate-passed` при внутренней ошибке разрешают с предупреждением (`warn`, код `0`); учётные hooks по замыслу не блокируют, поэтому их код `2` тоже считается внутренней ошибкой.
- **Профиль.** Берётся только из `hook_policy.profile` файла `.harness/project.json` проекта, в котором лежит `.claude/hooks/hook_response.py`. Переменные окружения, включая `HOOK_PROFILE`, и параметры инструмента не читаются. Проект без ключа, с нечитаемым файлом или неизвестным значением получает `standard`. Эскалации по ролям и путям конверт не вычисляет: hook не знает роль, а пути команды неизвестны.
- **Что меняет профиль.** `minimal` понижает блокировку поведенческого hook (`require-bounded-check`) до предупреждения (`warn`, код `0`, `reason_code` правила сохраняется). Критичные hooks блокируют в любом профиле. `standard` и `strict` сохраняют все решения hooks без изменений; проект без `hook_policy` ведёт себя как `standard`. Скоупный hook субагента test-author (`test-author-guard.py`) подключён в описании агента, отвечает собственным протоколом мягкого отказа и через конверт не проходит.
- **Реестр.** Значения `hook_id`, `profile`, `decision` и `reason_code` зарегистрированы в `harness/orchestration/system_fields.v1.json` (поля `hook.id`, `hook.profile`, `hook.decision`, `hook.reason_code`). `parse_hook_response` в `harness/orchestration/hook_policy.py` читает вердикт как последнюю непустую строку вывода и отклоняет незарегистрированное значение ошибкой контракта.
- **Без конверта.** Если Python или `hook_response.py` недоступны, сценарий выполняется напрямую и решает сам, как раньше: критичные сценарии без Python по-прежнему блокируют.
- **Маркер повторного запуска.** Исходный сценарий запускается с переменной `HARNESS_HOOK_ENVELOPE=1`, чтобы не обернуть себя второй раз.

## 3. Контракты
- **Вход (Input/Brief):** `run <hook_id> <команда...>`; `hook_id` обязан быть зарегистрирован. JSON-payload вызова инструмента — через stdin.
- **Выход (Output/Report):**
  - **Разрешение/предупреждение:** код `0`, последняя строка stderr — вердикт с `decision` `allow` или `warn`.
  - **Блокировка:** код `2`, последняя строка stderr — вердикт с `decision` `block`; перед ней стоит сообщение исходного сценария.
  - **Неизвестный `hook_id` или неверный вызов:** код `2` и сообщение в stderr без вердикта.
## Источник

[hook_response.py](../../harness/project/hooks/hook_response.py), [hook_policy.py](../../harness/orchestration/hook_policy.py)
