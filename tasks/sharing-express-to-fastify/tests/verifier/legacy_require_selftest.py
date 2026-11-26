#!/usr/bin/env python3
"""Self-test for legacy runtime detection in production code.

Runnable as a script (``python3 legacy_require_selftest.py``) or collected by
pytest.  Locks the rule that a plain ``require('express')`` in ``bin/**`` is
reported exactly like an npm-aliased legacy require, that indirect loader
forms (``require.call`` / ``require.apply`` / ``(0, require)`` /
``Reflect.apply(require, ...)`` / ``module.constructor._load`` / static
template literals) resolve to specifiers, that loader laundering
(``const r = require`` / ``new Function('return require')``) and script-like
subprocess bridges are caught, and that legitimate dependencies and variable
spawn targets are not.
"""
from __future__ import annotations

import sys
import tempfile
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hardgate import _legacy_fallback_evidence  # noqa: E402
from legacy_identity import (  # noqa: E402
    LEGACY_PACKAGES,
    indirect_loader_specifiers,
    legacy_loader_evidence,
    legacy_specifier_hits,
    load_lockfile_identity,
    node14_incompatible_specifiers,
    production_sources,
    unresolved_module_loader_evidence,
)

PLAIN_CASES = (
    r"const fwk = require('exp\u0072ess');",
    r"const fwk = require('exp\x72ess');",
    r"const fwk = require('exp\u{72}ess');",
    "const fwk = require('express');",
    "const u = require('express-fileupload');",
    "const b = require('express-basic-auth');",
    "const e = require('express');",
)
ALIASED_CASE = ("const fwk = require('framework');", {"framework": "express"})
STATIC_CASES = (
    "import express from 'express';",
    "import { Router } from 'express-fileupload';",
    "export { x } from 'express-basic-auth';",
    "const e = await import('express');",
)
LEGAL_CASES = (
    "const MIGRATED_FROM = 'express';",
    r"const helper = require('exp\\u0072ess');",
    "const fastify = require('fastify');",
    "const multipart = require('@fastify/multipart');",
    "import fastify from 'fastify';",
    "const helper = require('./lib/helper.js');",
    "// express was removed during the migration",
)
LOADER_CASES = (
    "const name = 'express'; const legacy = require(name);",
    "const legacy = require('exp' + 'ress');",
    "const name = 'express'; const legacy = module.require(name);",
)
INDIRECT_CASES = (
    "const legacy = module['constructor']['_load']('express');",
    "const legacy = module['constructor']._load('express');",
    r"const legacy = module['constructor']['_load']('exp\u0072ess');",
    "const legacy = require(`express`);",
    "const legacy = require.call(null, 'express');",
    "const legacy = require.apply(null, ['express']);",
    "const legacy = (0, require)('express');",
    "const legacy = (0, require)(`express`);",
    "const legacy = Reflect.apply(require, null, ['express']);",
    "const legacy = module.constructor._load('express');",
    "const legacy = module.constructor['_load']('express');",
    "const legacy = module.require('express');",
)
INDIRECT_ALIASED_CASE = ("const fwk = (0, require)('framework');", {"framework": "express"})
INDIRECT_NEGATIVE_CASES = (
    "const helper = require(`./lib/${name}.js`);",
    "const fastify = require('fastify');",
    "const helper = module.constructor._load('./lib/helper.js');",
    "const helper = (0, require)('./lib/helper.js');",
)
LOADER_EVIDENCE_CASES = (
    ("const r = require; r('express');", "require_function_alias"),
    ("const legacy = new Function('return require')()('express');", "dynamic_function_require"),
    ("const load = module.constructor._load; "
     "const name = ['exp', 'ress'].join(''); load(name)();", "indirect_legacy_module_loader"),
    ("const name = 'express'; module.constructor._load(name)();", "indirect_legacy_module_loader"),
)
BRIDGE_CASES = (
    "require('child_process').execSync('node legacy-server.js', { stdio: 'ignore' });",
    "const cp = require('node:child_process'); cp.spawnSync('node', ['srv.js']);",
    "const { execFileSync: run } = require('child_process'); run('srv.js');",
    "const loadModule = module.constructor._load; "
    "const child = loadModule('child_process'); child.spawn('node', ['legacy-server.js']);",
    "const load = () => module.constructor._load('child_process'); "
    "const run = () => load().spawn(process.execPath, ['legacy-server.js']);",
    "const cp = module.constructor._load(['child', 'process'].join('_')); "
    "const launch = cp[['sp', 'awn'].join('')]; "
    "const run = () => launch(process.execPath, ['legacy-bundle']);",
    "const cp = require('child_process'); "
    "const launch = cp[['sp', 'awn'].join('')]; "
    "const run = () => launch(process.execPath, ['legacy-bundle']);",
    "const { spawn } = require('child_process'); spawn('node', ['legacy-server.js']);",
    "const { execFile: launch } = require('node:child_process'); launch('srv.js');",
    "const { fork } = require('child_process'); fork('srv.cjs');",
    "const cp = require('child_process'); cp.spawn('node', ['srv.js']);",
    "const cp = require('child_process'); cp.execFile('srv.js', []);",
    "const cp = require('child_process'); cp.exec('node srv.js');",
    "const cp = require('child_process'); cp.fork('srv.cjs');",
    "const cp = require('child_process'); cp.exec('legacy-server');",
    "const cp = require(\"node:child_process\"); const runner = process.env.HARBOR_RUNNER; cp.execFile(runner, argv);",
)
BRIDGE_NEGATIVE_CASES = (
    "const note = \"require('child_process').execFile('srv.js')\";",
    "const cp = require('child_process'); const note = \"cp.execFile('srv.js')\";",
    "const cp = require('child_process'); cp.execSync('git --version'); const note = 'legacy server migration';",
    "const cp = require('child_process'); cp.execSync('git --version');",
    "const loadModule = module.constructor._load; "
    "const fastify = loadModule('fastify'); fastify.spawn('node', ['srv.js']);",
    "const source = 'child_process'; const method = ['sp', 'awn'].join(''); "
    "const oldScript = 'legacy-server.js'; void source; void method; void oldScript;",
    "const { spawn } = require('child_process'); spawn('xdg-open', ['http://localhost']);",
    "function spawn() {} spawn('node');",
    "const cp = require('child_process'); cp.execFile('pbcopy', []);",
    "const cp = require('child_process'); cp.spawn(cmd, args, {stdio: 'ignore'});",
    "const { spawn } = require('child_process'); spawn(cmd, args, {stdio: 'ignore'});",
    "const cp = require('node:child_process'); const cmd = process.env.OPEN_COMMAND; cp.execFile(cmd, args);",
)
NETWORK_BRIDGE_CASES = (
    "const http = require('http'); http.get({ socketPath: process.env.QR_SOCK, path: '/qr' }, cb);",
    "const net = require('node:net'); net.connect({ path: process.env.QR_SOCK }, cb);",
    "const { get: fetchLocal } = require('node:http'); fetchLocal('http://127.0.0.1:65530/qr', cb);",
    "const http = require('node:http'); const fetchLocal = http.get; fetchLocal('http://127.0.0.1:65530/qr', cb);",
    "const http = require('node:http'); http.get(process.env.LEGACY_SERVER_URL || 'http://127.0.0.1:65530/qr', cb);",
    "const http = require('node:https'); http.request(process.env.SOURCE_SERVER_URL, cb);",
    "const http = require('node:http'); http.get('http://127.0.0.1:65530/qr', cb);",
    "const https = require('node:https'); https.request('https://localhost:8443/qr', cb);",
)
NETWORK_BRIDGE_NEGATIVE_CASES = (
    "const note = \"require('http').get('http://127.0.0.1')\";",
    "const http = require('http'); http.get({ path: '/qr' }, cb);",
    "const http = require('http'); const metadata = { socketPath: '/tmp/unused.sock' }; http.get('https://example.com', cb);",
    "const http = require('http'); const app = fastify(); app.get('/qr', handler); "
    "const status = http.STATUS_CODES[404]; app.listen({ host: '0.0.0.0' });",
)
NEW_LOADER_KINDS = {"require_function_alias", "dynamic_function_require"}


def _expect(cases, aliased, hits_expected, label):
    failures = []
    for case in cases:
        text, alias_map = case if isinstance(case, tuple) else (case, {})
        hits = legacy_specifier_hits(text, alias_map)
        if bool(hits) != hits_expected or any(hit["aliased"] != aliased for hit in hits):
            failures.append((text, hits))
    assert not failures, f"{label}: unexpected result {failures}"
    return len(cases)


def test_plain_literal_legacy_requires_are_reported():
    assert _expect(PLAIN_CASES, False, True, "plain") == len(PLAIN_CASES)
    for text in PLAIN_CASES:
        hit = legacy_specifier_hits(text, {})[0]
        assert hit["resolves_to"] in LEGACY_PACKAGES
        assert hit["aliased"] is False


def test_aliased_legacy_require_is_reported_as_aliased():
    text, alias_map = ALIASED_CASE
    hit = legacy_specifier_hits(text, alias_map)[0]
    assert hit == {"specifier": "framework", "resolves_to": "express", "aliased": True}


def test_static_esm_legacy_specifiers_are_reported():
    assert _expect(STATIC_CASES, False, True, "static") == len(STATIC_CASES)


def test_non_legacy_specifiers_are_not_reported():
    assert _expect(LEGAL_CASES, False, False, "legal") == len(LEGAL_CASES)


def test_legacy_metadata_is_not_a_loader():
    text = "const MIGRATED_FROM = 'express'; const fastify = require('fastify');"
    assert legacy_loader_evidence(text) == []
    assert _legacy_fallback_evidence(text) == []
    assert legacy_loader_evidence("const name = 'express'; require(name);")
    assert legacy_loader_evidence("const name = 'express'; void name;") == []
    assert legacy_loader_evidence(
        "const load = module.constructor._load; const name = 'fastify'; load(name);"
    ) == []


def test_dynamic_loaders_are_left_to_loader_evidence():
    for text in LOADER_CASES:
        assert legacy_specifier_hits(text, {}) == []
        assert legacy_loader_evidence(text), f"loader evidence missed: {text}"


def test_indirect_loader_forms_are_reported():
    for text in INDIRECT_CASES:
        hits = legacy_specifier_hits(text, {})
        assert hits, f"indirect loader missed: {text}"
        assert hits[0]["resolves_to"] in LEGACY_PACKAGES
        assert hits[0]["aliased"] is False
        assert legacy_loader_evidence(text), f"hidden loader not classified as fallback: {text}"


def test_aliased_indirect_legacy_require_is_reported_as_aliased():
    text, alias_map = INDIRECT_ALIASED_CASE
    hit = legacy_specifier_hits(text, alias_map)[0]
    assert hit == {"specifier": "framework", "resolves_to": "express", "aliased": True}


def test_indirect_negative_controls_yield_no_specifier():
    assert _expect(INDIRECT_NEGATIVE_CASES, False, False, "indirect-legal") == len(INDIRECT_NEGATIVE_CASES)
    # A local path with an unknown runtime substitution does not prove that
    # any legacy runtime is loaded.
    template = "const helper = require(`./lib/${name}.js`);"
    assert indirect_loader_specifiers(template) == []
    assert legacy_loader_evidence(template) == []


def test_non_executable_mentions_and_nonlegacy_dynamic_loads_are_ignored():
    cases = (
        "// require(name)",
        "const text = \"require(name)\";",
        "// require('express')",
        "const text = \"require('express')\";",
        "const pattern = /require\\(name\\)/;",
        "const name = 'fastify'; require(name);",
        "const r = require; r('fastify');",
        "const fastify = new Function('return require')()('fastify');",
        "const unrelated = {require() {}}; unrelated.require('express');",
        "const name = 'express'; unrelated.require(name);",
    )
    for text in cases:
        assert legacy_specifier_hits(text, {}) == [], text
        assert legacy_loader_evidence(text) == [], text
        assert _legacy_fallback_evidence(text) == [], text
    assert unresolved_module_loader_evidence("require(name)") == ["require(name)"]
    assert unresolved_module_loader_evidence("// require(name)") == []
    assert unresolved_module_loader_evidence("const name = 'fastify'; require(name)") == []


def test_legacy_subpaths_and_aliased_subpaths_are_reported():
    for specifier in ("express/lib/express", "express/package.json", "express-fileupload/lib/index"):
        for text in (f"require('{specifier}')", f"import x from '{specifier}';"):
            hits = legacy_specifier_hits(text, {})
            assert len(hits) == 1 and hits[0]["resolves_to"] == specifier, text
            assert hits[0]["aliased"] is False
    hit = legacy_specifier_hits("require('framework/lib/express')", {"framework": "express"})
    assert hit == [{
        "specifier": "framework/lib/express",
        "resolves_to": "express/lib/express",
        "aliased": True,
    }]
    scoped = legacy_specifier_hits("require('@custom/framework/lib/express')", {
        "@custom/framework": "express"
    })
    assert scoped[0]["resolves_to"] == "express/lib/express" and scoped[0]["aliased"] is True
    assert legacy_specifier_hits("const text = `loaded ${require('express')}`;", {})


def test_node14_module_check_handles_prefix_and_non_executable_text():
    for text in ("require('stream/promises')", "require('node:stream/promises')",
                 "import('node:stream/promises')", "import x from 'node:stream/promises';"):
        assert len(node14_incompatible_specifiers(text)) == 1, text
    for text in ("// require('node:stream/promises')",
                 "const note = \"require('node:stream/promises')\";"):
        assert node14_incompatible_specifiers(text) == [], text


def test_dev_only_alias_is_not_deception_without_runtime_use():
    with tempfile.TemporaryDirectory() as temporary:
        repo = Path(temporary)
        (repo / "package.json").write_text(json.dumps({
            "devDependencies": {"framework": "npm:express@4.0.0"}
        }))
        (repo / "package-lock.json").write_text(json.dumps({"packages": {
            "": {"devDependencies": {"framework": "npm:express@4.0.0"}},
            "node_modules/framework": {"name": "express", "dev": True},
        }}))
        findings, aliases = load_lockfile_identity(repo)
        assert findings == [], findings
        assert aliases == {"framework": "express"}
        assert legacy_specifier_hits("require('framework')", aliases)[0]["aliased"] is True
        assert legacy_loader_evidence("const name = 'framework'; require(name);", aliases)


def test_loader_laundering_forms_are_caught():
    for text, kind in LOADER_EVIDENCE_CASES:
        kinds = [item["kind"] for item in legacy_loader_evidence(text)]
        assert kind in kinds, f"{kind} missed for: {text}"


def test_negative_controls_do_not_trigger_new_loader_rules():
    for text in INDIRECT_NEGATIVE_CASES + BRIDGE_NEGATIVE_CASES:
        kinds = {item["kind"] for item in legacy_loader_evidence(text)}
        assert not (kinds & NEW_LOADER_KINDS), f"false positive: {text}"


def test_script_like_subprocess_bridges_are_caught():
    for text in BRIDGE_CASES:
        kinds = [item["kind"] for item in _legacy_fallback_evidence(text)]
        assert "subprocess_bridge" in kinds, f"bridge missed: {text}"


def test_node_prefixed_network_bridges_are_caught():
    for text in NETWORK_BRIDGE_CASES:
        kinds = [item["kind"] for item in _legacy_fallback_evidence(text)]
        assert kinds, f"network bridge missed: {text}"
        assert any("network_bridge" in kind for kind in kinds), f"network evidence missing: {text}"


def test_ordinary_http_options_are_not_local_bridges():
    for text in NETWORK_BRIDGE_NEGATIVE_CASES:
        assert _legacy_fallback_evidence(text) == [], f"false network bridge: {text}"


def test_legitimate_subprocess_targets_are_not_bridges():
    for text in BRIDGE_NEGATIVE_CASES:
        assert _legacy_fallback_evidence(text) == [], f"false bridge: {text}"


def test_nonstandard_extensions_and_reachable_helpers_are_scanned():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "bin").mkdir()
        (repo / "helpers").mkdir()
        (repo / "bin/app.js").write_text("require('../helpers/bridge.bak');")
        (repo / "helpers/bridge.bak").write_text("require('./nested');")
        (repo / "helpers/nested.js").write_text("require('../bin/app.js'); require('express');")
        (repo / "bin/residue.bak").write_text("require('express');")
        (repo / "bin/form.html").write_text("<p>require(name)</p>")
        (repo / "helpers/unreachable.js").write_text("require('express');")
        paths = [p.relative_to(repo).as_posix() for p in production_sources(repo)]
        assert set(paths) == {"bin/app.js", "bin/residue.bak", "helpers/bridge.bak", "helpers/nested.js"}, paths
        assert len(paths) == len(set(paths)), paths


def _main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {test.__name__}: {exc}")
    print(f"{len(tests) - failed}/{len(tests)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_main())
