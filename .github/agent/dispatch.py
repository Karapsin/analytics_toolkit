"""Fixed GitHub-only dispatch bridge for the App's read-only Actions permission."""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

PREFIX = '<!-- github-agent-dispatch\n'


def validated(event, slug):
    comment = event['comment']
    if event['sender']['login'] != slug + '[bot]' or event['sender']['type'] != 'Bot':
        raise ValueError('Unexpected sender')
    if event['issue']['title'] != 'GitHub agent integration monitor' or 'pull_request' in event['issue']:
        raise ValueError('Dispatch must use the monitor issue')
    text = comment['body']
    if len(text) > 2000 or not text.startswith(PREFIX) or not text.endswith('\n-->'):
        raise ValueError('Invalid request envelope')
    task = json.loads(text[len(PREFIX):-4])
    if set(task) != {'operation', 'value'}:
        raise ValueError('Invalid request fields')
    if task['operation'] == 'visual':
        if not isinstance(task['value'], str) or not re.fullmatch('[0-9a-f]{40}', task['value']):
            raise ValueError('Visual candidate must be an immutable SHA')
    elif task['operation'] == 'integration':
        value = task['value']
        if value != 'all' and not (isinstance(value, dict) and set(value) == {'candidate', 'number'}
                and isinstance(value['candidate'], str) and re.fullmatch('[0-9a-f]{40}', value['candidate'])
                and isinstance(value['number'], int) and value['number'] > 0):
            raise ValueError('Only exhaustive integration may be requested')
    elif task['operation'] == 'rerun':
        if not isinstance(task['value'], int) or task['value'] < 1:
            raise ValueError('Invalid run ID')
    elif task['operation'] == 'python-support':
        value = task['value']
        if (not isinstance(value, dict) or set(value) != {'candidate', 'python'}
                or not isinstance(value['candidate'], str)
                or not re.fullmatch('[0-9a-f]{40}', value['candidate'])
                or not isinstance(value['python'], str)
                or not re.fullmatch(r'3\.[0-9]+', value['python'])):
            raise ValueError('Python support probes require a SHA and minor version')
    else:
        raise ValueError('Unsupported operation')
    return task


def main():
    event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text())
    task = validated(event, os.environ['APP_SLUG'])
    prefix = 'repos/' + os.environ['GITHUB_REPOSITORY'] + '/'
    if task['operation'] == 'rerun':
        run = json.loads(subprocess.check_output(['gh', 'api', prefix + 'actions/runs/' + str(task['value'])]))
        if run['path'] != '.github/workflows/sql-integration.yml' or run['head_branch'] != 'dev':
            raise ValueError('Only dev integration runs may be retried')
        path, data = 'actions/runs/' + str(task['value']) + '/rerun-failed-jobs', {}
    elif task['operation'] == 'visual':
        path = 'actions/workflows/agent-visual.yml/dispatches'
        data = {'ref': 'main', 'inputs': {'candidate': task['value']}}
    elif task['operation'] == 'python-support':
        value = task['value']
        base = json.loads(subprocess.check_output(['gh', 'api', prefix + 'git/ref/heads/dev']))
        if base['object']['sha'] != value['candidate']:
            raise ValueError('Python support base changed before dispatch')
        path = 'actions/workflows/python-support.yml/dispatches'
        data = {'ref': 'main', 'inputs': value}
    else:
        path = 'actions/workflows/sql-integration.yml/dispatches'
        inputs = {'profile': 'all'}
        if isinstance(task['value'], dict):
            value = task['value']
            pr = json.loads(subprocess.check_output(['gh', 'api', prefix + 'pulls/' + str(value['number'])]))
            if pr['base']['ref'] != 'dev' or pr['head']['sha'] != value['candidate']:
                raise ValueError('PR candidate changed before dispatch')
            inputs.update(candidate=value['candidate'], pull_number=str(value['number']))
        data = {'ref': 'dev', 'inputs': inputs}
    subprocess.run(['gh', 'api', prefix + path, '--method', 'POST', '--input', '-'],
                   input=json.dumps(data), text=True, check=True)


if __name__ == '__main__':
    main()
