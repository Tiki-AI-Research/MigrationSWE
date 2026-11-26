#!/usr/bin/env python3
"""Measure preparation evidence in disposable, offline Docker containers.

Run from the case directory. Writes only tests/assets and environment/repo on
the host. No images are built and no task implementation is modified.
"""

import hashlib
import io
import json
import subprocess
import tarfile
from datetime import datetime, timezone
from pathlib import Path

CASE = Path(__file__).resolve().parents[2]
ASSETS = CASE / "tests/assets"
SOURCE_IMAGE = "sha256:ec3c94bfa1038f9ab5f1b270e43431346474864cc7c5c24e0aacfdadabe11374"
TARGET_IMAGE = "sha256:d14e42a7ddb697ede428873b00f6dfdfc11d5c60d6d66cef8d1a26674dc6bc24"
BASE = "7a963d16ae5c17b430b57b307d3f3ba74c0cbb1f"


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def run_docker(image, program, mounts=False):
    command = ["docker", "run", "--rm", "--network", "none", "--entrypoint", "python3"]
    if mounts:
        command += ["-v", f"{CASE / 'tests'}:/preparation/tests:ro",
                    "-v", f"{CASE / 'solution'}:/preparation/solution:ro"]
    command += [image, "-c", program]
    result = subprocess.run(command, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(f"docker exited {result.returncode}: {result.stderr.decode()}")
    return result.stdout


def snapshot():
    # Archive tracked working-tree bytes, after checking each against the Git
    # object. git archive is intentionally avoided: export-ignore/subst can alter
    # the authoritative tree. No Git history, dependencies or hidden tests enter
    # this archive. Stable archive headers make regeneration deterministic.
    program = r'''
import gzip, io, os, pathlib, stat, subprocess, sys, tarfile
repo = pathlib.Path('/workspace/repo')
def git(*args):
    return subprocess.check_output(['git', '-C', str(repo), *args])
assert git('rev-parse', 'HEAD').decode().strip() == '7a963d16ae5c17b430b57b307d3f3ba74c0cbb1f'
assert not git('status', '--porcelain')
buf = io.BytesIO()
with tarfile.open(fileobj=buf, mode='w', format=tarfile.PAX_FORMAT) as archive:
    for record in git('ls-tree', '-rz', 'HEAD').split(b'\0'):
        if not record:
            continue
        header, raw_name = record.split(b'\t', 1)
        mode, kind, oid = header.split()
        assert kind == b'blob', record
        name = raw_name.decode()
        assert not any(p in {'.git', 'node_modules'} for p in pathlib.PurePosixPath(name).parts)
        path = repo / name
        data = os.readlink(path).encode() if path.is_symlink() else path.read_bytes()
        assert data == git('cat-file', 'blob', oid.decode()), name
        item = tarfile.TarInfo(name)
        item.uid = item.gid = item.mtime = 0
        item.uname = item.gname = ''
        item.mode = int(mode, 8) & 0o777
        if mode == b'120000':
            item.type = tarfile.SYMTYPE
            item.linkname = data.decode()
            archive.addfile(item)
        else:
            item.size = len(data)
            archive.addfile(item, io.BytesIO(data))
with gzip.GzipFile(fileobj=sys.stdout.buffer, mode='wb', filename='', mtime=0) as compressed:
    compressed.write(buf.getvalue())
'''
    data = run_docker(SOURCE_IMAGE, program)
    destination = CASE / "environment/repo"
    if destination.exists():
        raise RuntimeError("environment/repo already exists; refusing to overwrite source")
    destination.mkdir(parents=True)
    (ASSETS / "base-repo.tar.gz").write_bytes(data)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        archive.extractall(destination, filter="data")
        files = []
        for member in archive.getmembers():
            path = destination / member.name
            content = path.read_bytes()
            expected = archive.extractfile(member).read()
            assert content == expected, member.name
            assert path.stat().st_mode & 0o777 == member.mode, member.name
            files.append({"path": member.name, "mode": f"{member.mode:04o}",
                          "size": len(content), "sha256": hashlib.sha256(content).hexdigest()})
    assert len(list(destination.rglob("*"))) >= len(files)
    write_json(ASSETS / "base-source-files.json", {"schema_version": "jssip-source-files/v1", "files": files})
    return hashlib.sha256(data).hexdigest(), len(files)


MEASURE = r'''
import hashlib, json, pathlib, re, subprocess
from datetime import datetime, timezone
repo = pathlib.Path('/workspace/repo')
records = []
def run(label, command):
    result = subprocess.run(command, cwd=repo, capture_output=True, text=True, check=False)
    records.append({'label': label, 'argv': command, 'cwd': '/workspace/repo',
                    'exit_code': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr})
    return result
def ok(label, command):
    result = run(label, command)
    if result.returncode:
        raise RuntimeError(label + ': ' + result.stderr)
    return result
started = datetime.now(timezone.utc).isoformat()
head = ok('source_identity', ['git', 'rev-parse', 'HEAD', 'HEAD^{tree}']).stdout.splitlines()
assert head[0] == '7a963d16ae5c17b430b57b307d3f3ba74c0cbb1f'
assert not ok('clean_source', ['git', 'status', '--porcelain']).stdout
node = ok('node_version', ['node', '--version']).stdout.strip()
npm = ok('npm_version', ['npm', '--version']).stdout.strip()
ok('existing_tests', ['npm', 'run', 'test'])
source = (repo / 'src/Grammar.js').read_text()
rules = re.findall(r'^\s*"([A-Za-z_][A-Za-z_0-9]*)": parse_[A-Za-z_0-9]+,?$', source, re.M)
assert len(rules) == len(set(rules)) == 226
pkg = json.loads((repo / 'package.json').read_text())
lock = json.loads((repo / 'package-lock.json').read_text())
probe = r"""
const g=require('./src/Grammar.js'), j=require('./src/JsSIP.js');
const out={exports:Object.keys(g),entrypointGrammar:j.Grammar===g};
for(const [label,arg] of [['string','SIP_URI'],['options',{startRule:'SIP_URI'}],['null',null],['bogus','bogus'],['optionsBogus',{startRule:'bogus'}],['inherited','constructor']]) {
  try {out[label]={value:g.parse('sip:a@b',arg)}}
  catch(e){out[label]={error:e.constructor.name,message:e.message}}
}
console.log(JSON.stringify(out));
"""
facts = {'manifest_pegjs': pkg['devDependencies']['pegjs'],
         'lock_root_pegjs': lock['packages']['']['devDependencies']['pegjs'],
         'lock_pegjs': lock['packages']['node_modules/pegjs'],
         'npm_scripts': pkg['scripts'], 'start_rules': rules,
         'parser_sha256': hashlib.sha256((repo/'src/Grammar.js').read_bytes()).hexdigest(),
         'runtime_probe': json.loads(ok('source_contract_probe', ['node', '-e', probe]).stdout)}
reference = pathlib.Path('/preparation/tests/verifier/reference-grammar.js')
facts['reference_equals_source_parser'] = reference.read_bytes() == (repo/'src/Grammar.js').read_bytes()
ok('solution_apply_check', ['git', 'apply', '--check', '/preparation/solution/solution.patch'])
if TARGET:
    ok('solution_apply', ['git', 'apply', '/preparation/solution/solution.patch'])
    ok('target_offline_install', ['npm', 'ci', '--offline', '--no-audit', '--no-fund'])
    ok('target_grammar_check', ['node', 'npm-scripts.mjs', 'grammar:check'])
    original = (repo/'src/Grammar.js').read_bytes()
    ok('target_rebuild_first', ['node', 'npm-scripts.mjs', 'grammar'])
    first = (repo/'src/Grammar.js').read_bytes()
    ok('target_rebuild_second', ['node', 'npm-scripts.mjs', 'grammar'])
    second = (repo/'src/Grammar.js').read_bytes()
    facts['target_rebuild'] = {'committed_sha256': hashlib.sha256(original).hexdigest(),
                               'first_sha256': hashlib.sha256(first).hexdigest(),
                               'second_sha256': hashlib.sha256(second).hexdigest(),
                               'byte_identical': original == first == second}
    ok('target_existing_tests', ['npm', 'run', 'test'])
    # This negative command is a discrepancy measurement, not a harness failure.
    run('target_instruction_grammar_command', ['npm', 'run', 'grammar'])
ok('test_patch_apply_check', ['git', 'apply', '--check', '/preparation/tests/verifier/test.patch'])
ok('test_patch_apply', ['git', 'apply', '/preparation/tests/verifier/test.patch'])
harness = ['node', 'src/test/migration-verifier/run-checks.cjs', '--repo=/workspace/repo',
           '--reference=/preparation/tests/verifier/reference-grammar.js',
           '--expected=/preparation/tests/verifier/expected.json']
nodes = json.loads(ok('migration_harness', harness).stdout)
dump = json.loads(ok('baseline_behavior_dump', harness + ['--dump=1']).stdout)
expected = json.loads(pathlib.Path('/preparation/tests/verifier/expected.json').read_text())
facts['behavior_dump_equals_imported_expected'] = dump == expected
print(json.dumps({'schema_version':'jssip-preparation-measurement/v1',
                  'evidence_kind':'new_measurement', 'historical_evidence':False,
                  'started_at':started, 'completed_at':datetime.now(timezone.utc).isoformat(),
                  'network_mode':'none', 'base_commit':head[0], 'base_tree_hash':head[1],
                  'runtime':{'node':node,'npm':npm}, 'target_applied':TARGET,
                  'facts':facts, 'commands':records, 'nodes':nodes, 'behavior_values':dump}))
'''


def main():
    ASSETS.mkdir(parents=True, exist_ok=True)
    archive_sha, count = snapshot()
    for label, image, target in [("baseline", SOURCE_IMAGE, False), ("oracle", TARGET_IMAGE, True)]:
        program = "TARGET = " + repr(target) + "\n" + MEASURE
        measurement = json.loads(run_docker(image, program, mounts=True))
        measurement["image_id"] = image
        measurement["base_archive_sha256"] = archive_sha
        measurement["base_archive_file_count"] = count
        write_json(ASSETS / f"{label}-measurement.json", measurement)
        print(label, "measurement written; command exits:",
              [(c["label"], c["exit_code"]) for c in measurement["commands"]])


if __name__ == "__main__":
    main()
