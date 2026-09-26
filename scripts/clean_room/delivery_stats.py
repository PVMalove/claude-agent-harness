"""Статистика доставки: сценарий clean-room из `scripts/test_clean_room.py`."""

import json
import re
import shutil
import subprocess
import sys
from types import SimpleNamespace


def run(ctx: SimpleNamespace) -> None:
    """Статистика доставки.

    Читает из контекста: `pv_project`, `test_root`.
    Передаёт дальше: `claude_dir`, `report`, `stats_cli`, `stats_repo`.
    """
    pv_project = ctx.pv_project
    test_root = ctx.test_root
    # delivery-stats reads local session logs and git history and must never invent a figure: a
    # missing rate card, a missing runtime log and an unattributable Codex session each have to come
    # back as "no data" rather than as zero.
    # Run the resource from an actually installed pvmalove-suite harness, not the source tree.
    # This keeps delivery-stats coverage on the installed-harness seam it promises to users.
    stats_cli = pv_project / ".harness" / "reporting" / "delivery_stats.py"
    if not stats_cli.is_file():
        sys.exit("delivery-stats CLI missing")
    stats_repo = test_root / "stats_project"
    stats_repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=stats_repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=stats_repo,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Clean Room"], cwd=stats_repo, check=True
    )
    (stats_repo / ".harness").mkdir()
    (stats_repo / ".harness" / "project.json").write_text(
        json.dumps(
            {
                "language": "ru",
                "base_branch": "main",
                "branch_pattern": "^feature/issue-[0-9]+-.+",
                "qa_gate_commands": ["echo test"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (stats_repo / "app.py").write_text("print('base')\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=stats_repo, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "chore: baseline"], cwd=stats_repo, check=True
    )
    subprocess.run(["git", "branch", "-M", "main"], cwd=stats_repo, check=True)
    subprocess.run(
        ["git", "checkout", "-q", "-b", "feature/issue-7-widget"],
        cwd=stats_repo,
        check=True,
    )
    (stats_repo / "app.py").write_text(
        "print('base')\nprint('widget')\n", encoding="utf-8"
    )
    (stats_repo / "docs").mkdir()
    (stats_repo / "docs" / "adr").mkdir()
    (stats_repo / "docs" / "adr" / "0001-widget.md").write_text(
        "# Widget\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "."], cwd=stats_repo, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "feat: add widget"], cwd=stats_repo, check=True
    )
    subprocess.run(["git", "checkout", "-q", "main"], cwd=stats_repo, check=True)

    claude_dir = test_root / "stats-home" / ".claude" / "projects" / "stats"
    claude_dir.mkdir(parents=True)

    def turn(model, fresh, write, read, out, when):
        return json.dumps(
            {
                "type": "assistant",
                "gitBranch": "feature/issue-7-widget",
                "sessionId": "s1",
                "timestamp": when,
                "cwd": str(stats_repo),
                "message": {
                    "model": model,
                    "usage": {
                        "input_tokens": fresh,
                        "cache_creation_input_tokens": write,
                        "cache_read_input_tokens": read,
                        "output_tokens": out,
                    },
                },
            }
        )

    (claude_dir / "s1.jsonl").write_text(
        "\n".join(
            [
                turn("model-a", 100, 400, 9500, 250, "2026-01-01T10:00:00.000Z"),
                turn("model-b", 50, 100, 500, 50, "2026-01-01T11:00:00.000Z"),
                json.dumps(
                    {
                        "type": "assistant",
                        "gitBranch": "feature/issue-999-other",
                        "sessionId": "s2",
                        "timestamp": "2026-01-01T10:30:00.000Z",
                        "message": {
                            "model": "model-a",
                            "usage": {"input_tokens": 777, "output_tokens": 777},
                        },
                    }
                ),
                # A locally generated notice, not an API turn: no rate card can ever price it.
                json.dumps(
                    {
                        "type": "assistant",
                        "gitBranch": "feature/issue-7-widget",
                        "sessionId": "s1",
                        "timestamp": "2026-01-01T10:45:00.000Z",
                        "isApiErrorMessage": True,
                        "message": {
                            "model": "<synthetic>",
                            "usage": {
                                "input_tokens": 0,
                                "output_tokens": 0,
                                "cache_creation_input_tokens": 0,
                                "cache_read_input_tokens": 0,
                            },
                        },
                    }
                ),
                "{ truncated json",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    codex_dir = test_root / "stats-home" / ".codex" / "sessions" / "2026" / "01" / "01"
    codex_dir.mkdir(parents=True)
    (codex_dir / "rollout-1.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "session_meta",
                        "timestamp": "2026-01-01T10:05:00.000Z",
                        "payload": {"cwd": str(stats_repo), "model": "codex-model"},
                    }
                ),
                json.dumps(
                    {
                        "type": "event_msg",
                        "ordinal": 1,
                        "timestamp": "2026-01-01T10:10:00.000Z",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "last_token_usage": {
                                    "input_tokens": 900,
                                    "cached_input_tokens": 800,
                                    "cache_write_input_tokens": 0,
                                    "output_tokens": 60,
                                    "reasoning_output_tokens": 10,
                                }
                            },
                        },
                        "rate_limits": {
                            "primary": {"used_percent": 41.0, "window_minutes": 300}
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "event_msg",
                        "ordinal": 1,
                        "timestamp": "2026-01-01T10:10:00.000Z",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "last_token_usage": {
                                    "input_tokens": 900,
                                    "cached_input_tokens": 800,
                                    "cache_write_input_tokens": 0,
                                    "output_tokens": 60,
                                }
                            },
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (codex_dir / "rollout-2.jsonl").write_text(
        json.dumps(
            {
                "type": "event_msg",
                "ordinal": 1,
                "timestamp": "2026-01-01T10:10:00.000Z",
                "payload": {
                    "type": "token_count",
                    "cwd": "/somewhere/else",
                    "info": {
                        "last_token_usage": {
                            "input_tokens": 5000,
                            "output_tokens": 5000,
                        }
                    },
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    def stats_run(*arguments, claude_projects=claude_dir):
        return subprocess.run(
            [
                sys.executable,
                str(stats_cli),
                "--repo",
                str(stats_repo),
                "--epic",
                "7",
                "--tickets",
                "7",
                "--home",
                str(test_root / "stats-home"),
                "--claude-projects",
                str(claude_projects),
                "--json",
                *arguments,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )

    unpriced = stats_run()
    if unpriced.returncode != 0:
        sys.exit("delivery-stats failed on a valid fixture: " + unpriced.stderr)
    report = json.loads(unpriced.stdout)
    models = report["claude"]["models"]
    if set(models) != {"model-a", "model-b"}:
        sys.exit(
            "delivery-stats did not group Claude usage by model: " + json.dumps(models)
        )
    if (
        models["model-a"]["cache_read_input_tokens"] != 9500
        or models["model-b"]["output_tokens"] != 50
    ):
        sys.exit("delivery-stats mis-summed Claude usage: " + json.dumps(models))
    if report["claude"]["turns"] != 2:
        sys.exit(
            "delivery-stats counted turns from another ticket's branch or a truncated record"
        )
    if report["claude"]["non_billable_turns"] != 1:
        sys.exit(
            "delivery-stats did not separate the locally generated, never-billed turn"
        )
    if report["cache"]["cache_read"] != 10000 or report["cache"]["fresh"] != 150:
        sys.exit(
            "delivery-stats mis-split the cache buckets: " + json.dumps(report["cache"])
        )
    if report["cost"]["status"] != "нет данных":
        sys.exit("delivery-stats invented a cost without a rate card")
    if report["volume"]["totals"]["insertions"] != 2 or report["adr_added"] != 1:
        sys.exit(
            "delivery-stats mis-read local git volume: "
            + json.dumps(report["volume"]["totals"])
        )
    codex = report["codex"]
    if codex["status"] != "ok" or codex["attribution"] != "estimated":
        sys.exit(
            "delivery-stats did not mark Codex attribution as estimated: "
            + json.dumps(codex)
        )
    if codex["models"]["codex-model"]["output_tokens"] != 60:
        sys.exit(
            "delivery-stats double-counted a repeated Codex ordinal or read another repository"
        )

    # A saved report is a portable baseline for a later, comparable epic.  Comparison must retain
    # the provenance of both sides: exact Claude branch attribution, estimated Codex window
    # attribution, and missing telemetry must never become a numeric zero or a computed delta.
    baseline_path = test_root / "delivery-stats-baseline.json"
    saved = stats_run("--save-baseline", str(baseline_path))
    if saved.returncode != 0:
        sys.exit("delivery-stats could not save a baseline: " + saved.stderr)
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    if baseline.get("schema_version") != 1:
        sys.exit("delivery-stats saved baseline without a versioned contract")
    compared = stats_run("--baseline", str(baseline_path))
    if compared.returncode != 0:
        sys.exit(
            "delivery-stats could not compare a saved baseline: " + compared.stderr
        )
    comparison = json.loads(compared.stdout).get("comparison")
    if not isinstance(comparison, dict):
        sys.exit("delivery-stats did not add comparison data to the current report")
    if comparison["baseline"]["providers"]["claude"]["attribution"] != "exact":
        sys.exit("delivery-stats lost exact attribution on the baseline")
    if comparison["current"]["providers"]["codex"]["attribution"] != "estimated":
        sys.exit("delivery-stats lost estimated attribution on the current report")

    # Cache read/write tokens (Issue #141) ride along in the same versioned baseline snapshot as
    # every other provider figure, so they must appear in both the saved baseline and its delta.
    claude_baseline_cache = baseline["providers"]["claude"]
    if (
        claude_baseline_cache["cache_write_tokens"] != 500
        or claude_baseline_cache["cache_read_tokens"] != 10000
    ):
        sys.exit(
            "delivery-stats did not save cache read/write tokens in the baseline: "
            + json.dumps(claude_baseline_cache)
        )
    codex_baseline_cache = baseline["providers"]["codex"]
    if (
        codex_baseline_cache["cache_write_tokens"] != 0
        or codex_baseline_cache["cache_read_tokens"] != 800
    ):
        sys.exit(
            "delivery-stats did not save Codex cache read/write tokens in the baseline: "
            + json.dumps(codex_baseline_cache)
        )
    claude_cache_delta = comparison["delta"]["providers"]["claude"]
    if (
        claude_cache_delta["cache_write_tokens"] != 0
        or claude_cache_delta["cache_read_tokens"] != 0
    ):
        sys.exit(
            "delivery-stats did not compute a cache-token delta between two identical reports: "
            + json.dumps(claude_cache_delta)
        )

    missing_baseline = json.loads(json.dumps(baseline))
    missing_baseline["providers"]["codex"] = {
        "status": "нет данных",
        "reason": "fixture missing telemetry",
    }
    missing_path = test_root / "delivery-stats-missing-baseline.json"
    missing_path.write_text(json.dumps(missing_baseline), encoding="utf-8")
    missing_compared = stats_run("--baseline", str(missing_path))
    if missing_compared.returncode != 0:
        sys.exit(
            "delivery-stats failed while comparing missing telemetry: "
            + missing_compared.stderr
        )
    missing_comparison = json.loads(missing_compared.stdout)["comparison"]
    if missing_comparison["baseline"]["providers"]["codex"]["status"] != "нет данных":
        sys.exit("delivery-stats invented telemetry for a missing baseline provider")
    if missing_comparison["delta"]["providers"]["codex"] != "нет данных":
        sys.exit("delivery-stats calculated a delta from missing telemetry")

    incomplete_dir = test_root / "stats-home" / ".claude" / "projects" / "incomplete"
    incomplete_dir.mkdir()
    (incomplete_dir / "incomplete.jsonl").write_text(
        json.dumps(
            {
                "type": "assistant",
                "gitBranch": "feature/issue-7-widget",
                "sessionId": "incomplete",
                "timestamp": "2026-01-01T12:00:00.000Z",
                "cwd": str(stats_repo),
                "message": {"model": "model-a", "usage": {"input_tokens": 100}},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    incomplete_path = test_root / "delivery-stats-incomplete-baseline.json"
    incomplete = stats_run(
        "--save-baseline", str(incomplete_path), claude_projects=incomplete_dir
    )
    if incomplete.returncode != 0:
        sys.exit(
            "delivery-stats failed while recording incomplete provider telemetry: "
            + incomplete.stderr
        )
    incomplete_baseline = json.loads(incomplete_path.read_text(encoding="utf-8"))
    if incomplete_baseline["providers"]["claude"]["status"] != "нет данных":
        sys.exit(
            "delivery-stats converted incomplete Claude telemetry into a numeric baseline"
        )
    shutil.rmtree(incomplete_dir)

    # An untouched template is all zeros. Reporting its total as a real 0.00 would assert a number
    # nobody supplied, so the whole section has to read as missing and name the file to fill in.
    template = test_root / "stats-rates-template.json"
    template.write_text(
        (stats_cli.parent / "rates.example.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    blank = stats_run("--rates", str(template))
    if blank.returncode != 0:
        sys.exit("delivery-stats failed on an unfilled rate card: " + blank.stderr)
    blank_cost = json.loads(blank.stdout)["cost"]
    if blank_cost[
        "status"
    ] != "нет данных" or "stats-rates-template.json" not in blank_cost.get(
        "reason", ""
    ):
        sys.exit(
            "delivery-stats treated an unfilled rate card as real prices: "
            + json.dumps(blank_cost, ensure_ascii=False)
        )

    rates_file = test_root / "stats-rates.json"
    rates_file.write_text(
        json.dumps(
            {
                "currency": "USD",
                "effective_date": "2026-01-01",
                "source": "clean-room fixture",
                "models": {
                    "model-a": {
                        "input": 1.0,
                        "output": 10.0,
                        "cache_write_multiplier": 2.0,
                        "cache_read_multiplier": 0.5,
                    },
                    # Present but left at zero: "not stated", never "free".
                    "model-b": {"input": 0.0, "output": 0.0},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    priced = stats_run(
        "--rates",
        str(rates_file),
        "--baseline",
        str(baseline_path),
        "--html",
        str(test_root / "stats.html"),
    )
    if priced.returncode != 0:
        sys.exit("delivery-stats failed with a rate card: " + priced.stderr)
    cost = json.loads(priced.stdout)["cost"]
    # fresh 100*1 + write 400*1*2 + read 9500*1*0.5 + output 250*10 = 0.0001 + 0.0008 + 0.00475 + 0.0025
    if cost["status"] != "ok" or cost["unpriced_models"] != ["codex-model", "model-b"]:
        sys.exit(
            "delivery-stats did not report the unpriced model: " + json.dumps(cost)
        )
    if cost["total"] >= cost["uncached_total"] or cost["cache_saving"] <= 0:
        sys.exit("delivery-stats did not show the cache saving: " + json.dumps(cost))
    dashboard = (test_root / "stats.html").read_text(encoding="utf-8")
    if (
        "<script" in dashboard.lower()
        or "http://" in dashboard
        or "https://" in dashboard
    ):
        sys.exit("delivery-stats dashboard is not self-contained")
    if "оценка" not in dashboard:
        sys.exit("delivery-stats dashboard does not label the estimated Codex figure")
    if (
        "Сравнение baseline" not in dashboard
        or "exact" not in dashboard
        or "estimated" not in dashboard
    ):
        sys.exit(
            "delivery-stats dashboard does not render comparison attribution for both sides"
        )

    empty_home = test_root / "stats-empty-home"
    empty_home.mkdir()
    blind = subprocess.run(
        [
            sys.executable,
            str(stats_cli),
            "--repo",
            str(stats_repo),
            "--epic",
            "7",
            "--tickets",
            "7",
            "--home",
            str(empty_home),
            "--claude-projects",
            str(empty_home),
            "--json",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if blind.returncode != 0:
        sys.exit(
            "delivery-stats failed instead of reporting missing session logs: "
            + blind.stderr
        )
    blind_report = json.loads(blind.stdout)
    if (
        blind_report["claude"]["status"] != "нет данных"
        or blind_report["cache"] != "нет данных"
    ):
        sys.exit(
            "delivery-stats zeroed missing runtime data instead of marking it absent"
        )
    if blind_report["volume"]["totals"]["insertions"] != 2:
        sys.exit("delivery-stats lost git volume when session logs were absent")

    # The runtime's transcript directory name is not a published contract and has changed: a project
    # renamed by that change keeps its old, now-empty directory. Matching the expected name and
    # stopping there found that leftover and reported "no data" while the real transcripts sat next
    # to it, so discovery has to require actual transcripts and fall back to the recorded cwd.
    stale = (
        test_root
        / "stats-home"
        / ".claude"
        / "projects"
        / re.sub(r"[\\/:]", "-", str(stats_repo))
    )
    (stale / "memory").mkdir(parents=True)
    (stale / "memory" / "MEMORY.md").write_text("leftover\n", encoding="utf-8")
    discovered = subprocess.run(
        [
            sys.executable,
            str(stats_cli),
            "--repo",
            str(stats_repo),
            "--epic",
            "7",
            "--tickets",
            "7",
            "--home",
            str(test_root / "stats-home"),
            "--json",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if discovered.returncode != 0:
        sys.exit(
            "delivery-stats failed while discovering transcripts: " + discovered.stderr
        )
    found = json.loads(discovered.stdout)["claude"]
    if found["status"] != "ok" or found["turns"] != 2:
        sys.exit(
            "delivery-stats stopped at the empty directory whose name matched the expected slug: "
            + json.dumps(found, ensure_ascii=False)
        )
    if not any(source.endswith("stats") for source in found["sources"]):
        sys.exit(
            "delivery-stats did not record which transcript directory it read: "
            + json.dumps(found["sources"])
        )

    print("delivery-stats verification passed")
    ctx.claude_dir = claude_dir
    ctx.report = report
    ctx.stats_cli = stats_cli
    ctx.stats_repo = stats_repo
