## 1. Название и локализация
- **Английское название:** protect-hook-policy (PreToolUse Hook)
- **Русский перевод / Суть:**

```text
Защита политики hooks: запрет агенту менять ключ hook_policy, настройки и каталоги hooks и писать в .harness/project.json через shell.
```
## 2. Полное описание
Критичный hook `PreToolUse` для Bash и инструментов файлов (`Write`, `Edit`, `MultiEdit`, `NotebookEdit`). Он блокирует во всех профилях (`minimal`, `standard`, `strict`) и не зависит от роли и окружения: `HOOK_PROFILE` и подобные переменные не читаются. Изменения политики hooks делает человек (ADR 0024, раздел 1).

Решение принимает `protect_hook_policy.py` по принципу fail closed: разрешение — только код `0` и слово `allow`; отсутствие Python, любой другой код и частичная копия модуля дают код `2`. Через конверт `hook_response.py` hook отвечает вердиктом с `hook_id` `protect-hook-policy` и одним из `reason_code`, которые сценарий называет префиксом `[reason_code]` в первой строке stderr.

**Инструменты файлов.** Путь из `file_path`/`notebook_path` разрешается через `realpath` относительно `cwd` payload и сравнивается по хвосту, поэтому symlink на защищаемый файл, каталог-symlink и другой checkout или worktree решение не меняют. Регистр имён не различается.
- `.claude/settings*.json`, `.codex/hooks.json` и всё внутри каталогов `.claude/hooks` и `.codex/hooks` — блок, `agent-config-edit`.
- `.harness/project.json` — правка разрешена, только если разобранный ключ `hook_policy` до и после неё совпадает (сравниваются разобранные значения, а не байты; отсутствие ключа тоже значение). Ключ добавлен, изменён или удалён — `hook-policy-changed`. JSON до или после правки не разбирается, не является объектом или содержит повторяющийся ключ, либо результат правки нельзя вычислить заранее (`old_string` не найден или неоднозначен, `NotebookEdit`) — `hook-policy-unparseable`. Правка других ключей разрешена.

**Bash.** Любая запись в `.harness/project.json` — блок, `project-json-shell-write`. Записью считается:
- перенаправление в этот файл (`>`, `>>`, `>|`, `&>`, `<>`, с номером дескриптора и heredoc): цели перенаправлений записывает строгий лексер `pr_commands.StrictLexer` (`strict_redirections`);
- упоминание файла аргументом любой программы, кроме читающих (`cat`, `grep`, `rg`, `head`, `tail`, `wc`, `ls`, `jq`, `diff`, `sed` без `-i`/`--in-place`, `echo`, `printf`, `cd` и безопасные подкоманды git: `status`, `diff`, `log`, `show`, `add`, `commit`, `blame`, `ls-files` и т. п.): `tee`, `cp`, `mv`, `install`, `dd of=`, `truncate`, `ln`, `sed -i`, `git checkout|restore`, код интерпретатора (`python -c`, `perl -pi`), `bash -c`;
- программа за обёрткой `command`, `env`, `sudo` или `timeout` решает так же, как без обёртки: `command ls .harness` и `timeout 5 sed -n 1,5p .harness/project.json` разрешены, `sudo cp a .harness/project.json` — блок. Опции обёртки разбираются по таблице `WRAPPERS`. Обёртка с опцией не из таблицы (`env -C`, `env -S`, `sudo -D`, `sudo -e`) сама считается пишущей программой. Присваивание `env F=.harness/project.json` перед пишущей программой — тоже упоминание;
- слово, которое указывает на каталог `.harness`, для любой пишущей программы (не из читающих; для git — не из безопасных подкоманд): `git checkout <ref> -- .harness`, `git restore --source=<ref> .harness`, `cp -r <каталог>/. .harness/`, `tar -C .harness -xf <архив>`, `unzip … -d .harness`, `rsync`, `mv .harness …`, `rm -rf .harness` заменяют или удаляют файл вместе с ключом `hook_policy`; чтение каталога (`ls`, `git -C .harness status`) и запись в другие файлы под ним (`cp a .harness/notes.txt`) разрешены;
- слово называет файл по имени `project.json`, по пути после `realpath` относительно `cwd`, корня проекта и целей `cd` команды (в том числе через symlink) или по glob (`.harness/proj*.json`).

Команда вне строгого подмножества bash (`$`, обратные кавычки, скобки) разбирается общим разбором `pr_commands.parse`; цель перенаправления в ней ищется по тексту, присваивание значения с `project.json` переменной и интерпретаторный код с этим именем блокируются. Чтение файла и запись в другие файлы разрешены.

Блокировка идёт по упоминанию, а не по доказанной записи (fail closed): пишущая программа, в аргументах которой есть текст `project.json`, получает отказ, даже если файл не пишет (`gh issue comment 1 --body "edit project.json"`, `python3 -m json.tool .harness/project.json`). Текст для tracker передавай через `--body-file`, а файл читай инструментом `Read`; поведение закреплено тестом `test_naming_the_file_without_a_write_fails_closed`.

Остаточные ограничения: запись через скрипт в файле, косвенность через переменные и программы, в аргументах которых файл не назван, и shell-запись в настройки и каталоги hooks (их защищают только инструменты файлов).

## 3. Контракты
- **Вход (Input/Brief):** JSON-payload PreToolUse через stdin: `tool_name`, `tool_input` (`command` либо `file_path`/`notebook_path` с содержимым правки) и `cwd`.
- **Выход (Output/Report):**
  - **Разрешение:** код `0`; последняя строка stderr — вердикт `allow`.
  - **Блокировка:** код `2`, сообщение с префиксом `[reason_code]` в stderr и вердикт `block`. Коды: `hook-policy-changed`, `hook-policy-unparseable`, `project-json-shell-write`, `agent-config-edit`, `hook-policy-protected` (payload не разобран или в нём нет пути и команды, модуль не дал решения). Все зарегистрированы в `harness/orchestration/system_fields.v1.json` (`hook.reason_code`).
## Источник

[protect-hook-policy.sh](../../harness/project/hooks/protect-hook-policy.sh), [protect_hook_policy.py](../../harness/project/hooks/protect_hook_policy.py), [pr_commands.py](../../harness/project/hooks/pr_commands.py), [hook_response.py](../../harness/project/hooks/hook_response.py)
