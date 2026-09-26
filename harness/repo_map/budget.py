"""Сериализация Repo Map и отбор файлов в бюджет токенов.

Карта сериализуется в канонический JSON, а `estimated_tokens` учитывает само это поле. Отбор идёт по
ранжированному списку: файл входит в карту, если карта с ним укладывается в бюджет, иначе
пропускается, и проверяются следующие. Размер кандидата считается приращением длины JSON, а не новой
сериализацией всей карты, поэтому отбор линеен по числу файлов, рёбер и диагностик.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from harness.repo_map.graph import Diagnostic, EdgeRecord
from harness.token_estimator import estimate_tokens_for_bytes

# `_sized` stops after this many passes: the digit width of the estimate settles in at most a few.
MAX_SIZE_ITERATIONS = 8


def encode(payload: Mapping[str, object]) -> str:
    """Сериализовать карту в канонический JSON: сортировка ключей, без пробелов, перевод строки в конце."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


def _item_bytes(item: object) -> int:
    """Длина канонического JSON одного элемента списка в байтах UTF-8."""
    return len(json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def converged_estimate(byte_count_without_estimate: int) -> int:
    """Оценка токенов карты, в которой длина без числа `estimated_tokens` равна заданной.

    Число само входит в длину, поэтому оценка повторяется, пока ширина числа не стабилизируется;
    если за `MAX_SIZE_ITERATIONS` итераций сходимости нет, поднимается `ValueError`.
    """
    size = 0
    for _ in range(MAX_SIZE_ITERATIONS):
        next_size = estimate_tokens_for_bytes(byte_count_without_estimate + len(str(size)))
        if next_size == size:
            return size
        size = next_size
    raise ValueError("token estimate did not converge")


def sized(payload: dict[str, object]) -> tuple[str, int]:
    """Сериализовать карту с вычисленным `estimated_tokens`, учитывающим само это поле."""
    payload["estimated_tokens"] = 0
    size = converged_estimate(len(encode(payload).encode("utf-8")) - len("0"))
    payload["estimated_tokens"] = size
    return encode(payload), size


class _ListBytes:
    """Длина JSON-массива, к которому добавляются элементы: `[]`, элементы и запятые между ними."""

    def __init__(self) -> None:
        """Начать с пустого массива."""
        self.items = 0
        self.item_bytes = 0

    def extra_with(self, count: int, item_bytes: int) -> int:
        """Сколько байт массив займёт сверх `[]`, если добавить `count` элементов общей длиной `item_bytes`."""
        total = self.items + count
        return self.item_bytes + item_bytes + max(total - 1, 0)

    def add(self, count: int, item_bytes: int) -> None:
        """Зафиксировать добавление элементов."""
        self.items += count
        self.item_bytes += item_bytes


def select_within_budget(
    payload: dict[str, object],
    ordered: Sequence[str],
    files: Mapping[str, Mapping[str, object]],
    edges: Sequence[EdgeRecord],
    diagnostics: Sequence[Diagnostic],
    max_tokens: int,
) -> str:
    """Отобрать файлы по порядку `ordered`, пока карта укладывается в `max_tokens`, и сериализовать её.

    В карту попадают рёбра только между выбранными файлами и диагностика только выбранных файлов.
    `payload` должен содержать пустые `files`, `edges` и `diagnostics`; он дополняется результатом.
    """
    payload["files"], payload["edges"], payload["diagnostics"] = [], [], []
    payload["estimated_tokens"] = 0
    base_bytes = len(encode(payload).encode("utf-8")) - len("0")
    edge_bytes = [_item_bytes(edge) for edge in edges]
    incident: dict[str, list[int]] = {path: [] for path in files}
    for index, edge in enumerate(edges):
        incident[edge["source"]].append(index)
        if edge["target"] != edge["source"]:
            incident[edge["target"]].append(index)
    diagnostic_bytes: dict[str, list[int]] = {}
    for diagnostic in diagnostics:
        diagnostic_bytes.setdefault(diagnostic["path"], []).append(_item_bytes(diagnostic))
    lists = {"files": _ListBytes(), "edges": _ListBytes(), "diagnostics": _ListBytes()}
    selected: set[str] = set()
    for path in ordered:
        new_edges = [
            index
            for index in incident[path]
            if {edges[index]["source"], edges[index]["target"]} <= selected | {path}
        ]
        additions = {
            "files": (1, _item_bytes(files[path])),
            "edges": (len(new_edges), sum(edge_bytes[index] for index in new_edges)),
            "diagnostics": (
                len(diagnostic_bytes.get(path, [])),
                sum(diagnostic_bytes.get(path, [])),
            ),
        }
        candidate_bytes = base_bytes + sum(
            lists[name].extra_with(count, size) for name, (count, size) in additions.items()
        )
        if converged_estimate(candidate_bytes) <= max_tokens:
            selected.add(path)
            for name, (count, size) in additions.items():
                lists[name].add(count, size)
    payload["files"] = [files[path] for path in ordered if path in selected]
    payload["edges"] = [
        edge for edge in edges if edge["source"] in selected and edge["target"] in selected
    ]
    payload["diagnostics"] = [
        diagnostic for diagnostic in diagnostics if diagnostic["path"] in selected
    ]
    return sized(payload)[0]
