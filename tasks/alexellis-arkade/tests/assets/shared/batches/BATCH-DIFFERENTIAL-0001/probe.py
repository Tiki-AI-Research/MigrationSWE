#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import re
import subprocess

ANSI_RE = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')
# The description separator is one-or-more spaces on purpose: Cobra's tabwriter
# emits a column gap of two or more, while Kong separates the value type from the
# description with a single space.  Requiring a two-space gap dropped every Kong
# flag that carries a description, which turned a real flag into a missing one.
FLAG_RE = re.compile(r'^\s*(?:-([A-Za-z0-9]),\s+)?--([A-Za-z0-9_-]+)(?:=(\S+)| ([A-Za-z][A-Za-z0-9_-]*))?(?:\s+(.*))?$')


def clean(value):
    value = ANSI_RE.sub('', value or '')
    value = value.replace('\r\n', '\n').replace('\r', '')
    return '\n'.join(line.rstrip() for line in value.split('\n')).strip()


def strip_error_prefix(value):
    return re.sub(r'(?m)^Error: ', '', value)


def parse_flags(text):
    flags = {}
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        match = FLAG_RE.match(lines[index])
        if not match:
            index += 1
            continue
        short, long_name, equals_value, space_value, description = match.groups()
        value = equals_value if equals_value is not None else space_value
        description = description or ''
        following = index + 1
        while following < len(lines):
            line = lines[following]
            if not line.strip() or FLAG_RE.match(line):
                break
            if re.match(r'^\s{2,}\S', line):
                description += ' ' + line.strip()
                following += 1
                continue
            break
        index = following if following > index else index + 1
        kind = 'bool'
        default = False
        if value is not None:
            if value.startswith('"') and value.endswith('"') and len(value) >= 2:
                kind, default = 'string', value[1:-1]
            elif re.match(r'^-?\d+$', value):
                kind, default = 'int', int(value)
            elif value.lower() in ('true', 'false'):
                kind, default = 'bool', value.lower() == 'true'
            elif value.upper() == value and re.match(r'^[A-Z][A-Z0-9_-]*$', value):
                # A metavar does not by itself prove the value type.
                kind, default = 'opaque', ''
            elif value == 'string':
                kind, default = 'string', ''
            elif value in ('int', 'int32', 'int64'):
                kind, default = 'int', 0
            elif value in ('strings', 'stringArray', 'stringSlice'):
                kind, default = 'list', []
            else:
                kind, default = 'opaque', ''
        default_match = re.search(r'\(default ([^)]*)\)', description)
        if default_match and kind in ('string', 'int', 'list'):
            default = default_match.group(1).strip().strip('"')
            if kind == 'int':
                try:
                    default = int(default)
                except ValueError:
                    pass
        if kind == 'bool' and long_name in ('progress', 'verify'):
            default = True
        if kind == 'bool' and isinstance(default, str):
            default = default.lower() == 'true'
        flags[long_name] = {'short': short or '', 'kind': kind, 'default': default}
    return flags


def run(binary, args):
    return subprocess.run([binary] + args, capture_output=True, text=True, timeout=120, env={**os.environ, 'HOME': '/tmp/nohome'})


def error_result(binary, args, category=False, local_flag=None):
    process = run(binary, args)
    message = strip_error_prefix(clean(process.stderr)).strip()
    message = re.sub(r'^unknown flag:\s*', 'unknown flag ', message)
    message = contract_error(message)
    if local_flag is not None:
        # The contract fixes rejection, stderr and no handler output, not the
        # framework's wording for a flag used outside its command's scope.
        # Require the flag and an explicit scope rejection; an installer error
        # (or a successful invocation) must not be normalized into a rejection.
        lowered = message.lower()
        if (process.returncode != 0 and clean(process.stdout) == ''
                and local_flag in message
                and ('unknown flag' in lowered
                     or ('cannot be used' in lowered and 'app' in lowered))):
            message = 'unknown flag ' + local_flag
    if category and process.returncode != 0:
        lowered = message.lower()
        if 'unknown flag' in lowered:
            message = 'unknown_flag'
        elif 'flag needs an argument' in lowered:
            message = 'parse_error'
        elif 'unknown command' in lowered or 'unexpected argument' in lowered:
            message = 'unknown_command'
        elif (
            'expected' in lowered
            or 'invalid argument' in lowered
            or 'invalid value' in lowered
            # Frameworks render the same parse failure differently: Cobra wraps
            # Go's error ("invalid argument ... : strconv.ParseInt: ..."), Kong
            # surfaces it directly ("--parallel: strconv.ParseInt: ...").  The
            # category is the behavior; the wording is not.
            or 'strconv.parse' in lowered
            or 'invalid syntax' in lowered
        ):
            message = 'parse_error'
        else:
            message = message.split('\n')[0]
    return {'rc': process.returncode, 'err': message, 'stdout_empty': clean(process.stdout) == ''}


def contract_error(message):
    """Extract known errors while keeping exit and stream checks independent."""
    unknown_flag = re.search(r'unknown flag:?\s+(--[A-Za-z0-9_-]+)', message)
    if unknown_flag:
        return 'unknown flag ' + unknown_flag.group(1)
    for line in message.splitlines():
        flags = set(re.findall(r'--(?:major|minor|patch)\b', line))
        if len(flags) >= 2 and ('mutually exclusive' in line or "can't be used together" in line):
            return '--major, --minor, and --patch are mutually exclusive'
        for marker in ('no such app', 'please provide an image name'):
            start = line.find(marker)
            if start >= 0:
                return line[start:]
    return message


def help_result(binary, args):
    process = run(binary, args)
    text = clean(process.stdout) + '\n' + clean(process.stderr)
    flags = parse_flags(text)
    if args == ['install', '--help'] and 'print-table' not in flags:
        # A local flag may be represented on Kong's default child rather than
        # printed on the parent help page. Prove its boolean/default behavior
        # directly instead of inferring that an omitted help line removed it.
        default = run(binary, ['install'])
        disabled = run(binary, ['install', '--print-table=false'])
        enabled = run(binary, ['install', '--print-table'])
        if (default.returncode == disabled.returncode == enabled.returncode == 0
                and clean(default.stdout) == clean(disabled.stdout)
                and '|' in enabled.stdout
                and re.search(r'There are \d+ apps', enabled.stdout)):
            flags['print-table'] = {'short': '', 'kind': 'bool', 'default': False}
    return {'rc': process.returncode, 'flags': flags}


def token_result(binary, args, tokens, merge=True):
    process = run(binary, args)
    text = clean(process.stdout) + ('\n' + clean(process.stderr) if merge else '')
    return {'rc': process.returncode, 'tokens': {token: token in text for token in tokens}}


def hash_result(binary, args):
    process = run(binary, args)
    return {'rc': process.returncode, 'sha': hashlib.sha256(clean(process.stdout).encode()).hexdigest(), 'err': clean(process.stderr)}


def json_result(binary, args):
    process = run(binary, args)
    try:
        payload = json.loads(process.stdout)
    except Exception:
        payload = None
    return {'rc': process.returncode, 'json': payload, 'err': clean(process.stderr)}


def table_rows(text):
    rows = []
    for line in text.splitlines():
        line = ANSI_RE.sub('', line).strip()
        if line.startswith('|') and line.endswith('|'):
            cells = [cell.strip() for cell in line.strip('|').split('|')]
            if cells and cells[0] and cells[0].upper() not in ('TOOL', 'RANK', 'NAME') and not set(cells[0]) <= set('-: '):
                rows.append(cells[0])
    return rows


def table_result(binary, args):
    process = run(binary, args)
    text = clean(process.stdout) + '\n' + clean(process.stderr)
    return {'rc': process.returncode, 'rows': table_rows(text)}


def search_result(binary, args):
    process = run(binary, args)
    text = clean(process.stdout) + '\n' + clean(process.stderr)
    match = re.search(r'Found (\d+) tool\(s\) matching "([^"]*)"', text)
    return {
        'rc': process.returncode,
        'count': match.group(1) if match else None,
        'query': match.group(2) if match else None,
        'rows': table_rows(text),
        'list_lines': [line for line in clean(process.stdout).splitlines() if re.match(r'^\d+\. ', line)],
    }


def completion_result(binary, args, markers):
    process = run(binary, args)
    text = clean(process.stdout)
    return {'rc': process.returncode, 'markers': {marker: marker in text for marker in markers}, 'err': strip_error_prefix(clean(process.stderr))}


def release_result(binary, args):
    process = run(binary, args)
    text = clean(process.stdout)
    fields = {}
    for key in ('Tag', 'Title', 'Bump', 'Notes', 'Prerelease', 'Latest'):
        match = re.search(r'^\s*' + key + r':\s*(.*)$', text, re.M)
        if match:
            fields[key] = match.group(1).strip()
    return {'rc': process.returncode, 'fields': fields}


def build_results(binary):
    results = {}
    results['DIFF-001'] = [help_result(binary, args) for args in [
        ['chart', 'verify', '--help'], ['docker', 'gen', '--help'], ['fstail', '--help'], ['gha', 'bump', '--help'],
        ['oci', 'publish', '--help'], ['oci', 'login', '--help'], ['system', 'install', 'go', '--help'],
    ]]
    results['DIFF-002'] = [
        error_result(binary, ['get', '--parallel', '0', 'helm']),
        error_result(binary, ['get', 'helm', '--arch', 'notanarch']),
        error_result(binary, ['get', 'no-such-tool']),
        error_result(binary, ['install', 'kubectl']),
        json_result(binary, ['version', '--json']),
    ]
    top_level = {
        'install': 'install', 'i': 'install', 'version': 'version', 'v': 'version', 'info': 'info', 'f': 'info',
        'update': 'update', 'u': 'update', 'get': 'get', 'g': 'get', 'd': 'get', 'download': 'get',
        'uninstall': 'uninstall', 'delete': 'uninstall', 'completion': 'completion', 'release': 'release',
        'rel': 'release', 'r': 'release', 'chart': 'chart', 'c': 'chart', 'docker': 'docker', 'fstail': 'fstail',
        'ft': 'fstail', 'gha': 'gha', 'system': 'system', 's': 'system', 'oci': 'oci', 'o': 'oci', 'search': 'search',
    }
    results['DIFF-003'] = [token_result(binary, [token, '--help'], [canonical]) for token, canonical in top_level.items()]
    results['DIFF-004'] = [
        table_result(binary, ['d']), table_result(binary, ['docker']),
        help_result(binary, ['d', '--help']), help_result(binary, ['docker', '--help']),
        token_result(binary, ['docker'], []),
    ]
    results['DIFF-005'] = [table_result(binary, ['install', '--print-table'])] + [
        token_result(binary, ['install', app, '--help'], [app, alias]) for app, alias in [
            ('argocd', 'argocd'), ('nginx-ingress', 'ingress-nginx'), ('gatekeeper', 'opa-gatekeeper'),
            ('binfmt', 'qemu-static'), ('sealed-secrets', 'sealed-secrets'),
            ('nfs-client-provisioner', 'nfs-subdir-external-provisioner'),
        ]
    ]
    results['DIFF-006'] = [help_result(binary, args) for args in [
        ['get', '--help'], ['install', '--help'], ['uninstall', '--help'], ['version', '--help'],
        ['search', '--help'], ['release', '--help'], ['completion', '--help'],
        ['chart', 'verify', '--help'], ['chart', 'upgrade', '--help'], ['chart', 'bump', '--help'],
        ['docker', 'upgrade', '--help'], ['docker', 'gen', '--help'], ['fstail', '--help'], ['gha', 'bump', '--help'],
        ['s', 'i', 'go', '--help'], ['s', 'i', 'registry', '--help'], ['s', 'i', 'containerd', '--help'],
        ['oci', 'install', '--help'], ['oci', 'publish', '--help'], ['oci', 'login', '--help'],
    ]]
    results['DIFF-007'] = [
        help_result(binary, ['get', '--help']),
        error_result(binary, ['get', '--parallel', 'nope', 'helm'], True),
        error_result(binary, ['get', 'helm', '--version'], True),
        error_result(binary, ['get', '--format'], True),
    ]
    results['DIFF-008'] = [
        help_result(binary, ['install', '--help']), help_result(binary, ['get', '--help']),
    ] + [
        token_result(binary, ['install', app, '--help'], ['--kubeconfig', '--wait']) for app in ['argocd', 'nginx-ingress']
    ] + [
        error_result(binary, ['get', '--kubeconfig=/tmp/k', 'helm']),
        error_result(binary, ['get', '--wait', 'helm']),
    ]
    results['DIFF-009'] = [
        help_result(binary, ['uninstall', '--help']),
        error_result(binary, ['get', '--kubeconfig=/tmp/k', 'helm']),
        error_result(binary, ['get', '--wait', 'helm']),
    ]
    results['DIFF-010'] = [
        help_result(binary, ['version', '--help']),
        json_result(binary, ['version', '-j']),
        json_result(binary, ['version', '--json']),
    ]
    results['DIFF-011'] = [
        help_result(binary, ['search', '--help']),
        search_result(binary, ['search', 'k8s']),
        search_result(binary, ['search', 'k8s', '--format', 'table']),
        search_result(binary, ['search', 'k8s', '--format', 'list']),
        search_result(binary, ['search', 'k8s', '--format', 'markdown']),
    ]
    results['DIFF-012'] = [
        help_result(binary, ['release', '--help']),
        error_result(binary, ['release', '--major', '--minor', '--dry-run']),
        error_result(binary, ['release', '--major', '--patch', '--dry-run']),
    ]
    results['DIFF-013'] = [help_result(binary, args) for args in [
        ['s', 'i', 'registry', '--help'], ['oci', 'install', '--help'], ['chart', 'upgrade', '--help'],
        ['docker', 'gen', '--help'], ['gha', 'bump', '--help'], ['fstail', '--help'],
    ]]
    results['DIFF-014'] = [
        error_result(binary, ['oci', 'install', '--path=/tmp/x']),
        help_result(binary, ['oci', 'install', '--help']),
        help_result(binary, ['oci', 'install', '--path=/tmp/x', '--help']),
    ]
    results['DIFF-015'] = [
        token_result(binary, [], ['install', 'get', 'completion', 'Version:', '_ __']),
        {'rc': run(binary, []).returncode},
    ]
    results['DIFF-016'] = [
        hash_result(binary, ['get']), hash_result(binary, ['get', '--format', 'table']),
        hash_result(binary, ['get', '--format', 'markdown']), hash_result(binary, ['get', '--format', 'list']),
    ]
    results['DIFF-017'] = [
        hash_result(binary, ['install']), error_result(binary, ['install', 'kubectl']),
        error_result(binary, ['install', 'does-not-exist']),
        token_result(binary, ['install', 'argocd', '--help'], ['argocd']),
    ]
    results['DIFF-018'] = [
        hash_result(binary, ['install', '--print-table']),
        token_result(binary, ['install', '--print-table'], ['|', 'There are', 'apps']),
    ]
    results['DIFF-019'] = [
        hash_result(binary, ['info']), hash_result(binary, ['info', 'argocd']),
        error_result(binary, ['info', 'nope']), error_result(binary, ['info', 'a', 'b']),
    ]
    results['DIFF-020'] = [hash_result(binary, ['version']), json_result(binary, ['version', '--json'])]
    results['DIFF-021'] = [
        search_result(binary, ['search']), search_result(binary, ['search', 'k8s']),
        search_result(binary, ['search', 'openfaas']), search_result(binary, ['search', 'kubernetes', 'cluster']),
        search_result(binary, ['search', 'zzzznotatool']), search_result(binary, ['search', 'k8s', '--format', 'list']),
        search_result(binary, ['search', 'k8s', '--format', 'markdown']),
    ]
    core = ['arkade', 'completion', 'get', 'version']
    results['DIFF-022'] = [
        completion_result(binary, ['completion'], ['Use "arkade completion SHELL"']),
        completion_result(binary, ['completion', 'bash'], ['# bash completion for arkade'] + core),
        completion_result(binary, ['completion', 'zsh'], ['#compdef arkade', 'arkade', 'completion']),
        completion_result(binary, ['completion', 'fish'], ['complete'] + core),
        completion_result(binary, ['completion', 'powershell'], ['arkade']),
        completion_result(binary, ['completion', 'tcsh'], ['not supported']),
    ]
    results['DIFF-023'] = [
        help_result(binary, ['release', '--help']),
        release_result(binary, ['release', 'v1.2.3', 't', '--dry-run']),
        release_result(binary, ['release', '1.2.3', 't', '--dry-run']),
        release_result(binary, ['release', 'v1.2.3', 't', '--prerelease=false', '--dry-run']),
        release_result(binary, ['release', 'v1.2.3', 't', '--latest', '--dry-run']),
        error_result(binary, ['release', '--major', '--minor', '--dry-run']),
    ]
    results['DIFF-024'] = [
        error_result(binary, ['not-a-command'], True), error_result(binary, ['get', '--nope'], True),
        error_result(binary, ['get', '--parallel', '0', 'helm']),
        {'rc': run(binary, ['version']).returncode}, {'rc': run(binary, ['get', '--format', 'list']).returncode},
    ]

    def routing(args):
        process = run(binary, args)
        return {'rc': process.returncode, 'stdout': clean(process.stdout) != '', 'stderr': clean(process.stderr) != ''}

    results['DIFF-025'] = [routing(['not-a-command']), routing(['get', '--nope']), routing(['version', '--json']), routing(['get', '--format', 'list'])]
    nested = [
        ['install', 'argocd', '--help'], ['install', 'nginx-ingress', '--help'], ['chart', 'verify', '--help'],
        ['chart', 'upgrade', '--help'], ['chart', 'bump', '--help'], ['docker', 'upgrade', '--help'],
        ['docker', 'gen', '--help'], ['gha', 'bump', '--help'], ['s', 'i', 'go', '--help'],
        ['s', 'i', 'registry', '--help'], ['s', 'i', 'containerd', '--help'], ['oci', 'install', '--help'],
        ['oci', 'publish', '--help'], ['oci', 'login', '--help'], ['fstail', '--help'],
    ]
    results['DIFF-026'] = [token_result(binary, args, [args[-2]]) for args in nested]

    def help_available(args):
        process = run(binary, args)
        return {'rc': process.returncode, 'nonempty': (clean(process.stdout) + clean(process.stderr)).strip() != ''}

    results['DIFF-027'] = [help_available(['--help'])] + [help_available(args) for args in nested]
    results['DIFF-028'] = [
        error_result(binary, ['get', 'helm@v1.2.3', '--version', 'v1.2.3']),
        error_result(binary, ['get', 'helm', '--version', 'v1.2.3', '--arch', 'notanarch']),
        help_result(binary, ['get', '--help']),
        error_result(binary, ['get', '--parallel', '0', 'helm']),
    ]
    results['DIFF-029'] = [
        error_result(binary, ['get', '--parallel', '0', 'helm']),
        error_result(binary, ['get', 'helm', '--arch', 'notanarch']),
        error_result(binary, ['get', 'helm', '--os', 'notanos']),
        error_result(binary, ['get', 'no-such-tool']),
    ]
    results['DIFF-030'] = [
        error_result(binary, ['get', 'helm', '--arch', 'notanarch']),
        error_result(binary, ['get', 'no-such-tool']),
        error_result(binary, ['get', 'helm@v1.2.3', '--version', 'v1.2.3']),
        help_result(binary, ['get', '--help']),
    ]
    results['DIFF-031'] = [
        token_result(binary, ['install', 'argocd', '--help'], ['--kubeconfig', '--wait']),
        token_result(binary, ['install', 'nginx-ingress', '--help'], ['--kubeconfig', '--wait']),
        token_result(binary, ['install', 'argocd', '--kubeconfig=/tmp/k', '--wait', '--help'], ['--kubeconfig', '--wait']),
        error_result(binary, ['install', 'argocd', '--print-table'], local_flag='--print-table'),
        error_result(binary, ['get', '--kubeconfig=/tmp/k', 'helm']),
        error_result(binary, ['get', '--wait', 'helm']),
    ]
    results['DIFF-032'] = [
        error_result(binary, ['oci', 'install', '--path=/tmp/x']),
        error_result(binary, ['oci', 'install', '--path', '/tmp/x']),
        help_result(binary, ['oci', 'install', '--help']), help_result(binary, ['oci', 'i', '--help']),
        help_result(binary, ['oci', 'extract', '--help']),
        token_result(binary, ['oci', 'install', '--help'], ['--path']),
    ]
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--binary', required=True)
    parser.add_argument('--expected')
    parser.add_argument('--emit')
    args = parser.parse_args()
    results = build_results(args.binary)
    if args.emit:
        with open(args.emit, 'w', encoding='utf-8') as handle:
            json.dump(results, handle, indent=2, sort_keys=True)
            handle.write('\n')
        return 0
    expected = results
    if args.expected:
        with open(args.expected, encoding='utf-8') as handle:
            expected = json.load(handle)
    failures = []
    for observation_id in sorted(expected):
        if json.dumps(results.get(observation_id), sort_keys=True) != json.dumps(expected[observation_id], sort_keys=True):
            failures.append(observation_id)
    report = {'batch_id': 'BATCH-DIFFERENTIAL-0001', 'passed': not failures, 'failures': failures, 'observations': sorted(expected)}
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if not failures else 1


if __name__ == '__main__':
    raise SystemExit(main())
