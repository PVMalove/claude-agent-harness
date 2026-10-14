"""The future operational-store vocabularies have a versioned, checkable contract."""

import importlib

from harness.orchestration.core.system_fields import load_registry


def test_runtime_store_states_and_transition_reference_are_registered() -> None:
    registry = load_registry()

    assert set(registry.fields["runtime.state"].allowed) == {
        "ready",
        "running",
        "suspended",
        "reconciling",
        "completed",
    }
    assert registry.fields["runtime.state"].transition_contract == (
        "harness.orchestration.core.runtime_state_contract:RUNTIME_TRANSITIONS"
    )
    assert registry.fields["batch.state"].transition_contract == (
        "harness.orchestration.ledger.lifecycle:LifecycleLedger._validate_batch_transition"
    )


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
