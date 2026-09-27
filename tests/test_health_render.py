"""Grouped Russian text rendering, its "-> Как исправить: " fix line, and the ASCII marker
fallback for stdout that cannot encode the emoji status markers (ticket #342)."""

from __future__ import annotations

from harness.health.model import CheckResult, Fix, Report
from harness.health.render import GROUP_LABELS_RU, render_text, supports_markers


class _FakeStream:
    def __init__(self, encoding: str | None) -> None:
        self.encoding = encoding


def test_supports_markers_true_for_utf8() -> None:
    assert supports_markers(_FakeStream("utf-8")) is True


def test_supports_markers_false_for_ascii() -> None:
    assert supports_markers(_FakeStream("ascii")) is False


def test_supports_markers_false_for_unknown_encoding() -> None:
    assert supports_markers(_FakeStream("not-a-real-codec")) is False


def test_supports_markers_defaults_to_utf8_when_encoding_is_missing() -> None:
    class _NoEncoding:
        pass

    assert supports_markers(_NoEncoding()) is True  # type: ignore[arg-type]


def _report(*checks: CheckResult) -> Report:
    report = Report(schema_version=1, repo="/repo", online=False)
    report.checks = list(checks)
    return report


def test_render_text_groups_by_first_appearance_order() -> None:
    report = _report(
        CheckResult(
            id="repo_map.tier", group="repo_map", status="ok", message="tier ok"
        ),
        CheckResult(id="files.lock", group="files", status="ok", message="lock ok"),
    )

    text = render_text(report, stream=_FakeStream("utf-8"))

    assert text.index(GROUP_LABELS_RU["repo_map"]) < text.index(
        GROUP_LABELS_RU["files"]
    )


def test_render_text_uses_emoji_markers_on_a_utf8_stream() -> None:
    report = _report(
        CheckResult(id="files.a", group="files", status="ok", message="ok message"),
        CheckResult(id="files.b", group="files", status="warn", message="warn message"),
        CheckResult(id="files.c", group="files", status="fail", message="fail message"),
        CheckResult(
            id="files.d", group="files", status="skipped", message="skipped message"
        ),
    )

    text = render_text(report, stream=_FakeStream("utf-8"))

    assert "✅ ok message" in text
    assert "⚠️ warn message" in text
    assert "❌ fail message" in text
    assert "- skipped message" in text
    assert "[OK]" not in text and "[WARN]" not in text and "[FAIL]" not in text


def test_render_text_falls_back_to_ascii_markers_on_a_non_utf8_stream() -> None:
    report = _report(
        CheckResult(id="files.a", group="files", status="ok", message="ok message"),
        CheckResult(id="files.b", group="files", status="warn", message="warn message"),
        CheckResult(id="files.c", group="files", status="fail", message="fail message"),
    )

    text = render_text(report, stream=_FakeStream("ascii"))

    assert "[OK] ok message" in text
    assert "[WARN] warn message" in text
    assert "[FAIL] fail message" in text
    assert "✅" not in text and "⚠" not in text and "❌" not in text


def test_render_text_prints_the_fix_line_with_the_exact_ticket_label() -> None:
    report = _report(
        CheckResult(
            id="files.orchestration_config",
            group="files",
            status="fail",
            message="broken config",
            fix=Fix(text="почините конфиг"),
        ),
    )

    text = render_text(report, stream=_FakeStream("utf-8"))

    assert "-> Как исправить: почините конфиг" in text


def test_render_text_prints_the_fix_command_on_its_own_line_when_present() -> None:
    report = _report(
        CheckResult(
            id="files.orchestration_config",
            group="files",
            status="fail",
            message="broken config",
            fix=Fix(text="почините конфиг", command="harness registry ."),
        ),
    )

    text = render_text(report, stream=_FakeStream("utf-8"))

    assert "-> Как исправить: почините конфиг" in text
    assert "harness registry ." in text


def test_render_text_ends_with_the_summary_and_activation_footer() -> None:
    report = _report(
        CheckResult(id="files.a", group="files", status="ok", message="ok message"),
        CheckResult(id="files.b", group="files", status="fail", message="fail message"),
    )

    text = render_text(report, stream=_FakeStream("utf-8"))

    assert "Итого: ok=1 warn=0 fail=1 skipped=0" in text
    assert (
        "activation: verify advertised and invoked skills/integrations in a fresh runtime session"
        in text
    )
