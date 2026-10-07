"""Real-path and replacement semantics of the project access plan."""
from pathlib import Path
from harness.orchestration.core.utils import JsonObject
import subprocess

from harness.orchestration import runtime_access


def test_role_components_replace_defaults_and_keep_operational_roots(tmp_path: Path) -> None:
    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    plan = runtime_access.resolve_plan(repo, repo, {
        'access_policy': {'defaults': {'mode': 'sandbox', 'network': {'hosts': ['github.com']},
                                    'filesystem': [{'resource': 'cache', 'path': '.cache/uv', 'access': 'write'}]},
                          'roles': {'developer': {'network': {'hosts': []}, 'filesystem': []}}}
    }, 'developer', 'write')
    assert plan['mode'] == 'sandbox'
    assert plan['network'] == {'hosts': []}
    assert plan['filesystem'] == []
    assert plan['sources']['network'] == 'roles.developer'
    assert {item['path'] for item in plan['requirements']} == {
        str(repo), str(repo / '.git'), str(repo / '.harness'), str(repo / '.harness/.sandboxes/scratch')}
    assert len(plan['plan_digest']) == 64


def test_native_apply_requires_matching_worker_proof_and_runs_native_action(tmp_path: Path) -> None:
    from datetime import UTC, datetime
    from dataclasses import replace
    from harness.orchestration import extensions
    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    native_modes: list[str] = []

    class NativeRuntime:
        def observe(self, plan: JsonObject, transport: str) -> extensions.RuntimeAccessObservation:
            return extensions.RuntimeAccessObservation(
                plan_digest=plan['plan_digest'], transport=transport,
                supported_modes=('sandbox',), effective_mode='sandbox',
                mechanism='native-apply', environment_id='worker-session', launch_id='reserved-worker',
                source='native-runtime', observed_at=datetime.now(UTC).isoformat(), hosts=(),
                filesystem=tuple((item['path'], item['access']) for item in plan['requirements']))

        def apply(self, brief: JsonObject, observation: extensions.RuntimeAccessObservation) -> extensions.RuntimeAccessObservation:
            native_modes.append(brief['runtime_access']['mode'])
            return replace(observation, applied=True)

    extensions.register('runtime_access', 'test-native', NativeRuntime())
    try:
        plan = runtime_access.resolve_plan(repo, repo, {
            'access_policy': {'defaults': {'mode': 'sandbox'}},
            'extensions': {'runtime_access': 'test-native'}}, 'developer', 'write')
        proof = runtime_access.apply_plan({'runtime_access': plan}, 'in-process')
        assert proof['evidence']['applied'] is True
        assert native_modes == ['sandbox']
    finally:
        extensions.unregister('runtime_access', 'test-native')
