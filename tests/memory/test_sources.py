"""Reserved tracker snapshot sources honor selector membership."""

from pathlib import Path

from harness.memory.sources import source_type


def test_snapshot_namespace_has_no_generic_markdown_fallback(tmp_path: Path) -> None:
    assert source_type('.harness/.sandboxes/memory/snapshot/private.md') == ''
    assert source_type('.harness/.sandboxes/memory/snapshot/records/ticket-1-' + 'a' * 64 + '.json') == 'task_archive'
    assert source_type('.harness/.sandboxes/memory/snapshot/records/completion_report-1-' + 'a' * 64 + '.json') == 'completion_report'
