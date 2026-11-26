"""Focused security/adapter regressions; run in the verifier image."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

TESTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS / "assets/shared/integration"))
from verifier.detectors.runtime_identity import source_language, process_runtime_tokens, native_runtime_signals
from verifier.detectors.subprocess import detect_source_runtime_subprocess
from verifier.evidence import _test_integrity, _digest, _assertion_signature
from verifier.integrity import validate_public_integrity
from public_behavior import PublicBehavior, outputs, F2P, P2P, CONTRACT, SCENARIOS


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = {"source_technology": {"name": "TypeScript", "language_name": "TypeScript"}}

    def tearDown(self):
        self.temp.cleanup()

    def scan(self, source, path="main.go"):
        self.assertIsNotNone(shutil.which("migration-go-process-scan"))
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source)
        return detect_source_runtime_subprocess(self.root, self.config)

    def test_config_schemas_have_runtime_identity(self):
        for config in (self.config, {"migration": {"source": self.config["source_technology"]}}):
            self.assertEqual(source_language(config), "typescript")
            self.assertIn("node", process_runtime_tokens(config))
            self.assertIn("libnode", native_runtime_signals(config))

    def test_process_sinks_and_aliases(self):
        cases = [
            'import "os";func main(){n:="no"+"de";os.StartProcess(n,[]string{n,"bundle"},nil)}',
            'import "syscall";func main(){syscall.Exec("/usr/bin/node",nil,nil)}',
            'import "syscall";func main(){syscall.ForkExec("node",nil,nil)}',
            'import r "os/exec";func main(){r.Command("node","bundle").Run()}',
            'import(r "os/exec";"context");func main(){r.CommandContext(context.Background(),"node").Run()}',
            'import "os/exec";func main(){n,_:=exec.LookPath("node");exec.Command(n).Run()}',
            'import "os/exec";func main(){c:=exec.Command;c("node").Run()}',
            'import "os/exec";func main(){exec.Command("sh","-c","no"+"de bundle").Run()}',
            'import "os/exec";func main(){c:=exec.Cmd{Path:"node"};c.Run()}',
        ]
        for source in cases:
            with self.subTest(source=source):
                self.assertTrue(self.scan("package main\n" + source))

    def test_cross_file_constants_and_subpackages(self):
        (self.root / "internal/runner").mkdir(parents=True)
        (self.root / "internal/runner/runtime.go").write_text('package runner\nconst runtime = "no"+"de"')
        hits = self.scan('package runner\nimport "os"\nfunc Run(){os.StartProcess(runtime,nil,nil)}', "internal/runner/run.go")
        self.assertEqual(hits[0]["path"], "internal/runner/run.go")

    def test_legal_subprocess_and_inactive_residue(self):
        hits = self.scan('''package main
import "os/exec"
// exec.Command("node", "bundle") is an example, not executable code.
const documentation = "node"
func main(){exec.Command("git", "version").Run()}
''')
        self.assertEqual(hits, [])
        (self.root / "example_test.go").write_text('package main\nimport "os/exec"\nfunc Example(){exec.Command("node").Run()}')
        self.assertEqual(detect_source_runtime_subprocess(self.root, self.config), [])

    def baseline(self):
        p = self.root / "__tests__/Inputs.test.ts"
        p.parent.mkdir(exist_ok=True)
        p.write_text('expect(tag).toEqual("v1")\n')
        return p, {"files": {"__tests__/Inputs.test.ts": {"sha256": _digest(p), "assertions": _assertion_signature(p.read_text())}}, "marker_counts": {}}

    def test_oracle_added_tests_are_not_source_protected(self):
        _, baseline = self.baseline()
        (self.root / "renamed_test.go").write_text('package action\n')
        tamper, _ = _test_integrity(self.root, baseline, {})
        self.assertEqual(tamper["status"], "PASS")

    def test_weakened_source_assertions_still_fail(self):
        p, baseline = self.baseline()
        p.write_text('expect(tag).toBeDefined()\n')
        tamper, _ = _test_integrity(self.root, baseline, {})
        self.assertEqual(tamper["status"], "FAIL")

    def test_replacement_requires_execution_and_public_behavior(self):
        p, baseline = self.baseline(); p.unlink()
        config = {"policy": {"allow_go_test_replacement": True}}
        evidence = [{"id": "F2P-060", "kind": "F2P", "status": "PASS", "value": {"type": "json", "value": {"executed_assertion_suite": True}}},
                    {"id": "P2P-001", "kind": "P2P", "status": "PASS"}]
        tamper, _ = _test_integrity(self.root, baseline, config)
        self.assertEqual(tamper["status"], "FAIL")
        tamper, _ = _test_integrity(self.root, baseline, config, observations=evidence)
        self.assertEqual(tamper["status"], "PASS")
        evidence[1]["status"] = "FAIL"
        tamper, _ = _test_integrity(self.root, baseline, config, observations=evidence)
        self.assertEqual(tamper["status"], "INCONCLUSIVE")
        self.assertTrue(tamper["evidence"]["replacement_equivalence_pending"])

    def test_unexecuted_go_skip_is_not_an_actual_skipped_check(self):
        _, baseline = self.baseline()
        (self.root / "platform_test.go").write_text(
            'package main\nfunc TestPlatform(t *testing.T){if runtime.GOOS != "linux" {t.Skip("Linux fixture")}; t.Fatal("assertion")}')
        evidence = [{"id": "F2P-060", "kind": "F2P", "status": "PASS",
                     "value": {"type": "json", "value": {"executed_assertion_suite": True}}}]
        _, skip = _test_integrity(self.root, baseline, {}, observations=evidence)
        self.assertEqual(skip["status"], "PASS")
        self.assertTrue(skip["evidence"]["go_skips_verified_inactive"])
        evidence[0]["status"] = "FAIL"
        _, skip = _test_integrity(self.root, baseline, {}, observations=evidence)
        self.assertEqual(skip["status"], "FAIL")

    def test_new_skip_marker_is_rejected(self):
        _, baseline = self.baseline()
        (self.root / "new_test.go").write_text('package main\nfunc TestNew(t *testing.T){t.Skip("bypass")}')
        _, skip = _test_integrity(self.root, baseline, {})
        self.assertEqual(skip["status"], "FAIL")

    def test_go_business_skip_field_is_not_a_disabled_test(self):
        _, baseline = self.baseline()
        test = self.root / "action_test.go"
        test.write_text('package main\nfunc TestSkip(t *testing.T){f.skipper.skip = true; t.Log("t.Skip()")}')
        _, skip = _test_integrity(self.root, baseline, {})
        self.assertEqual(skip["status"], "PASS")
        test.write_text('package main\nfunc TestSkip(check *testing.T){check.Skip("bypass")}')
        _, skip = _test_integrity(self.root, baseline, {})
        self.assertEqual(skip["status"], "FAIL")

    def test_vacuous_native_suite_is_rejected(self):
        repo = self.root / "repo"; repo.mkdir()
        (repo / "go.mod").write_text("module example.test/migration\ngo 1.23.0\n")
        (repo / "main.go").write_text("package main\nfunc main(){}")
        engine = PublicBehavior(repo, self.root / "work")
        try:
            with self.assertRaises(AssertionError):
                engine.native_tests()
            (engine.mod / "empty_test.go").write_text('package main\nimport "testing"\nfunc TestNothing(t *testing.T){}')
            with self.assertRaises(AssertionError):
                engine.native_tests()
        finally:
            engine.fixture.close()

    def test_observation_mapping_has_no_missing_behavior(self):
        self.assertEqual(set(F2P), set(range(1, 73)))
        self.assertEqual(set(P2P), set(range(1, 41)))
        for names in [*F2P.values(), *P2P.values()]:
            self.assertTrue(names and set(names) <= set(SCENARIOS))

    def test_workflow_output_delimiters_are_not_fixed(self):
        self.assertEqual(outputs("id=42\nassets<<random\n{}\nrandom\n"), {"id": "42", "assets": "{}"})

    # --- release-action verifier repairs (Tasks 4 & 5) ---------------------

    def test_empty_artifact_requires_wire_length_and_assets_output(self):
        engine = PublicBehavior.__new__(PublicBehavior)
        engine.work = self.root
        broken = None

        def uploaded(inputs):
            file = Path(inputs["artifacts"])
            self.assertEqual(file.read_bytes(), b"")
            headers = {"Content-Length": "0"}
            if broken == "unknown-length":
                headers = {"Transfer-Encoding": "chunked"}
            elif broken == "wrong-length":
                headers["Content-Length"] = "1"
            elif broken == "chunked":
                headers["Transfer-Encoding"] = "chunked"
            row = {"query": {"name": [file.name]}, "raw": b"", "headers": headers}
            assets = {file.name: "https://download.test/" + file.name}
            if broken == "body":
                row["raw"] = b"x"
            if broken == "output":
                assets = {}
            return ([] if broken == "skipped" else [row]), assets, [], ""

        engine.uploaded = uploaded
        engine.empty_artifacts()
        for broken in ("unknown-length", "wrong-length", "chunked", "body", "output", "skipped"):
            with self.subTest(broken=broken), self.assertRaises(AssertionError):
                engine.empty_artifacts()
        self.assertIn("empty_artifacts", SCENARIOS)
        self.assertIn("empty_artifacts", F2P[63])
        self.assertIn("empty_artifacts", P2P[13])
        self.assertIn("empty_artifacts", CONTRACT[18])

    def test_glob_classes_scenario_is_frozen(self):
        self.assertIn("glob_classes", SCENARIOS)
        cases = dict(PublicBehavior.POSIX_CLASS_CASES)
        self.assertEqual(cases["[[:digit:]].zip"], ("1.zip",))
        self.assertEqual(sorted(cases["[[:alpha:]].zip"]), ["Z.zip", "a.zip", "é.zip"])
        self.assertEqual(sorted(cases["[![:digit:]].zip"]), ["Z.zip", "a.zip", "é.zip"])
        self.assertEqual(sorted(cases["[![:graph:]a].zip"]), ["1.zip", "Z.zip", "é.zip"])
        self.assertEqual(cases["[z-aa].zip"], ("a.zip",))
        self.assertEqual(cases["[z-a].zip"], ())
        self.assertIn("glob_classes", F2P[26])
        self.assertIn("glob_classes", P2P[15])

    def test_extended_glob_checks_reject_substring_negation_and_lost_dotfiles(self):
        engine = PublicBehavior.__new__(PublicBehavior)
        engine.work = self.root
        cases = dict(PublicBehavior.POSIX_CLASS_CASES + PublicBehavior.LITERAL_BRACKET_CASES
                     + PublicBehavior.EXTENDED_GLOB_CASES + PublicBehavior.ESCAPED_GLOB_CASES
                     + PublicBehavior.LITERAL_BRACE_CASES)

        def uploaded(inputs, broken=None):
            pattern = Path(inputs["artifacts"]).name
            names = list(cases[pattern])
            if broken == "negation" and pattern == "!(a)*.zip":
                names.append("ab.zip")
            if broken == "dotfile" and pattern == "@(.hidden|a).zip":
                names.remove(".hidden.zip")
            if broken == "nested_negation" and pattern == "@(a!(b)|b).zip":
                names.append("ab.zip")
            if broken == "empty_alternative" and pattern == "!(a|).zip":
                names.remove("a.zip")
            if broken == "nested_alternative" and pattern == "!(@(a|b)).zip":
                names.remove("b.zip")
            if broken == "unfinished_bracket" and pattern == "literal[.zip":
                raise AssertionError("malformed pattern")
            if broken == "empty_bracket" and pattern == "[].zip":
                names = []
            if broken == "negated_class_union" and pattern == "[![:graph:]a].zip":
                names = []
            if broken == "descending_range" and pattern == "[z-aa].zip":
                names = []
            if broken == "escaped_open_brace" and pattern == r"\{1..2}.zip":
                names = ["1.zip", "2.zip"]
            if broken == "escaped_close_brace" and pattern == r"{1..2\}.zip":
                names = []
            if broken == "escaped_nested_brace" and pattern == r"\{1..2\}{3..4}.zip":
                names = ["13.zip", "14.zip", "23.zip", "24.zip"]
            if broken == "escaped_alternative" and pattern == r"@(a\|b|c).zip":
                names = ["a|b.zip", "c.zip"]
            if broken == "escaped_close_group" and pattern == r"@(a\)|c).zip":
                names = []
            if broken == "escaped_open_group" and pattern == r"@(a\(b|c).zip":
                names = []
            if broken == "dollar_brace_expansion" and pattern == "${1..2}.zip":
                names = ["$1.zip", "$2.zip"]
            if broken == "nested_dollar_brace_expansion" and pattern == "${{1..2}}.zip":
                names = ["${1}.zip", "${2}.zip"]
            if broken == "nested_brace_expansion" and pattern == "{{1..2}}.zip":
                names = ["{1}.zip", "{2}.zip"]
            if broken == "lost_brace_suffix_expansion" and pattern == "${a}{1..2}.zip":
                names = []
            if broken == "expanded_nested_prefix" and pattern == "{{1..2}}{1..2}.zip":
                names = ["{{1..2}}1.zip", "{{1..2}}2.zip"]
            return ([{"query": {"name": [n]}} for n in names], {n: "url" for n in names}, [], "")

        engine.uploaded = uploaded
        engine.glob_classes()
        for broken in ("negation", "dotfile", "nested_negation", "empty_alternative",
                       "nested_alternative", "unfinished_bracket", "empty_bracket",
                       "negated_class_union", "descending_range", "escaped_open_brace",
                       "escaped_close_brace", "escaped_nested_brace", "escaped_alternative",
                       "escaped_close_group", "escaped_open_group", "dollar_brace_expansion",
                       "nested_dollar_brace_expansion", "nested_brace_expansion",
                       "lost_brace_suffix_expansion", "expanded_nested_prefix"):
            engine.uploaded = lambda inputs, broken=broken: uploaded(inputs, broken)
            with self.assertRaises(AssertionError):
                engine.glob_classes()

    def test_literal_bracket_failure_policy_is_checked(self):
        engine = PublicBehavior.__new__(PublicBehavior)
        engine.work = self.root
        broken = None
        exercised = []

        def uploaded(inputs):
            pattern = Path(inputs["artifacts"]).name
            exercised.append((pattern, False))
            if broken == "eager-error" and "[" in pattern:
                raise AssertionError("unexpected glob syntax error")
            names = ["nested["] if broken == "upload-directory" and pattern == "nested[" else []
            return names, {}, [], "::warning::invalid artifact"

        def invoke(inputs):
            pattern = Path(inputs["artifacts"]).name
            exercised.append((pattern, True))
            rc = 0 if broken == "ignore-strict" and "[" in pattern else 1
            return rc, "", {}, []

        engine.uploaded, engine.invoke = uploaded, invoke
        engine.invalid_artifacts()
        for pattern in ("absent[.zip", "nested["):
            self.assertIn((pattern, False), exercised)
            self.assertIn((pattern, True), exercised)
        for broken in ("eager-error", "upload-directory", "ignore-strict"):
            with self.subTest(broken=broken), self.assertRaises(AssertionError):
                engine.invalid_artifacts()

    def test_composite_go_flags_and_workflow_bindings(self):
        engine = PublicBehavior.__new__(PublicBehavior)
        engine.mod = self.root
        action = self.root / "action.yml"
        valid = '''inputs:
  tag:
    required: false
outputs:
  id:
    value: ${{ steps.release.outputs.id }}
runs:
  using: composite
  steps:
    - id: release
      shell: bash
      env:
        INPUT_TAG: ${{ inputs.tag }}
      run: go -C "$GITHUB_ACTION_PATH" run .
'''
        action.write_text(valid)
        engine.entry()
        action.write_text(valid.replace('${{ inputs.tag }}', ''))
        with self.assertRaisesRegex(AssertionError, 'does not forward input tag'):
            engine.entry()
        action.write_text(valid.replace('    value: ${{ steps.release.outputs.id }}\n', ''))
        with self.assertRaisesRegex(AssertionError, 'no value mapping'):
            engine.entry()

    def test_contract_and_differential_are_required(self):
        config = json.loads((TESTS / "config.json").read_text())
        self.assertEqual(config["group_applicability"]["CONTRACT"]["status"], "required")
        self.assertEqual(config["group_applicability"]["DIFFERENTIAL"]["status"], "required")
        self.assertEqual(
            {row["id"] for row in config["tests"]["contract"]},
            {"CONTRACT-%03d" % i for i in range(1, 32)},
        )
        self.assertEqual(
            {row["id"] for row in config["tests"]["differential"]},
            {"DIFF-%03d" % i for i in range(1, 32)},
        )

    def test_differential_corpus_covers_frozen_expectations(self):
        corpus = json.loads(
            (TESTS / "assets/shared/batches/BATCH-DIFFERENTIAL-0001/corpus/cases.json").read_text()
        )["cases"]
        expected = json.loads(
            (TESTS / "assets/fixtures/batches/BATCH-DIFFERENTIAL-0001/expected.json").read_text()
        )["cases"]
        self.assertEqual({case["id"] for case in corpus}, set(expected))

    def test_all_migration_units_are_scoped(self):
        from verifier.verifier_engine import evaluate_verifier
        config = json.loads((TESTS / "config.json").read_text())
        units = json.loads((TESTS / "assets/migration-units.json").read_text())["units"]
        baseline = json.loads((TESTS / "assets/source-baseline-outputs.json").read_text())["observations"]
        p2p = [{"id": row["id"], "kind": "P2P", "status": "PASS", "value": row.get("value")}
               for row in baseline if row["kind"] == "P2P"]
        f2p = [{"id": row["id"], "kind": "F2P", "status": "PASS", "value": row.get("value")}
               for row in baseline if row["kind"] == "F2P"]
        result = evaluate_verifier(
            hardgate={"status": "PASS"},
            migration_units=units,
            unit_checks=config["migration_unit_checks"],
            p2p=p2p,
            f2p=f2p,
            differential=[],
            contract=[],
            state={"applicable": False, "checks": [], "roles": {}},
            weights=config["weights"],
            applicability={
                "P2P": {"status": "required"}, "F2P": {"status": "required"},
                "DIFFERENTIAL": {"status": "na"}, "CONTRACT": {"status": "na"},
                "STATE": {"status": "na"},
            },
        )
        scope = result["components"]["migration_scope_complete"]
        self.assertEqual({row["id"] for row in scope["checks"]}, {unit["id"] for unit in units})
        self.assertEqual(result["diagnostics"]["scope_excluded_units"], [])

    def test_native_collector_import_and_executor_signature(self):
        from verifier.harbor_entry import _LocalCollectorExecutor
        from verifier.native_test_collector import collect_native_test_results
        repo = self.root / "candidate"; repo.mkdir()
        (repo / "probe_test.go").write_text("package main\n")
        xml = ('<testsuites><testsuite name="x"><testcase file="probe_test.go" '
               'classname="x" name="TestThing"></testcase></testsuite></testsuites>')
        program = "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[2])"
        runtime = {
            "technology": {"language": {"name": "Go"}, "framework": {"name": "Go"}},
            "native_test_commands": [["python3", "-c", program, "{report}", xml]],
        }
        collection = collect_native_test_results(
            role="target", runtime=runtime, repo_root=repo,
            executor=_LocalCollectorExecutor({}), image=None, assets_root=repo,
            evidence_root=self.root / "evidence", timeout=30, targets=("probe_test.go",),
        )
        self.assertEqual(collection.adapter, "contract-junit")
        self.assertTrue(collection.available, collection.as_dict())
        self.assertEqual([result.status for result in collection.results], ["PASS"])

    def test_collector_failure_fails_closed(self):
        from verifier.harbor_entry import _fail_closed_native_p2p
        rows = [
            {"id": "P2P-001", "kind": "P2P", "status": "PASS", "value": {"type": "json", "value": {"outcome": "PASS"}}},
            {"id": "F2P-001", "kind": "F2P", "status": "PASS", "value": {"type": "json", "value": {"result": "PASS"}}},
        ]
        result = _fail_closed_native_p2p(rows, "boom")
        self.assertEqual(result[0]["status"], "INCONCLUSIVE")
        self.assertEqual(result[1]["status"], "PASS")

    # --- release-action round-1 review repairs ------------------------------

    def test_contract_group_is_public_and_covers_all_ids(self):
        self.assertEqual(set(CONTRACT), set(range(1, 32)))
        for names in CONTRACT.values():
            self.assertTrue(names and set(names) <= set(SCENARIOS))

    def test_skip_file_checks_reject_eager_reads_and_ignored_errors(self):
        engine = PublicBehavior.__new__(PublicBehavior)
        engine.work = self.root
        lookup = {"method": "GET", "path": "/repos/ctx-owner/ctx-repo/releases/tags/ctx-tag"}
        broken = None

        def success(inputs):
            if broken == "body" and "bodyFile" in inputs:
                raise AssertionError("eager body-file read")
            if broken == "artifacts" and ("artifacts" in inputs or "artifact" in inputs):
                raise AssertionError("eager strict glob")
            return 0, "::notice::Skipping action", {}, [lookup]

        def invoke(inputs, state):
            return (0 if broken == "ignore-errors" else 1), "", {}, [lookup]

        engine.success, engine.invoke = success, invoke
        engine.skip_files()
        for broken in ("body", "artifacts", "ignore-errors"):
            with self.subTest(broken=broken), self.assertRaises(AssertionError):
                engine.skip_files()
        self.assertIn("skip_files", F2P[54])
        self.assertIn("skip_files", P2P[18])
        self.assertIn("skip_files", CONTRACT[14])

    def test_omitted_body_checks_distinguish_update_from_create_fallback(self):
        from types import SimpleNamespace
        engine = PublicBehavior.__new__(PublicBehavior)
        engine.work = self.root
        engine.fixture = SimpleNamespace(release=lambda **fields: fields)
        broken = None

        def created(inputs):
            if broken == "eager":
                raise AssertionError("eager body-file read")
            return {"data": {}}, {}, []

        def success(inputs, state=None):
            if broken == "eager-update":
                raise AssertionError("eager update body-file read")
            release_id = 51 if state else 42
            return 0, "", {}, [{"method": "PATCH", "path": "/repos/o/r/releases/%d" % release_id, "data": {}}]

        def invoke(inputs, state=None):
            calls = [{"method": "GET", "path": "/repos/o/r/releases"}] if state else []
            return (0 if broken == "omit-create" else 1), "", {}, calls

        engine.created, engine.success, engine.invoke = created, success, invoke
        engine.omitted_body_files()
        for broken in ("eager", "eager-update", "omit-create"):
            with self.subTest(broken=broken), self.assertRaises(AssertionError):
                engine.omitted_body_files()
        self.assertIn("omitted_body_files", F2P[51])
        self.assertIn("omitted_body_files", P2P[36])
        self.assertIn("omitted_body_files", CONTRACT[25])

    def test_contract_whitebox_go_probe_is_no_longer_dispatched(self):
        source = (TESTS / "assets/shared/integration/runner.py").read_text()
        self.assertNotIn("dispatch_contract_target", source)
        self.assertNotIn("TestContractPublicSurface", source)

    def test_differential_main_package_discovery(self):
        sys.path.insert(0, str(TESTS / "assets/shared/batches/BATCH-DIFFERENTIAL-0001/lib"))
        import harness
        packages = [
            {"Name": "main", "ImportPath": "example/cmd/release-action", "Dir": "/repo/cmd/release-action"},
            {"Name": "action", "ImportPath": "example/src", "Dir": "/repo/src"},
            {"Name": "main", "ImportPath": "example/tools/other", "Dir": "/repo/tools/other"},
        ]
        self.assertEqual(
            harness.select_main_package(
                packages,
                action_text="runs:\n  main: './cmd/release-action'",
                docker_text="",
                repo_root="/repo",
            ),
            "example/cmd/release-action",
        )
        self.assertEqual(
            harness.select_main_package(
                [{"Name": "main", "ImportPath": "example", "Dir": "/repo"}], repo_root="/repo"
            ),
            "example",
        )
        self.assertIsNone(
            harness.select_main_package(
                [{"Name": "lib", "ImportPath": "example/src", "Dir": "/repo/src"}], repo_root="/repo"
            )
        )

    def test_differential_failure_is_reported_but_not_blocking(self):
        from verifier.verifier_engine import evaluate_verifier
        config = json.loads((TESTS / "config.json").read_text())
        units = json.loads((TESTS / "assets/migration-units.json").read_text())["units"]
        baseline = json.loads((TESTS / "assets/source-baseline-outputs.json").read_text())["observations"]

        def rows(kind):
            return [
                {"id": row["id"], "kind": kind, "status": "PASS", "value": row.get("value")}
                for row in baseline if row["kind"] == kind
            ]

        result = evaluate_verifier(
            hardgate={"status": "PASS"},
            migration_units=units,
            unit_checks=config["migration_unit_checks"],
            p2p=rows("P2P"),
            f2p=rows("F2P"),
            differential=[{"id": "DIFF-010", "kind": "DIFFERENTIAL", "status": "FAIL",
                            "value": {"type": "json", "value": {"result": "FAIL"}}}],
            contract=[{"id": "CONTRACT-001", "kind": "CONTRACT", "status": "PASS",
                       "value": {"type": "json", "value": {"result": "PASS"}}}],
            state={"applicable": False, "checks": [], "roles": {}},
            weights=config["weights"],
            applicability=config["group_applicability"],
        )
        self.assertEqual(result["components"]["regression_test"]["differential"]["status"], "FAIL")
        self.assertEqual(result["status"], "success")


class BundleIntegrityTests(unittest.TestCase):
    def test_public_bundle_matches_manifest(self):
        validate_public_integrity(TESTS)

    def test_changed_asset_is_rejected(self):
        # Keep fixtures outside the bundle whose inventory is being checked.
        with tempfile.TemporaryDirectory(prefix="integrity-regression-", dir=TESTS.parent) as directory:
            root = Path(directory)
            (root / "assets").mkdir()
            asset = root / "probe.txt"
            data = b"original\n"
            asset.write_bytes(data)
            manifest = {
                "schema_version": "migration-public-assets:v1",
                "files": [{
                    "path": "probe.txt",
                    "sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
                    "size": len(data),
                }],
            }
            (root / "assets/assets-manifest.json").write_text(json.dumps(manifest))
            validate_public_integrity(root)
            asset.write_bytes(b"modified\n")
            with self.assertRaisesRegex(ValueError, "public integrity asset changed: probe.txt"):
                validate_public_integrity(root)


if __name__ == "__main__":
    unittest.main()
