"""Evidence derivation for the fixed Migration Grader hardgate contract."""

from __future__ import annotations

import hashlib
import json
import re
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
# Skip/xfail/disable markers, bucketed by intent.  A migration may be written
# in any source or target language, so the sets must cover the target language of
# the task as well as the generic Python/Jest forms: a Go candidate disables a
# test with ``t.Skip``/``t.SkipNow``/``b.Skip`` or the whole binary with
# ``testing.Short``, none of which the framework-specific patterns match.
_SKIP_MARKERS = (
    re.compile(r"pytest\.mark\.skip", re.IGNORECASE),
    # Go test/benchmark receivers.  Whitespace (including newlines) is allowed
    # around the selector dot -- ``t.\nSkipNow()`` and ``t . Skip()`` are the
    # same program as ``t.Skip()`` -- and the method-expression form
    # ``(*testing.T).Skip(t, ...)`` is accepted too.
    re.compile(
        r"(?:\(\s*\*\s*(?:testing\.)?[Tt][Bb]?\s*\)|\b(?:t|b|tb|f)\b)\s*\.\s*"
        r"Skip(?:Now|f)?\s*\(",
        re.IGNORECASE,
    ),
    re.compile(r"\btesting\.Short\s*\(", re.IGNORECASE),
)
_XFAIL_MARKERS = (
    re.compile(r"pytest\.mark\.xfail", re.IGNORECASE),
)
_DISABLED_MARKERS = (
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


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _is_test(path: str) -> bool:
    parts = path.replace("\\", "/").split("/")
    return "test" in {part.casefold() for part in parts[:-1]} or bool(
        _TEST_NAME.search(path)
    )


_EXCLUDED_PARTS = frozenset(
    {".git", "node_modules", ".gomodcache", ".gocache", "__pycache__", ".pytest_cache"}
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


def _strip_comments_keep_strings(text: str) -> str:
    """Blank ``//`` and ``/* */`` comments but keep string/rune literals.

    The frozen-test ``code_sha256`` compares *code state*, so prose edits and
    re-formatting must not look like tampering while a changed assertion or body
    must.  Unlike ``_strip_comments`` (which blanks string bodies too, for
    marker counting) this keeps the literal content and only removes comments;
    ``#`` is deliberately not treated as a comment because Rust test files use
    ``#[...]`` attributes that carry test semantics.
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
                index += 1
            continue
        if character == quote:
            state = "normal"
        index += 1
    return "".join(characters)


def _code_normalize(text: str) -> str:
    """Normalize code text: drop comments, collapse whitespace outside strings.

    Runs of whitespace (including indentation and newlines) collapse to a single
    space so a purely editorial reformat compares equal, while whitespace inside
    a string literal is preserved so a changed literal still differs.
    """

    text = _strip_comments_keep_strings(text)
    out: list[str] = []
    length = len(text)
    index = 0
    pending_space = False
    state = "normal"
    quote = ""
    while index < length:
        character = text[index]
        if state == "normal":
            if character in ("'", '"', "`"):
                if pending_space and out and out[-1] != " ":
                    out.append(" ")
                pending_space = False
                state = "string"
                quote = character
                out.append(character)
                index += 1
                continue
            if character.isspace():
                pending_space = True
                index += 1
                continue
            if pending_space and out and out[-1] != " ":
                out.append(" ")
            pending_space = False
            out.append(character)
            index += 1
            continue
        out.append(character)
        if character == "\\" and quote != "`" and index + 1 < length:
            out.append(text[index + 1])
            index += 2
            continue
        if character == quote:
            state = "normal"
        index += 1
    return "".join(out).strip()


def _code_digest(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        text = ""
    return "sha256:" + hashlib.sha256(_code_normalize(text).encode("utf-8")).hexdigest()


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
            1 for pattern in _SKIP_MARKERS for _ in pattern.finditer(text)
        )
        counts["xfail"] += sum(
            1 for pattern in _XFAIL_MARKERS for _ in pattern.finditer(text)
        )
        counts["disabled"] += sum(
            1
            for pattern in (*_DISABLED_MARKERS, _DISABLED_ALIAS)
            for _ in pattern.finditer(text)
        )
    return counts


def build_source_test_baseline(root: Path) -> dict[str, object]:
    """Freeze source-visible test identities and marker counts for a Case."""

    files = {
        relative: {"sha256": _digest(path), "code_sha256": _code_digest(path)}
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


def _merge_baseline_files(
    *baselines: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Union the ``files`` maps of several frozen test baselines."""

    files: dict[str, Any] = {}
    for baseline in baselines:
        if not isinstance(baseline, Mapping):
            continue
        candidates = baseline.get("files", {})
        if not isinstance(candidates, Mapping):
            continue
        for relative, record in candidates.items():
            files.setdefault(str(relative), record)
    return files


# ---------------------------------------------------------------------------
# Semantic assertion-strength analysis for target-language test files.
#
# ``tests_not_tampered`` must not depend on whether a delivered test file is
# byte-identical to the reference test (that rejected a legitimate
# candidate-authored equivalent rewrite), nor may it ignore the file entirely
# (that let a candidate silently delete/comment/weaken their own assertions).
# The comparison below is therefore *semantic*: it extracts, for every test
# function that exists in both the frozen reference test files and the
# candidate repository, the set of *decisions* the test makes -- the failing
# assertions (``t.Fatal``/``t.Fatalf``/``t.FailNow``/``t.Error``/``t.Errorf``/
# ``t.Fail``/``panic``) together with the comparison that guards them -- and
# flags only a genuine weakening:
#
#   * an ``if`` with a comparison condition whose body no longer contains a
#     failing assertion (the assertion was deleted or commented out), and
#   * a fixed expected value that was replaced by a degenerate one (empty
#     string / zero / nil) or by a different fixed value on the same compared
#     expression (the assertion was relaxed to something trivially true).
#
# Formatting, comments, reordering, equivalent syntax/framework conversions,
# table-driven rewrites, extra assertions and extra test functions are all
# ignored.  A baseline test function that the candidate does not provide is
# *not* flagged: writing one's own port is legitimate, and the structural
# checks already require the migration to ship target-language tests.
# ---------------------------------------------------------------------------

_GO_FAIL_METHODS = frozenset({"Fatal", "Fatalf", "FailNow", "Error", "Errorf", "Fail"})
_GO_COMPARISON_OPS = ("==", "!=", "<=", ">=", "<", ">")
_GO_MULTI_OPS = (
    "<<=", ">>=", "&^=", "...", "==", "!=", "<=", ">=", "&&", "||", "<-",
    "++", "--", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", ":=", "<<", ">>", "&^",
)
_GO_SINGLE_OPS = "+-*/%&|^!=<>()[]{},;:.~"


def _go_tokens(text: str) -> list[tuple[str, str, str]]:
    """Tokenize Go source into ``(kind, text, value)`` triples (comments dropped).

    Only the lexical structure needed for the assertion analysis is modelled;
    string/rune/comment bodies are never treated as code, so a commented-out or
    quoted assertion cannot be mistaken for a live one.
    """

    out: list[tuple[str, str, str]] = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n\f\v":
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            i += 2
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i = min(i + 2, n)
            continue
        if c == '"':
            j = i + 1
            buf: list[str] = []
            while j < n:
                if text[j] == "\\" and j + 1 < n:
                    buf.append(text[j + 1])
                    j += 2
                    continue
                if text[j] == '"':
                    break
                buf.append(text[j])
                j += 1
            out.append(("string", text[i:j + 1], "".join(buf)))
            i = j + 1
            continue
        if c == "`":
            j = i + 1
            while j < n and text[j] != "`":
                j += 1
            out.append(("string", text[i:j + 1], text[i + 1:j]))
            i = j + 1
            continue
        if c == "'":
            j = i + 1
            while j < n:
                if text[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                if text[j] == "'":
                    break
                j += 1
            out.append(("rune", text[i:j + 1], text[i + 1:j]))
            i = j + 1
            continue
        if c.isdigit() or (c == "." and i + 1 < n and text[i + 1].isdigit()):
            j = i
            while j < n and (text[j].isalnum() or text[j] in "._"):
                j += 1
            out.append(("number", text[i:j], text[i:j]))
            i = j
            continue
        if c.isalpha() or c == "_" or ord(c) > 127:
            j = i
            while j < n and (text[j].isalnum() or text[j] == "_" or ord(text[j]) > 127):
                j += 1
            out.append(("ident", text[i:j], text[i:j]))
            i = j
            continue
        matched = ""
        for op in _GO_MULTI_OPS:
            if text.startswith(op, i):
                matched = op
                break
        if matched:
            out.append(("op", matched, matched))
            i += len(matched)
            continue
        if c in _GO_SINGLE_OPS:
            out.append(("op", c, c))
            i += 1
            continue
        i += 1
    return out


def _go_match_brace(tokens: Sequence[tuple[str, str, str]], open_index: int) -> int | None:
    depth = 0
    for index in range(open_index, len(tokens)):
        token = tokens[index][1]
        if token == "{":
            depth += 1
        elif token == "}":
            depth -= 1
            if depth == 0:
                return index
    return None


def _go_body_open(tokens: Sequence[tuple[str, str, str]], start: int) -> int | None:
    """Index of the ``{`` that opens a statement body, honouring parentheses."""

    depth = 0
    for index in range(start, len(tokens)):
        token = tokens[index][1]
        if token in "([":
            depth += 1
        elif token in ")]":
            depth -= 1
        elif token == "{" and depth == 0:
            return index
    return None


def _go_split_top(
    tokens: Sequence[tuple[str, str, str]], separators: set[str]
) -> list[list[tuple[str, str, str]]]:
    parts: list[list[tuple[str, str, str]]] = [[]]
    depth = 0
    for token in tokens:
        text = token[1]
        if text in "([{":
            depth += 1
        elif text in ")]}":
            depth -= 1
        if depth == 0 and text in separators:
            parts.append([])
            continue
        parts[-1].append(token)
    return parts


def _go_join(tokens: Sequence[tuple[str, str, str]]) -> str:
    return "".join(token[1] for token in tokens)


def _go_is_zero(text: str) -> bool:
    try:
        return float(text) == 0.0
    except ValueError:
        return False


def _go_guard_atoms(
    condition: Sequence[tuple[str, str, str]],
) -> list[dict[str, object]]:
    """Extract the comparison atoms of a guard condition.

    ``if init; cond`` keeps only ``cond``; a compound ``a || b && c`` condition
    is split into its atomic comparisons so each ``expected value`` can be
    compared independently.  A right-hand side is a *literal expectation* only
    when it is a single literal token or ``nil``/``true``/``false``; anything
    else (a variable, a call, a computed expression) is treated as an
    indirection and never coerced into a literal expectation.
    """

    atoms: list[dict[str, object]] = []
    condition = _go_split_top(condition, {";"})[-1]
    for conjunct in _go_split_top(condition, {"||", "&&"}):
        depth = 0
        for index, token in enumerate(conjunct):
            text = token[1]
            if text in "([{":
                depth += 1
            elif text in ")]}":
                depth -= 1
            elif depth == 0 and text in _GO_COMPARISON_OPS:
                left = _go_join(conjunct[:index])
                right_tokens = conjunct[index + 1:]
                if not left or not right_tokens:
                    break
                literal = len(right_tokens) == 1 and (
                    right_tokens[0][0] in {"string", "number", "rune"}
                    or right_tokens[0][1] in {"nil", "true", "false"}
                )
                degenerate = False
                if len(right_tokens) == 1:
                    kind, raw, value = right_tokens[0]
                    if kind == "string" and value == "":
                        degenerate = True
                    elif kind == "number" and _go_is_zero(raw):
                        degenerate = True
                    elif raw == "nil":
                        degenerate = True
                atoms.append(
                    {
                        "lhs": left,
                        "op": text,
                        "rhs": _go_join(right_tokens),
                        "rhs_literal": bool(literal),
                        "degenerate": bool(degenerate),
                    }
                )
                break
    return atoms


def _go_has_assertion(tokens: Sequence[tuple[str, str, str]]) -> bool:
    for index, token in enumerate(tokens):
        if (
            token[1] == "."
            and index + 1 < len(tokens)
            and tokens[index + 1][0] == "ident"
            and tokens[index + 1][1] in _GO_FAIL_METHODS
            and index + 2 < len(tokens)
            and tokens[index + 2][1] == "("
        ):
            return True
        if (
            token[1] == "panic"
            and index + 1 < len(tokens)
            and tokens[index + 1][1] == "("
        ):
            return True
    return False


def _go_function_signature(
    body: Sequence[tuple[str, str, str]],
) -> dict[str, object]:
    guards: list[dict[str, object]] = []
    empty_guards = 0
    index = 0
    while index < len(body):
        if body[index][1] == "if":
            open_index = _go_body_open(body, index + 1)
            if open_index is not None:
                close_index = _go_match_brace(body, open_index)
                if close_index is not None:
                    atoms = _go_guard_atoms(body[index + 1:open_index])
                    inner = body[open_index + 1:close_index]
                    if atoms:
                        if _go_has_assertion(inner):
                            guards.extend(atoms)
                        elif not inner:
                            # ``if <comparison> { }`` with the assertion removed
                            # or commented out: a neutered guard.
                            empty_guards += 1
        index += 1
    return {"empty_guards": empty_guards, "guards": guards}


def go_test_signatures(text: str) -> dict[str, dict[str, object]]:
    """Map ``Test*`` function name -> semantic assertion signature."""

    tokens = _go_tokens(text)
    functions: dict[str, dict[str, object]] = {}
    index = 0
    while index < len(tokens):
        if tokens[index][1] == "func" and index + 1 < len(tokens):
            name = tokens[index + 1]
            if name[0] == "ident" and name[1].startswith("Test"):
                open_index = _go_body_open(tokens, index + 2)
                if open_index is not None:
                    close_index = _go_match_brace(tokens, open_index)
                    if close_index is not None:
                        functions[name[1]] = _go_function_signature(
                            tokens[open_index + 1:close_index]
                        )
                        index = close_index
                        continue
        index += 1
    return functions


def build_target_test_signatures(root: Path) -> dict[str, dict[str, dict[str, object]]]:
    """Authoring helper: freeze target-test assertion signatures for a tree."""

    signatures: dict[str, dict[str, dict[str, object]]] = {}
    for relative, path in sorted(_regular_files(root).items()):
        if not relative.endswith("_test.go"):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        functions = go_test_signatures(text)
        if functions:
            signatures[relative] = functions
    return signatures


def _target_test_weakening(
    root: Path,
    target_baseline: Mapping[str, Any] | None,
) -> list[dict[str, object]]:
    """Return the weakened target-test assertions, if any.

    Only test functions present in *both* the frozen reference baseline and the
    candidate repository are compared, so a candidate-authored port (which may
    keep fewer functions) is not itself a signal.  Formatting, comments and
    equivalent rewrites are invisible to the analysis.
    """

    if not isinstance(target_baseline, Mapping):
        return []
    baseline_functions = target_baseline.get("functions")
    if not isinstance(baseline_functions, Mapping):
        return []
    current = _regular_files(root)
    findings: list[dict[str, object]] = []
    for relative, baseline_funcs in baseline_functions.items():
        if not isinstance(baseline_funcs, Mapping):
            continue
        path = current.get(str(relative))
        if path is None:
            # The candidate did not ship this file: a self-authored port is not
            # tampering (mirrors the "missing baseline files are ignored" rule).
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        candidate_funcs = go_test_signatures(text)
        for name, baseline_sig in baseline_funcs.items():
            candidate_sig = candidate_funcs.get(str(name))
            if not isinstance(baseline_sig, Mapping) or not isinstance(candidate_sig, Mapping):
                continue
            if int(candidate_sig.get("empty_guards", 0)) > int(baseline_sig.get("empty_guards", 0)):
                findings.append(
                    {
                        "path": str(relative),
                        "function": str(name),
                        "reason": "assertion-guard-emptied",
                    }
                )
            baseline_guards = baseline_sig.get("guards", [])
            candidate_guards = candidate_sig.get("guards", [])
            baseline_guards = baseline_guards if isinstance(baseline_guards, list) else []
            candidate_guards = candidate_guards if isinstance(candidate_guards, list) else []
            baseline_by_lhs: dict[str, list[Mapping[str, Any]]] = {}
            candidate_by_lhs: dict[str, list[Mapping[str, Any]]] = {}
            for atom in baseline_guards:
                if isinstance(atom, Mapping):
                    baseline_by_lhs.setdefault(str(atom.get("lhs", "")), []).append(atom)
            for atom in candidate_guards:
                if isinstance(atom, Mapping):
                    candidate_by_lhs.setdefault(str(atom.get("lhs", "")), []).append(atom)
            for lhs, baseline_atoms in baseline_by_lhs.items():
                if not lhs:
                    continue
                candidate_atoms = candidate_by_lhs.get(lhs)
                if not candidate_atoms:
                    # The compared expression is gone entirely: a rewrite, not a
                    # weakening of a surviving assertion.
                    continue
                fixed = [
                    atom
                    for atom in baseline_atoms
                    if bool(atom.get("rhs_literal")) and not bool(atom.get("degenerate"))
                ]
                if not fixed:
                    continue
                if any(bool(atom.get("degenerate")) for atom in candidate_atoms):
                    findings.append(
                        {
                            "path": str(relative),
                            "function": str(name),
                            "reason": "degenerate-expectation",
                            "expression": lhs,
                        }
                    )
                    continue
                # A fixed expectation replaced by a *different* fixed value,
                # with no indirection (variable/computed expectation) anywhere on
                # this expression, is a weakened comparison rather than an
                # equivalent rewrite.
                if all(bool(atom.get("rhs_literal")) for atom in candidate_atoms):
                    expected = {(str(atom.get("op")), str(atom.get("rhs"))) for atom in fixed}
                    observed = {
                        (str(atom.get("op")), str(atom.get("rhs"))) for atom in candidate_atoms
                    }
                    if expected - observed:
                        findings.append(
                            {
                                "path": str(relative),
                                "function": str(name),
                                "reason": "replaced-expectation",
                                "expression": lhs,
                            }
                        )
    return findings


def _test_integrity(
    root: Path,
    baseline: Mapping[str, Any] | None,
    config: Mapping[str, Any],
    *,
    source: str = "source-test-baseline",
    target_baseline: Mapping[str, Any] | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    # ``tests_not_tampered`` protects the test files that already exist in the
    # base repository (the frozen *source*-test baseline).  That comparison is
    # *code-state* based (``code_sha256``): comments and formatting are
    # normalized away, so an equivalent editorial rewrite is not tampering,
    # while a changed or weakened assertion is.  Deleting a protected test file
    # or rewriting one away from its frozen code state is a tamper failure (a
    # cheating signal, ``reward = -1``).
    #
    # A reference solution is not the candidate's starting test tree. Only
    # target tests that also existed in the source baseline are protected
    # against weakening. Newly authored tests may reuse reference names and
    # local variables without becoming evaluator-owned assertions. Their
    # behavior is still covered by the independent evaluator probes, and
    # source-test integrity and new skip markers remain enforced below.
    if not isinstance(baseline, Mapping):
        inconclusive = _atom("INCONCLUSIVE", source=source)
        return inconclusive, inconclusive
    baseline_files = _merge_baseline_files(baseline)
    if not baseline_files:
        # A pass here would be vacuous: there is no test file to protect.  Keep
        # the evidence honest without turning a repository with no protected
        # source tests into a hard failure.
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

    def _tampered(record: Mapping[str, Any], path: Path) -> bool:
        expected = record.get("code_sha256")
        if isinstance(expected, str) and expected:
            return expected != _code_digest(path)
        # Baseline records without a code-state digest fall back to the raw
        # bytes so an older baseline is still enforced.
        return record.get("sha256") != _digest(path)

    changed = sorted(
        relative for relative, record in baseline_files.items()
        if relative in current
        and isinstance(record, Mapping)
        and _tampered(record, current[relative])
        and relative not in allowed
    )
    protected_target_baseline = None
    if isinstance(target_baseline, Mapping):
        functions = target_baseline.get("functions")
        if isinstance(functions, Mapping):
            protected_target_baseline = {
                **target_baseline,
                "functions": {
                    relative: signature
                    for relative, signature in functions.items()
                    if relative in baseline_files
                },
            }
    weakened = _target_test_weakening(root, protected_target_baseline)
    marker_before: Mapping[str, Any] = {}
    if isinstance(baseline.get("marker_counts"), Mapping):
        marker_before = baseline["marker_counts"]
    marker_after = _marker_counts(root)
    marker_delta = {
        key: int(marker_after.get(key, 0)) - int(marker_before.get(key, 0))
        for key in {"skip", "xfail", "disabled"}
    }
    tamper = _atom(
        "FAIL" if (changed or missing or weakened) else "PASS",
        source=source,
        evidence={"missing": missing, "changed": changed, "weakened": weakened},
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
    target_test_baseline: Mapping[str, Any] | None = None,
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
        target_baseline=target_test_baseline,
    )
    build_evidence, dependency_evidence, valid_receipts = _derive_execution_atoms(
        receipts
    )
    differential = [row for row in rows if row.get("kind") == "DIFFERENTIAL"]
    expected_p2p = (
        len(expected_tests.get("p2p", []))
        if isinstance(expected_tests.get("p2p", []), Sequence)
        else None
    )
    expected_diff = (
        len(expected_tests.get("differential", []))
        if isinstance(expected_tests.get("differential", []), Sequence)
        else None
    )
    # Behaviour equivalence: a P2P *or* a DIFFERENTIAL regression breaks the
    # "keep observable behaviour" bottom line, so both groups feed the hard gate
    # ``p2p_regression_preserved`` (reward 0, non-cheating).
    behaviour_rows = [*p2p, *differential]
    expected_behaviour = (
        None
        if expected_p2p is None and expected_diff is None
        else (expected_p2p or 0) + (expected_diff or 0)
    )
    p2p_regression = _group_status(
        behaviour_rows,
        expected=expected_behaviour,
        source="candidate-p2p-differential-observations",
        empty="NA",
    )
    # The public contract is driven by a *declared* contract observation subset
    # (the differential contract corpus), not by the full P2P set, so the two
    # gates carry independent signals instead of restating the same rows.
    contract_ids = policy.get("contract_observation_ids")
    contract_ids = (
        [str(item) for item in contract_ids]
        if isinstance(contract_ids, Sequence) and not isinstance(contract_ids, (str, bytes))
        else []
    )
    if contract_ids:
        contract_set = set(contract_ids)
        contract_rows = [row for row in rows if str(row.get("id")) in contract_set]
        public = _group_status(
            contract_rows,
            expected=len(contract_ids),
            source="declared-contract-observations",
            empty="NA",
        )
    else:
        public = _group_status(
            p2p,
            expected=expected_p2p,
            source="candidate-observations",
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
            "FAIL"
            if cheating_errors or protected_hits or marker_hits
            else "PASS",
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


__all__ = [
    "build_hardgate_evidence",
    "build_source_test_baseline",
    "build_target_test_signatures",
    "go_test_signatures",
]
