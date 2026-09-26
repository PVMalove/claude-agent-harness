"""Repo Map C# support against a real, verified tree-sitter parser bundle (ADR 0024, #279).

A separate file, never a `pytest -k` filter, so an unavailable bundle or empty collection is a
hard failure instead of a silent zero-test skip (the CI matrix leg runs only this file).
"""

from pathlib import Path

from conftest import build_map, file_record, records

PACKAGE = {
    "Helper.cs": (
        "public class Helper\n"
        "{\n"
        "    // this comment must not be serialized\n"
        "    public static string Render(string value, int retries = 3)\n"
        "    {\n"
        "        string bodySecret = \"function bodies are never serialized\";\n"
        "        return bodySecret + value;\n"
        "    }\n\n"
        "    public static void OnlyHere() {}\n"
        "}\n"
    ),
    "Consumer.cs": (
        "public class Consumer\n"
        "{\n"
        "    public void Use()\n"
        "    {\n"
        "        Helper.Render(\"x\", 1);\n"
        "        Helper.OnlyHere();\n"
        "    }\n"
        "}\n"
    ),
}


def test_csharp_signatures_and_relations_come_from_tree_sitter(tmp_path: Path, bundle_dir: Path) -> None:
    raw, result = build_map(tmp_path, bundle_dir, PACKAGE)

    assert result["tier"] == "full"
    assert result["parser"] == "bundle"
    provenance = result["parser_provenance"]
    assert isinstance(provenance, dict)
    grammars = provenance["grammars"]
    assert isinstance(grammars, list)
    csharp = next(grammar for grammar in grammars if grammar["name"] == "csharp")
    assert csharp["version"] == "0.23.5"
    assert csharp["abi"] == 15
    assert len(csharp["sha256"]) == 64

    helper = file_record(result, "Helper.cs")
    assert helper["parser_status"] == "ok"
    assert helper["signatures"] == [
        "class Helper",
        "static method Helper.Render(string value, int retries=...): string",
        "static method Helper.OnlyHere(): void",
    ]
    edges = records(result, "edges")
    assert {
        "source": "Consumer.cs",
        "target": "Helper.cs",
        "kind": "unique-name-ref",
        "confidence": "medium",
    } in edges
    # Import edges stay Python/JS-only (ADR 0024): C# gets name-ref relations, never import edges.
    assert not any(edge["kind"] == "import" for edge in edges)
    assert b"function bodies are never serialized" not in raw
    assert b"must not be serialized" not in raw


def test_csharp_classes_under_a_file_scoped_namespace_are_mapped(
    tmp_path: Path, bundle_dir: Path
) -> None:
    _, result = build_map(
        tmp_path,
        bundle_dir,
        {
            "Service.cs": (
                "namespace App.Core;\n\n"
                "using System;\n\n"
                "public class Service\n"
                "{\n"
                "    public int Run(int value) { return value; }\n"
                "}\n"
            ),
            "Caller.cs": (
                "namespace App.Web;\n\n"
                "public class Caller\n"
                "{\n"
                "    public void Call() { new Service().Run(1); }\n"
                "}\n"
            ),
        },
    )

    assert result["tier"] == "full"
    assert file_record(result, "Service.cs")["signatures"] == [
        "class Service",
        "method Service.Run(int value): int",
    ]
    assert {
        "source": "Caller.cs",
        "target": "Service.cs",
        "kind": "unique-name-ref",
        "confidence": "medium",
    } in records(result, "edges")


def test_csharp_syntax_error_keeps_intact_definitions(tmp_path: Path, bundle_dir: Path) -> None:
    _, result = build_map(
        tmp_path,
        bundle_dir,
        {
            "Broken.cs": (
                "public class Broken\n"
                "{\n"
                "    public int Intact(int value)\n"
                "    {\n"
                "        return value;\n"
                "    }\n\n"
                "    public void Bad()\n"
                "    {\n"
                "        return +;\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    broken = file_record(result, "Broken.cs")
    assert broken["parser_status"] == "syntax_error"
    signatures = broken["signatures"]
    assert isinstance(signatures, list)
    assert signatures[0] == "class Broken"
    assert "method Broken.Intact(int value): int" in signatures
    assert result["diagnostics"] == [{"code": "syntax_error", "path": "Broken.cs"}]


def test_csharp_symbol_redaction_applies_to_tree_sitter_facts(tmp_path: Path, bundle_dir: Path) -> None:
    raw, result = build_map(
        tmp_path,
        bundle_dir,
        {
            "Main.cs": (
                "public class Main\n"
                "{\n"
                "    public int Visible(int value)\n"
                "    {\n"
                "        return value;\n"
                "    }\n\n"
                "    public void HiddenHelper() {}\n"
                "}\n"
            ),
        },
        redact_symbols=["Hidden*"],
    )
    assert file_record(result, "Main.cs")["signatures"] == [
        "class Main",
        "method Main.Visible(int value): int",
    ]
    assert result["edges"] == []
    assert b"HiddenHelper" not in raw
