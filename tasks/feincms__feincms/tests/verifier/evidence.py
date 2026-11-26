"""Evidence derivation for the fixed Migration Grader hardgate contract."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
from collections import Counter
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
    re.compile(r"#\[ignore\]", re.IGNORECASE),
    re.compile(r"\bt\.Skip(?:Now|f)?\s*\(", re.IGNORECASE),
    re.compile(r"@(?:Disabled|Ignore)\b", re.IGNORECASE),
    re.compile(r"\b(?:test|it|describe)\.todo\s*\(", re.IGNORECASE),
    re.compile(r"\bpassWithNoTests\b", re.IGNORECASE),
    # A bare ``exclude`` / ``exclude =`` is ordinary code (``const exclude =
    # new Set()``), not a skipped test, so no generic exclude marker is counted.
    # Test-runner exclude flags live in config files that are not counted here.
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


_ASSERTION_TOKENS = re.compile(
    r"\b(?:"
    # unittest's CamelCase assertion methods (longest first so the trailing
    # word boundary resolves to the full method name).
    r"assert(?:"
    r"NotAlmostEqual|NotIsInstance|RaisesRegex|WarnsRegex|GreaterEqual|LessEqual"
    r"|NotEquals|MultiLineEqual|SequenceEqual|CountEqual|AlmostEqual|IsNotNone"
    r"|IsInstance|NotRegex|ItemsEqual|ListEqual|DictEqual|SetEqual"
    r"|Equals|Raises|Warns|Regex|Equal|NotEqual|True|False|Null|NotNull"
    r"|That|Is|In|NotIn|IsNone|Greater|Less|Logs"
    r")"
    r"|assert(?:_eq|_ne|_matches|_contains|_that)?!?"
    r"|expect|should|require|t\.(?:Error|Fatal|Fatalf|Errorf)"
    r")\b",
    re.IGNORECASE,
)


def _strip_comments_keep_strings(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"(?m)//.*$", " ", text)
    text = re.sub(r"(?m)#(?!\[).*$", " ", text)
    return text


def _normalized_text(text: str) -> str:
    text = _strip_comments_keep_strings(text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_python(text: str):
    try:
        return ast.parse(text)
    except SyntaxError:
        return None


_UNKNOWN_CONSTANT = object()

#: Zero-argument builtins that construct an empty (falsy) value.
_EMPTY_CONSTRUCTORS = frozenset(
    {"set", "frozenset", "tuple", "list", "dict", "bytes", "str"}
)


def _constant_literal(node, values):
    """Fold ``node`` to a literal value, resolving names bound to constants.

    Returns :data:`_UNKNOWN_CONSTANT` when the value is not statically known.
    """

    if isinstance(node, ast.Name):
        return values.get(node.id, _UNKNOWN_CONSTANT)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        name = node.func.id
        if name in _EMPTY_CONSTRUCTORS and not node.args and not node.keywords:
            # ``frozenset()`` / ``set()`` / ``list()`` ... fold to the empty
            # value so a name bound to it is recognised as a constant guard.
            return {
                "set": set,
                "frozenset": frozenset,
                "tuple": tuple,
                "list": list,
                "dict": dict,
                "bytes": bytes,
                "str": str,
            }[name]()
        if name == "len" and len(node.args) == 1:
            sequence = _constant_literal(node.args[0], values)
            if sequence is not _UNKNOWN_CONSTANT and hasattr(sequence, "__len__"):
                return len(sequence)
            return _UNKNOWN_CONSTANT
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return _UNKNOWN_CONSTANT


def _constant_truth(node, values):
    """Statically-known truthiness of ``node`` (:data:`_UNKNOWN_CONSTANT` else).

    A guard is a *constant* guard whenever its truth value is decided at author
    time, whatever the spelling: literals (``False`` / ``0`` / ``""`` / ``None``),
    empty containers (``[]`` / ``()`` / ``{}`` / ``set()``), ``len(<constant>)``,
    ``not <constant>`` / ``<constant> and ...`` / ``... or <constant>`` and
    constant comparisons.  ``if []:`` is as dead as ``if False:``.
    """

    literal = _constant_literal(node, values)
    if literal is not _UNKNOWN_CONSTANT:
        return bool(literal)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        inner = _constant_truth(node.operand, values)
        return _UNKNOWN_CONSTANT if inner is _UNKNOWN_CONSTANT else (not inner)
    if isinstance(node, ast.BoolOp):
        parts = [_constant_truth(value, values) for value in node.values]
        if isinstance(node.op, ast.And):
            if any(part is False for part in parts):
                return False
            if all(part is True for part in parts):
                return True
            return _UNKNOWN_CONSTANT
        if isinstance(node.op, ast.Or):
            if any(part is True for part in parts):
                return True
            if all(part is False for part in parts):
                return False
            return _UNKNOWN_CONSTANT
        return _UNKNOWN_CONSTANT
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if (
            node.func.id in _EMPTY_CONSTRUCTORS
            and not node.args
            and not node.keywords
        ):
            return False
        if node.func.id == "bool" and len(node.args) == 1:
            return _constant_truth(node.args[0], values)
        if node.func.id == "len" and len(node.args) == 1:
            sequence = _constant_literal(node.args[0], values)
            if sequence is not _UNKNOWN_CONSTANT and hasattr(sequence, "__len__"):
                return bool(len(sequence))
            return _UNKNOWN_CONSTANT
        return _UNKNOWN_CONSTANT
    if (
        isinstance(node, ast.Compare)
        and len(node.ops) == 1
        and len(node.comparators) == 1
    ):
        operator = node.ops[0]
        right = _constant_literal(node.comparators[0], values)
        # ``x in []`` / ``x not in ()`` is a constant guard for any ``x``.
        if isinstance(operator, (ast.In, ast.NotIn)) and right is not _UNKNOWN_CONSTANT:
            if hasattr(right, "__len__") and len(right) == 0:
                return isinstance(operator, ast.NotIn)
        left = _constant_literal(node.left, values)
        if left is _UNKNOWN_CONSTANT or right is _UNKNOWN_CONSTANT:
            return _UNKNOWN_CONSTANT
        try:
            if isinstance(operator, ast.Eq):
                return bool(left == right)
            if isinstance(operator, ast.NotEq):
                return bool(left != right)
            if isinstance(operator, ast.Lt):
                return bool(left < right)
            if isinstance(operator, ast.LtE):
                return bool(left <= right)
            if isinstance(operator, ast.Gt):
                return bool(left > right)
            if isinstance(operator, ast.GtE):
                return bool(left >= right)
            if isinstance(operator, ast.In):
                return bool(left in right)
            if isinstance(operator, ast.NotIn):
                return bool(left not in right)
        except TypeError:
            return _UNKNOWN_CONSTANT
    return _UNKNOWN_CONSTANT


def _constant_truth_names(tree) -> dict[str, object]:
    """Names bound exactly once to a constant value (conservative).

    Only names with a *single* simple assignment are resolved, and only when
    they are never re-bound by any other construct (function parameter, loop
    target, ``with`` binding, ``match`` capture, import alias, walrus, augmented
    or tuple assignment).  A flow-insensitive constant map must not claim a name
    is constant when a later statement can change it, or a legitimate ``while n:``
    loop would be mistaken for dead code.
    """

    counters: dict[str, int] = {}
    disqualified: set[str] = set()

    def _bind(name: str) -> None:
        counters[name] = counters.get(name, 0) + 1

    def _disqualify_target(target) -> None:
        for node in ast.walk(target):
            if isinstance(node, ast.Name):
                disqualified.add(node.id)

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            if not all(isinstance(target, ast.Name) for target in node.targets):
                for target in node.targets:
                    _disqualify_target(target)
                continue
            for target in node.targets:
                _bind(target.id)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                _bind(node.target.id)
            else:
                _disqualify_target(node.target)
        elif isinstance(node, (ast.AugAssign, ast.NamedExpr)):
            _disqualify_target(node.target)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            _disqualify_target(node.target)
        elif isinstance(node, ast.comprehension):
            _disqualify_target(node.target)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            arguments = node.args
            for argument in (
                list(getattr(arguments, "posonlyargs", []))
                + list(arguments.args)
                + list(arguments.kwonlyargs)
            ):
                disqualified.add(argument.arg)
            if arguments.vararg:
                disqualified.add(arguments.vararg.arg)
            if arguments.kwarg:
                disqualified.add(arguments.kwarg.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            disqualified.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                disqualified.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.MatchAs) and node.name:
            disqualified.add(node.name)
        elif isinstance(node, ast.MatchStar) and node.name:
            disqualified.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            disqualified.add(node.rest)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            disqualified.update(node.names)
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            _disqualify_target(node.optional_vars)

    values: dict[str, object] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and all(
            isinstance(target, ast.Name) for target in node.targets
        ):
            for target in node.targets:
                if counters.get(target.id, 0) != 1 or target.id in disqualified:
                    continue
                resolved = _constant_literal(node.value, {})
                if resolved is not _UNKNOWN_CONSTANT:
                    values[target.id] = resolved
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if counters.get(node.target.id, 0) == 1 and node.target.id not in disqualified:
                resolved = _constant_literal(node.value, {}) if node.value is not None else _UNKNOWN_CONSTANT
                if resolved is not _UNKNOWN_CONSTANT:
                    values[node.target.id] = resolved
    return values


def _statically_dead_lines(tree) -> set[int]:
    """Lines the candidate can never execute in the tests it keeps.

    Assertions wrapped in a guard that is *statically false* -- ``if False:`` /
    ``if 0:`` and every equivalent constant guard (``if []:`` / ``if ():`` /
    ``if {}:`` / ``if set():`` / ``if not True:`` / ``if len(""):`` ...) -- are
    textually present but never run, which is the "assertion silently removed"
    case expressed as dead code.  Their lines are treated as missing so the
    delete/weaken audit still fires.  An ``if`` whose guard is statically *true*
    marks its ``else`` branch dead, and a statically-false ``while`` body is
    dead as well.
    """

    dead: set[int] = set()
    values = _constant_truth_names(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            truth = _constant_truth(node.test, values)
            if truth is _UNKNOWN_CONSTANT:
                continue
            body = node.orelse if truth else node.body
        elif isinstance(node, ast.While):
            if _constant_truth(node.test, values) is not False:
                continue
            body = node.body
        else:
            continue
        for statement in body:
            for line in range(statement.lineno, (statement.end_lineno or statement.lineno) + 1):
                dead.add(line)
    return dead


def _assertion_token_lines(text: str) -> list[tuple[int, str]]:
    """Return ``(line_number, normalized_assertion_line)`` for every assertion."""

    text = _strip_comments_keep_strings(text)
    rows: list[tuple[int, str]] = []
    for number, line in enumerate(text.splitlines(), 1):
        if not _ASSERTION_TOKENS.search(line):
            continue
        normalized = _ASSERTION_TOKENS.sub("ASSERT", line)
        normalized = re.sub(r"[\s'\"!]+", "", normalized)
        if normalized:
            rows.append((number, normalized))
    return rows


def _test_function_names(text: str) -> list[str]:
    tree = _parse_python(text)
    if tree is None:
        return []
    return sorted(
        {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")
        }
    )


def _assertion_map(text: str) -> dict[str, list[str]]:
    """Bucket a test file's normalized assertion lines by owning function.

    Tampering is a per-test judgement: a migration may re-express a test with a
    stronger loop of assertions, but the assertions of an individual test must
    not disappear (deleted, commented out, rewritten, weakened or made
    unreachable) unless that same test gained assertions.  Bucketing by function
    name is what lets an added/strengthened test pass while a silently weakened
    one still fails.
    """

    token_lines = _assertion_token_lines(text)
    tree = _parse_python(text)
    if tree is None:
        return {"__top__": [row for _, row in token_lines]}
    dead = _statically_dead_lines(tree)
    buckets: dict[str, list[str]] = {}
    covered: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        last = node.end_lineno or node.lineno
        covered.update(range(node.lineno, last + 1))
        rows = [
            row
            for line, row in token_lines
            if node.lineno <= line <= last and line not in dead
        ]
        # ``assertEqual((a, b, ...), (x, y, ...))`` is one call but expresses
        # one equality for every tuple item.  Count those logical assertions
        # separately so consolidating several scalar assertions into a stronger
        # tuple comparison is not mislabeled as test weakening.  Keep the
        # existing line witness as the first equality and add one synthetic
        # witness for each remaining pair.  This does not make a changed scalar
        # assertion pass: only equal-length tuple/list comparisons gain rows.
        for call in ast.walk(node):
            if (
                not isinstance(call, ast.Call)
                or call.lineno in dead
                or not isinstance(call.func, ast.Attribute)
                or call.func.attr not in {"assertEqual", "assertEquals"}
                or len(call.args) < 2
                or not isinstance(call.args[0], (ast.Tuple, ast.List))
                or not isinstance(call.args[1], (ast.Tuple, ast.List))
                or len(call.args[0].elts) != len(call.args[1].elts)
            ):
                continue
            for index, (left, right) in enumerate(
                zip(call.args[0].elts[1:], call.args[1].elts[1:]), 1
            ):
                rows.append(
                    "ASSERT_TUPLE_ITEM:%d:%s:%s"
                    % (
                        index,
                        ast.dump(left, include_attributes=False),
                        ast.dump(right, include_attributes=False),
                    )
                )
        if rows:
            buckets.setdefault(node.name, []).extend(rows)
    remaining = [
        row for line, row in token_lines if line not in covered and line not in dead
    ]
    if remaining:
        buckets["__top__"] = remaining
    return buckets


def _assertion_lines(text: str) -> list[str]:
    """Return the normalized assertion-bearing lines of a test file.

    The list is the per-line *witness* of the assertions a file contains.  It is
    stored in the frozen baseline so tampering can be detected as "a baseline
    assertion line disappeared" (delete / comment out / weaken) instead of "the
    file changed at all".  Newly added assertions simply add rows and are not a
    regression, which matches the verifier contract that only deleting,
    commenting out, rewriting or weakening assertions is tampering.
    """

    return [row for _, row in _assertion_token_lines(text)]


def _assertion_signature(text: str) -> str:
    rows = _assertion_lines(text)
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest() if rows else ""


def _normalized_digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(_normalized_text(text).encode("utf-8")).hexdigest()


def _is_test(path: str) -> bool:
    parts = path.replace("\\", "/").split("/")
    directories = {part.casefold() for part in parts[:-1]}
    if directories & _NON_TEST_DIRS:
        return False
    if Path(path).suffix.casefold() in _NON_TEST_SUFFIXES:
        return False
    return bool(directories & _TEST_DIRS) or bool(_TEST_NAME.search(path))


_EXCLUDED_PARTS = frozenset(
    {
        ".git",
        "node_modules",
        ".gomodcache",
        ".gocache",
        "__pycache__",
        ".pytest_cache",
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
            if character == "#" and not (
                index + 1 < length and text[index + 1] == "["
            ):
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
    for relative, path in _regular_files(root).items():
        if not _is_test(relative):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        text = _strip_comments(text)
        counts["skip"] += sum(
            1 for pattern in _SKIP_MARKERS[:1] for _ in pattern.finditer(text)
        )
        counts["xfail"] += sum(
            1 for pattern in (_SKIP_MARKERS[1],) for _ in pattern.finditer(text)
        )
        counts["disabled"] += sum(
            1
            for pattern in (*_SKIP_MARKERS[2:], _DISABLED_ALIAS)
            for _ in pattern.finditer(text)
        )
    return counts


def build_source_test_baseline(root: Path) -> dict[str, object]:
    """Freeze source-visible test identities and marker counts for a Case."""

    files = {}
    for relative, path in sorted(_regular_files(root).items()):
        if not _is_test(relative):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            text = ""
        files[relative] = {
            "sha256": _digest(path),
            "normalized_sha256": _normalized_digest(text),
            "assertions": _assertion_signature(text),
            "assertion_lines": _assertion_lines(text),
            "assertion_functions": _assertion_map(text),
            "test_functions": _test_function_names(text),
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


def _public_contract_evidence(
    observations: Sequence[Mapping[str, Any]],
    surfaces: object,
) -> dict[str, object]:
    """Map declared public surfaces to independent CONTRACT observations.

    The public contract is audited only through its own ``CONTRACT``
    observations.  A declared surface that binds anything else (legacy P2P/F2P
    aliases) or a missing CONTRACT row is ``INCONCLUSIVE``: the evaluator cannot
    prove the contract without the independent probe.
    """

    if not isinstance(surfaces, Sequence) or isinstance(surfaces, (str, bytes)):
        return _atom(
            "NA",
            source="public-contract-surfaces",
            evidence={"reason": "no declared public surface"},
        )
    if not [row for row in surfaces if isinstance(row, Mapping)]:
        return _atom(
            "NA",
            source="public-contract-surfaces",
            evidence={"reason": "no declared public surface"},
        )
    by_id = {
        str(row.get("id")): row
        for row in observations
        if isinstance(row, Mapping) and row.get("id")
    }
    contract_ids = {
        identifier
        for identifier, row in by_id.items()
        if str(row.get("kind")) == "CONTRACT"
    }
    selected: list[Mapping[str, Any]] = []
    missing: list[str] = []
    bound_ids: list[str] = []
    for surface in surfaces:
        if not isinstance(surface, Mapping):
            continue
        for identifier in surface.get("observation_ids", []) or []:
            text = str(identifier)
            if text not in bound_ids:
                bound_ids.append(text)
    for identifier in bound_ids:
        if identifier not in contract_ids:
            missing.append(identifier)
            continue
        if all(str(row.get("id")) != identifier for row in selected):
            selected.append(by_id[identifier])
    if not contract_ids:
        return _atom(
            "INCONCLUSIVE",
            source="public-contract-surfaces",
            evidence={
                "reason": "no independent CONTRACT observation for declared public surfaces",
                "missing_observation_ids": sorted(by_id),
            },
        )
    if not selected and not missing:
        return _atom(
            "NA",
            source="public-contract-surfaces",
            evidence={"reason": "declared public surfaces have no observations"},
        )
    if missing:
        return _atom(
            "INCONCLUSIVE",
            source="public-contract-surfaces",
            evidence={
                "binding": "contract",
                "missing_observation_ids": sorted(missing),
            },
        )
    result = _group_status(
        selected,
        expected=len(selected),
        source="public-contract-observations",
        empty="NA",
    )
    evidence = dict(result.get("evidence", {})) if isinstance(result.get("evidence"), Mapping) else {}
    evidence["binding"] = "contract"
    result["evidence"] = evidence
    return result


_CRITICAL_INVARIANTS = (
    "record_count",
    "stable_ids",
    "foreign_key_relations",
    "timestamp_precision",
    "permission_boundaries",
    "error_contract",
)


def _invariant_status(value: object) -> str:
    if isinstance(value, bool):
        return "PASS" if value else "FAIL"
    if isinstance(value, Mapping):
        status = value.get("status")
        if isinstance(status, str) and status.upper() in {"PASS", "FAIL", "NA", "INCONCLUSIVE"}:
            return status.upper()
        for key in ("pass", "passed", "ok"):
            if key in value:
                return "PASS" if value[key] is True else "FAIL"
    if isinstance(value, str) and value.upper() in {"PASS", "FAIL", "NA"}:
        return value.upper()
    return "INCONCLUSIVE"


def _critical_data_evidence(
    rows: Sequence[Mapping[str, Any]],
    *,
    expected: int,
    applicable: bool,
) -> dict[str, object]:
    if not applicable:
        return _atom(
            "NA",
            source="critical-data-invariants",
            evidence={"reason": "persistent state is not applicable"},
        )
    if not rows:
        return _atom(
            "INCONCLUSIVE",
            source="critical-data-invariants",
            evidence={"reason": "no critical-data observations"},
        )
    statuses: list[str] = []
    details: dict[str, list[str]] = {key: [] for key in _CRITICAL_INVARIANTS}
    structured = 0
    legacy = 0
    for row in rows:
        value = row.get("value")
        payload = value.get("value") if isinstance(value, Mapping) and value.get("type") == "json" else None
        invariants = payload.get("invariants") if isinstance(payload, Mapping) else None
        if not isinstance(invariants, Mapping):
            if isinstance(payload, Mapping) and any(key in payload for key in _CRITICAL_INVARIANTS):
                invariants = payload
            else:
                legacy += 1
                statuses.append(str(row.get("status", "INCONCLUSIVE")))
                continue
        structured += 1
        for key in _CRITICAL_INVARIANTS:
            if key not in invariants:
                status = "INCONCLUSIVE"
            else:
                status = _invariant_status(invariants[key])
            details[key].append(status)
            statuses.append(status)
    overall = (
        "INFRA_ERROR" if "INFRA_ERROR" in statuses
        else "INCONCLUSIVE" if "INCONCLUSIVE" in statuses
        else "FAIL" if "FAIL" in statuses
        else "NA" if statuses and all(item == "NA" for item in statuses)
        else "PASS"
    )
    return _atom(
        overall,
        source="critical-data-invariants",
        evidence={
            "expected_observations": expected,
            "observed_observations": len(rows),
            "structured_observations": structured,
            "legacy_observations": legacy,
            "invariants": details,
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
    missing = sorted(
        str(relative) for relative in baseline_files
        if str(relative) not in current
    )
    allowed = {
        str(item).replace("\\", "/").strip("/")
        for item in config.get("policy", {}).get("allowed_equivalent_test_paths", [])
    } if isinstance(config.get("policy"), Mapping) else set()
    changed: list[str] = []
    weakened: list[str] = []
    for relative, record in baseline_files.items():
        if relative not in current or not isinstance(record, Mapping):
            continue
        if relative in allowed:
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
        changed.append(relative)
        baseline_functions = record.get("assertion_functions")
        if isinstance(baseline_functions, Mapping):
            candidate_functions = _assertion_map(text)
            candidate_tests = set(_test_function_names(text))
            for name, base_lines in baseline_functions.items():
                candidate_lines = candidate_functions.get(str(name))
                if candidate_lines is None:
                    # The test (or its assertion bucket) disappeared entirely.
                    weakened.append("%s::%s" % (relative, name))
                    continue
                if Counter(str(row) for row in base_lines) <= Counter(candidate_lines):
                    # Every baseline assertion is still present: additions and
                    # equivalent re-expressions are legitimate.
                    continue
                if len(candidate_lines) > len(base_lines):
                    # The contained test gained assertions, so the change is a
                    # strengthening (for example a literal assertion rewritten
                    # as a stronger loop).
                    continue
                weakened.append("%s::%s" % (relative, name))
            for name in record.get("test_functions") or ():
                if str(name) not in candidate_tests:
                    weakened.append("%s::%s::removed" % (relative, name))
        elif record.get("assertions"):
            # Legacy baseline without per-function witnesses: stay fail-closed
            # and treat any assertion-signature change as tampering.
            if record.get("assertions") != _assertion_signature(text):
                weakened.append(relative)
    changed = sorted(changed)
    weakened = sorted(weakened)
    marker_before = baseline.get("marker_counts", {})
    marker_before = marker_before if isinstance(marker_before, Mapping) else {}
    marker_after = _marker_counts(root)
    marker_delta = {
        key: int(marker_after.get(key, 0)) - int(marker_before.get(key, 0))
        for key in {"skip", "xfail", "disabled"}
    }
    tamper = _atom(
        "FAIL" if missing or weakened else "PASS",
        source=source,
        evidence={"missing": missing, "changed": changed, "weakened": weakened},
    )
    skip = _atom(
        "FAIL" if any(value > 0 for value in marker_delta.values()) else "PASS",
        source=source,
        evidence={"before": dict(marker_before), "after": marker_after, "delta": marker_delta},
    )
    return tamper, skip


def _deceptive_scan_paths(config: Mapping[str, Any]) -> list[str]:
    """Files named by the declarative deceptive-fallback rules.

    The semantic fallback detector inspects the same declared scan scope as the
    literal rules, so a rename of ``django.VERSION`` cannot shrink the surface.
    """

    policy = config.get("policy")
    policy = policy if isinstance(policy, Mapping) else {}
    rules = policy.get("deceptive_fallback_rules")
    if not isinstance(rules, (list, tuple)):
        return []
    paths: list[str] = []
    for row in rules:
        if not isinstance(row, Mapping):
            continue
        declared = row.get("paths")
        if not isinstance(declared, (list, tuple)):
            continue
        for item in declared:
            text = str(item).strip()
            if not text or Path(text).is_absolute() or ".." in Path(text).parts:
                continue
            if "\\" in text:
                continue
            if text not in paths:
                paths.append(text)
    return paths


#: Top-level directories that never hold production source of the migrated
#: project.  A version fallback relocated into a *new* module must still be
#: scanned, so the semantic detector widens the declared scope to the whole
#: production tree instead of only the two files named by the literal rules.
_NON_PRODUCTION_DIRS = frozenset(
    {
        "tests",
        "test",
        "docs",
        "doc",
        "node_modules",
        "vendor",
        ".git",
        ".tox",
        ".venv",
        "venv",
        "build",
        "dist",
        "site-packages",
        "__pycache__",
    }
)


def _production_source_files(root: Path) -> list[str]:
    """Repository-relative ``*.py`` production files (declared scope included)."""

    files: list[str] = []
    try:
        base = root.resolve()
    except OSError:
        return files
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            name
            for name in dirnames
            if name not in _NON_PRODUCTION_DIRS and not name.startswith(".")
        ]
        for name in filenames:
            if not name.endswith(".py"):
                continue
            full = Path(dirpath) / name
            try:
                if full.is_symlink() or not full.is_file():
                    continue
                relative = full.relative_to(base).as_posix()
            except (OSError, ValueError):
                continue
            files.append(relative)
    return sorted(files)


# ---------------------------------------------------------------------------
# static production-source analysis -- single implementation
# ---------------------------------------------------------------------------
# The retained-Django-version-branch atom (``no_deceptive_source_fallback``) and
# the F2P ``legacy_version_branch`` projection are the *same* property, so the
# detector is implemented exactly once (in the stdlib-only asset module
# ``assets/shared/integration/static_probes.py``) and reused here.  Keeping one
# implementation makes it impossible for the L2 anti-cheating atom and the F2P
# projection to drift, and guarantees a single equality family for the version
# value (``django.VERSION`` / ``django.__version__`` /
# ``importlib.metadata.version("Django")`` / ...).
def _load_static_probes():
    import importlib.util

    path = (
        Path(__file__).resolve().parents[1]
        / "assets"
        / "shared"
        / "integration"
        / "static_probes.py"
    )
    spec = importlib.util.spec_from_file_location("evidence_static_probes", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_STATIC_PROBES = _load_static_probes()
_resolve_string = _STATIC_PROBES._resolve_string
_string_constants = _STATIC_PROBES._string_constants
_dict_literal_keys = _STATIC_PROBES._dict_literal_keys
_has_version_branch = _STATIC_PROBES.has_version_branch
_has_is_dst_argument = _STATIC_PROBES.has_is_dst_argument



_TARGET_DJANGO = "5.2"


def _expand_tox_variables(tox_ini: str) -> str:
    """Expand tox ``{[section]key}`` substitutions to their literal values.

    A retained pre-5.2 matrix entry written as
    ``djold: Django>={[testenv]legacy_version},<4.0`` must be resolved before the
    version constraint is read.
    """

    variables: dict[tuple[str, str], str] = {}
    section = None
    for line in tox_ini.splitlines():
        header = re.match(r"^\[([^\]]+)\]\s*$", line.strip())
        if header:
            section = header.group(1)
            continue
        if section is None or line[:1].isspace():
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            variables[(section, key.strip())] = value.strip()
    pattern = re.compile(r"\{\[([^\]]+)\]([^}]+)\}")
    text = tox_ini
    for _ in range(10):
        expanded = pattern.sub(
            lambda match: variables.get(
                (match.group(1), match.group(2).strip()), match.group(0)
            ),
            text,
        )
        if expanded == text:
            break
        text = expanded
    # ``{env:KEY:DEFAULT}`` (and ``{env:KEY}``) substitution: resolve to the
    # declared default so a bound such as ``Django>={env:LEGACY_DJANGO:3.2},<4.0``
    # is still read instead of being hidden behind the braces.
    env_pattern = re.compile(r"\{env:([^}:]+)(?::([^}]*))?\}")
    text = env_pattern.sub(
        lambda match: match.group(2)
        if match.group(2) is not None
        else match.group(0),
        text,
    )
    return text


def _version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def _tox_permits_lower_django(tox_ini: str) -> bool:
    """True when an expanded tox dependency permits a Django version below 5.2."""

    text = _expand_tox_variables(tox_ini)
    target = _version_tuple(_TARGET_DJANGO)
    pattern = re.compile(
        r"(?i)django(?:\s*\[[^\]]*\])?\s*"
        r"((?:\s*(?:<=|>=|==|!=|~=|<|>)\s*v?\d+(?:\.\d+)*(?:\s*,\s*)?)+)"
    )
    bound = re.compile(r"(<=|>=|==|!=|~=|<|>)\s*v?(\d+(?:\.\d+)*)")
    for match in pattern.finditer(text):
        for part in match.group(1).split(","):
            version_match = bound.search(part)
            if not version_match:
                continue
            operator = version_match.group(1)
            version = _version_tuple(version_match.group(2))
            if operator in ("<=", "<"):
                if version <= target:
                    return True
            elif version < target:
                return True
    return False


def _tox_requirement_includes(root: Path, text: str) -> str:
    """Concatenate the contents of ``-r <file>`` requirement includes in ``text``.

    A retained pre-5.2 matrix entry written as ``djlegacy: -r
    requirements/legacy-django.txt`` must follow the referenced file, otherwise
    the Django constraint hiding inside it is never read.
    """

    try:
        base = root.resolve()
    except OSError:
        return ""
    chunks: list[str] = []
    for match in re.finditer(r"(?:^|\s)(?:-r|--requirement)\s+(\S+)", text):
        relative = match.group(1).strip().strip("'\"")
        candidate = root / relative
        try:
            if not candidate.is_file() or candidate.is_symlink():
                continue
            resolved = candidate.resolve()
            if not resolved.is_relative_to(base):
                continue
            chunks.append(candidate.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
    return "\n".join(chunks)


def _tox_scope_residues(root: Path) -> list[dict[str, Any]]:
    """Semantic scope residue: a ``tox`` matrix still permitting Django < 5.2.

    Renaming the environment (``djold``) or hiding the bound behind tox variable
    substitution (``{[testenv]legacy_version}`` / ``{env:KEY:DEFAULT}``) or a
    ``-r requirements/...`` include still tests a pre-5.2 Django and must fail
    ``migration_scope_complete``.
    """

    path = root / "tox.ini"
    if not path.is_file() or path.is_symlink():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    combined = text + "\n" + _tox_requirement_includes(root, text)
    if not _tox_permits_lower_django(combined):
        return []
    return [
        {
            "path": "tox.ini",
            "token": "django<5.2",
            "kind": "tox-django-floor",
            "rule_id": "SCOPE-TOX-DJANGO-FLOOR",
        }
    ]


def detect_source_version_fallback(
    root: Path, config: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Semantic detector for retained source-version compatibility fallbacks.

    Literal token rules only catch ``django.VERSION`` / ``is_dst=`` verbatim.
    This normalizes import aliases and expression forms so a retained
    ``Django < 5.2`` branch (``from django import VERSION as _DJV`` +
    ``if _DJV < (5,)``) or a keyword-expanded ``is_dst`` call
    (``**{"is_dst": True}``) is still reported as a deceptive source fallback.
    """

    hits: list[dict[str, Any]] = []
    scan_paths = list(_deceptive_scan_paths(config))
    for relative in _production_source_files(root):
        if relative not in scan_paths:
            scan_paths.append(relative)
    for relative in scan_paths:
        path = root / relative
        if not path.is_file() or path.is_symlink():
            continue
        try:
            source = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            tree = None
        version_branch = _has_version_branch(tree) if tree is not None else "django.VERSION" in source
        is_dst = _has_is_dst_argument(tree) if tree is not None else "is_dst=" in source
        if version_branch:
            hits.append(
                {
                    "detector": "source-version-fallback",
                    "path": relative,
                    "kind": "version-branch",
                    "reason": "runtime branch gated on the Django version is retained",
                }
            )
        if is_dst:
            hits.append(
                {
                    "detector": "source-version-fallback",
                    "path": relative,
                    "kind": "is-dst",
                    "reason": "legacy make_aware(is_dst=...) fallback is retained",
                }
            )
    return hits


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
    residue_hits = [
        *scan_residue_rules(candidate_root, residue_rules),
        *_tox_scope_residues(candidate_root),
    ]
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
    detector_hits = [
        *detect_source_runtime_subprocess(candidate_root, config),
        *detect_source_runtime_ffi(candidate_root, config),
        *detect_source_runtime_dynamic_load(candidate_root, config),
        *detect_source_version_fallback(candidate_root, config),
    ]
    if detector_hits:
        deceptive_hits = [*deceptive_hits, *detector_hits]
    public_contract = _public_contract_evidence(
        rows, policy.get("public_surface_evidence", [])
    )
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
        else _critical_data_evidence(
            critical_rows,
            expected=expected_critical,
            applicable=state_applicable,
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
        "public_contract_preserved": public_contract,
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
