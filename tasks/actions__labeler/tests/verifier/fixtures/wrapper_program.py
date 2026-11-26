#!/usr/bin/env python3
"""Independent wrapper-protocol fixture; not a Go implementation or oracle.

Use real HTTP when available, or file receipts to test the same input matrix
without requiring a localhost listener in the editing sandbox.
"""
import base64
import fnmatch
import json
import os
from pathlib import Path
import re
import urllib.request

import yaml


def main():
    token = os.environ['INPUT_REPO-TOKEN']
    receipt_path = os.environ.get('LABELER_ADAPTER_TEST_STATE')
    simple_receipt = os.environ.get('LABELER_ADAPTER_TEST_RECEIPT')
    state = json.loads(Path(receipt_path).read_text()) if receipt_path else {
        'pr_number': 123, 'pr_node_id': 'PR_node_1', 'pr_labels': [], 'changed': ['docs/a.md'],
        'head': 'main', 'base': 'main', 'requests': [], 'authorizations': [], 'added': [], 'graphql': []}

    def api(method, path, body=None):
        if not receipt_path and not simple_receipt:
            request = urllib.request.Request(os.environ['GITHUB_API_URL'] + path, method=method,
                data=json.dumps(body).encode() if body is not None else None,
                headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
            return json.loads(urllib.request.urlopen(request).read())
        state['requests'].append([method, path])
        state['authorizations'].append('Bearer ' + token)
        if path.endswith('/files'): return [{'filename': value} for value in state['changed']]
        if method == 'GET' and '/pulls/' in path:
            return {'labels': state['pr_labels'], 'node_id': state['pr_node_id'],
                    'head': {'ref': state['head']}, 'base': {'ref': state['base']}}
        if path.endswith('/graphql'):
            state['graphql'].append(body); return {'data': {}}
        if method == 'POST' and path.endswith('/labels'):
            state['added'].append(body['labels'])
            if simple_receipt: Path(simple_receipt).write_text(json.dumps({'labels': body['labels']}))
            return []
        if '/contents/' in path:
            return {'content': base64.b64encode((state.get('content') or '').encode()).decode()}
        raise ValueError('unexpected fixture request: ' + path)

    raw = os.environ.get('INPUT_PR-NUMBER', '').strip()
    numbers = [int(value.strip()) for value in raw.splitlines() if value.strip()] if raw else [
        json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text())['pull_request']['number']]
    config_path = Path(os.environ['INPUT_CONFIGURATION-PATH'])
    config_text = config_path.read_text() if config_path.exists() else base64.b64decode(
        api('GET', '/repos/monalisa/helloworld/contents/' + str(config_path))['content']).decode()
    configuration = yaml.safe_load(config_text)
    dot = os.environ.get('INPUT_DOT', 'true').lower() == 'true'
    sync = os.environ.get('INPUT_SYNC-LABELS', 'false').lower() == 'true'
    for number in numbers:
        pull = api('GET', '/repos/monalisa/helloworld/pulls/' + str(number))
        files = [item['filename'] for item in api('GET', '/repos/monalisa/helloworld/pulls/' + str(number) + '/files')]
        def option(rule):
            values = []
            for name, item in rule.items():
                if name in {'head-branch', 'base-branch'}:
                    patterns = item if isinstance(item, list) else [item]
                    values.append(any(re.search(pattern, pull[name.split('-')[0]]['ref']) for pattern in patterns))
                elif name == 'changed-files':
                    matches = []
                    for block in item:
                        for patterns in block.values():
                            patterns = patterns if isinstance(patterns, list) else [patterns]
                            matches.append(any((dot or not any(part.startswith('.') for part in path.split('/'))) and
                                (fnmatch.fnmatchcase(path, pattern) or pattern.startswith('**/') and fnmatch.fnmatchcase(path, pattern[3:]))
                                for path in files for pattern in patterns))
                    values.append(any(matches))
                elif name in {'all', 'any'}:
                    values.append((all if name == 'all' else any)(option(child) for child in item))
            return any(values)
        matched = [name for name, rules in configuration.items() if isinstance(rules, list) and all(option(rule) for rule in rules)]
        existing = [item['name'] for item in pull['labels']]
        added = [name for name in matched if name not in existing]
        if added: api('POST', '/repos/monalisa/helloworld/issues/' + str(number) + '/labels', {'labels': added})
        removed = [item for item in pull['labels'] if sync and item['name'] in configuration and item['name'] not in matched]
        if removed: api('POST', '/graphql', {'query': 'mutation removeLabelsFromLabelable',
            'variables': {'input': {'labelableId': pull['node_id'], 'labelIds': [item['node_id'] for item in removed]}}})
        all_labels = [name for name in existing if name not in [item['name'] for item in removed]] + added
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            output.write('new-labels=' + ','.join(added) + '\nall-labels=' + ','.join(all_labels) + '\n')
    if receipt_path: Path(receipt_path).write_text(json.dumps(state))


if __name__ == '__main__':
    main()
