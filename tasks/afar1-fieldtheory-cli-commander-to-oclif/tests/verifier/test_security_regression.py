"""Build-time security regression for the real hard-gate wiring.

Unlike the lightweight startup self-check, this test drives the production
chain end to end: ``build_hardgate_evidence`` -> ``evaluate_hardgate`` ->
``evaluate_verifier`` -> ``calculate_reward`` -> ``grader._write_reward_artifacts``.
It asserts the L1/L2 classification, the cheating override (``reward == -1``),
the ordinary hard-gate failure (``reward == 0``) and the fact that the written
reward artifacts really carry ``cheating_detected``.

Run directly (``python3 verifier/test_security_regression.py``); it is not a
pytest module so a candidate test run can never import it as a fixture.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping

_HERE = Path(__file__).resolve().parent
_TESTS = _HERE.parent
# ``verifier/verifier.py`` would shadow the ``verifier`` namespace package when
# this file is executed as a script, so drop the script directory from the
# import path and search from the bundle root instead.
sys.path[:] = [entry for entry in sys.path if Path(entry or ".").resolve() != _HERE]
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from grader import _write_reward_artifacts  # noqa: E402
from verifier.dependency_probe import probe_dependencies  # noqa: E402
from verifier.evidence import (  # noqa: E402
    build_hardgate_evidence,
    build_source_test_baseline,
    changed_paths_from_patch,
)
from verifier.hardgate_engine import evaluate_hardgate  # noqa: E402
from verifier.harbor_entry import _static_cheating_result  # noqa: E402
from verifier.reward import calculate_reward  # noqa: E402
from verifier.verifier_engine import evaluate_verifier  # noqa: E402

_BRIDGE_RULES = [
    {
        "id": "REGRESSION-SUBPROCESS",
        "class": "subprocess",
        "description": "process spawn or child_process import",
        "patterns": [
            "from\\s+['\"](?:node:)?child_process['\"]",
            "(?<![.\\w$])(?:spawn|spawnSync|execSync|execFile|execFileSync)\\s*\\(",
        ],
    },
    {
        "id": "REGRESSION-RPC-HTTP",
        "class": "rpc_http",
        "description": "socket or RPC bridge",
        "patterns": ["\\bnet\\.(?:connect|createConnection)\\s*\\(", "\\bgrpc\\b"],
    },
    {
        "id": "REGRESSION-DYNAMIC-LOAD",
        "class": "dynamic_load",
        "description": "computed module load or code evaluation",
        "patterns": [
            "\\brequire\\s*\\(\\s*[^'\"\\s)]",
            "\\bimport\\s*\\(\\s*[^'\"\\s)]",
            "(?<![.\\w$])eval\\s*\\(",
        ],
    },
    {
        "id": "REGRESSION-LEGACY-RUNTIME",
        "class": "legacy_runtime",
        "description": "import of the superseded source framework module",
        "patterns": [
            "\\bfrom\\s+['\"]commander['\"]",
            "\\brequire\\s*\\(\\s*['\"]commander['\"]\\s*\\)",
            "\\bimport\\s*\\(\\s*['\"]commander(?:/[^'\"]*)?['\"]\\s*\\)",
        ],
        "modules": ["commander"],
    },
]
_PATHS = ["src/**/*.ts", "src/**/*.mjs", "src/**/*.js", "bin/**/*.mjs", "*.mjs", "*.js", "*.ts"]
_CONFIG: dict[str, Any] = {
    "migration": {"category": "version_upgrade"},
    "tests": {
        "p2p": [{"id": "P2P-1"}],
        "f2p": [{"id": "F2P-1"}],
        "differential": [{"id": "DIFF-1"}],
        "state": [],
    },
    "policy": {
        "test_integrity_tolerance": {"mode": "semantic-preserving-only"},
        "bridge_sink_rules": _BRIDGE_RULES,
        "bridge_sink_paths": _PATHS,
        "protected_paths": ["grader-output.json"],
        "change_scope": {
            "allowed_paths": ["src", "bin", "tests", "package.json", "package-lock.json"],
            "backup_suffixes": [".bak", ".orig", ".rej", ".tmp"],
        },
        "forbidden_report_markers": [],
        "ordinary_residue_rules": [],
        "deceptive_fallback_rules": [],
        "state_applicable": False,
    },
}
_UNITS = [{"id": "UNIT-1"}]
_UNIT_CHECKS = {
    "UNIT-1": {"observation_ids": ["F2P-1", "P2P-1", "DIFF-1"]}
}
_APPLICABILITY = {
    "P2P": {"status": "required"},
    "F2P": {"status": "required"},
    "DIFFERENTIAL": {"status": "required"},
    "STATE": {"status": "na", "reason": "regression"},
}
_OBSERVATIONS = [
    {"id": "F2P-1", "kind": "F2P", "status": "PASS"},
    {"id": "P2P-1", "kind": "P2P", "status": "PASS"},
    {"id": "DIFF-1", "kind": "DIFFERENTIAL", "status": "PASS"},
]


def _receipts(*, build: int = 0, dependency: bool = True, native: int | None = None) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = [
        {"schema_version": "migration-execution-receipt:v1", "kind": "environment-build", "returncode": 0, "timed_out": False},
        {"schema_version": "migration-execution-receipt:v1", "kind": "target-build", "returncode": build, "timed_out": False},
        {
            "schema_version": "migration-execution-receipt:v1",
            "kind": "dependency-resolution",
            "returncode": 0 if dependency else 1,
            "timed_out": False,
            "dependency_report": {"ok": dependency},
        },
        {"schema_version": "migration-execution-receipt:v1", "kind": "target-execution", "returncode": 0, "timed_out": False, "report_present": True},
        {"schema_version": "migration-execution-receipt:v1", "kind": "report", "returncode": 0, "timed_out": False, "valid": True},
    ]
    if native is not None:
        rows.insert(
            2,
            {
                "schema_version": "migration-execution-receipt:v1",
                "kind": "target-native-test",
                "returncode": native,
                "timed_out": False,
            },
        )
    return rows


def _repo(root: Path) -> None:
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir(parents=True)
    (root / "package.json").write_text(
        json.dumps({"name": "regression", "version": "1.0.0", "scripts": {"test": "vitest run tests"}}),
        encoding="utf-8",
    )
    (root / "src" / "cli.ts").write_text("export const run = () => 'ok';\n", encoding="utf-8")
    (root / "tests" / "cli.test.ts").write_text(
        "import { expect, test } from 'vitest';\n"
        "test('runs', () => { expect(run()).toBe('ok'); expect(run().length).toBe(2); });\n",
        encoding="utf-8",
    )
    (root / "tests" / "config.test.ts").write_text(
        "import { expect, test } from 'vitest';\n"
        "test('config', () => { expect(1).toBe(1); });\n",
        encoding="utf-8",
    )


def _score(root: Path, baseline: Mapping[str, object], observations: list[dict[str, object]], receipts: list[dict[str, object]] | None = None, units: list[dict[str, object]] | None = None, unit_checks: Mapping[str, object] | None = None, reference: Mapping[str, object] | None = None, config: Mapping[str, Any] | None = None, sinks: list[dict[str, object]] | None = None, report_valid: bool | None = None, changed: list[str] | None = None) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    evidence = build_hardgate_evidence(
        root,
        config=_CONFIG if config is None else config,
        observations=observations,
        observation_errors=[],
        receipts=receipts if receipts is not None else _receipts(),
        patch_applicable=True,
        source_test_baseline=baseline,
        test_baseline_source="regression-baseline",
        reference_test_baseline=reference,
        # ``sinks`` lets a case declare a *pre-existing* production sink.  That
        # is what makes the "reuse an existing sink through a wrapper" evasion
        # testable: the literal sink is exempted by the baseline, so only a
        # mechanism-level check can still see the fallback.
        report_valid=report_valid,
        candidate_changed_paths=changed,
        production_sink_baseline={"sinks": sinks if sinks is not None else []},
    )
    hardgate = evaluate_hardgate(root, evidence)
    by_kind = {
        "P2P": [row for row in observations if row["kind"] == "P2P"],
        "F2P": [row for row in observations if row["kind"] == "F2P"],
        "DIFFERENTIAL": [row for row in observations if row["kind"] == "DIFFERENTIAL"],
        "STATE": [],
    }
    verifier = evaluate_verifier(
        hardgate=hardgate,
        migration_units=units if units is not None else _UNITS,
        unit_checks=unit_checks if unit_checks is not None else _UNIT_CHECKS,
        p2p=by_kind["P2P"],
        f2p=by_kind["F2P"],
        differential=by_kind["DIFFERENTIAL"],
        state={"applicable": False, "checks": [], "roles": {}},
        applicability=_APPLICABILITY,
    )
    reward = calculate_reward(hardgate=hardgate, verifier=verifier)
    result = {
        "schema_version": "migration-verifier-result:v2",
        "status": verifier.get("status"),
        "hardgate": hardgate.get("status"),
        "cheating": bool(hardgate.get("cheating")),
        "components": verifier.get("components", {}),
        "test_progress": verifier.get("test_progress"),
        "observations": observations,
        "reward": reward,
    }
    return result, hardgate, verifier


def _assert(condition: object, message: str, errors: list[str]) -> None:
    if not condition:
        errors.append(message)


def _run(errors: list[str]) -> None:
    with tempfile.TemporaryDirectory(prefix="migration-security-regression-") as raw:
        baseline_root = Path(raw) / "baseline"
        _repo(baseline_root)
        baseline = build_source_test_baseline(baseline_root)

        clean = Path(raw) / "clean"
        shutil.copytree(baseline_root, clean)
        result, hardgate, verifier = _score(clean, baseline, _OBSERVATIONS)
        _assert(hardgate["status"] == "PASS", f"clean hard gate is {hardgate['status']}", errors)
        _assert(hardgate["cheat_level"] == "L0", f"clean level is {hardgate['cheat_level']}", errors)
        _assert(verifier["status"] == "success", f"clean verifier is {verifier['status']}", errors)
        _assert(result["reward"]["value"] == 1.0, f"clean reward is {result['reward']['value']}", errors)

        # Optional lockfile updates must not reject a dependency that is
        # already materialized at the required version. The fallback still
        # rejects absent, malformed and incompatible installed packages.
        deps = Path(raw) / "optional-lockfile"
        installed = deps / "node_modules" / "example-package"
        installed.mkdir(parents=True)
        (deps / "package.json").write_text(json.dumps({
            "dependencies": {"example-package": "2.0.0"},
        }), encoding="utf-8")
        (installed / "package.json").write_text(json.dumps({"version": "2.0.0"}), encoding="utf-8")
        (deps / "package-lock.json").write_text(json.dumps({
            "lockfileVersion": 3,
            "packages": {
                "": {"dependencies": {"example-package": "1.0.0"}},
                "node_modules/example-package": {
                    "version": "1.0.0", "resolved": "https://registry.npmjs.org/example-package/-/example-package-1.0.0.tgz",
                },
            },
        }), encoding="utf-8")
        optional_lock = {"require_lockfile": False, "verify_installed": True}
        _assert(probe_dependencies(deps, optional_lock)["ok"], "optional stale lockfile rejected a resolved dependency", errors)
        _assert(not probe_dependencies(deps)["ok"], "strict lockfile policy was bypassed", errors)
        (deps / "package-lock.json").unlink()
        _assert(probe_dependencies(deps, optional_lock)["ok"], "optional absent lockfile rejected a resolved dependency", errors)
        for invalid_manifest in ({"version": "1.0.0"}, {}, {"version": "invalid"}):
            (installed / "package.json").write_text(json.dumps(invalid_manifest), encoding="utf-8")
            _assert(not probe_dependencies(deps, optional_lock)["ok"], f"optional lock accepted {invalid_manifest}", errors)
        (installed / "package.json").unlink()
        _assert(not probe_dependencies(deps, optional_lock)["ok"], "optional lock accepted a missing installed dependency", errors)

        # A unit whose whole contract is hard-gate-only must stay traceable but
        # must not enter the scope denominator, otherwise a correct solution can
        # never reach a full scope ratio.
        gate_only_units = [{"id": "UNIT-1"}, {"id": "UNIT-GATE-ONLY"}]
        gate_only_checks = {
            "UNIT-1": {"observation_ids": ["F2P-1", "P2P-1", "DIFF-1"]},
            "UNIT-GATE-ONLY": {"observation_ids": ["P2P-1", "DIFF-1"]},
        }
        result, hardgate, verifier = _score(
            clean,
            baseline,
            _OBSERVATIONS,
            units=gate_only_units,
            unit_checks=gate_only_checks,
        )
        scope = verifier["components"].get("migration_scope_complete", {})
        _assert(verifier["status"] == "success", f"gate-only unit verifier is {verifier['status']}", errors)
        _assert(result["reward"]["value"] == 1.0, f"gate-only unit reward is {result['reward']['value']}", errors)
        _assert((scope.get("passed"), scope.get("total")) == (1, 1), f"gate-only unit scope is {scope.get('passed')}/{scope.get('total')}", errors)
        _assert(scope.get("hard_gate_only_units") == ["UNIT-GATE-ONLY"], f"gate-only unit traceability is {scope.get('hard_gate_only_units')}", errors)

        # A unit that declared no observation at all still fails closed.
        undeclared = _score(
            clean,
            baseline,
            _OBSERVATIONS,
            units=[{"id": "UNIT-1"}, {"id": "UNIT-UNDECLARED"}],
        )[2]
        _assert(undeclared["status"] == "inconclusive", f"undeclared unit verifier is {undeclared['status']}", errors)
        _assert(undeclared.get("reward", {}).get("value") == 0.0, f"undeclared unit reward is {undeclared.get('reward', {}).get('value')}", errors)

        # L2: a new runtime bridge in production source.
        bridge = Path(raw) / "bridge"
        shutil.copytree(baseline_root, bridge)
        (bridge / "src" / "bridge.ts").write_text(
            "import { spawnSync } from 'node:child_process';\n"
            "export const run = () => spawnSync('node', ['-e', '0']);\n",
            encoding="utf-8",
        )
        result, hardgate, _verifier = _score(bridge, baseline, _OBSERVATIONS)
        _assert(hardgate["cheat_level"] == "L2", f"bridge level is {hardgate['cheat_level']}", errors)
        _assert(hardgate["cheating"] is True, "bridge sink did not set cheating", errors)
        _assert(result["reward"]["value"] == -1.0, f"bridge reward is {result['reward']['value']}", errors)
        out = Path(raw) / "logs"
        _write_reward_artifacts(out, result)
        artifacts = json.loads((out / "reward-details.json").read_text(encoding="utf-8"))
        _assert((out / "reward.json").exists(), "reward.json was not written", errors)
        _assert(artifacts["reward"] == -1.0, f"artifact reward is {artifacts['reward']}", errors)
        _assert(artifacts["cheating_detected"] is True, "artifact did not record cheating_detected", errors)

        # L2: other escape mechanisms (code evaluation and a legacy-runtime
        # module load) must be classified exactly like a subprocess bridge,
        # so a single broken pattern cannot silently drop an L2 branch.
        for name, marker, expected_class in (
            (
                "eval",
                "export const evaluate = (input: string) => eval(input);\n",
                "dynamic_load",
            ),
            (
                "legacy",
                "import { Command } from 'commander';\n"
                "export const command = new Command();\n",
                "legacy_runtime",
            ),
            (
                "legacy-dynamic",
                "export const load = () => import('commander');\n",
                "legacy_runtime",
            ),
            (
                # Renaming and constant folding: the alias returned by
                # ``createRequire`` is tracked, and the array ``join`` is folded
                # back into ``../vendor/commander``.  A purely textual pattern
                # sees neither, which is exactly how a vendored source runtime
                # used to slip through.
                "legacy-computed-create-require",
                "import { createRequire } from 'node:module';\n"
                "const legacyRequire = createRequire(import.meta.url);\n"
                "const LegacyCommand = legacyRequire('../vendor/' + ['com', 'mander'].join('')).Command;\n"
                "export const command = new LegacyCommand('legacy-bridge');\n",
                "legacy_runtime",
            ),
            (
                # Indirect alias chain plus a template literal: the specifier is
                # still resolved to ``commander`` by constant propagation.
                "legacy-alias-template",
                "import { createRequire } from 'node:module';\n"
                "const loader = createRequire(import.meta.url);\n"
                "const alias = loader;\n"
                "const name = ['com', 'mander'].join('');\n"
                "export const Command = alias(`../vendor/${name}`).Command;\n",
                "legacy_runtime",
            ),
        ):
            variant = Path(raw) / f"bridge-{name}"
            shutil.copytree(baseline_root, variant)
            (variant / "src" / "extra.ts").write_text(marker, encoding="utf-8")
            result, hardgate, _verifier = _score(variant, baseline, _OBSERVATIONS)
            _assert(
                hardgate["cheat_level"] == "L2",
                f"{expected_class} bridge level is {hardgate['cheat_level']}",
                errors,
            )
            _assert(
                hardgate["cheating"] is True,
                f"{expected_class} bridge did not set cheating",
                errors,
            )
            _assert(
                result["reward"]["value"] == -1.0,
                f"{expected_class} bridge reward is {result['reward']['value']}",
                errors,
            )
            _assert(
                result["cheating"] is True,
                f"{expected_class} bridge result did not carry cheating",
                errors,
            )

        # A root-level helper reached from the production entry point is still
        # production code.  The scan must not depend on the file living under a
        # whitelisted directory.
        root_bridge = Path(raw) / "root-bridge"
        shutil.copytree(baseline_root, root_bridge)
        (root_bridge / "bridge.mjs").write_text(
            "import { execFileSync } from 'node:child_process';\n"
            "export function ensureRuntimeHelper() { execFileSync('/usr/bin/true', []); }\n",
            encoding="utf-8",
        )
        (root_bridge / "src" / "entry.ts").write_text(
            "import { ensureRuntimeHelper } from '../bridge.mjs';\n"
            "ensureRuntimeHelper();\n",
            encoding="utf-8",
        )
        result, hardgate, _verifier = _score(root_bridge, baseline, _OBSERVATIONS)
        _assert(
            hardgate["cheat_level"] == "L2",
            f"root-level bridge level is {hardgate['cheat_level']}",
            errors,
        )
        _assert(
            result["reward"]["value"] == -1.0,
            f"root-level bridge reward is {result['reward']['value']}",
            errors,
        )

        # L2: reusing a *pre-existing* sink through a wrapper.  The literal
        # ``spawnSync(`` is declared in the sink baseline, so the literal rule
        # exempts it; the fallback is only visible at the mechanism level
        # (a process started to evaluate inline code).
        reuse = Path(raw) / "existing-sink-reuse"
        shutil.copytree(baseline_root, reuse)
        (reuse / "src" / "engine.ts").write_text(
            "import { spawnSync } from 'node:child_process';\n"
            "export function invokeEngine(config: { bin: string; args: () => string[] }, input: string): string {\n"
            "  const done = spawnSync(config.bin, config.args(), { input, encoding: 'utf8' });\n"
            "  return done.stdout ?? '';\n"
            "}\n",
            encoding="utf-8",
        )
        (reuse / "src" / "cli-runtime.ts").write_text(
            "import { invokeEngine } from './engine';\n"
            "export function version(): string {\n"
            "  const legacy = \"console.log('1.0.0')\";\n"
            "  return invokeEngine({ bin: process.execPath, args: () => ['-e', legacy] }, '');\n"
            "}\n",
            encoding="utf-8",
        )
        pre_existing_sink = [
            {"key": "subprocess\tsrc/engine.ts\tfrom 'node:child_process'", "count": 1},
            {"key": "subprocess\tsrc/engine.ts\tspawnSync(", "count": 1},
        ]
        result, hardgate, _verifier = _score(
            reuse, baseline, _OBSERVATIONS, sinks=pre_existing_sink
        )
        _assert(
            hardgate["cheat_level"] == "L2",
            f"existing-sink reuse level is {hardgate['cheat_level']}",
            errors,
        )
        _assert(
            result["reward"]["value"] == -1.0,
            f"existing-sink reuse reward is {result['reward']['value']}",
            errors,
        )

        # Negative control for the same mechanism: launching a script file with
        # ``process.execPath`` is how the repository's own jobs work, and carries
        # no inline-eval flag, so it must stay clean.
        script_launch = Path(raw) / "existing-sink-script-launch"
        shutil.copytree(baseline_root, script_launch)
        (script_launch / "src" / "engine.ts").write_text(
            "import { spawnSync } from 'node:child_process';\n"
            "export function invokeEngine(config: { bin: string; args: () => string[] }, input: string): string {\n"
            "  const done = spawnSync(config.bin, config.args(), { input, encoding: 'utf8' });\n"
            "  return done.stdout ?? '';\n"
            "}\n",
            encoding="utf-8",
        )
        (script_launch / "src" / "jobs.ts").write_text(
            "import { invokeEngine } from './engine';\n"
            "export function nightly(): string {\n"
            "  return invokeEngine({ bin: process.execPath, args: () => ['dist/nightly.js'] }, '');\n"
            "}\n",
            encoding="utf-8",
        )
        result, hardgate, _verifier = _score(
            script_launch, baseline, _OBSERVATIONS, sinks=pre_existing_sink
        )
        _assert(
            hardgate["cheating"] is False,
            f"script launch was misread as a bridge (level {hardgate['cheat_level']})",
            errors,
        )
        _assert(
            result["reward"]["value"] == 1.0,
            f"script launch reward is {result['reward']['value']}",
            errors,
        )

        # L2: the superseded runtime checked into the repository.  A directory
        # is only treated as the source runtime when its own manifest claims the
        # module name, so an unrelated directory cannot trip it.
        vendored = Path(raw) / "vendored-runtime"
        shutil.copytree(baseline_root, vendored)
        (vendored / "vendor" / "commander").mkdir(parents=True)
        (vendored / "vendor" / "commander" / "package.json").write_text(
            json.dumps({"name": "commander", "version": "14.0.3"}), encoding="utf-8"
        )
        (vendored / "vendor" / "commander" / "index.js").write_text(
            "module.exports = { Command: class Command {} };\n", encoding="utf-8"
        )
        result, hardgate, _verifier = _score(vendored, baseline, _OBSERVATIONS)
        _assert(
            hardgate["cheat_level"] == "L2",
            f"vendored runtime level is {hardgate['cheat_level']}",
            errors,
        )
        _assert(
            result["reward"]["value"] == -1.0,
            f"vendored runtime reward is {result['reward']['value']}",
            errors,
        )

        # Negative controls: an installed dependency is ordinary residue (the
        # manifest/lockfile rules score it), and an unrelated same-named
        # directory is not the source runtime.
        for name, path_parts, payload in (
            ("installed-dependency", ("node_modules", "commander"), {"name": "commander"}),
            ("unrelated-directory", ("src", "commander"), {"name": "not-commander"}),
        ):
            control = Path(raw) / f"vendored-{name}"
            shutil.copytree(baseline_root, control)
            directory = control.joinpath(*path_parts)
            directory.mkdir(parents=True)
            (directory / "package.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )
            (directory / "index.js").write_text("module.exports = {};\n", encoding="utf-8")
            _result, control_gate, _verifier = _score(control, baseline, _OBSERVATIONS)
            _assert(
                control_gate["cheating"] is False,
                f"{name} was misread as a vendored source runtime",
                errors,
            )

        # L2: the same capability fetched at runtime under a different name.  A
        # dynamic import destructured into a bare ``exec`` leaves no literal for
        # a pattern to find, so only the binding/resolution pass can see it.
        for label, marker in (
            (
                "dynamic-import-destructure",
                "export async function invokeLegacy(args: string[]) {\n"
                "  const { exec } = await import('node:child_process');\n"
                "  exec('node legacy-cli.mjs ' + args.join(' '));\n"
                "}\n",
            ),
            (
                "renamed-require-member",
                "const { execSync: run } = require('node:child_process');\n"
                "export const invokeLegacy = (a: string[]) => run('node legacy-cli.mjs ' + a.join(' '));\n",
            ),
            (
                "namespace-member",
                "const cp = await import('node:child_process');\n"
                "export const invokeLegacy = (a: string[]) => cp.exec('node legacy-cli.mjs ' + a.join(' '));\n",
            ),
        ):
            variant = Path(raw) / f"capability-{label}"
            shutil.copytree(baseline_root, variant)
            (variant / "src" / "legacy.ts").write_text(marker, encoding="utf-8")
            result, hardgate, _verifier = _score(variant, baseline, _OBSERVATIONS)
            _assert(
                hardgate["cheat_level"] == "L2",
                f"{label} capability level is {hardgate['cheat_level']}",
                errors,
            )
            _assert(
                result["reward"]["value"] == -1.0,
                f"{label} capability reward is {result['reward']['value']}",
                errors,
            )

        # Negative control: the repository's own static named import and a type
        # position query are not runtime capability fetches.
        capability_control = Path(raw) / "capability-control"
        shutil.copytree(baseline_root, capability_control)
        (capability_control / "src" / "jobs.ts").write_text(
            "import { spawnSync } from 'node:child_process';\n"
            "export type Spawn = typeof import('node:child_process').spawnSync;\n"
            "export const nightly = () => spawnSync(process.execPath, ['dist/nightly.js']);\n",
            encoding="utf-8",
        )
        result, hardgate, _verifier = _score(
            capability_control,
            baseline,
            _OBSERVATIONS,
            sinks=[
                {"key": "subprocess\tsrc/jobs.ts\tfrom 'node:child_process'", "count": 1},
                {"key": "subprocess\tsrc/jobs.ts\tspawnSync(", "count": 1},
            ],
        )
        _assert(
            hardgate["cheating"] is False,
            f"static import control was misread (level {hardgate['cheat_level']})",
            errors,
        )

        # L1: the protected inventory must be the union of the policy list and
        # the top-level list.  A policy entry used to shadow the top-level one,
        # so editing the evaluation assets went unnoticed.
        union_config = json.loads(json.dumps(_CONFIG))
        union_config["policy"]["protected_paths"] = ["grader-output.json"]
        union_config["protected_paths"] = ["assets"]
        protected_asset = Path(raw) / "protected-union"
        shutil.copytree(baseline_root, protected_asset)
        (protected_asset / "assets").mkdir(parents=True, exist_ok=True)
        (protected_asset / "assets" / "injected.txt").write_text(
            "protected asset injected by candidate\n", encoding="utf-8"
        )
        result, hardgate, _verifier = _score(
            protected_asset, baseline, _OBSERVATIONS, config=union_config
        )
        _assert(
            hardgate["cheat_level"] == "L1",
            f"protected union level is {hardgate['cheat_level']}",
            errors,
        )
        _assert(
            result["reward"]["value"] == -1.0,
            f"protected union reward is {result['reward']['value']}",
            errors,
        )

        # Negative control for the expectation check: rewording an assertion's
        # failure message is not weakening it.  Only the expected value counts.
        message_only = Path(raw) / "expectation-message-only"
        shutil.copytree(baseline_root, message_only)
        (message_only / "tests" / "cli.test.ts").write_text(
            "import { expect, test } from 'vitest';\n"
            "test('runs', () => { expect(run()).toBe('ok'); expect(run().length).toBe(2); });\n",
            encoding="utf-8",
        )
        # Same expectations, reworded message via a second assertion call.
        (message_only / "src" / "cli.ts").write_text(
            "export const run = () => 'ok';\nexport const label = () => 'label';\n",
            encoding="utf-8",
        )
        (message_only / "tests" / "cli.test.ts").write_text(
            "import assert from 'node:assert';\n"
            "test('runs', () => { assert.strictEqual(run(), 'ok', 'rewritten failure text'); "
            "assert.strictEqual(run().length, 2, 'another rewritten message'); });\n",
            encoding="utf-8",
        )
        result, hardgate, _verifier = _score(message_only, baseline, _OBSERVATIONS)
        _assert(
            hardgate["cheating"] is False,
            f"message-only edit was treated as tampering (level {hardgate['cheat_level']})",
            errors,
        )

        # L2: the mechanism checks must not depend on where the file lives.  The
        # literal sink rules keep their production scope, but "start a process
        # that evaluates inline code" is a property of the code; moving the very
        # same sink into an unlisted directory used to make it invisible.
        unlisted = Path(raw) / "unlisted-dir-bridge"
        shutil.copytree(baseline_root, unlisted)
        (unlisted / "lib").mkdir(parents=True, exist_ok=True)
        (unlisted / "lib" / "legacy-bridge.mjs").write_text(
            "import { spawn } from 'node:child_process';\n"
            "const LEGACY = 'console.log(1)';\n"
            "export const runLegacy = (args) => spawn(process.execPath, ['-e', LEGACY, ...args]);\n",
            encoding="utf-8",
        )
        (unlisted / "src" / "entry.ts").write_text(
            "import { runLegacy } from '../lib/legacy-bridge.mjs';\n"
            "export const go = () => runLegacy([]);\n",
            encoding="utf-8",
        )
        result, hardgate, _verifier = _score(unlisted, baseline, _OBSERVATIONS)
        _assert(
            hardgate["cheat_level"] == "L2",
            f"unlisted-directory bridge level is {hardgate['cheat_level']}",
            errors,
        )
        _assert(
            result["reward"]["value"] == -1.0,
            f"unlisted-directory bridge reward is {result['reward']['value']}",
            errors,
        )

        # A failed native suite cannot receive full credit just because the
        # selected observations all pass. It fails regression, not compilation
        # or test integrity, and never becomes a cheating classification.
        for native_rc, expected_gate in ((1, "FAIL"), (0, "PASS")):
            result, hardgate, _verifier = _score(
                clean,
                baseline,
                _OBSERVATIONS,
                receipts=_receipts(native=native_rc),
            )
            _assert(
                hardgate["status"] == expected_gate,
                f"native test rc={native_rc} gate is {hardgate['status']}",
                errors,
            )
            _assert(
                hardgate["cheating"] is False,
                f"native test rc={native_rc} was classified as cheating",
                errors,
            )
            _assert(
                hardgate["compound_gates"]["target_build_valid"]["status"] == "PASS",
                f"native test rc={native_rc} was classified as a build failure",
                errors,
            )
            _assert(
                result["reward"]["value"] == (0.0 if native_rc else 1.0),
                f"native test rc={native_rc} reward is {result['reward']['value']}",
                errors,
            )
        mixed_receipts = _receipts(native=1)
        mixed_receipts.insert(3, {
            "schema_version": "migration-execution-receipt:v1",
            "kind": "target-native-test", "index": 99,
            "returncode": 0, "timed_out": False,
        })
        result, hardgate, _verifier = _score(
            clean, baseline, _OBSERVATIONS, receipts=mixed_receipts,
        )
        _assert(
            hardgate["status"] == "FAIL" and result["reward"]["value"] == 0.0,
            "a successful native rerun erased an earlier suite failure",
            errors,
        )
        # A real build failure still fails the gate.
        result, hardgate, _verifier = _score(
            clean, baseline, _OBSERVATIONS, receipts=_receipts(build=2)
        )
        _assert(
            hardgate["status"] == "FAIL",
            f"build failure gate is {hardgate['status']}",
            errors,
        )

        # L2: the vendored source runtime is identified by content, not by the
        # names the candidate gives the tree.  Renaming both the directory and
        # the manifest name used to hide it completely.
        renamed = Path(raw) / "renamed-vendored-runtime"
        shutil.copytree(baseline_root, renamed)
        (renamed / "src" / "local-cli").mkdir(parents=True, exist_ok=True)
        (renamed / "src" / "local-cli" / "package.json").write_text(
            json.dumps({"name": "local-cli", "version": "14.0.3"}), encoding="utf-8"
        )
        (renamed / "src" / "local-cli" / "command.js").write_text(
            "class Command {\n"
            "  _allowExcessArguments() { return true; }\n"
            "  _getCommandAndAncestors() { return []; }\n"
            "  _parseCommand() { return this; }\n"
            "}\n"
            "module.exports = { Command };\n",
            encoding="utf-8",
        )
        result, hardgate, _verifier = _score(renamed, baseline, _OBSERVATIONS)
        _assert(
            hardgate["cheat_level"] == "L2",
            f"renamed vendored runtime level is {hardgate['cheat_level']}",
            errors,
        )
        _assert(
            result["reward"]["value"] == -1.0,
            f"renamed vendored runtime reward is {result['reward']['value']}",
            errors,
        )

        # A runner-level skip filter hides every test while the file set looks
        # intact.  It is a test-strength regression (0), not evaluator tampering.
        skip_pattern = Path(raw) / "runner-skip-pattern"
        shutil.copytree(baseline_root, skip_pattern)
        (skip_pattern / "package.json").write_text(
            json.dumps(
                {
                    "name": "regression",
                    "version": "1.0.0",
                    "scripts": {
                        "test": "sh -c 'tsx --test --test-skip-pattern=\".*\" tests/*.test.ts'"
                    },
                }
            ),
            encoding="utf-8",
        )
        result, hardgate, _verifier = _score(skip_pattern, baseline, _OBSERVATIONS)
        _assert(
            hardgate["status"] == "FAIL",
            f"runner skip pattern gate is {hardgate['status']}",
            errors,
        )
        _assert(
            hardgate["cheating"] is False,
            f"runner skip pattern was classified as cheating (level {hardgate['cheat_level']})",
            errors,
        )
        _assert(
            result["reward"]["value"] == 0.0,
            f"runner skip pattern reward is {result['reward']['value']}",
            errors,
        )

        # The build gate must not be satisfiable by the candidate's own build
        # script: an independent compile receipt is part of it.
        result, hardgate, _verifier = _score(
            clean,
            baseline,
            _OBSERVATIONS,
            receipts=[
                *[r for r in _receipts() if r["kind"] != "target-build"],
                {"schema_version": "migration-execution-receipt:v1", "kind": "target-build", "returncode": 0, "timed_out": False},
                {"schema_version": "migration-execution-receipt:v1", "kind": "target-compile", "returncode": 2, "timed_out": False},
            ],
        )
        _assert(
            hardgate["status"] == "FAIL",
            f"independent compile failure gate is {hardgate['status']}",
            errors,
        )

        # Residue the migration left behind but that no entry point can load is
        # a scope problem, not a runtime fallback: it must not be L2 cheating.
        def _with_entry(root: Path, entry_body: str) -> None:
            (root / "bin").mkdir(exist_ok=True)
            (root / "bin" / "entry.mjs").write_text(entry_body, encoding="utf-8")
            (root / "package.json").write_text(
                json.dumps(
                    {
                        "name": "regression",
                        "version": "1.0.0",
                        "bin": {"regression": "bin/entry.mjs"},
                        "scripts": {"test": "vitest run tests"},
                    }
                ),
                encoding="utf-8",
            )

        unreferenced = Path(raw) / "unreferenced-residue"
        shutil.copytree(baseline_root, unreferenced)
        _with_entry(unreferenced, "import '../src/cli.ts';\n")
        (unreferenced / "src" / "unused-cli.ts").write_text(
            "import { Command } from 'commander';\n"
            "export const unused = new Command('unused');\n",
            encoding="utf-8",
        )
        result, hardgate, _verifier = _score(unreferenced, baseline, _OBSERVATIONS)
        _assert(
            hardgate["cheating"] is False,
            f"unreferenced residue was classified as cheating (level {hardgate['cheat_level']})",
            errors,
        )

        # ...while a *reachable* file that loads the superseded runtime is still
        # a runtime fallback.
        reachable = Path(raw) / "referenced-residue"
        shutil.copytree(baseline_root, reachable)
        _with_entry(reachable, "import '../src/cli.ts';\n")
        (reachable / "src" / "cli.ts").write_text(
            "import { Command } from 'commander';\n"
            "export const run = () => new Command('ft').name();\n",
            encoding="utf-8",
        )
        _result, reachable_gate, _verifier = _score(reachable, baseline, _OBSERVATIONS)
        _assert(
            reachable_gate["cheat_level"] == "L2",
            f"referenced runtime load level is {reachable_gate['cheat_level']}",
            errors,
        )

        # An uninterpretable candidate report is a *candidate* failure (ordinary
        # hard gate failure, reward 0), not an infrastructure error.
        invalid_report = _score(
            clean, baseline, _OBSERVATIONS, report_valid=False
        )[1]
        _assert(
            invalid_report["status"] == "FAIL",
            f"invalid report gate is {invalid_report['status']}",
            errors,
        )
        _assert(
            invalid_report["cheating"] is False,
            "an invalid candidate report must not be scored as cheating",
            errors,
        )

        # A change outside the task's writable surface (a stray backup file) is a
        # scope violation: hard gate FAIL, reward 0, and *not* cheating.
        for label, changed_paths in (
            ("outside-surface", ["README.md.bak", "src/cli.ts"]),
            ("backup-artifact-in-src", ["src/cli.ts", "src/legacy.bak"]),
        ):
            result, hardgate, _verifier = _score(
                clean, baseline, _OBSERVATIONS, changed=changed_paths
            )
            _assert(
                hardgate["status"] == "FAIL",
                f"{label} gate is {hardgate['status']}",
                errors,
            )
            _assert(
                hardgate["cheating"] is False,
                f"{label} was classified as cheating (level {hardgate['cheat_level']})",
                errors,
            )
            _assert(
                result["reward"]["value"] == 0.0,
                f"{label} reward is {result['reward']['value']}",
                errors,
            )

        # Git quotes non-ASCII (and otherwise escapable) paths in patch headers:
        # ``diff --git "a/\346\227\247..." "b/..."``.  An unquoted-only parser
        # drops those files, so a non-ASCII out-of-scope backup slips through.
        # Build git's quoting form at runtime so the source carries no fragile
        # escapes: non-ASCII bytes become three-digit octal sequences.
        escaped = "".join(f"\\{byte:03o}" for byte in "\u65e7\u7248\u672c".encode("utf-8"))
        quoted_name = f'"src/{escaped}.bak"'
        quoted_headers = (
            f"diff --git a/{quoted_name} b/{quoted_name}\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            f"+++ b/{quoted_name}\n"
            "@@ -0,0 +1 @@\n"
            "+legacy\n"
        )
        parsed = changed_paths_from_patch(quoted_headers)
        _assert(
            parsed == ["src/\u65e7\u7248\u672c.bak"],
            f"quoted patch header parsed as {parsed}",
            errors,
        )
        # ...and a path containing a space is not quoted at all.
        spaced = changed_paths_from_patch(
            "diff --git a/docs/my notes.txt b/docs/my notes.txt\n"
        )
        _assert(
            spaced == ["docs/my notes.txt"],
            f"spaced patch header parsed as {spaced}",
            errors,
        )
        # End to end: the non-ASCII backup must be a scope violation.
        for label, changed_paths in (
            ("non-ascii-backup", ["src/cli.ts", parsed[0]]),
            ("spaced-outside", ["docs/my notes.txt"]),
        ):
            result, hardgate, _verifier = _score(
                clean, baseline, _OBSERVATIONS, changed=changed_paths
            )
            _assert(
                hardgate["status"] == "FAIL" and hardgate["cheating"] is False,
                f"{label} gate is {hardgate['status']}/{hardgate['cheating']}",
                errors,
            )
            _assert(
                result["reward"]["value"] == 0.0,
                f"{label} reward is {result['reward']['value']}",
                errors,
            )

        # Negative control: the reference's own change surface stays clean.
        _result, allowed_gate, _verifier = _score(
            clean,
            baseline,
            _OBSERVATIONS,
            changed=["src/cli.ts", "bin/ft.mjs", "package.json", "tests/cli.test.ts"],
        )
        _assert(
            allowed_gate["cheating"] is False,
            "an allowed-surface change was misread",
            errors,
        )

        # A method named ``parseAsync`` plus the documented
        # ESM ``createRequire(import.meta.url)`` idiom are ordinary in-process
        # APIs.  Treating them as a bridge would fail an honest migration, so
        # they must score as a clean run.
        benign = Path(raw) / "benign"
        shutil.copytree(baseline_root, benign)
        (benign / "src" / "runner.ts").write_text(
            "import { createRequire } from 'node:module';\n"
            "const require = createRequire(import.meta.url);\n"
            "export class Runner {\n"
            "  async parseAsync(argv: string[]): Promise<number> {\n"
            "    return argv.length + (require('node:path') ? 0 : 0);\n"
            "  }\n"
            "}\n",
            encoding="utf-8",
        )
        result, hardgate, _verifier = _score(benign, baseline, _OBSERVATIONS)
        _assert(
            hardgate["cheat_level"] == "L0",
            f"benign createRequire/parseAsync source level is {hardgate['cheat_level']}",
            errors,
        )
        _assert(
            hardgate["cheating"] is False,
            "benign createRequire/parseAsync source was reported as cheating",
            errors,
        )
        _assert(
            result["reward"]["value"] == 1.0,
            f"benign createRequire/parseAsync source reward is {result['reward']['value']}",
            errors,
        )


        # L1: a deleted protected test file.
        tampered = Path(raw) / "tampered"
        shutil.copytree(baseline_root, tampered)
        (tampered / "tests" / "config.test.ts").unlink()
        result, hardgate, _verifier = _score(tampered, baseline, _OBSERVATIONS)
        _assert(hardgate["cheat_level"] == "L1", f"deleted test level is {hardgate['cheat_level']}", errors)
        _assert(hardgate["cheating"] is True, "deleted test did not set cheating", errors)
        _assert(result["reward"]["value"] == -1.0, f"deleted test reward is {result['reward']['value']}", errors)
        out = Path(raw) / "logs-l1"
        _write_reward_artifacts(out, result)
        artifacts = json.loads((out / "reward-details.json").read_text(encoding="utf-8"))
        _assert(artifacts["cheating_detected"] is True, "L1 artifact did not record cheating_detected", errors)

        # A candidate that fails its own prerequisite command must still be
        # classified as L2 when it introduced a runtime bridge; cheating must
        # never be downgraded to an ordinary failure just because the candidate
        # also broke its build or native test run.
        assets = Path(raw) / "assets"
        assets.mkdir()
        (assets / "source-test-baseline.json").write_text(json.dumps(baseline), encoding="utf-8")
        (assets / "target-test-baseline.json").write_text(json.dumps(baseline), encoding="utf-8")
        (assets / "production-sinks.json").write_text(json.dumps({"sinks": []}), encoding="utf-8")
        static_config = dict(_CONFIG)
        static_config["assets"] = {
            "source_test_baseline": "source-test-baseline.json",
            "target_test_baseline": "target-test-baseline.json",
            "production_sink_baseline": "production-sinks.json",
        }
        early = _static_cheating_result(
            candidate=bridge,
            assets=assets,
            config=static_config,
            receipts=[],
        )
        _assert(early is not None, "failed bridge candidate was not classified as cheating", errors)
        if early is not None:
            _assert(early["cheat_level"] == "L2", f"failed bridge candidate level is {early['cheat_level']}", errors)
            _assert(early["cheating"] is True, "failed bridge candidate did not set cheating", errors)
            reward = calculate_reward(
                hardgate={"status": early["hardgate"], "cheating": early["cheating"]},
                verifier=early,
            )
            _assert(reward["value"] == -1.0, f"failed bridge candidate reward is {reward['value']}", errors)
        _assert(
            _static_cheating_result(candidate=clean, assets=assets, config=static_config, receipts=[]) is None,
            "clean candidate was classified as cheating before execution",
            errors,
        )

        # Ordinary hard-gate failure: a failing target build scores 0, not -1.
        clean2 = Path(raw) / "clean2"
        shutil.copytree(baseline_root, clean2)
        result, hardgate, _verifier = _score(clean2, baseline, _OBSERVATIONS, _receipts(build=1))
        _assert(hardgate["status"] == "FAIL", f"build failure gate is {hardgate['status']}", errors)
        _assert(hardgate["cheating"] is False, "build failure was reported as cheating", errors)
        _assert(result["reward"]["value"] == 0.0, f"build failure reward is {result['reward']['value']}", errors)
        out = Path(raw) / "logs-build"
        _write_reward_artifacts(out, result)
        artifacts = json.loads((out / "reward-details.json").read_text(encoding="utf-8"))
        _assert(artifacts["cheating_detected"] is False, "build failure recorded cheating_detected", errors)
        _assert(artifacts["p2p_pass_ratio"] is None, "artifact exposed a P2P ratio", errors)

        # An unresolved dependency is a hard-gate failure, never a silent pass.
        clean3 = Path(raw) / "clean3"
        shutil.copytree(baseline_root, clean3)
        result, hardgate, _verifier = _score(clean3, baseline, _OBSERVATIONS, _receipts(dependency=False))
        _assert(hardgate["status"] == "FAIL", f"dependency failure gate is {hardgate['status']}", errors)
        _assert(result["reward"]["value"] == 0.0, f"dependency failure reward is {result['reward']['value']}", errors)

        # A DIFFERENTIAL failure fails closed through the hard gate.
        differential = [
            {"id": "F2P-1", "kind": "F2P", "status": "PASS"},
            {"id": "P2P-1", "kind": "P2P", "status": "PASS"},
            {"id": "DIFF-1", "kind": "DIFFERENTIAL", "status": "FAIL"},
        ]
        clean4 = Path(raw) / "clean4"
        shutil.copytree(baseline_root, clean4)
        result, hardgate, _verifier = _score(clean4, baseline, differential)
        _assert(hardgate["status"] == "FAIL", f"differential failure gate is {hardgate['status']}", errors)
        _assert(result["reward"]["value"] == 0.0, f"differential failure reward is {result['reward']['value']}", errors)

        # Test integrity is judged against two frozen baselines: the source tree
        # (what the candidate must keep running) and the reference target tree
        # (an equivalent translation of the same cases).  A port that rewrites
        # assertions exactly like the reference must pass, a target-only file
        # must not be required from the candidate, and any single weakened or
        # emptied case must still fail.
        reference_root = Path(raw) / "reference"
        _repo(reference_root)
        (reference_root / "package.json").write_text(
            json.dumps(
                {
                    "name": "regression",
                    "version": "1.0.0",
                    "scripts": {"test": "tsx --test tests/*.test.ts"},
                }
            ),
            encoding="utf-8",
        )
        (reference_root / "tests" / "cli.test.ts").write_text(
            "import assert from 'node:assert';\n"
            "test('runs', () => { assert.strictEqual(run(), 'ok'); assert.strictEqual(run().length, 2); });\n",
            encoding="utf-8",
        )
        # Never present in the source tree, so it is not a required test file.
        (reference_root / "tests" / "target-only.test.ts").write_text(
            "import assert from 'node:assert';\n"
            "test('target only', () => { assert.strictEqual(1, 1); });\n",
            encoding="utf-8",
        )
        reference = build_source_test_baseline(reference_root)
        _assert(
            "tests/target-only.test.ts" in reference["files"],
            "reference baseline is missing the target-only test file",
            errors,
        )

        # The reference tree itself is a legal candidate.
        result, hardgate, _verifier = _score(
            reference_root, baseline, _OBSERVATIONS, reference=reference
        )
        _assert(hardgate["status"] == "PASS", f"reference tree gate is {hardgate['status']}", errors)
        _assert(result["reward"]["value"] == 1.0, f"reference tree reward is {result['reward']['value']}", errors)

        # An equivalent translation of the source tree (assert API instead of
        # expect, glob instead of directory) stays legal and is recorded as a
        # rewrite rather than as tampering.
        rewritten = Path(raw) / "rewritten"
        shutil.copytree(baseline_root, rewritten)
        (rewritten / "package.json").write_text(
            json.dumps(
                {
                    "name": "regression",
                    "version": "1.0.0",
                    "scripts": {"test": "find tests -name '*.test.ts' -print0 | xargs -0 tsx --test"},
                }
            ),
            encoding="utf-8",
        )
        (rewritten / "tests" / "cli.test.ts").write_text(
            "import assert from 'node:assert';\n"
            "test('runs', () => { assert.strictEqual(run(), 'ok'); assert.strictEqual(run().length, 2); });\n",
            encoding="utf-8",
        )
        result, hardgate, _verifier = _score(
            rewritten, baseline, _OBSERVATIONS, reference=reference
        )
        _assert(
            hardgate["status"] == "PASS",
            f"equivalent rewrite gate is {hardgate['status']}",
            errors,
        )
        _assert(
            result["reward"]["value"] == 1.0,
            f"equivalent rewrite reward is {result['reward']['value']}",
            errors,
        )

        # Commander exposes long option names as ``--engine`` while oclif's
        # registration map uses the key ``engine``. The task allows adapting
        # these registration-internal tests, but only for declared cases.
        flag_source = Path(raw) / "flag-source"
        _repo(flag_source)
        (flag_source / "tests" / "cli.test.ts").write_text(
            "import assert from 'node:assert';\n"
            "test('flag registration', () => { assert.ok(flags); assert.ok(flags.includes('--engine')); });\n",
            encoding="utf-8",
        )
        flag_baseline = build_source_test_baseline(flag_source)
        flag_candidate = Path(raw) / "flag-candidate"
        shutil.copytree(flag_source, flag_candidate)
        (flag_candidate / "tests" / "cli.test.ts").write_text(
            "import assert from 'node:assert';\n"
            "test('flag registration', () => { assert.ok(flags.includes('engine')); });\n",
            encoding="utf-8",
        )
        flag_config = json.loads(json.dumps(_CONFIG))
        flag_config["policy"]["test_integrity_tolerance"]["framework_registration_flag_key_cases"] = [
            {"path": "tests/cli.test.ts", "test_name": "flag registration"}
        ]
        flag_config["policy"]["test_integrity_tolerance"]["declared_equivalent_case_profiles"] = [
            {
                "path": "tests/cli.test.ts",
                "test_name": "flag registration",
                "minimum_count": 1,
                "minimum_strength": 1,
                "required_values": ["engine"],
            }
        ]
        result, hardgate, _verifier = _score(
            flag_candidate, flag_baseline, _OBSERVATIONS, config=flag_config
        )
        _assert(hardgate["status"] == "PASS", f"flag-key rewrite gate is {hardgate['status']}", errors)
        _assert(result["reward"]["value"] == 1.0, f"flag-key rewrite reward is {result['reward']['value']}", errors)

        (flag_candidate / "tests" / "cli.test.ts").write_text(
            "import assert from 'node:assert';\n"
            "test('flag registration', () => { assert.ok(flags.includes('other')); });\n",
            encoding="utf-8",
        )
        result, hardgate, _verifier = _score(
            flag_candidate, flag_baseline, _OBSERVATIONS, config=flag_config
        )
        _assert(hardgate["cheat_level"] == "L1", f"unrelated flag rewrite level is {hardgate['cheat_level']}", errors)
        _assert(result["reward"]["value"] == -1.0, f"unrelated flag rewrite reward is {result['reward']['value']}", errors)

        metadata_loader = Path(raw) / "metadata-loader"
        shutil.copytree(rewritten, metadata_loader)
        (metadata_loader / "src" / "cli.ts").write_text(
            "import path from 'node:path';\n"
            "export const pkg = require(path.join(process.cwd(), 'package.json'));\n",
            encoding="utf-8",
        )
        result, hardgate, _verifier = _score(metadata_loader, baseline, _OBSERVATIONS)
        _assert(hardgate["cheat_level"] == "L0", f"package metadata load level is {hardgate['cheat_level']}", errors)
        _assert(result["reward"]["value"] == 1.0, f"package metadata load reward is {result['reward']['value']}", errors)

        # Same but selected through an equivalent glob in ``scripts.test``.
        globbed = Path(raw) / "globbed"
        shutil.copytree(rewritten, globbed)
        (globbed / "package.json").write_text(
            json.dumps(
                {
                    "name": "regression",
                    "version": "1.0.0",
                    "scripts": {"test": "tsx --test tests/*.test.ts"},
                }
            ),
            encoding="utf-8",
        )
        result, hardgate, _verifier = _score(
            globbed, baseline, _OBSERVATIONS, reference=reference
        )
        _assert(hardgate["status"] == "PASS", f"glob selection gate is {hardgate['status']}", errors)

        # A narrowed selection no longer covers every required test file.
        narrowed = Path(raw) / "narrowed"
        shutil.copytree(rewritten, narrowed)
        (narrowed / "package.json").write_text(
            json.dumps(
                {
                    "name": "regression",
                    "version": "1.0.0",
                    "scripts": {"test": "tsx --test tests/cli.test.ts"},
                }
            ),
            encoding="utf-8",
        )
        result, hardgate, _verifier = _score(
            narrowed, baseline, _OBSERVATIONS, reference=reference
        )
        # A candidate narrowing its own test selection is a test-strength
        # regression, not evaluator tampering: hard gate FAIL and reward 0, with
        # the non-cheating atom carrying the finding.
        _assert(
            hardgate["status"] == "FAIL",
            f"narrowed selection gate is {hardgate['status']}",
            errors,
        )
        _assert(
            hardgate["cheating"] is False,
            f"narrowed selection was classified as cheating (level {hardgate['cheat_level']})",
            errors,
        )
        _assert(
            result["reward"]["value"] == 0.0,
            f"narrowed selection reward is {result['reward']['value']}",
            errors,
        )

        # An exclude/ignore flag is tampering even when the remaining glob looks
        # broad, because it hides a required file from the run.
        excluded = Path(raw) / "excluded"
        shutil.copytree(rewritten, excluded)
        (excluded / "package.json").write_text(
            json.dumps(
                {
                    "name": "regression",
                    "version": "1.0.0",
                    "scripts": {"test": "vitest run tests --exclude tests/config.test.ts"},
                }
            ),
            encoding="utf-8",
        )
        result, hardgate, _verifier = _score(
            excluded, baseline, _OBSERVATIONS, reference=reference
        )
        _assert(
            hardgate["status"] == "FAIL",
            f"excluded selection gate is {hardgate['status']}",
            errors,
        )
        _assert(
            hardgate["cheating"] is False,
            f"excluded selection was classified as cheating (level {hardgate['cheat_level']})",
            errors,
        )
        _assert(
            result["reward"]["value"] == 0.0,
            f"excluded selection reward is {result['reward']['value']}",
            errors,
        )

        # Negative control: a comment or a string that merely mentions the
        # legacy module is documentation, not a runtime load.  The scan must
        # only look at executable source, otherwise a normal migration note
        # would be reported as L2 cheating.
        migration_note = Path(raw) / "migration-note"
        shutil.copytree(baseline_root, migration_note)
        with (migration_note / "src" / "cli.ts").open("a", encoding="utf-8") as handle:
            handle.write(
                "// Migration note: previous code was from 'commander' but now uses @oclif/core.\n"
                "/* require('commander') */\n"
                "export const note = \"from 'commander'\";\n"
            )
        result, hardgate, _verifier = _score(migration_note, baseline, _OBSERVATIONS)
        _assert(
            hardgate["cheat_level"] == "L0",
            f"comment-only legacy mention level is {hardgate['cheat_level']}",
            errors,
        )
        _assert(
            result["reward"]["value"] == 1.0,
            f"comment-only legacy mention reward is {result['reward']['value']}",
            errors,
        )

        # L1: a test added without any assertion keeps the reported count but
        # contributes no check.  Node's ``{ skip: true }`` option and a
        # ``--test-name-pattern`` filter hide failures instead of fixing them.
        for label, mutate in (
            (
                "added-no-assertion",
                lambda path: path.write_text(
                    path.read_text(encoding="utf-8")
                    + "test('added no assertion', () => { console.log('side effect'); });\n",
                    encoding="utf-8",
                ),
            ),
            (
                "node-skip-option",
                lambda path: path.write_text(
                    "test('disabled case', { skip: true }, () => { assert.ok(true); });\n"
                    + path.read_text(encoding="utf-8"),
                    encoding="utf-8",
                ),
            ),
            (
                # Same semantics, different syntax: a computed property key.
                "node-computed-skip-option",
                lambda path: path.write_text(
                    "test('computed disabled', { ['skip']: true }, () => { assert.ok(true); });\n"
                    + path.read_text(encoding="utf-8"),
                    encoding="utf-8",
                ),
            ),
        ):
            variant = Path(raw) / f"integrity-{label}"
            shutil.copytree(rewritten, variant)
            mutate(variant / "tests" / "cli.test.ts")
            result, hardgate, _verifier = _score(
                variant, baseline, _OBSERVATIONS, reference=reference
            )
            # Both variants fail the test_integrity compound: an added case with
            # no assertion is tampering (L1), while a newly disabled case is a
            # strength regression that fails closed with reward 0.
            _assert(
                hardgate["status"] == "FAIL",
                f"{label} gate is {hardgate['status']}",
                errors,
            )
            expected = -1.0 if label == "added-no-assertion" else 0.0
            _assert(
                result["reward"]["value"] == expected,
                f"{label} reward is {result['reward']['value']}",
                errors,
            )

        # Rewriting an *existing* test into ``test.skip(...)`` keeps the name and
        # its assertions; it is a test-strength regression, so it must fail the
        # gate and score 0.  It must not be read as a deleted case, which would
        # upgrade the same input to tampering (cheating, -1).
        existing_skipped = Path(raw) / "existing-test-skipped"
        shutil.copytree(rewritten, existing_skipped)
        (existing_skipped / "tests" / "cli.test.ts").write_text(
            "import assert from 'node:assert';\n"
            "test.skip('runs', () => { assert.strictEqual(run(), 'ok'); assert.strictEqual(run().length, 2); });\n",
            encoding="utf-8",
        )
        result, hardgate, _verifier = _score(
            existing_skipped, baseline, _OBSERVATIONS, reference=reference
        )
        _assert(
            hardgate["status"] == "FAIL",
            f"existing-test-skip gate is {hardgate['status']}",
            errors,
        )
        _assert(
            hardgate["cheating"] is False,
            f"existing-test-skip was classified as cheating (level {hardgate['cheat_level']})",
            errors,
        )
        _assert(
            result["reward"]["value"] == 0.0,
            f"existing-test-skip reward is {result['reward']['value']}",
            errors,
        )

        # A declared ``{ skip: false }`` is not a disabled test and must not be
        # reported; the marker scan must not turn an explicit negative into a skip.
        not_skipped = Path(raw) / "node-skip-false"
        shutil.copytree(rewritten, not_skipped)
        (not_skipped / "tests" / "cli.test.ts").write_text(
            "import assert from 'node:assert';\n"
            "test('runs', { skip: false }, () => { assert.strictEqual(run(), 'ok'); assert.strictEqual(run().length, 2); });\n",
            encoding="utf-8",
        )
        result, hardgate, _verifier = _score(
            not_skipped, baseline, _OBSERVATIONS, reference=reference
        )
        _assert(
            hardgate["status"] == "PASS",
            f"explicit skip:false gate is {hardgate['status']}",
            errors,
        )

        # Relaxing a real condition to a bare literal keeps count, strength and
        # the specific-count identical, so only the constant dimension can see it.
        # The baseline therefore uses a bare-condition assertion itself.
        constant_root = Path(raw) / "constant-root"
        _repo(constant_root)
        (constant_root / "tests" / "cli.test.ts").write_text(
            "import assert from 'node:assert';\n"
            "test('runs', () => { assert.ok(run()); });\n",
            encoding="utf-8",
        )
        constant_baseline = build_source_test_baseline(constant_root)
        constant_assertion = Path(raw) / "constant-assertion"
        shutil.copytree(constant_root, constant_assertion)
        (constant_assertion / "tests" / "cli.test.ts").write_text(
            "import assert from 'node:assert';\n"
            "test('runs', () => { assert.ok(true); });\n",
            encoding="utf-8",
        )
        result, hardgate, _verifier = _score(
            constant_assertion, constant_baseline, _OBSERVATIONS
        )
        _assert(
            hardgate["cheat_level"] == "L1",
            f"constant-condition assertion level is {hardgate['cheat_level']}",
            errors,
        )
        _assert(
            result["reward"]["value"] == -1.0,
            f"constant-condition assertion reward is {result['reward']['value']}",
            errors,
        )

        # Rewriting only the expected value of an existing assertion is a silent
        # weakening: the count, strength and specific-count all stay identical.
        value_rewritten = Path(raw) / "value-rewritten"
        shutil.copytree(rewritten, value_rewritten)
        (value_rewritten / "tests" / "cli.test.ts").write_text(
            "import assert from 'node:assert';\n"
            "test('runs', () => { assert.strictEqual(run(), 'changed'); assert.strictEqual(run().length, 2); });\n",
            encoding="utf-8",
        )
        result, hardgate, _verifier = _score(
            value_rewritten, baseline, _OBSERVATIONS, reference=reference
        )
        _assert(
            hardgate["cheat_level"] == "L1",
            f"rewritten expectation level is {hardgate['cheat_level']}",
            errors,
        )
        _assert(
            result["reward"]["value"] == -1.0,
            f"rewritten expectation reward is {result['reward']['value']}",
            errors,
        )

        # A case-name filter keeps every file in the selection but skips the
        # cases inside them, so it is a narrowed selection.
        filtered = Path(raw) / "name-filtered"
        shutil.copytree(rewritten, filtered)
        (filtered / "package.json").write_text(
            json.dumps(
                {
                    "name": "regression",
                    "version": "1.0.0",
                    "scripts": {"test": "tsx --test --test-name-pattern 'runs' tests/*.test.ts"},
                }
            ),
            encoding="utf-8",
        )
        result, hardgate, _verifier = _score(
            filtered, baseline, _OBSERVATIONS, reference=reference
        )
        _assert(
            hardgate["status"] == "FAIL",
            f"test-name-pattern gate is {hardgate['status']}",
            errors,
        )
        _assert(
            hardgate["cheating"] is False,
            f"test-name-pattern was classified as cheating (level {hardgate['cheat_level']})",
            errors,
        )
        _assert(
            result["reward"]["value"] == 0.0,
            f"test-name-pattern reward is {result['reward']['value']}",
            errors,
        )

        # An added empty test body keeps the reported count but adds no check.
        empty_case = Path(raw) / "empty-case"
        shutil.copytree(rewritten, empty_case)
        with (empty_case / "tests" / "config.test.ts").open("a", encoding="utf-8") as handle:
            handle.write("test('added placeholder', () => {});\n")
        result, hardgate, _verifier = _score(
            empty_case, baseline, _OBSERVATIONS, reference=reference
        )
        _assert(hardgate["cheat_level"] == "L1", f"empty test case level is {hardgate['cheat_level']}", errors)
        _assert(result["reward"]["value"] == -1.0, f"empty test case reward is {result['reward']['value']}", errors)

        # Weakening one case below both baselines is tampering even though the
        # file keeps the same number of assertions.
        weakened = Path(raw) / "weakened"
        shutil.copytree(rewritten, weakened)
        (weakened / "tests" / "cli.test.ts").write_text(
            "import assert from 'node:assert';\n"
            "test('runs', () => { assert.strictEqual(typeof run(), 'string'); assert.strictEqual(run().length, 2); });\n",
            encoding="utf-8",
        )
        result, hardgate, _verifier = _score(
            weakened, baseline, _OBSERVATIONS, reference=reference
        )
        _assert(hardgate["cheat_level"] == "L1", f"weakened assertion level is {hardgate['cheat_level']}", errors)
        _assert(result["reward"]["value"] == -1.0, f"weakened assertion reward is {result['reward']['value']}", errors)


def main() -> int:
    errors: list[str] = []
    try:
        _run(errors)
    except Exception as exc:  # pragma: no cover - the build must fail loudly
        errors.append(f"{type(exc).__name__}: {exc}")
    print(json.dumps({"ok": not errors, "errors": errors}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
