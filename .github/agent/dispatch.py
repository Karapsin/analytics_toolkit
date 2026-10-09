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
        if task['value'] != 'all':
            raise ValueError('Only exhaustive integration may be requested')
    elif task['operation'] == 'rerun':
        if not isinstance(task['value'], int) or task['value'] < 1:
            raise ValueError('Invalid run ID')
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
    else:
        path = 'actions/workflows/sql-integration.yml/dispatches'
        data = {'ref': 'dev', 'inputs': {'profile': 'all'}}
    subprocess.run(['gh', 'api', prefix + path, '--method', 'POST', '--input', '-'],
                   input=json.dumps(data), text=True, check=True)


if __name__ == '__main__':
    main()
