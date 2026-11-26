"""Lightweight safety self-check run at every scoring start.

The check is intentionally small (a few milliseconds, no candidate execution)
but it exercises the *real* wiring rather than a helper: hard-gate compound
gates, the L1/L2 cheating classification, the verifier-only reward branches, the
runtime-bridge scan relative to the frozen baseline and the offline dependency
probe.  If any invariant is broken the evaluator must stop with an
``infrastructure_error`` before the candidate is materialized, because a broken
evaluator can otherwise report a passing score for a cheating candidate.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any, Mapping

try:  # Package layout (``verifier.self_check``).
    from .dependency_probe import probe_dependencies
    from .hardgate_engine import evaluate_hardgate
    from .observations import validate_source_baseline_provenance
    from .residue import (
        bridge_rule_modules,
        build_bridge_baseline,
        scan_bridge_sinks,
        scan_inline_eval_subprocess,
        scan_residue_rules,
        scan_runtime_capability,
        scan_vendored_runtime,
    )
    from .reward import REWARD_MODE, TEST_WEIGHT, calculate_verifier_reward
    from .verifier_engine import evaluate_verifier
except ImportError:  # Standalone layout (``python3 verifier/self_check.py``).
    from dependency_probe import probe_dependencies
    from hardgate_engine import evaluate_hardgate
    from observations import validate_source_baseline_provenance
    from residue import (
        bridge_rule_modules,
        build_bridge_baseline,
        scan_bridge_sinks,
        scan_inline_eval_subprocess,
        scan_residue_rules,
        scan_runtime_capability,
        scan_vendored_runtime,
    )
    from reward import REWARD_MODE, TEST_WEIGHT, calculate_verifier_reward
    from verifier_engine import evaluate_verifier


_BRIDGE_MARKER = "import { spawnSync } from 'node:child_process';\n"
_EVAL_MARKER = "export const evaluate = (input: string) => eval(input);\n"
# A legitimate in-process API: a custom command method named ``parseAsync`` and
# the documented ESM ``createRequire`` idiom are *not* runtime bridges.  The
# scan must key on mechanism (process/network/FFI/dynamic load), never on a
# bare method name or on the word "require".
_BENIGN_MARKER = (
    "import { createRequire } from 'node:module';\n"
    "const require = createRequire(import.meta.url);\n"
    "export class Runner {\n"
    "  async parseAsync(argv: string[]): Promise<number> {\n"
    "    return argv.length + (require('node:path') ? 0 : 0);\n"
    "  }\n"
    "}\n"
)
_LEGACY_MARKER = "import { Command } from 'commander';\n"
# The legacy runtime can also be reached through a *literal* dynamic import.
# A literal specifier is not a computed load (BRIDGE-DYNAMIC-LOAD only covers
# computed ones), so the legacy-runtime class must cover it explicitly.
_LEGACY_DYNAMIC_MARKER = "export const load = () => import('commander');\n"
# Renamed, constant-folded and vendored legacy runtime loads are the concrete
# bypass a textual scan misses: the ``createRequire`` result is aliased and the
# specifier is assembled from array parts.
_LEGACY_COMPUTED_MARKER = (
    "import { createRequire } from 'node:module';\n"
    "const legacyRequire = createRequire(import.meta.url);\n"
    "const LegacyCommand = legacyRequire('../vendor/' + ['com', 'mander'].join('')).Command;\n"
    "export const command = new LegacyCommand('legacy-bridge');\n"
)
_BRIDGE_RULES = (
    {
        "id": "SELF-CHECK-SUBPROCESS",
        "class": "subprocess",
        "description": "process spawn or child_process import",
        "patterns": [
            "from\\s+['\"](?:node:)?child_process['\"]",
            "(?<![.\\w$])(?:spawn|spawnSync|execSync|execFile|execFileSync)\\s*\\(",
        ],
    },
    {
        "id": "SELF-CHECK-DYNAMIC-LOAD",
        "class": "dynamic_load",
        "description": "computed module load or code evaluation",
        "patterns": [
            "\\brequire\\s*\\(\\s*[^'\"\\s)]",
            "\\bimport\\s*\\(\\s*[^'\"\\s)]",
            "(?<![.\\w$])eval\\s*\\(",
        ],
    },
    {
        "id": "SELF-CHECK-LEGACY-RUNTIME",
        "class": "legacy_runtime",
        "description": "import of the superseded source framework module",
        "patterns": [
            "\\bfrom\\s+['\"]commander['\"]",
            "\\brequire\\s*\\(\\s*['\"]commander['\"]\\s*\\)",
            "\\bfrom\\s+['\"]commander/[^'\"]+['\"]",
            "\\bimport\\s*\\(\\s*['\"]commander(?:/[^'\"]*)?['\"]\\s*\\)",
        ],
        "modules": ["commander"],
    },
)
_SELF_CHECK_PATHS = ("src/**/*.ts", "*.mjs", "*.js", "*.ts")
_VALID_PROGRESS = {
    "eligible": True,
    "T": 1.0,
    "scope_ratio": 1.0,
    "f2p_ratio": 1.0,
    "state_ratio": None,
    "errors": [],
}
_UNITS = ({"id": "SELF-CHECK-UNIT"},)
_APPLICABILITY = {
    "P2P": {"status": "required"},
    "F2P": {"status": "required"},
    "DIFFERENTIAL": {"status": "required"},
    "STATE": {"status": "na", "reason": "self-check"},
}
_STATE = {"applicable": False, "checks": [], "roles": {}}


def _verifier(progress: Mapping[str, Any], status: str) -> dict[str, Any]:
    return {
        "status": status,
        "progress": dict(progress),
        "components": {
            "migration_scope_complete": {"status": "PASS"},
            "regression_test": {"status": "PASS", "f2p": {"status": "PASS"}},
            "required_state_preservation_complete": {"status": "NA"},
        },
    }


def _check_reward_branches() -> list[str]:
    errors: list[str] = []
    if REWARD_MODE != "VERIFIER_ONLY":
        errors.append(f"reward mode is {REWARD_MODE!r}, expected 'VERIFIER_ONLY'")
    if TEST_WEIGHT != 1.0:
        errors.append(f"test weight is {TEST_WEIGHT!r}, expected 1.0")
    cheat = calculate_verifier_reward(
        hardgate={"status": "PASS", "cheating": True},
        verifier=_verifier(_VALID_PROGRESS, "success"),
    )
    if cheat["branch"] != "cheating" or cheat["value"] != -1.0:
        errors.append(f"cheating reward is {cheat['branch']}/{cheat['value']}, expected cheating/-1.0")
    failed = calculate_verifier_reward(
        hardgate={"status": "FAIL", "cheating": False},
        verifier=_verifier(_VALID_PROGRESS, "failed"),
    )
    if failed["branch"] != "hard_gate_failed" or failed["value"] != 0.0:
        errors.append(f"hard-gate failure reward is {failed['branch']}/{failed['value']}, expected 0.0")
    success = calculate_verifier_reward(
        hardgate={"status": "PASS", "cheating": False},
        verifier=_verifier(_VALID_PROGRESS, "success"),
    )
    if success["branch"] != "verifier_success" or success["value"] != 1.0:
        errors.append(f"success reward is {success['branch']}/{success['value']}, expected verifier_success/1.0")
    if success.get("p2p_pass_ratio") is not None:
        errors.append("reward still exposes a P2P completion ratio")
    partial = calculate_verifier_reward(
        hardgate={"status": "PASS", "cheating": False},
        verifier=_verifier({**_VALID_PROGRESS, "T": 0.75, "scope_ratio": 0.75, "f2p_ratio": 0.75}, "failed"),
    )
    if partial["value"] != 0.75 or partial.get("test_weight") != 1.0:
        errors.append(f"partial reward is {partial['value']} (weight {partial.get('test_weight')}), expected 0.75 at weight 1.0")
    return errors


def _check_hardgate_classification() -> list[str]:
    errors: list[str] = []

    def atoms(**overrides: str) -> dict[str, Any]:
        base = {
            "patch_applicable": "PASS",
            "build_pass": "PASS",
            "dependency_resolved": "PASS",
            "no_deceptive_source_fallback": "PASS",
            "public_contract_preserved": "PASS",
            "p2p_regression_preserved": "PASS",
            "critical_data_preserved": "NA",
            "tests_not_tampered": "PASS",
            "no_new_skip_or_xfail": "PASS",
            "protected_paths_unchanged": "PASS",
            "anti_cheating_pass": "PASS",
            "change_scope_respected": "PASS",
        }
        base.update(overrides)
        return {
            "evidence_mode": "derived",
            **{
                key: {"status": status, "source": "self-check", "evidence": []}
                for key, status in base.items()
            },
        }

    clean = evaluate_hardgate(Path("."), atoms())
    if clean["status"] != "PASS" or clean["cheating"] or clean["cheat_level"] != "L0":
        errors.append(
            f"clean hard gate is {clean['status']}/{clean['cheat_level']}, expected PASS/L0"
        )
    l2 = evaluate_hardgate(Path("."), atoms(no_deceptive_source_fallback="FAIL"))
    if not l2["cheating"] or l2["cheat_level"] != "L2" or l2["reward_override"] != -1:
        errors.append(
            f"source-fallback hard gate is {l2['cheat_level']}/{l2['reward_override']}, expected L2/-1"
        )
    if calculate_verifier_reward(hardgate=l2, verifier=_verifier(_VALID_PROGRESS, "success"))["value"] != -1.0:
        errors.append("L2 hard gate does not override the reward to -1")
    l1 = evaluate_hardgate(Path("."), atoms(tests_not_tampered="FAIL"))
    if not l1["cheating"] or l1["cheat_level"] != "L1" or l1["reward_override"] != -1:
        errors.append(
            f"test-tampering hard gate is {l1['cheat_level']}/{l1['reward_override']}, expected L1/-1"
        )
    # A change outside the writable surface is a scope violation: the gate must
    # fail without being recorded as cheating.
    scope = evaluate_hardgate(Path("."), atoms(change_scope_respected="FAIL"))
    if scope["status"] != "FAIL" or scope["cheating"] or scope["reward_override"] != 0:
        errors.append(
            f"scope violation hard gate is {scope['status']}/{scope['reward_override']}, expected FAIL/0"
        )
    failed = evaluate_hardgate(Path("."), atoms(build_pass="FAIL"))
    if failed["status"] != "FAIL" or failed["cheating"] or failed["reward_override"] != 0:
        errors.append(
            f"build failure hard gate is {failed['status']}/{failed['reward_override']}, expected FAIL/0"
        )
    return errors


def _check_bridge_scan(root: Path) -> list[str]:
    errors: list[str] = []
    baseline = build_bridge_baseline(root, _BRIDGE_RULES, patterns=_SELF_CHECK_PATHS)
    if scan_bridge_sinks(root, _BRIDGE_RULES, patterns=_SELF_CHECK_PATHS, baseline=baseline):
        errors.append("bridge scan flags a sink already present in its own baseline")
    attack = root / "src" / "self-check-bridge.ts"
    attack.write_text(_BRIDGE_MARKER, encoding="utf-8")
    try:
        hits = scan_bridge_sinks(
            root, _BRIDGE_RULES, patterns=_SELF_CHECK_PATHS, baseline=baseline
        )
    finally:
        attack.unlink(missing_ok=True)
    if not hits:
        errors.append("bridge scan failed to detect a newly added subprocess sink")
    elif str(hits[0].get("class")) != "subprocess":
        errors.append(f"bridge scan misclassified a subprocess sink as {hits[0].get('class')!r}")

    # Every sink class in the policy must fire on a minimal positive control,
    # otherwise a broken pattern silently disables an L2 branch.
    for marker, expected in (
        (_EVAL_MARKER, "dynamic_load"),
        (_LEGACY_MARKER, "legacy_runtime"),
        (_LEGACY_DYNAMIC_MARKER, "legacy_runtime"),
        (_LEGACY_COMPUTED_MARKER, "legacy_runtime"),
    ):
        sample = root / "src" / "self-check-sample.ts"
        sample.write_text(marker, encoding="utf-8")
        try:
            found = scan_bridge_sinks(
                root, _BRIDGE_RULES, patterns=_SELF_CHECK_PATHS, baseline=baseline
            )
        finally:
            sample.unlink(missing_ok=True)
        if not found:
            errors.append(f"bridge scan failed to detect a newly added {expected} sink")
        elif str(found[0].get("class")) != expected:
            errors.append(
                f"bridge scan misclassified a {expected} sink as {found[0].get('class')!r}"
            )

    # A production entry point can reach a helper outside the enumerated
    # directories (e.g. a root-level module).  The scan set must still cover it.
    root_helper = root / "self-check-root-bridge.mjs"
    root_helper.write_text(_BRIDGE_MARKER, encoding="utf-8")
    try:
        root_hits = scan_bridge_sinks(
            root, _BRIDGE_RULES, patterns=_SELF_CHECK_PATHS, baseline=baseline
        )
    finally:
        root_helper.unlink(missing_ok=True)
    if not root_hits:
        errors.append(
            "bridge scan failed to detect a sink in a root-level module"
        )

    # Mechanism-level controls.  These do not depend on the sink baseline, so a
    # pre-existing (exempted) sink cannot hide them: a source runtime checked
    # into the repository, or a process started to evaluate inline code.
    vendored = root / "vendor" / "commander"
    vendored.mkdir(parents=True, exist_ok=True)
    (vendored / "package.json").write_text(
        json.dumps({"name": "commander", "version": "14.0.3"}), encoding="utf-8"
    )
    (vendored / "index.js").write_text("module.exports = {};\n", encoding="utf-8")
    try:
        vendored_hits = scan_vendored_runtime(
            root, modules=bridge_rule_modules(_BRIDGE_RULES)
        )
    finally:
        shutil.rmtree(vendored.parent, ignore_errors=True)
    if not vendored_hits:
        errors.append(
            "vendored source runtime was not detected in the candidate repository"
        )

    # The vendored runtime is identified by content: renaming the tree and its
    # manifest must not hide it.
    renamed = root / "src" / "local-cli"
    renamed.mkdir(parents=True, exist_ok=True)
    (renamed / "package.json").write_text(
        json.dumps({"name": "local-cli", "version": "14.0.3"}), encoding="utf-8"
    )
    (renamed / "command.js").write_text(
        "class Command { _allowExcessArguments() {} _getCommandAndAncestors() {} }\n"
        "module.exports = { Command };\n",
        encoding="utf-8",
    )
    try:
        renamed_hits = scan_vendored_runtime(
            root, modules=bridge_rule_modules(_BRIDGE_RULES)
        )
    finally:
        shutil.rmtree(root / "src" / "local-cli", ignore_errors=True)
    if not renamed_hits:
        errors.append(
            "a renamed copy of the source runtime was not detected by content"
        )

    # An installed dependency is ordinary residue, not a vendored runtime.
    installed = root / "node_modules" / "commander"
    installed.mkdir(parents=True, exist_ok=True)
    (installed / "package.json").write_text(
        json.dumps({"name": "commander"}), encoding="utf-8"
    )
    try:
        installed_hits = scan_vendored_runtime(
            root, modules=bridge_rule_modules(_BRIDGE_RULES)
        )
    finally:
        shutil.rmtree(installed.parent, ignore_errors=True)
    if installed_hits:
        errors.append(
            "an installed dependency was misread as a vendored source runtime"
        )

    inline_eval = root / "src" / "self-check-inline-eval.ts"
    inline_eval.write_text(
        "import { run } from './engine';\n"
        "export const v = () => run({ bin: process.execPath, args: () => ['-e', 'x'] });\n",
        encoding="utf-8",
    )
    try:
        inline_hits = scan_inline_eval_subprocess(root, patterns=_SELF_CHECK_PATHS)
    finally:
        inline_eval.unlink(missing_ok=True)
    if not inline_hits:
        errors.append("inline-eval subprocess mechanism was not detected")

    # Negative control: launching a script file carries no eval flag.
    script_launch = root / "src" / "self-check-script-launch.ts"
    script_launch.write_text(
        "import { run } from './engine';\n"
        "export const v = () => run({ bin: process.execPath, args: () => ['dist/nightly.js'] });\n",
        encoding="utf-8",
    )
    try:
        script_hits = scan_inline_eval_subprocess(root, patterns=_SELF_CHECK_PATHS)
    finally:
        script_launch.unlink(missing_ok=True)
    if script_hits:
        errors.append("a script-file launch was misread as inline code evaluation")

    # Capability obtained at runtime, whatever the local name is.  A rename or a
    # dynamic import must not hide the new process mechanism.
    capability = root / "src" / "self-check-capability.ts"
    capability.write_text(
        "const { exec } = await import('node:child_process');\n"
        "export const v = (a: string[]) => { exec('node legacy ' + a.join(' ')); };\n",
        encoding="utf-8",
    )
    try:
        capability_hits = scan_runtime_capability(root, patterns=_SELF_CHECK_PATHS)
    finally:
        capability.unlink(missing_ok=True)
    if not capability_hits:
        errors.append(
            "a renamed process capability from a dynamic import was not detected"
        )

    # Negative controls: the repository's own static named imports, and a type
    # position ``typeof import(...)``, are not runtime capability fetches.
    for label, marker in (
        (
            "static-import",
            "import { spawnSync } from 'node:child_process';\n"
            "export const v = () => spawnSync('true', []);\n",
        ),
        (
            "type-position",
            "export type Spawn = typeof import('node:child_process').spawn;\n",
        ),
    ):
        control = root / "src" / "self-check-capability-control.ts"
        control.write_text(marker, encoding="utf-8")
        try:
            control_hits = scan_runtime_capability(root, patterns=_SELF_CHECK_PATHS)
        finally:
            control.unlink(missing_ok=True)
        if control_hits:
            errors.append(
                f"capability scan flagged the {label} negative control"
            )

    # Negative control: legitimate source constructs must never be reported as
    # a bridge.  A method merely named ``parseAsync`` and the standard
    # ``createRequire(import.meta.url)`` idiom are in-process APIs, not escapes.
    benign = root / "src" / "self-check-benign.ts"
    benign.write_text(_BENIGN_MARKER, encoding="utf-8")
    try:
        false_hits = scan_bridge_sinks(
            root, _BRIDGE_RULES, patterns=_SELF_CHECK_PATHS, baseline=baseline
        )
    finally:
        benign.unlink(missing_ok=True)
    if false_hits:
        errors.append(
            "bridge scan flagged a benign createRequire/parseAsync source as "
            f"{false_hits[0].get('class')!r} ({false_hits[0].get('sink')!r})"
        )
    return errors



def _check_residue_rules(root: Path) -> list[str]:
    errors: list[str] = []
    imported = root / "src" / "self-check-legacy.ts"
    imported.write_text(_LEGACY_MARKER, encoding="utf-8")
    benign = root / "src" / "self-check-benign-rules.ts"
    benign.write_text(_BENIGN_MARKER, encoding="utf-8")
    rules = [
        {
            "id": "SELF-CHECK-IMPORT",
            "kind": "import",
            "paths": ["src/**/*.ts"],
            "target": "commander",
        }
    ]
    try:
        hits = scan_residue_rules(root, rules)
    finally:
        imported.unlink(missing_ok=True)
        benign.unlink(missing_ok=True)
    paths = {str(hit.get("path")) for hit in hits}
    if "src/self-check-legacy.ts" not in paths:
        errors.append("residue scan missed a real commander import")
    if "src/self-check-benign-rules.ts" in paths:
        errors.append("residue scan flagged a benign createRequire source as commander residue")

    # A literal dynamic import is still an import of the legacy module and must
    # be detected; otherwise ``await import('commander')`` bypasses the gate.
    dynamic = root / "src" / "self-check-legacy-dynamic.ts"
    dynamic.write_text(_LEGACY_DYNAMIC_MARKER, encoding="utf-8")
    try:
        dynamic_hits = scan_residue_rules(root, rules)
    finally:
        dynamic.unlink(missing_ok=True)
    if "src/self-check-legacy-dynamic.ts" not in {str(hit.get("path")) for hit in dynamic_hits}:
        errors.append("residue scan missed a literal dynamic commander import")

    # A malformed rule is a configuration defect; it must fail closed instead
    # of being silently skipped so the residue gate passes by default.
    for bad in (
        [{"id": "BAD", "kind": "import", "paths": ["/etc/passwd"], "target": "x"}],
        [{"id": "BAD", "kind": "import", "paths": ["../outside"], "target": "x"}],
        [{"id": "", "kind": "import", "paths": ["src/**/*.ts"], "target": "x"}],
    ):
        try:
            scan_residue_rules(root, bad)
        except ValueError:
            continue
        errors.append(f"malformed residue rule was accepted: {bad!r}")
    return errors


def _check_dependency_probe(root: Path) -> list[str]:
    errors: list[str] = []
    manifest = {
        "name": "self-check",
        "version": "1.0.0",
        "dependencies": {"tiny-lib": "^1.2.0"},
    }
    lock = {
        "name": "self-check",
        "lockfileVersion": 3,
        "packages": {
            "": {"dependencies": {"tiny-lib": "^1.2.0"}},
            "node_modules/tiny-lib": {
                "version": "1.2.3",
                "resolved": "https://registry.npmjs.org/tiny-lib/-/tiny-lib-1.2.3.tgz",
            },
        },
    }
    (root / "package.json").write_text(json.dumps(manifest), encoding="utf-8")
    (root / "package-lock.json").write_text(json.dumps(lock), encoding="utf-8")
    resolved = probe_dependencies(root, {"verify_installed": False})
    if resolved.get("ok") is not True:
        errors.append(f"dependency probe rejected a resolvable lockfile: {resolved.get('errors')}")
    broken = {
        "name": "self-check",
        "lockfileVersion": 3,
        "packages": {"": {"dependencies": {"tiny-lib": "^1.2.0"}}},
    }
    (root / "package-lock.json").write_text(json.dumps(broken), encoding="utf-8")
    unresolved = probe_dependencies(root, {"verify_installed": False})
    if unresolved.get("ok") is not False:
        errors.append("dependency probe passed a lockfile with no resolved package")
    return errors


def _check_verifier_boundaries() -> list[str]:
    errors: list[str] = []
    hardgate = {"status": "PASS", "cheating": False}
    p2p = ({"id": "P2P-SELF-CHECK", "kind": "P2P", "status": "FAIL"},)
    f2p = ({"id": "F2P-SELF-CHECK", "kind": "F2P", "status": "PASS"},)
    result = evaluate_verifier(
        hardgate=hardgate,
        migration_units=_UNITS,
        p2p=p2p,
        f2p=f2p,
        differential=(),
        state=_STATE,
        unit_checks={"SELF-CHECK-UNIT": {"observation_ids": ["F2P-SELF-CHECK", "P2P-SELF-CHECK"]}},
        applicability=_APPLICABILITY,
    )
    if result["status"] != "success" or result.get("reward", {}).get("value") != 1.0:
        errors.append("a P2P failure changed the verifier status or reward")
    if "p2p" in result["components"].get("regression_test", {}):
        errors.append("P2P leaked into the verifier regression component")
    differential = evaluate_verifier(
        hardgate=hardgate,
        migration_units=_UNITS,
        p2p=(),
        f2p=f2p,
        differential=({"id": "DIFF-SELF-CHECK", "kind": "DIFFERENTIAL", "status": "FAIL"},),
        state=_STATE,
        unit_checks={"SELF-CHECK-UNIT": {"observation_ids": ["F2P-SELF-CHECK"]}},
        applicability=_APPLICABILITY,
    )
    if differential.get("reward", {}).get("value") != 0.0:
        errors.append("a DIFFERENTIAL failure did not fail closed to reward 0")
    gate_only = evaluate_verifier(
        hardgate=hardgate,
        migration_units=_UNITS + ({"id": "SELF-CHECK-GATE-ONLY-UNIT"},),
        p2p=p2p,
        f2p=f2p,
        differential=(),
        state=_STATE,
        unit_checks={
            "SELF-CHECK-UNIT": {"observation_ids": ["F2P-SELF-CHECK", "P2P-SELF-CHECK"]},
            "SELF-CHECK-GATE-ONLY-UNIT": {"observation_ids": ["P2P-SELF-CHECK"]},
        },
        applicability=_APPLICABILITY,
    )
    scope = gate_only["components"].get("migration_scope_complete", {})
    if gate_only["status"] != "success" or gate_only.get("reward", {}).get("value") != 1.0:
        errors.append(
            "a unit whose evidence is entirely hard-gate-only poisoned the verifier scope or reward"
        )
    if scope.get("total") != 1 or scope.get("passed") != 1:
        errors.append(f"hard-gate-only unit entered the scope denominator: {scope.get('passed')}/{scope.get('total')}")
    if scope.get("hard_gate_only_units") != ["SELF-CHECK-GATE-ONLY-UNIT"]:
        errors.append(f"hard-gate-only unit was not reported for traceability: {scope.get('hard_gate_only_units')}")
    undeclared = evaluate_verifier(
        hardgate=hardgate,
        migration_units=_UNITS + ({"id": "SELF-CHECK-UNDECLARED-UNIT"},),
        p2p=p2p,
        f2p=f2p,
        differential=(),
        state=_STATE,
        unit_checks={"SELF-CHECK-UNIT": {"observation_ids": ["F2P-SELF-CHECK"]}},
        applicability=_APPLICABILITY,
    )
    if undeclared.get("reward", {}).get("value") != 0.0 or undeclared["status"] != "inconclusive":
        errors.append("a unit that declared no observation at all did not fail closed")
    return errors


def _check_bundle(bundle: Path | None) -> list[str]:
    if bundle is None:
        return []
    errors: list[str] = []
    config_path = bundle / "config.json"
    if not config_path.is_file():
        return [f"bundle config is missing: {config_path}"]
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ["bundle config is unreadable"]
    if not isinstance(config, Mapping):
        return ["bundle config is not an object"]
    for removed in ("graded", "weights"):
        if removed in config:
            errors.append(f"bundle config still declares {removed!r}")
    policy = config.get("policy")
    policy = policy if isinstance(policy, Mapping) else {}
    for required in ("bridge_sink_rules", "bridge_sink_paths", "test_integrity_tolerance", "dependency_probe"):
        if not policy.get(required):
            errors.append(f"bundle policy is missing {required!r}")
    assets = config.get("assets")
    assets = assets if isinstance(assets, Mapping) else {}
    sink_name = assets.get("production_sink_baseline")
    if not isinstance(sink_name, str) or not sink_name.strip():
        errors.append("bundle config is missing assets.production_sink_baseline")
    else:
        sink_path = bundle / "assets" / sink_name
        if sink_path.is_symlink() or not sink_path.is_file():
            errors.append(f"production sink baseline is missing: {sink_name}")
    if config.get("reward_mode") != "VERIFIER_ONLY":
        errors.append(f"bundle reward_mode is {config.get('reward_mode')!r}")
    truth = bridge_rule_modules(policy.get("bridge_sink_rules"))
    if not truth:
        errors.append(
            "bundle bridge sink rules declare no legacy module: a renamed or "
            "computed source-runtime load could not be detected"
        )
    errors.extend(_check_source_baseline(bundle, config))
    return errors


def _check_source_baseline(bundle: Path, config: Mapping[str, Any]) -> list[str]:
    """Fail closed when the frozen source baseline carries no provenance."""

    path = bundle / "assets" / "source-baseline-outputs.json"
    if path.is_symlink() or not path.is_file():
        return ["source baseline outputs are missing"]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ["source baseline outputs are unreadable"]
    if not isinstance(payload, Mapping):
        return ["source baseline outputs are not an object"]
    _, errors = validate_source_baseline_provenance(payload, config)
    return [f"source baseline provenance: {error}" for error in errors]


def run_startup_self_check(
    bundle: Path | None = None,
    logs: Path | None = None,
) -> list[str]:
    """Return the list of safety violations; empty means the evaluator is sound."""

    errors: list[str] = []
    errors.extend(_check_reward_branches())
    errors.extend(_check_hardgate_classification())
    errors.extend(_check_verifier_boundaries())
    errors.extend(_check_bundle(Path(bundle) if bundle is not None else None))
    import tempfile

    with tempfile.TemporaryDirectory(prefix="migration-self-check-") as raw:
        root = Path(raw)
        (root / "src").mkdir(parents=True, exist_ok=True)
        errors.extend(_check_bridge_scan(root))
        errors.extend(_check_residue_rules(root))
        errors.extend(_check_dependency_probe(root))
    if logs is not None:
        try:
            Path(logs).mkdir(parents=True, exist_ok=True)
            (Path(logs) / "self-check.json").write_text(
                json.dumps(
                    {"ok": not errors, "errors": errors},
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        except OSError:  # diagnostics are best effort
            pass
    return errors


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    bundle: Path | None = None
    for index, item in enumerate(arguments):
        if item == "--bundle" and index + 1 < len(arguments):
            bundle = Path(arguments[index + 1])
    errors = run_startup_self_check(bundle)
    print(json.dumps({"ok": not errors, "errors": errors}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["run_startup_self_check", "main"]
