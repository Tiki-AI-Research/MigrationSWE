"""Lightweight regression checks for conditional versus executed Go skips.

No Go toolchain, Docker, candidate execution, or scoring service is used.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile

sys.dont_write_bytecode = True
task = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(task / "tests"))

from verifier.evidence import _test_integrity, build_source_test_baseline
from verifier.harbor_entry import _go_skip_audit


def report(*rows: dict[str, object]) -> str:
    return "\n".join(json.dumps(row) for row in rows)


def main() -> None:
    passed = _go_skip_audit(report(
        {"Action": "pass", "Package": "engine", "Test": "TestFull"},
        {"Action": "pass", "Package": "engine"},
        {"Action": "skip", "Package": "engine/no_tests"},
    ), 0)
    assert passed["status"] == "PASS" and passed["skipped_tests"] == []
    skipped = _go_skip_audit(report(
        {"Action": "skip", "Package": "engine", "Test": "TestFull"},
        {"Action": "pass", "Package": "engine"},
    ), 0)
    assert len(skipped["skipped_tests"]) == 1
    incomplete = _go_skip_audit("", 0)
    assert incomplete["status"] == "INCONCLUSIVE"
    assert _go_skip_audit("not a test event", 0)["status"] == "INCONCLUSIVE"
    baseline = {"files": {}, "marker_counts": {}}
    with tempfile.TemporaryDirectory(prefix="skip-audit-", dir=task / "environment") as directory:
        root = Path(directory)
        source = root / "conditional_test.go"
        source.write_text('package probe\nfunc TestFull(t *testing.T) { if testing.Short() { t.Skip("short") } }\n')

        def status(audit: object = None) -> str:
            _, atom = _test_integrity(root, baseline, {}, go_skip_audit=audit)
            return str(atom["status"])

        assert status() == "FAIL"  # no execution evidence: keep the static gate
        assert status(passed) == "PASS"  # full checks executed, branch inactive
        assert status(skipped) == "PASS"  # optional new tests are not source obligations
        assert status(incomplete) != "PASS"
        # A source-derived test remains an obligation, including when its
        # conditional skip executes without any source-file modification.
        source_baseline = build_source_test_baseline(root)
        source_baseline["marker_counts"] = {"skip": 0, "xfail": 0, "disabled": 0}
        _, atom = _test_integrity(root, source_baseline, {}, go_skip_audit=skipped)
        assert atom["status"] == "FAIL"
        source.write_text('package probe\nfunc TestMain(m *testing.M) { os.Exit(0) }\n')
        assert status(passed) == "FAIL"  # process masking is never exempted
        source.unlink()
        (root / "mask.go").write_text('package probe\nfunc hide(t *testing.T) { t.SkipNow() }\n')
        assert status(passed) == "FAIL"  # production masking is never exempted
    print("go_skip_audit_selfcheck: PASS")


if __name__ == "__main__":
    main()
