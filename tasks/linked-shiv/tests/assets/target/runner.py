"""Execute the registered migration tests and emit a verifier observation report.

The report is derived from pytest's own JUnit XML in a separate process.  The
candidate code under test is never imported into the process that writes the
report, so a production-module import side effect cannot rewrite the result
aggregate.  Test execution remains isolated in its own process; a nonzero
pytest status with an all-PASS report is treated as contradictory and is not
accepted as evidence.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


TEST_NAME_RE = re.compile(r"(test_(?:p2p|f2p)_\d{3})")

# A test that could not establish the property it exists to check appends a
# record here instead of asserting, so the observation can be reported as an
# unfinished evaluation rather than as a pass.
COVERAGE_ENV = "SHIV_UNVERIFIED_COVERAGE_PATH"


def _read_coverage_records(path: Path) -> tuple[dict[str, list[str]], list[str]]:
    """Read the coverage gaps the tests reported during this phase."""

    gaps: dict[str, list[str]] = {}
    errors: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        # The harness creates the file before the phase starts, so its absence
        # here means it was removed while tests were running.  A missing record
        # must not be read as "no gaps".
        return gaps, ["the coverage record file was removed during the run"]
    except OSError as exc:
        return gaps, [f"coverage record could not be read: {exc}"]
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"coverage record line {number} is not JSON: {exc}")
            continue
        identifier = record.get("id") if isinstance(record, dict) else None
        reason = record.get("reason") if isinstance(record, dict) else None
        if not isinstance(identifier, str) or not isinstance(reason, str) or not reason:
            errors.append(
                f"coverage record line {number} does not name an observation and reason"
            )
            continue
        gaps.setdefault(identifier, []).append(reason)
    return gaps, errors


def _identifier(nodeid: str) -> str | None:
    match = TEST_NAME_RE.search(nodeid)
    if match is None:
        return None
    parts = match.group(1).split("_", 2)
    if len(parts) != 3:
        return None
    return f"{parts[1].upper()}-{parts[2]}"


def _junit_outcomes(
    path: Path,
    expected: dict[str, str],
) -> tuple[dict[str, list[str]], dict[str, str], list[str]]:
    """Map pytest's JUnit XML to observation outcomes.

    Only the evaluator-owned test cases are retained.  A missing expected case
    is left absent so the report can mark it as an unfinished evaluation.
    """

    outcomes: dict[str, list[str]] = {}
    reasons: dict[str, str] = {}
    errors: list[str] = []
    if not path.is_file():
        return outcomes, reasons, ["pytest did not write a JUnit report"]
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        return outcomes, reasons, [f"pytest JUnit report is unreadable: {exc}"]

    cases = list(root.iter("testcase"))
    for case in cases:
        identifier = _identifier(case.get("name", "")) or _identifier(
            case.get("classname", "")
        )
        if identifier is None or identifier not in expected:
            continue
        tags = {child.tag for child in case}
        if "failure" in tags or "error" in tags:
            outcome = "FAIL"
            for child in case:
                if child.tag in {"failure", "error"}:
                    message = (child.get("message") or child.text or "").strip()
                    if message:
                        reasons.setdefault(identifier, message[:2000])
                    break
        elif "skipped" in tags:
            outcome = "INCONCLUSIVE"
        else:
            outcome = "PASS"
        outcomes.setdefault(identifier, []).append(outcome)

    declared_tests = 0
    declared_failures = 0
    declared_errors = 0
    for suite in root.iter("testsuite"):
        try:
            declared_tests += int(suite.get("tests", "0"))
            declared_failures += int(suite.get("failures", "0"))
            declared_errors += int(suite.get("errors", "0"))
        except ValueError:
            errors.append("pytest JUnit suite counters are invalid")
            break
    if declared_tests and declared_tests != len(cases):
        errors.append(
            "pytest JUnit test count does not match its testcase records"
        )
    observed_failures = sum(
        1 for case in cases if {"failure", "error"} & {child.tag for child in case}
    )
    if declared_failures + declared_errors != observed_failures:
        errors.append(
            "pytest JUnit failure count does not match its testcase records"
        )
    return outcomes, reasons, errors


def _run_phase(
    repo: Path,
    test_paths: list[str],
    selector: str,
    junit_path: Path,
    coverage_path: Path,
) -> tuple[subprocess.CompletedProcess[str] | None, str | None]:
    command = [
        sys.executable,
        "-m",
        "pytest",
        *test_paths,
        "-k",
        selector,
        "-o",
        "addopts=",
        "-q",
        "--disable-warnings",
        "-p",
        "no:cacheprovider",
        "--confcutdir",
        str(repo / "test"),
        "--junitxml",
        str(junit_path),
    ]
    environment = os.environ.copy()
    environment[COVERAGE_ENV] = str(coverage_path)
    # Do not let a candidate-installed setuptools entry point become a pytest
    # plugin that can rewrite reports.
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    try:
        return (
            subprocess.run(
                command,
                cwd=repo,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
                env=environment,
            ),
            None,
        )
    except OSError as exc:
        return None, f"pytest could not be started: {exc}"


def _record_coverage(
    expected: dict[str, str],
    gaps: dict[str, list[str]],
    errors: list[str],
) -> None:
    """Attach tests' coverage gaps to the observations they qualify."""

    if errors:
        # An unreadable record proves nothing about any P2P case, so no P2P
        # observation may claim a verified result.  A demonstrated failure or
        # an existing gap still dominates this fallback.
        for identifier, kind in expected.items():
            if kind == "P2P" and not gaps.get(identifier):
                gaps[identifier] = [
                    "the unverified-coverage record could not be read"
                ]


def _rows(
    expected: dict[str, str],
    outcomes: dict[str, list[str]],
    not_run: dict[str, str],
    gaps: dict[str, list[str]],
    reasons: dict[str, str],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for identifier, kind in expected.items():
        values = outcomes.get(identifier, [])
        gap_values = gaps.get(identifier, [])
        reason = reasons.get(identifier, "")
        if not values and identifier in not_run:
            status = "NA"
            reason = not_run[identifier]
        elif not values:
            status = "INFRA_ERROR"
            reason = reason or "pytest did not report this observation"
        elif "FAIL" in values:
            status = "FAIL"
        elif "INCONCLUSIVE" in values:
            status = "INCONCLUSIVE"
            reason = reason or "pytest did not complete this observation"
        elif gap_values:
            status = "INCONCLUSIVE"
            reason = "evaluation incomplete: " + "; ".join(gap_values)
        else:
            status = "PASS"
        evidence: dict[str, object] = {
            "runner": "pytest-junit",
            "cases": len(values),
        }
        if identifier in not_run and not values:
            evidence["not_run_reason"] = not_run[identifier]
        if gap_values:
            evidence["coverage_unverified"] = list(gap_values)
        row: dict[str, object] = {
            "id": identifier,
            "kind": kind,
            "status": status,
            "value": {
                "type": "json",
                "value": {"test": identifier, "outcomes": values},
            },
            "evidence": evidence,
        }
        if reason:
            row["reason"] = reason
        rows.append(row)
    return rows


def _group_clean(
    expected: dict[str, str],
    outcomes: dict[str, list[str]],
    kind: str,
) -> bool:
    return all(
        outcomes.get(identifier) and all(outcome == "PASS" for outcome in outcomes[identifier])
        for identifier, expected_kind in expected.items()
        if expected_kind == kind
    )


RUNTIME_KEYS = {
    "source": "source_runtime_sha256",
    "target": "target_runtime_sha256",
}

TEST_PATHS = ["test/test_migration_p2p.py", "test/test_dependency.py"]


def main(role: str = "target") -> int:
    """Run the migration phases and write an observation report for ``role``.

    The role decides both the report's ``role`` field and which runtime digest
    it claims.  They are read from the same argument so a source report cannot
    silently carry the target digest (and vice versa).
    """

    if role not in RUNTIME_KEYS:
        raise ValueError(f"unknown observation role: {role!r}")
    report_path = Path(sys.argv[1]).resolve()
    assets = Path(__file__).resolve().parents[1]
    config_candidates = (
        assets / "config.json",
        assets.parent / "config.json",
        Path(__file__).resolve().with_name("config.json"),
        Path("/tests/config.json"),
        Path.cwd() / "case/tests/config.json",
    )
    config_path = next((path for path in config_candidates if path.is_file()), None)
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path else {
        "executor": {"id": "command", "version": "1"},
        "source_runtime_sha256": None,
        "target_runtime_sha256": None,
        "tests": {
            "p2p": [{"id": f"P2P-{index:03d}"} for index in range(1, 41)],
            "f2p": [{"id": f"F2P-{index:03d}"} for index in range(1, 9)],
        },
    }
    expected: dict[str, str] = {}
    for kind in ("p2p", "f2p"):
        for row in config["tests"][kind]:
            expected[str(row["id"])] = kind.upper()
    repo_candidates = (
        Path.cwd(),
        Path.cwd() / "source",
        assets.parent.parent.parent,
        assets.parent.parent.parent / "source",
    )
    repo = next(
        (
            path
            for path in repo_candidates
            if (path / "test/test_migration_p2p.py").is_file()
        ),
        None,
    )
    if repo is None:
        raise RuntimeError("cannot locate candidate test tree")
    report_dir = report_path.parent
    try:
        report_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(f"cannot prepare the report directory: {exc}") from exc
    coverage_path = report_dir / "coverage-records.jsonl"
    p2p_junit = report_dir / ".pytest-p2p.xml"
    f2p_junit = report_dir / ".pytest-f2p.xml"
    for path in (p2p_junit, f2p_junit):
        path.unlink(missing_ok=True)

    gaps: dict[str, list[str]] = {}
    outcomes: dict[str, list[str]] = {}
    reasons: dict[str, str] = {}
    not_run: dict[str, str] = {}
    phase_errors: list[str] = []

    coverage_path.write_text("", encoding="utf-8")
    p2p_completed, p2p_start_error = _run_phase(
        repo, TEST_PATHS, "test_p2p_", p2p_junit, coverage_path
    )
    p2p_outcomes, p2p_reasons, p2p_errors = _junit_outcomes(p2p_junit, expected)
    outcomes.update(p2p_outcomes)
    reasons.update(p2p_reasons)
    phase_errors.extend(p2p_errors)
    if p2p_start_error:
        phase_errors.append(p2p_start_error)
    p2p_gaps, p2p_coverage_errors = _read_coverage_records(coverage_path)
    gaps.update(p2p_gaps)
    _record_coverage(expected, gaps, p2p_coverage_errors)
    p2p_rc = p2p_completed.returncode if p2p_completed is not None else 1
    p2p_junit.unlink(missing_ok=True)
    if p2p_completed is not None:
        observed_failures = any(
            "FAIL" in values for values in p2p_outcomes.values()
        )
        if p2p_rc != 0 and not observed_failures and not p2p_errors:
            phase_errors.append(
                "pytest returned nonzero without a failed P2P test case"
            )
        elif p2p_rc == 0 and observed_failures:
            phase_errors.append(
                "pytest returned zero while P2P test cases reported failure"
            )
    if phase_errors:
        for identifier in p2p_outcomes:
            p2p_outcomes[identifier] = ["INCONCLUSIVE"]

    returncode = p2p_rc
    if (
        p2p_start_error is None
        and p2p_completed is not None
        and p2p_rc == 0
        and not phase_errors
        and _group_clean(expected, p2p_outcomes, "P2P")
    ):
        coverage_path.write_text("", encoding="utf-8")
        f2p_completed, f2p_start_error = _run_phase(
            repo, TEST_PATHS, "test_f2p_", f2p_junit, coverage_path
        )
        f2p_outcomes, f2p_reasons, f2p_errors = _junit_outcomes(f2p_junit, expected)
        outcomes.update(f2p_outcomes)
        reasons.update(f2p_reasons)
        phase_errors.extend(f2p_errors)
        if f2p_start_error:
            phase_errors.append(f2p_start_error)
        f2p_gaps, f2p_coverage_errors = _read_coverage_records(coverage_path)
        gaps.update(f2p_gaps)
        _record_coverage(expected, gaps, f2p_coverage_errors)
        returncode = f2p_completed.returncode if f2p_completed is not None else 1
        f2p_junit.unlink(missing_ok=True)
        if f2p_completed is not None:
            observed_failures = any(
                "FAIL" in values for values in f2p_outcomes.values()
            )
            if returncode != 0 and not observed_failures and not f2p_errors:
                phase_errors.append(
                    "pytest returned nonzero without a failed F2P test case"
                )
            elif returncode == 0 and observed_failures:
                phase_errors.append(
                    "pytest returned zero while F2P test cases reported failure"
                )
        if phase_errors:
            for identifier in f2p_outcomes:
                f2p_outcomes[identifier] = ["INCONCLUSIVE"]
    else:
        for identifier, kind in expected.items():
            if kind == "F2P":
                not_run[identifier] = "P2P phase failed; F2P phase was not started"

    coverage_path.unlink(missing_ok=True)
    runtime_key = RUNTIME_KEYS[role]
    if runtime_key not in config:
        # Reporting another role's digest would present this run as calibrated
        # against an environment it never used.
        raise RuntimeError(
            f"the loaded config does not declare {runtime_key}; "
            f"cannot report {role} observations"
        )
    runtime_sha = config.get(runtime_key)
    payload = {
        "schema_version": "migration-observations:v6",
        "role": role,
        "executor": config.get("executor", {"id": "command", "version": "1"}),
        "runtime_sha256": runtime_sha,
        "receipt": {"returncode": int(returncode), "report_path": "/logs/verifier/result.json"},
        "observations": _rows(expected, outcomes, not_run, gaps, reasons),
        "runner_returncode": int(returncode),
        "runner_errors": list(phase_errors),
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return int(returncode)


if __name__ == "__main__":
    raise SystemExit(main())
