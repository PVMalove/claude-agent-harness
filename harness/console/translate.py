"""Switching the console between Russian and English without rebuilding screens.

Screens are written in Russian. `retranslate` walks a screen and rewrites the fixed texts of its
widgets - frame titles, button labels, placeholders, tab titles, fixed hints, key-binding
descriptions - through `i18n.tr`. The first text a widget showed is kept as its source, so
switching back and forth never compounds; a text a screen sets later is adopted as the new source."""

from __future__ import annotations

from dataclasses import replace
from typing import Callable, cast

from textual.dom import DOMNode
from textual.widgets import (
    Button,
    Input,
    Select,
    Static,
    TabbedContent,
    TabPane,
)
from textual.widgets._toggle_button import ToggleButton

from . import brand, i18n
from .chrome import TopBar


def _translated(
    node: DOMNode,
    attr: str,
    current: str,
    language: str,
    wrap: Callable[[str], str] | None = None,
    strip: bool = False,
    phrases: bool = False,
) -> str:
    """The text for `attr` in `language`; remembers the source and what was produced."""
    memory: dict[str, tuple[str, str]] = node.__dict__.setdefault("_i18n_src", {})
    source, produced = memory.get(attr, (current, current))
    if current != produced:
        source = current  # the screen set a new text since: it is the new source
    if strip:
        source = source.strip()
    text = i18n.tr_text(language, source) if phrases else i18n.tr(language, source)
    text = wrap(text) if wrap else text
    memory[attr] = (source, text)
    return text


def _translate_widget(widget: DOMNode, language: str) -> None:
    title = getattr(widget, "border_title", None)
    if title and str(title).strip():
        widget.border_title = _translated(  # type: ignore[attr-defined]
            widget, "border_title", str(title), language, _title(widget), strip=True
        )
    if isinstance(widget, Button):
        widget.label = _translated(widget, "label", str(widget.label), language)
        widget.refresh(layout=True)
    elif isinstance(widget, Input) and widget.placeholder:
        widget.placeholder = _translated(
            widget, "placeholder", widget.placeholder, language
        )
    elif isinstance(widget, Select) and isinstance(widget.prompt, str):
        widget.prompt = _translated(widget, "prompt", widget.prompt, language)
    elif isinstance(widget, ToggleButton):
        widget.label = _translated(widget, "label", str(widget.label), language)
    elif (
        isinstance(widget, Static)
        and type(widget) is Static
        and not isinstance(widget.parent, TopBar)
        and not widget.has_class("no-i18n")
    ):
        content = getattr(widget, "content", None)
        if isinstance(content, str) and content:
            widget.update(
                _translated(widget, "content", content, language, phrases=True)
            )
    elif isinstance(widget, TopBar):
        widget.refresh_status()


def _title(widget: DOMNode) -> Callable[[str], str]:
    """Frame titles are padded with spaces; keep that padding."""
    return lambda text: brand.frame_title(text)


def _translate_tabs(root: DOMNode, language: str) -> None:
    for tabbed in root.query(TabbedContent):
        for pane in tabbed.query(TabPane):
            if not pane.id:
                continue
            tab = tabbed.get_tab(pane.id)
            tab.label = _translated(tab, "label", str(tab.label), language)


def translate_bindings(node: DOMNode, language: str) -> None:
    """Rewrite the descriptions the footer shows for `node`'s key bindings."""
    memory: dict[tuple[str, str], str] = node.__dict__.setdefault("_i18n_bindings", {})
    table = node._bindings.key_to_bindings
    for key, bindings in list(table.items()):
        rewritten = []
        for binding in bindings:
            if not binding.description:
                rewritten.append(binding)
                continue
            source = memory.setdefault((key, binding.action), binding.description)
            rewritten.append(replace(binding, description=i18n.tr(language, source)))
        table[key] = rewritten


def retranslate(root: DOMNode, language: str) -> None:
    """Translate `root` and everything inside it."""
    for widget in [root, *root.query("*")]:
        _translate_widget(widget, language)
    _translate_tabs(root, language)
    translate_bindings(root, language)
    sync = getattr(root, "sync_language", None)
    if sync is not None:
        sync()


class Translated:
    """Mixin for screens: translate on mount, on resume and after every recompose."""

    def on_mount(self) -> None:
        self.call_after_refresh(self.translate_now)  # type: ignore[attr-defined]

    def on_screen_resume(self) -> None:
        self.translate_now()

    async def recompose(self) -> None:
        await super().recompose()  # type: ignore[misc]
        self.translate_now()

    def translate_now(self) -> None:
        retranslate(cast(DOMNode, self), getattr(self.app, "language", "ru"))  # type: ignore[attr-defined]
        self.app.refresh_bindings()  # type: ignore[attr-defined]
