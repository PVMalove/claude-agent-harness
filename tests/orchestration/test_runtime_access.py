"""Real-path and replacement semantics of the project access plan."""
from pathlib import Path
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
