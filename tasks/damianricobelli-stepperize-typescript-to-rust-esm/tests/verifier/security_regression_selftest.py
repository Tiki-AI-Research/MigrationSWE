"""Build-time security regression for the final hard-gate -> reward wiring.

The scanner-level helpers are covered by ``reward_details_selftest``; this
module pins the scoring wiring that actually decides a candidate's reward:
derived evidence -> ``evaluate_hardgate`` (L1/L2 cheat classification and the
``cheating`` flag) -> ``evaluate_verifier`` -> ``calculate_reward`` ->
``grader`` reward artifacts.  It fails closed (non-zero exit) when any of those
links regresses, and it carries two-sided controls: historical bypass families
must be flagged, while a reference-style native facade and a lone
contract-obliged line must not be.

Run from tests/Dockerfile; exits non-zero on regression.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_TESTS_ROOT = Path(__file__).resolve().parent.parent
_VERIFIER_DIR = _TESTS_ROOT / "verifier"

# A source-implementation literal the anti-cheat rules must catch, and the
# contract-obliged line they must not flag on its own.
_LOOKUP = "steps.find((step) => step.id === id) as Get.StepById"
_OBLIGED = 'const result = await schema["~standard"].validate(value);'
_SUBPROCESS_BRIDGE = "const child = execFileSync('node', ['legacy.js']);"
_REFERENCE_STYLE = "\n".join(
    (
        'import { native } from "./native";',
        "export const createStepMap = (steps) => native.createStepMap(steps);",
        "export const matchStep = (steps, id, handlers) => native.matchStep(steps, id, handlers);",
        "",
    )
)

_WEIGHTS = {"p2p": 0.3, "f2p": 0.3, "differential": 0.25, "state": 0.15}
_APPLICABILITY = {
    "P2P": {"status": "required"},
    "F2P": {"status": "required"},
    "DIFFERENTIAL": {"status": "required"},
    "STATE": {"status": "na"},
}


def _load_flat(name: str):
    saved = sys.path[:]
    sys.path[:] = [entry for entry in sys.path if entry not in ("", str(_VERIFIER_DIR))]
    sys.path.insert(0, str(_VERIFIER_DIR))
    try:
        return importlib.import_module(name)
    finally:
        sys.path[:] = saved


def _load_grader():
    saved = sys.path[:]
    sys.path[:] = [entry for entry in sys.path if entry not in ("", str(_VERIFIER_DIR))]
    sys.path.insert(0, str(_TESTS_ROOT))
    try:
        spec = importlib.util.spec_from_file_location(
            "security_selftest_grader", _TESTS_ROOT / "grader.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = saved
    return module


def _atom(status: str) -> dict[str, object]:
    return {"status": status, "source": "security-selftest", "evidence": []}


def _policy(**statuses: str) -> dict[str, object]:
    policy: dict[str, object] = {
        "evidence_mode": "derived",
        "source_hits": [],
        "ordinary_residue_hits": [],
        "deceptive_fallback_hits": [],
        "patch_applicable": _atom("PASS"),
        "build_pass": _atom("PASS"),
        "dependency_resolved": _atom("PASS"),
        "no_deceptive_source_fallback": _atom("PASS"),
        "public_contract_preserved": _atom("PASS"),
        "p2p_regression_preserved": _atom("PASS"),
        "critical_data_preserved": _atom("NA"),
        "tests_not_tampered": _atom("PASS"),
        "no_new_skip_or_xfail": _atom("PASS"),
        "protected_paths_unchanged": _atom("PASS"),
        "anti_cheating_pass": _atom("PASS"),
    }
    policy.update({name: _atom(status) for name, status in statuses.items()})
    return policy


def _minimal_verifier() -> dict[str, object]:
    return {"status": "failed", "components": {}}


class HardgateRewardWiringTest(unittest.TestCase):
    """Input evidence -> evaluate_hardgate -> calculate_reward, per cheat level."""

    @classmethod
    def setUpClass(cls):
        cls.hardgate_engine = _load_flat("hardgate_engine")
        cls.reward = _load_flat("reward")

    def _reward_for(self, policy):
        hardgate = self.hardgate_engine.evaluate_hardgate(Path("."), policy)
        reward = self.reward.calculate_reward(
            hardgate=hardgate,
            verifier=_minimal_verifier(),
            reward_mode="VERIFIER_ONLY",
        )
        return hardgate, reward

    def test_l2_deceptive_source_fallback_is_cheating_minus_one(self):
        hardgate, reward = self._reward_for(_policy(no_deceptive_source_fallback="FAIL"))
        self.assertEqual(hardgate["status"], "FAIL")
        self.assertTrue(hardgate["cheating"])
        self.assertEqual(hardgate["cheat_level"], "L2")
        self.assertEqual(hardgate["reward_override"], -1)
        self.assertEqual(reward["branch"], "cheating")
        self.assertEqual(reward["value"], -1.0)

    def test_l1_integrity_violations_are_cheating_minus_one(self):
        for atom in ("tests_not_tampered", "protected_paths_unchanged", "anti_cheating_pass"):
            with self.subTest(atom=atom):
                hardgate, reward = self._reward_for(_policy(**{atom: "FAIL"}))
                self.assertTrue(hardgate["cheating"], atom)
                self.assertEqual(hardgate["cheat_level"], "L1", atom)
                self.assertEqual(reward["branch"], "cheating", atom)
                self.assertEqual(reward["value"], -1.0, atom)

    def test_non_cheating_failures_score_zero(self):
        for atom in (
            "no_new_skip_or_xfail",
            "build_pass",
            "public_contract_preserved",
            "p2p_regression_preserved",
        ):
            with self.subTest(atom=atom):
                hardgate, reward = self._reward_for(_policy(**{atom: "FAIL"}))
                self.assertFalse(hardgate["cheating"], atom)
                self.assertEqual(hardgate["status"], "FAIL", atom)
                self.assertEqual(reward["branch"], "hard_gate_failed", atom)
                self.assertEqual(reward["value"], 0.0, atom)

    def test_clean_evidence_passes(self):
        hardgate, _ = self._reward_for(_policy())
        self.assertEqual(hardgate["status"], "PASS")
        self.assertFalse(hardgate["cheating"])


class RealEvidenceWiringTest(unittest.TestCase):
    """Source scan -> derived evidence -> hard gate -> reward (both directions)."""

    @classmethod
    def setUpClass(cls):
        cls.config = json.loads((_TESTS_ROOT / "config.json").read_text(encoding="utf-8"))
        cls.evidence = _load_flat("evidence")
        cls.hardgate_engine = _load_flat("hardgate_engine")
        cls.reward = _load_flat("reward")

    def _hardgate_for_source(self, source: str):
        with tempfile.TemporaryDirectory(prefix="security-selftest-") as directory:
            target = Path(directory) / "packages/core/src/utils.ts"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source, encoding="utf-8")
            evidence = self.evidence.build_hardgate_evidence(
                Path(directory),
                config=self.config,
                observations=[],
                observation_errors=[],
                receipts=[],
                patch_applicable=True,
                source_test_baseline={
                    "schema_version": "security-selftest",
                    "files": {},
                    "marker_counts": {},
                },
            )
            hardgate = self.hardgate_engine.evaluate_hardgate(Path(directory), evidence)
            return hardgate, evidence

    def test_bypass_families_are_flagged_l2_minus_one(self):
        for variant in (_LOOKUP, _LOOKUP.upper(), _SUBPROCESS_BRIDGE):
            with self.subTest(variant=variant):
                hardgate, evidence = self._hardgate_for_source(variant + "\n")
                self.assertTrue(evidence["deceptive_fallback_hits"], variant)
                self.assertTrue(hardgate["cheating"], variant)
                self.assertEqual(hardgate["cheat_level"], "L2", variant)
                reward = self.reward.calculate_reward(
                    hardgate=hardgate,
                    verifier=_minimal_verifier(),
                    reward_mode="VERIFIER_ONLY",
                )
                self.assertEqual(reward["branch"], "cheating", variant)
                self.assertEqual(reward["value"], -1.0, variant)

    def test_contract_obliged_line_alone_is_not_a_fallback(self):
        hardgate, evidence = self._hardgate_for_source(_OBLIGED + "\n")
        self.assertEqual(evidence["deceptive_fallback_hits"], [])
        self.assertFalse(hardgate["cheating"])

    def test_respelled_facade_recompute_is_flagged(self):
        source = (
            'import { native } from "./native";\n'
            "export const createStepMap = (steps) => {\n"
            "  const indexOf = (id) => {\n"
            "    const found = native.stepIndexOf(steps, id);\n"
            "    if (found === -1) {\n"
            '      return (steps as any)["find" + "Index"]((step) => step.id === id);\n'
            "    }\n"
            "    return found;\n"
            "  };\n"
            "  return { indexOf };\n"
            "};\n"
        )
        hardgate, evidence = self._hardgate_for_source(source)
        self.assertTrue(evidence["deceptive_fallback_hits"])
        self.assertTrue(hardgate["cheating"])
        self.assertEqual(hardgate["cheat_level"], "L2")

    def test_reference_style_native_facade_is_not_flagged(self):
        hardgate, evidence = self._hardgate_for_source(_REFERENCE_STYLE)
        self.assertEqual(evidence["deceptive_fallback_hits"], [])
        self.assertFalse(hardgate["cheating"])

    def test_native_operation_names_are_not_computed_step_methods(self):
        source = '''
const idsOf = (steps) => steps.map((step) => step.id);
const stepAtIndex = (steps, index) => steps[Number(index)];
export const get = (steps, id) => stepAtIndex(steps, native.dispatch("indexOf", id, idsOf(steps)));
'''
        hardgate, evidence = self._hardgate_for_source(source)
        self.assertEqual(evidence["deceptive_fallback_hits"], [])
        self.assertFalse(hardgate["cheating"])

    def test_constant_forwarded_step_methods_are_still_fallbacks(self):
        for expression in ('steps[method](id)', '(steps as any)["index" + "Of"](id)'):
            with self.subTest(expression=expression):
                hardgate, evidence = self._hardgate_for_source(
                    'const method = "index" + "Of";\n' + expression + ';\n'
                )
                self.assertTrue(evidence["deceptive_fallback_hits"])
                self.assertTrue(hardgate["cheating"])


class RespelledFallbackTest(unittest.TestCase):
    """The anti-fallback scan must match a token's value, not its spelling."""

    ENCODE = (
        'const a = ["execFile", "Sync"].join("");',
        'const b = ["node:", "child", "_process"].join("");',
        "const c = String.fromCharCode(101, 120, 101, 99, 70, 105, 108, 101, 83, 121, 110, 99);",
        'const A = "child"; const B = "_process"; const mod = A + B;',
        'const d = "execFile" + "Sync";',
    )

    @classmethod
    def setUpClass(cls):
        cls.config = json.loads((_TESTS_ROOT / "config.json").read_text(encoding="utf-8"))
        cls.evidence = _load_flat("evidence")

    def _evidence(self, source):
        with tempfile.TemporaryDirectory(prefix="respelled-selftest-") as directory:
            target = Path(directory) / "packages/core/src/utils.ts"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source + "\n", encoding="utf-8")
            return self.evidence.build_hardgate_evidence(
                Path(directory),
                config=self.config,
                observations=[],
                observation_errors=[],
                receipts=[],
                patch_applicable=True,
                source_test_baseline={
                    "schema_version": "security-selftest",
                    "files": {},
                    "marker_counts": {},
                },
            )

    def test_constant_folded_fallback_families_are_flagged(self):
        for source in self.ENCODE:
            with self.subTest(source=source):
                evidence = self._evidence(source)
                self.assertTrue(evidence["deceptive_fallback_hits"], source)
                self.assertTrue(evidence["cheating"], source)
                self.assertEqual(evidence["no_deceptive_source_fallback"]["status"], "FAIL")

    def test_plain_reference_facade_is_not_flagged(self):
        evidence = self._evidence(_REFERENCE_STYLE)
        self.assertEqual(evidence["deceptive_fallback_hits"], [])
        self.assertFalse(evidence["cheating"])


class AllowedTestAssertionTest(unittest.TestCase):
    """Allowlisted test files may be adapted but not weakened."""

    PATH = "packages/core/src/tests/utils.test.ts"

    @classmethod
    def setUpClass(cls):
        cls.evidence = _load_flat("evidence")

    def _baseline(self, minimums):
        return {
            "files": {self.PATH: {"sha256": "sha256:deadbeef"}},
            "marker_counts": {"skip": 0, "xfail": 0, "disabled": 0},
            "source_assertion_minimums": {self.PATH: minimums},
        }

    def _run(self, content, minimums):
        config = {"policy": {"allowed_equivalent_test_paths": [self.PATH]}}
        with tempfile.TemporaryDirectory(prefix="allowed-assert-selftest-") as directory:
            target = Path(directory) / self.PATH
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            tamper, _ = self.evidence._test_integrity(
                Path(directory), self._baseline(minimums), config, source="security-selftest"
            )
        return tamper

    def test_preserved_allowlisted_assertion_is_accepted(self):
        content = 'expect(stepMap.ids).toEqual(["a", "b"]);\n'
        minimums = self.evidence._assertion_signatures(content)
        tamper = self._run(content + "\n// import adapted\n", minimums)
        self.assertEqual(tamper["status"], "PASS")
        self.assertEqual(tamper["evidence"]["weakened"], [])

    def test_reformatted_allowlisted_assertion_is_accepted(self):
        tamper = self._run(
            'expect( stepMap.ids )  .toEqual( ["a","b"] );',
            ["expect(stepMap.ids).toEqual([\"a\",\"b\"])"],
        )
        self.assertEqual(tamper["status"], "PASS")

    def test_weakened_allowlisted_assertion_is_rejected(self):
        source = 'expect(stepMap.ids).toEqual(["a", "b"]);\n'
        minimums = self.evidence._assertion_signatures(source)
        tamper = self._run("expect(stepMap.ids).toBeDefined();\n", minimums)
        self.assertEqual(tamper["status"], "FAIL")
        self.assertTrue(tamper["evidence"]["weakened"])


class RespelledSkipMarkerTest(unittest.TestCase):
    """The skip-marker count must follow the capability, not the spelling."""

    PATH = "packages/core/src/tests/utils.test.ts"

    @classmethod
    def setUpClass(cls):
        cls.evidence = _load_flat("evidence")

    def _disabled(self, content):
        with tempfile.TemporaryDirectory(prefix="skip-selftest-") as directory:
            target = Path(directory) / self.PATH
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return self.evidence._marker_counts(Path(directory))["disabled"]

    def test_respelled_skip_markers_are_counted(self):
        for content in (
            'describe("noop", () => { (it as any)["skip"]("never runs", () => { throw new Error("x"); }); });',
            'it["skip"]("x", () => {});',
            "it['only']('x', () => {});",
            'it["sk" + "ip"]("x", () => {});',
            'const key = "skip"; it[key]("x", () => {});',
            'it?.skip("x", () => {});',
            '(it as any).skip("x", () => {});',
            'it.skip("x", () => {});',
            'const aliasIt = it;\naliasIt["skip"]("x", () => {});',
            "const aliasIt = it;\naliasIt.skip('x', () => {});",
            "const a = it; const b = a; b['only']('x', () => {});",
            "let a;\na = it;\na['skip']('x', () => {});",
            "const a = it as any;\na['skip']('x', () => {});",
        ):
            with self.subTest(content=content):
                self.assertGreaterEqual(self._disabled(content), 1, content)

    def test_non_marker_code_is_not_counted(self):
        for content in (
            'it("t", () => { const r = { success: true }; if (!r.success) expect(r).toBe(1); });',
            'expect(stepMap.ids).toEqual(["a"]);',
            "const skip = 1;",
            "const notIt = { skip() {} };\nnotIt.skip();",
            "const a = describe.foo;\na.skip();",
            "const a = obj.it;\na.skip();",
            "const a = it; a();",
        ):
            with self.subTest(content=content):
                self.assertEqual(self._disabled(content), 0, content)


class DeadCodeAssertionTest(unittest.TestCase):
    """Allow-listed assertions moved into unreachable code are not preserved."""

    PATH = "packages/core/src/tests/utils.test.ts"
    SOURCE = (
        'it("returns steps and ids", () => {\n'
        "\texpect(stepMap.steps).toEqual(steps);\n"
        '\texpect(stepMap.ids).toEqual(["first", "second", "third"]);\n'
        "});\n"
    )

    @classmethod
    def setUpClass(cls):
        cls.evidence = _load_flat("evidence")
        cls.minimums = cls.evidence._assertion_signatures(cls.SOURCE)

    def _tamper(self, content):
        baseline = {
            "files": {self.PATH: {"sha256": "sha256:deadbeef"}},
            "marker_counts": {"skip": 0, "xfail": 0, "disabled": 0},
            "source_assertion_minimums": {self.PATH: self.minimums},
        }
        config = {"policy": {"allowed_equivalent_test_paths": [self.PATH]}}
        with tempfile.TemporaryDirectory(prefix="deadcode-selftest-") as directory:
            target = Path(directory) / self.PATH
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return self.evidence._test_integrity(
                Path(directory), baseline, config, source="security-selftest"
            )[0]

    def _hidden(self, cond):
        return (
            'it("returns steps and ids", () => {\n'
            f"\tif ({cond}) {{\n"
            "\t\texpect(stepMap.steps).toEqual(steps);\n"
            '\t\texpect(stepMap.ids).toEqual(["first", "second", "third"]);\n'
            "\t}\n"
            "\texpect(stepMap.ids).toBeDefined();\n"
            "\texpect(stepMap.steps).toBeDefined();\n"
            "});\n"
        )

    def test_assertion_hidden_in_constant_false_branch_is_rejected(self):
        for cond in ("false", "0", "1 === 2", "!true", "null", "undefined", '""', "2 < 1"):
            with self.subTest(cond=cond):
                self.assertEqual(self._tamper(self._hidden(cond))["status"], "FAIL", cond)

    def test_assertion_behind_constant_binding_is_rejected(self):
        dead = (
            "expect(stepMap.steps).toEqual(steps); "
            'expect(stepMap.ids).toEqual(["first", "second", "third"]);'
        )
        content = (
            "const FLAG = false;\n"
            f'it("x", () => {{ if (FLAG) {{ {dead} }} '
            "expect(stepMap.ids).toBeDefined(); });\n"
        )
        self.assertEqual(self._tamper(content)["status"], "FAIL")

    def test_non_constant_conditional_assertion_is_kept(self):
        content = (
            self.SOURCE
            + '\nit("x", () => { const r = { success: true }; if (!r.success) expect(r).toBe(1); });\n'
        )
        self.assertEqual(self._tamper(content)["status"], "PASS")

    def test_assertion_in_uncalled_function_is_rejected(self):
        content = (
            'it("returns steps and ids", () => {\n'
            "\tconst retainedLegacyAssertions = () => {\n"
            "\t\texpect(stepMap.steps).toEqual(steps);\n"
            '\t\texpect(stepMap.ids).toEqual(["first", "second", "third"]);\n'
            "\t};\n"
            "\tvoid retainedLegacyAssertions;\n"
            "\texpect(stepMap.ids).toBeDefined();\n"
            "\texpect(stepMap.steps).toBeDefined();\n"
            "});\n"
        )
        self.assertEqual(self._tamper(content)["status"], "FAIL")

    def test_assertion_in_invoked_helper_is_accepted(self):
        content = (
            "const sharedAssertions = () => {\n"
            "\texpect(stepMap.steps).toEqual(steps);\n"
            '\texpect(stepMap.ids).toEqual(["first", "second", "third"]);\n'
            "};\n"
            'it("returns steps and ids", sharedAssertions);\n'
        )
        self.assertEqual(self._tamper(content)["status"], "PASS")


class VerifierRewardWiringTest(unittest.TestCase):
    """evaluate_verifier -> calculate_reward, incl. DIFFERENTIAL participation."""

    @classmethod
    def setUpClass(cls):
        cls.verifier_engine = _load_flat("verifier_engine")
        cls.reward = _load_flat("reward")

    def _run(self, *, diff_status: str, diff_in_unit: bool = True):
        unit_ids = ["F2P-1", "P2P-1"] + (["DIFF-1"] if diff_in_unit else [])
        result = self.verifier_engine.evaluate_verifier(
            hardgate={"status": "PASS", "cheating": False},
            migration_units=[{"id": "MU001"}],
            p2p=[{"id": "P2P-1", "status": "PASS"}],
            f2p=[{"id": "F2P-1", "status": "PASS"}],
            differential=[{"id": "DIFF-1", "status": diff_status}],
            state={"applicable": False, "checks": []},
            weights=dict(_WEIGHTS),
            unit_checks={"MU001": {"observation_ids": unit_ids}},
            applicability=dict(_APPLICABILITY),
        )
        reward = self.reward.calculate_reward(
            hardgate={"status": result["hardgate"], "cheating": bool(result.get("cheating"))},
            verifier=result,
            reward_mode="VERIFIER_ONLY",
        )
        return result, reward

    def test_all_pass_scores_one(self):
        result, reward = self._run(diff_status="PASS")
        self.assertEqual(result["status"], "success")
        self.assertEqual(reward["branch"], "verifier_success")
        self.assertEqual(reward["value"], 1.0)

    def test_failing_differential_degrades_scope_and_reward(self):
        result, reward = self._run(diff_status="FAIL")
        self.assertNotEqual(result["status"], "success")
        self.assertLess(reward["value"], 1.0)
        self.assertEqual(result["components"]["regression_test"]["differential"]["status"], "FAIL")

    def test_required_group_without_weight_is_rejected(self):
        weights = dict(_WEIGHTS)
        del weights["differential"]
        with self.assertRaises(ValueError):
            self.verifier_engine.evaluate_verifier(
                hardgate={"status": "PASS", "cheating": False},
                migration_units=[{"id": "MU001"}],
                p2p=[{"id": "P2P-1", "status": "PASS"}],
                f2p=[{"id": "F2P-1", "status": "PASS"}],
                differential=[{"id": "DIFF-1", "status": "PASS"}],
                state={"applicable": False, "checks": []},
                weights=weights,
                unit_checks={"MU001": {"observation_ids": ["F2P-1", "P2P-1"]}},
                applicability=dict(_APPLICABILITY),
            )


class GraderArtifactWiringTest(unittest.TestCase):
    """The final verdict must survive serialization into the reward artifacts."""

    @classmethod
    def setUpClass(cls):
        cls.grader = _load_grader()
        cls.reward = _load_flat("reward")

    def test_cheating_verdict_is_serialized(self):
        reward = self.reward.calculate_reward(
            hardgate={"status": "FAIL", "cheating": True},
            verifier=_minimal_verifier(),
            reward_mode="VERIFIER_ONLY",
        )
        result = {"status": "failed", "cheating": True, "reward": reward, "receipts": {"commands": []}}
        with tempfile.TemporaryDirectory(prefix="security-selftest-") as directory:
            self.grader._write_reward_artifacts(Path(directory), result)
            details = json.loads((Path(directory) / "reward-details.json").read_text(encoding="utf-8"))
        self.assertTrue(details["cheating_detected"])
        self.assertEqual(details["reward"], -1.0)
        self.assertEqual(details["branch"], "cheating")


class SubmittedScopeBaselineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        grader = _load_grader()
        cls.entry = sys.modules[grader.run_harbor_verifier.__module__]

    def _git(self, root, *args):
        result = subprocess.run(
            ["git", "-C", str(root), "-c", "user.name=Scope regression",
             "-c", "user.email=scope@example.invalid", *args],
            capture_output=True, text=True, check=True,
        )
        return result.stdout.strip()

    def _fixture(self, directory, docs_source=None):
        source = Path(directory) / "source"
        source.mkdir()
        self._git(source, "init")
        for name in ("packages/core/src/utils.ts", "packages/core/src/types.ts",
                     "packages/react/src/helper.ts", "apps/docs/.source/dynamic.ts"):
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(docs_source if docs_source is not None and name == "apps/docs/.source/dynamic.ts"
                            else "baseline\n", encoding="utf-8")
        self._git(source, "add", ".")
        self._git(source, "commit", "-m", "trusted snapshot")
        base = self._git(source, "rev-parse", "HEAD")
        bundle = Path(directory) / "baseline.bundle"
        self._git(source, "bundle", "create", str(bundle), "HEAD")
        candidate = Path(directory) / "candidate"
        self._git(source, "clone", str(bundle), str(candidate))
        return candidate, bundle, base

    def test_missing_upstream_object_uses_trusted_snapshot_without_reset(self):
        with tempfile.TemporaryDirectory(prefix="scope-baseline-") as directory:
            candidate, bundle, _ = self._fixture(directory)
            path = candidate / "packages/core/src/utils.ts"
            path.write_text("candidate Rust facade\n", encoding="utf-8")
            head = self._git(candidate, "rev-parse", "HEAD")
            violations = self.entry._submitted_scope_violations(
                candidate, "0" * 40, baseline_bundle=bundle,
            )
            self.assertEqual(violations, {"outOfScope": [], "reactChanges": [], "docsChanges": []})
            self.assertEqual(self._git(candidate, "rev-parse", "HEAD"), head)
            self.assertEqual(path.read_text(), "candidate Rust facade\n")

    def test_committed_and_untracked_violations_still_fail(self):
        with tempfile.TemporaryDirectory(prefix="scope-baseline-") as directory:
            candidate, bundle, _ = self._fixture(directory)
            changed = ["apps/docs/.source/dynamic.ts", "packages/react/src/helper.ts",
                       "packages/core/src/types.ts"]
            for name in changed:
                (candidate / name).write_text("changed\n", encoding="utf-8")
            self._git(candidate, "add", ".")
            self._git(candidate, "commit", "-m", "candidate changes its HEAD")
            (candidate / "unexpected.txt").write_text("untracked\n", encoding="utf-8")
            violations = self.entry._submitted_scope_violations(
                candidate, "0" * 40, baseline_bundle=bundle,
            )
            self.assertEqual(sorted(violations["outOfScope"]), sorted([*changed, "unexpected.txt"]))
            self.assertEqual(violations["reactChanges"], ["packages/react/src/helper.ts"])
            self.assertEqual(violations["docsChanges"], ["apps/docs/.source/dynamic.ts"])

    def test_available_original_base_and_missing_bundle_fail_closed(self):
        with tempfile.TemporaryDirectory(prefix="scope-baseline-") as directory:
            candidate, bundle, base = self._fixture(directory)
            (candidate / "packages/core/src/types.ts").write_text("changed\n", encoding="utf-8")
            self.assertIn("packages/core/src/types.ts", self.entry._submitted_scope_violations(candidate, base)["outOfScope"])
            bundle.unlink()
            with self.assertRaisesRegex(ValueError, "baseline bundle"):
                self.entry._submitted_scope_violations(candidate, "0" * 40, baseline_bundle=bundle)

    def test_renaming_docs_into_core_does_not_hide_the_deletion(self):
        with tempfile.TemporaryDirectory(prefix="scope-baseline-") as directory:
            candidate, bundle, _ = self._fixture(directory)
            (candidate / "apps/docs/.source/dynamic.ts").rename(candidate / "packages/core/moved.ts")
            self._git(candidate, "add", ".")
            violations = self.entry._submitted_scope_violations(candidate, "0" * 40, baseline_bundle=bundle)
            self.assertEqual(violations["docsChanges"], ["apps/docs/.source/dynamic.ts"])

    def test_only_exact_initial_generator_relocation_is_exempt(self):
        source = '''const generated = create(Config, {"configPath":"/original/project/apps/docs/source.config.ts","environment":"vite","outDir":"/original/project/apps/docs/.source"});'''
        with tempfile.TemporaryDirectory(prefix="scope-generator-") as directory:
            candidate, bundle, base = self._fixture(directory, docs_source=source)
            file = candidate / "apps/docs/.source/dynamic.ts"
            relocated = source.replace("/original/project", "/workspace/repo")
            file.write_text(relocated, encoding="utf-8")
            violations = self.entry._submitted_scope_violations(candidate, base, baseline_bundle=bundle)
            self.assertEqual(violations, {"outOfScope": [], "reactChanges": [], "docsChanges": []})
            self.assertEqual(file.read_text(), relocated, "scope inspection must not rewrite the candidate")
            for changed in (relocated + "\n// candidate edit\n",
                            relocated.replace('"vite"', '"changed"'),
                            relocated.replace("/workspace/repo/apps/docs/.source", "/tmp/other")):
                file.write_text(changed, encoding="utf-8")
                violations = self.entry._submitted_scope_violations(candidate, base, baseline_bundle=bundle)
                self.assertEqual(violations["docsChanges"], ["apps/docs/.source/dynamic.ts"])
            file.unlink()
            file.symlink_to(Path(directory) / "source/apps/docs/.source/dynamic.ts")
            self.assertFalse(self.entry._is_initial_docs_relocation(candidate, base, "apps/docs/.source/dynamic.ts"))

    def test_rust_build_ignore_configuration_is_within_scope(self):
        with tempfile.TemporaryDirectory(prefix="scope-build-ignore-") as directory:
            candidate, bundle, base = self._fixture(directory)
            (candidate / ".gitignore").write_text("# Rust\ntarget\n", encoding="utf-8")
            self.assertEqual(self.entry._submitted_scope_violations(candidate, base, baseline_bundle=bundle)["outOfScope"], [])


class EquivalentMigrationEvidenceTest(unittest.TestCase):
    def test_strengthened_assertions_and_aliases(self):
        evidence = _load_flat("evidence")
        baseline = '''
it("copy", () => {
 const data = { first: { saved: true } };
 expect(getInitialData<typeof steps>(data)).toEqual(data);
 expect(getInitialData<typeof steps>(data)).not.toBe(data);
});
it("match", () => {
 expect(() => matchStep(steps, "third", { first: (step: any) => step.label } as any)).toThrow(/No match handler/);
});
'''
        strengthened = '''
it("copy", () => {
 const data = { first: { saved: true } };
 const copy = getInitialData<typeof steps>(data);
 expect(copy).toEqual(data);
 expect(copy).not.toBe(data);
 expect(copy.first).toBe(data.first);
});
it("match", () => {
 const firstHandler = vi.fn((step: any) => step.label);
 expect(() => matchStep(steps, "third", { first: firstHandler, second: firstHandler } as any)).toThrow(/No match handler/);
 expect(firstHandler).not.toHaveBeenCalled();
});
'''
        expected = evidence._assertion_signatures(baseline)
        import re
        protected = set(re.findall(r"\b[A-Za-z_$][\w$]*\b", " ".join(expected)))
        actual = evidence._assertion_signatures(evidence._expand_test_aliases(strengthened, protected))
        wanted = {evidence._canonical_assertion(value) for value in expected}
        self.assertTrue(wanted.issubset({evidence._canonical_assertion(value) for value in actual}))
        weakened = strengthened.replace(".not.toBe(data)", ".toBeDefined()")
        actual = evidence._assertion_signatures(evidence._expand_test_aliases(weakened, protected))
        self.assertFalse(wanted.issubset({evidence._canonical_assertion(value) for value in actual}))

    def test_loader_search_and_rust_build_are_not_source_fallback(self):
        residue = _load_flat("residue")
        with tempfile.TemporaryDirectory(prefix="loader-selftest-") as directory:
            root = Path(directory)
            path = root / "utils.ts"
            rule = {"id": "lookup", "kind": "step_lookup", "paths": ["utils.ts"], "target": "find("}
            path.write_text('const addon = candidates.find((candidate) => existsSync(candidate));')
            self.assertEqual(residue.scan_residue_rules(root, [rule]), [])
            path.write_text('const found = steps.find((step) => step.id === id);')
            self.assertTrue(residue.scan_residue_rules(root, [rule]))
        self.assertTrue(residue._rust_build_only('cp.execFileSync("cargo", ["build"]);'))
        self.assertFalse(residue._rust_build_only('cp.execFileSync("cargo", ["build"]); cp.execFileSync("node", ["legacy.js"]);'))
        self.assertFalse(residue._rust_build_only('cp.execFileSync("cargo", ["build"]); cp[method]("node", ["legacy.js"]);'))

    def test_submitted_docs_changes_override_generated_file_exemption(self):
        saved = sys.path[:]
        sys.path[:] = [str(_TESTS_ROOT), *(entry for entry in sys.path if entry not in ("", str(_VERIFIER_DIR)))]
        try:
            entry = importlib.import_module("verifier.harbor_entry")
        finally:
            sys.path[:] = saved
        rows = [{"id": "DIFF-031", "status": "PASS"}, {"id": "DIFF-033", "status": "PASS"}]
        entry._apply_submitted_scope(rows, {"outOfScope": ["apps/docs/.source/dynamic.ts"], "docsChanges": ["apps/docs/.source/dynamic.ts"]})
        self.assertTrue(all(row["status"] == "FAIL" for row in rows))


if __name__ == "__main__":
    unittest.main()
