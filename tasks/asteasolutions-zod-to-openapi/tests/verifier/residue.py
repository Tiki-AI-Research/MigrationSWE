"""Language-neutral evaluation of declarative residue rules."""

from __future__ import annotations

import json
import re
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Iterable, Mapping, Sequence


_EXCLUDED_PARTS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "target",
    "dist",
    "build",
    "coverage",
}
_RULE_KINDS = {"literal", "dependency", "import", "file"}


def _safe_relative(value: str) -> bool:
    path = Path(value)
    return bool(value) and not path.is_absolute() and ".." not in path.parts and "\\" not in value


def _eligible(root: Path, path: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    return (
        path.is_file()
        and not path.is_symlink()
        and not any(part in _EXCLUDED_PARTS for part in relative.parts)
    )


def _paths(root: Path, patterns: Sequence[str]) -> Iterable[Path]:
    if not patterns:
        return ()
    collected: list[Path] = []
    for pattern in patterns:
        if not _safe_relative(pattern):
            continue
        path = root / pattern
        if any(token in pattern for token in "*?["):
            collected.extend(root.glob(pattern))
        elif path.is_dir() and not path.is_symlink():
            collected.extend(path.rglob("*"))
        else:
            collected.append(path)
    candidates = collected
    unique = {path.resolve(): path for path in candidates if _eligible(root, path)}
    return (unique[key] for key in sorted(unique, key=lambda item: item.as_posix()))


def _target_matches(value: str, target: str) -> bool:
    actual = value.casefold().strip()
    expected = target.casefold().strip()
    return actual == expected or fnmatchcase(actual, expected)


def _literal_contains(path: Path, target: str) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return target.casefold() in text.casefold()


def _dependency_contains(path: Path, target: str) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, Mapping):
        for field in (
            "dependencies",
            "devDependencies",
            "peerDependencies",
            "optionalDependencies",
            "require",
            "require-dev",
        ):
            entries = payload.get(field)
            if isinstance(entries, Mapping) and any(
                _target_matches(str(name), target) for name in entries
            ):
                return True
    dependency_lines = (
        re.compile(r"^\s*([A-Za-z0-9@._+/-]+)\s*(?:[<>=~!].*)?$"),
        re.compile(r"^\s*(?:gem|implementation|api|compileOnly|runtimeOnly)\s*[( ]\s*['\"]([^'\"]+)"),
        re.compile(r"^\s*([A-Za-z0-9_.+-]+)\s*=\s*['\"]?[0-9*^~<>=]"),
    )
    for line in text.splitlines():
        for pattern in dependency_lines:
            match = pattern.search(line)
            if match and _target_matches(match.group(1), target):
                return True
    return False


def _import_contains(path: Path, target: str) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    candidates: list[str] = []
    patterns = (
        re.compile(r"\bfrom\s+([A-Za-z0-9_./@+-]+)\s+import\b"),
        re.compile(r"\bimport\s+([A-Za-z0-9_./@+-]+)"),
        re.compile(r"\bfrom\s*['\"]([^'\"]+)['\"]"),
        re.compile(r"\brequire\s*\(\s*['\"]([^'\"]+)['\"]\s*\)"),
        re.compile(r"\brequire\s+['\"]([^'\"]+)['\"]"),
        re.compile(r"\b(?:use|using)\s+([A-Za-z0-9_.\\]+)"),
    )
    for pattern in patterns:
        candidates.extend(match.group(1) for match in pattern.finditer(text))
    return any(
        _target_matches(candidate, target)
        or _target_matches(re.split(r"[./\\]", candidate, maxsplit=1)[0], target)
        for candidate in candidates
    )


def _matches(root: Path, path: Path, *, kind: str, target: str) -> bool:
    if kind == "literal":
        return _literal_contains(path, target)
    if kind == "dependency":
        return _dependency_contains(path, target)
    if kind == "import":
        return _import_contains(path, target)
    relative = path.relative_to(root).as_posix()
    return fnmatchcase(relative.casefold(), target.casefold()) or fnmatchcase(
        path.name.casefold(), target.casefold()
    )


def scan_residue_rules(
    root: Path, rules: object
) -> list[dict[str, str]]:
    if not isinstance(rules, Sequence) or isinstance(rules, (str, bytes)):
        return []
    hits: list[dict[str, str]] = []
    for row in rules:
        if not isinstance(row, Mapping):
            continue
        identifier = row.get("id")
        kind = row.get("kind")
        target = row.get("target")
        patterns = row.get("paths", [])
        if (
            not isinstance(identifier, str)
            or not identifier
            or not isinstance(kind, str)
            or not isinstance(target, str)
            or not target
            or not isinstance(patterns, Sequence)
            or isinstance(patterns, (str, bytes))
            or not patterns
            or any(
                not isinstance(item, str) or not _safe_relative(item)
                for item in patterns
            )
        ):
            raise ValueError(f"invalid residue rule: {identifier or '<unknown>'}")
        if kind not in _RULE_KINDS:
            continue
        for path in _paths(root, tuple(patterns)):
            matched = _matches(root, path, kind=str(kind), target=target)
            if matched:
                hits.append(
                    {
                        "path": path.relative_to(root).as_posix(),
                        "token": target,
                        "kind": str(kind),
                        "rule_id": identifier,
                    }
                )
    return hits


_MECHANISM_KINDS = {
    "call",
    "token",
    "dynamic_require",
    "dynamic_import",
    "dynamic_code",
    "computed_module",
    "sink_member",
    "encoded_name",
}
# A low-level loader/binding sink is reachable even when the module is not
# imported literally (``process.binding('spawn_sync')``, ``Module._load(...)``,
# ``module.require(...)``); the member may be wrapped in a TS cast or extracted
# into a variable first, so the base and member are matched within a bounded
# window rather than as one literal token.
_SINK_MEMBER_WINDOW = 200
_ENCODED_NAME_PATTERNS = {
    "base64": re.compile(r"Buffer\s*\.\s*from\s*\([^;)]*['\"]base64['\"]"),
    "hex": re.compile(r"Buffer\s*\.\s*from\s*\([^;)]*['\"]hex['\"]"),
    "atob": re.compile(r"(?<![\w$.])atob\s*\("),
    "fromCharCode": re.compile(r"String\s*\.\s*fromCharCode\s*\("),
    "percent": re.compile(r"decodeURIComponent\s*\([^)]*%[0-9A-Fa-f]{2}"),
}

# The member key of a sink access can be any compile-time string expression, not
# just a bracketed literal: ``process['bin' + 'ding']``, ``process[bindKey]``
# with ``const bindKey = ['bin','ding'].join('')``, a template, or an escaped
# spelling.  Constant-folding every bracket key whose expression evaluates to a
# compile-time string collapses that whole family to ``base.member`` so the
# member scan below recognises it.  This mirrors the folding model the marker
# scan uses in ``evidence.py``.
_JS_STRING_LITERAL = re.compile(r"""^(['"])((?:\\.|(?!\1).)*)\1$""", re.DOTALL)
_JS_IDENTIFIER = re.compile(r"^[A-Za-z_$][\w$]*$")
_DECLARATION = re.compile(r"(?<![\w$])(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*([^;\n]+)")
_TS_CAST = re.compile(r"\bas\s+(?:const\s+)?[A-Za-z_$][\w$]*(?:\s*<[^;{}()]*>)?")
_JS_ESCAPE = re.compile(
    r"\\(?:u\{([0-9A-Fa-f]{1,6})\}|u([0-9A-Fa-f]{4})|x([0-9A-Fa-f]{2})|([0-7]{1,3})|(.))",
    re.DOTALL,
)
_JS_SIMPLE_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "b": "\b",
    "f": "\f",
    "v": "\v",
    "0": "\0",
    "\\": "\\",
    "'": "'",
    '"': '"',
    "`": "`",
}


def _unescape_js_string(body: str) -> str:
    def replace(match: "re.Match[str]") -> str:
        if match.group(1) is not None:
            return chr(int(match.group(1), 16))
        if match.group(2) is not None:
            return chr(int(match.group(2), 16))
        if match.group(3) is not None:
            return chr(int(match.group(3), 16))
        if match.group(4) is not None:
            return chr(int(match.group(4), 8))
        return _JS_SIMPLE_ESCAPES.get(match.group(5), match.group(5))

    return _JS_ESCAPE.sub(replace, body)


def _balanced_group(
    code: str, open_index: int, open_char: str = "(", close_char: str = ")"
) -> tuple[str, int]:
    depth = 0
    index = open_index
    length = len(code)
    quote = ""
    while index < length:
        character = code[index]
        if quote:
            if character == "\\":
                index += 2
                continue
            if character == quote:
                quote = ""
            index += 1
            continue
        if character in ("'", '"', "`"):
            quote = character
        elif character == open_char:
            depth += 1
        elif character == close_char:
            depth -= 1
            if depth == 0:
                return code[open_index + 1:index], index + 1
        index += 1
    return code[open_index + 1:], length


def _split_top_level_operator(expression: str, operator: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    index = 0
    length = len(expression)
    quote = ""
    start = 0
    while index < length:
        character = expression[index]
        if quote:
            if character == "\\":
                index += 2
                continue
            if character == quote:
                quote = ""
            index += 1
            continue
        if character in ("'", '"', "`"):
            quote = character
        elif character in "([{":
            depth += 1
        elif character in ")]}":
            depth -= 1
        elif depth == 0 and expression.startswith(operator, index):
            parts.append(expression[start:index])
            index += len(operator)
            start = index
            continue
        index += 1
    parts.append(expression[start:])
    return parts


def _split_top_level_arguments(arguments: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    index = 0
    length = len(arguments)
    quote = ""
    start = 0
    while index < length:
        character = arguments[index]
        if quote:
            if character == "\\":
                index += 2
                continue
            if character == quote:
                quote = ""
            index += 1
            continue
        if character in ("'", '"', "`"):
            quote = character
        elif character in "([{":
            depth += 1
        elif character in ")]}":
            depth -= 1
        elif character == "," and depth == 0:
            parts.append(arguments[start:index])
            start = index + 1
        index += 1
    tail = arguments[start:]
    if tail.strip() or parts:
        parts.append(tail)
    return parts


def _eval_template_string(body: str, known: Mapping[str, str]) -> str | None:
    result: list[str] = []
    literal: list[str] = []
    index = 0
    length = len(body)
    while index < length:
        character = body[index]
        if character == "\\" and index + 1 < length:
            literal.append(body[index:index + 2])
            index += 2
            continue
        if character == "$" and index + 1 < length and body[index + 1] == "{":
            if literal:
                result.append(_unescape_js_string("".join(literal)))
                literal = []
            inner, end = _balanced_group(body, index + 1, "{", "}")
            value = _eval_const_string(inner, known)
            if value is None:
                return None
            result.append(value)
            index = end
            continue
        literal.append(character)
        index += 1
    if literal:
        result.append(_unescape_js_string("".join(literal)))
    return "".join(result)


def _eval_const_string(expression: str, known: Mapping[str, str]) -> str | None:
    expression = expression.strip()
    if not expression:
        return None
    while expression.startswith("(") and expression.endswith(")"):
        inner, end = _balanced_group(expression, 0)
        if end != len(expression):
            break
        expression = inner.strip()
    literal = _JS_STRING_LITERAL.match(expression)
    if literal:
        return _unescape_js_string(literal.group(2))
    if expression.startswith("`") and expression.endswith("`"):
        return _eval_template_string(expression[1:-1], known)
    parts = _split_top_level_operator(expression, "+")
    if len(parts) > 1:
        values = [_eval_const_string(part, known) for part in parts]
        if any(value is None for value in values):
            return None
        return "".join(value for value in values if value is not None)
    array_join = re.match(r"^\[(.*)\]\s*\.\s*join\s*\((.*)\)$", expression, re.DOTALL)
    if array_join:
        elements = _split_top_level_arguments(array_join.group(1))
        values = [_eval_const_string(element, known) for element in elements]
        separator = _eval_const_string(array_join.group(2), known)
        if separator is not None and all(value is not None for value in values):
            return separator.join(value for value in values if value is not None)
        return None
    if _JS_IDENTIFIER.match(expression):
        return known.get(expression)
    return None


def _build_const_string_map(code: str) -> dict[str, str]:
    known: dict[str, str] = {}
    for _ in range(3):
        changed = False
        for match in _DECLARATION.finditer(code):
            name, expression = match.group(1), match.group(2)
            if name in known:
                continue
            value = _eval_const_string(expression, known)
            if value is not None:
                known[name] = value
                changed = True
        if not changed:
            break
    return known


def _strip_ts_casts(code: str) -> str:
    previous = None
    while previous != code:
        previous = code
        code = _TS_CAST.sub(" ", code)
    return code


def _fold_bracket_properties(code: str) -> str:
    known = _build_const_string_map(code)
    result: list[str] = []
    index = 0
    length = len(code)
    while index < length:
        character = code[index]
        if character == "[":
            probe = index - 1
            while probe >= 0 and code[probe] in " \t":
                probe -= 1
            previous = code[probe] if probe >= 0 else ""
            if previous and (previous.isalnum() or previous in "_$])."):
                inner, end = _balanced_group(code, index, "[", "]")
                value = _eval_const_string(inner, known)
                if value is not None and _JS_IDENTIFIER.match(value):
                    result.append("." + value)
                    index = end
                    continue
        result.append(character)
        index += 1
    return "".join(result)


def _normalize_member_access(text: str) -> str:
    code = _strip_js_comments(text)
    code = _strip_ts_casts(code)
    return _fold_bracket_properties(code)


def _strip_js_comments(text: str) -> str:
    """Blank JS/TS comments while preserving string literal contents.

    Mechanism detection must see the string fragments a candidate assembles
    (``['child','process'].join('_')``) while ignoring prose in comments.
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
                for _ in range(2):
                    if index < length:
                        characters[index] = " "
                        index += 1
                continue
            if character in ("'", '"', "`"):
                state = "string"
                quote = character
            index += 1
            continue
        if character == "\\":
            index += 2
            continue
        if character == quote:
            state = "normal"
        index += 1
    return "".join(characters)


def _balanced_arguments(code: str, open_index: int) -> str:
    depth = 0
    index = open_index
    length = len(code)
    quote = ""
    while index < length:
        character = code[index]
        if quote:
            if character == "\\":
                index += 2
                continue
            if character == quote:
                quote = ""
            index += 1
            continue
        if character in ("'", '"', "`"):
            quote = character
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return code[open_index + 1:index]
        index += 1
    return code[open_index + 1:]


def _dynamic_module_load(code: str, name: str) -> bool:
    """A module load is dynamic unless its argument is a single literal."""

    pattern = re.compile(r"(?<![\w$.])" + re.escape(name) + r"\s*\(")
    for match in pattern.finditer(code):
        open_index = code.index("(", match.start())
        argument = _balanced_arguments(code, open_index).strip()
        if re.fullmatch(r"['\"][^'\"]*['\"]", argument) or re.fullmatch(r"`[^`]*`", argument):
            continue
        return True
    return False


def _mechanism_matches(code: str, kind: str, target: str) -> bool:
    escaped = re.escape(target)
    if kind == "call":
        return re.search(r"(?<![\w$])" + escaped + r"\s*\(", code) is not None
    if kind == "token":
        return re.search(r"(?<![\w$])" + escaped + r"(?![\w$])", code) is not None
    if kind == "dynamic_require":
        return _dynamic_module_load(code, "require")
    if kind == "dynamic_import":
        return _dynamic_module_load(code, "import")
    if kind == "dynamic_code":
        if target == "Function":
            return re.search(r"(?<![\w$.])new\s+Function\s*\(", code) is not None
        return re.search(r"(?<![\w$.])" + escaped + r"\s*\(", code) is not None
    if kind == "computed_module":
        fragments = [part for part in target.split("_") if part]
        if len(fragments) < 2:
            return False
        return all(
            re.search(r"['\"`]" + re.escape(part) + r"['\"`]", code)
            for part in fragments
        )
    if kind == "sink_member":
        if "." not in target:
            return False
        base, member = target.split(".", 1)
        pattern = re.compile(
            r"(?<![\w$])" + re.escape(base) + r"\b[\s\S]{0," + str(_SINK_MEMBER_WINDOW)
            + r"}?\.\s*" + re.escape(member) + r"\b"
        )
        return pattern.search(code) is not None
    if kind == "encoded_name":
        matcher = _ENCODED_NAME_PATTERNS.get(target)
        return matcher.search(code) is not None if matcher else False
    return False


def scan_deceptive_mechanisms(root: Path, mechanisms: object) -> list[dict[str, str]]:
    """Detect source-runtime bridges by mechanism, not by literal spelling.

    The literal rule scan only matches contiguous tokens, so a candidate can
    reach the source runtime by assembling the module name at run time
    (``['child','process'].join('_')`` + ``process.getBuiltinModule``), through a
    dynamic ``require``/``import``, or through ``createRequire``.  This scan
    recognises those sources and sinks so the whole family is covered.
    """

    if not isinstance(mechanisms, Sequence) or isinstance(mechanisms, (str, bytes)):
        return []
    hits: list[dict[str, str]] = []
    cache: dict[Path, str] = {}
    for row in mechanisms:
        if not isinstance(row, Mapping):
            continue
        identifier = row.get("id")
        kind = row.get("kind")
        target = row.get("target")
        patterns = row.get("paths", ["src"])
        if (
            not isinstance(identifier, str)
            or not identifier
            or not isinstance(kind, str)
            or kind not in _MECHANISM_KINDS
            or not isinstance(target, str)
            or not target
            or not isinstance(patterns, Sequence)
            or isinstance(patterns, (str, bytes))
            or not patterns
            or any(
                not isinstance(item, str) or not _safe_relative(item)
                for item in patterns
            )
        ):
            raise ValueError(f"invalid deceptive mechanism: {identifier or '<unknown>'}")
        for path in _paths(root, tuple(patterns)):
            text = cache.get(path)
            if text is None:
                try:
                    text = path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    text = ""
                cache[path] = text
            code = _normalize_member_access(text)
            if _mechanism_matches(code, kind, target):
                hits.append(
                    {
                        "path": path.relative_to(root).as_posix(),
                        "token": target,
                        "kind": kind,
                        "rule_id": identifier,
                    }
                )
    return hits

def scan_deceptive_rules(
    root: Path,
    rules: object,
    baseline_root: Path | None = None,
    mechanisms: object = None,
) -> list[dict[str, str]]:
    """Scan deceptive rules while ignoring tokens the pristine baseline ships.

    A ``no_deceptive_source_fallback`` hit must be *introduced* by the candidate
    (a hidden FFI / subprocess / bridge dependency that the task baseline did not
    have).  A token that the task baseline already contains is ordinary
    pre-migration code the candidate merely left untouched: that is an incomplete
    migration (scored by the behaviour/scope dimensions), never an L2 cheat.  The
    previous rule set matched a literal from ``src/zod-extensions.ts`` that ships
    in the pristine baseline, so every candidate that did not rewrite that file
    was graded as cheating.

    Filtering by ``(path, kind, token, rule_id)`` keeps the whole family of
    "rule matches baseline content" mistakes inert without weakening the scan for
    genuinely introduced tokens (which are absent from the baseline and therefore
    still reported).
    """

    hits = scan_residue_rules(root, rules) + scan_deceptive_mechanisms(root, mechanisms)
    if baseline_root is None:
        return hits
    baseline = {
        (hit["path"], hit["kind"], hit["token"], hit["rule_id"])
        for hit in scan_residue_rules(baseline_root, rules)
    }
    baseline |= {
        (hit["path"], hit["kind"], hit["token"], hit["rule_id"])
        for hit in scan_deceptive_mechanisms(baseline_root, mechanisms)
    }
    return [
        hit
        for hit in hits
        if (hit["path"], hit["kind"], hit["token"], hit["rule_id"]) not in baseline
    ]


def classify_residue_rules(
    root: Path, rules: object
) -> list[dict[str, object]]:
    """Classify residue rules without treating unsupported syntax as a failure."""

    if not isinstance(rules, Sequence) or isinstance(rules, (str, bytes)):
        return []
    diagnostics: list[dict[str, object]] = []
    for row in rules:
        if not isinstance(row, Mapping):
            continue
        identifier = row.get("id")
        kind = row.get("kind")
        if kind not in _RULE_KINDS:
            diagnostics.append(
                {
                    "rule_id": str(identifier or "<unknown>"),
                    "kind": str(kind or ""),
                    "status": "UNSUPPORTED",
                    "hits": [],
                    "reason": "residue rule syntax is not supported by the generic scanner",
                }
            )
            continue
        hits = scan_residue_rules(root, [row])
        diagnostics.append(
            {
                "rule_id": str(identifier),
                "kind": str(kind),
                "status": "FAIL" if hits else "PASS",
                "hits": hits,
            }
        )
    return diagnostics


__all__ = [
    "classify_residue_rules",
    "scan_deceptive_mechanisms",
    "scan_deceptive_rules",
    "scan_residue_rules",
]
