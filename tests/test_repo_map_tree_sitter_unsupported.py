"""An unsupported language stays in the minimal path inventory under tier `full` (ADR 0024, #279).

The parser bundle now carries seven grammars (Python, TS, TSX, JS, Go, Java, C#). A file whose
extension is not one of them must still be listed -- but only with a path and no real signatures,
definitions, or edges -- exactly like every file under the `minimal` tier, never silently dropped
and never fabricated from an unsupported grammar.
"""

from pathlib import Path

from conftest import build_map, file_record, records


def test_unsupported_language_file_stays_path_only_under_full_tier(
    tmp_path: Path, bundle_dir: Path
) -> None:
    _, result = build_map(
        tmp_path,
        bundle_dir,
        {
            "pkg/helper.go": (
                "package pkg\n\nfunc Render(value int) string {\n\treturn \"\"\n}\n"
            ),
            "script.rb": "def render(value)\n  value.to_s\nend\n",
        },
    )
    assert result["tier"] == "full"
    assert result["parser"] == "bundle"

    go_file = file_record(result, "pkg/helper.go")
    assert go_file["parser_status"] == "ok"
    assert go_file["signatures"] == ["func Render(value int) string"]

    ruby_file = file_record(result, "script.rb")
    assert ruby_file == {"path": "script.rb", "signatures": [], "parser_status": "ok"}

    edges = records(result, "edges")
    assert not any(edge["source"] == "script.rb" or edge["target"] == "script.rb" for edge in edges)
    assert result["diagnostics"] == []
