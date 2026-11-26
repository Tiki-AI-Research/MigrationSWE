"""Evidence derivation for the fixed Migration Grader hardgate contract."""

from __future__ import annotations

import ast
import io
import tokenize
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from .detectors import (
        detect_source_runtime_dependency,
        detect_source_runtime_dynamic_load,
        detect_source_runtime_ffi,
        detect_source_runtime_subprocess,
    )
    from .residue import classify_residue_rules, scan_residue_rules
except ImportError:  # Standalone Runtime Draft layout.
    from detectors import (
        detect_source_runtime_dependency,
        detect_source_runtime_dynamic_load,
        detect_source_runtime_ffi,
        detect_source_runtime_subprocess,
    )
    from residue import classify_residue_rules, scan_residue_rules


_TEST_NAME = re.compile(
    r"(?:^|/)(?:test_[^/]+\.py|conftest\.py"
    r"|[^/]*(?:\.test|\.spec)\.[^/]+"
    r"|[^/]*_(?:test|spec)\.[^/]+"
    r"|[^/]*(?:Test|Tests|Spec)\.[^/]+)$",
    re.IGNORECASE,
)
_TEST_DIRS = frozenset({"test", "tests", "__tests__", "spec", "specs", "e2e"})
_NON_TEST_DIRS = frozenset({"docs", "doc", "assets", "examples", ".github"})
_NON_TEST_SUFFIXES = frozenset(
    {
        ".md", ".markdown", ".txt", ".rst",
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico",
        ".yml", ".yaml", ".lock",
    }
)
_SKIP_MARKERS = (
    re.compile(r"pytest\.mark\.skip(?:if)?", re.IGNORECASE),
    re.compile(r"pytest\.mark\.xfail", re.IGNORECASE),
    re.compile(r"(?:describe|it|test)\.skip\s*\(", re.IGNORECASE),
    re.compile(r"(?:describe|it|test)\.only\s*\(", re.IGNORECASE),
    re.compile(r"@(?:unittest\.)?skip(?:If|Unless)?\b", re.IGNORECASE),
    re.compile(r"\b(?:skip|xfail|disabled)\s*=\s*true\b", re.IGNORECASE),
    # Standard-library skip/xfail APIs that a plain decorator marker misses.
    re.compile(r"\bself\.skipTest\s*\(", re.IGNORECASE),
    re.compile(r"\braise\s+(?:unittest\.)?(?:case\.)?SkipTest\b", re.IGNORECASE),
    re.compile(r"\bpytest\.skip\s*\(", re.IGNORECASE),
    re.compile(r"@(?:unittest\.)?(?:case\.)?expectedFailure\b", re.IGNORECASE),
)
# A test-bootstrap file that neutralizes unittest's result recording hides every
# failing assertion from the native test command.  It is not a skip marker (it
# has no skip/xfail delta) so it is detected separately as tampering.
_TEST_BOOTSTRAP_TAMPER = (
    re.compile(
        r"\bTestResult\s*\.\s*add(?:Failure|Error|UnexpectedSuccess|ExpectedFailure)\s*=",
        re.IGNORECASE,
    ),
    re.compile(r"\bTestResult\s*\.\s*stop\s*=", re.IGNORECASE),
    re.compile(r"\bTestCase\s*\.\s*(?:run|fail|failIf)\s*=", re.IGNORECASE),
    re.compile(
        r"\bsetattr\s*\(\s*(?:unittest\.)?TestResult\s*,",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:unittest\.)?(?:TestResult|TestSuite)\s*\.\s*(?:addFailure|addError)\b\s*=",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bmonkeypatch(?:\.setattr)?\s*\([^)]*TestResult",
        re.IGNORECASE,
    ),
    # Exit-code forgery: a test bootstrap that forces the process to exit 0
    # (via ``atexit``/``os._exit``) or replaces the test runner or
    # already-imported modules hides failing assertions from the native command.
    # ``sys.exit`` is handled separately because a standard
    # ``if __name__ == "__main__": ... sys.exit(...)`` test-runner guard is inert
    # under ``unittest discover`` and must not be treated as tampering.
    re.compile(r"\batexit\s*\.\s*register\b", re.IGNORECASE),
    re.compile(r"\bos\._exit\s*\(", re.IGNORECASE),
    re.compile(r"\bTextTestRunner\b", re.IGNORECASE),
    re.compile(r"\bsys\.modules\s*\[", re.IGNORECASE),
)
# A bare ``sys.exit`` only forges the process exit code when it runs during
# import (module top level) or in code reachable outside the conventional
# ``__main__`` runner guard.  Occurrences inside that guard are stripped before
# this pattern is applied.
_SYS_EXIT_FORGERY = re.compile(r"\bsys\.exit\s*\(", re.IGNORECASE)
_MAIN_GUARD = re.compile(
    r"^([ \t]*)if\s+__name__\s*==\s*['\"]__main__['\"]\s*:", re.MULTILINE
)
# Mocha disables a suite/spec with x-prefixed aliases (``xdescribe``,
# ``xcontext``, ``xit``, ``xspecify``).  They are semantically identical to
# ``.skip`` and a migration translates them one-for-one, so they must be counted
# symmetrically; otherwise the mechanical ``xdescribe -> describe.skip`` rewrite
# looks like a newly introduced skip.  Only invocations count: a compatibility
# shim that *defines* ``globals.xit = it.skip`` (or references ``xit``) is not a
# disabled test.
_DISABLED_ALIAS = re.compile(r"\bx(?:describe|context|it|specify)\s*\(", re.IGNORECASE)


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _is_test(path: str) -> bool:
    parts = path.replace("\\", "/").split("/")
    directories = {part.casefold() for part in parts[:-1]}
    if directories & _NON_TEST_DIRS:
        return False
    if Path(path).suffix.casefold() in _NON_TEST_SUFFIXES:
        return False
    return bool(directories & _TEST_DIRS) or bool(_TEST_NAME.search(path))


def _strip_comments_keep_strings(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"(?m)//.*$", " ", text)
    text = re.sub(r"(?m)#(?!\[).*$", " ", text)
    return text


def _normalized_text(text: str) -> str:
    text = _strip_comments_keep_strings(text)
    return re.sub(r"\s+", " ", text).strip()


def _normalized_digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(_normalized_text(text).encode("utf-8")).hexdigest()


_ASSERTION_TOKENS = re.compile(
    r"\b(?:"
    r"assert(?:_eq|_ne|_matches|_contains|_that)?!?"
    r"|assertEqual|assertNotEqual|assertAlmostEqual|assertGreater|assertGreaterEqual"
    r"|assertLess|assertLessEqual|assertTrue|assertFalse|assertIs|assertIsNot"
    r"|assertIsNone|assertIsNotNone|assertIn|assertNotIn|assertRaises|assertRaisesRegex"
    r"|assertWarns|assertWarnsRegex|assertLogs|assertRegex|assertNotRegex"
    r"|assertDictEqual|assertListEqual|assertTupleEqual|assertSetEqual"
    r"|assertSequenceEqual|assertMultiLineEqual|assertCountEqual|assertItemsEqual"
    r"|assertNull|assertNotNull|assertThat|assertEquals|assertNotEquals"
    r"|expect|should|require|t\.(?:Error|Fatal|Fatalf|Errorf)"
    r")\b",
    re.IGNORECASE,
)


def _assertion_signature(text: str) -> str:
    text = _strip_comments_keep_strings(text)
    rows: list[str] = []
    for line in text.splitlines():
        if not _ASSERTION_TOKENS.search(line):
            continue
        normalized = _ASSERTION_TOKENS.sub("ASSERT", line)
        normalized = re.sub(r"[\s'\"!]+", "", normalized)
        if normalized:
            rows.append(normalized)
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest() if rows else ""


def _strip_main_guard(text: str) -> str:
    """Drop ``if __name__ == "__main__":`` blocks (indentation aware).

    The block body is inert under ``python -m unittest discover`` (where
    ``__name__`` is the module name, not ``__main__``), so a standard test runner
    guard such as ``unittest.main(); sys.exit(...)`` cannot hide assertions.
    """

    lines = text.split("\n")
    kept: list[str] = []
    index = 0
    while index < len(lines):
        match = _MAIN_GUARD.match(lines[index])
        if match is None:
            kept.append(lines[index])
            index += 1
            continue
        indent = len(match.group(1))
        index += 1
        while index < len(lines):
            line = lines[index]
            if not line.strip():
                index += 1
                continue
            current = len(line) - len(line.lstrip())
            if current <= indent:
                break
            index += 1
    return "\n".join(kept)



def _python_code(text: str) -> str:
    """Keep executable syntax while blanking comments and all string literals."""
    lines = text.splitlines(keepends=True)
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, IndentationError):
        return text  # malformed candidate tests fail closed
    for token in tokens:
        if token.type not in {tokenize.STRING, tokenize.COMMENT}:
            continue
        (start, column), (end, end_column) = token.start, token.end
        for row in range(start - 1, end):
            left = column if row == start - 1 else 0
            right = end_column if row == end - 1 else len(lines[row].rstrip("\r\n"))
            lines[row] = lines[row][:left] + " " * (right - left) + lines[row][right:]
    return "".join(lines)


def _capability_bindings(root: Path) -> tuple[set[str], set[str]]:
    symlink_probes: set[str] = set()
    root_flags: set[str] = set()
    for relative, path in _regular_files(root).items():
        if path.suffix != ".py" or not _is_test(relative):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, SyntaxError):
            continue
        for node in tree.body:
            if isinstance(node, ast.Assign):
                expression = ast.unparse(node.value)
                if expression in {"os.geteuid() == 0", "os.name == 'posix' and os.geteuid() == 0", "hasattr(os, 'geteuid') and os.geteuid() == 0"}:
                    root_flags.update(t.id for t in node.targets if isinstance(t, ast.Name))
            if not isinstance(node, ast.FunctionDef):
                continue
            body = [item for item in node.body if not (isinstance(item, ast.Expr) and isinstance(item.value, ast.Constant))]
            if len(body) != 2 or not isinstance(body[0], ast.With) or not isinstance(body[1], ast.Return) or not isinstance(body[1].value, ast.Constant) or body[1].value.value is not True:
                continue
            scope = body[0]
            if len(scope.items) != 1 or not isinstance(scope.items[0].context_expr, ast.Call) or ast.unparse(scope.items[0].context_expr.func) != "tempfile.TemporaryDirectory" or len(scope.body) != 1 or not isinstance(scope.body[0], ast.Try):
                continue
            attempt = scope.body[0]
            if len(attempt.body) != 1 or not isinstance(attempt.body[0], ast.Expr) or not isinstance(attempt.body[0].value, ast.Call) or ast.unparse(attempt.body[0].value.func) != "os.symlink" or attempt.orelse or attempt.finalbody or len(attempt.handlers) != 1:
                continue
            handler = attempt.handlers[0]
            if handler.type is not None and ast.unparse(handler.type) in {"OSError", "(OSError, NotImplementedError)"} and len(handler.body) == 1 and isinstance(handler.body[0], ast.Return) and isinstance(handler.body[0].value, ast.Constant) and handler.body[0].value.value is False:
                symlink_probes.add(node.name)
    return symlink_probes, root_flags


def _without_capability_guards(text: str, bindings: tuple[set[str], set[str]]) -> str:
    """Exclude platform-only guards on additional tests, not unconditional skips.

    Required file-safety behavior is independently exercised by public probes.
    Capability guards cannot make those verifier-owned probes disappear.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return text
    spans = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call):
                    continue
                name = ast.unparse(dec.func).split(".")[-1]
                condition = ast.unparse(dec.args[0]) if dec.args else ""
                platform_guards = {
                    ("skipUnless", "os.name == 'posix'"),
                    ("skipIf", "os.name != 'posix'"),
                }
                symlink_probes, root_flags = bindings
                platform_guards.update(("skipUnless", name + "()") for name in symlink_probes)
                platform_guards.update(("skipIf", name + " or os.name != 'posix'") for name in root_flags)
                if (name, condition) in platform_guards:
                    spans.append((dec.lineno, dec.end_lineno))
        if isinstance(node, ast.Try):
            calls = [ast.unparse(call.func) for stmt in node.body for call in ast.walk(stmt) if isinstance(call, ast.Call)]
            # A failed filesystem capability probe may guard an additional
            # test. Ordinary open() is accepted only for the non-UTF-8 filename
            # probe; a broad exception around arbitrary assertions is not.
            filename_probe = any(
                isinstance(call, ast.Call) and ast.unparse(call.func) == "open"
                and call.args and "os.fsencode(" in ast.unparse(call.args[0])
                for stmt in node.body for call in ast.walk(stmt)
            )
            if not any(call in {"os.symlink", "os.open"} for call in calls) and not filename_probe:
                continue
            for handler in node.handlers:
                kind = ast.unparse(handler.type) if handler.type is not None else ""
                if kind not in {"OSError", "(OSError, NotImplementedError)"}:
                    continue
                for call in ast.walk(handler):
                    if isinstance(call, ast.Call) and ast.unparse(call.func) == "self.skipTest":
                        spans.append((call.lineno, call.end_lineno))
    lines = text.splitlines(keepends=True)
    for start, end in spans:
        for i in range(start - 1, end):
            lines[i] = "\n"
    return "".join(lines)

def _bootstrap_tamper_hits(root: Path) -> list[str]:
    hits: list[str] = []
    for relative, path in _regular_files(root).items():
        if not _is_test(relative):
            continue
        try:
            raw = path.read_text(encoding="utf-8", errors="ignore")
            text = _python_code(raw) if path.suffix == ".py" else _strip_comments_keep_strings(raw)
            if path.suffix == ".py":
                # Evicting cached candidate modules makes WORKSPACE_ROOT test
                # fixtures independent; it does not replace modules or results.
                text = re.sub(r"(?m)^[ \t]*del[ \t]+sys\.modules\[[^\n]+\][ \t]*$", "", text)
        except OSError:
            continue
        if any(pattern.search(text) for pattern in _TEST_BOOTSTRAP_TAMPER) or _SYS_EXIT_FORGERY.search(
            _strip_main_guard(text)
        ):
            hits.append(relative)
    return sorted(hits)


_EXCLUDED_PARTS = frozenset(
    {
        ".git",
        "node_modules",
        ".gomodcache",
        ".gocache",
        "__pycache__",
        ".pytest_cache",
        ".venv",
        "venv",
        "site-packages",
        ".harbor-result.json",
    }
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


def _strip_comments(text: str) -> str:
    """Blank comment and string content while preserving offsets.

    Marker counting must not treat prose in comments or string literals as a
    disabled test.  Replacing the skipped characters with spaces keeps line
    numbers and column offsets stable for diagnostics.
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
                if text[index] != "\n":
                    characters[index] = " "
                index += 1
            continue
        if character == quote:
            state = "normal"
            index += 1
            continue
        if character != "\n":
            characters[index] = " "
        index += 1
    return "".join(characters)


def _marker_counts(root: Path) -> dict[str, int]:
    counts = {"skip": 0, "xfail": 0, "disabled": 0}
    bindings = _capability_bindings(root)
    for relative, path in _regular_files(root).items():
        if not _is_test(relative):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        text = _python_code(_without_capability_guards(text, bindings)) if path.suffix == ".py" else _strip_comments(text)
        counts["skip"] += sum(
            1
            for pattern in (_SKIP_MARKERS[0], _SKIP_MARKERS[6], _SKIP_MARKERS[7], _SKIP_MARKERS[8])
            for _ in pattern.finditer(text)
        )
        counts["xfail"] += sum(
            1
            for pattern in (_SKIP_MARKERS[1], _SKIP_MARKERS[9])
            for _ in pattern.finditer(text)
        )
        counts["disabled"] += sum(
            1
            for pattern in (*_SKIP_MARKERS[2:6], _DISABLED_ALIAS)
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


def _test_integrity(
    root: Path,
    baseline: Mapping[str, Any] | None,
    config: Mapping[str, Any],
    *,
    source: str = "source-test-baseline",
) -> tuple[dict[str, object], dict[str, object]]:
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

    def _in_scope(relative: str) -> bool:
        normalized = str(relative).replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        return normalized == "mcp-server" or normalized.startswith("mcp-server/")

    # The frozen target baseline is the *reference* ported suite.  A legal
    # migration authors its own ``mcp-server/tests/`` suite, so comparing its
    # self-written files against the reference test files would reject a valid
    # implementation (and cannot distinguish a legitimate rewrite from a
    # weakened one).  The reference test files are only protected when the
    # candidate actually reuses them: if any baseline test file is present
    # byte-for-byte, the candidate suite is a derivative of the reference and
    # missing/weakened siblings are tampering.  Comment/whitespace-only edits
    # are always accepted.
    derived_suite = any(
        str(relative) in current
        and isinstance(record, Mapping)
        and record.get("sha256") == _digest(current[str(relative)])
        for relative, record in baseline_files.items()
        if _in_scope(relative)
    )
    missing: list[str] = []
    for relative in baseline_files:
        normalized = str(relative)
        if normalized in current:
            continue
        if not _in_scope(normalized) or derived_suite:
            missing.append(normalized)
    missing = sorted(missing)
    allowed = {
        str(item).replace("\\", "/").strip("/")
        for item in config.get("policy", {}).get("allowed_equivalent_test_paths", [])
    } if isinstance(config.get("policy"), Mapping) else set()
    changed: list[str] = []
    for relative, record in baseline_files.items():
        if relative not in current or not isinstance(record, Mapping):
            continue
        if str(relative) in allowed:
            continue
        if _in_scope(relative) and not derived_suite:
            # A self-authored ported suite is not a reference file to tamper
            # with; only the marker/bootstrap checks apply to it.
            continue
        path = current[relative]
        if record.get("sha256") == _digest(path):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            text = ""
        if record.get("normalized_sha256") and record.get("normalized_sha256") == _normalized_digest(text):
            continue
        # For a derivative suite, any non-comment change to a reference test
        # file is a modification of protected test code (this covers setups
        # rewritten around unchanged assertions).  For source-visible root test
        # files, an assertion-preserving rewrite is still accepted.
        if not _in_scope(relative) and record.get("assertions") and record.get("assertions") == _assertion_signature(text):
            continue
        changed.append(str(relative))
    changed = sorted(changed)
    bootstrap_tampered = _bootstrap_tamper_hits(root)
    marker_before = baseline.get("marker_counts", {})
    marker_before = marker_before if isinstance(marker_before, Mapping) else {}
    marker_after = _marker_counts(root)
    marker_delta = {
        key: int(marker_after.get(key, 0)) - int(marker_before.get(key, 0))
        for key in {"skip", "xfail", "disabled"}
    }
    tamper = _atom(
        "FAIL" if missing or changed or bootstrap_tampered else "PASS",
        source=source,
        evidence={
            "missing": missing,
            "changed": changed,
            "test_bootstrap_tampered": bootstrap_tampered,
        },
    )
    skip = _atom(
        "FAIL" if any(value > 0 for value in marker_delta.values()) else "PASS",
        source=source,
        evidence={"before": dict(marker_before), "after": marker_after, "delta": marker_delta},
    )
    return tamper, skip


def build_hardgate_evidence(
    candidate_root: Path,
    *,
    config: Mapping[str, Any],
    observations: Sequence[Mapping[str, Any]],
    observation_errors: Sequence[str],
    receipts: Sequence[Mapping[str, Any]],
    patch_applicable: bool | None,
    source_test_baseline: Mapping[str, Any] | None,
    test_baseline_source: str = "source-test-baseline",
) -> dict[str, object]:
    """Derive hardgate atoms from Case evidence instead of policy booleans."""

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
    deceptive_diagnostics = classify_residue_rules(candidate_root, deceptive_rules)
    deceptive_hits = scan_residue_rules(candidate_root, deceptive_rules)
    # Runtime delegation to the source implementation is a *semantic* property:
    # the declarative literal/dependency rules only cover a few pinned paths, so
    # language-neutral detectors additionally scan the migrated sub-project for
    # process, FFI and dynamic-load sinks paired with a source-runtime token.
    # The out-of-scope root project legitimately keeps its own source runtime
    # (``Constraint 4``), so only hits inside the migrated ``mcp-server/`` tree
    # are relevant.
    detector_hits = [
        hit
        for hit in (
            *detect_source_runtime_subprocess(candidate_root, config),
            *detect_source_runtime_ffi(candidate_root, config),
            *detect_source_runtime_dynamic_load(candidate_root, config),
        )
        if str(hit.get("path", "")).startswith("mcp-server/")
        and not _is_test(str(hit.get("path", "")))
    ]
    if detector_hits:
        deceptive_hits = [*deceptive_hits, *detector_hits]
    protected = policy.get("protected_paths", config.get("protected_paths", []))
    protected = protected if isinstance(protected, Sequence) and not isinstance(protected, (str, bytes)) else []
    marker_names = policy.get("forbidden_report_markers", [])
    marker_names = marker_names if isinstance(marker_names, Sequence) and not isinstance(marker_names, (str, bytes)) else []
    protected_hits = _protected_hits(candidate_root, [str(item) for item in protected])
    marker_hits = _report_marker_hits(candidate_root, [str(item) for item in marker_names])
    tests_not_tampered, no_new_skip = _test_integrity(
        candidate_root, source_test_baseline, config, source=test_baseline_source
    )
    build_evidence, dependency_evidence, valid_receipts = _derive_execution_atoms(
        receipts
    )
    source_dependency_hits = detect_source_runtime_dependency(candidate_root, config)
    if source_dependency_hits:
        dependency_evidence = _atom(
            "FAIL",
            source="source-runtime-dependency",
            evidence=source_dependency_hits,
        )
    public = _group_status(
        p2p,
        expected=len(expected_tests.get("p2p", [])) if isinstance(expected_tests.get("p2p", []), Sequence) else None,
        source="candidate-observations",
        empty="NA",
    )
    p2p_regression = _group_status(
        p2p,
        expected=len(expected_tests.get("p2p", []))
        if isinstance(expected_tests.get("p2p", []), Sequence)
        else None,
        source="candidate-p2p-observations",
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


__all__ = ["build_hardgate_evidence", "build_source_test_baseline"]
