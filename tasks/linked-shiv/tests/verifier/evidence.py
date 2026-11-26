"""Evidence derivation for the fixed Migration Grader hardgate contract."""

from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from .fallback import scan_deceptive_fallbacks
    from .residue import scan_residue_rules
except ImportError:  # Standalone Runtime Draft layout.
    from fallback import scan_deceptive_fallbacks
    from residue import scan_residue_rules


_TEST_NAME = re.compile(
    r"(?:^|/)(?:test[^/]*|[^/]*(?:\.test|\.spec)\.[^/]*)$",
    re.IGNORECASE,
)
_TEST_MODULE_NAME = re.compile(
    r"^(?:test[^/]*|[^/]*_test\.py|[^/]*\.(?:test|spec)\.[^/]*)$",
    re.IGNORECASE,
)
_SKIP_MARKERS = (
    # Keep the runtime helper separate from ``test.skip`` below.  The latter
    # must have a non-word boundary so ``pytest.skip(...)`` is not classified
    # as a disabled test merely because it contains the substring ``test.skip``.
    re.compile(r"pytest\.mark\.skip(?:if|unless)?\b|pytest\.skip\s*\(", re.IGNORECASE),
    re.compile(r"pytest\.mark\.xfail\b", re.IGNORECASE),
    re.compile(r"(?<![A-Za-z0-9_])(?:describe|it|test)\.skip\s*\(", re.IGNORECASE),
    re.compile(r"(?<![A-Za-z0-9_])(?:describe|it|test)\.only\s*\(", re.IGNORECASE),
    re.compile(r"@(?:unittest\.)?skip(?:If|Unless)?\b", re.IGNORECASE),
    re.compile(r"\b(?:skip|xfail|disabled)\s*=\s*true\b", re.IGNORECASE),
)


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _normalized_digest(value: object) -> str:
    """Normalize legacy bare SHA-256 values to the canonical digest form."""

    text = str(value).strip().lower()
    return text if text.startswith("sha256:") else f"sha256:{text}"


def _is_candidate_test_module(relative: str) -> bool:
    """Return True for a conventional test module a candidate may add.

    A candidate's own test module is not evaluator test tampering: it can only
    skip or fail itself.  Files that can change how the evaluator suite is
    discovered or executed (``__init__.py``, ``conftest.py``, helper modules,
    configuration) are deliberately not included.
    """

    return bool(_TEST_MODULE_NAME.match(relative.rsplit("/", 1)[-1]))


def _is_test(path: str) -> bool:
    parts = path.replace("\\", "/").split("/")
    return "test" in {part.casefold() for part in parts[:-1]} or bool(
        _TEST_NAME.search(path)
    )


_IGNORED_TREE_PARTS = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        # The fixture package is built in place by its own tests; ``build`` and
        # ``*.egg-info`` are generated outputs, not candidate-authored tests.
        "build",
        "dist",
        ".eggs",
        ".tox",
    }
)


def _ignored_tree_path(relative: str) -> bool:
    parts = Path(relative).parts
    if any(part in _IGNORED_TREE_PARTS for part in parts):
        return True
    if parts and parts[-1] == ".gitkeep":
        # Empty directory placeholders cannot execute or replace a test.  They
        # are commonly added to preserve an empty fixture directory in git.
        return True
    return any(
        part.endswith((".egg-info", ".dist-info")) for part in parts
    )


def _regular_files(root: Path) -> dict[str, Path]:
    if not root.is_dir() or root.is_symlink():
        return {}
    values: dict[str, Path] = {}
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if _ignored_tree_path(relative):
            continue
        if path.suffix in {".pyc", ".pyo"}:
            continue
        values[relative] = path
    return values


def _marker_counts(
    root: Path,
    *,
    relative_prefix: str = "",
    include_paths: set[str] | None = None,
) -> dict[str, int]:
    counts = {"skip": 0, "xfail": 0, "disabled": 0}
    for relative, path in _regular_files(root).items():
        if include_paths is not None and relative not in include_paths:
            continue
        test_relative = "/".join(part for part in (relative_prefix, relative) if part)
        if not _is_test(test_relative):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        counts["skip"] += sum(
            1 for pattern in _SKIP_MARKERS[:1] for _ in pattern.finditer(text)
        )
        counts["xfail"] += sum(
            1 for pattern in (_SKIP_MARKERS[1],) for _ in pattern.finditer(text)
        )
        counts["disabled"] += sum(
            1 for pattern in _SKIP_MARKERS[2:] for _ in pattern.finditer(text)
        )
    return counts


def build_source_test_baseline(root: Path) -> dict[str, object]:
    """Freeze source-visible test identities and marker counts for a Case."""

    files = {
        relative: {"sha256": _digest(path)}
        for relative, path in sorted(_regular_files(root).items())
        if _is_test(relative)
    }
    file_marker_counts = {
        relative: _marker_count_file(path)
        for relative, path in sorted(_regular_files(root).items())
        if relative in files
    }
    return {
        "schema_version": "migration-source-test-baseline:v1",
        "files": files,
        "marker_counts": _marker_counts(root),
        "file_marker_counts": file_marker_counts,
    }


def _atom(
    status: str,
    *,
    source: str,
    evidence: object = None,
    applicable: bool | None = None,
) -> dict[str, object]:
    normalized = str(status).upper()
    if normalized == "TIMEOUT":
        normalized = "INFRA_ERROR"
    return {
        "status": normalized,
        "applicable": normalized != "NA" if applicable is None else bool(applicable),
        "source": source,
        "evidence": [] if evidence is None else evidence,
    }


def _status(value: object, *, source: str, default: str = "INCONCLUSIVE") -> dict[str, object]:
    if isinstance(value, Mapping) and isinstance(value.get("status"), str):
        applicable = value.get("applicable")
        if applicable is not None and not isinstance(applicable, bool):
            return _atom("INFRA_ERROR", source=source, evidence={"value": dict(value)})
        return _atom(
            str(value["status"]),
            source=str(value.get("source", source)),
            evidence=value.get("evidence", []),
            applicable=applicable,
        )
    if isinstance(value, bool):
        return _atom("PASS" if value else "FAIL", source=source)
    if value is None:
        return _atom(default, source=source)
    return _atom(str(value), source=source)


def _rows(observations: object) -> list[Mapping[str, Any]]:
    return [
        row for row in observations
        if isinstance(row, Mapping)
    ] if isinstance(observations, Sequence) and not isinstance(observations, (str, bytes)) else []


def _receipt_identity(receipt: Mapping[str, Any]) -> str:
    command = receipt.get("command", [])
    return json.dumps(
        {
            "kind": receipt.get("kind"),
            "phase": receipt.get("phase"),
            "index": receipt.get("index"),
            "command": command,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _derive_execution_atoms(
    receipts: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, object], dict[str, object], list[dict[str, object]]]:
    accepted_kinds = {
        "environment-build",
        "target-build",
        "target-native-test",
        "dependency-resolution",
        "import-probe",
        "version-probe",
        "target-execution",
        "report",
    }
    valid: list[dict[str, object]] = []
    malformed: list[dict[str, object]] = []
    seen: dict[str, tuple[object, object, object]] = {}
    contradictions: list[dict[str, object]] = []
    for offset, raw in enumerate(receipts):
        receipt = dict(raw)
        kind = receipt.get("kind")
        if (
            receipt.get("schema_version") != "migration-execution-receipt:v1"
            or kind not in accepted_kinds
            or not isinstance(receipt.get("timed_out"), bool)
        ):
            malformed.append({"index": offset, "receipt": receipt})
            continue
        returncode = receipt.get("returncode")
        if returncode is not None and (
            not isinstance(returncode, int) or isinstance(returncode, bool)
        ):
            malformed.append({"index": offset, "receipt": receipt})
            continue
        identity = _receipt_identity(receipt)
        facts = (
            returncode,
            receipt.get("timed_out"),
            receipt.get("report_present"),
        )
        if identity in seen and seen[identity] != facts:
            contradictions.append(
                {"identity": identity, "first": seen[identity], "second": facts}
            )
        else:
            seen[identity] = facts
        valid.append(receipt)
    evidence = {
        "receipts": valid,
        "malformed": malformed,
        "contradictions": contradictions,
    }
    if contradictions:
        atom = _atom("INFRA_ERROR", source="execution-receipts", evidence=evidence)
        return atom, atom, valid
    if any(bool(row.get("timed_out")) for row in valid):
        atom = _atom("INFRA_ERROR", source="execution-receipts", evidence=evidence)
        return atom, atom, valid

    environment = [row for row in valid if row.get("kind") == "environment-build"]
    target_build = [
        row
        for row in valid
        if row.get("kind") in {"target-build", "target-native-test"}
    ]
    dependency = [
        row
        for row in valid
        if row.get("kind") in {"dependency-resolution", "import-probe", "version-probe"}
    ]
    executions = [row for row in valid if row.get("kind") == "target-execution"]
    reports = [row for row in valid if row.get("kind") == "report"]
    report_present = any(row.get("report_present") is True for row in executions)
    report_valid = not reports or all(row.get("valid") is True for row in reports)

    if any(row.get("returncode") not in {0, None} for row in environment):
        build = _atom("INFRA_ERROR", source="environment-build-receipt", evidence=evidence)
    elif any(row.get("returncode") not in {0, None} for row in target_build):
        build = _atom("FAIL", source="target-build-receipt", evidence=evidence)
    elif target_build and all(row.get("returncode") == 0 for row in target_build):
        build = _atom("PASS", source="target-build-receipt", evidence=evidence)
    # The harness propagates pytest's exit status, so a nonzero returncode can
    # simply mean an F2P expectation failed.  It is a test-result signal, not a
    # dependency/build failure, once the evaluator-owned report is present and
    # valid.  Target-build/native-test failures above still take precedence.
    elif report_present and report_valid:
        build = _atom("PASS", source="target-execution-receipt", evidence=evidence)
    elif any(row.get("returncode") not in {0, None} for row in executions):
        build = _atom("FAIL", source="target-execution-receipt", evidence=evidence)
    else:
        build = _atom("INCONCLUSIVE", source="execution-receipts", evidence=evidence)

    if any(row.get("returncode") not in {0, None} for row in dependency):
        dependencies = _atom(
            "FAIL", source="dependency-probe-receipts", evidence=evidence
        )
    elif dependency and all(row.get("returncode") == 0 for row in dependency):
        dependencies = _atom(
            "PASS", source="dependency-probe-receipts", evidence=evidence
        )
    # Same reasoning as ``build``: pytest's nonzero exit is a test-result
    # signal, not evidence that dependencies could not be resolved, when the
    # evaluator-owned report is present and valid.
    elif report_present and report_valid:
        dependencies = _atom(
            "PASS", source="target-execution-receipt", evidence=evidence
        )
    elif any(row.get("returncode") not in {0, None} for row in executions):
        dependencies = _atom(
            "FAIL", source="target-execution-receipt", evidence=evidence
        )
    else:
        dependencies = _atom(
            "INCONCLUSIVE", source="execution-receipts", evidence=evidence
        )
    return build, dependencies, valid


def _group_status(
    rows: Sequence[Mapping[str, Any]],
    *,
    expected: int | None,
    source: str,
    empty: str = "NA",
) -> dict[str, object]:
    if expected == 0 and not rows:
        return _atom(empty, source=source, evidence={"total": 0, "checks": []})
    if expected is None and not rows:
        return _atom(empty, source=source, evidence={"total": 0, "checks": []})
    if not rows:
        return _atom("INCONCLUSIVE", source=source, evidence={"total": 0, "checks": []})
    statuses = [str(row.get("status", "INCONCLUSIVE")) for row in rows]
    count_mismatch = expected is not None and len(rows) != expected
    status = (
        "INFRA_ERROR" if any(item in {"INFRA_ERROR", "TIMEOUT"} for item in statuses)
        else "INCONCLUSIVE" if any(item in {"INCONCLUSIVE", "NA"} for item in statuses)
        else "INCONCLUSIVE" if count_mismatch else "FAIL" if any(item == "FAIL" for item in statuses)
        else "PASS"
    )
    return _atom(
        status,
        source=source,
        evidence={
            "passed": sum(item == "PASS" for item in statuses),
            "total": len(statuses),
            "expected": expected,
            "count_mismatch": count_mismatch,
            "checks": [dict(row) for row in rows],
        },
    )


def _changed_paths(root: Path) -> set[str] | None:
    """Return candidate paths changed from HEAD, including untracked files."""

    if not (root / ".git").exists():
        return None
    try:
        tracked = subprocess.run(
            ["git", "-C", str(root), "diff", "--name-only", "--diff-filter=ACDMRTUXB", "HEAD", "--"],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=True,
        )
        untracked = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--others", "--exclude-standard"],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return {
        line.replace("\\", "/").strip()
        for output in (tracked.stdout, untracked.stdout)
        for line in output.splitlines()
        if line.strip()
    }


def _protected_hits(root: Path, protected: Sequence[str]) -> list[str]:
    """Return protected paths that were changed, not merely present."""

    changed = _changed_paths(root)
    if changed is None:
        return []
    hits: list[str] = []
    for relative in changed:
        normalized = relative.replace("\\", "/").strip("/")
        for raw in protected:
            path = str(raw).replace("\\", "/").strip("/")
            if path and (normalized == path or normalized.startswith(path + "/")):
                hits.append(normalized)
                break
    return sorted(set(hits))


def _report_marker_hits(
    root: Path,
    markers: Sequence[str],
    changed_paths: set[str] | None = None,
) -> list[dict[str, str]]:
    hits: list[dict[str, str]] = []
    # Without a trustworthy change set, a marker's mere presence is not
    # evidence that the candidate introduced it.  Avoid converting that
    # uncertainty into an anti-cheating failure.
    if not markers or changed_paths is None:
        return hits
    for relative, path in _regular_files(root).items():
        if changed_paths is not None and relative not in changed_paths:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for marker in markers:
            if str(marker) and str(marker).casefold() in text.casefold():
                hits.append({"path": relative, "marker": str(marker)})
    return hits


_SCORING_INTERFERENCE_PATTERNS = (
    re.compile(r"\bObservationPlugin\b"),
    re.compile(r"\b_qa_rows\b"),
    re.compile(r"\bFunction\.runtest\b"),
    re.compile(r"\bpytest_runtest_(?:makereport|logreport|setup|call|teardown)\b"),
    re.compile(r"\bpytest_collection_modifyitems\b"),
    re.compile(r"\bpytest_report_(?:header|teststatus)\b"),
    re.compile(r"\bsys\.modules\s*(?:\.\s*get\s*)?\(\s*['\"]__main__['\"]"),
    re.compile(r"\bsys\.modules\s*\[\s*['\"]__main__['\"]\s*\]"),
    re.compile(r"\bHARDCODED_VERIFIER_PASS\b"),
)


def _scoring_interference_hits(
    root: Path,
    changed_paths: set[str] | None = None,
) -> list[dict[str, str]]:
    """Find candidate source that reaches into the scoring process.

    This is deliberately limited to candidate-owned source/configuration.  A
    legitimate migration has no reason to inspect pytest's runner internals,
    the observation plugin, or the ``__main__`` module.  Without a trustworthy
    change set the scan is still safe because these tokens are absent from the
    untouched project source.
    """

    hits: list[dict[str, str]] = []
    for relative, path in _regular_files(root).items():
        if _is_test(relative):
            continue
        if not relative.endswith((".py", ".cfg", ".toml", ".ini")):
            continue
        if changed_paths is not None and relative not in changed_paths:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pattern in _SCORING_INTERFERENCE_PATTERNS:
            match = pattern.search(text)
            if match is not None:
                hits.append(
                    {
                        "path": relative,
                        "token": match.group(0),
                        "kind": "scoring-interference",
                    }
                )
                break
    return hits


def _public_contract_selection(
    p2p: Sequence[Mapping[str, Any]],
    *,
    config: Mapping[str, Any],
    policy: Mapping[str, Any],
    expected_p2p: int | None,
) -> tuple[list[Mapping[str, Any]], int | None, str]:
    """Select explicit public-surface observations when a case declares them.

    Older case bundles do not carry a separate public-surface inventory. For
    those bundles the P2P observations are the available observable contract
    evidence; the source label records this compatibility fallback.
    """

    declared = policy.get("public_surface_evidence", config.get("public_surface_evidence", []))
    ids: set[str] = set()
    if isinstance(declared, Sequence) and not isinstance(declared, (str, bytes)):
        for entry in declared:
            if isinstance(entry, Mapping):
                observation_ids = entry.get("observation_ids", [])
                if isinstance(observation_ids, Sequence) and not isinstance(observation_ids, (str, bytes)):
                    ids.update(str(item) for item in observation_ids if str(item))
            elif isinstance(entry, str) and entry:
                ids.add(entry)
    if ids:
        selected = [row for row in p2p if str(row.get("id")) in ids]
        return selected, len(ids), "candidate-public-contract-observations"
    return list(p2p), expected_p2p, "candidate-observations-compatibility-fallback"


def _pytest_hook_hits(
    root: Path,
    baseline_files: Mapping[str, Any],
    changed_paths: set[str] | None,
) -> list[dict[str, str]]:
    """Return candidate-owned pytest hook files or plugin declarations.

    Evaluator tests run from the candidate tree so they can import the
    migrated code, which also means pytest will discover a candidate-authored
    root ``conftest.py``.  That file can rewrite test reports without touching
    an evaluator test.  The runner also cuts conftest discovery at ``test/`` and
    disables entry-point plugin autoload; this inventory makes the attempt an
    explicit integrity failure instead of silently ignoring it.
    """

    hits: list[dict[str, str]] = []
    baseline = {str(path).replace("\\", "/").strip("/") for path in baseline_files}
    config_names = {"pytest.ini", "tox.ini", "pyproject.toml", "setup.cfg"}
    plugin_patterns = (
        re.compile(r"\bpytest11\b", re.IGNORECASE),
        re.compile(r"\bpytest_plugins\b", re.IGNORECASE),
        re.compile(r"\brequired_plugins\b", re.IGNORECASE),
    )
    for relative, path in _regular_files(root).items():
        name = relative.rsplit("/", 1)[-1]
        if name == "conftest.py" and relative not in baseline:
            hits.append(
                {
                    "path": relative,
                    "kind": "conftest",
                    "reason": "candidate-owned pytest hook file",
                }
            )
            continue
        if name not in config_names:
            continue
        # Existing build configuration is normally untouched.  Inspect changed
        # files only when a trustworthy change set exists, plus any newly
        # introduced pytest.ini (which is not part of the source baseline).
        if changed_paths is not None and relative not in changed_paths and not (
            name == "pytest.ini" and relative not in baseline
        ):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pattern in plugin_patterns:
            match = pattern.search(text)
            if match is not None:
                hits.append(
                    {
                        "path": relative,
                        "kind": "pytest-plugin-config",
                        "reason": f"pytest plugin declaration: {match.group(0)}",
                    }
                )
                break
    return hits


def _marker_count_file(path: Path) -> dict[str, int]:
    """Count skip/xfail/disabled markers in one test file."""

    counts = {"skip": 0, "xfail": 0, "disabled": 0}
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return counts
    counts["skip"] = sum(
        1 for pattern in _SKIP_MARKERS[:1] for _ in pattern.finditer(text)
    )
    counts["xfail"] = sum(
        1 for pattern in (_SKIP_MARKERS[1],) for _ in pattern.finditer(text)
    )
    counts["disabled"] = sum(
        1 for pattern in _SKIP_MARKERS[2:] for _ in pattern.finditer(text)
    )
    return counts


def _qualified_name(node: ast.AST) -> str:
    """Return a dotted name for the simple references used by test markers."""

    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _qualified_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


_PLATFORM_VALUES = frozenset({"os.name", "sys.platform"})
_PLATFORM_CALLS = frozenset({"platform.system", "platform.platform"})
_PLATFORM_LITERALS = frozenset(
    {
        "aix",
        "cygwin",
        "darwin",
        "freebsd",
        "java",
        "linux",
        "linux2",
        "nt",
        "openbsd",
        "posix",
        "sunos",
        "win",
        "win32",
        "windows",
    }
)


def _platform_value(node: ast.AST) -> bool:
    if _qualified_name(node) in _PLATFORM_VALUES:
        return True
    return (
        isinstance(node, ast.Call)
        and _qualified_name(node.func) in _PLATFORM_CALLS
        and not node.args
        and not node.keywords
    )


def _platform_literal(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.casefold() in _PLATFORM_LITERALS
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)) and len(node.elts) == 1:
        return _platform_literal(node.elts[0])
    return False


def _platform_condition(node: ast.AST) -> bool:
    """Recognize conditions that can only select a platform-specific branch.

    The intentionally small grammar avoids turning a mere reference to
    ``os.name`` into a bypass.  In particular, OR expressions are rejected, so
    conditions such as ``os.name == 'nt' or True`` remain actionable.
    """

    if isinstance(node, ast.BoolOp):
        return isinstance(node.op, ast.And) and any(
            _platform_condition(value) for value in node.values
        )
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return _platform_condition(node.operand)
    if isinstance(node, ast.Compare) and len(node.ops) == len(node.comparators) == 1:
        if not isinstance(node.ops[0], (ast.Eq, ast.NotEq, ast.In, ast.NotIn)):
            return False
        right = node.comparators[0]
        return (_platform_value(node.left) and _platform_literal(right)) or (
            _platform_literal(node.left) and _platform_value(right)
        )
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        return (
            node.func.attr == "startswith"
            and _platform_value(node.func.value)
            and len(node.args) == 1
            and _platform_literal(node.args[0])
            and not node.keywords
        )
    return False


class _PlatformSkipVisitor(ast.NodeVisitor):
    """Count skip markers proven to be guarded by platform selection."""

    def __init__(self) -> None:
        self.counts = {"skip": 0, "xfail": 0, "disabled": 0}
        self._guard_depth = 0

    def visit_If(self, node: ast.If) -> None:  # noqa: N802 - ast visitor API
        self.visit(node.test)
        guarded = _platform_condition(node.test)
        if guarded:
            self._guard_depth += 1
        for statement in node.body:
            self.visit(statement)
        if guarded:
            self._guard_depth -= 1
        # An else branch of ``platform_condition and another_condition`` is
        # not necessarily platform-bounded, so it is deliberately unguarded.
        for statement in node.orelse:
            self.visit(statement)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 - ast visitor API
        name = _qualified_name(node.func)
        if name == "pytest.mark.skipif" and node.args and _platform_condition(node.args[0]):
            self.counts["skip"] += 1
        elif name == "pytest.skip" and self._guard_depth:
            self.counts["skip"] += 1
        elif name in {"unittest.skipIf", "unittest.skipUnless", "skipIf", "skipUnless"}:
            if node.args and _platform_condition(node.args[0]):
                self.counts["disabled"] += 1
        self.generic_visit(node)


def _actionable_marker_count_file(path: Path) -> dict[str, int]:
    """Count markers that can weaken tests on the current evaluator platform.

    Newly authored cross-platform tests commonly guard POSIX-only behavior on
    Windows (and vice versa).  Those guards add coverage without suppressing a
    failing test on the platform where it is applicable.  Everything that
    cannot be parsed or proven platform-conditional remains fail-closed.
    """

    counts = _marker_count_file(path)
    if path.suffix.casefold() != ".py" or not any(counts.values()):
        return counts
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, SyntaxError, ValueError):
        return counts
    visitor = _PlatformSkipVisitor()
    visitor.visit(tree)
    return {
        key: max(0, int(counts.get(key, 0)) - int(visitor.counts.get(key, 0)))
        for key in counts
    }


def _negative_marker_hits(
    *,
    baseline_file_markers: Mapping[str, Any],
    candidate_files: Mapping[str, Path],
    changed_test_paths: Sequence[str],
    added_test_paths: Sequence[str],
) -> list[dict[str, object]]:
    """Report only markers that a candidate added to a test file."""

    hits: list[dict[str, object]] = []
    for relative in changed_test_paths:
        path = candidate_files.get(relative)
        if path is None or relative not in baseline_file_markers:
            continue
        before = baseline_file_markers.get(relative)
        before = before if isinstance(before, Mapping) else {}
        after = _actionable_marker_count_file(path)
        delta = {
            key: int(after.get(key, 0)) - int(before.get(key, 0))
            for key in {"skip", "xfail", "disabled"}
        }
        if any(value > 0 for value in delta.values()):
            hits.append({"path": relative, "delta": delta})
    for relative in added_test_paths:
        path = candidate_files.get(relative)
        if path is None:
            continue
        after = _actionable_marker_count_file(path)
        if any(value > 0 for value in after.values()):
            hits.append({"path": relative, "after": after, "kind": "added-test"})
    return hits


def _symlink_test_paths(test_root: Path) -> list[str]:
    if not test_root.is_dir() or test_root.is_symlink():
        return []
    found: list[str] = []
    for path in test_root.rglob("*"):
        if path.is_symlink():
            found.append(path.relative_to(test_root).as_posix())
    return sorted(found)


def _test_integrity(
    candidate_root: Path,
    baseline: Mapping[str, Any] | None,
    config: Mapping[str, Any],
    changed_paths: set[str] | None = None,
    *,
    overlay_root: Path | None = None,
) -> tuple[dict[str, object], dict[str, object], list[dict[str, str]]]:
    """Check candidate-owned tests and the evaluator overlay separately.

    The candidate's original tree is inspected before the evaluator copies its
    frozen tests over it.  Only paths that the candidate actually changed are
    compared, so an untouched checkout (which intentionally lacks the newer
    evaluator test files) is not treated as tampering.  The overlay tree is
    then checked for extra files such as ``test/__init__.py`` and for a missing
    evaluator file.
    """

    if not isinstance(baseline, Mapping):
        inconclusive = _atom("INCONCLUSIVE", source="source-test-baseline")
        return inconclusive, inconclusive, []
    baseline_files = baseline.get("files", {})
    baseline_files = baseline_files if isinstance(baseline_files, Mapping) else {}
    baseline_rel = {
        str(relative).replace("\\", "/").strip("/"): record
        for relative, record in baseline_files.items()
    }
    candidate_files = _regular_files(candidate_root)

    changed_test_paths: list[str] = []
    if changed_paths is not None:
        changed_test_paths = sorted(
            relative
            for path in changed_paths
            if (relative := path.replace("\\", "/").strip("/")).startswith("test/")
            and not _ignored_tree_path(relative)
        )
    # A candidate may legitimately add or update its own copy of a test that is
    # also part of the frozen evaluator suite; the evaluator overwrites that
    # path before execution, so its content cannot affect scoring.  What it must
    # not do is introduce a *new* importable file (for example
    # ``test/__init__.py``) that survives the overlay and runs at pytest import
    # time.  The overlay inventory below catches both that and a missing frozen
    # file.
    added = sorted(
        relative for relative in changed_test_paths if relative not in baseline_rel
    )
    missing: list[str] = []
    changed: list[str] = []

    overlay_extra: list[str] = []
    overlay_missing: list[str] = []
    overlay_symlinks: list[str] = []
    if overlay_root is not None:
        test_root = overlay_root / "test" if (overlay_root / "test").is_dir() else overlay_root
        overlay_files = _regular_files(test_root)
        expected_overlay = {
            relative.removeprefix("test/")
            for relative in baseline_rel
            if relative.startswith("test/")
        }
        overlay_extra = sorted(set(overlay_files) - expected_overlay)
        overlay_missing = sorted(expected_overlay - set(overlay_files))
        overlay_symlinks = _symlink_test_paths(test_root)
    # A candidate may add its own test module; that is candidate-owned test
    # code, not tampering with the evaluator suite.  Only added files that can
    # alter discovery/execution (for example test/__init__.py or a helper) are
    # treated as an integrity failure.
    overlay_extra_tamper = [
        relative
        for relative in overlay_extra
        if not _is_candidate_test_module(relative)
    ]

    hook_hits = _pytest_hook_hits(candidate_root, baseline_rel, changed_paths)
    baseline_file_markers = baseline.get("file_marker_counts", {})
    baseline_file_markers = (
        baseline_file_markers if isinstance(baseline_file_markers, Mapping) else {}
    )
    marker_hits = _negative_marker_hits(
        baseline_file_markers=baseline_file_markers,
        candidate_files=candidate_files,
        # Existing frozen-path files are replaced by the evaluator overlay, so
        # only newly introduced test modules can contribute a live skip marker.
        changed_test_paths=[],
        added_test_paths=added,
    )
    marker_before = baseline.get("marker_counts", {})
    marker_before = marker_before if isinstance(marker_before, Mapping) else {}
    marker_after = {
        key: int(marker_before.get(key, 0)) + sum(
            int(item.get("delta", {}).get(key, 0))
            for item in marker_hits
            if isinstance(item.get("delta"), Mapping)
        )
        for key in {"skip", "xfail", "disabled"}
    }
    marker_delta = {
        key: int(marker_after.get(key, 0)) - int(marker_before.get(key, 0))
        for key in {"skip", "xfail", "disabled"}
    }
    tamper = _atom(
        "FAIL"
        if missing
        or changed
        or overlay_extra_tamper
        or overlay_missing
        or overlay_symlinks
        or hook_hits
        else "PASS",
        source="source-test-baseline",
        evidence={
            "changed_test_paths": changed_test_paths,
            "missing": missing,
            "changed": changed,
            "added": added,
            "overlay_extra": overlay_extra,
            "overlay_extra_tamper": overlay_extra_tamper,
            "overlay_missing": overlay_missing,
            "overlay_symlinks": overlay_symlinks,
            "pytest_hook_hits": hook_hits,
        },
    )
    skip = _atom(
        "FAIL" if marker_hits else "PASS",
        source="source-test-baseline",
        evidence={
            "before": dict(marker_before),
            "after": marker_after,
            "delta": marker_delta,
            "marker_hits": marker_hits,
        },
    )
    return tamper, skip, hook_hits


def build_hardgate_evidence(
    candidate_root: Path,
    *,
    config: Mapping[str, Any],
    observations: Sequence[Mapping[str, Any]],
    observation_errors: Sequence[str],
    receipts: Sequence[Mapping[str, Any]],
    patch_applicable: bool | None,
    source_test_baseline: Mapping[str, Any] | None,
    test_integrity_root: Path | None = None,
) -> dict[str, object]:
    """Derive hardgate atoms from Case evidence instead of policy booleans."""

    policy = config.get("policy", {})
    policy = policy if isinstance(policy, Mapping) else {}
    rows = _rows(observations)
    expected_tests = config.get("tests", {})
    expected_tests = expected_tests if isinstance(expected_tests, Mapping) else {}
    p2p = [row for row in rows if row.get("kind") == "P2P"]
    state = [row for row in rows if row.get("kind") == "STATE"]
    expected_p2p = (
        len(expected_tests.get("p2p", []))
        if isinstance(expected_tests.get("p2p", []), Sequence)
        and not isinstance(expected_tests.get("p2p", []), (str, bytes))
        else None
    )
    public_rows, expected_public, public_source = _public_contract_selection(
        p2p,
        config=config,
        policy=policy,
        expected_p2p=expected_p2p,
    )
    semantic_rules = policy.get("semantic_rules", [])
    residue_rules = policy.get("residue_rules", semantic_rules)
    residue_hits = scan_residue_rules(candidate_root, residue_rules)
    # Migration residue is ordinary candidate evidence. It must not become
    # anti-cheating evidence merely because a case has not declared a
    # separate deceptive-fallback inventory. A legacy dependency left in the
    # candidate is a failed migration, not proof of test or report tampering.
    deceptive_hits = scan_deceptive_fallbacks(candidate_root, policy)
    # Policy additions must not shadow the bundle's protected inventory.
    protected: list[str] = []
    for configured in (config.get("protected_paths", []), policy.get("protected_paths", [])):
        if isinstance(configured, Sequence) and not isinstance(configured, (str, bytes)):
            protected.extend(str(item) for item in configured)
    marker_names: list[str] = []
    for configured in (config.get("forbidden_report_markers", []), policy.get("forbidden_report_markers", [])):
        if isinstance(configured, Sequence) and not isinstance(configured, (str, bytes)):
            marker_names.extend(str(item) for item in configured)
    changed_paths = _changed_paths(candidate_root)
    protected_hits = _protected_hits(candidate_root, [str(item) for item in protected])
    marker_hits = _report_marker_hits(
        candidate_root,
        [str(item) for item in marker_names],
        changed_paths,
    )
    scoring_hits = _scoring_interference_hits(candidate_root, changed_paths)
    tests_not_tampered, no_new_skip, pytest_hook_hits = _test_integrity(
        candidate_root,
        source_test_baseline,
        config,
        changed_paths=changed_paths,
        overlay_root=test_integrity_root or candidate_root,
    )
    build_evidence, dependency_evidence, valid_receipts = _derive_execution_atoms(
        receipts
    )
    public = _group_status(
        public_rows,
        expected=expected_public,
        source=public_source,
        empty="NA",
    )
    p2p_no_regression = _group_status(
        p2p,
        expected=expected_p2p,
        source="candidate-p2p-observations",
        empty="NA",
    )
    state_applicable = bool(policy.get("state_applicable"))
    critical = _group_status(
        state,
        expected=len(expected_tests.get("state", [])) if isinstance(expected_tests.get("state", []), Sequence) else None,
        source="candidate-state-observations",
        empty="NA" if not state_applicable else "INCONCLUSIVE",
    )
    anti_evidence = {
        "observation_errors": list(observation_errors),
        "protected_hits": protected_hits,
        "report_marker_hits": marker_hits,
        "pytest_hook_hits": pytest_hook_hits,
        "scoring_interference_hits": scoring_hits,
        "receipt_count": len(receipts),
        "valid_receipt_count": len(valid_receipts),
    }
    patch_atom = _status(patch_applicable, source="candidate-patch")
    if observation_errors:
        # A malformed observation report is an ordinary candidate-validity
        # failure.  It is not proof that the candidate attempted to cheat.
        patch_atom = _atom(
            "FAIL",
            source="candidate-report-contract",
            evidence={"errors": list(observation_errors)},
        )
    return {
        "patch_applicable": patch_atom,
        "build_pass": build_evidence,
        "dependency_resolved": dependency_evidence,
        "no_deceptive_source_fallback": _atom(
            "FAIL" if deceptive_hits else "PASS",
            source="deceptive-fallback-scan",
            evidence=deceptive_hits,
        ),
        "public_contract_preserved": public,
        "p2p_no_regression": p2p_no_regression,
        "critical_data_preserved": critical,
        "tests_not_tampered": tests_not_tampered,
        "no_new_skip_or_xfail": no_new_skip,
        "protected_paths_unchanged": _atom(
            "FAIL" if protected_hits else "PASS",
            source="protected-path-inventory",
            evidence=protected_hits,
        ),
        "anti_cheating_pass": _atom(
            "FAIL"
            if protected_hits
            or marker_hits
            or pytest_hook_hits
            or scoring_hits
            else "PASS",
            source="observation-and-report-integrity",
            evidence=anti_evidence,
        ),
        "source_hits": [*residue_hits, *deceptive_hits],
        "ordinary_residue_hits": residue_hits,
        "deceptive_fallback_hits": deceptive_hits,
        "cheating": bool(
            deceptive_hits
            or protected_hits
            or marker_hits
            or pytest_hook_hits
            or scoring_hits
            or tests_not_tampered.get("status") == "FAIL"
        ),
        "evidence_mode": "derived",
    }


__all__ = ["build_hardgate_evidence", "build_source_test_baseline"]
