"""Deterministic native-test P2P collector for Migration Grader.

The behavior-preservation (P2P) observations bound to the repository's own
test files (``test:<file>#<title>`` surfaces) are semantically deterministic:
run the repository's existing tests and compare the outcome.  This module
executes those tests through the standard ``ContainerExecutor`` and maps the
machine-readable result back onto the frozen P2P observations so authoring no
longer depends on a model-authored collection step.

Everything here is pure and auditable: adapters only describe *how* to run and
*how* to parse a framework report, and the mapping only uses the frozen
inventory plus the parsed case results.  No model is involved.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, Literal

try:  # The verifier bundle ships this module without the grader package.
    from scripts.migration.grader.contracts import CaseObservationInventory
except ImportError:  # pragma: no cover - verifier bundle import
    CaseObservationInventory = Any  # type: ignore[assignment,misc]
try:
    from scripts.migration.grader.generation.observation_policy import (
        is_native_test_row,
        native_test_file_of_row,
    )
except ImportError:  # pragma: no cover - verifier bundle import

    def _native_surface(row: Mapping[str, Any]) -> str:
        coverage = row.get("coverage")
        surface = coverage.get("surface") if isinstance(coverage, Mapping) else row.get("surface")
        return str(surface or "").strip()

    def is_native_test_row(row: Mapping[str, Any]) -> bool:  # type: ignore[misc]
        return _native_surface(row).startswith("test:")

    def native_test_file_of_row(row: Mapping[str, Any]) -> str:  # type: ignore[misc]
        return _native_surface(row)[len("test:"):].split("#", 1)[0].strip()

try:
    from scripts.utils.errors import PipelineError
except ImportError:  # pragma: no cover - verifier bundle import

    class PipelineError(RuntimeError):  # type: ignore[no-redef]
        pass

NativeStatus = Literal["PASS", "FAIL", "NA"]

# Test directories used both to normalize absolute in-container paths and to
# detect the repository-relative portion of a reported test file.
_TEST_ANCHORS = frozenset({"test", "tests", "spec", "specs", "__tests__", "e2e"})
_REPO_ROOTS = ("/workspace/repo/", "/workspace/", "/input/repo/")
_TERMINAL_BUCKETS = (("passes", "PASS"), ("failures", "FAIL"), ("pending", "NA"))
# Bound test files under these directories typically require external services
# (message brokers, databases, clusters) and hang the run.  They are excluded
# deterministically; their observations stay INCONCLUSIVE and are dropped.
_HEAVY_TEST_SEGMENTS = frozenset(
    {"integration", "e2e", "system", "acceptance", "compat", "benchmark", "benchmarks"}
)


def _is_heavy_target(relative: str) -> bool:
    parts = PurePosixPath(relative).parts[:-1]
    return any(part.casefold() in _HEAVY_TEST_SEGMENTS for part in parts)


@dataclasses.dataclass(frozen=True)
class NativeCaseResult:
    file: str
    full_title: str
    status: NativeStatus


@dataclasses.dataclass(frozen=True)
class Adapter:
    name: str
    parse: Callable[[str], list[NativeCaseResult]]
    build_command: Callable[[Sequence[str], str], tuple[str, ...]]
    report_suffix: str = ".json"


def _object(text: str) -> Mapping[str, Any]:
    try:
        payload = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"native test report is not valid JSON: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("native test report must be a JSON object")
    return payload


def _as_rows(value: object) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [row for row in value if isinstance(row, Mapping)]


def parse_mocha_json(text: str) -> list[NativeCaseResult]:
    """Parse the mocha ``json`` reporter output.

    Mocha emits ``passes``/``failures``/``pending`` buckets; some wrappers nest
    them under ``data``.  Both layouts are accepted deterministically.
    """

    payload = _object(text)
    container: Mapping[str, Any] = payload
    nested = payload.get("data")
    if isinstance(nested, Mapping) and any(
        bucket in nested for bucket, _status in _TERMINAL_BUCKETS
    ):
        container = nested
    results: list[NativeCaseResult] = []
    for bucket, status in _TERMINAL_BUCKETS:
        for row in _as_rows(container.get(bucket)):
            file = str(row.get("file") or row.get("fullFile") or "").strip()
            title = str(
                row.get("fullTitle")
                or row.get("full_title")
                or row.get("title")
                or ""
            ).strip()
            results.append(NativeCaseResult(file, title, status))  # type: ignore[arg-type]
    return results


def _vitest_status(value: object) -> NativeStatus:
    token = str(value or "").strip().casefold()
    if token in {"passed", "pass"}:
        return "PASS"
    if token in {"failed", "fail", "error"}:
        return "FAIL"
    return "NA"


def parse_vitest_json(text: str) -> list[NativeCaseResult]:
    """Parse the vitest ``json`` reporter output (``testResults`` tree)."""

    payload = _object(text)
    results: list[NativeCaseResult] = []
    for suite in _as_rows(payload.get("testResults")):
        file = str(suite.get("name") or suite.get("file") or "").strip()
        for case in _as_rows(suite.get("assertionResults")):
            title = str(
                case.get("fullName") or case.get("title") or ""
            ).strip()
            status = _vitest_status(case.get("status"))
            results.append(NativeCaseResult(file, title, status))
    return results


def _pytest_status(value: object) -> NativeStatus:
    token = str(value or "").strip().casefold()
    if token == "passed":
        return "PASS"
    if token in {"failed", "error"}:
        return "FAIL"
    return "NA"


def parse_pytest_json(text: str) -> list[NativeCaseResult]:
    """Parse the pytest ``pytest-json-report`` output (``tests[].outcome``)."""

    payload = _object(text)
    results: list[NativeCaseResult] = []
    for case in _as_rows(payload.get("tests")):
        nodeid = str(case.get("nodeid") or "").strip()
        file, _, rest = nodeid.partition("::")
        if not file:
            file = str(case.get("_file") or case.get("file") or "").strip()
        title = rest.replace("::", " › ").strip()
        status = _pytest_status(case.get("outcome"))
        results.append(NativeCaseResult(file, title, status))
    return results


def _junit_status(case: ET.Element) -> NativeStatus:
    # Some reporters encode the outcome as a ``failure``/``error``/``skipped``
    # attribute (e.g. ``node --test --test-reporter=junit``) instead of a child
    # element; accept both deterministically.
    for key in ("failure", "error"):
        if case.get(key) is not None:
            return "FAIL"
    if case.get("skipped") is not None:
        return "NA"
    for child in case:
        tag = child.tag.rsplit("}", 1)[-1].casefold()
        if tag in {"failure", "error"}:
            return "FAIL"
        if tag == "skipped":
            return "NA"
    return "PASS"


def parse_junit_xml(text: str) -> list[NativeCaseResult]:
    """Parse a JUnit XML report into case results.

    JUnit is the single language-neutral report contract: any runner that can
    emit JUnit (jest-junit, mocha-junit-reporter, ``vitest --reporter=junit``,
    ``pytest --junitxml``, Surefire/Gradle, ``cargo nextest``, ``dotnet test``,
    RSpec, PHPUnit, ...) works here without a framework-specific parser.  A
    ``<testcase>`` maps to PASS by default, FAIL on ``<failure>``/``<error>`` and
    NA on ``<skipped>``.  ``file`` is preferred for the repository-relative path;
    emitters that only set ``classname`` still normalize deterministically.
    """

    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError(f"native test report is not valid JUnit XML: {exc}") from exc
    if root.tag.rsplit("}", 1)[-1].casefold() not in {"testsuites", "testsuite"}:
        raise ValueError("native test report is not a JUnit testsuite document")
    results: list[NativeCaseResult] = []
    for case in root.iter():
        if case.tag.rsplit("}", 1)[-1].casefold() != "testcase":
            continue
        file = str(case.get("file") or "").strip()
        classname = str(case.get("classname") or "").strip()
        title = str(case.get("name") or "").strip()
        results.append(
            NativeCaseResult(file or classname, title, _junit_status(case))
        )
    return results


def _contract_junit_adapter(runtime: object) -> Adapter | None:
    """Build a neutral adapter from a declared ``native_test_commands`` entry.

    A command opts into deterministic native collection by writing a JUnit
    report to the ``{report}`` placeholder (an already-supported runtime
    contract placeholder).  No language, framework, or filename is inspected:
    the repository declares *how* to run its tests and *where* to write JUnit.
    """

    commands = _runtime_field(runtime, "native_test_commands", ()) or ()
    for command in commands:
        if (
            not isinstance(command, Sequence)
            or isinstance(command, (str, bytes))
            or not command
        ):
            continue
        template = tuple(str(part) for part in command)
        if not any("{report}" in part for part in template):
            continue

        def _build(
            _targets: Sequence[str],
            report_path: str,
            _template: tuple[str, ...] = template,
        ) -> tuple[str, ...]:
            return tuple(
                part.replace("{report}", report_path)
                .replace("{repo}", "/workspace/repo")
                .replace("{assets}", "/assets")
                for part in _template
            )

        return Adapter(
            "contract-junit",
            parse_junit_xml,
            _build,
            report_suffix=".xml",
        )
    return None


def _mocha_command(targets: Sequence[str], report_path: str) -> tuple[str, ...]:
    # Execute the ``.bin`` shim directly rather than via ``node``: pnpm/yarn
    # create it as a ``#!/bin/sh`` wrapper, which ``node <shim>`` mis-parses as
    # JavaScript.  A bare ``.bin`` shim (or npm symlink) has a shebang and runs
    # under either package manager.
    return (
        "node_modules/.bin/mocha",
        *targets,
        "--reporter",
        "json",
        "--reporter-options",
        f"output={report_path}",
        "--exit",
    )


def _vitest_command(targets: Sequence[str], report_path: str) -> tuple[str, ...]:
    return (
        "node_modules/.bin/vitest",
        "run",
        # ``runner`` loads the (often TypeScript) config directly instead of
        # bundling it, which would otherwise write a temp file next to the
        # config and fail when the workspace is read-only.
        "--configLoader",
        "runner",
        *targets,
        # Node may finish a graceful close after its keep-alive timeout (6 s).
        # Preserve behavioral assertions without imposing Vitest's unrelated 5 s default.
        "--testTimeout=15000",
        "--reporter=json",
        f"--outputFile={report_path}",
    )


def _pytest_command(targets: Sequence[str], report_path: str) -> tuple[str, ...]:
    return (
        "python",
        "-m",
        "pytest",
        *targets,
        "--json-report",
        f"--json-report-file={report_path}",
    )


_MOCHA_ADAPTER = Adapter("mocha", parse_mocha_json, _mocha_command)
_VITEST_ADAPTER = Adapter("vitest", parse_vitest_json, _vitest_command)
_PYTEST_ADAPTER = Adapter("pytest", parse_pytest_json, _pytest_command)


def _runtime_field(runtime: object, name: str, default: object = ()) -> object:
    """Read a runtime field from either an object or a mapping.

    Authoring passes the parsed ``RuntimeContract`` object while the verifier
    passes the projected ``runtime.json`` mapping; both must yield the same
    collection behavior so the frozen source baseline and the scored candidate
    agree.
    """

    if isinstance(runtime, Mapping):
        return runtime.get(name, default)
    return getattr(runtime, name, default)


def _detection_tokens(runtime: object) -> list[str]:
    tokens: list[str] = []
    technology = _runtime_field(runtime, "technology", None)
    if isinstance(technology, Mapping):
        for key in ("toolchain", "framework", "language"):
            item = technology.get(key)
            if isinstance(item, Mapping):
                name = item.get("name")
                if isinstance(name, str) and name.strip():
                    tokens.append(name)
    for field in ("native_test_commands", "build_commands", "preflight_commands"):
        commands = _runtime_field(runtime, field, ()) or ()
        for command in commands:
            if isinstance(command, Sequence) and not isinstance(command, (str, bytes)) and command:
                # Scan every argv item, not only the executable: runners are
                # routinely invoked as ``python -m pytest`` / ``node .../vitest``
                # where the framework name is not argv[0].
                for part in command:
                    name = Path(str(part)).name
                    if name:
                        tokens.append(name)
    harness = _runtime_field(runtime, "harness_command", ()) or ()
    if isinstance(harness, Sequence) and not isinstance(harness, (str, bytes)) and harness:
        for part in harness:
            name = Path(str(part)).name
            if name:
                tokens.append(name)
    return tokens


def _adapter_for_tokens(text: str) -> Adapter | None:
    lowered = text.casefold()
    if "vitest" in lowered:
        return _VITEST_ADAPTER
    if "mocha" in lowered:
        return _MOCHA_ADAPTER
    if "pytest" in lowered:
        return _PYTEST_ADAPTER
    return None


def _package_manifest(repo_root: Path) -> Mapping[str, Any] | None:
    path = Path(repo_root) / "package.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, Mapping) else None


def _repo_detection_groups(repo_root: Path) -> list[list[str]]:
    """Infer a framework from a repository when runtime metadata is generic.

    Signals are grouped by strength so a leftover framework in
    ``devDependencies`` cannot outrank the declared ``scripts.test`` command of
    an already-migrated repository.
    """

    root = Path(repo_root)
    script_tokens: list[str] = []
    dependency_tokens: list[str] = []
    marker_tokens: list[str] = []
    manifest = _package_manifest(root)
    if manifest is not None:
        scripts = manifest.get("scripts")
        if isinstance(scripts, Mapping):
            test_script = scripts.get("test")
            if isinstance(test_script, str):
                script_tokens.append(test_script)
        for key in ("dependencies", "devDependencies", "peerDependencies"):
            declared = manifest.get(key)
            if isinstance(declared, Mapping):
                dependency_tokens.extend(str(name) for name in declared)
    for name in (".mocharc.js", ".mocharc.cjs", ".mocharc.mjs", ".mocharc.json", ".mocharc.yml", ".mocharc.yaml"):
        if (root / name).is_file():
            marker_tokens.append("mocha")
    for name in ("vitest.config.js", "vitest.config.ts", "vitest.config.mjs", "vitest.config.mts"):
        if (root / name).is_file():
            marker_tokens.append("vitest")
    for name in ("pytest.ini", "conftest.py", "tox.ini"):
        if (root / name).is_file():
            marker_tokens.append("pytest")
    return [script_tokens, marker_tokens, dependency_tokens]


def detect_adapter(runtime: object, *, repo_root: Path | None = None) -> Adapter | None:
    """Select a deterministic adapter from runtime technology, executables, or repo."""

    adapter = _adapter_for_tokens(" ".join(_detection_tokens(runtime)))
    if adapter is not None:
        return adapter
    if repo_root is not None:
        for tokens in _repo_detection_groups(repo_root):
            if not tokens:
                continue
            adapter = _adapter_for_tokens(" ".join(tokens))
            if adapter is not None:
                return adapter
    return None


def _repo_relative(value: object, root: Path) -> str:
    """Strip an in-container repository root from a reported test path.

    Reporters emit the absolute path they ran under.  Inside a Docker contract
    container that is ``/workspace/repo`` (handled by ``_norm_file``), but the
    Harbor verifier runs the collector in-process with the candidate root as its
    working directory (for example ``/tmp/harbor-candidate``), so the reported
    path must be made repository-relative before the frozen observation mapping.
    """

    text = str(value or "").strip().replace("\\", "/")
    prefix = str(root).replace("\\", "/").rstrip("/") + "/"
    if prefix != "/" and text.startswith(prefix):
        return text[len(prefix):]
    return text


def _norm_file(value: object) -> str:
    """Normalize a reported/declared test file to a repo-relative POSIX path."""

    text = str(value or "").strip().replace("\\", "/")
    if not text:
        return ""
    if "://" in text:
        scheme, _, remainder = text.partition("://")
        if scheme.casefold() == "file":
            text = remainder
        else:
            text = "/" + remainder.split("/", 1)[-1]
    for prefix in _REPO_ROOTS:
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    parts = [part for part in text.split("/") if part not in {"", "."}]
    for index, part in enumerate(parts):
        if part.casefold() in _TEST_ANCHORS:
            return "/".join(parts[index:])
    return "/".join(parts)


def _normalize_title(value: object) -> str:
    return " ".join(str(value or "").split())


def _matches_title(full_title: object, title: object) -> bool:
    full = _normalize_title(full_title)
    wanted = _normalize_title(title)
    if not full or not wanted:
        return False
    return (
        full == wanted
        or full.endswith(" " + wanted)
        or full.endswith(" › " + wanted)
    )


def _contains_title(full_title: object, title: object) -> bool:
    """Containment fallback for suite (``describe``) surfaces.

    A ``describe('...')`` title is discovered as a ``test:<file>#<title>``
    surface but reporters render it *inside* the case's ``fullName`` (e.g.
    ``suite describe case``), so it never suffix-matches.  Containment is safe
    here because reporter names are space-joined whole titles; it is only used
    when no exact/suffix match exists.
    """

    full = _normalize_title(full_title)
    wanted = _normalize_title(title)
    if not full or not wanted:
        return False
    return wanted in full


def _surface(row: Mapping[str, object]) -> str:
    coverage = row.get("coverage")
    if isinstance(coverage, Mapping):
        return str(coverage.get("surface", "")).strip()
    return ""


def native_test_targets(inventory: CaseObservationInventory) -> tuple[str, ...]:
    """Return the deduplicated P2P native-test files to execute."""

    targets: list[str] = []
    for row in inventory.p2p:
        if not is_native_test_row(row):
            continue
        candidate = native_test_file_of_row(row).strip()
        if candidate and candidate not in targets:
            targets.append(candidate)
    return tuple(targets)


@dataclasses.dataclass(frozen=True)
class NativeTestCollection:
    role: str
    adapter: str | None
    command: tuple[str, ...]
    results: tuple[NativeCaseResult, ...] = ()
    timed_out: bool = False
    parsed_count: int = 0
    targets: tuple[str, ...] = ()
    diagnostics: tuple[Mapping[str, object], ...] = ()

    @property
    def available(self) -> bool:
        return bool(self.results)

    def as_dict(self) -> dict[str, object]:
        return {
            "role": self.role,
            "adapter": self.adapter,
            "command": list(self.command),
            "timed_out": self.timed_out,
            "parsed_count": self.parsed_count,
            "targets": list(self.targets),
            "diagnostics": [dict(row) for row in self.diagnostics],
            "results": [
                {
                    "file": result.file,
                    "full_title": result.full_title,
                    "status": result.status,
                }
                for result in self.results
            ],
        }


def _timed_out(exc: BaseException) -> bool:
    if isinstance(exc, subprocess.TimeoutExpired):
        return True
    return isinstance(exc, PipelineError) and "timed out" in str(exc).casefold()


def collect_native_test_results(
    *,
    role: str,
    runtime: object,
    repo_root: Path,
    executor: object,
    image: object,
    assets_root: Path,
    evidence_root: Path,
    timeout: float,
    targets: Sequence[str],
) -> NativeTestCollection:
    """Run the role's bound native tests and parse a deterministic case list."""

    requested = tuple(str(item).strip() for item in targets if str(item).strip())
    adapter = detect_adapter(runtime, repo_root=repo_root)
    if adapter is None:
        # Tier B: no framework adapter matched, so fall back to the declared
        # native test command that writes a JUnit report.  This keeps native
        # P2P available for any ecosystem without a framework-specific parser.
        adapter = _contract_junit_adapter(runtime)
    if adapter is None:
        return NativeTestCollection(
            role=role,
            adapter=None,
            command=(),
            targets=requested,
            diagnostics=(
                {
                    "role": role,
                    "reason": "runner-missing",
                    "detail": "no native-test adapter matched the runtime technology",
                },
            ),
        )

    root = Path(repo_root)
    existing = [item for item in requested if (root / item).is_file()]
    missing = [item for item in requested if item not in existing]
    heavy = [item for item in existing if _is_heavy_target(item)]
    present = tuple(item for item in existing if item not in heavy)
    diagnostics: list[Mapping[str, object]] = [
        {"role": role, "reason": "missing-file", "file": item} for item in missing
    ]
    diagnostics.extend(
        {"role": role, "reason": "excluded-heavy-suite", "file": item}
        for item in heavy
    )
    if not present:
        diagnostics.append(
            {
                "role": role,
                "reason": "no-results",
                "detail": "no lightweight bound test files exist",
            }
        )
        return NativeTestCollection(
            role=role,
            adapter=adapter.name,
            command=(),
            targets=requested,
            diagnostics=tuple(diagnostics),
        )

    report_name = f"native-tests-{role}{adapter.report_suffix}"
    report_container_path = f"/logs/{report_name}"
    evidence = Path(evidence_root)
    evidence.mkdir(parents=True, exist_ok=True)
    report_path = evidence / report_name
    if report_path.is_symlink():
        raise PipelineError(f"native test report path is unsafe: {report_path}")
    report_path.unlink(missing_ok=True)

    environment = dict(_runtime_field(runtime, "environment", {}) or {})
    materializations = tuple(
        _runtime_field(runtime, "workspace_materializations", ()) or ()
    )

    def _execute(cmd: Sequence[str]) -> tuple[bool, str]:
        try:
            executor.run(  # type: ignore[attr-defined]
                image,
                input_root=root,
                assets_root=Path(assets_root),
                evidence_root=evidence,
                command=cmd,
                environment=environment,
                network="none",
                writable_input=False,
                timeout=timeout,
                workspace_materializations=materializations,
            )
        except Exception as exc:  # classified below and never re-raised
            if _timed_out(exc):
                return True, ""
            return False, f"{type(exc).__name__}: {exc}"
        return False, ""

    command = adapter.build_command(present, report_container_path)
    timed_out, run_error = _execute(command)
    # Some runners need a writable workspace (e.g. vitest bundling a TS config
    # writes a temp file next to it).  The vitest command opts out via
    # ``--configLoader runner``; if an older vitest rejects the flag, retry once
    # without it so the runner-neutral write is the only difference.
    if not report_path.is_file() and "--configLoader" in command:
        retry = tuple(
            item
            for index, item in enumerate(command)
            if item != "--configLoader"
            and not (index > 0 and command[index - 1] == "--configLoader")
        )
        timed_out, run_error = _execute(retry)
        command = retry

    results: tuple[NativeCaseResult, ...] = ()
    if report_path.is_file():
        try:
            parsed = adapter.parse(report_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            diagnostics.append(
                {"role": role, "reason": "unparseable-report", "detail": str(exc)}
            )
        else:
            results = tuple(
                dataclasses.replace(result, file=_repo_relative(result.file, root))
                for result in parsed
            )
    if timed_out:
        diagnostics.append({"role": role, "reason": "timeout"})
    elif run_error:
        diagnostics.append(
            {"role": role, "reason": "runner-failed", "detail": run_error}
        )
    elif not results and adapter.name == "contract-junit" and not report_path.is_file():
        diagnostics.append(
            {
                "role": role,
                "reason": "report-missing",
                "detail": f"declared native_test_commands did not write the report {report_path}",
            }
        )
    elif not results:
        diagnostics.append(
            {
                "role": role,
                "reason": "no-results",
                "detail": "runner produced no enumerated cases",
            }
        )

    return NativeTestCollection(
        role=role,
        adapter=adapter.name,
        command=command,
        results=results,
        timed_out=timed_out,
        parsed_count=len(results),
        targets=requested,
        diagnostics=tuple(diagnostics),
    )


def _resolved_status(statuses: Iterable[NativeStatus]) -> str:
    values = list(statuses)
    if any(value == "FAIL" for value in values):
        return "FAIL"
    if any(value == "PASS" for value in values):
        return "PASS"
    return "INCONCLUSIVE"


def map_p2p_rows(
    inventory: CaseObservationInventory,
    results_by_role: Mapping[str, NativeTestCollection],
) -> tuple[dict[str, list[dict[str, object]]], list[dict[str, object]]]:
    """Map parsed native results onto the frozen native P2P observations."""

    rows_by_role: dict[str, list[dict[str, object]]] = {}
    diagnostics: list[dict[str, object]] = []
    for role, collection in results_by_role.items():
        by_file: dict[str, list[NativeCaseResult]] = {}
        for result in collection.results:
            by_file.setdefault(_norm_file(result.file), []).append(result)
        targeted = {_norm_file(item) for item in collection.targets}
        rows: list[dict[str, object]] = []
        for row in inventory.p2p:
            if not is_native_test_row(row):
                continue
            surface = _surface(row)
            file = native_test_file_of_row(row)
            title = surface.split("#", 1)[1] if "#" in surface else ""
            normalized_file = _norm_file(file)
            candidates = by_file.get(normalized_file, [])
            reason = ""
            matched_by = ""
            if not candidates:
                if not collection.results:
                    reason = "no-results"
                elif normalized_file not in targeted:
                    reason = "missing-file"
                else:
                    reason = "no-results"
                status = "INCONCLUSIVE"
            else:
                matched = [
                    candidate
                    for candidate in candidates
                    if _matches_title(candidate.full_title, title)
                ]
                matched_by = "title" if matched else ""
                if not matched:
                    # Suite (describe) surfaces appear inside the reported
                    # fullName rather than at its end; fall back to containment
                    # so the whole suite's cases resolve the observation.
                    matched = [
                        candidate
                        for candidate in candidates
                        if _contains_title(candidate.full_title, title)
                    ]
                    matched_by = "title-contains" if matched else ""
                if not matched:
                    # A migration that renamed/removed the case leaves no
                    # evidence for this observation; keep it INCONCLUSIVE rather
                    # than borrowing the file's aggregate status (which would
                    # mask a missing test as a PASS).
                    reason = "unmatched-title"
                    matched_by = "unmatched"
                    status = "INCONCLUSIVE"
                else:
                    status = _resolved_status(candidate.status for candidate in matched)
            rows.append(
                {
                    "id": str(row["id"]),
                    "kind": "P2P",
                    "status": status,
                    "value": {
                        "type": "json",
                        # Only the behavioral outcome belongs in the value.
                        # The file/title are role/tool identity and remain in
                        # the private collection diagnostics; including them
                        # here makes cross-language P2P compare fail even when
                        # both sides PASS.
                        "value": {"outcome": status},
                    },
                }
            )
            if reason:
                diagnostics.append(
                    {
                        "role": role,
                        "observation_id": str(row["id"]),
                        "reason": reason,
                        "file": file,
                        "title": title,
                    }
                )
            elif matched_by in {"title-contains", "file-fallback"}:
                diagnostics.append(
                    {
                        "role": role,
                        "observation_id": str(row["id"]),
                        "reason": "matched-by",
                        "matched_by": matched_by,
                        "file": file,
                        "title": title,
                    }
                )
        rows_by_role[role] = rows
    return rows_by_role, diagnostics


def native_p2p_report_rows(
    inventory: CaseObservationInventory,
    collection: NativeTestCollection,
) -> list[dict[str, object]]:
    rows_by_role, _diagnostics = map_p2p_rows(inventory, {collection.role: collection})
    return rows_by_role.get(collection.role, [])


def merge_role_observation_rows(
    *,
    inventory: CaseObservationInventory,
    role: str,
    model_observations: Sequence[Mapping[str, object]],
    collection: NativeTestCollection,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    """Merge collector P2P rows over the model report, fail-closed on gaps.

    When the collector is unavailable (no adapter/runner or no parsed results)
    the model report is preserved unchanged so existing harness behavior is not
    regressed.  Otherwise the collector is authoritative for native P2P rows,
    and any frozen observation still absent is emitted INCONCLUSIVE.
    """

    summary: dict[str, object] = {
        "role": role,
        "available": collection.available,
        "adapter": collection.adapter,
        "parsed_count": collection.parsed_count,
        "timed_out": collection.timed_out,
        "diagnostics": [dict(row) for row in collection.diagnostics],
    }
    model_rows = [dict(row) for row in model_observations if isinstance(row, Mapping)]
    if not collection.available:
        return model_rows, summary

    rows_by_role, map_diagnostics = map_p2p_rows(inventory, {role: collection})
    collector_rows = rows_by_role.get(role, [])
    summary["diagnostics"] = [
        *[dict(row) for row in collection.diagnostics],
        *[dict(row) for row in map_diagnostics],
    ]
    collector_by_id = {row["id"]: row for row in collector_rows}
    merged: list[dict[str, object]] = []
    seen: set[str] = set()
    for row in model_rows:
        identifier = str(row.get("id"))
        if identifier in collector_by_id:
            merged.append(collector_by_id[identifier])
        else:
            merged.append(row)
        seen.add(identifier)
    for row in collector_rows:
        if row["id"] not in seen:
            merged.append(row)
            seen.add(row["id"])
    for observation in inventory.all_observations():
        identifier = str(observation["id"])
        if identifier in seen:
            continue
        merged.append(
            {
                "id": identifier,
                "kind": str(observation["kind"]),
                "status": "INCONCLUSIVE",
                "value": {
                    "type": "json",
                    "value": {"reason": "missing-observation", "source": "collector"},
                },
            }
        )
        seen.add(identifier)
    return merged, summary


__all__ = [
    "Adapter",
    "NativeCaseResult",
    "NativeStatus",
    "NativeTestCollection",
    "collect_native_test_results",
    "detect_adapter",
    "map_p2p_rows",
    "merge_role_observation_rows",
    "native_p2p_report_rows",
    "native_test_targets",
    "parse_junit_xml",
    "parse_mocha_json",
    "parse_pytest_json",
    "parse_vitest_json",
]
