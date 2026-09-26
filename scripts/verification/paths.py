"""Пути репозитория, общие для проверок."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCS_AGENTS_TEMPLATE = ROOT / "harness" / "project" / "docs-agents"
CAPABILITIES = ROOT / "harness" / "CAPABILITIES.json"
HARNESS_GUIDE = ROOT / "docs" / "agents" / "harness-guide.md"
