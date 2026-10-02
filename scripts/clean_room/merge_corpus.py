"""Корпус payload'ов merge-блока для дифференциального сценария `project_hooks`.

Каждую команду корпуса, которую блокировала проверка хука до #443 (`OLD_MERGE_CHECK` по сырому
payload), `block-pr-merge.sh` должен блокировать вместе с её glab-вариантами. Исключение — формы
упоминания `EXEMPT_MENTIONS`: их снимает allowlist инертных команд, правило указано в записи.
"""

import re

OLD_MERGE_CHECK = re.compile(r'"command"\s*:\s*"[^"]*gh pr merge')

# Mention forms the old check blocks and the allowlist exempts: (id, command, allowlist rule).
EXEMPT_MENTIONS = (
    ("cat-heredoc", "cat <<'EOF'\ngh pr merge 1\nEOF", "cat; a quoted heredoc body"),
    (
        "cat-heredoc-file",
        "cat > notes.md <<'EOF'\nnever run gh pr merge\nEOF",
        "cat with a redirection; a quoted heredoc body",
    ),
    (
        "cat-plain-heredoc",
        "cat <<EOF\nnever run gh pr merge\nEOF",
        "cat; an unquoted heredoc body without $, backquotes or backslashes",
    ),
    ("echo", "echo gh pr merge 1", "echo"),
    ("printf", "printf '%s\\n' 'gh pr merge' > notes.txt", "printf with a redirection"),
    ("grep", "grep -n 'gh pr merge' docs/hooks/block-pr-merge.md", "grep"),
    ("git-commit", "git commit -m 'docs: forbid gh pr merge'", "git commit"),
    (
        "gh-pr-comment",
        "gh pr comment 1 --body 'merge by hand: gh pr merge 1'",
        "gh pr comment",
    ),
    (
        "glab-mr-note",
        "glab mr note 1 -m 'merge by hand: gh pr merge 1'",
        "glab mr note",
    ),
)

# Payloads of the #443 code review, by their payload id (its g1-g3 are the probe's g1-g3).
REVIEW_PAYLOADS = (
    ("b1", "trap 'gh pr merge 1' EXIT"),
    ("b2", "source /dev/stdin <<'EOF'\ngh pr merge 1\nEOF"),
    ("b3", "source <(echo 'gh pr merge 1')"),
    ("b4", ". /dev/stdin <<< 'gh pr merge 1'"),
    ("b5", ". <(cat <<'EOF'\ngh pr merge 1\nEOF\n)"),
    ("b6", "shopt -s expand_aliases\nalias m='gh pr merge 1'\nm"),
    ("f1", "echo 'gh pr merge 1' > m.sh; bash m.sh"),
    ("f2", "echo 'gh pr merge 1' > m.sh; . ./m.sh"),
    ("g1", "{ cat <<'EOF'\ngh pr merge 1\nEOF\n} | bash"),
    ("g2", "cat <<'EOF' |\ngh pr merge 1\nEOF\nbash"),
    ("g3", "echo $'\\' ' ; gh pr merge 1 --body '\\'"),
    ("h1", "eval $(\ncat <<'EOF'\ngh pr merge 1\nEOF\n)"),
    ("h2", "x=$(cat <<'EOF'\ngh pr merge 1\nEOF\n)\neval \"$x\""),
    ("h3", "cat <<'EOF' > m.sh\ngh pr merge 1\nEOF\nbash m.sh"),
    ("h4", "(\ncat <<'EOF'\ngh pr merge 1\nEOF\n) | bash"),
    ("h5", "cat <<'EOF' | $SHELL\ngh pr merge 1\nEOF"),
    ("i1", "python3 <<'EOF'\nimport os\nos.system('gh pr merge 1')\nEOF"),
    ("i2", "perl <<'EOF'\nsystem('gh pr merge 1');\nEOF"),
    ("i3", "python3 <<EOF\nimport os\nos.system('gh pr merge 1')\nEOF"),
    (
        "i4",
        "echo 'gh pr merge 1' | python3 -c 'import os,sys; os.system(sys.stdin.read())'",
    ),
    ("i5", "node <<'EOF'\nrequire('child_process').execSync('gh pr merge 1')\nEOF"),
    ("lb1", "trap 'glab mr merge 1' EXIT"),
    ("lg1", "{ cat <<'EOF'\nglab mr merge 1\nEOF\n} | bash"),
    ("lt1", "true # note \\\nglab mr merge 1"),
    ("lt3", "echo $(true)#; glab mr accept 1"),
    ("lv1", "x='glab mr accept 1'; $x"),
    ("p1", "echo 'gh pr merge 1' | xargs -I{} sh -c '{}'"),
    ("p2", "printf 'gh pr merge 1' | xargs -0 bash -c"),
    ("p3", "echo x | sed '1e gh pr merge 1'"),
    ("p4", "echo x | sed 's/.*/gh pr merge 1/e'"),
    ("p5", "env -S 'gh pr merge 1'"),
    ("p6", "flock m.lock -c 'gh pr merge 1'"),
    ("p7", "git -c alias.m='!gh pr merge 1' m"),
    ("p8", "awk -v c='gh pr merge 1' 'BEGIN{system(c)}'"),
    ("p9", "make -f - <<'EOF'\nall:\n\tgh pr merge 1\nEOF"),
    ("q0", "gh pr create --fill"),
    ("q0b", "gh pr create --head other --fill"),
    ("q1", "{ cat <<'EOF'\ngh pr create --head other --fill\nEOF\n} | bash"),
    ("q2", "cat <<'EOF' |\ngh pr create --head other --fill\nEOF\nbash"),
    ("q3", "true # note \\\ngh pr create --head other --fill"),
    ("q4", "echo $(true)#; gh pr create --head other --fill"),
    ("q5", "cat <<'EOF'\nx\\\nEOF\ngh pr create --head other --fill\nEOF"),
    ("q6", "{ cat <<'EOF'\nglab mr create -s other\nEOF\n} | bash"),
    ("q7", "true # note \\\nglab mr create -s other"),
    ("s1", "echo 'gh pr merge 1' | $SHELL"),
    ("s2", "echo 'gh pr merge 1' | rbash"),
    ("s3", "echo 'gh pr merge 1' | bash /dev/stdin"),
    ("s4", "echo 'gh pr merge 1' | bash -o errexit"),
    ("t1", "true # note \\\ngh pr merge 1"),
    ("t2", "cat <<'EOF'\nx\\\nEOF\ngh pr merge 1\nEOF"),
    ("t3", "echo $(true)#; gh pr merge 1"),
    ("t4", "echo $((1))#; gh pr merge 1"),
    ("t5", "x=$(date)#; gh pr merge 1"),
    ("v1", "x='gh pr merge 1'; $x"),
    ("v2", "x='gh pr merge 1'; eval \"$x\""),
    ("v3", "x='gh pr merge 1'; bash -c \"$x\""),
    ("v4", "read -r a <<< 'gh pr merge 1'; $a"),
    ("v5", "echo 'gh pr merge 1' | while read -r l; do $l; done"),
    ("v6", "eval $'gh pr merge 1'"),
    ("v7", "printf -v c 'gh pr merge 1'; $c"),
    ("z1", "gh pr -R o/r merge 1"),
    ("z2", "gh -R o/r pr merge 1"),
    ("z3", "echo 'pr merge 1' | xargs gh"),
    ("z4", "gh pr mer''ge 1"),
    ("z5", 'glab mr acc""ept 1'),
    ("z6", "gh pr {merge,} 1"),
    ("z7", "$'gh' pr merge 1"),
    ("z8", "GH=gh; $GH pr merge 1"),
    ("z9", "awk 'BEGIN{system(\"gh pr merge 1\")}'"),
    ("z10", "gh api -X PUT repos/o/r/pulls/1/merge"),
)

# Probe payloads of #443 h1-h7, which differ from the review's h1-h5.
PROBE_PAYLOADS = (
    ("probe-h1", 'echo "<<EOF"\ngh pr merge 1\nEOF'),
    ("probe-h2", "echo '<<EOF'\nglab mr merge 1\nEOF"),
    ("probe-h3", 'printf "%s" "<<X"; gh pr merge 1\nX'),
    ("probe-h4", 'cat <<"EOF"\ngh pr merge 1\nEOF'),
    ("probe-h5", "git status && gh pr merge 1"),
    ("probe-h6", "echo '<<EOF'\ngh pr merge 1\nEOF"),
    ("probe-h7", "true '<<EOF' && gh pr merge 1\nEOF"),
)


def _nested_eval(depth: int) -> str:
    """Payload `perf-nested-eval-<depth>` ревью: `eval "$(bash -c '…')"`, вложенные `depth` раз."""
    inner = "gh pr view 1 # merge"
    for _ in range(depth):
        inner = f"eval \"$(bash -c '{inner}')\""
    return inner


# The review's large payloads, rebuilt from the review's formulas: the hook must decide in time.
PERF_PAYLOADS = (
    *(
        (f"perf-heredocs-{count}", "cat <<E\nmerge\nE\n" + "cat <<E\nx\nE\n" * count)
        for count in (2000, 8000, 16000)
    ),
    *((f"perf-nested-eval-{depth}", _nested_eval(depth)) for depth in (6, 8)),
    ("perf-wide-dollar-paren-20000", "echo " + "$(true) " * 20000 + "# merge"),
)


def glab_variants(command: str) -> list[str]:
    """Команда и её варианты с `glab mr merge` и `glab mr accept` вместо `gh pr merge`."""
    return list(
        dict.fromkeys(
            command.replace("gh pr merge", replacement)
            for replacement in ("gh pr merge", "glab mr merge", "glab mr accept")
        )
    )
