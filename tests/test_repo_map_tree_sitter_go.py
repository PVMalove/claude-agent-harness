"""Repo Map Go support against a real, verified tree-sitter parser bundle (ADR 0024, #279).

A separate file, never a `pytest -k` filter, so an unavailable bundle or empty collection is a
hard failure instead of a silent zero-test skip (the CI matrix leg runs only this file).
"""

from pathlib import Path

from conftest import build_map, file_record, records

PACKAGE = {
    "pkg/helper.go": (
        "package pkg\n\n"
        "// Render formats a value; this comment must not be serialized.\n"
        "func Render(value int, label string) (string, error) {\n"
        "\tbodySecret := \"function bodies are never serialized\"\n"
        "\t_ = bodySecret\n"
        "\treturn label, nil\n"
        "}\n\n"
        "func onlyHere() {}\n"
    ),
    "pkg/consumer.go": (
        "package pkg\n\n"
        "func consumer() {\n"
        "\tRender(1, \"x\")\n"
        "\tonlyHere()\n"
        "}\n"
    ),
}


def test_go_signatures_and_relations_come_from_tree_sitter(tmp_path: Path, bundle_dir: Path) -> None:
    raw, result = build_map(tmp_path, bundle_dir, PACKAGE)

    assert result["tier"] == "full"
    assert result["parser"] == "bundle"
    provenance = result["parser_provenance"]
    assert isinstance(provenance, dict)
    grammars = provenance["grammars"]
    assert isinstance(grammars, list)
    go = next(grammar for grammar in grammars if grammar["name"] == "go")
    assert go["version"] == "0.25.0"
    assert go["abi"] == 15
    assert len(go["sha256"]) == 64

    helper = file_record(result, "pkg/helper.go")
    assert helper["parser_status"] == "ok"
    assert helper["signatures"] == [
        "func Render(value int, label string) (string, error)",
        "func onlyHere()",
    ]
    edges = records(result, "edges")
    assert {
        "source": "pkg/consumer.go",
        "target": "pkg/helper.go",
        "kind": "unique-name-ref",
        "confidence": "medium",
    } in edges
    # Import edges stay Python/JS-only (ADR 0024): Go gets name-ref relations, never import edges.
    assert not any(edge["kind"] == "import" for edge in edges)
    assert b"function bodies are never serialized" not in raw
    assert b"must not be serialized" not in raw


def test_go_syntax_error_keeps_intact_definitions(tmp_path: Path, bundle_dir: Path) -> None:
    _, result = build_map(
        tmp_path,
        bundle_dir,
        {
            "broken.go": (
                "package pkg\n\n"
                "func intact(value int) int {\n"
                "\treturn value\n"
                "}\n\n"
                "func broken( {\n"
            ),
        },
    )
    broken = file_record(result, "broken.go")
    assert broken["parser_status"] == "syntax_error"
    assert broken["signatures"] == ["func intact(value int) int"]
    assert result["diagnostics"] == [{"code": "syntax_error", "path": "broken.go"}]


def test_go_symbol_redaction_applies_to_tree_sitter_facts(tmp_path: Path, bundle_dir: Path) -> None:
    raw, result = build_map(
        tmp_path,
        bundle_dir,
        {
            "main.go": (
                "package pkg\n\n"
                "func Visible(value int) int {\n"
                "\treturn value\n"
                "}\n\n"
                "func hiddenHelper() {}\n"
            ),
        },
        redact_symbols=["hidden*"],
    )
    assert file_record(result, "main.go")["signatures"] == ["func Visible(value int) int"]
    assert result["edges"] == []
    assert b"hiddenHelper" not in raw
