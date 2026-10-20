"""Transition-contract references of the registry resolve in the packaged namespace."""

import importlib

from harness.orchestration.core.system_fields import load_registry


def test_all_registered_transition_references_resolve_in_the_packaged_namespace() -> (
    None
):
    for field in load_registry().fields.values():
        if field.transition_contract is None:
            continue
        module, name = field.transition_contract.split(":")
        target: object = importlib.import_module(module)
        for part in name.split("."):
            target = getattr(target, part)
        assert target is not None
        if isinstance(target, tuple):
            # Dispatch transitions reference real public command handlers, not only a tuple.
            for reference in target:
                module_name, handler = reference.split(":")
                assert callable(getattr(importlib.import_module(module_name), handler))
