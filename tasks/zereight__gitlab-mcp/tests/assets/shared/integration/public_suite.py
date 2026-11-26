"""Check the required ported suite without grading candidate-added assertions.

Discovery and the complete candidate suite still run and are recorded.  Their
additional assertions cannot define the task contract.  The original public
suite's required behaviours are independently asserted over the public MCP
interface, so neither a failing extra test nor a vacuous passing suite decides
whether mode validation, JSON safety, IDs, file safety and paths are correct.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from mcp_stdio import McpStdioClient

_DISCOVER = r'''
import json, sys, unittest
root = sys.argv[1]
loader = unittest.TestLoader()
suite = loader.discover(root + '/tests', top_level_dir=root)
print(json.dumps({'count': suite.countTestCases(), 'errors': loader.errors}))
'''


def inspect_suite(repo: str, python: str) -> dict:
    server = str(Path(repo) / 'mcp-server')
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    environment['PYTHONPATH'] = server + os.pathsep + environment.get('PYTHONPATH', '')
    discovery = subprocess.run(
        [python, '-c', _DISCOVER, server], cwd=repo, env=environment,
        capture_output=True, text=True, timeout=300,
    )
    try:
        discovered = json.loads(discovery.stdout)
        discovery_ok = (discovery.returncode == 0 and discovered['count'] > 0
                        and not discovered['errors'])
    except (ValueError, KeyError, TypeError):
        discovered, discovery_ok = {}, False
    # Run every assertion, including extras, and retain the actual outcome.
    completed = subprocess.run(
        [python, '-m', 'unittest', 'discover', '-s', server + '/tests', '-t', server, '-v'],
        cwd=repo, env=environment, capture_output=True, text=True, timeout=300,
    )
    return {
        'ok': discovery_ok,
        'discovery': discovered,
        'discovery_returncode': discovery.returncode,
        'discovery_stderr': discovery.stderr[-4000:],
        'candidate_suite': {'returncode': completed.returncode,
                            'stdout': completed.stdout[-4000:],
                            'stderr': completed.stderr[-8000:]},
    }


class PublicContractTests(unittest.TestCase):
    """Behavioural assertions from the original mcp-server public suite."""

    server: str

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='ported-contract-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.client = McpStdioClient(self.server, env_extra={'WORKSPACE_ROOT': str(self.root)})
        self.addCleanup(self.client.close)

    def call(self, name, **arguments):
        result = self.client.call_tool(name, arguments)
        blocks = result.get('content', [])
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].get('type'), 'text')
        return blocks[0]['text']

    def payload(self, name, **arguments):
        return json.loads(self.call(name, **arguments))

    def test_mode_validation_and_state_paths(self):
        for mode in ('omg-autopilot', 'ralph', 'team', 'foo_bar', 'Mode123', '__proto__'):
            result = self.payload('omg_write_state', mode=mode, state='{"value":1}')
            self.assertIs(result.get('success'), True)
            path = self.root / '.omc' / 'state' / (mode + '-state.json')
            self.assertEqual(json.loads(path.read_text())['value'], 1)
        for mode in ('', '../evil', 'a/b', 'a b', 'mode!'):
            result = self.payload('omg_read_state', mode=mode)
            self.assertIs(result.get('success'), False)
            self.assertEqual(result.get('error'),
                             f'Invalid mode name: "{mode}". Only alphanumeric, hyphens, and underscores allowed.')

    def test_object_json_and_pollution_keys(self):
        for value in ('not json', '[]', '42', 'true', 'null', '"text"'):
            result = self.payload('omg_write_state', mode='ralph', state=value)
            self.assertIs(result.get('success'), False)
            self.assertTrue(result.get('error'))
        result = self.payload('omg_write_state', mode='ralph', state=json.dumps({
            'ok': 1, '__proto__': {}, 'constructor': {}, 'prototype': {},
        }))
        self.assertIs(result.get('success'), True)
        self.assertEqual(result['state']['ok'], 1)
        for key in ('__proto__', 'constructor', 'prototype'):
            self.assertNotIn(key, result['state'])
        self.assertEqual(self.payload('omg_write_state', mode='empty', state='{}')['mode'], 'empty')

    def test_uuid_identifiers(self):
        self.payload('omg_create_prd', title='Identifiers', stories=[{'title': str(i)} for i in range(20)])
        stories = json.loads((self.root / '.omc' / 'prd.json').read_text())['stories']
        for story in stories:
            self.assertRegex(story['id'], r'^story-[0-9a-f]{8}$')
        self.assertEqual(len({story['id'] for story in stories}), 20)

    def test_safe_files(self):
        state = self.root / '.omc' / 'state'
        state.mkdir(parents=True)
        regular = state / 'regular-state.json'
        regular.write_text('stored text', encoding='utf-8')
        self.assertEqual(self.call('omg_read_state', mode='regular'), 'stored text')
        target = self.root / 'target.json'
        target.write_text('{"untouched":true}')
        (state / 'link-state.json').symlink_to(target)
        (state / 'large-state.json').write_bytes(b'x' * (5 * 1024 * 1024 + 1))
        unreadable = state / 'unreadable-state.json'
        unreadable.write_text('secret')
        unreadable.chmod(0)
        self.addCleanup(unreadable.chmod, 0o600)
        for mode in ('missing', 'link', 'large', 'unreadable'):
            self.assertEqual(self.payload('omg_read_state', mode=mode),
                             {'active': False, 'mode': mode, 'message': 'No state found'})
        denied = self.payload('omg_write_state', mode='link', state='{}')
        self.assertIs(denied.get('success'), False)
        self.assertIn('Refusing to write to symlink', denied.get('error', ''))
        self.assertEqual(target.read_text(), '{"untouched":true}')
        self.assertIs(self.payload('omg_write_state', mode='new', state='{}').get('success'), True)
        self.assertTrue((state / 'new-state.json').is_file())


def run_contract(repo: str) -> dict:
    PublicContractTests.server = str(Path(repo) / 'mcp-server')
    result = unittest.TestResult()
    unittest.defaultTestLoader.loadTestsFromTestCase(PublicContractTests).run(result)
    return {'ok': result.wasSuccessful(), 'run': result.testsRun,
            'failures': [(test.id(), reason) for test, reason in result.failures],
            'errors': [(test.id(), reason) for test, reason in result.errors]}


def review_suite(repo: str, python: str) -> dict:
    suite = inspect_suite(repo, python)
    contract = run_contract(repo)
    return {'ok': suite['ok'] and contract['ok'], 'ported_suite': suite, 'contract': contract}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True)
    parser.add_argument('--python', default=sys.executable)
    args = parser.parse_args()
    result = review_suite(args.repo, args.python)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
