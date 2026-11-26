"""Two-sided semantic regressions for the rubyfmt verifier (no Go required)."""
from __future__ import annotations

import difflib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS / "assets/shared"))
sys.path.insert(0, str(TESTS))
from diff_contract import canonical_diff, diff_matches, validate_diff, DiffError
from verifier import security_selfcheck as sc


def load_probe(relative, name):
    spec = importlib.util.spec_from_file_location(name, TESTS / "assets" / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def unified(path, before, after, variant="canonical", context=3):
    """Independent test producer, including legal and historical bad output."""
    rows = difflib.diff_bytes(difflib.unified_diff, before.splitlines(keepends=True),
                             after.splitlines(keepends=True), fromfile=path.encode(),
                             tofile=path.encode(), n=context)
    output = b"".join(row if row.endswith(b"\n") else row + b"\n\\ No newline at end of file\n" for row in rows)
    if variant == "equivalent":
        output = output.replace(b"@@ -1 +1 @@", b"@@ -1,1 +1,1 @@ section")
        output = output.replace(b"--- " + path.encode() + b"\n", b"--- " + path.encode() + b"\t2026-11-12 00:00:00 +0000\n")
        output = output.replace(b"+++ " + path.encode() + b"\n", b"+++ " + path.encode() + b"\t2026-11-12 00:00:01 +0000\n")
    elif variant == "phantom":
        output = output.replace(b"@@ -1 +1 @@", b"@@ -1,2 +1,2 @@") + b" \n"
    elif variant == "missing_marker":
        output = output.replace(b"\\ No newline at end of file\n", b"")
    return output


class UnifiedDiffContractTests(unittest.TestCase):
    before = b"a 1,2,3\n"
    after = b"a(1, 2, 3)\n"

    def test_equivalent_counts_dates_and_headings_are_accepted(self):
        changes = [("test.rb", self.before, self.after)]
        for variant in ("canonical", "equivalent"):
            with self.subTest(variant=variant):
                output = unified("test.rb", self.before, self.after, variant)
                validate_diff(output, changes)
                self.assertEqual(canonical_diff(output, changes), unified("test.rb", self.before, self.after))

    def test_context_size_and_split_hunks_are_semantic(self):
        before = b"one\ntwo\nthree\nfour\nfive\nsix\nseven\n"
        after = before.replace(b"two\n", b"TWO\n").replace(b"six\n", b"SIX\n")
        changes = [("nested/file.rb", before, after)]
        for context in (0, 1, 3, 20):
            with self.subTest(context=context):
                output = unified("nested/file.rb", before, after, context=context)
                self.assertTrue(diff_matches(output, changes))
                self.assertEqual(canonical_diff(output, changes), unified("nested/file.rb", before, after))

    def test_insert_delete_empty_files_and_missing_final_newlines(self):
        for before, after in ((b"", b"hello\n"), (b"hello\n", b""),
                              (b"hello\n", b"hello\nworld\n"),
                              (b"hello\nworld\n", b"hello\n"),
                              (b"hello", b"hello\n"), (b"hello\n", b"hello"),
                              (b"hello", b"world"), (b"", b"world"),
                              (b"hello", b"")):
            with self.subTest(before=before, after=after):
                output = unified("stdin", before, after)
                self.assertTrue(diff_matches(output, [("stdin", before, after)]))
                self.assertEqual(canonical_diff(output, [("stdin", before, after)]), output)

    def test_header_like_body_bytes_are_not_parsed_as_file_headers(self):
        before, after = b"-- old\n", b"++ new\n"
        self.assertTrue(diff_matches(unified("test.rb", before, after), [("test.rb", before, after)]))

    def test_non_lf_control_bytes_and_unicode_are_preserved(self):
        for before, after in ((b"a\r\v\x0c\x85\n", b"b\r\v\x0c\x85\n"),
                              ("\u540d\u5b57\n".encode(), "\u540d\u79f0\n".encode())):
            output = b"--- name.rb\n+++ name.rb\n@@ -1 +1 @@\n-" + before + b"+" + after
            self.assertTrue(diff_matches(output, [("name.rb", before, after)]))
            self.assertTrue(diff_matches(canonical_diff(output, [("name.rb", before, after)]), [("name.rb", before, after)]))

    def test_wrong_paths_ranges_counts_body_and_extra_output_are_rejected(self):
        good = unified("test.rb", self.before, self.after)
        variants = [
            good.replace(b"--- test.rb", b"--- wrong.rb"),
            good.replace(b"+++ test.rb", b"+++ wrong.rb"),
            good.replace(b"@@ -1 +1 @@", b"@@ -2 +1 @@"),
            good.replace(b"@@ -1 +1 @@", b"@@ -1 +2 @@"),
            good.replace(b"@@ -1 +1 @@", b"@@ -0 +1 @@"),
            good.replace(b"@@ -1 +1 @@", b"@@ -1,2 +1 @@"),
            good.replace(b"@@ -1 +1 @@", b"@@ -1 +1,2 @@"),
            good.replace(b"@@ -1 +1 @@", b"@@ -1,0 +1,0 @@"),
            good.replace(b"-a 1,2,3", b"-b 1,2,3"),
            good.replace(b"+a(1, 2, 3)", b"+a(9, 2, 3)"),
            b"debug\n" + good, good + b"debug\n", good + good,
            good[:-1], b"", good + b"\\ No newline at end of file\n",
            good.replace(b"@@ -1 +1 @@", b"@@ -" + b"9" * 5000 + b" +1 @@"),
            unified("test.rb", self.before, self.after, "phantom"),
        ]
        for output in variants:
            with self.subTest(output=output[:100]):
                self.assertFalse(diff_matches(output, [("test.rb", self.before, self.after)]))
                self.assertTrue(canonical_diff(output, [("test.rb", self.before, self.after)]).startswith(b"INVALID UNIFIED DIFF\n"))

    def test_newline_markers_cannot_hide_a_wrong_line_count(self):
        # Joining these malformed additions gives the right bytes but the
        # advertised new line count is false: a missing newline must be at EOF.
        invalid = b"--- test.rb\n+++ test.rb\n@@ -1 +1,2 @@\n-a\n+b\n\\ No newline at end of file\n+c\n"
        self.assertFalse(diff_matches(invalid, [("test.rb", b"a\n", b"bc\n")]))
        empty_line = b"--- test.rb\n+++ test.rb\n@@ -1 +1,2 @@\n-a\n+b\n+\n\\ No newline at end of file\n"
        self.assertFalse(diff_matches(empty_line, [("test.rb", b"a\n", b"b\n")]))

    def test_no_final_newline_missing_and_misplaced_markers_are_rejected(self):
        before = self.before.rstrip(b"\n")
        good = unified("stdin", before, self.after)
        variants = [good.replace(b"\\ No newline at end of file\n", b""),
                    good.replace(b"-a 1,2,3\n", b"-a 1,2,3"),
                    good + b"\\ No newline at end of file\n"]
        for output in variants:
            self.assertFalse(diff_matches(output, [("stdin", before, self.after)]))

    def test_multi_file_diffs_require_every_input_and_correct_paths(self):
        changes = [("one.rb", self.before, self.after), ("dir/two.rb", b"b\n", b"B\n")]
        one = unified(*changes[0], variant="equivalent")
        two = unified(*changes[1], variant="equivalent")
        self.assertTrue(diff_matches(one + two, changes))
        self.assertTrue(diff_matches(two + one, changes))
        self.assertEqual(canonical_diff(two + one, changes), canonical_diff(one + two, changes))
        for output in (one, two, one + one, one + two + two):
            self.assertFalse(diff_matches(output, changes))

    def test_context_mismatch_and_overlapping_hunks_are_rejected(self):
        before, after = b"a\nb\nc\n", b"a\nB\nc\n"
        good = unified("test.rb", before, after)
        self.assertFalse(diff_matches(good.replace(b" a\n", b" z\n"), [("test.rb", before, after)]))
        second_hunk = b"@@ -2 +2 @@\n-b\n+B\n"
        self.assertFalse(diff_matches(good + second_hunk, [("test.rb", before, after)]))


class DiffProbeIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.f1 = load_probe("shared/batches/BATCH-F2P-0001/probe.py", "contract_f2p1")
        cls.f2 = load_probe("shared/batches/BATCH-F2P-0002/probe.py", "contract_f2p2")
        cls.f3 = load_probe("shared/batches/BATCH-F2P-0003/probe_support.py", "contract_f2p3")
        cls.corpus = load_probe("shared/batches/BATCH-DIFFERENTIAL-0001/corpus.py", "contract_corpus")

    def test_first_and_second_f2p_batches_accept_equivalence_and_reject_phantoms(self):
        for variant, accepted in (("canonical", True), ("equivalent", True), ("phantom", False)):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as tmp:
                output = unified("test.rb", b"a 1,2,3\n", b"a(1, 2, 3)\n", variant)
                probe = self.f1.TargetProbe(Path(tmp))
                try:
                    with mock.patch.object(probe, "run_binary", return_value=self.f1.Result(5, output.decode(), "")), mock.patch.object(
                        probe, "run", return_value=self.f1.Result(0, "github.com/pmezard/go-difflib/difflib\n", "")
                    ):
                        if accepted:
                            probe.check_check_diff_exact()
                        else:
                            with self.assertRaises(self.f1.ProbeFailure):
                                probe.check_check_diff_exact()
                finally:
                    probe.cleanup()
                work = Path(tmp) / "work"
                work.mkdir()
                with mock.patch.object(self.f2, "_run", return_value=subprocess.CompletedProcess([], 5, output, b"")), mock.patch.object(
                    self.f2.tempfile, "mkdtemp", return_value=str(work)
                ):
                    self.assertEqual(self.f2._check_check_diff(Path(tmp), Path("/unused"))[0], accepted)

    def test_third_f2p_batch_checks_all_diff_input_modes(self):
        for variant in ("canonical", "equivalent"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as tmp:
                ctx = self.f3.Context(Path(tmp))

                def run(args, input_bytes=None, cwd=None):
                    case = Path(cwd or tmp)
                    if "--header-opt-out" in args:
                        return subprocess.CompletedProcess([], 0, b"", b"")
                    if "--stdin-filepath" in args:
                        inputs = [(args[args.index("--stdin-filepath") + 1], input_bytes)]
                    else:
                        names = [name for name in args if not name.startswith("-")]
                        inputs = []
                        for name in names:
                            if name.startswith("@"):
                                inputs.extend((entry, (case / entry).read_bytes()) for entry in (case / name[1:]).read_text().splitlines())
                            elif (case / name).is_dir():
                                inputs.extend((p.relative_to(case).as_posix(), p.read_bytes()) for p in sorted((case / name).rglob("*.rb")))
                            else:
                                inputs.append((name, (case / name).read_bytes()))
                        if not names:
                            inputs = [("stdin", input_bytes)]
                    output = b""
                    for name, source in inputs:
                        formatted = source.replace(b"a 1,2,3", b"a(1, 2, 3)").replace(b"a 4,5,6,7", b"a(4, 5, 6, 7)")
                        if not formatted.endswith(b"\n"):
                            formatted += b"\n"
                        if "--check" in args:
                            output += unified(name, source, formatted, variant)
                        elif "-i" in args:
                            (case / name).write_bytes(formatted)
                        else:
                            output += formatted
                    return subprocess.CompletedProcess([], 5 if "--check" in args and output else 0, output, b"")

                try:
                    with mock.patch.object(ctx, "run", side_effect=run):
                        for check in (self.f3._check_diff_case, self.f3._check_stdin_diff_case,
                                      self.f3._check_diff_without_final_newline_case,
                                      self.f3._check_multi_file_case, self.f3._check_directory_case,
                                      self.f3._check_at_file_case, self.f3._check_header_case):
                            check(ctx)
                finally:
                    ctx.cleanup()

    def test_third_f2p_batch_keeps_rejecting_historical_diff_failures(self):
        for variant in ("phantom", "missing_marker"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as tmp:
                ctx = self.f3.Context(Path(tmp))
                output = unified("stdin", b"a 1,2,3", b"a(1, 2, 3)\n", variant)
                try:
                    with mock.patch.object(ctx, "run", return_value=subprocess.CompletedProcess([], 5, output, b"")):
                        with self.assertRaises(self.f3.ProbeFailure):
                            self.f3._check_diff_without_final_newline_case(ctx)
                finally:
                    ctx.cleanup()

    def test_shared_and_target_p2p_use_the_same_semantic_checks(self):
        class StopAfterDiffChecks(Exception):
            pass

        for side in ("shared", "target"):
            probe = load_probe(side + "/batches/BATCH-P2P-0001/p2p_probe.py", "contract_p2p_" + side)
            for variant, expected in (("equivalent", "PASS"), ("phantom", "FAIL")):
                with self.subTest(side=side, variant=variant), tempfile.TemporaryDirectory() as tmp:
                    original_record = probe.record
                    probe.RESULTS.clear()

                    def record(oid, *args, **kwargs):
                        original_record(oid, *args, **kwargs)
                        if oid == "P2P-017":
                            raise StopAfterDiffChecks()

                    def run(binary, args, cwd, **kwargs):
                        if "--check" in args or "-c" in args:
                            path = args[args.index("--stdin-filepath") + 1] if "--stdin-filepath" in args else args[-1]
                            return 5, unified(path, b"a 1,2,3\n", b"a(1, 2, 3)\n", variant).decode(), ""
                        return 0, "", ""

                    with mock.patch.object(sys, "argv", ["probe", "--repo", tmp, "--side", "target"]), mock.patch.object(
                        probe, "resolve_binary", return_value="/unused"
                    ), mock.patch.object(probe, "native_suite_projection", return_value={"command": [], "result": "PASS"}), mock.patch.object(
                        probe, "fixture_native_projection", return_value={}
                    ), mock.patch.object(probe, "run", side_effect=run), mock.patch.object(probe, "record", side_effect=record):
                        with self.assertRaises(StopAfterDiffChecks):
                            probe.main()
                    for oid in ("P2P-003", "P2P-007", "P2P-017"):
                        self.assertEqual(probe.RESULTS[oid]["result"], expected)

    def test_differential_stdout_and_digest_remain_stable_for_equivalent_diff(self):
        expected = json.loads((TESTS / "assets/fixtures/batches/BATCH-DIFFERENTIAL-0001/expected-results.json").read_text())
        for variant, accepted in (("canonical", True), ("equivalent", True), ("phantom", False)):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as tmp:
                ctx = self.corpus.Context("/unused", Path(tmp))

                def run(args, stdin=None, cwd=None):
                    if "-i" in args:
                        Path(args[-1]).write_text("a(1, 2, 3)\n")
                        return {"code": 0, "stdout": "", "stderr": ""}
                    path = args[args.index("--stdin-filepath") + 1] if "--stdin-filepath" in args else (args[-1] if args[-1] not in ("--check", "-c") else "stdin")
                    return {"code": 5, "stdout": unified(path, b"a 1,2,3\n", b"a(1, 2, 3)\n", variant).decode(), "stderr": ""}

                try:
                    with mock.patch.object(ctx, "run", side_effect=run):
                        for oid in ("DIFF-005", "DIFF-023"):
                            self.assertEqual(self.corpus.CASES[oid](ctx) == expected[oid], accepted)
                        # No fixture pairs are needed to compare the digest's
                        # diff contribution across two presentation variants.
                        digest = self.corpus.diff_030(ctx)
                        if variant == "canonical":
                            canonical = digest
                        else:
                            self.assertEqual(digest == canonical, accepted)
                finally:
                    ctx.cleanup()


class RuntimeCapabilityContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.probe = load_probe("shared/batches/BATCH-F2P-0003/probe_support.py", "contract_runtime")

    def test_os_exec_dependency_is_allowed_but_legacy_packages_are_not(self):
        deps = ["rubyfmt/cmd/rubyfmt", "rubyfmt/internal/cli", "rubyfmt/internal/formatter", "rubyfmt/internal/prism", "runtime/cgo"]
        for extra in ([], ["os/exec"], ["librubyfmt"], ["rubyfmt-main"], ["ruby-prism"]):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as tmp:
                ctx = self.probe.Context(Path(tmp))
                try:
                    with mock.patch.object(ctx, "deps", return_value=deps + extra), mock.patch.object(
                        ctx, "buildinfo", return_value="path\trubyfmt/cmd/rubyfmt\nmod\trubyfmt\t(devel)\nbuild\tCGO_ENABLED=1\n"
                    ), mock.patch.object(ctx, "run", return_value=subprocess.CompletedProcess([], 0, b"a(1, 2, 3)\n", b"")):
                        if not extra or extra == ["os/exec"]:
                            ctx.require_go_tech()
                            self.probe._go_list_case(ctx)
                        else:
                            with self.assertRaises(self.probe.ProbeFailure):
                                ctx.require_go_tech()
                finally:
                    ctx.cleanup()

    def test_real_legacy_exec_still_fails_the_hardgate(self):
        benign = sc._CLEAN_FORMATTER.replace("package formatter", 'package formatter\nimport "os/exec"')
        benign += '\nfunc Diagnostic() error { return exec.Command("printf", "ready").Run() }\n'
        for source, expected in ((benign, "PASS"), (sc._PROCESS_FALLBACK, "FAIL")):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                sc._write(root, "internal/formatter/formatter.go", source)
                sc._write(root, "internal/formatter/fixtures_test.go", sc._GOOD_TEST)
                hardgate, reward = sc._evaluate(root)
                self.assertEqual(hardgate["status"], expected)
                self.assertEqual(hardgate["cheating"], expected == "FAIL")
                if expected == "FAIL":
                    self.assertEqual(reward["value"], -1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
