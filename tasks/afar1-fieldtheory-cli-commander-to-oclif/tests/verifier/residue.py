"""Language-neutral evaluation of declarative residue rules."""

from __future__ import annotations

import json
import posixpath
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


_JS_SUFFIXES = frozenset(
    {".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".mts", ".cts"}
)
_JS_TOKEN = re.compile(
    r"(?P<space>\s+)"
    r"|(?P<line>//[^\n]*)"
    r"|(?P<block>/\*.*?\*/)"
    r"|(?P<str>'(?:\\.|[^'\\\n])*'|\"(?:\\.|[^\"\\\n])*\")"
    r"|(?P<tpl>`(?:\\.|[^`\\])*`)"
    r"|(?P<id>[A-Za-z_$][A-Za-z0-9_$]*)"
    r"|(?P<num>\d+(?:\.\d+)?)"
    r"|(?P<punct>[()[\]{}.,;:=+\-*/%<>!&|?~^])",
    re.S,
)
_REQUIRE_FUNCTION = "__createRequire__"
_MAX_JS_TOKENS = 200000


def _is_js(path: Path) -> bool:
    return path.suffix.casefold() in _JS_SUFFIXES


def _blocked_mask(text: str) -> list[bool]:
    """Mark offsets that live inside comments or string bodies.

    A literal rule must only see executable source: a migration note such as
    ``// previous code was from 'commander'`` is prose, not a module load, and
    must never trip the L2 bridge gate.  Delimiters stay visible so a real
    ``from 'commander'`` statement (matched from its ``from`` keyword) is still
    found, and template interpolations (``${...}``) stay visible because they
    are executable code.

    The mask is derived from the same tokenizer the module-load analysis uses,
    so a regex such as ``/'/g`` inside a template can never desynchronize it
    and hide a later, real call site.
    """

    blocked = [False] * len(text)
    for match in _JS_TOKEN.finditer(text):
        kind = match.lastgroup or ""
        start, end = match.span()
        if kind in {"line", "block"}:
            for offset in range(start, end):
                blocked[offset] = True
            continue
        if kind == "str":
            for offset in range(start + 1, max(start + 1, end - 1)):
                blocked[offset] = True
            continue
        if kind != "tpl":
            continue
        cursor = start + 1
        stop = max(start + 1, end - 1)
        while cursor < stop:
            character = text[cursor]
            if character == "\\":
                blocked[cursor] = True
                if cursor + 1 < stop:
                    blocked[cursor + 1] = True
                cursor += 2
                continue
            if character == "$" and cursor + 1 < stop and text[cursor + 1] == "{":
                depth = 0
                inner = cursor + 1
                while inner < stop:
                    if text[inner] == "{":
                        depth += 1
                    elif text[inner] == "}":
                        depth -= 1
                        if depth == 0:
                            break
                    inner += 1
                nested = _blocked_mask(text[cursor + 2 : inner])
                for offset, value in enumerate(nested):
                    if value:
                        blocked[cursor + 2 + offset] = True
                cursor = inner + 1
                continue
            blocked[cursor] = True
            cursor += 1
    return blocked


def _literal_contains(path: Path, target: str) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    if not _is_js(path):
        return target.casefold() in text.casefold()
    blocked = _blocked_mask(text)
    lowered = text.casefold()
    needle = target.casefold()
    start = 0
    while True:
        found = lowered.find(needle, start)
        if found < 0:
            return False
        if not blocked[found]:
            return True
        start = found + 1


def _unquote(raw: str) -> str:
    body = raw[1:-1]
    return re.sub(
        r"\\(.)",
        lambda match: {"n": "\n", "t": "\t", "r": "\r"}.get(
            match.group(1), match.group(1)
        ),
        body,
    )


def _unquote_template(
    raw: str,
    environment: Mapping[str, object],
    depth: int,
) -> str | None:
    """Fold a template literal whose interpolations are known constants."""

    body = raw[1:-1]
    pieces: list[str] = []
    index = 0
    while index < len(body):
        character = body[index]
        if character == "\\" and index + 1 < len(body):
            pieces.append(body[index + 1])
            index += 2
            continue
        if character == "$" and index + 1 < len(body) and body[index + 1] == "{":
            closing = body.find("}", index + 2)
            if closing < 0:
                return None
            inner = body[index + 2 : closing]
            evaluated, _ = _eval_constant(
                _js_tokens(inner), 0, environment, depth=depth + 1
            )
            if not isinstance(evaluated, str):
                return None
            pieces.append(evaluated)
            index = closing + 1
            continue
        pieces.append(character)
        index += 1
    return "".join(pieces)


def _js_tokens(text: str) -> list[tuple[str, str, int, int]]:
    tokens: list[tuple[str, str, int, int]] = []
    for match in _JS_TOKEN.finditer(text):
        kind = match.lastgroup or ""
        if kind in {"space", "line", "block"}:
            continue
        tokens.append((kind, match.group(0), match.start(), match.end()))
        if len(tokens) >= _MAX_JS_TOKENS:
            break
    return tokens


def _skip_balanced(
    tokens: Sequence[tuple[str, str, int, int]], index: int
) -> tuple[int, int]:
    """Return the indices of the opening and closing bracket at ``index``."""

    if index >= len(tokens) or tokens[index][1] != "(":
        return index, index
    depth = 0
    for cursor in range(index, len(tokens)):
        value = tokens[cursor][1]
        if value == "(":
            depth += 1
        elif value == ")":
            depth -= 1
            if depth == 0:
                return index, cursor
    return index, len(tokens) - 1


def _eval_constant(
    tokens: Sequence[tuple[str, str, int, int]],
    index: int,
    environment: Mapping[str, object],
    *,
    depth: int = 0,
) -> tuple[object, int]:
    """Evaluate the limited constant expressions a module specifier can use.

    Supports string/template literals, ``+`` concatenation, arrays and their
    ``join``/``concat`` methods, parenthesised expressions and local bindings.
    Anything else folds to ``None`` (an unresolvable dynamic load), which the
    callers treat as computed rather than as absent.
    """

    if depth > 8 or index >= len(tokens):
        return None, min(index, len(tokens))
    kind, raw = tokens[index][0], tokens[index][1]
    value: object = None
    cursor = index + 1
    if kind == "str":
        value = _unquote(raw)
    elif kind == "tpl":
        value = _unquote_template(raw, environment, depth)
    elif kind == "num":
        value = None
    elif raw == "(":
        value, cursor = _eval_constant(tokens, index + 1, environment, depth=depth + 1)
        if cursor < len(tokens) and tokens[cursor][1] == ")":
            cursor += 1
    elif raw == "[":
        items: list[str] = []
        cursor = index + 1
        while cursor < len(tokens) and tokens[cursor][1] != "]":
            item, cursor = _eval_constant(tokens, cursor, environment, depth=depth + 1)
            if not isinstance(item, str):
                items = []
                break
            items.append(item)
            if cursor < len(tokens) and tokens[cursor][1] == ",":
                cursor += 1
        if cursor < len(tokens) and tokens[cursor][1] == "]":
            cursor += 1
        value = items or None
    elif kind == "id":
        if raw in {"createRequire", "createRequireFromPath"}:
            if cursor < len(tokens) and tokens[cursor][1] == "(":
                _, closing = _skip_balanced(tokens, cursor)
                value = _REQUIRE_FUNCTION
                cursor = closing + 1
        elif raw in environment:
            value = environment[raw]
        elif raw == "require":
            value = _REQUIRE_FUNCTION

    # Postfix and binary operators over the primary expression.
    while cursor < len(tokens):
        token = tokens[cursor][1]
        if token == "(":
            # A call to an unknown callee has no statically known value; only
            # the recognised ``createRequire(...)`` primary is folded.
            _, closing = _skip_balanced(tokens, cursor)
            value = None
            cursor = closing + 1
            break
        if token == "." and cursor + 2 < len(tokens):
            member = tokens[cursor + 1][1]
            if tokens[cursor + 2][1] == "(":
                _, closing = _skip_balanced(tokens, cursor + 2)
                argument, _ = _eval_constant(
                    tokens, cursor + 3, environment, depth=depth + 1
                )
                if member == "join" and isinstance(value, list):
                    separator = argument if isinstance(argument, str) else ","
                    value = separator.join(value)
                    cursor = closing + 1
                    continue
                if member == "concat" and isinstance(value, list):
                    tail = argument if isinstance(argument, list) else []
                    value = value + tail
                    cursor = closing + 1
                    continue
            value = None
            cursor += 1
            break
        if token == "+":
            right, cursor_after = _eval_constant(
                tokens, cursor + 1, environment, depth=depth + 1
            )
            if isinstance(value, str) and isinstance(right, str):
                value = value + right
                cursor = cursor_after
                continue
            value = None
            cursor = cursor_after
            break
        break
    return value, cursor


def _collect_bindings(
    tokens: Sequence[tuple[str, str, int, int]], environment: dict[str, object]
) -> None:
    for index in range(len(tokens) - 3):
        if tokens[index][0] != "id" or tokens[index][1] not in {"const", "let", "var"}:
            continue
        if tokens[index + 1][0] != "id" or tokens[index + 2][1] != "=":
            continue
        name = tokens[index + 1][1]
        value, _ = _eval_constant(tokens, index + 3, environment)
        if value is not None:
            environment[name] = value
        _collect_object_members(tokens, index + 3, name, environment)


def _collect_object_members(
    tokens: Sequence[tuple[str, str, int, int]],
    index: int,
    name: str,
    environment: dict[str, object],
) -> None:
    """Flatten ``{ load: require }`` so ``holder.load(...)`` stays traceable."""

    if index >= len(tokens) or tokens[index][1] != "{":
        return
    cursor = index + 1
    while cursor + 2 < len(tokens):
        if tokens[cursor][1] == "}":
            return
        if tokens[cursor][0] in {"id", "str"} and tokens[cursor + 1][1] == ":":
            member = tokens[cursor][1].strip("'\"")
            value, after = _eval_constant(tokens, cursor + 2, environment)
            if value is not None:
                environment[f"{name}.{member}"] = value
            cursor = max(after, cursor + 3)
            if cursor < len(tokens) and tokens[cursor][1] == ",":
                cursor += 1
            continue
        return


def _module_loads(text: str) -> list[dict[str, object]]:
    """Resolve module specifiers, folding aliases and constant expressions.

    ``const legacyRequire = createRequire(import.meta.url)`` followed by
    ``legacyRequire('../vendor/' + ['com', 'mander'].join(''))`` resolves to
    ``../vendor/commander``: the alias is tracked and the concatenation is
    folded, so a renamed or split specifier cannot hide a source-runtime load.
    """

    tokens = _js_tokens(text)
    environment: dict[str, object] = {}
    for _ in range(3):
        _collect_bindings(tokens, environment)
    require_names = {
        name for name, value in environment.items() if value == _REQUIRE_FUNCTION
    }
    require_names.add("require")
    loads: list[dict[str, object]] = []
    for index, token in enumerate(tokens):
        kind, raw, start, _ = token
        if kind != "id":
            continue
        previous = tokens[index - 1][1] if index > 0 else ""
        cursor = index + 1
        callee = raw
        if (
            cursor + 2 < len(tokens)
            and tokens[cursor][1] == "."
            and tokens[cursor + 1][0] == "id"
            and f"{raw}.{tokens[cursor + 1][1]}" in require_names
        ):
            callee = f"{raw}.{tokens[cursor + 1][1]}"
            cursor += 2
        if callee in require_names and previous != ".":
            alias = callee != "require"
            if cursor < len(tokens) and tokens[cursor][1] == "(":
                value, after = _eval_constant(tokens, cursor + 1, environment)
                _, closing = _skip_balanced(tokens, cursor)
                loads.append(
                    {
                        "specifier": value if isinstance(value, str) else None,
                        "computed": not isinstance(value, str),
                        "alias": alias,
                        "start": start,
                        "expression": _expression_text(text, tokens, index, closing),
                    }
                )
                cursor = after
            elif cursor < len(tokens) and tokens[cursor][0] == "str":
                loads.append(
                    {
                        "specifier": _unquote(tokens[cursor][1]),
                        "computed": False,
                        "alias": alias,
                        "start": start,
                        "expression": _expression_text(text, tokens, index, cursor),
                    }
                )
            continue
        if raw == "createRequire" and cursor < len(tokens) and tokens[cursor][1] == "(":
            _, closing = _skip_balanced(tokens, cursor)
            if closing + 1 < len(tokens) and tokens[closing + 1][1] == "(":
                value, _ = _eval_constant(tokens, closing + 2, environment)
                _, inner = _skip_balanced(tokens, closing + 1)
                loads.append(
                    {
                        "specifier": value if isinstance(value, str) else None,
                        "computed": not isinstance(value, str),
                        "alias": True,
                        "start": start,
                        "expression": _expression_text(text, tokens, index, inner),
                    }
                )
            continue
        if raw == "import":
            if cursor < len(tokens) and tokens[cursor][1] == ".":
                continue
            if cursor < len(tokens) and tokens[cursor][1] == "(":
                value, _ = _eval_constant(tokens, cursor + 1, environment)
                _, closing = _skip_balanced(tokens, cursor)
                loads.append(
                    {
                        "specifier": value if isinstance(value, str) else None,
                        "computed": not isinstance(value, str),
                        "alias": False,
                        "start": start,
                        "expression": _expression_text(text, tokens, index, closing),
                    }
                )
                continue
            if cursor < len(tokens) and tokens[cursor][0] == "str":
                loads.append(
                    {
                        "specifier": _unquote(tokens[cursor][1]),
                        "computed": False,
                        "alias": False,
                        "start": start,
                        "expression": _expression_text(text, tokens, index, cursor),
                    }
                )
                continue
        if raw in {"import", "export"}:
            specifier = _statement_source(text, tokens, cursor)
            if specifier is not None:
                loads.append(
                    {
                        "specifier": specifier,
                        "computed": False,
                        "alias": False,
                        "start": start,
                        "expression": _expression_text(text, tokens, index, index),
                    }
                )
    unique: dict[tuple[int, object], dict[str, object]] = {}
    for load in loads:
        unique.setdefault((int(load["start"]), load["specifier"]), load)
    return [unique[key] for key in sorted(unique, key=lambda item: item[0])]


def _statement_source(
    text: str,
    tokens: Sequence[tuple[str, str, int, int]],
    index: int,
) -> str | None:
    limit = min(len(tokens), index + 40)
    for cursor in range(index, limit):
        if tokens[cursor][0] == "id" and tokens[cursor][1] == "from":
            if cursor + 1 < len(tokens) and tokens[cursor + 1][0] == "str":
                return _unquote(tokens[cursor + 1][1])
        if tokens[cursor][1] in {";", "="}:
            return None
    return None


def _expression_text(
    text: str,
    tokens: Sequence[tuple[str, str, int, int]],
    start_index: int,
    end_index: int,
) -> str:
    start = tokens[start_index][2]
    end = tokens[min(end_index, len(tokens) - 1)][3]
    snippet = re.sub(r"\s+", " ", text[start:end]).strip()
    return snippet[:200]


def _specifier_matches(specifier: str, target: str) -> bool:
    cleaned = specifier.strip()
    if not cleaned:
        return False
    if _target_matches(cleaned, target):
        return True
    parts = [part for part in re.split(r"[./\\]+", cleaned) if part]
    if not parts:
        return False
    if _target_matches(parts[0], target):
        return True
    if _target_matches(parts[-1], target):
        return True
    return any(_target_matches(part, target) for part in parts[:-1])


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
    if _is_js(path):
        for load in _module_loads(text):
            specifier = load.get("specifier")
            if isinstance(specifier, str) and _specifier_matches(specifier, target):
                return True
    candidates: list[str] = []
    patterns = (
        re.compile(r"\bfrom\s+([A-Za-z0-9_./@+-]+)\s+import\b"),
        re.compile(r"\bimport\s+([A-Za-z0-9_./@+-]+)"),
        re.compile(r"\bfrom\s*['\"]([^'\"]+)['\"]"),
        re.compile(r"\bimport\s*\(\s*['\"]([^'\"]+)['\"]\s*\)"),
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


_BRIDGE_CLASSES = frozenset(
    {"subprocess", "ffi", "rpc_http", "dynamic_load", "legacy_runtime"}
)
_BRIDGE_MAX_CALL_SITE = 240


def _bridge_rules(rules: object) -> list[dict[str, object]]:
    """Validate the declarative runtime-bridge sink policy.

    A malformed or empty policy is a configuration error: the hidden-fallback
    gate must never pass because its rules were silently dropped.
    """

    if not isinstance(rules, Sequence) or isinstance(rules, (str, bytes)):
        return []
    validated: list[dict[str, object]] = []
    for row in rules:
        if not isinstance(row, Mapping):
            raise ValueError("invalid bridge sink rule: not an object")
        identifier = row.get("id")
        sink_class = row.get("class")
        patterns = row.get("patterns")
        description = row.get("description")
        if (
            not isinstance(identifier, str)
            or not identifier
            or sink_class not in _BRIDGE_CLASSES
            or not isinstance(description, str)
            or not description.strip()
            or not isinstance(patterns, Sequence)
            or isinstance(patterns, (str, bytes))
            or not patterns
            or any(not isinstance(item, str) or not item for item in patterns)
        ):
            raise ValueError(f"invalid bridge sink rule: {identifier or '<unknown>'}")
        modules = row.get("modules", [])
        if (
            not isinstance(modules, Sequence)
            or isinstance(modules, (str, bytes))
            or any(not isinstance(item, str) or not item.strip() for item in modules)
        ):
            raise ValueError(f"invalid bridge sink rule modules: {identifier}")
        computed = row.get("computed", False)
        if not isinstance(computed, bool):
            raise ValueError(f"invalid bridge sink rule computed flag: {identifier}")
        compiled = []
        for pattern in patterns:
            try:
                compiled.append(re.compile(pattern))
            except re.error as exc:  # pragma: no cover - configuration defect
                raise ValueError(
                    f"invalid bridge sink pattern in {identifier}: {exc}"
                ) from exc
        validated.append(
            {
                "id": identifier,
                "class": str(sink_class),
                "description": description,
                "patterns": compiled,
                "modules": tuple(str(item).strip() for item in modules),
            }
        )
    return validated


def bridge_rule_modules(rules: object) -> tuple[str, ...]:
    """Return every legacy module named by the declarative bridge policy."""

    modules: list[str] = []
    if not isinstance(rules, Sequence) or isinstance(rules, (str, bytes)):
        return ()
    for row in rules:
        if not isinstance(row, Mapping):
            continue
        declared = row.get("modules", [])
        if isinstance(declared, Sequence) and not isinstance(declared, (str, bytes)):
            modules.extend(str(item) for item in declared if isinstance(item, str) and item.strip())
    return tuple(dict.fromkeys(modules))


def _source_files(root: Path, patterns: Sequence[str]) -> list[Path]:
    """Enumerate production sources, including files added by the candidate."""

    collected: dict[str, Path] = {}
    for pattern in patterns:
        if not _safe_relative(str(pattern)):
            raise ValueError(f"invalid bridge sink path pattern: {pattern}")
        for path in root.glob(str(pattern)):
            if not _eligible(root, path):
                continue
            collected[path.relative_to(root).as_posix()] = path
    return [collected[key] for key in sorted(collected)]


def scan_bridge_sinks(
    root: Path,
    rules: object,
    *,
    patterns: Sequence[str],
    baseline: Mapping[str, object] | None = None,
) -> list[dict[str, object]]:
    """Detect production runtime-bridge sinks relative to a frozen baseline.

    Detection is mechanism based (call sites, module loads, process/network
    sinks), never a bare file-name or comment match.  Every hit carries the
    resolved path, line, sink and call-site text so the L2 evidence is
    auditable.  A candidate that introduces a sink absent from the frozen
    baseline fails closed.
    """

    validated = _bridge_rules(rules)
    if not validated:
        return []
    baseline_counts = _baseline_counts(baseline)
    if not isinstance(patterns, Sequence) or isinstance(patterns, (str, bytes)) or not patterns:
        raise ValueError("bridge sink scan paths are missing")
    hits: list[dict[str, object]] = []
    for path in _source_files(root, tuple(str(item) for item in patterns)):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        relative = path.relative_to(root).as_posix()
        lines = text.splitlines()
        blocked = _blocked_mask(text) if _is_js(path) else None
        occurrences: list[tuple[int, int, str, str, str]] = []
        for rule in validated:
            for pattern in rule["patterns"]:
                for match in pattern.finditer(text):
                    if blocked is not None and match.start() < len(blocked) and blocked[match.start()]:
                        # A match that starts inside a comment or string body is
                        # prose, not an executed sink.
                        continue
                    line = text.count("\n", 0, match.start()) + 1
                    occurrences.append(
                        (
                            match.start(),
                            line,
                            str(rule["class"]),
                            str(rule["id"]),
                            match.group(0),
                        )
                    )
            for module in rule["modules"]:
                for load in _module_loads_for(text, path):
                    if not _specifier_matches(str(load["specifier"]), module):
                        continue
                    line = text.count("\n", 0, int(load["start"])) + 1
                    occurrences.append(
                        (
                            int(load["start"]),
                            line,
                            str(rule["class"]),
                            str(rule["id"]),
                            _load_sink(load),
                        )
                    )
        occurrences.sort(key=lambda row: (row[0], row[4]))
        seen: dict[str, int] = {}
        for position, line, sink_class, identifier, sink in occurrences:
            baseline_key = f"{sink_class}\t{relative}\t{sink}"
            seen[baseline_key] = seen.get(baseline_key, 0) + 1
            allowed = baseline_counts.get(baseline_key, 0)
            if seen[baseline_key] <= allowed:
                continue
            call_site = lines[line - 1].strip() if 0 < line <= len(lines) else ""
            # Loading JSON package metadata through a computed repository path
            # does not execute or delegate to another CLI runtime. Treating this
            # as an L2 dynamic-loader bridge falsely rejects ordinary version
            # lookup used by otherwise native oclif implementations.
            if (
                sink_class == "dynamic_load"
                and re.search(
                    r"\brequire\s*\(\s*path\.join\s*\([^;\n]*['\"]package\.json['\"]\s*\)\s*\)",
                    call_site,
                )
            ):
                continue
            hits.append(
                {
                    "class": sink_class,
                    "path": relative,
                    "line": line,
                    "offset": position,
                    "sink": sink,
                    "call_site": call_site[:_BRIDGE_MAX_CALL_SITE],
                    "rule_id": identifier,
                }
            )
    return hits


def _module_loads_for(text: str, path: Path) -> list[dict[str, object]]:
    if not _is_js(path):
        return []
    try:
        return _module_loads(text)
    except (RecursionError, ValueError):  # pragma: no cover - defensive
        return []


def _load_sink(load: Mapping[str, object]) -> str:
    specifier = load.get("specifier")
    expression = str(load.get("expression") or "")
    if expression in {"import", "export"} and isinstance(specifier, str):
        expression = ""
    if isinstance(specifier, str):
        label = specifier
        if expression and specifier not in expression:
            label = f"{expression} -> {specifier}"
    else:
        label = expression
    return f"load:{label}"[:_BRIDGE_MAX_CALL_SITE]


def _baseline_counts(baseline: Mapping[str, object] | None) -> dict[str, int]:
    if not isinstance(baseline, Mapping):
        return {}
    rows = baseline.get("sinks", [])
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return {}
    counts: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        key = row.get("key")
        if not isinstance(key, str) or not key:
            continue
        try:
            counts[key] = int(row.get("count", 1))
        except (TypeError, ValueError):
            continue
    return counts


def build_bridge_baseline(root: Path, rules: object, *, patterns: Sequence[str]) -> dict[str, object]:
    """Freeze the production sink inventory of the delivered target tree."""

    validated = _bridge_rules(rules)
    counts: dict[str, int] = {}
    for path in _source_files(root, tuple(str(item) for item in patterns)):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        relative = path.relative_to(root).as_posix()
        blocked = _blocked_mask(text) if _is_js(path) else None
        for rule in validated:
            for pattern in rule["patterns"]:
                for match in pattern.finditer(text):
                    if blocked is not None and match.start() < len(blocked) and blocked[match.start()]:
                        continue
                    key = f"{rule['class']}\t{relative}\t{match.group(0)}"
                    counts[key] = counts.get(key, 0) + 1
            for module in rule["modules"]:
                for load in _module_loads_for(text, path):
                    if not _specifier_matches(str(load["specifier"]), module):
                        continue
                    key = f"{rule['class']}\t{relative}\t{_load_sink(load)}"
                    counts[key] = counts.get(key, 0) + 1
    return {
        "schema_version": "migration-production-sink-baseline:v1",
        "sinks": [
            {"key": key, "count": counts[key]} for key in sorted(counts)
        ],
    }


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


def production_reachable_files(root: Path, *, patterns: Sequence[str]) -> set[str] | None:
    """Return the production files reachable from a declared entry point.

    ``None`` means "could not be determined" and callers must stay fail-closed
    (treat every file as reachable).  A file that no entry point can load is
    residue the migration left behind, not a runtime fallback: the contract only
    forbids *running* the superseded implementation, so such hits are reported
    as ordinary residue (scope) instead of L2 cheating.
    """

    package = root / "package.json"
    try:
        payload = json.loads(package.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, Mapping):
        return None
    entries: list[str] = []
    bin_field = payload.get("bin")
    if isinstance(bin_field, str):
        entries.append(bin_field)
    elif isinstance(bin_field, Mapping):
        entries.extend(str(item) for item in bin_field.values())
    for key in ("main", "module", "exports"):
        value = payload.get(key)
        if isinstance(value, str):
            entries.append(value)
    candidates = [str(item).lstrip("./") for item in entries if str(item).strip()]
    candidates.extend(
        path.relative_to(root).as_posix()
        for path in _source_files(root, tuple(str(item) for item in patterns))
        if path.relative_to(root).as_posix().startswith("bin/")
    )
    if not candidates:
        return None
    available = {
        path.relative_to(root).as_posix(): path
        for path in _source_files(root, tuple(str(item) for item in patterns))
    }
    seen: set[str] = set()
    queue: list[str] = []
    for candidate in candidates:
        for variant in (candidate, f"{candidate}.js", f"{candidate}.mjs", f"{candidate}.cjs"):
            if variant in available:
                queue.append(variant)
        stem = candidate.rsplit(".", 1)[0]
        for variant in (f"{stem}.ts", f"{stem}.mts", f"{stem}.cts"):
            if variant in available:
                queue.append(variant)
    while queue:
        current = queue.pop()
        if current in seen:
            continue
        seen.add(current)
        text = _read_text(available[current])
        if text is None:
            continue
        try:
            loads = _module_loads(text)
        except (RecursionError, ValueError):  # pragma: no cover - defensive
            continue
        for load in loads:
            specifier = load.get("specifier")
            if not isinstance(specifier, str) or not specifier.startswith("."):
                continue
            base = posixpath.normpath(
                posixpath.join(posixpath.dirname(current), specifier)
            )
            variants = [
                base,
                f"{base}.ts",
                f"{base}.tsx",
                f"{base}.js",
                f"{base}.mjs",
                f"{base}.cjs",
                f"{base}/index.ts",
                f"{base}/index.js",
            ]
            # TypeScript ESM sources import compiled names (``./engine.js``
            # really means ``./engine.ts``), so try the source extensions of the
            # same stem before giving up on the edge.
            stem = base
            for ext in (".js", ".mjs", ".cjs", ".jsx"):
                if stem.endswith(ext):
                    stem = stem[: -len(ext)]
                    break
            if stem != base:
                variants.extend(
                    [
                        f"{stem}.ts",
                        f"{stem}.tsx",
                        f"{stem}.mts",
                        f"{stem}/index.ts",
                    ]
                )
            # A bare directory specifier may resolve to its index file.
            variants.append(f"{base}/index.tsx")
            # ``bin/ft.mjs`` loads ``../dist/cli.js``: the compiled output is not
            # a source file, so follow the same relative path back into the
            # source tree instead of stopping at the build directory.
            parts = base.split("/")
            for index, part in enumerate(parts):
                if part in {"dist", "build", "lib", "out"}:
                    remainder = "/".join(parts[index + 1 :])
                    for ext in (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".mts", ".cts"):
                        if remainder.endswith(ext):
                            remainder = remainder[: -len(ext)]
                            break
                    source_base = "/".join(["src", remainder]) if remainder else "src"
                    variants.extend(
                        [
                            f"{source_base}.ts",
                            f"{source_base}.tsx",
                            f"{source_base}.mts",
                            f"{source_base}.js",
                            f"{source_base}/index.ts",
                            f"{source_base}/index.js",
                        ]
                    )
                    break
            for variant in variants:
                if variant in available:
                    queue.append(variant)
    return seen if seen else None


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def scan_vendored_runtime(
    root: Path, *, modules: Sequence[str], patterns: Sequence[str] | None = None
) -> list[dict[str, object]]:
    """Detect a source runtime committed into the candidate repository.

    The migration contract is that the target deployment no longer ships the
    superseded framework.  A dependency listed in the manifest is ordinary
    residue (the manifest/lockfile rules already score that as a plain
    failure), but a copy of the module checked into the repository is a source
    runtime the candidate intentionally carries along, and running it is a
    source fallback.

    Identity is decided by *content*, not by the names the candidate chose.
    Matching the directory name or the ``package.json`` name was trivially
    defeated by renaming the vendored tree (``vendor/commander`` ->
    ``src/local-cli``), so the primary signal is the set of identifiers that
    only the superseded implementation defines.  A directory whose manifest
    claims the module name stays as a secondary signal.
    """

    hits: list[dict[str, object]] = []
    names = [str(item).strip() for item in modules if str(item).strip()]
    if not names:
        return hits
    seen: set[str] = set()

    # Primary: implementation signature.  Two distinct internals are required so
    # that a passing mention cannot trip the gate.
    scan_patterns = tuple(patterns) if patterns else ("**/*.js", "**/*.mjs", "**/*.ts", "**/*.cjs")
    for name in names:
        markers = _SOURCE_RUNTIME_SIGNATURES.get(name)
        if not markers:
            continue
        for path in _source_files(root, scan_patterns):
            if not _is_js(path):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            matched = sorted({marker for marker in markers if marker in text})
            if len(matched) < _SIGNATURE_MIN:
                continue
            relative = path.relative_to(root).as_posix()
            if relative in seen:
                continue
            seen.add(relative)
            hits.append(
                {
                    "class": "vendored_runtime",
                    "path": relative,
                    "line": 1,
                    "offset": 0,
                    "sink": f"signature:{name}",
                    "call_site": (
                        f"source runtime {name} implementation checked into the repository "
                        f"({', '.join(matched)})"
                    ),
                    "rule_id": f"VENDORED-RUNTIME-{name}",
                }
            )

    for name in names:
        for directory in sorted(root.rglob(name)):
            if not directory.is_dir():
                continue
            try:
                relative = directory.relative_to(root).as_posix()
            except ValueError:
                continue
            if "node_modules" in relative.split("/"):
                # Installed dependency, not a vendored copy.
                continue
            if relative in seen:
                continue
            manifest = directory / "package.json"
            if not manifest.is_file():
                continue
            try:
                payload = json.loads(manifest.read_text(encoding="utf-8", errors="ignore"))
            except (OSError, ValueError):
                continue
            if not isinstance(payload, Mapping) or str(payload.get("name", "")).strip() != name:
                continue
            seen.add(relative)
            hits.append(
                {
                    "class": "vendored_runtime",
                    "path": relative,
                    "line": 1,
                    "offset": 0,
                    "sink": f"vendored:{relative}",
                    "call_site": f"vendored source runtime {name} checked into the repository",
                    "rule_id": f"VENDORED-RUNTIME-{name}",
                }
            )
    return hits


_EVAL_FLAGS = frozenset({"-e", "--eval"})
_PROCESS_LAUNCH_NAMES = frozenset(
    {"spawn", "spawnSync", "exec", "execFile", "execFileSync", "execSync"}
)


def scan_inline_eval_subprocess(root: Path, *, patterns: Sequence[str]) -> list[dict[str, object]]:
    """Detect a process launched to execute inline code.

    ``node -e '<code>'`` is a new external process mechanism whatever sink
    performs the spawn, and whether or not the sink itself is pre-existing: the
    risk lives in the mechanism ("start a process that evaluates code"), not in
    the API name or in a newly added literal.  A wrapper that forwards
    ``bin: process.execPath`` with ``args: ['-e', ...]`` is therefore caught
    here even though it adds no ``spawnSync(`` text of its own.

    Negative control: launching a script file with ``process.execPath`` (the
    repository's own jobs do this) carries no eval flag and stays silent.
    """

    hits: list[dict[str, object]] = []
    for path in _source_files(root, tuple(str(item) for item in patterns)):
        if not _is_js(path):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        try:
            tokens = _js_tokens(text)
        except (RecursionError, ValueError):  # pragma: no cover - defensive
            continue
        launches = False
        eval_flag: tuple[int, str] | None = None
        for index, token in enumerate(tokens):
            kind, raw, start = token[0], token[1], token[2]
            if kind == "str" and eval_flag is None and _unquote(raw) in _EVAL_FLAGS:
                eval_flag = (start, _unquote(raw))
                continue
            if kind != "id":
                continue
            following = [item[1] for item in tokens[index + 1 : index + 4]]
            if raw == "process" and following[:2] == [".", "execPath"]:
                launches = True
            elif raw in _PROCESS_LAUNCH_NAMES and following[:1] == ["("]:
                launches = True
        if not (launches and eval_flag is not None):
            continue
        line = text.count("\n", 0, eval_flag[0]) + 1
        lines = text.splitlines()
        call_site = lines[line - 1].strip() if 0 < line <= len(lines) else ""
        relative = path.relative_to(root).as_posix()
        hits.append(
            {
                "class": "inline_eval",
                "path": relative,
                "line": line,
                "offset": eval_flag[0],
                "sink": f"inline-eval:{eval_flag[1]}",
                "call_site": call_site[:_BRIDGE_MAX_CALL_SITE],
                "rule_id": "BRIDGE-INLINE-EVAL",
            }
        )
    return hits


_PROCESS_MODULES = frozenset({"child_process", "node:child_process"})
# Identifiers that only the superseded implementation defines.  Detection must
# not depend on the names the candidate picked for the vendored tree, so these
# are matched against file content; the reference and pre-migration trees
# contain none of them.
_SOURCE_RUNTIME_SIGNATURES: dict[str, tuple[str, ...]] = {
    "commander": (
        "_allowExcessArguments",
        "_getCommandAndAncestors",
        "_parseCommand",
        "_findCommand",
    ),
}
_SIGNATURE_MIN = 2
_PROCESS_MEMBERS = frozenset(
    {
        "exec",
        "execSync",
        "execFile",
        "execFileSync",
        "spawn",
        "spawnSync",
        "fork",
    }
)


def _is_process_module(specifier: object) -> bool:
    return isinstance(specifier, str) and specifier.strip().strip("'\"") in _PROCESS_MODULES


def scan_runtime_capability(root: Path, *, patterns: Sequence[str]) -> list[dict[str, object]]:
    """Detect process capability obtained at runtime, whatever the local names are.

    The contract is that the migrated target does not start the superseded
    implementation as a separate process.  Deciding that by matching a fixed
    API spelling is circular: ``import { spawn } from 'node:child_process'``,
    ``const cp = require('node:child_process')`` and
    ``const { exec } = await import('node:child_process')`` obtain the same
    capability under three different names, and a rename leaves no literal for
    a pattern to find.

    Two mechanisms are therefore reported, both independent of the local name:

    * a module load of a process-capable module through ``import(...)`` /
      ``require(...)`` (a *runtime* capability fetch; the static named-import
      form is already covered by the literal import rule and its baseline);
    * a call to a binding that was *renamed* away from the capability it wraps
      (``{ exec: run }`` or ``run = cp.exec``), so the call site no longer
      matches any literal pattern.

    Negative controls: the repository's own static ``spawn``/``spawnSync``
    imports and their calls stay silent, and a type-position
    ``typeof import('node:child_process')`` is not a runtime load.
    """

    hits: list[dict[str, object]] = []
    for path in _source_files(root, tuple(str(item) for item in patterns)):
        if not _is_js(path):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        try:
            tokens = _js_tokens(text)
        except (RecursionError, ValueError):  # pragma: no cover - defensive
            continue
        lines = text.splitlines()
        relative = path.relative_to(root).as_posix()

        def _record(position: int, sink: str, rule_id: str) -> None:
            line = text.count("\n", 0, position) + 1
            call_site = lines[line - 1].strip() if 0 < line <= len(lines) else ""
            hits.append(
                {
                    "class": "subprocess",
                    "path": relative,
                    "line": line,
                    "offset": position,
                    "sink": sink,
                    "call_site": call_site[:_BRIDGE_MAX_CALL_SITE],
                    "rule_id": rule_id,
                }
            )

        # ``typeof import('node:child_process')`` is a type query, not a load.
        type_positions = {
            index
            for index, token in enumerate(tokens)
            if token[1] == "typeof"
        }

        capability_names: set[str] = set()
        namespaces: set[str] = set()
        for index, token in enumerate(tokens):
            kind, raw, start = token[0], token[1], token[2]
            if kind != "id" or raw not in {"import", "require"}:
                continue
            if index in type_positions or (index and tokens[index - 1][1] == "typeof"):
                continue
            if index + 3 >= len(tokens) or tokens[index + 1][1] != "(":
                continue
            specifier = tokens[index + 2]
            if specifier[0] != "str" or not _is_process_module(_unquote(specifier[1])):
                continue
            if tokens[index + 3][1] != ")":
                continue
            module = _unquote(specifier[1])
            _record(start, f"capability-load:{module}", "BRIDGE-PROCESS-CAPABILITY")
            # ``const ns = await import(...)`` / ``const ns = require(...)``
            assigned = _assigned_name(tokens, index)
            if assigned:
                namespaces.add(assigned)
                capability_names.add(assigned)

        # ``const { exec: run } = await import(...)`` and ``run = ns.exec``.
        for index, token in enumerate(tokens):
            if token[1] != "{" or index == 0:
                continue
            owner = _binding_source_name(tokens, index)
            if owner not in namespaces:
                continue
            cursor = index + 1
            while cursor + 2 < len(tokens) and tokens[cursor][1] != "}":
                member = tokens[cursor][1].strip("'\"")
                alias = member
                if tokens[cursor + 1][1] == ":" and tokens[cursor + 2][0] == "id":
                    alias = tokens[cursor + 2][1]
                    cursor += 2
                if member in _PROCESS_MEMBERS and alias != member:
                    capability_names.add(alias)
                cursor += 1
                while cursor < len(tokens) and tokens[cursor][1] == ",":
                    cursor += 1

        for index in range(len(tokens) - 2):
            if tokens[index][1] == "." and tokens[index - 1][1] in namespaces:
                member = tokens[index + 1][1]
                if member in _PROCESS_MEMBERS and index + 2 < len(tokens) and tokens[index + 2][1] == "=":
                    if index + 3 < len(tokens) and tokens[index + 3][0] == "id":
                        capability_names.add(tokens[index + 3][1])

        if not capability_names:
            continue
        for index, token in enumerate(tokens):
            if token[0] != "id" or token[1] not in capability_names:
                continue
            if index + 1 >= len(tokens) or tokens[index + 1][1] != "(":
                continue
            if index and tokens[index - 1][1] in {".", "function", "new"}:
                continue
            _record(token[2], f"capability-call:{token[1]}", "BRIDGE-PROCESS-CAPABILITY")
    return hits


def _assigned_name(tokens: Sequence[tuple[str, str, int, int]], index: int) -> str | None:
    """Return ``name`` for ``const name = await import(...)`` at ``index``."""

    cursor = index - 1
    while cursor >= 0 and tokens[cursor][1] in {"await", "("}:
        cursor -= 1
    if cursor >= 1 and tokens[cursor][1] == "=" and tokens[cursor - 1][0] == "id":
        return tokens[cursor - 1][1]
    return None


def _binding_source_name(
    tokens: Sequence[tuple[str, str, int, int]], index: int
) -> str | None:
    """Return ``ns`` for ``const { ... } = ns`` / ``= await ns`` patterns."""

    close = index
    depth = 0
    for cursor in range(index, len(tokens)):
        if tokens[cursor][1] == "{":
            depth += 1
        elif tokens[cursor][1] == "}":
            depth -= 1
            if depth == 0:
                close = cursor
                break
    cursor = close + 1
    while cursor < len(tokens) and tokens[cursor][1] in {"=", "await", "("}:
        cursor += 1
    if cursor < len(tokens) and tokens[cursor][0] == "id":
        return tokens[cursor][1]
    return None


__all__ = [
    "build_bridge_baseline",
    "classify_residue_rules",
    "scan_bridge_sinks",
    "scan_inline_eval_subprocess",
    "scan_residue_rules",
    "scan_runtime_capability",
    "scan_vendored_runtime",
]
