#!/bin/bash
# PreToolUse(Bash): "Zero Auto-Merge" from docs/agents/git-workflow.md, enforced deterministically.
# pr_commands.py blocks `gh pr merge` and `glab mr merge`/`accept` text unless every command bash
# would run is an allowlisted inert one (echo, cat, grep, git commit, gh pr comment, ...) or the
# text is only heredoc data: a quoted-delimiter body that cat or tee writes to a file with its own
# stdout redirection, outside a pipeline, while no other part of the command names that file,
# the writer or exec. A heredoc fed to a shell or an interpreter (bash, python3 -) stays blocked.
# Python decides every call; only its exit 0 with the word `allow` lets the command run, so a
# missing interpreter, any nonzero status or a partial copy of the helper blocks (fail closed).
# Verdict envelope (hook_response.py): reruns this script, answers with the JSON verdict and the
# exit code of the hook policy. Without Python or the helper the script below decides alone.
if [ -z "${HARNESS_HOOK_ENVELOPE:-}" ]; then
  ENVELOPE_DIR="$(cd "$(dirname "$0")" && pwd)"
  ENVELOPE_PY="$(command -v python3 || command -v python)"
  if [ -n "$ENVELOPE_PY" ] && [ -f "$ENVELOPE_DIR/hook_response.py" ]; then
    exec "$ENVELOPE_PY" "$ENVELOPE_DIR/hook_response.py" run block-pr-merge "${BASH:-bash}" "$0"
  fi
fi
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$(command -v python3 || command -v python)"
if [ -z "$PY" ]; then
  echo "Zero Auto-Merge: Python 3 не найден, команду нельзя проверить." >&2
  exit 2
fi

if VERDICT="$("$PY" "$SCRIPT_DIR/pr_commands.py" merge)" && [ "$VERDICT" = "allow" ]; then
  exit 0
fi
echo "Zero Auto-Merge: 'gh pr merge' и 'glab mr merge'/'accept' запрещены агенту — мердж в основную ветку выполняет только разработчик вручную (docs/agents/git-workflow.md). Упоминание merge допускается только в командах echo/printf/cat/grep/head/tail/wc, git commit и текстовых подкомандах gh/glab без \$, обратных кавычек, комментариев и скобок. В цепочке с другими командами merge-текст допустим только в теле heredoc с разделителем в кавычках (<<'EOF'), которое cat или tee записывает в файл своим перенаправлением stdout (> файл) вне конвейера, если этот файл, cat/tee и exec не называет другая часть команды. Heredoc на stdin оболочки или интерпретатора (bash, python3 -) и запуск записанного файла блокируются." >&2
exit 2
