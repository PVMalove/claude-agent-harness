#!/usr/bin/env python3
"""Parser-bundle worker: extract per-file facts with tree-sitter grammars.

Runs only as the bounded subprocess that `parser_bundle.run_bundle_parser` starts:
`python tree_sitter_worker.py <install_dir>`, a JSON request `{"paths": {path: base64}}` on
stdin, and a JSON `{"files": {path: FileFacts}}` response on stdout (see `FileFacts` in
harness/repo_map/parser_bundle.py). The release process copies this file next to the bundle lock
and records its sha256 there, so it must stay standalone: stdlib plus the bundle's pinned
`tree_sitter*` packages from `<install_dir>`, never a harness import.

It returns facts only -- signature text with the symbols it exposes, imports, definitions, and
references. Symbol policy, redaction, and edges stay in repo_map.py, so no policy content ever
reaches this process. Function bodies and comments are never serialized. A file with syntax
errors still yields facts from its intact definitions; tree-sitter types never leave this process
(ADR 0024). This module is excluded from the main mypy run and checked by the bundle CI job.
"""

from __future__ import annotations

import base64
import json
import sys
from collections.abc import Iterator
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tree_sitter import Node, Parser

# Parameter nodes whose default value is replaced by `...`, matching the old signature form.
_DEFAULT_PARAMETERS = frozenset({"default_parameter", "typed_default_parameter"})
# Identifiers under these parents are bindings, not references (the `name` field is checked below).
_BINDING_PARENTS = frozenset(
    {
        "parameters",
        "lambda_parameters",
        "typed_parameter",
        "as_pattern_target",
        "list_splat_pattern",
        "dictionary_splat_pattern",
        "global_statement",
        "nonlocal_statement",
    }
)
_NAMED_BINDINGS = frozenset(
    {
        "function_definition",
        "class_definition",
        "default_parameter",
        "typed_default_parameter",
        "keyword_argument",
    }
)
_STORE_CONTAINERS = frozenset(
    {"pattern_list", "tuple_pattern", "list_pattern", "tuple", "list", "parenthesized_expression"}
)


def _text(node: Node | None) -> str:
    if node is None or node.text is None:
        return ""
    return " ".join(node.text.decode("utf-8", "replace").split())


def _walk(node: Node) -> Iterator[Node]:
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def _symbols(nodes: list[Node | None]) -> list[str]:
    """Every identifier and string literal that a serialized signature part exposes."""
    found: list[str] = []
    for part in nodes:
        if part is None:
            continue
        for node in _walk(part):
            if node.type in ("identifier", "string_content"):
                found.append(_text(node))
    return found


def _parameter(node: Node) -> tuple[str, list[Node | None]]:
    """One parameter's normalized text and the nodes it serializes (defaults become `...`)."""
    if node.type not in _DEFAULT_PARAMETERS:
        return _text(node), [node]
    name = node.child_by_field_name("name")
    annotation = node.child_by_field_name("type")
    if annotation is None:
        return f"{_text(name)}=...", [name]
    return f"{_text(name)}: {_text(annotation)}=...", [name, annotation]


def _function_signature(node: Node, owner: str) -> dict[str, object]:
    name = node.child_by_field_name("name")
    parameters = node.child_by_field_name("parameters")
    returns = node.child_by_field_name("return_type")
    rendered: list[str] = []
    exposed: list[Node | None] = [name, returns]
    for child in parameters.named_children if parameters is not None else []:
        if child.type == "comment":
            continue
        text, parts = _parameter(child)
        rendered.append(text)
        exposed.extend(parts)
    is_async = any(child.type == "async" for child in node.children)
    qualified = f"{owner}.{_text(name)}" if owner else _text(name)
    arrow = f" -> {_text(returns)}" if returns is not None else ""
    text = f"{'async def' if is_async else 'def'} {qualified}({', '.join(rendered)}){arrow}"
    symbols = ([owner] if owner else []) + _symbols(exposed)
    return {"text": text, "symbols": symbols}


def _class_signature(node: Node) -> dict[str, object]:
    name = node.child_by_field_name("name")
    superclasses = node.child_by_field_name("superclasses")
    bases: list[str] = []
    exposed: list[Node | None] = [name]
    for child in superclasses.named_children if superclasses is not None else []:
        if child.type == "comment":
            continue
        exposed.append(child)
        if child.type != "keyword_argument":
            bases.append(_text(child))
    text = f"class {_text(name)}({', '.join(bases)})" if bases else f"class {_text(name)}"
    return {"text": text, "symbols": _symbols(exposed)}


def _definition(node: Node) -> Node:
    if node.type == "decorated_definition":
        inner = node.child_by_field_name("definition")
        if inner is not None:
            return inner
    return node


def _signatures(root: Node) -> list[dict[str, object]]:
    """Top-level functions and classes, plus the methods directly inside each top-level class."""
    found: list[dict[str, object]] = []
    for child in root.named_children:
        node = _definition(child)
        if node.has_error:
            continue
        if node.type == "function_definition":
            found.append(_function_signature(node, ""))
        elif node.type == "class_definition":
            found.append(_class_signature(node))
            body = node.child_by_field_name("body")
            owner = _text(node.child_by_field_name("name"))
            for member in body.named_children if body is not None else []:
                method = _definition(member)
                if method.type == "function_definition" and not method.has_error:
                    found.append(_function_signature(method, owner))
    return found


def _imports(root: Node) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    for node in _walk(root):
        if node.type == "import_statement":
            for name in node.children_by_field_name("name"):
                target = name.child_by_field_name("name") if name.type == "aliased_import" else name
                found.append({"module": _text(target), "level": 0, "names": []})
        elif node.type == "import_from_statement":
            module = node.child_by_field_name("module_name")
            level = 0
            module_text = _text(module)
            if module is not None and module.type == "relative_import":
                prefix = next((c for c in module.children if c.type == "import_prefix"), None)
                level = len(_text(prefix))
                dotted = next((c for c in module.children if c.type == "dotted_name"), None)
                module_text = _text(dotted)
            names: list[str] = []
            if any(child.type == "wildcard_import" for child in node.children):
                names.append("*")
            for name in node.children_by_field_name("name"):
                target = name.child_by_field_name("name") if name.type == "aliased_import" else name
                names.append(_text(target))
            found.append({"module": module_text, "level": level, "names": names})
    return found


def _is_store(node: Node) -> bool:
    """Whether `node` is (inside) an assignment/loop target rather than a load."""
    child = node
    parent = node.parent
    while parent is not None and parent.type in _STORE_CONTAINERS:
        child, parent = parent, parent.parent
    if parent is None:
        return False
    if parent.type in ("assignment", "augmented_assignment", "for_statement", "for_in_clause"):
        return parent.child_by_field_name("left") == child
    if parent.type == "named_expression":
        return parent.child_by_field_name("name") == child
    return False


def _is_reference(node: Node) -> bool:
    parent = node.parent
    if parent is None:
        return False
    if parent.type in _BINDING_PARENTS:
        return False
    if parent.type in _NAMED_BINDINGS and parent.child_by_field_name("name") == node:
        return False
    if parent.type == "attribute" and parent.child_by_field_name("attribute") == node:
        return False
    ancestor: Node | None = parent
    while ancestor is not None:
        if ancestor.type in ("import_statement", "import_from_statement", "dotted_name"):
            return False
        ancestor = ancestor.parent
    return not _is_store(node)


def _names(root: Node) -> tuple[list[str], list[str]]:
    definitions: list[str] = []
    references: list[str] = []
    for node in _walk(root):
        if node.type in ("function_definition", "class_definition") and not node.has_error:
            definitions.append(_text(node.child_by_field_name("name")))
        elif node.type == "identifier" and _is_reference(node):
            references.append(_text(node))
    return sorted(set(definitions)), sorted(set(references))


def _file_facts(parser: Parser, content: bytes) -> dict[str, object]:
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        return {
            "parser_status": "invalid_encoding",
            "signatures": [],
            "imports": [],
            "definitions": [],
            "references": [],
        }
    root = parser.parse(content).root_node
    definitions, references = _names(root)
    return {
        "parser_status": "syntax_error" if root.has_error else "ok",
        "signatures": _signatures(root),
        "imports": _imports(root),
        "definitions": definitions,
        "references": references,
    }


def main() -> None:
    sys.path.insert(0, sys.argv[1])
    import tree_sitter
    import tree_sitter_python

    parsers = {".py": tree_sitter.Parser(tree_sitter.Language(tree_sitter_python.language()))}
    request = json.loads(sys.stdin.buffer.read())
    files: dict[str, dict[str, object]] = {}
    for path, encoded in sorted(request.get("paths", {}).items()):
        parser = parsers.get(PurePosixPath(path).suffix)
        if parser is not None:
            files[path] = _file_facts(parser, base64.b64decode(encoded))
    sys.stdout.write(json.dumps({"files": files}, sort_keys=True))


if __name__ == "__main__":
    main()
