"""Fail-closed self-check for the Migration verifier safety invariants."""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import argparse
import json
import tempfile
from pathlib import Path
from typing import Any, Mapping

try:
    from .evidence import _dependency_scan, _test_integrity, build_hardgate_evidence, build_source_test_baseline
    from .hardgate_engine import evaluate_hardgate
    from .integrity import validate_public_integrity, validate_public_projection_contract
    from .reward import calculate_verifier_reward
    from .residue import scan_residue_rules
except ImportError:  # Executed as a standalone script inside the image.
    from evidence import _dependency_scan, _test_integrity, build_hardgate_evidence, build_source_test_baseline
    from hardgate_engine import evaluate_hardgate
    from integrity import validate_public_integrity, validate_public_projection_contract
    from reward import calculate_verifier_reward
    from residue import scan_residue_rules


_ATOMS = (
    "patch_applicable",
    "build_pass",
    "dependency_resolved",
    "no_deceptive_source_fallback",
    "public_contract_preserved",
    "p2p_regression_preserved",
    "differential_preserved",
    "critical_data_preserved",
    "tests_not_tampered",
    "no_new_skip_or_xfail",
    "protected_paths_unchanged",
    "anti_cheating_pass",
)


def _atom(status: str = "PASS") -> dict[str, Any]:
    return {"status": status, "source": "selfcheck", "evidence": []}


def _evidence(**overrides: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "evidence_mode": "derived",
        **{name: _atom() for name in _ATOMS},
        "source_hits": [],
        "ordinary_residue_hits": [],
        "deceptive_fallback_hits": [],
        "cheating": False,
    }
    for name, status in overrides.items():
        payload[name] = _atom(status)
    return payload


def _check_hardgate_reward() -> list[str]:
    failures: list[str] = []
    passing = evaluate_hardgate(Path("/tmp"), _evidence())
    if passing.get("status") != "PASS" or passing.get("cheating"):
        failures.append("all-pass evidence must produce a non-cheating PASS hardgate")
    reward = calculate_verifier_reward(
        hardgate=passing,
        verifier={
            "status": "success",
            "progress": {
                "eligible": True,
                "T": 1.0,
                "scope_ratio": 1.0,
                "f2p_ratio": 1.0,
                "state_ratio": None,
                "state_applicability": "na",
                "errors": [],
            },
        },
    )
    if reward.get("branch") != "verifier_success" or reward.get("value") != 1.0:
        failures.append("verifier success must reward 1.0")
    zero = calculate_verifier_reward(
        hardgate=evaluate_hardgate(Path("/tmp"), _evidence(patch_applicable="FAIL")),
        verifier={"status": "failed"},
    )
    if zero.get("branch") != "hard_gate_failed" or zero.get("value") != 0.0:
        failures.append("non-cheating hardgate failure must reward 0.0")
    cheating = evaluate_hardgate(
        Path("/tmp"), _evidence(no_deceptive_source_fallback="FAIL")
    )
    penalized = calculate_verifier_reward(hardgate=cheating, verifier={"status": "failed"})
    if not cheating.get("cheating") or penalized.get("value") != -1.0:
        failures.append("deceptive fallback must be cheating and reward -1.0")
    l1 = evaluate_hardgate(
        Path("/tmp"), _evidence(tests_not_tampered="FAIL")
    )
    l1_reward = calculate_verifier_reward(
        hardgate=l1, verifier={"status": "failed"}
    )
    if (
        not l1.get("cheating")
        or l1.get("cheat_level") != "L1"
        or l1_reward.get("value") != -1.0
    ):
        failures.append("test tampering must be L1 cheating and reward -1.0")
    return failures


def _check_residue() -> list[str]:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        (root / "lib/legacy").mkdir(parents=True)
        (root / "lib/server.js").write_text(
            'const { relay } = require("./legacy/relay");\n'
            'require("./legacy/relay2");\n'
            'require("./legacy/asset-relay");\n'
            'relay();\n',
            encoding="utf-8",
        )
        (root / "lib/legacy/relay.js").write_text(
            'const express = require("express");\nconst app = express();\n'
            "app.use(express.static('public'));\nmodule.exports = { relay: () => app };\n",
            encoding="utf-8",
        )
        (root / "lib/legacy/relay2.js").write_text(
            'const sourceRuntime = require(["exp","ress"].join(""));\n'
            "const app = sourceRuntime();\n"
            'app.use(sourceRuntime["sta" + "tic"]("public"));\n'
            "module.exports = app;\n",
            encoding="utf-8",
        )
        (root / "lib/legacy/charrelay.js").write_text(
            "const sourceRuntime = require(String.fromCharCode("
            "101, 120, 112, 114, 101, 115, 115));\n"
            "module.exports = sourceRuntime;\n",
            encoding="utf-8",
        )
        (root / "lib/legacy/mentions.js").write_text(
            'const MIGRATED_FROM_FRAMEWORKS = ["express", "fastify", "koa"];\n'
            "module.exports = { MIGRATED_FROM_FRAMEWORKS };\n",
            encoding="utf-8",
        )
        (root / "lib/legacy/asset-relay").write_text(
            'const express = require("express");\nconst app = express();\n'
            "module.exports = app;\n",
            encoding="utf-8",
        )
        (root / "lib/legacy/dead.js").write_text(
            'const express = require("express");\nmodule.exports = express;\n',
            encoding="utf-8",
        )
        (root / "lib/legacy/hono.js").write_text(
            'const { Hono } = require("hono");\nmodule.exports = new Hono();\n',
            encoding="utf-8",
        )
        (root / "lib/server/http").mkdir(parents=True)
        server_path = root / "lib/server.js"
        server_path.write_text(
            server_path.read_text(encoding="utf-8")
            + 'require("./server/http/express-runtime");\n'
            + 'require("./server/http/hono-app");\n'
            + 'require("./server/http/hono-express");\n'
            + 'require("./legacy/charrelay");\n'
            + 'require("./legacy/mentions");\n',
            encoding="utf-8",
        )
        (root / "lib/server/http/express-runtime.js").write_text(
            'const express = require(["ex","press"].join(""));\n'
            "module.exports = express;\n",
            encoding="utf-8",
        )
        (root / "lib/server/http/hono-app.js").write_text(
            'const { Hono } = require("hono");\n'
            "module.exports = { createHonoApp: () => new Hono() };\n",
            encoding="utf-8",
        )
        (root / "lib/server/http/hono-express.js").write_text(
            'const { Hono } = require("hono");\n'
            "module.exports = { createApp: () => new Hono() };\n",
            encoding="utf-8",
        )
        runtime = scan_residue_rules(
            root,
            [
                {
                    "id": "SC-RUNTIME",
                    "kind": "express_runtime",
                    "target": "express",
                    "paths": ["lib", "bin"],
                }
            ],
        )
        leftover = scan_residue_rules(
            root,
            [
                {
                    "id": "SC-LEFTOVER",
                    "kind": "express_leftover",
                    "target": "express",
                    "paths": ["lib", "bin"],
                }
            ],
        )
        if [hit["path"] for hit in runtime] != [
            "lib/legacy/asset-relay",
            "lib/legacy/charrelay.js",
            "lib/legacy/relay.js",
            "lib/legacy/relay2.js",
            "lib/server/http/express-runtime.js",
        ]:
            failures.append(f"reachable Express bridge not classified as deceptive: {runtime}")
        if [hit["path"] for hit in leftover] != ["lib/legacy/dead.js"]:
            failures.append(f"unreachable Express module not classified as leftover: {leftover}")
    return failures


def _tamper_config(allowed: list[str]) -> dict[str, Any]:
    return {
        "policy": {
            "allowed_equivalent_test_paths": allowed,
            "deceptive_fallback_rules": [
                {"id": "SC-NONE", "kind": "literal", "paths": ["package.json"], "target": "__absent__"}
            ],
        }
    }


def _check_tamper_and_skip() -> list[str]:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        relative = "tests/sample.test.js"
        path = root / relative
        path.parent.mkdir(parents=True)
        original = (
            'it("keeps values", () => {\n'
            "  expect(load(1)).toEqual(1);\n"
            "  expect(load(2)).toBe(2);\n"
            "  expect(load(3)).not.toBeNull();\n"
            "  expect(load(4)).toBeDefined();\n"
            "});\n"
        )
        path.write_text(original, encoding="utf-8")
        baseline = build_source_test_baseline(root)
        config = _tamper_config([relative])

        path.write_text('it("keeps values", () => {\n  expect(load(1)).toEqual(1);\n});\n', encoding="utf-8")
        tamper, _ = _test_integrity(root, baseline, config)
        if tamper.get("status") != "FAIL":
            failures.append("deleted assertions in an allowed test must fail tamper detection")

        path.write_text(
            'it("keeps values", () => {\n'
            "  expect(true).toBe(true);\n"
            "  expect(load(2)).toBe(2);\n"
            "  expect(load(3)).not.toBeNull();\n"
            "  expect(load(4)).toBeDefined();\n"
            "});\n",
            encoding="utf-8",
        )
        tamper, _ = _test_integrity(root, baseline, config)
        if tamper.get("status") != "FAIL":
            failures.append("a trivial assertion replacing a value assertion must fail tamper detection")

        path.write_text(original.replace("toEqual(1)", "toMatchObject(1)"), encoding="utf-8")
        tamper, _ = _test_integrity(root, baseline, config)
        if tamper.get("status") != "FAIL":
            failures.append("toEqual to toMatchObject weakening must fail tamper detection")

        path.write_text(original.replace("toEqual(1)", "toBeGreaterThan(0)"), encoding="utf-8")
        tamper, _ = _test_integrity(root, baseline, config)
        if tamper.get("status") != "FAIL":
            failures.append("an exact assertion replaced by a range matcher must fail tamper detection")

        path.write_text(
            original.replace("expect(load(1)).toEqual(1);", "false && expect(load(1)).toEqual(1);"),
            encoding="utf-8",
        )
        tamper, _ = _test_integrity(root, baseline, config)
        if tamper.get("status") != "FAIL":
            failures.append("a short-circuited assertion must fail tamper detection")

        duplicate_relative = "tests/duplicate.test.js"
        duplicate_path = root / duplicate_relative
        duplicate_original = (
            'it("keeps status", () => {\n'
            "  expect(a.status).toBe(401);\n"
            "  expect(b.status).toBe(401);\n"
            "});\n"
        )
        duplicate_path.write_text(duplicate_original, encoding="utf-8")
        duplicate_baseline = build_source_test_baseline(root)
        duplicate_path.write_text(
            duplicate_original.replace(
                "expect(a.status).toBe(401);",
                "expect(a.status).toBeGreaterThan(399);",
            ),
            encoding="utf-8",
        )
        tamper, _ = _test_integrity(
            root, duplicate_baseline, _tamper_config([duplicate_relative])
        )
        if tamper.get("status") != "FAIL":
            failures.append("a sibling copy must not mask a weakened assertion in the same file")

        path.write_text(
            original + 'it("adds coverage", () => {\n  expect(load(5)).toEqual(5);\n});\n',
            encoding="utf-8",
        )
        tamper, _ = _test_integrity(root, baseline, config)
        if tamper.get("status") != "PASS":
            failures.append("adding assertions to an allowed test must keep tamper detection passing")

        response_relative = "tests/response.test.js"
        response_path = root / response_relative
        response_path.write_text(
            'it("keeps the response body", () => {\n'
            '  expect(response.body).toBe("retry me");\n'
            '});\n',
            encoding="utf-8",
        )
        response_baseline = build_source_test_baseline(root)
        response_path.write_text(
            'it("keeps the response body", async () => {\n'
            '  expect(await response.text()).toBe("retry me");\n'
            '});\n',
            encoding="utf-8",
        )
        tamper, _ = _test_integrity(
            root, response_baseline, _tamper_config([response_relative])
        )
        if tamper.get("status") != "PASS":
            failures.append("equivalent HTTP response body accessors must preserve assertions")

        path.write_text(
            'it("keeps values", () => {\n'
            "  return;\n"
            "  expect(load(1)).toEqual(1);\n"
            "  expect(load(2)).toBe(2);\n"
            "  expect(load(3)).not.toBeNull();\n"
            "  expect(load(4)).toBeDefined();\n"
            "});\n",
            encoding="utf-8",
        )
        tamper, _ = _test_integrity(root, baseline, config)
        if tamper.get("status") != "FAIL":
            failures.append("unreachable assertions after return must fail tamper detection")

        path.write_text(original + '\nit.todo("disabled later");\n', encoding="utf-8")
        _, skip = _test_integrity(root, baseline, config)
        delta = (skip.get("evidence") or {}).get("delta", {})
        if skip.get("status") != "FAIL" or int(delta.get("disabled", 0)) <= 0:
            failures.append("it.todo must be counted as a new disabled test")

        path.write_text(original + '\nit.concurrent.skip("disabled later");\n', encoding="utf-8")
        _, skip = _test_integrity(root, baseline, config)
        if skip.get("status") != "FAIL":
            failures.append("it.concurrent.skip must be counted as a new disabled test")
    return failures


def _differential_evidence(diff_status: str) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        (root / "package.json").write_text("{}", encoding="utf-8")
        config: Mapping[str, Any] = {
            "tests": {
                "p2p": [{"id": "P2P-001", "kind": "P2P"}],
                "differential": [{"id": "DIFF-001", "kind": "DIFFERENTIAL"}],
                "state": [],
            },
            "policy": {
                "state_applicable": False,
                "deceptive_fallback_rules": [
                    {"id": "SC-NONE", "kind": "literal", "paths": ["package.json"], "target": "__absent__"}
                ],
                "protected_paths": [],
                "forbidden_report_markers": [],
            },
        }
        observations = [
            {"id": "P2P-001", "kind": "P2P", "status": "PASS"},
            {"id": "DIFF-001", "kind": "DIFFERENTIAL", "status": diff_status},
        ]
        receipts = [
            {
                "schema_version": "migration-execution-receipt:v1",
                "kind": "target-execution",
                "index": 1,
                "command": ["node"],
                "returncode": 0,
                "timed_out": False,
                "report_present": True,
            },
            {
                "schema_version": "migration-execution-receipt:v1",
                "kind": "report",
                "index": 2,
                "command": [],
                "returncode": 0,
                "timed_out": False,
                "valid": True,
            },
        ]
        return build_hardgate_evidence(
            root,
            config=config,
            observations=observations,
            observation_errors=[],
            receipts=receipts,
            patch_applicable=True,
            source_test_baseline={"files": {}, "marker_counts": {}},
        )


def _check_differential() -> list[str]:
    failures: list[str] = []
    failing = evaluate_hardgate(Path("/tmp"), _differential_evidence("FAIL"))
    if failing.get("status") != "FAIL":
        failures.append("a DIFFERENTIAL failure must fail the hard gate")
    compound = (failing.get("compound_gates") or {}).get("p2p_regression_preserved") or {}
    if compound.get("status") != "FAIL":
        failures.append("a DIFFERENTIAL failure must fail p2p_regression_preserved")
    passing = evaluate_hardgate(Path("/tmp"), _differential_evidence("PASS"))
    if passing.get("status") != "PASS":
        failures.append("all-pass DIFFERENTIAL evidence must keep the hard gate PASS")
    return failures


def _check_residual_dependency() -> list[str]:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        (root / "package-lock.json").write_text(
            json.dumps(
                {
                    "packages": {
                        "node_modules/hono": {"version": "4.13.7"},
                        "node_modules/@hono/node-server": {"version": "2.1.1"},
                    }
                }
            ),
            encoding="utf-8",
        )
        clean = {"dependencies": {"hono": "4.13.7", "@hono/node-server": "2.1.1"}}
        (root / "package.json").write_text(json.dumps(clean), encoding="utf-8")
        report = _dependency_scan(root, residual_dependencies=("express",))
        if report.get("status") != "PASS":
            failures.append("a clean manifest must keep the dependency scan passing")
        (root / "package.json").write_text(
            json.dumps(
                {
                    "dependencies": clean["dependencies"],
                    "devDependencies": {"express": "^5.2.1"},
                }
            ),
            encoding="utf-8",
        )
        report = _dependency_scan(root, residual_dependencies=("express",))
        if report.get("status") != "FAIL" or not any(
            row.get("reason") == "residual-source-dependency"
            for row in report.get("failures", [])
        ):
            failures.append("a devDependency source-framework residue must fail the dependency scan")
        (root / "package.json").write_text(
            json.dumps(
                {
                    "dependencies": {
                        **clean["dependencies"],
                        "express-like": "npm:express@5.2.1",
                    }
                }
            ),
            encoding="utf-8",
        )
        report = _dependency_scan(root, residual_dependencies=("express",))
        if report.get("status") != "FAIL":
            failures.append("an aliased source dependency must fail the dependency scan")
    return failures


def _check_protected_paths() -> list[str]:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        (root / "package.json").write_text("{}", encoding="utf-8")
        (root / "grader-output.json").write_text("{}", encoding="utf-8")
        config: Mapping[str, Any] = {
            "tests": {"p2p": [], "differential": [], "state": []},
            "policy": {
                "state_applicable": False,
                "deceptive_fallback_rules": [
                    {"id": "SC-NONE", "kind": "literal", "paths": ["package.json"], "target": "__absent__"}
                ],
                "protected_paths": ["grader-output.json"],
                "forbidden_report_markers": [],
            },
        }
        receipts = [
            {
                "schema_version": "migration-execution-receipt:v1",
                "kind": "dependency-resolution",
                "index": 1,
                "command": ["npm", "ls", "hono"],
                "returncode": 1,
                "timed_out": False,
            }
        ]
        evidence = build_hardgate_evidence(
            root,
            config=config,
            observations=[],
            observation_errors=[],
            receipts=receipts,
            patch_applicable=True,
            source_test_baseline={"files": {}, "marker_counts": {}},
        )
        hardgate = evaluate_hardgate(root, evidence)
        reward = calculate_verifier_reward(
            hardgate=hardgate, verifier={"status": "failed"}
        )
        if (evidence.get("protected_paths_unchanged") or {}).get("status") != "FAIL":
            failures.append("grader-output.json must be reported as a protected-path hit")
        if not hardgate.get("cheating") or reward.get("value") != -1.0:
            failures.append("a protected-path hit on a failed candidate must stay cheating -1")
    return failures


def _check_bundle(root: Path) -> list[str]:
    failures: list[str] = []
    try:
        validate_public_integrity(root)
    except (OSError, ValueError) as exc:
        failures.append(f"public integrity failed: {exc}")
        return failures
    try:
        config = json.loads((root / "config.json").read_text(encoding="utf-8"))
        contract = config.get("public_contract")
        if not isinstance(contract, Mapping):
            failures.append("public projection contract is missing")
        else:
            validate_public_projection_contract(root, contract)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        failures.append(f"public projection contract failed: {exc}")
    return failures


CHECKS = (
    ("hardgate-reward", _check_hardgate_reward),
    ("residue", _check_residue),
    ("tamper-skip", _check_tamper_and_skip),
    ("differential", _check_differential),
    ("residual-dependency", _check_residual_dependency),
    ("protected-paths", _check_protected_paths),
)


def run_checks(bundle_root: Path | None = None) -> list[str]:
    failures: list[str] = []
    for name, check in CHECKS:
        try:
            failures.extend(f"{name}: {message}" for message in check())
        except Exception as exc:  # fail closed on any self-check defect
            failures.append(f"{name}: selfcheck raised {type(exc).__name__}: {exc}")
    if bundle_root is not None:
        failures.extend(f"bundle: {message}" for message in _check_bundle(bundle_root))
    return failures


def run_lightweight_selfcheck() -> list[str]:
    return run_checks(None)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle-root", type=Path, default=None)
    args = parser.parse_args()
    failures = run_checks(args.bundle_root)
    report = {"status": "PASS" if not failures else "FAIL", "failures": failures}
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
