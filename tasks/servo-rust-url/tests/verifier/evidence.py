"""Evidence derivation for the fixed Migration Grader hardgate contract."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import xml.etree.ElementTree as ET
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from .residue import classify_residue_rules, scan_residue_rules
except ImportError:  # Standalone Runtime Draft layout.
    from residue import classify_residue_rules, scan_residue_rules


_TEST_NAME = re.compile(
    r"(?:^|/)(?:test[^/]*|[^/]*(?:\.test|\.spec)\.[^/]*"
    r"|[^/]*_(?:test|spec)\.[^/]+|[^/]*(?:Test|Tests|Spec)\.[^/]+)$",
    re.IGNORECASE,
)
_SKIP_MARKERS = (
    re.compile(r"pytest\.mark\.skip", re.IGNORECASE),
    re.compile(r"pytest\.mark\.xfail", re.IGNORECASE),
    re.compile(r"(?:describe|it|test)\.skip\s*\(", re.IGNORECASE),
    re.compile(r"(?:describe|it|test)\.only\s*\(", re.IGNORECASE),
    re.compile(r"@(?:unittest\.)?skip(?:If|Unless)?\b", re.IGNORECASE),
    re.compile(r"\b(?:skip|xfail|disabled)\s*=\s*true\b", re.IGNORECASE),
)
# Mocha disables a suite/spec with x-prefixed aliases (``xdescribe``,
# ``xcontext``, ``xit``, ``xspecify``).  They are semantically identical to
# ``.skip`` and a migration translates them one-for-one, so they must be counted
# symmetrically; otherwise the mechanical ``xdescribe -> describe.skip`` rewrite
# looks like a newly introduced skip.  Only invocations count: a compatibility
# shim that *defines* ``globals.xit = it.skip`` (or references ``xit``) is not a
# disabled test.
_DISABLED_ALIAS = re.compile(r"\bx(?:describe|context|it|specify)\s*\(", re.IGNORECASE)
# Go disables a test in-band with a ``testing.T`` skip method.  A lexical
# selector regex (``t\.Skip``) only catches one spelling and is trivially
# bypassed by the equivalent Go forms:
#   * selector call      t.Skip(...) / t.Skipf(...) / t.SkipNow()
#   * method expression  (*testing.T).Skip(t, "...")
#   * method value       f := t.Skip; f(...)   (value bound, then invoked)
#   * aggregate value    Holder{fn: t.Skip} / map[...]{"k": t.Skip} /
#                        []func(...any){t.Skip} / ch <- t.Skip / apply(t.Skip) /
#                        return t.Skip     (stored in any container, invoked
#                        indirectly through a field/element/callback)
#   * reflection         reflect.ValueOf(t).MethodByName("Skip").Call(...)
# A method *value* (``recv.Skip`` not immediately followed by ``(``) is itself
# the semantic marker: taking ``testing.T.Skip`` as a value has no purpose other
# than skipping, regardless of *where* it is stored or how it is later invoked.
# We therefore flag the *source* reference with one rule that covers every
# storage family, instead of tracking a sink (which is uncatchable through
# arbitrary aggregates).  Two narrow exemptions keep legitimate code safe:
#   * a direct binding ``x := f.Skip`` (statement-anchored) is counted only when
#     its target is invoked, because it is otherwise indistinguishable from a
#     struct bool field read bound to a variable;
#   * a value used as a boolean condition (``if f.Skip`` / ``!f.Skip`` /
#     ``f.Skip && …``) is a field read, never a ``func`` value.
# The comments-and-strings view keeps prose/data out of the selector match; the
# comment-stripped-but-string-preserving view exposes the reflect method-name
# literal.  Whitespace may separate tokens, so ``t./* adv */Skip(`` still match.
_GO_SKIP_SELECTOR = re.compile(
    r"\.\s*Skip(?:f|Now)?\s*\("        # t.Skip( ... ) / (*testing.T).Skip( ... )
    r"|\)\s*\.\s*Skip(?:f|Now)?\b"     # (*testing.T).Skip  (method expression)
)
_GO_SKIP_REFLECT = re.compile(r"\.\s*MethodByName\s*\(\s*['\"]Skip(?:f|Now)?['\"]\s*\)")
# A skip method *value*: a receiver-selector chain ending in ``Skip/Skipf/SkipNow``
# that is *not* itself called (no trailing ``(``).  The receiver chain requires
# at least one dot so a plain package-level function named ``Skip`` is never a
# method value.  One rule covers every storage family (variable, struct field,
# map/slice/array element, channel, argument, return, interface, nested
# aggregate).
_GO_SKIP_VALUE = re.compile(
    r"(?:[A-Za-z_]\w*\s*\.\s*)+Skip(?:f|Now)?\b(?!\s*\()"
)
# The ambiguous direct-binding form ``[var] IDENT :=/= recv.Skip`` (anchored at a
# statement boundary): counted only when ``IDENT`` is invoked, so a struct bool
# field read bound to a variable (``x := f.Skip``) is not mistaken for a skip
# method value.  Boundary anchoring also keeps ``var i any = x.Skip`` (explicit
# type) out of this rule, so its method value still falls through to
# ``_GO_SKIP_VALUE``.
_GO_SKIP_VALUE_BIND = re.compile(
    r"(?:^|[;{}\n])[ \t]*(?:var[ \t]+)?\b([A-Za-z_]\w*)\s*(?::=|=)\s*"
    r"(?:[A-Za-z_]\w*\s*\.\s*)+Skip(?:f|Now)?\b(?!\s*\()"
)
# Boolean-condition context around a selector (a field read, never a method value).
_GO_BOOL_BEFORE = re.compile(
    r"(?:^|[^\w])(?:if|for|switch|case|!|&&|\|\||==|!=)\s*$"
)
_GO_BOOL_AFTER = re.compile(r"\s*(?:\{|&&|\|\||==|!=)")


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _is_test(path: str) -> bool:
    parts = path.replace("\\", "/").split("/")
    return "test" in {part.casefold() for part in parts[:-1]} or bool(
        _TEST_NAME.search(path)
    )


_EXCLUDED_PARTS = frozenset(
    {".git", ".gomodcache", ".gocache", "__pycache__", ".pytest_cache"}
)


def _regular_files(root: Path) -> dict[str, Path]:
    if not root.is_dir() or root.is_symlink():
        return {}
    values: dict[str, Path] = {}
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if any(part in _EXCLUDED_PARTS for part in Path(relative).parts):
            continue
        values[relative] = path
    return values


def _strip_comments(text: str, *, blank_strings: bool = True) -> str:
    """Blank comment content (and string content unless ``blank_strings``).

    Marker counting must not treat prose in comments as a disabled test, so a
    comment such as ``// do not .Skip()`` is invisible.  String *content* is
    blanked too by default because a data literal must not satisfy a marker; the
    ``blank_strings=False`` view is used only where a method-name string literal
    is itself the evidence (reflection ``MethodByName("Skip")``).  Replacing the
    skipped characters with spaces keeps line numbers and column offsets stable.
    """

    characters = list(text)
    length = len(text)
    state = "normal"
    quote = ""
    index = 0
    while index < length:
        character = text[index]
        if state == "normal":
            if character == "/" and index + 1 < length and text[index + 1] == "/":
                while index < length and text[index] != "\n":
                    characters[index] = " "
                    index += 1
                continue
            if character == "#":
                while index < length and text[index] != "\n":
                    characters[index] = " "
                    index += 1
                continue
            if character == "/" and index + 1 < length and text[index + 1] == "*":
                characters[index] = " "
                characters[index + 1] = " "
                index += 2
                while index < length and not (
                    text[index] == "*"
                    and index + 1 < length
                    and text[index + 1] == "/"
                ):
                    if text[index] != "\n":
                        characters[index] = " "
                    index += 1
                if index < length:
                    characters[index] = " "
                    index += 1
                if index < length:
                    characters[index] = " "
                    index += 1
                continue
            if character in ("'", '"', "`"):
                state = "string"
                quote = character
                index += 1
                continue
            index += 1
            continue
        if character == "\\":
            index += 1
            if index < length:
                if text[index] != "\n" and blank_strings:
                    characters[index] = " "
                index += 1
            continue
        if character == quote:
            state = "normal"
            index += 1
            continue
        if character != "\n" and blank_strings:
            characters[index] = " "
        index += 1
    return "".join(characters)


def _go_skip_value_count(code: str) -> int:
    """Count method-value references to a ``testing.T`` skip method.

    A method value (``recv.Skip`` not followed by ``(``) is a skip marker no
    matter where it is stored, so a single source-level rule covers every
    storage family.  The only exceptions are the ambiguous direct binding whose
    target is never invoked and a value used as a boolean condition (a field
    read).
    """

    bound: list[tuple[int, int]] = []
    count = 0
    for match in _GO_SKIP_VALUE_BIND.finditer(code):
        bound.append(match.span())
        name = match.group(1)
        if re.search(r"\b" + re.escape(name) + r"\s*\(", code):
            count += 1
    for match in _GO_SKIP_VALUE.finditer(code):
        start, end = match.span()
        if any(left <= start and end <= right for left, right in bound):
            continue
        if _GO_BOOL_BEFORE.search(code[:start]):
            continue
        if _GO_BOOL_AFTER.match(code[end:]):
            continue
        count += 1
    return count


@lru_cache(maxsize=1)
def _go_skip_scanner():
    scanner_path = Path(__file__).resolve().parents[1] / "assets/shared/integration/go_skips.py"
    spec = importlib.util.spec_from_file_location("migration_go_skips", scanner_path)
    if spec is None or spec.loader is None:
        raise ValueError("Go skip scanner is missing")
    scanner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scanner)
    return scanner


def _go_skip_count(text: str, path: Path | None = None) -> int:
    """Count Go ``testing.T`` skip methods across their equivalent spellings."""

    text = _go_skip_scanner().scannable_source(text, path)
    code = _strip_comments(text)
    count = len(_GO_SKIP_SELECTOR.findall(code))
    if _GO_SKIP_REFLECT.search(_strip_comments(text, blank_strings=False)):
        count += 1
    return count + _go_skip_value_count(code)


def _marker_counts(root: Path) -> dict[str, int]:
    counts = {"skip": 0, "xfail": 0, "disabled": 0}
    for path in _regular_files(root).values():
        try:
            raw = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        text = _strip_comments(raw)
        counts["skip"] += sum(
            1 for pattern in _SKIP_MARKERS[:1] for _ in pattern.finditer(text)
        )
        counts["skip"] += _go_skip_count(raw, path) if path.suffix == ".go" else _go_skip_count(raw)
        counts["xfail"] += sum(
            1 for pattern in (_SKIP_MARKERS[1],) for _ in pattern.finditer(text)
        )
        counts["disabled"] += sum(
            1
            # A Go boolean named skip/disabled is ordinary program state.
            # Actual testing.T skips are covered by the Go scanner and the
            # runtime JUnit sink, including indirect method calls.
            for pattern in (() if path.suffix == ".go" else (*_SKIP_MARKERS[2:], _DISABLED_ALIAS))
            for _ in pattern.finditer(text)
        )
    return counts


def build_source_test_baseline(root: Path) -> dict[str, object]:
    """Freeze source-visible test identities and marker counts for a Case."""

    files = {
        relative: {"sha256": _digest(path)}
        for relative, path in sorted(_regular_files(root).items())
        if _is_test(relative)
    }
    return {
        "schema_version": "migration-source-test-baseline:v1",
        "files": files,
        "marker_counts": _marker_counts(root),
    }


def _atom(
    status: str,
    *,
    source: str,
    evidence: object = None,
) -> dict[str, object]:
    return {
        "status": status,
        "source": source,
        "evidence": [] if evidence is None else evidence,
    }


def _status(value: object, *, source: str, default: str = "INCONCLUSIVE") -> dict[str, object]:
    if isinstance(value, Mapping) and isinstance(value.get("status"), str):
        return {
            "status": str(value["status"]),
            "source": str(value.get("source", source)),
            "evidence": value.get("evidence", []),
        }
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
        "target-diagnostic",
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
    elif any(row.get("returncode") not in {0, None} for row in executions):
        build = _atom("FAIL", source="target-execution-receipt", evidence=evidence)
    elif report_present and report_valid:
        build = _atom("PASS", source="target-execution-receipt", evidence=evidence)
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
    elif any(row.get("returncode") not in {0, None} for row in executions):
        dependencies = _atom(
            "FAIL", source="target-execution-receipt", evidence=evidence
        )
    elif report_present and report_valid:
        dependencies = _atom(
            "PASS", source="target-execution-receipt", evidence=evidence
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
    if expected == 0 or (expected is None and not rows):
        return _atom(empty, source=source, evidence={"total": 0, "checks": []})
    if not rows:
        return _atom("INCONCLUSIVE", source=source, evidence={"total": 0, "checks": []})
    statuses = [str(row.get("status", "INCONCLUSIVE")) for row in rows]
    status = (
        "INFRA_ERROR" if any(item in {"INFRA_ERROR", "TIMEOUT"} for item in statuses)
        else "INCONCLUSIVE" if any(item in {"INCONCLUSIVE", "NA"} for item in statuses)
        else "FAIL" if any(item == "FAIL" for item in statuses)
        else "PASS"
    )
    return _atom(
        status,
        source=source,
        evidence={
            "passed": sum(item == "PASS" for item in statuses),
            "total": len(statuses),
            "checks": [dict(row) for row in rows],
        },
    )


def _protected_hits(root: Path, protected: Sequence[str]) -> list[str]:
    hits: list[str] = []
    for relative in _regular_files(root):
        normalized = relative.replace("\\", "/")
        for raw in protected:
            path = str(raw).replace("\\", "/").strip("/")
            if path and (normalized == path or normalized.startswith(path + "/")):
                hits.append(normalized)
                break
    for raw in protected:
        path = root / Path(str(raw).replace("\\", "/"))
        if path.exists() and str(raw).replace("\\", "/") not in hits:
            hits.append(str(raw).replace("\\", "/").strip("/"))
    return sorted(set(hits))


def _report_marker_hits(root: Path, markers: Sequence[str]) -> list[dict[str, str]]:
    hits: list[dict[str, str]] = []
    if not markers:
        return hits
    for relative, path in _regular_files(root).items():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for marker in markers:
            if str(marker) and str(marker).casefold() in text.casefold():
                hits.append({"path": relative, "marker": str(marker)})
    return hits


_RUNTIME_SKIP_SOURCE = "native-junit-runtime"


def _junit_skipped_tests(root: ET.Element) -> list[str]:
    """Return identifiers of ``<testcase>`` elements the runner skipped.

    A skipped case is detected directly from its own subtree (a ``<skipped/>``
    child or a ``skipped`` attribute), never inferred from a generic
    pass/fail/absent classification.  The raw ``file``/``classname`` plus
    ``name`` is reported so the evidence names what ran away.
    """

    skipped: list[str] = []
    for case in root.iter():
        if case.tag.rsplit("}", 1)[-1].casefold() != "testcase":
            continue
        has_skipped_child = any(
            child.tag.rsplit("}", 1)[-1].casefold() == "skipped"
            for child in case
        )
        if not has_skipped_child and case.get("skipped") is None:
            continue
        file = str(case.get("file") or case.get("classname") or "").strip()
        title = str(case.get("name") or "").strip()
        skipped.append(f"{file}#{title}" if file else (title or "testcase"))
    return skipped


def runtime_skip_evidence_from_junit(
    report_present: bool,
    text: str | None,
    *,
    report_label: str = "native-report",
) -> dict[str, object]:
    """Derive a runtime skip atom from a freshly written native JUnit report.

    This is the *runtime sink* for the skip family: a candidate that suppresses
    a test through a spelling the static marker scan cannot see (for example a
    reflectively assembled method name) still emits a ``<skipped>`` case, which
    this parser observes directly.  A missing, malformed or non-JUnit report is
    ``INCONCLUSIVE`` and never ``PASS``: an unreadable report cannot be evidence
    that nothing was skipped.
    """

    if not report_present or text is None:
        return _atom(
            "INCONCLUSIVE",
            source=_RUNTIME_SKIP_SOURCE,
            evidence={
                "report": report_label,
                "report_present": False,
                "reason": "report-missing",
            },
        )
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        return _atom(
            "INCONCLUSIVE",
            source=_RUNTIME_SKIP_SOURCE,
            evidence={
                "report": report_label,
                "report_present": True,
                "reason": "report-unparseable",
                "parse_error": f"{type(exc).__name__}: {exc}",
            },
        )
    if root.tag.rsplit("}", 1)[-1].casefold() not in {"testsuites", "testsuite"}:
        return _atom(
            "INCONCLUSIVE",
            source=_RUNTIME_SKIP_SOURCE,
            evidence={
                "report": report_label,
                "report_present": True,
                "reason": "report-not-junit",
            },
        )
    skipped = _junit_skipped_tests(root)
    return _atom(
        "FAIL" if skipped else "PASS",
        source=_RUNTIME_SKIP_SOURCE,
        evidence={
            "report": report_label,
            "report_present": True,
            "skipped_count": len(skipped),
            "skipped_tests": skipped[:50],
        },
    )


def _combine_skip_evidence(
    static_skip: Mapping[str, object],
    runtime_skip: Mapping[str, object] | None,
) -> dict[str, object]:
    """Merge the static marker delta with the runtime JUnit skip evidence.

    ``runtime_skip is None`` preserves the historical static-only contract for
    callers that do not run the native report sink.  A ``FAIL`` from either side
    wins; otherwise an ``INCONCLUSIVE`` from either side wins over ``PASS``, so a
    missing runtime report can never be laundered into a passing skip atom.
    """

    if runtime_skip is None:
        return dict(static_skip)
    static_status = str(static_skip.get("status", "INCONCLUSIVE"))
    runtime_status = str(runtime_skip.get("status", "INCONCLUSIVE"))
    if "FAIL" in (static_status, runtime_status):
        status = "FAIL"
    elif "INCONCLUSIVE" in (static_status, runtime_status):
        status = "INCONCLUSIVE"
    else:
        status = "PASS"
    return _atom(
        status,
        source=str(static_skip.get("source") or "source-test-baseline"),
        evidence={
            "static": {"status": static_status, "evidence": static_skip.get("evidence")},
            "runtime": {
                "status": runtime_status,
                "source": runtime_skip.get("source"),
                "evidence": runtime_skip.get("evidence"),
            },
        },
    )


def _test_integrity(
    root: Path,
    baseline: Mapping[str, Any] | None,
    config: Mapping[str, Any],
    *,
    source: str = "source-test-baseline",
    marker_baseline: Mapping[str, Any] | None = None,
    runtime_skip: Mapping[str, Any] | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    # ``baseline`` is the *source* test baseline: it defines the tests the
    # candidate must preserve (existence + content).  ``marker_baseline`` is the
    # *target* test baseline (source baseline when omitted) and only anchors the
    # skip/xfail/disabled increment, because the migration may legitimately add
    # tests the reference introduced.  Treating target-only files as required
    # would mis-flag a valid migration that does not reproduce the reference's
    # newly added tests as ``tests_not_tampered`` (missing/tamper).
    if not isinstance(baseline, Mapping):
        inconclusive = _atom("INCONCLUSIVE", source=source)
        return inconclusive, inconclusive
    baseline_files = baseline.get("files", {})
    baseline_files = baseline_files if isinstance(baseline_files, Mapping) else {}
    if not baseline_files:
        # A pass here would be vacuous: there is no test file to protect.  Keep
        # the evidence honest without turning a repository with no target tests
        # into a hard failure.
        empty = _atom(
            "NA",
            source=source,
            evidence={"reason": "baseline-has-no-test-files", "files": 0},
        )
        return empty, empty
    current = _regular_files(root)
    missing = sorted(
        str(relative) for relative in baseline_files
        if str(relative) not in current
    )
    allowed = {
        str(item).replace("\\", "/").strip("/")
        for item in config.get("policy", {}).get("allowed_equivalent_test_paths", [])
    } if isinstance(config.get("policy"), Mapping) else set()
    changed = sorted(
        relative for relative, record in baseline_files.items()
        if relative in current
        and isinstance(record, Mapping)
        and record.get("sha256") != _digest(current[relative])
        and relative not in allowed
    )
    marker_source = marker_baseline if isinstance(marker_baseline, Mapping) else baseline
    marker_before = marker_source.get("marker_counts", {})
    marker_before = marker_before if isinstance(marker_before, Mapping) else {}
    marker_after = _marker_counts(root)
    marker_delta = {
        key: int(marker_after.get(key, 0)) - int(marker_before.get(key, 0))
        for key in {"skip", "xfail", "disabled"}
    }
    tamper = _atom(
        "FAIL" if missing or changed else "PASS",
        source=source,
        evidence={"missing": missing, "changed": changed},
    )
    static_skip = _atom(
        "FAIL" if any(value > 0 for value in marker_delta.values()) else "PASS",
        source=source,
        evidence={"before": dict(marker_before), "after": marker_after, "delta": marker_delta},
    )
    return tamper, _combine_skip_evidence(static_skip, runtime_skip)


def build_hardgate_evidence(
    candidate_root: Path,
    *,
    config: Mapping[str, Any],
    observations: Sequence[Mapping[str, Any]],
    observation_errors: Sequence[str],
    receipts: Sequence[Mapping[str, Any]],
    patch_applicable: bool | None,
    source_test_baseline: Mapping[str, Any] | None,
    target_test_baseline: Mapping[str, Any] | None = None,
    test_baseline_source: str = "source-test-baseline",
    runtime_skip_evidence: Mapping[str, Any] | None = None,
) -> dict[str, object]:
    """Derive hardgate atoms from Case evidence instead of policy booleans.

    ``runtime_skip_evidence`` is the optional native-report atom produced by the
    public Harbor entry point.  Its default of ``None`` keeps every other caller
    on the historical static-only skip contract.
    """

    policy = config.get("policy", {})
    policy = policy if isinstance(policy, Mapping) else {}
    rows = _rows(observations)
    expected_tests = config.get("tests", {})
    expected_tests = expected_tests if isinstance(expected_tests, Mapping) else {}
    p2p = [row for row in rows if row.get("kind") == "P2P"]
    state = [row for row in rows if row.get("kind") == "STATE"]
    semantic_rules = policy.get("semantic_rules", [])
    residue_rules = policy.get("ordinary_residue_rules", policy.get("residue_rules", semantic_rules))
    residue_diagnostics = classify_residue_rules(candidate_root, residue_rules)
    residue_hits = scan_residue_rules(candidate_root, residue_rules)
    deceptive_rules = policy.get("deceptive_fallback_rules", [])
    exemption = policy.get("deceptive_fallback_not_applicable")
    migration = config.get("migration", {})
    category = migration.get("category") if isinstance(migration, Mapping) else None
    missing_l2 = (
        category != "version_upgrade"
        and not deceptive_rules
        and not (
            isinstance(exemption, Mapping)
            and str(exemption.get("reason", "")).strip()
            and str(exemption.get("evidence", "")).strip()
        )
    )
    deceptive_diagnostics = classify_residue_rules(
        candidate_root, deceptive_rules, go_mechanism=True
    )
    deceptive_hits = scan_residue_rules(
        candidate_root, deceptive_rules, go_mechanism=True
    )
    protected = policy.get("protected_paths", config.get("protected_paths", []))
    protected = protected if isinstance(protected, Sequence) and not isinstance(protected, (str, bytes)) else []
    marker_names = policy.get("forbidden_report_markers", [])
    marker_names = marker_names if isinstance(marker_names, Sequence) and not isinstance(marker_names, (str, bytes)) else []
    protected_hits = _protected_hits(candidate_root, [str(item) for item in protected])
    marker_hits = _report_marker_hits(candidate_root, [str(item) for item in marker_names])
    tests_not_tampered, no_new_skip = _test_integrity(
        candidate_root,
        source_test_baseline,
        config,
        source=test_baseline_source,
        marker_baseline=target_test_baseline,
        runtime_skip=runtime_skip_evidence,
    )
    build_evidence, dependency_evidence, valid_receipts = _derive_execution_atoms(
        receipts
    )
    public = _group_status(
        p2p,
        expected=len(expected_tests.get("p2p", [])) if isinstance(expected_tests.get("p2p", []), Sequence) else None,
        source="candidate-observations",
        empty="NA",
    )
    differential = [row for row in rows if row.get("kind") == "DIFFERENTIAL"]
    expected_p2p = expected_tests.get("p2p", [])
    expected_diff = expected_tests.get("differential", [])
    expected_p2p_count = (
        len(expected_p2p) if isinstance(expected_p2p, Sequence) and not isinstance(expected_p2p, (str, bytes)) else 0
    )
    expected_diff_count = (
        len(expected_diff) if isinstance(expected_diff, Sequence) and not isinstance(expected_diff, (str, bytes)) else 0
    )
    expected_regression = expected_p2p_count + expected_diff_count
    p2p_regression = _group_status(
        [*p2p, *differential],
        expected=expected_regression if expected_regression else None,
        source="candidate-regression-observations",
        empty="NA",
    )
    state_applicable = bool(policy.get("state_applicable"))
    expected_state = expected_tests.get("state", [])
    expected_state = expected_state if isinstance(expected_state, list) else []
    state_roles = {
        str(item.get("id")): str(item.get("state_role", ""))
        for item in expected_state if isinstance(item, Mapping)
    }
    invalid_state_roles = state_applicable and (
        not state_roles
        or any(role not in {"critical_data", "required_state"} for role in state_roles.values())
    )
    critical_rows = [row for row in state if state_roles.get(str(row.get("id"))) == "critical_data"]
    expected_critical = sum(role == "critical_data" for role in state_roles.values())
    critical = (
        _atom("INCONCLUSIVE", source="state-role-validation", evidence={"missing_or_invalid_roles": sorted(state_roles)})
        if invalid_state_roles
        else _group_status(
            critical_rows,
            expected=expected_critical,
            source="candidate-critical-data-observations",
            empty="NA" if not state_applicable else "INCONCLUSIVE",
        )
    )
    anti_evidence = {
        "observation_errors": list(observation_errors),
        "protected_hits": protected_hits,
        "report_marker_hits": marker_hits,
        "receipt_count": len(receipts),
        "valid_receipt_count": len(valid_receipts),
    }
    cheating_errors = [
        error for error in observation_errors
        if any(
            marker in str(error).casefold()
            for marker in ("receipt", "duplicate", "unexpected", "forged")
        )
    ]
    return {
        "patch_applicable": _status(patch_applicable, source="candidate-patch"),
        "build_pass": build_evidence,
        "dependency_resolved": dependency_evidence,
        "no_deceptive_source_fallback": _atom(
            "FAIL" if deceptive_hits else "INCONCLUSIVE" if missing_l2 else "NA" if not deceptive_rules else "PASS",
            source="deceptive-fallback-scan",
            evidence=deceptive_hits,
        ),
        "public_contract_preserved": public,
        "p2p_regression_preserved": p2p_regression,
        "critical_data_preserved": critical,
        "tests_not_tampered": tests_not_tampered,
        "no_new_skip_or_xfail": no_new_skip,
        "protected_paths_unchanged": _atom(
            "FAIL" if protected_hits else "PASS",
            source="protected-path-inventory",
            evidence=protected_hits,
        ),
        "anti_cheating_pass": _atom(
            "FAIL" if cheating_errors or protected_hits or marker_hits else "PASS",
            source="observation-and-report-integrity",
            evidence=anti_evidence,
        ),
        "source_hits": [*residue_hits, *deceptive_hits],
        "ordinary_residue_hits": residue_hits,
        "deceptive_fallback_hits": deceptive_hits,
        "residue_diagnostics": residue_diagnostics,
        "deceptive_residue_diagnostics": deceptive_diagnostics,
        "cheating": bool(
            deceptive_hits
            or cheating_errors
            or protected_hits
            or marker_hits
        ),
        "evidence_mode": "derived",
    }


def _selfcheck() -> None:
    """Assert the runtime JUnit skip contract with synthetic fixtures.

    Covers the four cases the runtime sink must distinguish — a real skip, a
    clean report, a malformed report and a missing report — plus the static
    dominance rule, so the reflect-clean bypass cannot silently regress.
    """

    skipped_child = (
        '<testsuites><testsuite name="url">'
        '<testcase classname="url" name="TestSkip"><skipped message="r8"/></testcase>'
        "</testsuite></testsuites>"
    )
    skipped_attr = (
        '<testsuite name="url">'
        '<testcase classname="url" name="TestSkip" skipped="true"/>'
        "</testsuite>"
    )
    clean = (
        '<testsuites><testsuite name="url">'
        '<testcase classname="url" name="TestOK"/>'
        "</testsuite></testsuites>"
    )
    malformed = "<testsuites><testsuite><testcase"
    not_junit = "<html><body>not a report</body></html>"

    missing = runtime_skip_evidence_from_junit(False, None)
    assert missing["status"] == "INCONCLUSIVE", missing
    unparseable = runtime_skip_evidence_from_junit(True, malformed)
    assert unparseable["status"] == "INCONCLUSIVE", unparseable
    assert unparseable["evidence"]["reason"] == "report-unparseable", unparseable
    wrong_root = runtime_skip_evidence_from_junit(True, not_junit)
    assert wrong_root["status"] == "INCONCLUSIVE", wrong_root

    clean_atom = runtime_skip_evidence_from_junit(True, clean)
    assert clean_atom["status"] == "PASS", clean_atom
    assert clean_atom["evidence"]["skipped_count"] == 0, clean_atom

    for text in (skipped_child, skipped_attr):
        hit = runtime_skip_evidence_from_junit(True, text)
        assert hit["status"] == "FAIL", hit
        assert hit["evidence"]["skipped_count"] == 1, hit
        assert "TestSkip" in hit["evidence"]["skipped_tests"][0], hit

    static_pass = _atom("PASS", source="source-test-baseline", evidence={"delta": {}})
    static_fail = _atom(
        "FAIL", source="source-test-baseline", evidence={"delta": {"skip": 1}}
    )
    # Default ``None`` preserves the static-only contract byte-for-byte.
    assert _combine_skip_evidence(static_pass, None) == static_pass
    assert _combine_skip_evidence(static_fail, None) == static_fail
    # A runtime skip (or an unreadable report) is never laundered into PASS.
    assert _combine_skip_evidence(static_pass, clean_atom)["status"] == "PASS"
    assert _combine_skip_evidence(static_pass, runtime_skip_evidence_from_junit(True, skipped_child))["status"] == "FAIL"
    assert _combine_skip_evidence(static_pass, missing)["status"] == "INCONCLUSIVE"
    # A static marker delta FAIL dominates an inconclusive runtime report.
    assert _combine_skip_evidence(static_fail, missing)["status"] == "FAIL"


if __name__ == "__main__":  # pragma: no cover - manual selfcheck entry point
    _selfcheck()
    print("evidence selfcheck: PASS")


__all__ = [
    "build_hardgate_evidence",
    "build_source_test_baseline",
    "runtime_skip_evidence_from_junit",
]
