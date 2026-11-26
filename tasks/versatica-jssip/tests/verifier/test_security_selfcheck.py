#!/usr/bin/env python3
"""Pinned regression suite for the JsSIP verifier security detectors.

The suite is deliberately independent of any candidate workspace.  It runs
during the verifier image build (``--full``) and again at scoring startup
(``--quick``) so a later refactor cannot silently reintroduce one of the
supplied bypasses or false positives.

Coverage:
* hidden fallback: split-string ``require``, dynamic loader, VM/base64 loader;
* legitimate controls: build-time ``child_process`` helper, a production
  comment containing ``skip(empty)``, a production comment/string that merely
  names ``child_process``, and a candidate-added test that runs the grammar
  build; a test helper loaded by production code must still be rejected;
* exact target version and published SRI, alias/local dependency and
  manifest/lock consistency;
* literal dynamic build-helper imports versus computed module loaders;
* added-vs-edited test classification and candidate-owned skip markers;
* test-selection configuration: edits to Jest config, ``package.json`` Jest
  options, or ``--passWithNoTests`` style test flags are tampering even when no
  test source changed;
* JsSIP entry-point export preservation;
* real hard-gate and grader wiring: L2 cheating -> reward ``-1``; visible
  migration residue -> hard gate success with a partial verifier-failed T;
  target dependency failure -> hard-gate failure with reward ``0``;
  missing/not_run state -> verifier failure with T<1.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

HERE = Path(__file__).resolve().parent


def _load(name: str, filename: str) -> Any:
    path = HERE / filename
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


hardgate = _load("jssip_hardgate_selfcheck", "hardgate.py")
grader = _load("jssip_grader_selfcheck", "../grader.py")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _json(path: Path, value: Any) -> None:
    _write(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _init_repo(root: Path) -> str:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "selfcheck@example.invalid")
    _git(root, "config", "user.name", "verifier-selfcheck")
    _git(root, "add", "-A")
    commit = _git(root, "commit", "-q", "-m", "selfcheck baseline")
    assert commit.returncode == 0, commit.stderr
    head = _git(root, "rev-parse", "HEAD")
    assert head.returncode == 0, head.stderr
    return head.stdout.strip()


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _assert_status(actual: tuple[str, str], expected: str, label: str) -> None:
    _assert(actual[0] == expected, f"{label}: expected {expected}, got {actual}")


def _package(peggy: str = "4.2.0", **extra: Any) -> dict[str, Any]:
    package = {
        "name": "jssip",
        "version": "3.13.8",
        "scripts": {"grammar": "node npm-scripts.mjs grammar"},
        "dependencies": {},
        "devDependencies": {"peggy": peggy},
    }
    package.update(extra)
    return package


def _lock(package: dict[str, Any], peggy: str = "4.2.0") -> dict[str, Any]:
    dev = dict(package.get("devDependencies") or {})
    if peggy:
        dev["peggy"] = peggy
    else:
        dev.pop("peggy", None)
    return {
        "name": "jssip",
        "version": "3.13.8",
        "lockfileVersion": 3,
        "requires": True,
        "packages": {
            "": {
                "name": "jssip",
                "version": "3.13.8",
                "dependencies": dict(package.get("dependencies") or {}),
                "devDependencies": dev,
            },
            "node_modules/peggy": {
                "version": "4.2.0",
                "resolved": "https://registry.npmjs.org/peggy/-/peggy-4.2.0.tgz",
                "integrity": hardgate._TARGET_LOCK_INTEGRITIES[("peggy", "4.2.0")],
                "dev": True,
            },
        },
    }


def _valid_grammar() -> str:
    """A minimal public-contract-compliant parser used by gate/grader tests."""
    return (
        "'use strict';\n"
        "const declaredStartRules = [];\n"
        "for (let index = 0; index < 225; index += 1) declaredStartRules.push('RULE_' + index);\n"
        "declaredStartRules.push('SIP_URI');\n"
        "function invalid(name) {\n"
        "  throw new Error('Invalid rule name: \"' + name + '\".');\n"
        "}\n"
        "function parse(input, options) {\n"
        "  let startRule = 'SIP_URI';\n"
        "  if (typeof options === 'string') startRule = options;\n"
        "  else if (options !== null && options !== undefined && typeof options === 'object') {\n"
        "    if (options.startRule !== undefined) startRule = String(options.startRule);\n"
        "  }\n"
        "  if (declaredStartRules.indexOf(startRule) === -1) invalid(startRule);\n"
        "  if (input === '') return -1;\n"
        "  if (startRule === 'SIP_URI' && input.indexOf('sip:') === 0) {\n"
        "    return { _scheme: 'sip', _user: 'alice', _host: 'example.com' };\n"
        "  }\n"
        "  return -1;\n"
        "}\n"
        "class SyntaxError extends Error {\n"
        "  constructor(message) { super(message); this.name = 'SyntaxError'; }\n"
        "}\n"
        "module.exports = { StartRules: Object.freeze(declaredStartRules.slice()), SyntaxError, parse };\n"
    )


def _minimal_repo(
    tmp: Path,
    *,
    hidden: bool = False,
    valid_contract: bool = False,
    visible_residue: bool = False,
) -> Path:
    repo = tmp / "repo"
    package = _package()
    if visible_residue:
        package["devDependencies"]["pegjs"] = "^0.7.0"
    _json(repo / "package.json", package)
    _json(repo / "package-lock.json", _lock(package))
    _write(
        repo / "npm-scripts.mjs",
        "import peggy from 'peggy';\nconsole.log(peggy);\n",
    )
    _write(
        repo / "src/Grammar.js",
        _valid_grammar()
        if valid_contract
        else "// Generated by PEG.js 0.7.0\n"
        "module.exports = { parse: function () { return -1; } };\n",
    )
    _write(repo / "src/Grammar.pegjs", "{ var data = {}; }\n\nstart = \"x\"\n")
    for relative in (
        "src/Parser.js",
        "src/NameAddrHeader.js",
        "src/SIPMessage.js",
    ):
        _write(repo / relative, "module.exports = {};\n")
    _write(
        repo / "src/URI.js",
        (
            "const cp = require('child_' + 'process');\n"
            "module.exports = { cp };\n"
            if hidden
            else "module.exports = {};\n"
        ),
    )
    _write(
        repo / "src/JsSIP.js",
        "const Grammar = require('./Grammar');\n"
        "module.exports = { Grammar, URI: require('./URI') };\n",
    )
    return repo


def _config_for(repo: Path, tmp: Path) -> dict[str, Any]:
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    return {
        "schema_version": "deepswe-verifier-config/v2",
        "reward_mode": "VERIFIER_ONLY",
        "task_id": "selfcheck",
        "base_commit": head,
        "f2p": ["f2p-selfcheck"],
        "p2p": ["p2p-selfcheck"],
        "differential": [],
        "hard_gate_order": [
            "candidate_valid",
            "target_build_valid",
            "migration_integrity_preserved",
            "p2p_regression_preserved",
            "test_integrity",
            "evaluation_environment_integrity",
        ],
        "protected_paths": ["tests/**"],
        "persistent_state_involved": False,
        "hard_gate": {"compile_paths": [], "cheat_penalty": -1.0},
        "weights": {"test": 1.0},
        "reports": {
            "reward": str(tmp / "reward.json"),
            "reward_details": str(tmp / "reward-details.json"),
        },
    }


def _ctrf(repo: Path, config: dict[str, Any], path: Path) -> None:
    tests = []
    for group, nodes in (("F2P", config["f2p"]), ("P2P", config["p2p"])):
        for node in nodes:
            tests.append(
                {
                    "name": node,
                    "testId": node,
                    "status": "passed",
                    "duration": 0,
                    "message": "",
                    "extra": {"deepswe": {"group": group, "required": True}},
                }
            )
    _json(
        path,
        {
            "reportFormat": "CTRF",
            "specVersion": "0.0.0",
            "results": {
                "tool": {"name": "selfcheck", "version": "1"},
                "summary": {"tests": len(tests), "passed": len(tests)},
                "tests": tests,
                "environment": {
                    "taskId": config["task_id"],
                    "baseCommit": config["base_commit"],
                },
            },
        },
    )


def _run_gate_and_finalize(
    repo: Path, tmp: Path, verifier: dict[str, Any] | None = None
) -> dict[str, Any]:
    config = _config_for(repo, tmp)
    config_path = tmp / "config.json"
    ctrf_path = tmp / "ctrf.json"
    hard_gate_path = tmp / "hard-gate.json"
    gate_path = tmp / "gate.json"
    verifier_path = tmp / "verifier-result.json"
    if verifier is None:
        verifier = {
            "schema_version": "deepswe-verifier-result/v2",
            "migration_scope_complete": {"status": "failed", "ratio": 0.6},
            "required_state_preservation_complete": {
                "status": "na",
                "ratio": None,
                "reason": "No persistent state is involved.",
            },
        }
    _json(verifier_path, verifier)
    config["diagnostics"] = {"verifier": str(verifier_path)}
    _json(config_path, config)
    _ctrf(repo, config, ctrf_path)
    completed = subprocess.run(
        [
            sys.executable,
            str(HERE / "hardgate.py"),
            "--config", str(config_path),
            "--repo", str(repo),
            "--output", str(hard_gate_path),
            "--ctrf", str(ctrf_path),
            "--no-sandbox",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    _assert(completed.returncode == 0, "hard gate crashed: " + completed.stderr[-1000:])
    completed = subprocess.run(
        [
            sys.executable,
            str(HERE.parent / "grader.py"),
            "gate",
            "--config", str(config_path),
            "--ctrf", str(ctrf_path),
            "--hard-gate", str(hard_gate_path),
            "--output", str(gate_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    _assert(completed.returncode == 0, "grader gate crashed: " + completed.stderr[-1000:])
    completed = subprocess.run(
        [
            sys.executable,
            str(HERE.parent / "grader.py"),
            "finalize",
            "--config", str(config_path),
            "--gate", str(gate_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    _assert(completed.returncode == 0, "grader finalize crashed: " + completed.stderr[-1000:])
    return json.loads((tmp / "reward-details.json").read_text(encoding="utf-8"))


def _false_negative_regressions() -> None:
    with tempfile.TemporaryDirectory(prefix="jssip-selfcheck-false-negatives-") as temporary:
        repo = _minimal_repo(Path(temporary), valid_contract=True)
        package = json.loads((repo / "package.json").read_text())
        package["scripts"] = {}
        _json(repo / "package.json", package)
        _write(repo / "BUILDING.md", "```sh\nnode npm-scripts.mjs grammar\n```\n")
        _write(repo / "npm-scripts.mjs", "import grammar from './grammar.mjs';\ngrammar();\n")
        _write(
            repo / "grammar.mjs",
            "import fs from 'node:fs';\nimport peggy from 'peggy';\n"
            "fs.writeFileSync('src/Grammar.js', peggy.generate('start = \"x\"'));\n",
        )
        _assert(
            hardgate._grammar_build_script(repo) == "node npm-scripts.mjs grammar",
            "the documented Node grammar command must work without an npm shortcut",
        )
        with patch.object(hardgate.shutil, "which", return_value="/usr/bin/npm"), patch.object(
            hardgate, "sandbox_run", return_value=subprocess.CompletedProcess([], 0)
        ) as runner:
            hardgate._run_grammar_script(repo, "node npm-scripts.mjs grammar")
            _assert(
                runner.call_args.args[0] == ["node", "npm-scripts.mjs", "grammar"],
                "the documented command must not be replaced with a nonexistent npm script",
            )
            package = json.loads((repo / "package.json").read_text())
            package["scripts"] = {"grammar": "node npm-scripts.mjs grammar"}
            _json(repo / "package.json", package)
            hardgate._run_grammar_script(repo, package["scripts"]["grammar"])
            _assert(
                runner.call_args.args[0] == ["/usr/bin/npm", "run", "grammar"],
                "a declared npm grammar script must retain npm PATH behavior",
            )
        _assert_status(hardgate._generator_check(repo), "passed", "imported root grammar helper")
        _write(
            repo / "npm-scripts.mjs",
            "async function grammar() {\n"
            "  const { default: peggy } = await import('peggy');\n"
            "  const helper = await import('./grammar.mjs');\n"
            "  return helper.default(peggy);\n"
            "}\nvoid grammar();\n",
        )
        _write(
            repo / "grammar.mjs",
            "import peggy from 'peggy';\nexport default function grammar() { return peggy; }\n",
        )
        _assert_status(
            hardgate._generator_check(repo),
            "passed",
            "literal dynamic imports are statically resolvable",
        )
        _write(
            repo / "npm-scripts.mjs",
            "const helperPath = './grammar.mjs';\n"
            "const helper = await import(helperPath);\n"
            "console.log(helper);\n",
        )
        _assert_status(
            hardgate._generator_check(repo),
            "failed",
            "computed dynamic import remains forbidden",
        )
        _write(repo / "npm-scripts.mjs", "import grammar from './grammar.mjs';\ngrammar();\n")
        _write(
            repo / "grammar.mjs",
            "import fs from 'node:fs';\nimport peggy from 'peggy';\n"
            "export default function grammar() {\n"
            "  fs.writeFileSync('src/Grammar.js', peggy.generate('start = \\\"x\\\"'));\n"
            "}\n",
        )
        harness_lines = []
        for line in (HERE / "test.patch").read_text().splitlines():
            if line.startswith("diff --git ") and harness_lines:
                break
            if line.startswith("+") and not line.startswith("+++"):
                harness_lines.append(line[1:])
        harness = "\n".join(harness_lines)
        helper_start = harness.index("function generatorSources()")
        helper_opening = harness.index("{", helper_start)
        helper_end = hardgate._find_object_end(harness, helper_opening)
        helper = harness[helper_start:helper_end]
        program = (
            "const fs = require('fs'); const path = require('path');"
            "const repo = process.argv[1]; const readFileSync = fs.readFileSync;"
            "const parseJson = JSON.parse;"
            "function readJson(relative) { return parseJson(readFileSync(path.join(repo, relative), 'utf8')); }"
            + helper + "\nprocess.stdout.write(generatorSources());"
        )
        hidden_sources = subprocess.run(
            ["node", "-e", program, str(repo)], capture_output=True, text=True, timeout=30
        )
        _assert(
            hidden_sources.returncode == 0 and "peggy.generate" in hidden_sources.stdout,
            "the protected hidden harness must follow imported grammar helpers",
        )
        hidden_patch = (HERE / "test.patch").read_text()
        _assert(
            "new Grammar.SyntaxError();" not in hidden_patch
            and "new Grammar.SyntaxError('boom');" not in hidden_patch,
            "the hidden type check must not require undocumented constructor arities",
        )
        _assert(
            "const constructed = new Grammar.SyntaxError(" in hidden_patch,
            "the hidden type check must retain the official four-argument constructor check",
        )
        _assert(
            "source: 'input'," in hidden_patch,
            "the hidden type check must provide Peggy's required location source",
        )
        syntax_error_contract = json.loads(
            (HERE.parent / "assets" / "migration-contract.json").read_text()
        )["public_contract"]["declaration"]["syntax_error"]
        _assert(
            syntax_error_contract["constructor_parameters"]
            == ["message", "expected", "found", "location"],
            "the frozen contract must retain Peggy's SyntaxError constructor",
        )
        _assert(
            set(syntax_error_contract["location_members"])
            == {"source", "start", "end"},
            "the frozen contract must retain Peggy's location members",
        )
        _assert(
            "buildMessage" in syntax_error_contract["required_static_members"]
            and "format" in syntax_error_contract["required_instance_members"],
            "the frozen contract must retain Peggy's SyntaxError methods",
        )
        _assert(
            set(syntax_error_contract["expected_variants"])
            == {"literal", "class", "any", "end", "other"},
            "the frozen contract must retain all expectation variants",
        )
        _assert(
            "any" in syntax_error_contract["expected_forbidden_broad_types"],
            "the frozen contract must reject an any-typed expected property",
        )
        _assert(
            syntax_error_contract["build_message_expected_parameter"] == "Expectation[]"
            and syntax_error_contract["build_message_expected_may_be_nullish"] is False,
            "the frozen contract must require buildMessage's expectation array",
        )
        _assert(
            syntax_error_contract["class_parts_members"]
            == ["string", "[string, string]"]
            and syntax_error_contract["class_range_tuple_length"] == 2,
            "the frozen contract must require two-element character ranges",
        )
        instruction_path = HERE.parent.parent / "instruction.md"
        if instruction_path.is_file():
            instruction = instruction_path.read_text()
            for required_syntax_error_contract in (
                "`buildMessage(expected, found)`",
                "`expected` parameter is a non-nullish array of expectations",
                "`format(sources)`",
                "`(message, expected, found, location)`",
                "`source`, `start`, and `end` fields",
                "five discriminated expectation variants",
                "exact two-element `[start, end]` string tuple",
                "neither the property nor its array element type may be `any`",
            ):
                _assert(
                    required_syntax_error_contract in instruction,
                    "the public task must explicitly specify every hidden SyntaxError requirement",
                )
        for required_expectation_check in (
            "type SyntaxErrorExpectation =",
            "readonly type: 'literal'",
            "readonly type: 'class'",
            "readonly parts: readonly (string | readonly [string, string])[]",
            "readonly type: 'any'",
            "readonly type: 'end'",
            "readonly type: 'other'",
            "IsAny<Grammar.SyntaxError['expected']>",
            "IsAny<SyntaxErrorExpectedElement>",
            "IsAny<ClassParts>",
            "IsAny<ClassParts[number]>",
            "parts: ['a', ['a', 'z']]",
            "parts: [['a', 'z', 'extra']]",
            "Grammar.SyntaxError.buildMessage(null, null)",
        ):
            _assert(
                required_expectation_check in hidden_patch,
                "the hidden type check must enforce the explicit expectation union",
            )
        _assert_status(hardgate._hidden_fallback(repo), "passed", "build-time artifact write")
        _write(repo / "src/URI.js", "import './grammar-bridge.mjs';\nmodule.exports = {};\n")
        _write(repo / "src/grammar-bridge.mjs", "import '../grammar.mjs';\n")
        _assert_status(hardgate._hidden_fallback(repo), "failed", "runtime-loaded build helper")
        _write(repo / "src/URI.js", "module.exports = {};\n")
        _write(repo / "src/grammar-bridge.mjs", "export const bridge = true;\n")
        _write(repo / "grammar.mjs", "import legacy from './nested-generator.mjs';\n")
        _write(repo / "nested-generator.mjs", "import pegjs from 'pegjs';\n")
        _assert_status(hardgate._generator_check(repo), "failed", "transitive legacy generator")
        (repo / "nested-generator.mjs").unlink()
        _write(repo / "grammar.mjs", "import peggy from 'peggy';\n")
        _write(
            repo / "src/test/cross-realm.test.js",
            "const fs = require('fs');\nconst vm = require('vm');\n"
            "fs.readFileSync('baseline.json', 'utf8');\nvm.runInNewContext('Object(true)');\n",
        )
        _assert_status(hardgate._residual_scan(repo), "passed", "standalone cross-realm test")
        _write(repo / "src/URI.js", "require('./test/cross-realm.test');\nmodule.exports = {};\n")
        _assert_status(hardgate._residual_scan(repo), "failed", "production-loaded VM test")
        _assert_status(hardgate._hidden_fallback(repo), "failed", "production-loaded VM helper")
        _write(repo / "src/URI.js", "module.exports = {};\n")
        _write(repo / "tools/runtime.mjs", "const fs = require('fs');\nfs.writeFileSync(1, 'fake');\n")
        _write(repo / "src/URI.js", "require('../tools/runtime.mjs');\nmodule.exports = {};\n")
        _assert_status(hardgate._hidden_fallback(repo), "failed", "runtime helper in build directory")
        _write(repo / "src/URI.js", "module.exports = {};\n")
        marker = "qaProbeFalseNegativeControl"
        declaration = f'var {marker} = "{marker}";'
        for name in ("", "publicParser", "grammarCompatibility"):
            wrapped = (
                f"module.exports = (function {name}(parser) {{ return parser; }})("
                f"(function () {{ {declaration} return {{}}; }})());\n"
            )
            _assert(hardgate._marker_in_generated_core(wrapped, marker), "named wrapper marker")
            _assert(
                not hardgate._marker_in_generated_core(
                    wrapped.replace(declaration, "") + declaration + "\n", marker
                ), "a marker appended outside the exported expression must fail",
            )
        _assert(
            not hardgate._marker_in_generated_core(
                f"// {declaration}\nmodule.exports = {{}};\n", marker
            ), "a commented marker must fail",
        )
        _assert(hardgate._grammar_initializer_offset("{{\n}}\nstart='x'") == 1, "global initializer offset")
        _assert(hardgate._grammar_initializer_offset("{\n}\nstart='x'") == 0, "per-parse initializer offset")
        _assert(
            hardgate._grammar_initializer_offset("/*\n{\n*/\n{{\n}}\n") == 9,
            "braces in comments must not receive the source probe",
        )
        generated = (
            "// Generated by Peggy 4.2.0\n"
            "function peg$SyntaxError() {}\n"
            "function peg$parse(input, options) { var peg$startRuleFunctions = {}; var peg$FAILED = {}; return input; }\n"
            "function grammarParse(input, selection) { return peg$parse(input, selection); }\n"
            "module.exports = { StartRules: ['start'], SyntaxError: peg$SyntaxError, parse: peg$parse };\n"
        )
        candidate = generated + (
            "const grammarStartRules = new Set(module.exports.StartRules);\n"
            "module.exports.parse = grammarParse;\n"
        )
        _assert(hardgate._source_generated_export_adapter(candidate, generated), "source-generated export adapter")
        for invalid in (
            generated + "module.exports.parse = function () { return -1; };\n",
            candidate.replace("return peg$parse(input, selection)", "return 'fake'"),
            candidate + "module.exports.toSource = function () {};\n",
        ):
            _assert(not hardgate._source_generated_export_adapter(invalid, generated), "handwritten export mutation")
        wrapped_candidate = generated + (
            "(function () {\n"
            "  var baseParser = module.exports;\n"
            "  var baseError = baseParser.SyntaxError;\n"
            "  var baseRules = baseParser.StartRules;\n"
            "  function parse(input, selection) {\n"
            "    var options = selection || {};\n"
            "    try { return baseParser.parse(input, options); }\n"
            "    catch (error) {\n"
            "      if (error instanceof baseError) { return -1; }\n"
            "      throw error;\n"
            "    }\n"
            "  }\n"
            "  module.exports = { StartRules: baseRules, SyntaxError: baseError, parse: parse };\n"
            "})();\n"
        )
        _assert(
            hardgate._source_generated_export_adapter(wrapped_candidate, generated),
            "source-generated module wrapper",
        )
        for invalid in (
            wrapped_candidate.replace("return baseParser.parse(input, options)", "return -1"),
            wrapped_candidate.replace(
                "var options = selection || {};",
                "if (input === 'hidden') return -1; var options = selection || {};",
            ),
            wrapped_candidate.replace("parse: parse };", "parse: parse, toSource: parse };"),
            wrapped_candidate.replace(
                "var baseParser = module.exports;",
                "var baseParser = module.exports; require('evil');",
            ),
            wrapped_candidate.replace("StartRules: ['start']", "StartRules: ['fake']", 1),
        ):
            _assert(
                not hardgate._source_generated_export_adapter(invalid, generated),
                "untrusted module wrapper",
            )
        reassigned_candidate = generated + (
            "module.exports = (function grammarCompatibility(parser) {\n"
            "  const generatedParse = parser.parse;\n"
            "  parser.parse = function (input, options) {\n"
            "    return generatedParse(input, options);\n"
            "  };\n"
            "  return parser;\n"
            "})(module.exports);\n"
        )
        _assert(
            hardgate._source_generated_export_adapter(reassigned_candidate, generated),
            "serialized parser-object compatibility wrapper",
        )
        _assert(
            not hardgate._source_generated_export_adapter(
                reassigned_candidate.replace(
                    "return generatedParse(input, options);",
                    "if (input === 'hidden') return -1; return generatedParse(input, options);",
                ),
                generated,
            ),
            "serialized wrapper must remain a pure forwarding adapter",
        )
        factory_generated = generated.replace(
            "function grammarParse(input, selection)",
            "function createParse(parser) {\n"
            "  const generatedParse = parser.parse;\n"
            "  return function (input, options) {\n"
            "    return generatedParse(input, options);\n"
            "  };\n"
            "}\n"
            "function grammarParse(input, selection)",
        )
        factory_candidate = factory_generated + (
            "module.exports.parse = createParse(module.exports);\n"
        )
        _assert(
            hardgate._source_generated_export_adapter(
                factory_candidate, factory_generated
            ),
            "grammar-source-generated parse factory",
        )
        direct_candidate = generated + (
            "const generatedParse = module.exports.parse;\n"
            "module.exports.parse = function (input, selection) {\n"
            "  const options = { startRule: selection };\n"
            "  return generatedParse(input, options);\n"
            "};\n"
        )
        _assert(
            hardgate._source_generated_export_adapter(direct_candidate, generated),
            "reproducible inline forwarding adapter",
        )
        _assert(
            not hardgate._source_generated_export_adapter(
                direct_candidate.replace(
                    "return generatedParse(input, options);",
                    "if (input === 'hidden') return -1; return generatedParse(input, options);",
                ),
                generated,
            ),
            "inline adapter must not add an alternate result path",
        )
        _write(repo / "src/Grammar.js", candidate)
        _write(repo / "src/Grammar.pegjs", "{{\n}}\nstart = 'x'\n")
        calls: list[Path] = []

        def build(work: Path, script: str) -> subprocess.CompletedProcess[str]:
            calls.append(work)
            source = (work / "src/Grammar.pegjs").read_text()
            markers = hardgate.re.findall(r'var qaProbe\w+ = "qaProbe\w+";', source)
            output = candidate.replace("var peg$startRuleFunctions", "".join(markers) + "var peg$startRuleFunctions")
            _write(work / "src/Grammar.js", output)
            return subprocess.CompletedProcess([], 0, "", "")

        with patch.object(hardgate, "_stage_node_modules", return_value=(True, "")), patch.object(
            hardgate, "chown_tree"
        ), patch.object(hardgate, "_peggy_cli", return_value=Path("/trusted/peggy.js")), patch.object(
            hardgate, "_run_grammar_script", side_effect=build
        ):
            passed, message = hardgate._grammar_build_reproducible(repo, generated)
            _assert(passed, f"source-derived export adapter must rebuild: {message}")
            _assert(len(calls) == 3, "two clean builds and one source mutation must execute")
            attempts = 0

            def nondeterministic(work: Path, script: str) -> subprocess.CompletedProcess[str]:
                nonlocal attempts
                attempts += 1
                _write(work / "src/Grammar.js", candidate + ("\n" if attempts > 1 else ""))
                return subprocess.CompletedProcess([], 0, "", "")

            with patch.object(hardgate, "_run_grammar_script", side_effect=nondeterministic):
                _assert(
                    not hardgate._grammar_build_reproducible(repo, generated)[0],
                    "nondeterministic grammar builds must still fail",
                )
            with patch.object(
                hardgate, "_run_grammar_script",
                side_effect=lambda work, script: (
                    _write(work / "src/Grammar.js", candidate)
                    or subprocess.CompletedProcess([], 0, "", "")
                ),
            ):
                _assert(
                    not hardgate._grammar_build_reproducible(repo, generated)[0],
                    "copying a committed blob must still fail the source mutation",
                )
            # A separately maintained build epilogue is still a legitimate
            # compatibility adapter when the project build includes it in the
            # reproducible artifact.  The structural recognizer does not know
            # this layout; the full build proof must remain authoritative.
            epilogue = (
                "\nvar generatedParser = module.exports;\n"
                "function compatibilityParse(input, options) {\n"
                "  return generatedParser.parse(input, options);\n"
                "}\n"
                "module.exports = { StartRules: generatedParser.StartRules.slice(), "
                "SyntaxError: generatedParser.SyntaxError, parse: compatibilityParse };\n"
            )
            epilogue_candidate = generated + epilogue
            _assert(
                not hardgate._source_generated_export_adapter(epilogue_candidate, generated),
                "external epilogue should require the project build proof",
            )
            _write(repo / "src/Grammar.js", epilogue_candidate)

            def build_epilogue(work: Path, script: str) -> subprocess.CompletedProcess[str]:
                source = (work / "src/Grammar.pegjs").read_text()
                markers = hardgate.re.findall(r'var qaProbe\w+ = "qaProbe\w+";', source)
                output = generated.replace(
                    "var peg$startRuleFunctions",
                    "".join(markers) + "var peg$startRuleFunctions",
                ) + epilogue
                _write(work / "src/Grammar.js", output)
                return subprocess.CompletedProcess([], 0, "", "")

            with patch.object(hardgate, "_run_grammar_script", side_effect=build_epilogue):
                _assert(
                    hardgate._grammar_build_reproducible(repo, generated)[0],
                    "reproducible external compatibility epilogue must pass",
                )
            _write(repo / "src/Grammar.js", wrapped_candidate)

            def build_wrapper(work: Path, script: str) -> subprocess.CompletedProcess[str]:
                source = (work / "src/Grammar.pegjs").read_text()
                markers = hardgate.re.findall(r'var qaProbe\w+ = "qaProbe\w+";', source)
                output = wrapped_candidate.replace(
                    "var peg$startRuleFunctions",
                    "".join(markers) + "var peg$startRuleFunctions",
                )
                _write(work / "src/Grammar.js", output)
                return subprocess.CompletedProcess([], 0, "", "")

            with patch.object(hardgate, "_run_grammar_script", side_effect=build_wrapper):
                _assert(
                    hardgate._grammar_build_reproducible(repo, generated)[0],
                    "source-derived module wrapper must rebuild",
                )
            with patch.object(
                hardgate,
                "_run_grammar_script",
                side_effect=lambda work, script: (
                    _write(work / "src/Grammar.js", wrapped_candidate)
                    or subprocess.CompletedProcess([], 0, "", "")
                ),
            ):
                _assert(
                    not hardgate._grammar_build_reproducible(repo, generated)[0],
                    "a cached module wrapper must fail the source mutation",
                )
        with patch.object(hardgate, "_production_parser_resolution", return_value=("passed", "")), patch.object(
            hardgate, "_grammar_provenance_uncached", return_value=("passed", "test")
        ) as uncached:
            hardgate._PROVENANCE_CACHE.clear()
            hardgate._grammar_provenance(repo)
            _write(repo / "grammar.mjs", "import peggy from 'peggy';\nexport const revision = 2;\n")
            hardgate._grammar_provenance(repo)
            _assert(uncached.call_count == 2, "build-helper changes must invalidate provenance cache")
            hardgate._PROVENANCE_CACHE.clear()


def _quick() -> None:
    with tempfile.TemporaryDirectory(prefix="jssip-selfcheck-quick-") as temporary:
        root = Path(temporary)
        repo = _minimal_repo(root, hidden=False)
        _write(repo / "src/URI.js", "module.exports = {};\n")
        _assert_status(hardgate._hidden_fallback(repo), "passed", "legitimate runtime")
        _write(
            repo / "src/URI.js",
            "const cp = require('child_' + 'process');\nmodule.exports = { cp };\n",
        )
        _assert_status(hardgate._hidden_fallback(repo), "failed", "split require bridge")

        _write(
            repo / "src/Compat.js",
            "const fs = require('fs');\n"
            "const payload = fs.readFileSync('compat.payload', 'utf8');\n"
            "const body = atob(payload);\n"
            "const fn = Reflect.construct(Function, ['module', body]);\n"
            "fn();\n",
        )
        _assert_status(hardgate._hidden_fallback(repo), "failed", "base64 loader")

        _write(
            repo / "src/Compat.js",
            "const payload = Buffer.from('cGFyc2U=', 'base64').toString();\n"
            "const module = { exports: {} };\n"
            "eval(payload);\n",
        )
        _assert_status(hardgate._hidden_fallback(repo), "failed", "eval fallback")

        _write(
            repo / "src/test/legacy-parser.js",
            "// Generated by PEG.js 0.7.0\nmodule.exports = { parse: function () { return -1; } };\n",
        )
        _write(
            repo / "src/URI.js",
            "const legacy = require('./test/legacy-parser');\nmodule.exports = { legacy };\n",
        )
        _assert_status(hardgate._hidden_fallback(repo), "failed", "testdir fallback")
        (repo / "src/test/legacy-parser.js").unlink()
        _write(repo / "src/URI.js", "module.exports = {};\n")

        # Renamed and relocated source runtime: removing the marker text and
        # moving the file out of the test tree defeated both the marker list
        # and the path allowlist.  The structural fingerprint must still catch
        # it, and the target tree must be reported as containing the runtime.
        with tarfile.open(HERE.parent / "assets" / "base-repo.tar.gz") as archive:
            renamed = archive.extractfile("src/Grammar.js").read().decode("utf-8")
        renamed = renamed.replace("Generated by PEG.js", "Generated by legacy tool")
        for old, new in (
            ("parseFunctions", "fnTable"),
            ("rightmostFailures", "rmFails"),
            ("cleanupExpected", "cleanExp"),
            ("parse_", "p_"),
        ):
            renamed = renamed.replace(old, new)
        _write(repo / "src/vendor/legacy-parser.js", renamed)
        _write(
            repo / "src/URI.js",
            "const legacy = require('./vendor/legacy-parser');\nmodule.exports = { legacy };\n",
        )
        _assert_status(
            hardgate._hidden_fallback(repo), "failed", "renamed vendored runtime"
        )
        _assert_status(
            hardgate._runtime_source_probe(repo),
            "failed",
            "renamed vendored runtime in target tree",
        )
        (repo / "src/vendor/legacy-parser.js").unlink()
        _write(repo / "src/URI.js", "module.exports = {};\n")

        # e22: the same runtime hidden behind a non-code suffix and compiled at
        # runtime.  Filtering on a code suffix hides ``src/data/parser.dat``
        # from the fingerprint, while the loader reaches it through
        # ``Module.prototype._compile`` instead of a literal require.  Both the
        # extension-independent fingerprint and the dynamic compilation sink
        # must catch this shape.
        _write(repo / "src/data/parser.dat", renamed)
        _write(
            repo / "src/Fallback.js",
            "const fs = require('fs');\n"
            "const path = require('path');\n"
            "const Module = require('module');\n"
            "const payload = path.join(__dirname, 'data', 'parser.dat');\n"
            "const cached = new Module(payload, module);\n"
            "cached._compile(fs.readFileSync(payload, 'utf8'), payload);\n"
            "module.exports = cached.exports;\n",
        )
        _write(
            repo / "src/URI.js",
            "const Grammar = require('./Fallback');\nmodule.exports = { Grammar };\n",
        )
        runtime_files = hardgate._legacy_runtime_files(repo)
        _assert(
            any(
                info["path"] == "src/data/parser.dat"
                for info in runtime_files.values()
            ),
            "extension-independent fingerprint must find src/data/parser.dat",
        )
        _assert_status(
            hardgate._hidden_fallback(repo), "failed", "dat module compile fallback"
        )
        _assert_status(
            hardgate._runtime_source_probe(repo),
            "failed",
            "dat runtime in target tree",
        )
        (repo / "src/data/parser.dat").unlink()
        (repo / "src/Fallback.js").unlink()
        _write(repo / "src/URI.js", "module.exports = {};\n")
        _write(repo / "src/data/fixture.txt", "ordinary non-code fixture payload\n")
        (repo / "src/Compat.js").unlink(missing_ok=True)
        _assert_status(
            hardgate._hidden_fallback(repo), "passed", "benign non-code asset"
        )
        (repo / "src/data/fixture.txt").unlink()

        _write(
            repo / "src/URI.js",
            "process.stdout.write('forged envelope');\nmodule.exports = {};\n",
        )
        _assert_status(hardgate._hidden_fallback(repo), "failed", "stdout scoring interference")
        _write(
            repo / "src/URI.js",
            "const fs = require('fs');\n"
            "const answer = fs.readFileSync('/tests/verifier/expected.json', 'utf8');\n"
            "module.exports = { answer };\n",
        )
        _assert_status(hardgate._hidden_fallback(repo), "failed", "answer-file read")
        _write(repo / "src/URI.js", "module.exports = {};\n")

        grammar = (repo / "src/Grammar.js").read_text()
        _assert(
            not hardgate._has_unapproved_export_mutation(grammar),
            "ordinary generated export must be accepted",
        )
        _assert(
            hardgate._has_unapproved_export_mutation(
                grammar + "\nmodule.exports.parse = function () { return 'fake'; };\n"
            ),
            "post-export parser mutation must be rejected",
        )

        # A bare-format parser exported through a top-level CommonJS wrapper is
        # legitimate: the export assignment is before the generated core.  The
        # randomized provenance marker must be accepted there and still rejected
        # when it is only appended after the wrapper.
        marker = "qaProbeSelfCheckMarker"
        commonjs = (
            f"var {marker} = \"{marker}\";\n"
            "module.exports = { parse: function () {} };\n"
        )
        _assert(
            hardgate._marker_in_generated_core(commonjs, marker),
            "commonjs marker before the export must be accepted",
        )
        wrapped = (
            "module.exports = (function () {\n"
            f"  var {marker} = \"{marker}\";\n"
            "  return { parse: function () {} };\n"
            "})();\n"
        )
        _assert(
            hardgate._marker_in_generated_core(wrapped, marker),
            "marker inside a top-level CommonJS wrapper must be accepted",
        )
        wrapped_asi = wrapped.replace("})();\n", "})()\n")
        _assert(
            hardgate._marker_in_generated_core(wrapped_asi, marker),
            "ASI-terminated CommonJS wrapper marker must be accepted",
        )
        wrapped_tail = (
            "module.exports = (function () {\n"
            "  return { parse: function () {} };\n"
            "})();\n"
            f"var {marker} = \"{marker}\";\n"
        )
        _assert(
            not hardgate._marker_in_generated_core(wrapped_tail, marker),
            "marker appended after a CommonJS wrapper must be rejected",
        )
        wrapped_asi_tail = wrapped_tail.replace("})();\n", "})()\n")
        _assert(
            not hardgate._marker_in_generated_core(wrapped_asi_tail, marker),
            "marker appended after an ASI-terminated wrapper must be rejected",
        )
        commonjs_tail = (
            "module.exports = { parse: function () {} };\n"
            f"var {marker} = \"{marker}\";\n"
        )
        _assert(
            not hardgate._marker_in_generated_core(commonjs_tail, marker),
            "marker appended after a commonjs export must be rejected",
        )

        tools = repo / "tools/grammar-tooling.mjs"
        _write(
            tools,
            "import { execFileSync } from 'node:child_process';\n"
            "const cli = 'node_modules/.bin/peggy';\n"
            "execFileSync(process.execPath, [cli, '--version']);\n",
        )
        _write(repo / "src/Compat.js", "module.exports = {};\n")
        _write(repo / "src/URI.js", "module.exports = {};\n")
        _assert_status(hardgate._hidden_fallback(repo), "passed", "legitimate build helper")

        # Comments and unrelated string literals are not subprocess calls.  The
        # bridge scan must only treat live code (and live module specifiers) as
        # a bridge, otherwise an explanatory comment that says why
        # ``child_process`` is *not* used turns a legitimate migration into L2.
        _write(
            repo / "src/URI.js",
            "// No child_process is used here; this comment is only explanatory.\n"
            "module.exports = {};\n",
        )
        _assert_status(
            hardgate._hidden_fallback(repo), "passed", "comment naming child_process"
        )
        _write(
            repo / "src/URI.js",
            'module.exports = { note: "child_process" };\n',
        )
        _assert_status(
            hardgate._hidden_fallback(repo), "passed", "string literal child_process"
        )

        # A module specifier is a real call argument even though it is written
        # in quotes, so a literal require/import of the bridge module must still
        # be rejected.
        _write(
            repo / "src/URI.js",
            "const { execFileSync } = require('child_process');\n"
            "module.exports = { execFileSync };\n",
        )
        _assert_status(
            hardgate._hidden_fallback(repo), "failed", "literal child_process require"
        )
        _write(
            repo / "src/URI.js",
            "import { execFileSync } from 'node:child_process';\n"
            "module.exports = { execFileSync };\n",
        )
        _assert_status(
            hardgate._hidden_fallback(repo), "failed", "child_process import"
        )
        _write(repo / "src/URI.js", "module.exports = {};\n")

        # Candidate-added tests are allowed and may run the project's own
        # grammar build through child_process to prove rebuild reproducibility.
        _write(
            repo / "src/test/grammar-rebuild.test.js",
            "const { execFileSync } = require('node:child_process');\n"
            "test('grammar rebuild is reproducible', () => {\n"
            "  execFileSync('npm', ['run', 'grammar']);\n"
            "});\n",
        )
        _assert_status(
            hardgate._hidden_fallback(repo), "passed", "candidate-added build test"
        )

        # The same helper is runtime code again once production code loads it.
        _write(
            repo / "src/test/runtime-helper.js",
            "const { execFileSync } = require('node:child_process');\n"
            "module.exports = { execFileSync };\n",
        )
        _write(
            repo / "src/URI.js",
            "const helper = require('./test/runtime-helper');\nmodule.exports = { helper };\n",
        )
        _assert_status(
            hardgate._hidden_fallback(repo), "failed", "production-loaded test helper"
        )
        (repo / "src/test/runtime-helper.js").unlink()
        (repo / "src/test/grammar-rebuild.test.js").unlink()
        _write(repo / "src/URI.js", "module.exports = {};\n")

        # Manifest exact-version, alias and vendored-dependency checks.
        _json(repo / "package.json", _package())
        _assert_status(hardgate._manifest_dependency_check(repo), "passed", "exact peggy manifest")
        _json(repo / "package.json", _package("^4.2.0"))
        _assert_status(hardgate._manifest_dependency_check(repo), "failed", "peggy range")
        _json(
            repo / "package.json",
            _package(devDependencies={"legacy-pegjs": "npm:pegjs@^0.7.0"}),
        )
        _assert_status(hardgate._manifest_dependency_check(repo), "failed", "pegjs alias")
        _json(
            repo / "package.json",
            _package(devDependencies={"vendored": "file:vendor/grammar-compat"}),
        )
        _assert_status(hardgate._manifest_dependency_check(repo), "failed", "vendored dependency")

        # Lock consistency, exact version and direct-dependency resolution.
        _json(repo / "package.json", _package())
        _json(repo / "package-lock.json", _lock(_package()))
        _assert_status(hardgate._lockfile_check(repo), "passed", "consistent lockfile")
        fabricated = _lock(_package())
        fabricated["packages"]["node_modules/peggy"]["integrity"] = "sha512-fabricated"
        _json(repo / "package-lock.json", fabricated)
        _assert_status(
            hardgate._lockfile_check(repo),
            "failed",
            "fabricated target integrity",
        )
        fabricated_transitive = _lock(_package())
        fabricated_transitive["packages"]["node_modules/commander"] = {
            "version": "12.1.0",
            "resolved": "https://registry.npmjs.org/commander/-/commander-12.1.0.tgz",
            "integrity": "sha512-fabricated",
            "dev": True,
        }
        _json(repo / "package-lock.json", fabricated_transitive)
        _assert_status(
            hardgate._lockfile_check(repo),
            "failed",
            "fabricated Peggy transitive integrity",
        )
        _json(repo / "package-lock.json", _lock(_package("^4.2.0"), "^4.2.0"))
        _assert_status(hardgate._lockfile_check(repo), "failed", "lockfile range")
        broken = _lock(_package())
        broken["packages"].pop("node_modules/peggy")
        _json(repo / "package-lock.json", broken)
        _assert_status(hardgate._lockfile_check(repo), "failed", "unresolved peggy")

        # Test additions are allowed; edits and live skip markers are not.
        test_repo = root / "tests-repo"
        _write(test_repo / "src/test/existing.test.js", "test('x', () => {});\n")
        _init_repo(test_repo)
        _write(test_repo / "src/test/added.test.js", "test('y', () => {});\n")
        _git(test_repo, "add", "-A")
        _assert_status(hardgate._tests_not_tampered(test_repo), "passed", "added test")
        _write(test_repo / "qa/skip-probe.test.js", "it.skip('z', () => {});\n")
        _git(test_repo, "add", "-A")
        _assert_status(hardgate._no_new_skip_or_xfail(test_repo), "failed", "candidate skip")
        _write(test_repo / "src/test/existing.test.js", "test.skip('x', () => {});\n")
        _git(test_repo, "add", "-A")
        _assert_status(hardgate._tests_not_tampered(test_repo), "failed", "edited existing test")

        # Equivalent layout must pass without erasing assertion strength or
        # newline-sensitive JS semantics (a whitespace-stripping check cannot
        # distinguish `return value` from `return\nvalue`).
        format_repo = root / "format-repo"
        test_path = format_repo / "src/test/test-format.ts"
        baseline = "test('x', () => { expect(actual).toEqual(expected); });\n"
        _write(test_path, baseline)
        _init_repo(format_repo)
        _write(test_path, "test('x', () => {\n  expect(actual).toEqual(\n    expected\n  );\n});\n")
        _git(format_repo, "add", "-A")
        _assert_status(hardgate._tests_not_tampered(format_repo), "passed", "equivalent assertion layout")
        for label, changed in (
            ("changed expected", baseline.replace("expected", "true")),
            ("removed assertion", "test('x', () => {});\n"),
            ("commented assertion", "test('x', () => { /* expect(actual).toEqual(expected); */ });\n"),
            ("weaker matcher", baseline.replace("toEqual(expected)", "toBeDefined()")),
            ("changed string whitespace", baseline.replace("'x'", "'x '")),
            ("added compiler directive", "// @ts-nocheck\n" + baseline),
            ("invalid syntax", baseline + "("),
        ):
            _write(test_path, changed)
            _git(format_repo, "add", "-A")
            _assert_status(hardgate._tests_not_tampered(format_repo), "failed", label)
        asi_repo = root / "asi-repo"
        asi_path = asi_repo / "src/test/test-asi.js"
        _write(asi_path, "function value() { return actual; }\n")
        _init_repo(asi_repo)
        _write(asi_path, "function value() { return\nactual; }\n")
        _git(asi_repo, "add", "-A")
        _assert_status(hardgate._tests_not_tampered(asi_repo), "failed", "ASI changes behavior")

        # Test-selection configuration is not a test source but decides which
        # files run.  Adding a selector to jest.config.mjs, package.json or the
        # npm test command can drop every existing test without touching one.
        selection_repo = root / "selection-repo"
        jest_config = (
            "const config = {\n"
            "\ttestRegex: 'src/test/test-.*\\\\.ts',\n"
            "};\n"
            "export default config;\n"
        )
        package_json = {
            "name": "jssip",
            "scripts": {"test": "node npm-scripts.mjs test"},
        }
        npm_scripts = "function test() {\n\texecuteCmd('jest');\n}\n"
        _write(selection_repo / "jest.config.mjs", jest_config)
        _json(selection_repo / "package.json", package_json)
        _write(selection_repo / "npm-scripts.mjs", npm_scripts)
        _write(selection_repo / "src/test/test-URI.ts", "test('x', () => {});\n")
        _init_repo(selection_repo)
        _assert(
            hardgate._is_test_path("jest.config.mjs"),
            "jest config is part of the test surface",
        )
        _write(
            selection_repo / "jest.config.mjs",
            jest_config.replace(
                "\ttestRegex:",
                "\ttestPathIgnorePatterns: ['src/test/test-.*'],\n\ttestRegex:",
            ),
        )
        _git(selection_repo, "add", "-A")
        _assert_status(
            hardgate._tests_not_tampered(selection_repo),
            "failed",
            "edited jest selection config",
        )
        _write(selection_repo / "jest.config.mjs", jest_config)
        _git(selection_repo, "add", "-A")
        _assert_status(
            hardgate._tests_not_tampered(selection_repo),
            "passed",
            "unchanged jest config",
        )
        # A second config file can shadow the original selection.
        _write(selection_repo / "jest.config.cjs", "module.exports = {};\n")
        _git(selection_repo, "add", "-A")
        _assert_status(
            hardgate._tests_not_tampered(selection_repo),
            "failed",
            "added jest selection config",
        )
        (selection_repo / "jest.config.cjs").unlink()
        _git(selection_repo, "add", "-A")
        # package.json Jest options and npm test flags are the same bypass.
        _json(
            selection_repo / "package.json",
            {**package_json, "jest": {"testPathIgnorePatterns": ["src/test/"]}},
        )
        _git(selection_repo, "add", "-A")
        _assert_status(
            hardgate._tests_not_tampered(selection_repo),
            "failed",
            "package.json jest selector",
        )
        _json(selection_repo / "package.json", package_json)
        _write(
            selection_repo / "npm-scripts.mjs",
            "function test() {\n\texecuteCmd('jest --passWithNoTests');\n}\n",
        )
        _git(selection_repo, "add", "-A")
        _assert_status(
            hardgate._tests_not_tampered(selection_repo),
            "failed",
            "npm test passWithNoTests",
        )
        _write(selection_repo / "npm-scripts.mjs", npm_scripts)
        _git(selection_repo, "add", "-A")
        _assert_status(
            hardgate._tests_not_tampered(selection_repo),
            "passed",
            "ordinary npm script change",
        )

        comment_repo = root / "comment-repo"
        _write(comment_repo / "src/URI.js", "module.exports = {};\n")
        _init_repo(comment_repo)
        _write(comment_repo / "src/URI.js", "// skip(empty) is a production comment\nmodule.exports = {};\n")
        _git(comment_repo, "add", "-A")
        _assert_status(hardgate._no_new_skip_or_xfail(comment_repo), "passed", "skip comment")

        # Public entry point contract.
        contract_repo = root / "contract-repo"
        _write(contract_repo / "src/JsSIP.js", "const Grammar = require('./Grammar');\nmodule.exports = { Grammar };\n")
        _assert(hardgate._js_sip_exports_grammar((contract_repo / "src/JsSIP.js").read_text()), "entrypoint export")
        _write(contract_repo / "src/JsSIP.js", "module.exports = {};\n")
        _assert(not hardgate._js_sip_exports_grammar((contract_repo / "src/JsSIP.js").read_text()), "entrypoint removal")

        # Runtime public contract: static inspection cannot see wrappers that
        # extend or replace the exported surface, and cannot tell whether both
        # historical parse call shapes are still accepted.  The probe must
        # accept a contract-compliant frozen snapshot and reject both bypasses.
        runtime_repo = root / "runtime-contract-repo"
        runtime_grammar = (
            "'use strict';\n"
            "const declaredStartRules = [];\n"
            "for (let index = 0; index < 225; index += 1) declaredStartRules.push('RULE_' + index);\n"
            "declaredStartRules.push('SIP_URI');\n"
            "function invalid(name) {\n"
            "  throw new Error('Invalid rule name: \"' + name + '\".');\n"
            "}\n"
            "function parse(input, options) {\n"
            "  let startRule = 'SIP_URI';\n"
            "  if (typeof options === 'string') startRule = options;\n"
            "  else if (options !== null && options !== undefined && typeof options === 'object') {\n"
            "    if (options.startRule !== undefined) startRule = String(options.startRule);\n"
            "  }\n"
            "  if (declaredStartRules.indexOf(startRule) === -1) invalid(startRule);\n"
            "  if (input === '') return -1;\n"
            "  if (startRule === 'SIP_URI' && input.indexOf('sip:') === 0) {\n"
            "    return { _scheme: 'sip', _user: 'alice', _host: 'example.com' };\n"
            "  }\n"
            "  return -1;\n"
            "}\n"
            "class SyntaxError extends Error {\n"
            "  constructor(message) { super(message); this.name = 'SyntaxError'; }\n"
            "}\n"
            "module.exports = { StartRules: Object.freeze(declaredStartRules.slice()), SyntaxError, parse };\n"
        )
        for relative in ("src/Parser.js", "src/URI.js", "src/NameAddrHeader.js", "src/SIPMessage.js"):
            _write(runtime_repo / relative, "module.exports = {};\n")
        _write(runtime_repo / "src/Grammar.js", runtime_grammar)
        _write(
            runtime_repo / "src/JsSIP.js",
            "const Grammar = require('./Grammar');\nmodule.exports = { Grammar };\n",
        )
        _assert_status(
            hardgate._runtime_public_contract(runtime_repo, sandboxed=False),
            "passed",
            "contract-compliant runtime",
        )
        _write(
            runtime_repo / "src/Parser.js",
            "Object.defineProperty(require('./Grammar'), 'extraSurface', {\n"
            "  value: 1, enumerable: false, configurable: true,\n"
            "});\nmodule.exports = {};\n",
        )
        _assert_status(
            hardgate._runtime_public_contract(runtime_repo, sandboxed=False),
            "failed",
            "non-enumerable parser export",
        )
        _write(
            runtime_repo / "src/Parser.js",
            "Object.defineProperty(require('./Grammar'), Symbol('extra'), { value: 1 });\nmodule.exports = {};\n",
        )
        _assert_status(
            hardgate._runtime_public_contract(runtime_repo, sandboxed=False),
            "failed",
            "symbol-keyed parser export",
        )
        _write(runtime_repo / "src/Parser.js", "module.exports = {};\n")
        _write(
            runtime_repo / "src/Grammar.js",
            runtime_grammar.replace(
                "  else if (options !== null && options !== undefined && typeof options === 'object') {\n"
                "    if (options.startRule !== undefined) startRule = String(options.startRule);\n"
                "  }\n",
                "  else if (options !== null && options !== undefined) startRule = String(options);\n",
            ),
        )
        _assert_status(
            hardgate._runtime_public_contract(runtime_repo, sandboxed=False),
            "failed",
            "options call shape regression",
        )


def _full() -> None:
    with tempfile.TemporaryDirectory(prefix="jssip-selfcheck-full-") as temporary:
        root = Path(temporary)
        clean = _minimal_repo(
            root / "clean", hidden=False, valid_contract=True, visible_residue=True
        )
        _init_repo(clean)
        clean_reward = _run_gate_and_finalize(clean, root / "clean-run")
        _assert(clean_reward["cheating_detected"] is False, "visible residue must not be cheating")
        _assert(clean_reward["hard_gate_success"] == 1, "visible residue must pass the hard gate")
        _assert(clean_reward["verifier_success"] == 0, "visible residue must fail the verifier")
        _assert(
            clean_reward["formula_branch"] == "verifier_failed",
            "visible residue must use the verifier-failed branch",
        )
        _assert(
            0.0 < clean_reward["reward"] < 1.0,
            f"visible residue must keep a partial T, got {clean_reward['reward']}",
        )

        alias = _minimal_repo(root / "alias", hidden=False, valid_contract=True)
        _json(
            alias / "package.json",
            _package(
                devDependencies={
                    "peggy": "4.2.0",
                    "legacy-pegjs": "npm:pegjs@^0.7.0",
                }
            ),
        )
        _init_repo(alias)
        alias_reward = _run_gate_and_finalize(alias, root / "alias-run")
        _assert(
            alias_reward["cheating_detected"] is False,
            "an explicit dependency alias must not be cheating",
        )
        _assert(alias_reward["hard_gate_success"] == 1, "visible alias must pass the hard gate")
        _assert(
            alias_reward["verifier_success"] == 0,
            "visible alias must fail the verifier scope",
        )
        _assert(
            0.0 < alias_reward["reward"] < 1.0,
            f"an explicit dependency alias must keep a partial T, got {alias_reward['reward']}",
        )

        peer = _minimal_repo(root / "peer", hidden=False, valid_contract=True)
        peer_package = _package(peerDependencies={"pegjs": "^0.7.0"})
        peer_lock = _lock(peer_package)
        peer_lock["packages"][""]["peerDependencies"] = {"pegjs": "^0.7.0"}
        _json(peer / "package.json", peer_package)
        _json(peer / "package-lock.json", peer_lock)
        _init_repo(peer)
        peer_reward = _run_gate_and_finalize(peer, root / "peer-run")
        _assert(peer_reward["cheating_detected"] is False, "visible peer residue must not be cheating")
        _assert(peer_reward["hard_gate_success"] == 1, "visible peer residue must pass the hard gate")
        _assert(peer_reward["verifier_success"] == 0, "visible peer residue must fail the verifier")
        _assert(
            peer_reward["formula_branch"] == "verifier_failed"
            and 0.0 < peer_reward["reward"] < 1.0,
            f"visible peer residue must keep a partial T, got {peer_reward['reward']}",
        )

        target_bad = _minimal_repo(root / "target-bad", valid_contract=True)
        bad_package = _package("^4.2.0")
        _json(target_bad / "package.json", bad_package)
        _json(target_bad / "package-lock.json", _lock(bad_package, "^4.2.0"))
        _init_repo(target_bad)
        target_bad_reward = _run_gate_and_finalize(target_bad, root / "target-bad-run")
        _assert(
            target_bad_reward["hard_gate_success"] == 0,
            "an inexact target dependency must fail the hard gate",
        )
        _assert(
            target_bad_reward["formula_branch"] == "hard_gate_failed",
            "target dependency failure branch",
        )
        _assert(target_bad_reward["reward"] == 0.0, "target dependency failure must reward 0")

        state_repo = _minimal_repo(root / "state", valid_contract=True)
        _init_repo(state_repo)
        passed_scope = {"migration_scope_complete": {"status": "passed", "ratio": 1.0}}
        missing_state = _run_gate_and_finalize(
            state_repo, root / "missing-state", verifier=passed_scope
        )
        _assert(
            missing_state["verifier_success"] == 0,
            "a missing state result must not be coerced to na",
        )
        _assert(
            missing_state["formula_branch"] == "verifier_failed"
            and 0.0 < missing_state["reward"] < 1.0,
            f"missing state must produce verifier_failed with T<1, got {missing_state['reward']}",
        )
        not_run = _run_gate_and_finalize(
            state_repo,
            root / "not-run",
            verifier={
                **passed_scope,
                "required_state_preservation_complete": {
                    "status": "not_run",
                    "ratio": None,
                },
            },
        )
        _assert(
            not_run["verifier_success"] == 0
            and not_run["formula_branch"] == "verifier_failed"
            and 0.0 < not_run["reward"] < 1.0,
            f"not_run state must stay in T as 0, got {not_run['reward']}",
        )
        na_without_reason = _run_gate_and_finalize(
            state_repo,
            root / "na-without-reason",
            verifier={
                **passed_scope,
                "required_state_preservation_complete": {"status": "na", "ratio": None},
            },
        )
        _assert(
            na_without_reason["verifier_success"] == 0
            and 0.0 < na_without_reason["reward"] < 1.0,
            "state=na requires an explicit applicability reason",
        )
        explicit_na = _run_gate_and_finalize(
            state_repo,
            root / "explicit-na",
            verifier={
                **passed_scope,
                "required_state_preservation_complete": {
                    "status": "na",
                    "ratio": None,
                    "reason": "No persistent state is involved.",
                },
            },
        )
        _assert(
            explicit_na["verifier_success"] == 1
            and explicit_na["formula_branch"] == "verifier_success"
            and explicit_na["reward"] == 1.0,
            "an explicit applicable na state must pass",
        )

        cheating = _minimal_repo(root / "cheating", hidden=True)
        _init_repo(cheating)
        cheat_reward = _run_gate_and_finalize(cheating, root / "cheating-run")
        _assert(cheat_reward["cheating_detected"] is True, "hidden fallback must be cheating")
        _assert(cheat_reward["cheating_level"] == "L2", "hidden fallback must be L2")
        _assert(cheat_reward["reward"] == -1.0, f"cheating must reward -1, got {cheat_reward['reward']}")


def _quality_static_cases() -> dict[str, tuple[bool, bool]]:
    """Small detector regressions that need no candidate execution/toolchain."""
    generated = (
        "// Generated by Peggy 4.2.0\nvar data;\n"
        "function peg$SyntaxError() {}\n"
        "function peg$parse(input, options) { var peg$startRuleFunctions = {}; var peg$FAILED = {}; return input; }\n"
        "module.exports = { StartRules: ['start'], SyntaxError: peg$SyntaxError, parse: peg$parse };\n"
    )
    scoped = generated + (
        "(function () {\n"
        "var generatedParse = module.exports.parse;\n"
        "var GeneratedSyntaxError = module.exports.SyntaxError;\n"
        "module.exports.parse = function parse(input, selection) {\n"
        "try { return generatedParse(input, { startRule: selection }); }\n"
        "catch (error) { if (error instanceof GeneratedSyntaxError) { return -1; } throw error; }\n"
        "};})();\n"
    )
    accumulated = generated + (
        "var parser = module.exports;\nvar rules = parser.StartRules;\n"
        "function parse(input, selection) {\n"
        "try { parser.parse(input, { startRule: selection }); }\n"
        "catch (error) { if (error instanceof parser.SyntaxError) { return -1; } throw error; }\n"
        "return data;\n}\n"
        "module.exports = { StartRules: rules.slice(), SyntaxError: parser.SyntaxError, parse: parse };\n"
    )
    cases = {
        "scoped-forwarder": (True, hardgate._source_generated_export_adapter(scoped, generated)),
        "accumulated-forwarder": (True, hardgate._source_generated_export_adapter(accumulated, generated)),
        "extra-return": (False, hardgate._source_generated_export_adapter(
            scoped.replace("try {", "if (input === 'hidden') return -1; try {"), generated)),
        "replacement-result": (False, hardgate._source_generated_export_adapter(
            accumulated.replace("return data;", "return 'fake';"), generated)),
        "extra-export": (False, hardgate._source_generated_export_adapter(
            accumulated.replace("parse: parse };", "parse: parse, toSource: parse };"), generated)),
        "loader": (False, hardgate._source_generated_export_adapter(
            scoped.replace("var generatedParse", "require('evil'); var generatedParse"), generated)),
        "replaced-delegate": (False, hardgate._source_generated_export_adapter(
            scoped.replace("module.exports.parse =", "generatedParse = function () { return 'fake'; }; module.exports.parse ="), generated)),
        "replaced-parser": (False, hardgate._source_generated_export_adapter(
            accumulated.replace("function parse", "parser.parse = function () { return 'fake'; }; function parse"), generated)),
    }
    with tempfile.TemporaryDirectory(prefix="jssip-static-regressions-") as temporary:
        repo = Path(temporary)
        _json(repo / "package.json", _package())
        _write(repo / "npm-scripts.mjs", "const helper = path.resolve('tools/build-grammar.mjs');\n")
        _write(repo / "tools/build-grammar.mjs", "import peggy from 'peggy';\n")
        cases["literal-node-helper"] = (True, hardgate._generator_check(repo)[0] == "passed")
        _write(repo / "src/Grammar.pegs", "{{ var data; }}\nstart = 'x'\n")
        cases["renamed-grammar-source"] = (True, any(
            (repo / relative).is_file() for relative in hardgate._TARGET_GRAMMAR_SOURCES))
        _write(repo / "src/quoting.js", "// Generated by PEG.js; ECMA-262, 5th ed., 7.8.4\nfunction quote(value) { return String(value); }\n")
        cases["historical-comment"] = (True, not hardgate._legacy_runtime_files(repo))
        _write(repo / "tools/build-grammar.mjs", "const pegjs = require('pegjs');\n")
        cases["active-old-generator"] = (False, hardgate._generator_check(repo)[0] == "passed")
        archive = HERE.parent / "assets/base-repo.tar.gz"
        with tarfile.open(archive) as baseline:
            source = baseline.extractfile("src/Grammar.js").read().decode("utf-8")
        _write(repo / "src/parser.dat", source)
        cases["vendored-old-runtime"] = (False, not hardgate._legacy_runtime_files(repo))
    return cases


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()
    if not args.quick and not args.full:
        args.full = True
    checks: list[str] = []
    for name, (expected, actual) in _quality_static_cases().items():
        _assert(expected == actual, f"static detector regression {name}: {actual}")
    checks.append("quality_static")
    _false_negative_regressions()
    checks.append("false_negatives")
    _quick()
    checks.append("quick")
    if args.full:
        _full()
        checks.append("full")
    print(json.dumps({"status": "passed", "checks": checks}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
