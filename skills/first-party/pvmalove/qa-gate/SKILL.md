---
name: qa-gate
description: Run this project's full local quality gate (lint/typecheck/test commands from .harness/project.json) and report pass/fail. Use before opening a PR, or whenever asked to run the full check/test suite.
context: fork
background: false
---

Run this project's quality gate and report the result. Nothing else — no restating the commands, no unrelated commentary.

1. Read `qa_gate_commands` from `.harness/project.json` — an ordered list of shell commands (e.g. `["make check", "make test"]` or `["ruff check .", "mypy .", "pytest"]`). If the file or field is missing, tell the user to fill it in (run `harness init`/`update` again, or edit `.harness/project.json` directly) and stop.
2. Run each command in order through the installed agent-facing wrapper:

   ```bash
   python .harness/skills/qa-gate/scripts/test_summary.py -- bash -lc '<exact command from qa_gate_commands>'
   ```

   On the first failure, stop. Return the wrapper's bounded summary — status, pytest totals, failed
   node IDs, and its sanitized local log path — rather than command output. Do not run the remaining
   commands. Read a failure log only when the developer needs that particular diagnostic evidence.
3. If every command succeeds, record the pass marker yourself: run `bash "$CLAUDE_PROJECT_DIR/.claude/hooks/record-qa-gate-pass.sh"`. Don't rely on the `mark-qa-gate-passed.sh` PostToolUse hook alone for this — this skill runs forked (see `context: fork` above), and the hook isn't guaranteed to fire for Bash calls made inside that fork.
4. Report a compact one-line PASS summary only — do not paste the passing output.
\n<!--
Краткое описание (Summary): Навык запускает проверки качества кода (линтинг, типизация) и тесты проекта, опираясь на конфигурацию `qa_gate_commands`, и сообщает результаты, записывая факт успешного прохождения.

Перевод:
---
name: qa-gate
description: Запускает полный локальный контроль качества этого проекта (команды линтинга/проверки типов/тестирования из .harness/project.json) и сообщает об успехе/неудаче. Используйте перед открытием PR, или когда вас просят запустить полный набор проверок/тестов.
context: fork
background: false
---

Запустите контроль качества этого проекта и сообщите результат. Больше ничего — никакого повторения команд, никаких посторонних комментариев.

1. Прочитайте `qa_gate_commands` из `.harness/project.json` — упорядоченный список shell-команд (например, `["make check", "make test"]` или `["ruff check .", "mypy .", "pytest"]`). Если файл или поле отсутствуют, скажите пользователю заполнить его (запустите `harness init`/`update` снова, или отредактируйте `.harness/project.json` напрямую) и остановитесь.
2. Запустите каждую команду по порядку через установленную обертку для агента:

   ```bash
   python .harness/skills/qa-gate/scripts/test_summary.py -- bash -lc '<точная команда из qa_gate_commands>'
   ```

   При первой же ошибке остановитесь. Верните ограниченную сводку обертки — статус, итоги pytest, ID упавших узлов (nodes), и ее очищенный локальный путь к логу — вместо вывода команды. Не запускайте оставшиеся команды. Читайте лог ошибки только когда разработчику нужны эти конкретные диагностические данные.
3. Если каждая команда завершается успешно, запишите отметку об успехе самостоятельно: выполните `bash "$CLAUDE_PROJECT_DIR/.claude/hooks/record-qa-gate-pass.sh"`. Не полагайтесь только на хук PostToolUse `mark-qa-gate-passed.sh` для этого — этот навык запускается в ответвлении (fork) (см. `context: fork` выше), и нет гарантии, что хук сработает для вызовов Bash, сделанных внутри этого ответвления.
4. Сообщите только компактную однострочную сводку об УСПЕХЕ (PASS) — не вставляйте успешный вывод.
-->\n