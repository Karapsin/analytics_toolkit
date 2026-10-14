"""Trusted, source-only Python support proposals; never run candidate code here."""
from __future__ import annotations

import base64
import hashlib
import importlib
import json
import os
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import urlopen

PREFIX = 'codex/python-support-'
WORKFLOW = '.github/workflows/python-support.yml'
FEED = 'https://www.python.org/api/v2/downloads/release/?is_published=true'


def policy_module():
    # An isolated package prevents candidate cwd/PYTHONPATH from replacing helpers.
    name = '_trusted_python_support'
    if name not in sys.modules:
        package = types.ModuleType(name)
        package.__path__ = [str(Path(__file__).resolve().parents[2] / 'release_routines/lib')]
        sys.modules[name] = package
    return importlib.import_module(name + '.python_support')


def read_policy(gh, base):
    item = gh.api('contents/release_routines/python_support.json?ref=' + base)
    if item.get('type') != 'file' or item.get('encoding') != 'base64':
        raise ValueError('Python support policy must be a regular JSON file')
    value = json.loads(base64.b64decode(item['content']))
    policy_module().validate_policy(value)
    return value


def ready_extras(gh, base, latest, now):
    expected = f'Python support {base} {latest}'
    runs = gh.pages('actions/workflows/python-support.yml/runs?event=workflow_dispatch&per_page=100', 'workflow_runs')
    for run in runs:
        if (run.get('display_title') != expected or run.get('head_branch') != 'main'
                or run.get('path') != WORKFLOW or run.get('status') != 'completed'):
            continue
        created = datetime.fromisoformat(run['created_at'].replace('Z', '+00:00'))
        if not now - timedelta(hours=48) <= created <= now:
            continue
        jobs = gh.pages(f"actions/runs/{run['id']}/jobs?filter=latest&per_page=100", 'jobs')
        results = {job['name']: job['conclusion'] for job in jobs}
        # No successful extra can justify changing support if core failed.
        if results.get('probe (core)') != 'success':
            return set(), run['html_url']
        extras = {*policy_module().EXTRAS, 'all'}
        ready = {extra for extra in extras if results.get(f'probe ({extra})') == 'success'}
        return ready, run['html_url']
    return set(), None


def proposal_context(proto, identity, latest, evidence):
    return {'version': 1, 'task_id': identity, 'producer_id': 'python-support:' + identity,
            'source': 'standing-python-support', 'corrections': [],
            'starting_prompt': 'User-authorized standing Python support maintenance: support '
            'the seven latest stable Python minors. Add newest core support only after '
            'installation and tests pass. Retain older minors while extras lag; allow '
            'unbounded overlap. Shrink to seven only when every extra and all work on the '
            'newest minor. Never reintroduce Python 3.8. Preserve toolkit APIs and Airflow '
            '2/3 compatibility; necessary dependency upgrades and compatibility fixes are '
            'authorized. Extra ranges may be narrower; all is their intersection. Require '
            'fast CI and independent review. Never test or render on the private host. '
            f'Target Python {latest}. Transition identity: {identity}. Python release feed: {FEED}.'}


def publish_proposal(controller, gh, base, policy, proposed, latest, evidence):
    module = policy_module()
    identity = hashlib.sha256(json.dumps({'base_policy': policy, 'proposed': proposed}, sort_keys=True).encode()).hexdigest()[:24]
    branch = PREFIX + latest.replace('.', '-') + '-' + identity
    owner = gh.repository.split('/')[0]
    previous = gh.pages('pulls?state=all&head=' + owner + ':' + branch + '&per_page=100')
    if previous:
        return previous[0]['html_url']  # Includes owner cancellations; never force-push.
    directory = Path(os.environ['AGENT_CONTROL_DIR'])
    proto = controller.trusted_module('task_context')
    context = proposal_context(proto, identity, latest, evidence)
    controller.command('git', 'fetch', 'origin', base, cwd=directory)
    with tempfile.TemporaryDirectory(prefix='python-support-') as temporary:
        work = Path(temporary) / 'work'
        controller.command('git', 'worktree', 'add', '--detach', str(work), base, cwd=directory)
        try:
            policy_path = work / 'release_routines/python_support.json'
            paths = ['release_routines/python_support.json', 'pyproject.toml', 'README.md', 'tox.ini', 'docs/PYTHON_SUPPORT.md']
            for path in paths:
                if not (work / path).resolve().is_relative_to(work.resolve()) or (work / path).is_symlink():
                    raise ValueError('Support proposal paths must be regular checkout files')
            policy_path.write_text(json.dumps(proposed, indent=2) + '\n')
            module.sync(work, proposed)
            fragment = 'agent_tools/changelog/python-support-' + identity + '.md'
            (work / fragment).parent.mkdir(parents=True, exist_ok=True)
            (work / fragment).write_text(f'- Update Python support to {proposed["minimum"]}–{proposed["maximum"]} with validated extra readiness.\n')
            paths.append(fragment)
            controller.command('git', 'add', '--', *paths, cwd=work)
            controller.command('git', 'diff', '--cached', '--check', cwd=work)
            if gh.base() != base:
                raise RuntimeError('Dev changed before Python support publication; retry discovery')
            tree = controller.command('git', 'write-tree', cwd=work)
            message = 'Propose Python ' + latest + ' support window' + proto.trailers(context)
            commit = controller.command('git', *controller.signing_options(), 'commit-tree', '-S', tree, '-p', base, '-m', message, cwd=work)
            # Recover an interrupted publication only if the existing tree/context match.
            remote = controller.command('git', 'ls-remote', 'origin', 'refs/heads/' + branch, cwd=work)
            if remote:
                head = remote.split()[0]
                controller.command('git', 'fetch', 'origin', head, cwd=work)
                if controller.command('git', 'rev-parse', head + '^{tree}', cwd=work) != tree:
                    raise RuntimeError('Existing Python proposal differs; preserve unpublished work')
                proto.verify_commit(context, controller.command('git', 'log', '-1', '--format=%B', head, cwd=work))
            else:
                controller.command('git', 'push', 'origin', commit + ':refs/heads/' + branch, cwd=work)
            pr = gh.api('pulls', 'POST', {'title': 'Update Python support window for ' + latest, 'head': branch, 'base': 'dev',
                'body': 'Core support is accepted only after fast CI and independent review. '
                'Extra ranges retain known limitations during overlap.\n\nEvidence: ' + evidence + '\n\n' + proto.render(context)})
            return pr['html_url']
        finally:
            controller.command('git', 'worktree', 'remove', '--force', str(work), cwd=directory)


def tick(controller, gh, state, base, pulls):
    if not os.environ.get('AGENT_CONTROL_DIR') or not os.environ.get('AGENT_STATE_DIR'):
        return
    record = state.setdefault('python_support', {})
    now = datetime.now(timezone.utc)
    previous = record.get('checked_at')
    if previous and now - datetime.fromisoformat(previous) < timedelta(days=1):
        return
    record['checked_at'] = now.isoformat()
    try:
        policy = read_policy(gh, base)
        with urlopen(FEED, timeout=20) as response:
            releases = json.loads(response.read(2_000_000))
        module = policy_module()
        latest = module.stable_minors(releases)[-1]
        record['latest'] = latest
        if module.minor(latest) < module.minor(policy['maximum']):
            return  # Never roll accepted support backwards on an incomplete feed.
        if any(pr['head']['ref'].startswith(PREFIX) for pr in pulls):
            return  # Existing independent review/correction owns that proposal.
        ready, evidence = ready_extras(gh, base, latest, now)
        controller.dispatch(gh, 'python-support', {'candidate': base, 'python': latest})
        proposed = module.transition(policy, latest, ready)
        record.update(base=base, ready=sorted(ready), evidence=evidence)
        if proposed != policy:
            record['proposal'] = publish_proposal(controller, gh, base, policy, proposed, latest, evidence or 'Readiness pending; CI must validate core support')
        record.pop('error', None)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        record['error'] = str(exc)[:1200]
