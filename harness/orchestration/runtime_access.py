"""Resolve project access without treating settings as runtime permission evidence.

Filesystem permission roots do not replace source write_paths, tool policy or native approval.
No resolver call writes files, probes network hosts or changes a machine profile.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime
from dataclasses import asdict
from . import extensions
from pathlib import Path

from harness.errors import HarnessError
from harness.storage import storage_root
from .contract import access_policy_problems
from .core.utils import JsonObject


class AccessError(HarnessError):
    """The requested role access cannot be safely resolved or verified."""


def _digest(plan: Mapping[str, object]) -> str:
    data = {key: value for key, value in plan.items() if key != 'plan_digest'}
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _root(path: Path) -> str:
    resolved = path.expanduser().resolve()
    if resolved == Path(resolved.anchor) or resolved == Path.home().resolve():
        raise AccessError('access requires an entire filesystem or home directory',
                          remedy='select a specific project, Git, storage or cache directory')
    return str(resolved)


def resolve_plan(repo: Path, worktree: Path, config: Mapping[str, object], role: str,
                 access: str, *, operation: str | None = None) -> JsonObject:
    """Replace specified components, resolve real roots and retain mandatory artifacts.

    A plan without authored access preserves historical inherit behavior. An authored policy
    also exposes operational requirements that an empty filesystem override cannot remove.
    """
    policy = config.get('access_policy')
    if 'access_policy' not in config:
        plan: JsonObject = {'mode': 'inherit', 'network': {'hosts': []}, 'filesystem': [],
                            'sources': {key: 'legacy' for key in ('mode', 'network', 'filesystem')},
                            'requirements': [], 'runtime_extension': 'none'}
        plan['plan_digest'] = _digest(plan)
        return plan
    problems = access_policy_problems(policy, {role})
    # Other role overrides are validated by the config contract, not this selected-role view.
    problems = [problem for problem in problems if 'access_policy.roles has unknown name' not in problem]
    if problems:
        raise AccessError('; '.join(problems), remedy='fix access_policy and repeat preflight before approval')
    assert isinstance(policy, dict)
    selected: JsonObject = {'mode': 'inherit', 'network': {'hosts': []}, 'filesystem': []}
    sources: JsonObject = {key: 'default' for key in selected}
    layers = [('defaults', policy.get('defaults', {}))]
    for section, name in (('roles', role), ('operations', operation)):
        overrides = policy.get(section, {})
        if name is not None and isinstance(overrides, dict) and name in overrides:
            layers.append((f'{section}.{name}', overrides[name]))
    for label, layer in layers:
        assert isinstance(layer, dict)
        for key in selected:
            if key in layer:
                selected[key] = layer[key]
                sources[key] = label
    result = subprocess.run(['git', '-C', str(worktree), 'rev-parse', '--git-common-dir'],
                            capture_output=True, text=True, check=False)
    if result.returncode:
        raise AccessError('cannot resolve shared Git metadata', remedy='prepare a registered Git worktree and repeat preflight')
    common = Path(result.stdout.strip())
    roots = {'checkout': worktree, 'git_common': common if common.is_absolute() else worktree / common,
             'shared_storage': storage_root(repo, require_main_checkout=True)}
    filesystem: list[JsonObject] = []
    for item in selected['filesystem']:
        resource = item['resource']
        path = worktree / Path(item['path']).expanduser() if resource == 'cache' else roots[resource]
        if access == 'read-only' and item['access'] == 'write' and resource != 'cache':
            raise AccessError(f'read-only role cannot request write access to {resource}',
                              remedy='keep source and Git roots read-only; operational scratch is added separately')
        filesystem.append({'resource': resource, 'access': item['access'], 'path': _root(path)})
    requirements = [
        {'resource': 'checkout', 'path': _root(worktree), 'access': 'write' if access == 'write' else 'read'},
        {'resource': 'git_common', 'path': _root(roots['git_common']), 'access': 'write' if access == 'write' else 'read'},
        {'resource': 'shared_storage', 'path': _root(roots['shared_storage']), 'access': 'read'},
        {'resource': 'artifacts', 'path': _root(roots['shared_storage'] / '.sandboxes/scratch'), 'access': 'write'},
    ]
    for item in filesystem:
        if item not in requirements:
            requirements.append(item)
    extensions = config.get('extensions', {})
    extension = extensions.get('runtime_access', 'none') if isinstance(extensions, dict) else 'none'
    plan = {'mode': selected['mode'], 'network': {'hosts': sorted(set(host.lower() for host in selected['network']['hosts']))},
            'filesystem': filesystem, 'sources': sources, 'requirements': requirements,
            'runtime_extension': extension}
    plan['plan_digest'] = _digest(plan)
    return plan


_REMEDY = ("prepare the named roots, hosts and mode in a new runtime session, select a native "
           "runtime_access implementation that verifies the actual worker launch, and repeat preflight")


def verify_plan(plan: JsonObject, transport: str) -> tuple[JsonObject, extensions.RuntimeAccessObservation | None]:
    """Ask the pinned native implementation for fresh, worker-scoped evidence."""
    if plan.get('plan_digest') != _digest(plan):
        raise AccessError('access plan digest mismatch', remedy='create a new dispatch and approval for the resolved access plan')
    if plan['mode'] == 'inherit' and all(source == 'legacy' for source in plan['sources'].values()) and not plan['filesystem'] and not plan['requirements'] and not plan['network']['hosts']:
        return {'status': 'legacy-inherit', 'verified': [], 'unverified': [], 'remedy': None}, None
    provider = extensions.runtime_access(plan['runtime_extension'])
    # The inert extension uses the telemetry observe signature and is never a permission proof.
    observation = None if plan['runtime_extension'] == 'none' else provider.observe(plan, transport)
    reason = _observation_problem(plan, transport, observation)
    summary: JsonObject = {'status': 'unverified' if reason else 'verified', 'reason': reason,
                           'required': {'hosts': plan['network']['hosts'], 'filesystem': plan['requirements']},
                           'verified': [] if reason else ['mode', 'network', 'filesystem'],
                           'unverified': ['mode', 'network', 'filesystem'] if reason else [],
                           'remedy': _REMEDY if reason else None}
    if observation is not None:
        summary['evidence'] = asdict(observation)
    return summary, observation


def _observation_problem(plan: JsonObject, transport: str,
                         observation: extensions.RuntimeAccessObservation | None) -> str | None:
    if not isinstance(observation, extensions.RuntimeAccessObservation):
        return 'native worker access proof is unavailable'
    if observation.plan_digest != plan['plan_digest'] or observation.transport != transport:
        return 'native worker access proof names a different plan or transport'
    if (not observation.environment_id or not observation.launch_id
        or observation.source not in ('native-runtime', 'runtime-adapter')
        or observation.mechanism not in ('native-apply', 'confirmed-inheritance')):
        return 'native worker launch identity or application mechanism is unverified'
    try:
        observed = datetime.fromisoformat(observation.observed_at)
        age = (datetime.now(UTC) - observed).total_seconds()
    except (ValueError, TypeError):
        return 'native worker proof timestamp is invalid'
    if age < 0 or age > 300:
        return 'native worker proof is stale'
    mode = plan['mode']
    if mode not in observation.supported_modes:
        return f'native runtime does not support requested mode {mode}'
    if mode != 'inherit' and observation.effective_mode != mode:
        return 'native worker effective mode differs from the requested mode'
    if any(host not in observation.hosts for host in plan['network']['hosts']):
        return 'native worker network host requirements are unverified'
    if any((item['path'], item['access']) not in observation.filesystem for item in plan['requirements']):
        return 'native worker filesystem requirements are unverified'
    return None


def apply_plan(brief: JsonObject, transport: str) -> JsonObject:
    """Gate handoff and apply the approved plan through the native implementation.

    A new observation comes from the pinned implementation on every send. Neither current
    config nor a previous preflight observation can expand or prove this dispatch's access.
    """
    plan = brief.get('runtime_access')
    if plan is None:
        return {'status': 'legacy-inherit'}
    if not isinstance(plan, dict):
        raise AccessError('invalid pinned access plan', remedy='create a new dispatch with a valid access plan')
    verification, observation = verify_plan(plan, transport)
    if verification['status'] == 'legacy-inherit':
        return verification
    if verification['status'] != 'verified' or observation is None:
        raise AccessError(str(verification['reason']), remedy=_REMEDY)
    provider = extensions.runtime_access(plan['runtime_extension'])
    try:
        applied = provider.apply(brief, observation)
    except Exception as exc:
        raise AccessError('native runtime could not apply the approved worker access', remedy=_REMEDY) from exc
    reason = _observation_problem(plan, transport, applied)
    if (reason or applied is None or not applied.applied
        or applied.environment_id != observation.environment_id
        or applied.launch_id != observation.launch_id):
        raise AccessError(reason or 'native access application or matching inheritance was not confirmed', remedy=_REMEDY)
    return {'status': 'applied', 'evidence': asdict(applied)}
