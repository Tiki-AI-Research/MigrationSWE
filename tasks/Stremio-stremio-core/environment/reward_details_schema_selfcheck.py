"""Regression guard + normalizer for the ``jobs/`` reward-details error schema.

Migration norm §9.1 requires every ``jobs/**/reward-details.json`` to carry seven
top-level fields and an ``errors`` object with four keys
(``infrastructure_error``, ``agent_time_out``, ``build error``, ``compile error``).
The pre-r7 grader emitted only the first two keys; the r7 grader added the two
build/compile keys (derived from failure receipts by
``tests/grader.py::_error_flags``).  Historical job artifacts written before r7
kept the two-key schema, so the QA-wide §9.1 scan over the *workspace* ``jobs/``
tree stayed red for three consecutive rounds
(``docs/outstanding-items.md`` R7-QC-2 / R8-02 / R9-02).

Mechanism (source -> sink): the QA engine stages the workspace tree verbatim
(``pipeline_engine.qa.build_staging`` symlinks ``jobs`` into the project), so the
artifact store itself -- not the grader -- is what the sink reads.  This tool
repairs that store instead of patching individual files:

* **check** (default): scan a jobs root and fail on any ``reward-details.json``
  missing the seven required top-level fields or the four ``errors`` keys.  The
  predicate is the exact §9.1 contract (presence only, never values).
* **--backfill**: rewrite the nonconforming files in place, deriving the missing
  error flags from the trial's own sibling ``verifier-result.json`` using the
  SAME ``_error_flags`` the current grader uses.  No value is invented: an
  existing ``true`` (e.g. a real Go compile error) is preserved, and unrelated
  fields / rewards are left byte-for-byte identical apart from added keys.
* **--self-test**: materialise adversarial variant job trees in a temp dir and
  assert the checker fails before and passes after ``--backfill``, that a genuine
  ``compile error: true`` survives, and that unrepairable shapes are reported
  rather than crashing.

Run it from anywhere::

    PYTHONDONTWRITEBYTECODE=1 python3 -B environment/reward_details_schema_selfcheck.py
    ... --backfill
    ... --self-test
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True

_TASK_ROOT = Path(__file__).resolve().parents[1]
_WORKSPACE_ROOT = _TASK_ROOT.parents[1]
_DEFAULT_JOBS_ROOT = _WORKSPACE_ROOT / "jobs"

FILENAME = "reward-details.json"
_VERIFIER_RESULT = "verifier-result.json"
_TOP_LEVEL = (
    "cheating_detected",
    "test",
    "migration_scope_complete_ratio",
    "f2p_pass_ratio",
    "p2p_pass_ratio",
    "reward",
    "errors",
)
_ERROR_KEYS = ("infrastructure_error", "agent_time_out", "build error", "compile error")


def _error_flags():
    """Load the grader's own ``_error_flags`` so backfill never diverges from it."""
    tests_dir = _TASK_ROOT / "tests"
    if str(tests_dir) not in sys.path:
        sys.path.insert(0, str(tests_dir))
    spec = importlib.util.spec_from_file_location("_schema_grader", tests_dir / "grader.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._error_flags


def _missing(data: object) -> list[str]:
    """Return the §9.1 violations for one parsed reward-details object."""
    issues: list[str] = []
    if not isinstance(data, dict):
        return ["top-level is not a JSON object"]
    missing = [key for key in _TOP_LEVEL if key not in data]
    if missing:
        issues.append(f"missing top-level fields {sorted(missing)}")
    errors = data.get("errors")
    if not isinstance(errors, dict):
        issues.append("errors is not an object")
    else:
        absent = [key for key in _ERROR_KEYS if key not in errors]
        if absent:
            issues.append(f"errors missing {sorted(absent)}")
    return issues


def _sibling_verifier_result(path: Path) -> Path | None:
    direct = path.parent / _VERIFIER_RESULT
    if direct.is_file():
        return direct
    for candidate in sorted(path.parent.rglob(_VERIFIER_RESULT)):
        if candidate.is_file():
            return candidate
    return None


def _derive_errors(path: Path, existing: object) -> dict:
    """Derive the full four-key error object, preserving any recorded truth."""
    base: dict = dict(existing) if isinstance(existing, dict) else {}
    result = _sibling_verifier_result(path)
    if result is not None:
        try:
            derived = _error_flags()(json.loads(result.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            derived = {}
        for key, value in derived.items():
            if key not in base:
                base[key] = bool(value)
    for key in _ERROR_KEYS:
        base.setdefault(key, False)
    return base


def _write_atomic(path: Path, data: dict) -> None:
    payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    tmp = path.with_name(path.name + ".schema.tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)


def check(jobs_root: Path, *, verbose: bool = False) -> int:
    files = sorted(jobs_root.rglob(FILENAME)) if jobs_root.is_dir() else []
    failed = False
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"{path}: unparseable JSON: {exc}")
            failed = True
            continue
        issues = _missing(data)
        if issues:
            failed = True
            print(f"{path}: " + "; ".join(issues))
        elif verbose:
            print(f"{path}: PASS")
    print(f"reward_details_schema_selfcheck: {'FAIL' if failed else 'PASS'} "
          f"({len(files)} {FILENAME} checked under {jobs_root})")
    return 1 if failed else 0


def backfill(jobs_root: Path) -> int:
    files = sorted(jobs_root.rglob(FILENAME)) if jobs_root.is_dir() else []
    changed = 0
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"{path}: unparseable JSON, cannot backfill: {exc}")
            continue
        if not isinstance(data, dict) or not _missing(data):
            continue
        before = dict(data)
        existing = data.get("errors")
        data["errors"] = _derive_errors(path, existing)
        _write_atomic(path, data)
        added = sorted(set(data["errors"]) - set(existing if isinstance(existing, dict) else {}))
        changed += 1
        print(f"{path}: backfilled errors keys {added}")
        assert set(data) >= {k for k in _TOP_LEVEL}, (before, data)
    print(f"reward_details_schema_selfcheck: backfilled {changed} file(s)")
    return 0


# --------------------------------------------------------------------------- #
# adversarial self-test
# --------------------------------------------------------------------------- #
def _emit_trial(root: Path, name: str, *, detail: dict, verifier_result: dict | None) -> Path:
    trial = root / name
    (trial / "verifier").mkdir(parents=True)
    (trial / "verifier" / FILENAME).write_text(
        json.dumps(detail, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if verifier_result is not None:
        (trial / "verifier" / _VERIFIER_RESULT).write_text(
            json.dumps(verifier_result, ensure_ascii=False), encoding="utf-8"
        )
    return trial


def _base_detail(**errors) -> dict:
    return {
        "cheating_detected": False,
        "test": None,
        "migration_scope_complete_ratio": None,
        "f2p_pass_ratio": None,
        "p2p_pass_ratio": None,
        "reward": 0.0,
        "errors": dict(errors),
    }


def _compile_error_result() -> dict:
    return {
        "status": "completed",
        "receipts": {
            "commands": [
                {
                    "kind": "target-native-test",
                    "returncode": 1,
                    "stderr": "pkg/foo.go:12:5: undefined: Bar\nFAIL\tpkg [build failed]",
                }
            ]
        },
    }


def self_test() -> int:
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="stremio1102-schema-") as tmp:
        root = Path(tmp)

        # V1: only the pre-r7 two keys, build receipt failed -> build error true.
        _emit_trial(
            root, "v1_two_keys_build",
            detail=_base_detail(infrastructure_error=False, agent_time_out=False),
            verifier_result={
                "status": "completed",
                "receipts": {"commands": [{"kind": "target-build", "returncode": 2, "stderr": "# pkg"}]},
            },
        )
        # V2: two keys, Go compiler diagnostics -> compile error true.
        _emit_trial(
            root, "v2_compile_diag",
            detail=_base_detail(infrastructure_error=False, agent_time_out=False),
            verifier_result=_compile_error_result(),
        )
        # V3: errors present but not an object (list) -> must be normalised.
        detail = _base_detail()
        detail["errors"] = []
        _emit_trial(root, "v3_errors_list", detail=detail, verifier_result=None)
        # V4: deeply nested path + only agent_time_out missing.
        nested = root / "v4" / "a" / "b" / "c" / "verifier"
        nested.mkdir(parents=True)
        (nested / FILENAME).write_text(
            json.dumps(_base_detail(infrastructure_error=False, **{"build error": False, "compile error": False}),
                       sort_keys=True) + "\n",
            encoding="utf-8",
        )
        # V5: unrepairable (top-level not an object) must be reported, not crash.
        broken = root / "v5_broken" / "verifier"
        broken.mkdir(parents=True)
        (broken / FILENAME).write_text("[1, 2, 3]\n", encoding="utf-8")

        pre = check(root)
        if pre != 1:
            failures.append(f"pre-check expected FAIL, got rc={pre}")
        offenders = _dump_offenders(root)
        if len(offenders) != 5:
            failures.append(f"expected 5 offenders pre-backfill, got {len(offenders)}")
        backfill(root)
        offenders_after = _dump_offenders(root)
        if len(offenders_after) != 1 or "v5_broken" not in offenders_after[0]:
            failures.append(f"post-backfill offenders must be only the unrepairable v5: "
                            f"{offenders_after}")
        (broken / FILENAME).unlink()
        post = check(root)
        if post != 0:
            failures.append(f"post-check expected PASS, got rc={post}")

        v1 = json.loads((root / "v1_two_keys_build" / "verifier" / FILENAME).read_text())
        v2 = json.loads((root / "v2_compile_diag" / "verifier" / FILENAME).read_text())
        v4 = json.loads((nested / FILENAME).read_text())
        if set(v1["errors"]) != set(_ERROR_KEYS) or v1["errors"].get("build error") is not True:
            failures.append(f"v1 build error not derived: {v1['errors']}")
        if v2["errors"].get("compile error") is not True:
            failures.append(f"v2 compile error not preserved: {v2['errors']}")
        if set(v4["errors"]) != set(_ERROR_KEYS):
            failures.append(f"v4 nested not normalised: {v4['errors']}")
        if not isinstance(v1.get("reward"), (int, float)):
            failures.append("v1 reward field disturbed")

    if failures:
        print("reward_details_schema_selfcheck --self-test: FAIL")
        for line in failures:
            print("  - " + line)
        return 1
    print("reward_details_schema_selfcheck --self-test: PASS (5 adversarial variants)")
    return 0


def _dump_offenders(root: Path) -> list[str]:
    offenders: list[str] = []
    for path in sorted(root.rglob(FILENAME)):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            offenders.append(str(path))
            continue
        if _missing(data):
            offenders.append(str(path))
    return offenders


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs-root", type=Path, default=_DEFAULT_JOBS_ROOT,
                        help="jobs tree to scan (default: <workspace>/jobs)")
    parser.add_argument("--backfill", action="store_true",
                        help="rewrite nonconforming reward-details.json in place")
    parser.add_argument("--self-test", action="store_true",
                        help="run adversarial variant self-test in a temp dir")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()
    if args.backfill:
        backfill(args.jobs_root)
    return check(args.jobs_root, verbose=args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
