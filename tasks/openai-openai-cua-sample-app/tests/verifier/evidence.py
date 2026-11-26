"""Evidence derivation for the fixed Migration Grader hardgate contract."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from .residue import (
        _deceptive_targets,
        classify_residue_rules,
        scan_residue_rules,
        scan_source_runtime_tree,
        strip_comments,
    )
except ImportError:  # Standalone Runtime Draft layout.
    from residue import (
        _deceptive_targets,
        classify_residue_rules,
        scan_residue_rules,
        scan_source_runtime_tree,
        strip_comments,
    )


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


_ASSERTION_CALL = re.compile(r"\b(?:expect|assert)\s*\(")
_ASSERTION_CHAIN = re.compile(r"\s*\.\s*[A-Za-z_$][A-Za-z0-9_$]*\s*")


def _balanced_end(text: str, open_index: int) -> int:
    depth = 0
    index = open_index
    length = len(text)
    while index < length:
        character = text[index]
        if character in "([{":
            depth += 1
        elif character in ")]}":
            depth -= 1
            if depth == 0:
                return index + 1
        elif character in "'\"`":
            quote = character
            index += 1
            while index < length and text[index] != quote:
                if text[index] == "\\":
                    index += 1
                index += 1
        index += 1
    return length


def _request_adapters(text: str) -> tuple[str, ...]:
    """Recognise test-only HTTP adapters without depending on helper names.

    Only adapters that forward the caller's URL/method/body to a real HTTP
    transport and decode its response qualify. Assertions still retain their
    full request, subject, matcher, expected value, and multiplicity.
    """
    transports = {"fetch"}
    for match in re.finditer(r'import\s*\{([^}]+)\}\s*from\s*[\'"]node:https?[\'"]', text):
        for entry in match[1].split(","):
            imported = re.fullmatch(r"\s*request(?:\s+as\s+(\w+))?\s*", entry)
            if imported:
                transports.add(imported[1] or "request")
    adapters = []
    for match in re.finditer(r"\basync\s+function\s+(\w+)\s*\(", text):
        end = _balanced_end(text, match.end() - 1)
        parameters = text[match.end():end - 1]
        names = re.match(r"\s*(\w+)\s*:[\s\S]*?,\s*(\w+)\s*:", parameters)
        if names is None:
            continue
        # Skip the optional TypeScript return annotation (including generics).
        index, depth = end, 0
        while index < len(text):
            char = text[index]
            if char == "<":
                depth += 1
            elif char == ">":
                depth = max(0, depth - 1)
            elif char == "{" and depth == 0:
                break
            index += 1
        body = text[index:_balanced_end(text, index)]
        options = re.escape(names[2])
        if (all(re.search(rf"\b{options}\s*\.\s*{field}\b", body)
                for field in ("url", "method", "payload"))
                and any(re.search(rf"\b{re.escape(name)}\s*\(", body) for name in transports)
                and re.search(r"\bJSON\s*\.\s*parse\s*\(", body)
                and re.search(r"\.\s*status(?:Code)?\b", body)
                and re.search(rf"\b{re.escape(names[1])}\b", body)):
            adapters.append(match[1])
    return tuple(adapters)


def _assertion_units(text: str, *, compact: bool = True) -> tuple[str, ...]:
    """Normalised assertion expressions (``expect(...)``/``assert(...)`` chains).

    Comment/whitespace-insensitive, so reformatting does not change the
    fingerprint; the string/regex arguments inside assertions are preserved.
    """

    text = strip_comments(text)
    adapters = _request_adapters(text)
    units: list[str] = []
    for match in _ASSERTION_CALL.finditer(text):
        start = match.start()
        end = _balanced_end(text, match.end() - 1)
        while True:
            chain = _ASSERTION_CHAIN.match(text, end)
            if chain is None:
                break
            if chain.end() < len(text) and text[chain.end()] == "(":
                end = _balanced_end(text, chain.end())
            else:
                end = chain.end()
        unit = text[start:end]
        for adapter in adapters:
            unit = re.sub(
                rf"\b{re.escape(adapter)}\s*\(\s*([A-Za-z_$][\w$]*)\s*,\s*(?=\{{)",
                r"\1.inject(", unit,
            )
        units.append("".join(unit.split()) if compact else unit)
    return tuple(units)


def _split_arguments(text: str) -> list[str]:
    """Split TS call/object arguments without splitting nested expressions."""
    parts, start, index = [], 0, 0
    while index < len(text):
        char = text[index]
        if char in "([{":
            index = _balanced_end(text, index)
            continue
        if char in "\"'`":
            quote = char
            index += 1
            while index < len(text) and text[index] != quote:
                if text[index] == "\\":
                    index += 1
                index += 1
        elif char == ",":
            parts.append(text[start:index].strip())
            start = index + 1
        index += 1
    if text[start:].strip():
        parts.append(text[start:].strip())
    return parts


def _semantic_test_assertions(text: str) -> Counter:
    """Compare assertion behavior across real HTTP harness adaptations.

    Keep test identity, request inputs, matchers, values and multiplicity. Only
    inline bindings derived from HTTP responses; arbitrary local bindings are
    never treated as equivalent. This supplements, rather than replaces, the
    exact assertion check and the independently executed native/public tests.
    """
    text = strip_comments(text)
    tests = list(re.finditer(r'\bit(?:\.each\s*\([^\n]*?\))?\s*\(\s*([\'"])(.*?)\1\s*,', text))
    preamble = text[:tests[0].start()] if tests else text
    transports = {"fetch", "request"}
    for match in re.finditer(r'import\s*\{([^}]+)\}\s*from\s*[\'"]node:https?[\'"]', preamble):
        for entry in match[1].split(","):
            imported = re.fullmatch(r"\s*request(?:\s+as\s+(\w+))?\s*", entry)
            if imported:
                transports.add(imported[1] or "request")
    real_adapter = (
        any(re.search(rf'\b{re.escape(name)}\s*\(', preamble) for name in transports)
        and re.search(r'\.(?:url|path)\b|\$\{path\}', preamble)
        and re.search(r'\.method\b', preamble)
        and re.search(r'\.(?:payload|body)\b', preamble)
        and re.search(r'JSON\s*\.\s*parse|\.json\s*\(', preamble)
    )
    real_adapter = real_adapter or bool(re.search(
        r'request:\s*\(\s*(\w+)\s*,\s*(\w+)\s*\)\s*=>\s*fetch\(`\$\{\w+\}\$\{\1\}`,\s*\2\)', preamble
    ))
    json_helpers = set(re.findall(r'async\s+function\s+(\w+)\s*\(\s*\w+\s*:\s*Response\s*\)\s*\{\s*return\s*\(?\s*await\s+\w+\.json\(', preamble))
    json_request = bool(re.search(r'function\s+jsonRequest\b[\s\S]*?body:\s*JSON\.stringify\(payload\)', preamble))
    result = Counter()
    for number, test in enumerate(tests):
        body = text[test.end():tests[number + 1].start() if number + 1 < len(tests) else len(text)]
        bindings = {}

        def normalize(value: str, depth: int = 0) -> str:
            if depth > 12:
                return value
            value = re.sub(r'\.json\s*<[^<>]*>', '.json', value)
            value = re.sub(r'\s+as\s+(?:string|number|boolean)\b', '', value)
            if real_adapter:
                value = re.sub(r'\bfetch\(`\$\{[\w.]+\}([^`]+)`', r'__runner.request(`\1`', value)
            # Replace complete request calls first, retaining every request input.
            request_pattern = r'\b\w+\.(inject' + ('|request' if real_adapter else '') + r')\s*\('
            cursor = 0
            while (call := re.search(request_pattern, value[cursor:])) is not None:
                start = cursor + call.start()
                opening = cursor + call.end() - 1
                end = _balanced_end(value, opening)
                args = _split_arguments(value[opening + 1:end - 1])
                fields = {}
                if args and args[0].startswith('{'):
                    options = args[0]
                elif args:
                    fields['url'] = args[0]
                    options = args[1] if len(args) > 1 else '{}'
                else:
                    options = '{}'
                if json_request and options.startswith('jsonRequest('):
                    request_args = _split_arguments(options[len('jsonRequest('):-1])
                    if len(request_args) == 2:
                        fields.update(method=request_args[0], payload=request_args[1])
                        options = '{}'
                if not options.startswith('{'):
                    cursor = end
                    continue
                for entry in _split_arguments(options[1:-1]):
                    pair = re.match(r'([\w\'"-]+)\s*:\s*([\s\S]*)', entry)
                    if pair:
                        key = pair[1].strip('\'"')
                        fields[{'path': 'url', 'body': 'payload'}.get(key, key)] = pair[2]
                    else:
                        fields[entry] = entry
                fields.setdefault('method', '"GET"')
                fields.setdefault('headers', '{}')
                payload = fields.get('payload', '').strip()
                if payload.startswith('JSON.stringify(') and payload.endswith(')'):
                    payload = payload[len('JSON.stringify('):-1].strip()
                    fields['payload'] = payload
                if payload.startswith('{'):
                    fields['headers'] = re.sub(r',?\s*[\'"]content-type[\'"]\s*:\s*[\'"]application/json[\'"]\s*,?', '', fields['headers'])
                target = fields.get('url', '')
                if target.startswith('`') and '${' not in target:
                    fields['url'] = '"' + target[1:-1] + '"'
                encoded = 'HTTP(' + repr(sorted((k, re.sub(r',\s*([}\]])', r'\1', ''.join(v.split()))) for k, v in fields.items())) + ')'
                value = value[:start] + encoded + value[end:]
                cursor = start + len(encoded)
            for name, binding in sorted(bindings.items(), key=lambda row: -len(row[0])):
                value = re.sub(rf'(?<![\w$.]){re.escape(name)}\b', lambda _: binding, value)
            for helper in json_helpers:
                pattern = rf'\b{re.escape(helper)}\s*\('
                while (call := re.search(pattern, value)) is not None:
                    opening = call.end() - 1
                    end = _balanced_end(value, opening)
                    value = value[:call.start()] + value[opening + 1:end - 1] + '.json()' + value[end:]
            value = re.sub(r'\bawait\s+', '', value)
            # Native fetch response access has the same observable projection.
            value = re.sub(r'\.headers\.get\(([^()]*)\)', r'.headers[\1]', value)
            value = re.sub(r'(?<!\.run)\.status(?=\))', '.statusCode', value).replace('.text()', '.body')
            for call in list(re.finditer(r'HTTP\(', value))[::-1]:
                end = _balanced_end(value, call.end() - 1)
                atom = value[call.start():end]
                value = re.sub(r'(?<![\w$.])\(' + re.escape(atom) + r'\)', lambda _: atom, value)
                value = value.replace('JSON.parse(' + atom + '.body)', atom + '.json()')
                value = value.replace('startRunResponseSchema.parse(' + atom + '.json()).runId', atom + '.json().runId')
                for projection in ('.json()', '.body'):
                    value = re.sub(r'(?<![\w$.])\(' + re.escape(atom + projection) + r'\)', lambda _: atom + projection, value)
            if '.headers[' in value:
                value = value.replace('.toBeNull()', '.toBeUndefined()')
            return ''.join(value.split())

        # Bindings are collected in source order. A declaration contributes only
        # when its initializer is derived from an actual request/response.
        declarations = list(re.finditer(r'\bconst\s+(\w+)\s*=\s*', body))
        assertions = []
        for match in _ASSERTION_CALL.finditer(body):
            end = _balanced_end(body, match.end() - 1)
            while (chain := _ASSERTION_CHAIN.match(body, end)) is not None:
                end = _balanced_end(body, chain.end()) if chain.end() < len(body) and body[chain.end()] == '(' else chain.end()
            assertions.append((match.start(), body[match.start():end]))
        declaration_index = 0
        def bind(declaration):
            start = declaration.end()
            index = start
            while index < len(body) and body[index] != ';':
                if body[index] in '([{':
                    index = _balanced_end(body, index)
                else:
                    index += 1
            normalized = normalize(body[start:index])
            if 'HTTP(' in normalized:
                bindings[declaration[1]] = normalized
        for position, assertion in assertions:
            while declaration_index < len(declarations) and declarations[declaration_index].start() < position:
                bind(declarations[declaration_index])
                declaration_index += 1
            normalized = normalize(assertion)
            result[(test[2], normalized)] += 1
    return result


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
    changed = sorted(
        relative for relative, record in baseline_files.items()
        if relative in current
        and isinstance(record, Mapping)
        and record.get("sha256") != _digest(current[relative])
        and relative not in allowed
    )
    # In-scope tests (``allowed_equivalent_test_paths``) may legitimately adapt
    # their harness, so their digest is not pinned.  Their *behavioral
    # assertions* are still protected: every baseline assertion expression must
    # survive in the candidate, otherwise the assertion was removed/rewritten/
    # weakened.  Added assertions and harness changes are allowed.  Weakening an
    # in-scope (candidate-owned) test is a *non-cheating* test-strength
    # regression (score 0), not tampering with evaluator-owned tests.
    assertion_weakened: list[str] = []
    for relative in sorted(set(baseline_files) & set(current) & allowed):
        record = baseline_files[relative]
        expected = record.get("assertions") if isinstance(record, Mapping) else None
        if not expected:
            continue
        try:
            candidate_text = current[relative].read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        present = Counter(_assertion_units(candidate_text))
        exact_missing = any(
            present.get(str(unit), 0) < count
            for unit, count in Counter(str(item) for item in expected).items()
        )
        source_text = record.get("source") if isinstance(record, Mapping) else None
        equivalent = False
        if exact_missing and isinstance(source_text, str):
            required = _semantic_test_assertions(source_text)
            actual = _semantic_test_assertions(candidate_text)
            equivalent = bool(required) and not (required - actual)
        if exact_missing and not equivalent:
            assertion_weakened.append(relative)
    marker_before = baseline.get("marker_counts", {})
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
    in_scope = _atom(
        "FAIL" if assertion_weakened else "PASS",
        source="in-scope-test-strength",
        evidence={"weakened": assertion_weakened},
    )
    skip = _atom(
        "FAIL" if any(value > 0 for value in marker_delta.values()) else "PASS",
        source=source,
        evidence={"before": dict(marker_before), "after": marker_after, "delta": marker_delta},
    )
    return tamper, in_scope, skip


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
    # Relocating a source-runtime load into an unlisted file/extension must not
    # evade detection, so the whole ``javascript-app/`` tree is also scanned.
    deceptive_hits = [
        *deceptive_hits,
        *scan_source_runtime_tree(candidate_root, _deceptive_targets(config)),
    ]
    protected = policy.get("protected_paths", config.get("protected_paths", []))
    protected = protected if isinstance(protected, Sequence) and not isinstance(protected, (str, bytes)) else []
    marker_names = policy.get("forbidden_report_markers", [])
    marker_names = marker_names if isinstance(marker_names, Sequence) and not isinstance(marker_names, (str, bytes)) else []
    protected_hits = _protected_hits(candidate_root, [str(item) for item in protected])
    marker_hits = _report_marker_hits(candidate_root, [str(item) for item in marker_names])
    tests_not_tampered, in_scope_test_strength, no_new_skip = _test_integrity(
        candidate_root, source_test_baseline, config, source=test_baseline_source
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
        "in_scope_test_strength": in_scope_test_strength,
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
