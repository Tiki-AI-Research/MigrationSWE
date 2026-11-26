#!/usr/bin/env python3
"""Build-time security regression for the Migration verifier (RG-E).

Two-sided controls: known attacks/hazardous inputs must be caught, while
legitimate equivalents (comments, reformatting, behaviour-preserving test
rewrites, ordinary go-message usage) must pass.  Exits non-zero on any
regression so ``tests/Dockerfile`` fails the image build instead of shipping a
silently weakened verifier.

Run: ``python3 /tests/verifier/security_self_check.py``
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import evidence  # noqa: E402
import hardgate_engine  # noqa: E402
import residue  # noqa: E402

_failures: list[str] = []


def check(label: str, condition: bool) -> None:
    if not condition:
        _failures.append(label)


def _write(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _sha(path: pathlib.Path) -> str:
    return evidence._digest(path)


def test_residue_ignores_comments_and_strings() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        _write(root / "mime" / "note.go",
               "package mime\n\n// legacy VisitAll traversal removed during migration\n"
               'const note = "VisitAll"\n')
        _write(root / "mime" / "call.go",
               "package mime\n\nfunc g() { gomime.VisitAll(nil) }\n")
        rules = [{"id": "RES", "kind": "literal", "paths": ["mime"], "target": "VisitAll"}]
        hits = residue.scan_residue_rules(root, rules, strip_strings=True)
        check("residue: comment/string not a residue hit",
              [row["path"] for row in hits] == ["mime/call.go"])


def test_anti_bridge_covers_whole_package() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        _write(root / "mime" / "mime.go",
               'package mime\n\nimport "github.com/emersion/go-message"\nvar _ = message.HandlerFunc\n')
        _write(root / "mime" / "bridge_extra.go",
               'package mime\n\nimport "os/exec"\n\nfunc init() { _ = exec.Command("true").Run() }\n')
        rules = [{"id": "BR", "kind": "literal", "paths": ["mime"],
                  "exclude": ["mime/*_test.go"], "target": "exec.Command"}]
        hits = residue.scan_residue_rules(root, rules, strip_strings=False)
        check("anti-bridge: sibling-file exec.Command detected",
              any(row["path"] == "mime/bridge_extra.go" for row in hits))
        # Negative control: a package that does not bridge must stay clean.
        (root / "mime" / "bridge_extra.go").unlink()
        hits_clean = residue.scan_residue_rules(root, rules, strip_strings=False)
        check("anti-bridge: clean package not flagged", hits_clean == [])


def test_go_skip_markers_counted() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        _write(root / "mime" / "a_test.go", "package mime\n\nfunc TestA(t *testing.T) {}\n")
        before = evidence._marker_counts(root)
        _write(root / "mime" / "z_skip_test.go",
               'package mime\n\nfunc TestZ(t *testing.T) { t.Skip("extra") }\n')
        after = evidence._marker_counts(root)
        check("go t.Skip counted as a new skip marker",
              after["skip"] == before["skip"] + 1)


def test_integrity_equivalence_and_weakening() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        baseline_src = ("package mime\n\nfunc TestA(t *testing.T) {\n"
                        "\tif got != want {\n\t\tt.Errorf(\"got %v\", got)\n\t}\n}\n")
        _write(root / "mime" / "a_test.go", baseline_src)
        baseline = {"files": {"mime/a_test.go": {
            "sha256": _sha(root / "mime" / "a_test.go"),
            **evidence._assertion_signature(baseline_src)}},
            "marker_counts": {"skip": 0, "xfail": 0, "disabled": 0}}
        config = {"policy": {"test_integrity": {"assertion_strength": True}}}
        # comment / branch reshape: tolerated
        _write(root / "mime" / "a_test.go",
               "package mime\n\n// reviewed\n\nfunc TestA(t *testing.T) {\n"
               "\tif got == want {\n\t\treturn\n\t}\n\t\tt.Errorf(\"got %v\", got)\n}\n")
        tamper, _ = evidence._test_integrity(root, baseline, config)
        check("test-integrity: equivalent rewrite tolerated", tamper["status"] == "PASS")
        # assertion deleted: caught
        _write(root / "mime" / "a_test.go",
               "package mime\n\nfunc TestA(t *testing.T) {\n\t_ = want\n}\n")
        tamper2, _ = evidence._test_integrity(root, baseline, config)
        check("test-integrity: deleted assertion caught", tamper2["status"] == "FAIL")


def test_hardgate_cheating_classification() -> None:
    def policy(**statuses):
        base = {
            "evidence_mode": "derived",
            "patch_applicable": {"status": "PASS"},
            "build_pass": {"status": "PASS"},
            "dependency_resolved": {"status": "PASS"},
            "no_deceptive_source_fallback": {"status": "PASS"},
            "public_contract_preserved": {"status": "PASS"},
            "critical_data_preserved": {"status": "NA"},
            "p2p_regression_preserved": {"status": "PASS"},
            "tests_not_tampered": {"status": "PASS"},
            "no_new_skip_or_xfail": {"status": "PASS"},
            "protected_paths_unchanged": {"status": "PASS"},
            "anti_cheating_pass": {"status": "PASS"},
        }
        base.update(statuses)
        return base

    result = hardgate_engine.evaluate_hardgate(pathlib.Path("."),
                                               policy(tests_not_tampered={"status": "FAIL"}))
    check("hardgate: tests_not_tampered FAIL is L1 cheating (reward -1)",
          result["status"] == "FAIL" and result["cheating"] is True
          and result["reward_override"] == -1)
    result_skip = hardgate_engine.evaluate_hardgate(
        pathlib.Path("."), policy(no_new_skip_or_xfail={"status": "FAIL"}))
    check("hardgate: new-skip alone is non-cheating (reward 0)",
          result_skip["status"] == "FAIL" and result_skip["cheating"] is False
          and result_skip["reward_override"] == 0)
    result2 = hardgate_engine.evaluate_hardgate(
        pathlib.Path("."), policy(anti_cheating_pass={"status": "FAIL"}))
    check("hardgate: anti_cheating FAIL is cheating (reward -1)",
          result2["cheating"] is True and result2["reward_override"] == -1)
    result3 = hardgate_engine.evaluate_hardgate(
        pathlib.Path("."), policy(no_deceptive_source_fallback={"status": "FAIL"}))
    check("hardgate: source fallback is L2 cheating",
          result3["cheat_level"] == "L2" and result3["reward_override"] == -1)


def test_dotimport_and_alias_mechanisms_caught() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        _write(root / "mime" / "bridge.go",
               'package mime\n\nimport . "syscall"\n\nfunc init() { _ = ForkExec("/bin/true", nil, nil) }\n')
        rules = [{"id": "D1", "kind": "literal", "paths": ["mime"],
                  "exclude": ["mime/*_test.go"], "target": "\"syscall\""},
                 {"id": "D2", "kind": "literal", "paths": ["mime"],
                  "exclude": ["mime/*_test.go"], "target": "ForkExec"}]
        hits = residue.scan_residue_rules(root, rules, strip_strings=False)
        check("anti-bridge: dotted syscall import + ForkExec caught",
              any(row["path"] == "mime/bridge.go" for row in hits))


def test_go_message_delegation_required() -> None:
    spec = {"paths": ["mime"], "exclude": ["mime/*_test.go"],
            "api_calls": {"github.com/emersion/go-message": ["Read"],
                          "github.com/emersion/go-message/textproto": ["NewMultipartReader"]}}
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        _write(root / "mime" / "vendored.go",
               'package mime\n\nimport "net/mail"\n\nfunc f() { _, _ = mail.ReadMessage(nil) }\n')
        check("delegation: vendored (no go-message API) fails",
              evidence._target_delegated(root, {"target_delegation": spec}) is False)
        _write(root / "mime" / "good.go",
               'package mime\n\nimport "github.com/emersion/go-message"\n\nfunc f() { _, _ = message.Read(nil) }\n')
        (root / "mime" / "vendored.go").unlink()
        check("delegation: go-message usage passes",
              evidence._target_delegated(root, {"target_delegation": spec}) is True)


def test_strong_assertion_weakening_caught() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        base = ("package mime\n\nfunc TestA(t *testing.T) {\n"
                "\tassert.Exactly(t, want, got)\n}\n")
        _write(root / "mime" / "a_test.go", base)
        baseline = {"files": {"mime/a_test.go": {
            "sha256": _sha(root / "mime" / "a_test.go"),
            **evidence._assertion_signature(base)}},
            "marker_counts": {"skip": 0, "xfail": 0, "disabled": 0}}
        config = {"policy": {"test_integrity": {"assertion_strength": True}}}
        _write(root / "mime" / "a_test.go",
               "package mime\n\nfunc TestA(t *testing.T) {\n\tassert.NotNil(t, got)\n}\n")
        tamper, _ = evidence._test_integrity(root, baseline, config)
        check("test-integrity: assert.Exactly -> assert.NotNil caught", tamper["status"] == "FAIL")


def test_reimplementation_signal() -> None:
    spec = {"paths": ["mime"], "exclude": ["mime/*_test.go"],
            "api_calls": {"net/mail": ["ReadMessage"], "mime/multipart": ["NewReader"]}}
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        # net/mail reads the baseline's top-level envelope. That operation was
        # not migrated from go-mime and must not be mistaken for MIME traversal.
        for imported, call in (
            ('"net/mail"', 'm, _ := mail.ReadMessage(nil); use(m.Body)'),
            ('m "net/mail"', 'msg, _ := m.ReadMessage(nil); use(msg.Header)'),
            ('. "net/mail"', 'm, _ := ReadMessage(nil); use(m.Body)'),
        ):
            _write(root / "mime" / "vendored.go",
                   f'package mime\n\nimport {imported}\n\nfunc f() {{ {call} }}\n')
            check(f"reimplementation: baseline envelope reader allowed ({imported})",
                  evidence._target_reimplemented(root, {"target_reimplementation": spec}) is False)
        # Consuming the old multipart parser still replaces migrated traversal,
        # even if an unrelated go-message call is present as padding.
        for imported, call in (
            ('"mime/multipart"', 'r := multipart.NewReader(nil, "b"); r.NextPart()'),
            ('mp "mime/multipart"', 'r := mp.NewReader(nil, "b"); r.NextPart()'),
            ('. "mime/multipart"', 'r := NewReader(nil, "b"); r.NextPart()'),
        ):
            _write(root / "mime" / "vendored.go",
                   f'package mime\n\nimport ({imported}; gm "github.com/emersion/go-message")\n'
                   f'func f() {{ {call}; gm.Read(nil) }}\n')
            check(f"reimplementation: consumed multipart traversal flagged ({imported})",
                  evidence._target_reimplemented(root, {"target_reimplementation": spec}) is True)
        _write(root / "mime" / "good.go",
               'package mime\n\nimport "github.com/emersion/go-message"\n\nfunc f() { _, _ = message.Read(nil) }\n')
        (root / "mime" / "vendored.go").unlink()
        check("reimplementation: go-message usage not flagged",
              evidence._target_reimplemented(root, {"target_reimplementation": spec}) is False)


def test_assertion_tautology_caught() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        base = ("package mime\n\nfunc TestA(t *testing.T) {\n"
                '\tassert.Exactly(t, readTestFile("x", false), body)\n}\n')
        _write(root / "mime" / "a_test.go", base)
        baseline = {"files": {"mime/a_test.go": {
            "sha256": _sha(root / "mime" / "a_test.go"),
            **evidence._assertion_signature(base)}},
            "marker_counts": {"skip": 0, "xfail": 0, "disabled": 0}}
        config = {"policy": {"test_integrity": {"assertion_strength": True}}}
        _write(root / "mime" / "a_test.go",
               "package mime\n\nfunc TestA(t *testing.T) {\n\tassert.Exactly(t, body, body)\n}\n")
        tamper, _ = evidence._test_integrity(root, baseline, config)
        check("test-integrity: tautology (identical operands) caught", tamper["status"] == "FAIL")


def main() -> int:
    test_residue_ignores_comments_and_strings()
    test_anti_bridge_covers_whole_package()
    test_dotimport_and_alias_mechanisms_caught()
    test_go_skip_markers_counted()
    test_integrity_equivalence_and_weakening()
    test_strong_assertion_weakening_caught()
    test_hardgate_cheating_classification()
    test_go_message_delegation_required()
    test_reimplementation_signal()
    test_assertion_tautology_caught()
    if _failures:
        sys.stderr.write("security self-check FAILED:\n")
        for label in _failures:
            sys.stderr.write("  - %s\n" % label)
        return 1
    sys.stdout.write("security self-check PASSED (10 checks)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
