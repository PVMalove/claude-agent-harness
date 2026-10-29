#!/usr/bin/env python3
"""The container executor guide is a shipped payload of backend-orchestration only (#710).

It installs as ``.harness/docs/container-executor.md``, comes from ``harness/docs/`` and never from
the source repository's root ``docs/``, and its example adapter keeps the agent's native approval.
"""

from __future__ import annotations

import unittest

from harness.bin import harness as harness_cli

INSTALLED = ".harness/docs/container-executor.md"
SOURCE = harness_cli.PACKAGE / "docs" / "container-executor.md"
# Options that would approve actions on the human's behalf or skip the agent's permission checks.
BYPASS_TOKENS = ("dangerously", "bypassPermissions", "acceptEdits", "skip-permissions")


class ContainerGuideTests(unittest.TestCase):
    def test_backend_orchestration_installs_the_guide_byte_for_byte(self) -> None:
        payload = harness_cli.package_files(["backend-orchestration"])

        self.assertEqual(payload[INSTALLED], SOURCE.read_bytes())

    def test_a_project_without_orchestration_does_not_receive_the_guide(self) -> None:
        payload = harness_cli.package_files(["engineering-suite"])

        self.assertNotIn(INSTALLED, payload)

    def test_the_guide_comes_from_the_package_and_not_from_root_docs(self) -> None:
        root_docs = (harness_cli.ROOT / "docs").resolve()
        sources = harness_cli.selected_resources(["backend-orchestration"])

        self.assertIn(SOURCE, sources)
        self.assertFalse(SOURCE.resolve().is_relative_to(root_docs))

    def test_the_guide_gives_the_build_commands_of_both_engines(self) -> None:
        text = SOURCE.read_text(encoding="utf-8")

        for engine in ("docker", "podman"):
            for dockerfile in ("Dockerfile.sandbox", "Dockerfile.proxy"):
                with self.subTest(engine=engine, dockerfile=dockerfile):
                    self.assertIn(
                        f"{engine} build -f .harness/container/{dockerfile}", text
                    )

    def test_the_guide_marks_podman_and_rootless_docker_as_unverified(self) -> None:
        text = SOURCE.read_text(encoding="utf-8")
        section = text.split("## Verified and unverified environments", 1)[1]
        after = section.split("not verified by a real smoke", 1)[1]
        unverified = after.split("\n\n")[1]

        self.assertIn("Podman, rootful and rootless", unverified)
        self.assertIn("Rootless Docker", unverified)

    def test_the_example_adapter_keeps_native_approval(self) -> None:
        text = SOURCE.read_text(encoding="utf-8")
        example = text.split("## Example project-owned agent adapter", 1)[1]

        self.assertIn('"${HARNESS_EXECUTOR:-}" != docker', example)
        self.assertIn(".allowed_tools", example)
        for token in BYPASS_TOKENS:
            with self.subTest(token=token):
                self.assertNotIn(token, text)


if __name__ == "__main__":
    unittest.main()
