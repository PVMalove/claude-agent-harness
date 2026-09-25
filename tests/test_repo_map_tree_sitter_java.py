"""Repo Map Java support against a real, verified tree-sitter parser bundle (ADR 0024, #279).

A separate file, never a `pytest -k` filter, so an unavailable bundle or empty collection is a
hard failure instead of a silent zero-test skip (the CI matrix leg runs only this file).
"""

from pathlib import Path

from conftest import build_map, file_record, records

PACKAGE = {
    "Helper.java": (
        "public class Helper {\n"
        "    // this comment must not be serialized\n"
        "    public static String render(String value) {\n"
        "        String bodySecret = \"function bodies are never serialized\";\n"
        "        return bodySecret + value;\n"
        "    }\n\n"
        "    public static void onlyHere() {}\n"
        "}\n"
    ),
    "Consumer.java": (
        "public class Consumer {\n"
        "    public void use() {\n"
        "        Helper.render(\"x\");\n"
        "        Helper.onlyHere();\n"
        "    }\n"
        "}\n"
    ),
}


def test_java_signatures_and_relations_come_from_tree_sitter(tmp_path: Path, bundle_dir: Path) -> None:
    raw, result = build_map(tmp_path, bundle_dir, PACKAGE)

    assert result["tier"] == "full"
    assert result["parser"] == "bundle"
    provenance = result["parser_provenance"]
    assert isinstance(provenance, dict)
    grammars = provenance["grammars"]
    assert isinstance(grammars, list)
    java = next(grammar for grammar in grammars if grammar["name"] == "java")
    assert java["version"] == "0.23.5"
    assert java["abi"] == 14
    assert len(java["sha256"]) == 64

    helper = file_record(result, "Helper.java")
    assert helper["parser_status"] == "ok"
    assert helper["signatures"] == [
        "class Helper",
        "static method Helper.render(String value): String",
        "static method Helper.onlyHere(): void",
    ]
    edges = records(result, "edges")
    assert {
        "source": "Consumer.java",
        "target": "Helper.java",
        "kind": "unique-name-ref",
        "confidence": "medium",
    } in edges
    # Import edges stay Python/JS-only (ADR 0024): Java gets name-ref relations, never import edges.
    assert not any(edge["kind"] == "import" for edge in edges)
    assert b"function bodies are never serialized" not in raw
    assert b"must not be serialized" not in raw


def test_java_syntax_error_keeps_intact_definitions(tmp_path: Path, bundle_dir: Path) -> None:
    _, result = build_map(
        tmp_path,
        bundle_dir,
        {
            "Broken.java": (
                "public class Broken {\n"
                "    public int intact(int value) {\n"
                "        return value;\n"
                "    }\n\n"
                "    public void broken( {\n"
                "}\n"
            ),
        },
    )
    broken = file_record(result, "Broken.java")
    assert broken["parser_status"] == "syntax_error"
    signatures = broken["signatures"]
    assert isinstance(signatures, list)
    assert signatures[0] == "class Broken"
    assert "method Broken.intact(int value): int" in signatures
    assert result["diagnostics"] == [{"code": "syntax_error", "path": "Broken.java"}]


def test_java_symbol_redaction_applies_to_tree_sitter_facts(tmp_path: Path, bundle_dir: Path) -> None:
    raw, result = build_map(
        tmp_path,
        bundle_dir,
        {
            "Main.java": (
                "public class Main {\n"
                "    public int visible(int value) {\n"
                "        return value;\n"
                "    }\n\n"
                "    public void hiddenHelper() {}\n"
                "}\n"
            ),
        },
        redact_symbols=["hidden*"],
    )
    assert file_record(result, "Main.java")["signatures"] == [
        "class Main",
        "method Main.visible(int value): int",
    ]
    assert result["edges"] == []
    assert b"hiddenHelper" not in raw
