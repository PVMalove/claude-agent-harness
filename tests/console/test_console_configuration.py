"""Configuration drafts are read-only until explicit application."""

from __future__ import annotations

import json
from pathlib import Path

from harness.console.configuration import ConfigurationDraft


def test_editing_and_validating_a_draft_preserves_the_active_file(
    tmp_path: Path,
) -> None:
    root = tmp_path / ".harness"
    root.mkdir()
    original = {
        "language": "ru",
        "base_branch": "main",
        "branch_pattern": "^feature/issue-[0-9]+-.+",
        "qa_gate_commands": ["pytest"],
        "tracker": {"type": "local"},
    }
    path = root / "project.json"
    path.write_text(json.dumps(original), encoding="utf-8")
    before = path.read_bytes()
    draft = ConfigurationDraft.load(tmp_path, ("project.json",))
    draft.set_value("project.json", ("language",), "en")
    assert draft.problems() == []
    assert draft.documents["project.json"]["tracker"] == {"type": "local"}
    assert '"en"' in draft.diff()
    assert path.read_bytes() == before


def test_a_missing_project_file_starts_from_a_valid_default(tmp_path: Path) -> None:
    # The template gained test-field placeholders; the default fills them as `harness init`
    # does without test flags, so no placeholder text breaks the JSON.
    draft = ConfigurationDraft.load(tmp_path, ("project.json",))

    document = draft.documents["project.json"]
    assert document["qa_gate_commands"] == []
    assert "test_paths" not in document
    assert not (tmp_path / ".harness/project.json").exists()


_SECRET_VALUES: list[object] = [
    {"token": "abc"},
    {"tracker": {"type": "github", "api_key": "k"}},
    {"url": "https://user:pass@example.com/repo"},
    {"command": "run --token=abc123"},
    {"header": "Bearer abcdef"},
    ["ok", "sk-abcdefghijklmnop"],
    {"list": [{"password": 1}]},
    {"authorization": True},
    {"items": ["ghp_abcdefghijklmnop"]},
    {1: "non-string key"},
]
_PLAIN_VALUES: list[object] = [
    {"language": "ru", "tracker": {"type": "local"}},
    {"url": "https://example.com/repo"},
    {"command": "run --token=$TOKEN"},
    ["pytest", "ruff check"],
    "plain text",
    7,
    None,
    {"flag": True},
    {"tokenizer": "x", "token_limit": 5},
]


def test_mask_secrets_changes_a_value_exactly_when_reject_secrets_refuses_it() -> None:
    import copy

    import pytest

    from harness.configuration import SECRET_MASK, mask_secrets, reject_secrets

    for value in _SECRET_VALUES:
        before = copy.deepcopy(value)
        with pytest.raises(ValueError):
            reject_secrets(value)
        assert mask_secrets(value) != value
        assert value == before
    for value in _PLAIN_VALUES:
        before = copy.deepcopy(value)
        reject_secrets(value)
        assert mask_secrets(value) == value
        assert value == before
    assert mask_secrets({"token": "abc", "name": "x"}) == {
        "token": SECRET_MASK,
        "name": "x",
    }
    assert mask_secrets(
        {"tracker": {"url": "https://u:p@h/x", "type": "g"}}
    ) == {"tracker": {"url": SECRET_MASK, "type": "g"}}
    assert mask_secrets("abc", "token") == SECRET_MASK
    assert mask_secrets({"a": "b"}, "token") == SECRET_MASK
    assert mask_secrets("abc", "name") == "abc"
    # The mask is never taken for a secret text, but under a secret key it is still refused.
    reject_secrets(SECRET_MASK)
    with pytest.raises(ValueError):
        reject_secrets({"token": SECRET_MASK})


def _bundle(**files: object) -> dict[str, bytes | None]:
    return {
        name: None if value is None else json.dumps(value, indent=2).encode()
        for name, value in files.items()
    }


def test_bundle_diff_is_unchanged_without_secrets_and_masks_a_stored_secret() -> None:
    from harness.configuration import SECRET_MASK
    from harness.console.configuration import bundle_diff

    plain_old = {"project.json": b'{"a": 1}\n', "orchestration.json": None}
    plain_new = {"project.json": b'{"a": 2}\n', "orchestration.json": None}
    assert bundle_diff(plain_old, plain_new, "old", "new") == (
        "--- old/project.json\n+++ new/project.json\n@@ -1 +1 @@\n"
        '-{"a": 1}\n+{"a": 2}\n'
    )
    old = _bundle(**{"project.json": {"tracker": {"type": "a", "token": "S3CRET"}}})
    new = _bundle(**{"project.json": {"tracker": {"type": "b", "token": "S3CRET"}}})
    old["orchestration.json"] = new["orchestration.json"] = None
    diff = bundle_diff(old, new, "old", "new")
    assert "S3CRET" not in diff and SECRET_MASK in diff
    assert '-    "type": "a"' in diff and '+    "type": "b"' in diff
    assert diff.count("изменены только скрытые значения") == 0


def test_bundle_diff_says_when_only_hidden_values_changed_and_hides_unparsed_files() -> None:
    from harness.console.configuration import bundle_diff

    old = _bundle(**{"project.json": {"token": "ONE"}})
    new = _bundle(**{"project.json": {"token": "TWO"}})
    old["orchestration.json"] = new["orchestration.json"] = None
    diff = bundle_diff(old, new, "old", "new")
    assert "ONE" not in diff and "TWO" not in diff
    assert "project.json: изменены только скрытые значения или форматирование" in diff
    english = bundle_diff(old, new, "old", "new", language="en")
    assert "project.json: only hidden values or formatting changed" in english
    broken = {"project.json": b"{token: SECRET", "orchestration.json": None}
    fixed = {"project.json": b'{"a": 1}\n', "orchestration.json": None}
    hidden = bundle_diff(broken, fixed, "old", "new")
    assert "SECRET" not in hidden
    assert "содержимое скрыто: файл не разобран как JSON" in hidden
    assert "file is not valid JSON" in bundle_diff(
        broken, fixed, "old", "new", language="en"
    )


def test_bundle_diff_masks_a_secret_under_an_overridden_duplicate_key() -> None:
    from harness.console.configuration import bundle_diff

    duplicate = b'{"url": "https://user:pass@host", "url": "https://host"}\n'
    nested = b'{"a": {"token": "HUNTER2", "token": "x"}, "b": 1}\n'
    clean = b'{"url": "https://host"}\n'
    for raw, secret in ((duplicate, "user:pass"), (nested, "HUNTER2")):
        old = {"project.json": raw, "orchestration.json": None}
        new = {"project.json": clean, "orchestration.json": None}
        for diff in (
            bundle_diff(old, new, "old", "new"),
            bundle_diff(new, old, "old", "new"),
        ):
            assert secret not in diff
    plain = b'{"url": "https://host", "url": "https://other"}\n'
    shown = bundle_diff(
        {"project.json": plain, "orchestration.json": None},
        {"project.json": clean, "orchestration.json": None},
        "old",
        "new",
    )
    assert '-{"url": "https://host", "url": "https://other"}' in shown
