#!/usr/bin/env python3
"""Worker parser bundle: извлечь факты о файлах грамматиками tree-sitter.

Запускается только как ограниченный subprocess из `parser_bundle.run_bundle_parser`:
`python tree_sitter_worker.py <install_dir>`, на stdin — JSON-запрос
`{"paths": {path: base64}, "languages": {extension: grammar}}`, на stdout — JSON
`{"files": {path: FileFacts}}` (см. `FileFacts` в harness/repo_map/bundle_worker.py). Релизный процесс копирует этот файл рядом с lock и записывает
его SHA-256, поэтому модуль остаётся автономным: stdlib и закреплённые пакеты `tree_sitter*` из
`<install_dir>`, без импорта harness.

Worker возвращает только факты: текст сигнатуры с раскрываемыми символами, импорты, определения и
ссылки. Политика символов, редактирование и рёбра остаются в repo_map.py, поэтому содержимое
политики в этот процесс не попадает. Тела функций и комментарии не сериализуются. Файл с
синтаксическими ошибками всё равно даёт факты целых определений; типы tree-sitter не покидают
процесс (ADR 0024). Модуль исключён из основного mypy и проверяется в CI-задаче bundle.
"""

from __future__ import annotations

import base64
import importlib
import json
import sys
from collections.abc import Callable, Iterator
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
# Identifiers nested in these nodes name modules or packages, never a referenced symbol.
_PYTHON_IMPORT_CONTEXT = frozenset({"import_statement", "import_from_statement", "dotted_name"})
_GO_IMPORT_CONTEXT = frozenset({"import_declaration"})
_JAVA_IMPORT_CONTEXT = frozenset({"import_declaration", "package_declaration"})
_CS_IMPORT_CONTEXT = frozenset({"using_directive"})
# JS/TS identifiers inside imports, parameter lists, and type-only declarations are not references.
_JS_NON_REFERENCE_CONTEXT = frozenset(
    {
        "import_statement",
        "formal_parameters",
        "required_parameter",
        "optional_parameter",
        "type_alias_declaration",
        "interface_declaration",
    }
)


def _text(node: Node | None) -> str:
    """Вернуть текст узла с нормализованными пробелами; пустую строку для отсутствующего узла."""
    if node is None or node.text is None:
        return ""
    return " ".join(node.text.decode("utf-8", "replace").split())


def _has_ancestor(node: Node, types: frozenset[str]) -> bool:
    """Проверить, лежит ли узел внутри узла одного из типов `types`."""
    ancestor = node.parent
    while ancestor is not None:
        if ancestor.type in types:
            return True
        ancestor = ancestor.parent
    return False


def _qualified(owner: str, name: Node | None) -> str:
    """Имя члена с префиксом владельца, если владелец есть."""
    return f"{owner}.{_text(name)}" if owner else _text(name)


def _class_text(name: Node | None, bases: list[str]) -> str:
    """Текст сигнатуры класса с базовыми типами в скобках, если они есть."""
    return f"class {_text(name)}({', '.join(bases)})" if bases else f"class {_text(name)}"


def _walk(node: Node) -> Iterator[Node]:
    """Обойти поддерево в глубину в порядке исходного текста без рекурсии."""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def _symbols(nodes: list[Node | None]) -> list[str]:
    """Все идентификаторы и строковые литералы, которые раскрывает сериализуемая часть сигнатуры."""
    found: list[str] = []
    for part in nodes:
        if part is None:
            continue
        for node in _walk(part):
            if node.type in ("identifier", "type_identifier", "property_identifier", "string_content", "string_fragment"):
                found.append(_text(node))
    return found


def _parameter(node: Node) -> tuple[str, list[Node | None]]:
    """Нормализованный текст параметра Python и раскрываемые им узлы; значение по умолчанию становится `...`."""
    if node.type not in _DEFAULT_PARAMETERS:
        return _text(node), [node]
    name = node.child_by_field_name("name")
    annotation = node.child_by_field_name("type")
    if annotation is None:
        return f"{_text(name)}=...", [name]
    return f"{_text(name)}: {_text(annotation)}=...", [name, annotation]


def _function_signature(node: Node, owner: str) -> dict[str, object]:
    """Сигнатура функции или метода Python: `def`/`async def`, параметры и аннотация результата."""
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
    qualified = _qualified(owner, name)
    arrow = f" -> {_text(returns)}" if returns is not None else ""
    text = f"{'async def' if is_async else 'def'} {qualified}({', '.join(rendered)}){arrow}"
    symbols = ([owner] if owner else []) + _symbols(exposed)
    return {"text": text, "symbols": symbols}


def _class_signature(node: Node) -> dict[str, object]:
    """Сигнатура класса Python с базовыми классами; keyword-аргументы в текст не попадают."""
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
    return {"text": _class_text(name, bases), "symbols": _symbols(exposed)}


def _definition(node: Node) -> Node:
    """Вернуть определение внутри `decorated_definition` или сам узел."""
    if node.type == "decorated_definition":
        inner = node.child_by_field_name("definition")
        if inner is not None:
            return inner
    return node


def _signatures(root: Node) -> list[dict[str, object]]:
    """Функции и классы верхнего уровня Python и методы непосредственно внутри таких классов."""
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
    """Импорты Python: `import` и `from ... import` с уровнем относительного импорта."""
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
    """Проверить, является ли узел целью присваивания или цикла (запись), а не чтением."""
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
    """Проверить, является ли идентификатор Python ссылкой, а не привязкой имени, атрибутом или импортом."""
    parent = node.parent
    if parent is None:
        return False
    if parent.type in _BINDING_PARENTS:
        return False
    if parent.type in _NAMED_BINDINGS and parent.child_by_field_name("name") == node:
        return False
    if parent.type == "attribute" and parent.child_by_field_name("attribute") == node:
        return False
    if _has_ancestor(node, _PYTHON_IMPORT_CONTEXT):
        return False
    return not _is_store(node)


def _names(root: Node) -> tuple[list[str], list[str]]:
    """Определения и ссылки Python-модуля."""
    return _collect_names(
        root, ("function_definition", "class_definition"), ("identifier",), _is_reference
    )


def _js_parameter(node: Node) -> tuple[str, list[Node | None]]:
    """Отрисовать параметр JS/TS без сериализации выражения по умолчанию."""
    if node.type == "assignment_pattern":
        left = node.child_by_field_name("left")
        return f"{_text(left)}=...", [left]
    if node.type in {"required_parameter", "optional_parameter"}:
        pattern = node.child_by_field_name("pattern")
        annotation = node.child_by_field_name("type")
        value = node.child_by_field_name("value")
        suffix = _text(annotation)
        marker = "?" if node.type == "optional_parameter" else ""
        rendered = f"{_text(pattern)}{marker}{suffix}"
        if value is not None:
            rendered += "=..."
        return rendered, [pattern, annotation]
    return _text(node), [node]


def _js_callable_signature(node: Node, name: Node | None, owner: str = "") -> dict[str, object]:
    """Сигнатура функции, стрелочной функции или метода JS/TS."""
    parameters = node.child_by_field_name("parameters")
    return_type = node.child_by_field_name("return_type")
    rendered: list[str] = []
    exposed: list[Node | None] = [name, return_type]
    for parameter in parameters.named_children if parameters is not None else []:
        value, parts = _js_parameter(parameter)
        rendered.append(value)
        exposed.extend(parts)
    qualified = _qualified(owner, name)
    keyword = "method" if owner else ("const" if node.type == "arrow_function" else "function")
    async_prefix = "async " if any(child.type == "async" for child in node.children) else ""
    suffix = _text(return_type)
    return {
        "text": f"{async_prefix}{keyword} {qualified}({', '.join(rendered)}){suffix}",
        "symbols": ([owner] if owner else []) + _symbols(exposed),
    }


def _js_signatures(root: Node) -> list[dict[str, object]]:
    """Функции, классы с методами и стрелочные константы верхнего уровня JS/TS, включая экспорт."""
    found: list[dict[str, object]] = []
    for top in root.named_children:
        node = top.child_by_field_name("declaration") if top.type == "export_statement" else top
        if node is None or node.has_error:
            continue
        if node.type == "function_declaration":
            found.append(_js_callable_signature(node, node.child_by_field_name("name")))
        elif node.type == "class_declaration":
            name = node.child_by_field_name("name")
            heritage = next((c for c in node.named_children if c.type == "class_heritage"), None)
            prefix = f"class {_text(name)}"
            if heritage is not None:
                prefix += f" {_text(heritage)}"
            found.append({"text": prefix, "symbols": _symbols([name, heritage])})
            body = node.child_by_field_name("body")
            for member in body.named_children if body is not None else []:
                if member.type == "method_definition" and not member.has_error:
                    found.append(
                        _js_callable_signature(member, member.child_by_field_name("name"), _text(name))
                    )
        elif node.type in {"lexical_declaration", "variable_declaration"}:
            for variable in node.named_children:
                if variable.type != "variable_declarator" or variable.has_error:
                    continue
                value = variable.child_by_field_name("value")
                name = variable.child_by_field_name("name")
                if value is not None and value.type == "arrow_function" and name is not None:
                    found.append(_js_callable_signature(value, name))
    return found


def _js_imports(root: Node) -> list[dict[str, object]]:
    """Статические импорты JS/TS верхнего уровня: только строка источника."""
    found: list[dict[str, object]] = []
    for node in root.named_children:
        if node.type != "import_statement" or node.has_error:
            continue
        source = node.child_by_field_name("source")
        if source is not None:
            found.append({"module": _text(source).strip("\"'"), "level": 0, "names": []})
    return found


def _js_names(root: Node) -> tuple[list[str], list[str]]:
    """Определения и ссылки JS/TS; узлы с синтаксической ошибкой пропускаются."""
    definitions: set[str] = set()
    references: set[str] = set()
    for node in _walk(root):
        if node.has_error:
            continue
        if node.type in {"function_declaration", "class_declaration", "method_definition"}:
            name = node.child_by_field_name("name")
            if name is not None:
                definitions.add(_text(name))
        elif node.type == "variable_declarator":
            name = node.child_by_field_name("name")
            if name is not None and name.type == "identifier":
                definitions.add(_text(name))
        elif node.type in {"identifier", "type_identifier"}:
            parent = node.parent
            if parent is None:
                continue
            if parent.child_by_field_name("name") == node and parent.type in {
                "function_declaration", "class_declaration", "method_definition", "variable_declarator"
            }:
                continue
            if parent.type in {"member_expression", "subscript_expression"} and parent.child_by_field_name("property") == node:
                continue
            if not _has_ancestor(node, _JS_NON_REFERENCE_CONTEXT):
                references.add(_text(node))
    return sorted(definitions), sorted(references)


def _no_imports(root: Node) -> list[dict[str, object]]:
    """Для Go, Java и C# импорты не извлекаются.

    Однозначного соответствия импорт → файл нет без разбора go.mod, корней пакетов и .csproj;
    repo_map.py строит рёбра импорта только для Python и JS (ADR 0024, #279), поэтому здесь
    возвращается пустой список, а не приближение.
    """
    return []


def _go_callable_signature(node: Node) -> dict[str, object]:
    """Сигнатура функции или метода Go с получателем, параметрами и результатом."""
    receiver = node.child_by_field_name("receiver")
    name = node.child_by_field_name("name")
    parameters = node.child_by_field_name("parameters")
    result = node.child_by_field_name("result")
    exposed: list[Node | None] = [name, result]
    rendered: list[str] = []
    for child in parameters.named_children if parameters is not None else []:
        if child.type == "comment":
            continue
        rendered.append(_text(child))
        exposed.append(child)
    receiver_text = ""
    if receiver is not None:
        receiver_param = next(iter(receiver.named_children), None)
        if receiver_param is not None:
            receiver_text = f" ({_text(receiver_param)})"
            exposed.append(receiver_param)
    result_text = f" {_text(result)}" if result is not None else ""
    text = f"func{receiver_text} {_text(name)}({', '.join(rendered)}){result_text}"
    return {"text": text, "symbols": _symbols(exposed)}


def _go_type_signature(node: Node) -> dict[str, object] | None:
    """Сигнатура `struct` или `interface` Go; `None` для остальных типов."""
    name = node.child_by_field_name("name")
    kind = node.child_by_field_name("type")
    if name is None or kind is None or kind.type not in ("struct_type", "interface_type"):
        return None
    keyword = "struct" if kind.type == "struct_type" else "interface"
    return {"text": f"type {_text(name)} {keyword}", "symbols": _symbols([name])}


def _go_signatures(root: Node) -> list[dict[str, object]]:
    """Функции, методы и типы struct/interface верхнего уровня Go."""
    found: list[dict[str, object]] = []
    for node in root.named_children:
        if node.has_error:
            continue
        if node.type in ("function_declaration", "method_declaration"):
            found.append(_go_callable_signature(node))
        elif node.type == "type_declaration":
            for spec in node.named_children:
                if spec.type != "type_spec" or spec.has_error:
                    continue
                signature = _go_type_signature(spec)
                if signature is not None:
                    found.append(signature)
    return found


def _go_is_reference(node: Node) -> bool:
    """Проверить, является ли идентификатор Go ссылкой, а не объявлением, параметром, полем или импортом."""
    parent = node.parent
    if parent is None:
        return False
    if (
        parent.type in ("function_declaration", "method_declaration", "type_spec")
        and parent.child_by_field_name("name") == node
    ):
        return False
    if parent.type in (
        "parameter_declaration", "variadic_parameter_declaration"
    ) and node in parent.children_by_field_name("name"):
        return False
    if parent.type == "selector_expression" and parent.child_by_field_name("field") == node:
        return False
    return not _has_ancestor(node, _GO_IMPORT_CONTEXT)


def _collect_names(
    root: Node,
    definition_types: tuple[str, ...],
    reference_types: tuple[str, ...],
    is_reference: Callable[[Node], bool],
) -> tuple[list[str], list[str]]:
    """Собрать отсортированные определения и ссылки по типам узлов языка."""
    definitions: set[str] = set()
    references: set[str] = set()
    for node in _walk(root):
        # A syntax error anywhere in a node's subtree marks that node `has_error` too, so this
        # checks each definition node's own flag rather than skipping the whole walk -- an
        # error in one member must not hide its intact siblings (or their enclosing type).
        if node.type in definition_types and not node.has_error:
            name = node.child_by_field_name("name")
            if name is not None:
                definitions.add(_text(name))
        elif node.type in reference_types and is_reference(node):
            references.add(_text(node))
    return sorted(definitions), sorted(references)


def _go_names(root: Node) -> tuple[list[str], list[str]]:
    """Определения и ссылки Go."""
    return _collect_names(
        root,
        ("function_declaration", "method_declaration", "type_spec"),
        ("identifier", "type_identifier"),
        _go_is_reference,
    )


def _member_callable_signature(
    node: Node,
    owner: str,
    returns: Node | None,
    is_static: bool,
    parameter: Callable[[Node], tuple[str, list[Node | None]]],
) -> dict[str, object]:
    """Отрисовать метод или конструктор Java/C#, объявленный внутри `owner`."""
    is_constructor = node.type == "constructor_declaration"
    name = node.child_by_field_name("name")
    parameters = node.child_by_field_name("parameters")
    exposed: list[Node | None] = [name, returns]
    rendered: list[str] = []
    for child in parameters.named_children if parameters is not None else []:
        if child.type == "comment":
            continue
        text, parts = parameter(child)
        rendered.append(text)
        exposed.extend(parts)
    qualified = _qualified(owner, name)
    keyword = "constructor" if is_constructor else ("static method" if is_static else "method")
    suffix = f": {_text(returns)}" if returns is not None else ""
    text = f"{keyword} {qualified}({', '.join(rendered)}){suffix}"
    return {"text": text, "symbols": ([owner] if owner else []) + _symbols(exposed)}


def _java_parameter(node: Node) -> tuple[str, list[Node | None]]:
    """Текст параметра Java и раскрываемый им узел."""
    return _text(node), [node]


def _java_callable_signature(node: Node, owner: str) -> dict[str, object]:
    """Сигнатура метода или конструктора Java с признаком `static`."""
    modifiers = next((child for child in node.children if child.type == "modifiers"), None)
    is_static = modifiers is not None and any(child.type == "static" for child in modifiers.children)
    return _member_callable_signature(
        node, owner, node.child_by_field_name("type"), is_static, _java_parameter
    )


def _java_class_signature(node: Node) -> dict[str, object]:
    """Сигнатура класса Java с суперклассом и интерфейсами."""
    name = node.child_by_field_name("name")
    superclass = node.child_by_field_name("superclass")
    interfaces = node.child_by_field_name("interfaces")
    bases: list[str] = []
    if superclass is not None:
        base = next(iter(superclass.named_children), None)
        if base is not None:
            bases.append(_text(base))
    if interfaces is not None:
        type_list = next(iter(interfaces.named_children), None)
        if type_list is not None:
            bases.extend(_text(item) for item in type_list.named_children)
    return {"text": _class_text(name, bases), "symbols": _symbols([name, superclass, interfaces])}


def _java_signatures(root: Node) -> list[dict[str, object]]:
    # A syntax error inside one member marks the enclosing class_declaration `has_error` too (Java
    # nests every member inside the class body), so only individual members are gated below --
    # matching the same "intact definitions survive" resilience as Python's top-level functions.
    """Классы Java верхнего уровня с методами и конструкторами; член с ошибкой пропускается."""
    found: list[dict[str, object]] = []
    for node in root.named_children:
        if node.type != "class_declaration":
            continue
        found.append(_java_class_signature(node))
        owner = _text(node.child_by_field_name("name"))
        body = node.child_by_field_name("body")
        for member in body.named_children if body is not None else []:
            if member.type in ("method_declaration", "constructor_declaration") and not member.has_error:
                found.append(_java_callable_signature(member, owner))
    return found


def _java_is_reference(node: Node) -> bool:
    """Проверить, является ли идентификатор Java ссылкой, а не объявлением, именем вызова, полем или импортом."""
    parent = node.parent
    if parent is None:
        return False
    if (
        parent.type in ("class_declaration", "method_declaration", "constructor_declaration")
        and parent.child_by_field_name("name") == node
    ):
        return False
    if parent.type == "formal_parameter" and parent.child_by_field_name("name") == node:
        return False
    if parent.type == "variable_declarator" and parent.child_by_field_name("name") == node:
        return False
    if parent.type == "method_invocation" and parent.child_by_field_name("name") == node:
        return False
    if parent.type == "field_access" and parent.child_by_field_name("field") == node:
        return False
    return not _has_ancestor(node, _JAVA_IMPORT_CONTEXT)


def _java_names(root: Node) -> tuple[list[str], list[str]]:
    """Определения и ссылки Java."""
    return _collect_names(
        root,
        ("class_declaration", "method_declaration", "constructor_declaration"),
        ("identifier", "type_identifier"),
        _java_is_reference,
    )


def _cs_parameter(node: Node) -> tuple[str, list[Node | None]]:
    """Текст параметра C# (тип и имя, значение по умолчанию становится `...`) и раскрываемые узлы."""
    if node.type != "parameter":
        return _text(node), [node]
    type_node = node.child_by_field_name("type")
    name = node.child_by_field_name("name")
    has_default = any(child.type == "=" for child in node.children)
    rendered = f"{_text(type_node)} {_text(name)}" if type_node is not None else _text(name)
    if has_default:
        rendered += "=..."
    return rendered, [type_node, name]


def _cs_callable_signature(node: Node, owner: str) -> dict[str, object]:
    """Сигнатура метода или конструктора C# с признаком `static`."""
    is_static = any(
        child.type == "modifier" and _text(child) == "static" for child in node.children
    )
    return _member_callable_signature(
        node, owner, node.child_by_field_name("returns"), is_static, _cs_parameter
    )


def _cs_class_signature(node: Node) -> dict[str, object]:
    """Сигнатура класса C# со списком базовых типов."""
    name = node.child_by_field_name("name")
    base_list = next((child for child in node.children if child.type == "base_list"), None)
    bases = [_text(item) for item in base_list.named_children] if base_list is not None else []
    return {"text": _class_text(name, bases), "symbols": _symbols([name, base_list])}


def _cs_top_level_classes(root: Node) -> Iterator[Node]:
    """Классы C# верхнего уровня и непосредственно внутри блочных `namespace`."""
    for node in root.named_children:
        if node.type == "class_declaration":
            yield node
        elif node.type == "namespace_declaration":
            body = node.child_by_field_name("body")
            if body is not None:
                yield from (child for child in body.named_children if child.type == "class_declaration")


def _cs_signatures(root: Node) -> list[dict[str, object]]:
    # Same resilience rule as Java: a member's syntax error marks the enclosing class_declaration
    # `has_error` too, so only individual members are gated below.
    """Классы C# с методами и конструкторами; член с ошибкой пропускается, соседние сохраняются."""
    found: list[dict[str, object]] = []
    for node in _cs_top_level_classes(root):
        found.append(_cs_class_signature(node))
        owner = _text(node.child_by_field_name("name"))
        body = node.child_by_field_name("body")
        for member in body.named_children if body is not None else []:
            if member.type in ("method_declaration", "constructor_declaration") and not member.has_error:
                found.append(_cs_callable_signature(member, owner))
    return found


def _cs_is_reference(node: Node) -> bool:
    """Проверить, является ли идентификатор C# ссылкой, а не объявлением, параметром, членом или `using`."""
    parent = node.parent
    if parent is None:
        return False
    if (
        parent.type in ("class_declaration", "method_declaration", "constructor_declaration", "namespace_declaration")
        and parent.child_by_field_name("name") == node
    ):
        return False
    if parent.type == "parameter" and parent.child_by_field_name("name") == node:
        return False
    if parent.type == "variable_declarator" and parent.child_by_field_name("name") == node:
        return False
    if parent.type == "member_access_expression" and parent.child_by_field_name("name") == node:
        return False
    return not _has_ancestor(node, _CS_IMPORT_CONTEXT)


def _cs_names(root: Node) -> tuple[list[str], list[str]]:
    """Определения и ссылки C#."""
    return _collect_names(
        root,
        ("class_declaration", "method_declaration", "constructor_declaration"),
        ("identifier",),
        _cs_is_reference,
    )


_LANGUAGE_EXTRACTORS: dict[
    str,
    tuple[
        Callable[[Node], list[dict[str, object]]],
        Callable[[Node], list[dict[str, object]]],
        Callable[[Node], tuple[list[str], list[str]]],
    ],
] = {
    "python": (_signatures, _imports, _names),
    "typescript": (_js_signatures, _js_imports, _js_names),
    "tsx": (_js_signatures, _js_imports, _js_names),
    "javascript": (_js_signatures, _js_imports, _js_names),
    "go": (_go_signatures, _no_imports, _go_names),
    "java": (_java_signatures, _no_imports, _java_names),
    "csharp": (_cs_signatures, _no_imports, _cs_names),
}
# Grammar name -> (package in the bundle, factory returning the language pointer).
_GRAMMAR_PACKAGES: dict[str, tuple[str, str]] = {
    "python": ("tree_sitter_python", "language"),
    "typescript": ("tree_sitter_typescript", "language_typescript"),
    "tsx": ("tree_sitter_typescript", "language_tsx"),
    "javascript": ("tree_sitter_javascript", "language"),
    "go": ("tree_sitter_go", "language"),
    "java": ("tree_sitter_java", "language"),
    "csharp": ("tree_sitter_c_sharp", "language"),
}
# The bundle lock assigns extensions to grammars and repo_map sends that table as `languages`. This
# default only serves a harness released before it did, so a newer bundle keeps working with it.
_DEFAULT_LANGUAGES = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "javascript",
    ".go": "go",
    ".java": "java",
    ".cs": "csharp",
}


def _file_facts(parser: Parser, content: bytes, language: str) -> dict[str, object]:
    """Разобрать один файл и вернуть его факты; неверный UTF-8 даёт `invalid_encoding` без фактов."""
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
    signatures_fn, imports_fn, names_fn = _LANGUAGE_EXTRACTORS[language]
    definitions, references = names_fn(root)
    return {
        "parser_status": "syntax_error" if root.has_error else "ok",
        "signatures": signatures_fn(root),
        "imports": imports_fn(root),
        "definitions": definitions,
        "references": references,
    }


def _load_parser(grammar: str) -> Parser | None:
    """Импортировать пакет грамматики и создать парсер; `None`, если пакет недоступен в bundle."""
    import tree_sitter

    module_name, factory = _GRAMMAR_PACKAGES[grammar]
    try:
        module = importlib.import_module(module_name)
        language = tree_sitter.Language(getattr(module, factory)())
    except (ImportError, AttributeError, ValueError):
        return None
    return tree_sitter.Parser(language)


def _parser_for(grammar: str, parsers: dict[str, Parser | None]) -> Parser | None:
    """Парсер грамматики, загружаемый при первом обращении и переиспользуемый дальше."""
    if grammar not in parsers:
        parsers[grammar] = _load_parser(grammar)
    return parsers[grammar]


def _request_languages(request: dict[str, object]) -> dict[str, str]:
    """Соответствие расширения грамматике из запроса или из совместимого значения по умолчанию."""
    languages = request.get("languages")
    if not isinstance(languages, dict):
        return _DEFAULT_LANGUAGES
    return {
        str(extension): str(grammar)
        for extension, grammar in languages.items()
        if isinstance(extension, str) and isinstance(grammar, str)
    }


def main() -> None:
    """Точка входа worker: прочитать запрос из stdin, разобрать файлы и вывести факты в stdout.

    Грамматики загружаются лениво из `<install_dir>`: файл грамматики, которой нет в bundle или в
    `_GRAMMAR_PACKAGES`, пропускается, а остальные языки разбираются как обычно.
    """
    sys.path.insert(0, sys.argv[1])
    request = json.loads(sys.stdin.buffer.read())
    languages = _request_languages(request)
    parsers: dict[str, Parser | None] = {}
    files: dict[str, dict[str, object]] = {}
    for path, encoded in sorted(request.get("paths", {}).items()):
        grammar = languages.get(PurePosixPath(path).suffix)
        if grammar is None or grammar not in _LANGUAGE_EXTRACTORS:
            continue
        parser = _parser_for(grammar, parsers)
        if parser is not None:
            files[path] = _file_facts(parser, base64.b64decode(encoded), grammar)
    sys.stdout.write(json.dumps({"files": files}, sort_keys=True))


if __name__ == "__main__":
    main()
