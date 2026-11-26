"""Small deterministic regressions for detector and final report wiring.

Synthetic harness observations isolate security policy from framework behavior;
production evidence, hardgate, report and grader artifact writers run unchanged.
No submitted candidate code is executed by these checks.
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path


def run_security_selfcheck() -> None:
    from .harbor_entry import run_harbor_verifier
    from .residue import _javascript_analysis
    from .runner_integrity import dependency_inventory, source_inventory
    from grader import _write_reward_artifacts

    def check(condition: bool, message: str) -> None:
        if not condition:
            raise RuntimeError(message)

    bundle_source = Path(__file__).resolve().parents[1]
    original = json.loads((bundle_source / 'config.json').read_text())
    cases = [
        ('normal', 'export const ok = true;', 'L0', 1.0),
        ('static-process', 'import cp from "node:child_process";', 'L2', -1.0),
        ('builtin-process', 'const cp = process?.getBuiltinModule?.("child_process"); cp.spawnSync("node", []);', 'L2', -1.0),
        ('builtin-alias', 'const p = globalThis.process; const load = p["getBuiltinModule"]; const cp = load("child_" + "process");', 'L2', -1.0),
        ('builtin-destructured', 'const { getBuiltinModule } = (globalThis as any).process; const cp = getBuiltinModule("node:child_process"); cp.spawnSync("node", []);', 'L2', -1.0),
        ('builtin-destructured-alias', 'const p = globalThis.process; const key = "getBuiltin" + "Module"; const { [key]: load } = p; load("child_process");', 'L2', -1.0),
        ('comma-source', 'function legacyRuntimeBridge() { return (0, (globalThis as any).require)("hono"); }', 'L2', -1.0),
        ('comma-alias', 'const load = (0, require); load((0, "hono/adapter"));', 'L2', -1.0),
        ('create-require', 'const { createRequire: make } = require("node:module"); const load = make(import.meta.url); load("hono");', 'L2', -1.0),
        ('object-loader', 'const o = { load: process.getBuiltinModule }; o.load("node:child_process");', 'L2', -1.0),
        ('object-nested', 'const key = "load"; const o = { nested: { [key]: require } }; const { nested: n } = o; n[key]("hono");', 'L2', -1.0),
        ('wrapper-method', 'const o = { load(fn, spec) { return fn(spec); } }; o.load(process.getBuiltinModule, "child_process");', 'L2', -1.0),
        ('module-constructor', 'const load = globalThis.module?.constructor?._load; load?.("hono");', 'L2', -1.0),
        ('reflect-loader', 'const apply = Reflect["apply"]; apply(globalThis.require, null, ["hono"]);', 'L2', -1.0),
        ('function-return', 'const load = new Function("return globalThis.require")(); load("hono");', 'L2', -1.0),
        ('function-parameter-return', 'new Function("read", "return read")(require)("hono");', 'L2', -1.0),
        ('normal-indirection', 'const o = { load: require }; o.load("elysia"); Reflect.apply(customObject.load, null, ["hono"]); new Function("return 42")(); const m = { load(s) { return customObject.load(s); } }; m.load("hono"); const text = (fn, s) => fn(s); text("require", "hono");', 'L0', 1.0),
        ('indirect-source', 'const spec = String(process.env.SPEC ?? "hono"); void import(spec);', 'L2', -1.0),
        ('computed-source', 'void import(["o","n","o","h"].reverse().join(""));', 'L2', -1.0),
        ('ordinary-comment', '// process.getBuiltinModule("child_process"); import("hono")\nexport const note = "child_process";', 'L0', 1.0),
        ('normal-loaders', 'const load = (0, globalThis.require); load("elysia"); const { getBuiltinModule: read } = customObject; read("child_process"); const { createRequire } = customObject; createRequire("hono");', 'L0', 1.0),
        ('protected', 'export const ok = true;', 'L1', -1.0),
        ('bad-target-range', 'export const ok = true;', 'L0', 0.0),
        ('unused-source', 'export const ok = true;', 'L0', 0.0),
        ('source-alias', 'export const ok = true;', 'L0', 0.0),
        ('unused-optional-source', 'export const ok = true;', 'L0', 1.0),
    ]
    with tempfile.TemporaryDirectory(prefix='verifier-security-') as raw:
        root = Path(raw)
        candidate = root / 'candidate'
        package = candidate / 'packages/hono-party'
        (package / 'src').mkdir(parents=True)
        bundle = root / 'bundle'
        assets = bundle / 'assets'
        assets.mkdir(parents=True)
        rows = [{'id': 'SELF-P2P', 'kind': 'P2P', 'status': 'PASS', 'value': {'type': 'json', 'value': True}}, {'id': 'SELF-F2P', 'kind': 'F2P', 'status': 'PASS', 'value': {'type': 'json', 'value': True}}]
        config = dict(original)
        config.pop('runtime', None)
        config['tests'] = {'p2p': [{'id': 'SELF-P2P'}], 'f2p': [{'id': 'SELF-F2P'}], 'differential': [], 'state': []}
        config['migration_unit_checks'] = {'SELF': {'observation_ids': ['SELF-F2P']}}
        config['group_applicability'] = {'P2P': {'status': 'required'}, 'F2P': {'status': 'required'}, 'DIFFERENTIAL': {'status': 'na', 'reason': 'synthetic security fixture'}, 'STATE': {'status': 'na', 'reason': 'no state'}}
        config['policy'] = dict(original['policy'], allowed_change_paths=['*'], protected_paths=[])
        config['protected_paths'] = ['config.json']
        config['assets'] = {'migration_units': 'units.json', 'source_test_baseline': 'baseline.json'}
        # A trusted subprocess emits a fixed observation fixture; it never loads
        # the attack source. Security scanning must reject it independently.
        harness = 'import json,sys;from pathlib import Path;Path(sys.argv[1]).write_text(json.dumps(' + repr({'schema_version': 'migration-observations:v6', 'observations': rows}) + '))'
        config['target_environment'] = {'runtime': {'harness_command': [sys.executable, '-c', harness.replace('{', '{{').replace('}', '}}'), '{report}'], 'timeout_seconds': 30}}
        (assets / 'source-baseline-outputs.json').write_text(json.dumps({'observations': rows}))
        (assets / 'units.json').write_text(json.dumps({'units': [{'id': 'SELF'}]}))
        (assets / 'baseline.json').write_text(json.dumps({'files': {}, 'marker_counts': {}}))
        (bundle / 'config.json').write_text(json.dumps(config))
        for name, text, level, reward in cases:
            manifest = {'name': 'hono-party', 'peerDependencies': {'elysia': '^1.3.0'}, 'devDependencies': {'elysia': '1.3.0'}}
            if name == 'bad-target-range':
                manifest['devDependencies']['elysia'] = '^0.0.1'
            if name == 'unused-source':
                manifest['devDependencies']['hono'] = '^4.12.15'
            if name == 'source-alias':
                manifest['peerDependencies']['legacy'] = 'npm:hono@^4.12.15'
            if name == 'unused-optional-source':
                manifest['optionalDependencies'] = {'hono': '^4.12.15'}
            (package / 'package.json').write_text(json.dumps(manifest))
            (package / 'src/index.ts').write_text(text)
            protected = candidate / 'config.json'
            if name == 'protected':
                protected.write_text('{}')
            else:
                protected.unlink(missing_ok=True)
            logs = root / name
            result = run_harbor_verifier(candidate_root=candidate, bundle_root=bundle, logs_root=logs)
            details = result.get('hardgate_detail', {})
            actual_level = 'L2' if details.get('no_deceptive_source_fallback', {}).get('status') == 'FAIL' else 'L1' if result.get('cheating') else 'L0'
            check(actual_level == level, f'{name}: expected {level}, got {actual_level}: {result}')
            check(result['hardgate'] == ('PASS' if reward == 1 else 'FAIL'), f'{name}: hardgate {result}')
            check(result['reward']['value'] == reward, f'{name}: reward {result["reward"]}')
            report = json.loads((logs / 'verifier-result.json').read_text())
            _write_reward_artifacts(logs, report)
            artifacts = json.loads((logs / 'reward-details.json').read_text())
            check(artifacts['cheating_detected'] == (level != 'L0'), f'{name}: cheating output')
            check(artifacts['reward'] == reward, f'{name}: reward output')
        modules = candidate / 'node_modules'
        (modules / '.bin').mkdir(parents=True)
        runner = modules / '.bin/vitest'
        runner.write_text('original')
        before = dependency_inventory(candidate)
        (modules / '.cache').mkdir()
        (modules / '.cache/file').write_text('cache')
        check(dependency_inventory(candidate) == before, 'legitimate dependency cache')
        runner.write_text('forged')
        check(dependency_inventory(candidate) != before, 'test runner replacement')
        check(_javascript_analysis('test.skip("x", () => {});')['disabled'] == 1, 'skip detector')
        check(_javascript_analysis('test("x", () => {});')['disabled'] == 0, 'normal test detector')
        before = source_inventory(candidate)
        (package / 'dist').mkdir()
        (package / 'dist/index.js').write_text('generated')
        check(source_inventory(candidate) == before, 'legitimate build output')
        (package / 'src/index.ts').write_text('replaced')
        check(source_inventory(candidate) != before, 'build source replacement')


if __name__ == '__main__':
    run_security_selfcheck()
    print('verifier security self-check passed')
