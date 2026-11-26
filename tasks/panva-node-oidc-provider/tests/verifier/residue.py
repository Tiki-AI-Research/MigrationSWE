"""Language-neutral evaluation of declarative residue rules.

Module ownership detection is **value-based, not syntax-based**: a scope file is
reported when the module it loads resolves to the source framework, regardless of
how the reference is written -- static import, ``import()``, ``require()``,
``createRequire`` aliases, string concatenation, template literals, ``[...].join``,
``.reverse()``/``.slice()``/index access and the other purely-static string
operations that still produce the same module name.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
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


# ---------------------------------------------------------------------------
# Value-based module reference resolution
# ---------------------------------------------------------------------------

def _strip_comments(text: str) -> str:
    """Blank ``//`` and ``/* */`` comments while preserving string contents."""

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
                    text[index] == "*" and index + 1 < length and text[index + 1] == "/"
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
            index += 2
            continue
        if character == quote:
            state = "normal"
            quote = ""
            index += 1
            continue
        index += 1
    return "".join(characters)


def _split_top_level(text: str, separator: str) -> list[str]:
    parts: list[str] = []
    buffer = ""
    depth = 0
    quote = ""
    index = 0
    length = len(text)
    while index < length:
        character = text[index]
        if quote:
            buffer += character
            if character == "\\" and index + 1 < length:
                buffer += text[index + 1]
                index += 2
                continue
            if character == quote:
                quote = ""
            index += 1
            continue
        if character in ("'", '"', "`"):
            quote = character
            buffer += character
            index += 1
            continue
        if character in "([{":
            depth += 1
        elif character in ")]}":
            depth -= 1
        if character == separator and depth == 0:
            parts.append(buffer)
            buffer = ""
            index += 1
            continue
        buffer += character
        index += 1
    parts.append(buffer)
    return parts


_STRING_LITERAL = re.compile(r"""^(?:'((?:[^'\\]|\\.)*)'|"((?:[^"\\]|\\.)*)")$""", re.S)
_IDENTIFIER = re.compile(r"^[A-Za-z_$][\w$]*$")
_INTEGER = re.compile(r"^\d+$")
_ARRAY_LITERAL = re.compile(r"^\[(.*)\]$", re.S)
_METHOD_CALL = re.compile(r"^(.+)\.([A-Za-z_$][\w$]*)\s*\((.*)\)$", re.S)
_STRING_CALL = re.compile(r"^String\s*\(\s*(.*?)\s*\)$", re.S)


def _unescape(value: str) -> str:
    return value.replace("\\'", "'").replace('\\"', '"').replace("\\\\", "\\")


def _balanced(text: str) -> bool:
    depth = 0
    quote = ""
    index = 0
    length = len(text)
    while index < length:
        character = text[index]
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
            index += 1
            continue
        if character in "([{":
            depth += 1
        elif character in ")]}":
            depth -= 1
            if depth < 0:
                return False
        index += 1
    return depth == 0 and quote == ""


def _strip_outer_parens(text: str) -> str:
    stripped = text.strip()
    while stripped.startswith("(") and stripped.endswith(")") and _balanced(stripped[1:-1]):
        stripped = stripped[1:-1].strip()
    return stripped


def _split_trailing_index(text: str) -> tuple[str, str] | None:
    """Split ``BASE[INDEX]`` at the trailing, top-level ``[...]``."""

    if not text.endswith("]"):
        return None
    depth = 0
    quote = ""
    index = len(text) - 1
    while index >= 0:
        character = text[index]
        if quote:
            if character == quote and text[index - 1 : index] != "\\":
                quote = ""
            index -= 1
            continue
        if character in ("'", '"', "`"):
            quote = character
            index -= 1
            continue
        if character == "]":
            depth += 1
        elif character == "[":
            depth -= 1
            if depth == 0:
                base = text[:index].strip()
                if not base:
                    return None
                return base, text[index + 1 : -1]
        index -= 1
    return None


def _apply_method(base, method: str, arguments: str, environment, depth: int):
    if depth > 12:
        return None
    argument_values = [_resolve_expr(item, environment, depth + 1) for item in _split_top_level(arguments, ",")]
    args = [value for value in argument_values if value is not None]
    try:
        if method == "join":
            if not isinstance(base, list):
                return None
            separator = args[0] if args else ","
            if not isinstance(separator, str) or any(not isinstance(item, str) for item in base):
                return None
            return separator.join(base)
        if method == "reverse":
            return list(reversed(base)) if isinstance(base, list) else base[::-1]
        if method == "concat":
            if isinstance(base, list):
                return base + [item for value in argument_values if isinstance(value, list) for item in value]
            return "".join([base, *[value for value in args if isinstance(value, str)]])
        if method in ("slice", "substring"):
            start = int(args[0]) if args else 0
            end = int(args[1]) if len(args) > 1 else None
            return base[start:end]
        if method == "substr":
            start = int(args[0]) if args else 0
            length = int(args[1]) if len(args) > 1 else None
            return base[start : start + length] if length is not None else base[start:]
        if method in ("charAt", "at"):
            return base[int(args[0])] if args else ""
        if method == "repeat" and args:
            return base * int(args[0])
        if method == "trim":
            return base.strip()
        if method == "toLowerCase":
            return base.lower()
        if method == "toUpperCase":
            return base.upper()
        if method in ("replace", "replaceAll") and len(args) >= 2:
            return base.replace(args[0], args[1])
        if method == "split" and args:
            if not isinstance(base, str):
                return None
            return list(base.split(args[0]))
        if method == "flat":
            return [item for value in base for item in (value if isinstance(value, list) else [value])]
    except (ValueError, IndexError, TypeError):
        return None
    return None


_STATIC_GLOBAL = re.compile(r"^(Array|String)\.([A-Za-z_$][\w$]*)\s*\((.*)\)$", re.S)


def _resolve_static_global(text: str, environment: Mapping[str, object], depth: int):
    match = _STATIC_GLOBAL.match(text)
    if not match or not _balanced(text):
        return None
    owner, name, arguments = match.groups()
    if owner == "Array" and name == "from":
        value = _resolve_expr(arguments, environment, depth + 1)
        if isinstance(value, str):
            return list(value)
        if isinstance(value, list):
            return list(value)
        return None
    if owner == "String" and name == "fromCharCode":
        values = [_resolve_expr(item, environment, depth + 1) for item in _split_top_level(arguments, ",")]
        if values and all(isinstance(item, int) for item in values):
            return "".join(chr(item) for item in values)  # type: ignore[arg-type]
        return None
    return None


def _resolve_expr(expression: str, environment: Mapping[str, object], depth: int = 0):
    """Resolve a purely-static expression to its value (str or list[str])."""

    if depth > 12:
        return None
    text = _strip_outer_parens(expression)
    if not text:
        return None
    match = _STRING_LITERAL.match(text)
    if match:
        return _unescape(match.group(1) if match.group(1) is not None else match.group(2))
    if _INTEGER.match(text):
        return int(text)
    if text.startswith("`") and text.endswith("`") and len(text) >= 2:
        body = text[1:-1]
        if "${" not in body:
            return body
        out = ""
        for part in re.split(r"(\$\{[^{}]*\})", body):
            if part.startswith("${") and part.endswith("}"):
                value = _resolve_expr(part[2:-1], environment, depth + 1)
                if not isinstance(value, str):
                    return None
                out += value
            else:
                out += part
        return out
    if _IDENTIFIER.match(text):
        return environment.get(text)
    if text.startswith("[") and text.endswith("]") and _balanced(text) and _split_trailing_index(text) is None:
        elements: list[object] = []
        for item in _split_top_level(text[1:-1], ","):
            stripped = item.strip()
            if stripped.startswith("..."):
                spread = _resolve_expr(stripped[3:], environment, depth + 1)
                if isinstance(spread, list):
                    elements.extend(spread)
                elif isinstance(spread, str):
                    elements.extend(list(spread))
                else:
                    return None
            else:
                value = _resolve_expr(item, environment, depth + 1)
                if value is None:
                    return None
                elements.append(value)
        return elements
    index_part = _split_trailing_index(text)
    if index_part is not None:
        base, index_text = index_part
        base_value = _resolve_expr(base, environment, depth + 1)
        index_value = _resolve_expr(index_text, environment, depth + 1)
        if base_value is not None and isinstance(index_value, int):
            try:
                return base_value[index_value]
            except (IndexError, KeyError, TypeError):
                return None
    global_value = _resolve_static_global(text, environment, depth)
    if global_value is not None:
        return global_value
    method = _METHOD_CALL.match(text)
    if method and _balanced(text):
        base, name, arguments = method.groups()
        base_value = _resolve_expr(base, environment, depth + 1)
        if base_value is not None:
            value = _apply_method(base_value, name, arguments, environment, depth)
            if value is not None:
                return value
    parts = _split_top_level(text, "+")
    if len(parts) > 1:
        resolved = [_resolve_expr(part, environment, depth + 1) for part in parts]
        if all(isinstance(part, (str, int)) for part in resolved):
            return "".join(str(part) for part in resolved)
        return None
    match = _STRING_CALL.match(text)
    if match:
        value = _resolve_expr(match.group(1), environment, depth + 1)
        return str(value) if isinstance(value, (str, int)) else None
    return None


_ASSIGNMENT = re.compile(
    r"(?:^|[;\n{}(,])\s*(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*([^;\n]+)"
)


def _constant_environment(text: str) -> dict[str, object]:
    environment: dict[str, object] = {}
    for _ in range(6):
        changed = False
        for match in _ASSIGNMENT.finditer(text):
            name = match.group(1)
            if name in environment:
                continue
            value = _resolve_expr(match.group(2), environment)
            if value is not None:
                environment[name] = value
                changed = True
        if not changed:
            break
    return environment


def _require_like_names(text: str) -> set[str]:
    names = {"require"}
    for match in re.finditer(r"""import\s*\{([^}]*)\}\s*from\s*['"]node:module['"]""", text):
        for part in match.group(1).split(","):
            alias = re.match(r"\s*createRequire(?:\s+as\s+([A-Za-z_$][\w$]*))?\s*$", part)
            if alias:
                names.add(alias.group(1) or "createRequire")
    for _ in range(5):
        before = len(names)
        for match in re.finditer(
            r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:[\w$.]+\.)?createRequire\s*\(",
            text,
        ):
            names.add(match.group(1))
        for match in re.finditer(
            r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*([A-Za-z_$][\w$]*)\s*\(",
            text,
        ):
            if match.group(2) in names:
                names.add(match.group(1))
        if len(names) == before:
            break
    return names


def _module_references(text: str) -> set[str]:
    source = _strip_comments(text)
    environment = _constant_environment(source)
    require_like = _require_like_names(source)
    specifiers: set[str] = set()

    for match in re.finditer(
        r"""(?:^|[^\w$.])(?:import|export)\s+(?:[^'"]*?\bfrom\s*)?['"]([^'"]+)['"]""",
        source,
    ):
        specifiers.add(match.group(1))
    for match in re.finditer(r"""(?:^|[^\w$.])import\s*\(\s*([^)]*?)\s*\)""", source):
        value = _resolve_expr(match.group(1), environment)
        if isinstance(value, str) and value:
            specifiers.add(value)
    for match in re.finditer(r"""\brequire\s*\(\s*([^)]*?)\s*\)""", source):
        value = _resolve_expr(match.group(1), environment)
        if isinstance(value, str) and value:
            specifiers.add(value)
    for match in re.finditer(r"""\brequire\s+['"]([^'"]+)['"]""", source):
        specifiers.add(match.group(1))
    for name in require_like:
        if name == "require":
            continue
        for match in re.finditer(r"""\b%s\s*\(\s*([^)]*?)\s*\)""" % re.escape(name), source):
            value = _resolve_expr(match.group(1), environment)
            if isinstance(value, str) and value:
                specifiers.add(value)
    return specifiers


def _import_contains(path: Path, target: str) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    if any(
        _target_matches(candidate, target)
        or _target_matches(re.split(r"[./\\]", candidate, maxsplit=1)[0], target)
        for candidate in _module_references(text)
    ):
        return True
    evaluated = _js_eval(path)
    if evaluated:
        for value in evaluated["resolved"]:
            if _target_matches(value, target) or _target_matches(
                re.split(r"[./\\]", value, maxsplit=1)[0], target
            ):
                return True
    return False


_JS_HELPER = (
    Path(__file__).resolve().parent.parent
    / "assets"
    / "shared"
    / "integration"
    / "module-name-eval.cjs"
)
_JS_CACHE: dict[str, dict[str, list[str]] | None] = {}


def _js_eval(path: Path) -> dict[str, list[str]] | None:
    """Evaluate a file's dynamic module-name expressions with the real JS engine."""

    key = str(path)
    if key in _JS_CACHE:
        return _JS_CACHE[key]
    result: dict[str, list[str]] | None = None
    try:
        if _JS_HELPER.is_file() and shutil.which("node"):
            completed = subprocess.run(
                ["node", str(_JS_HELPER), str(path)],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            if completed.returncode == 0 and completed.stdout.strip():
                payload = json.loads(completed.stdout)
                arguments = payload.get("args", [])
                values = payload.get("values", [])
                resolved = [value for value in values if isinstance(value, str) and value]
                unresolved = [
                    str(argument)
                    for argument, value in zip(arguments, values)
                    if value is None and "import.meta" not in str(argument)
                ]
                result = {"resolved": resolved, "unresolved": unresolved}
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, ValueError):
        result = None
    _JS_CACHE[key] = result
    return result


def _dynamic_callsites(text: str) -> tuple[list[str], dict[str, object]]:
    """Return the argument text of every dynamic module load in ``text``."""

    source = _strip_comments(text)
    environment = _constant_environment(source)
    arguments: list[str] = []
    for match in re.finditer(r"""(?:^|[^\w$.])import\s*\(\s*([^)]*?)\s*\)""", source):
        arguments.append(match.group(1))
    for match in re.finditer(r"""\brequire\s*\(\s*([^)]*?)\s*\)""", source):
        arguments.append(match.group(1))
    for name in _require_like_names(source):
        if name == "require":
            continue
        for match in re.finditer(r"""\b%s\s*\(\s*([^)]*?)\s*\)""" % re.escape(name), source):
            arguments.append(match.group(1))
    return arguments, environment


def scan_unresolved_dynamic_references(root: Path, rules: object) -> list[dict[str, str]]:
    """Fail closed on a dynamic module load whose specifier cannot be resolved.

    A migration-scope file that loads a module through a value the scanner cannot
    reduce to a literal is treated as a potential source fallback: for this task
    the reference solution has no dynamic module loads in scope, so an
    unresolvable one is a signal rather than a legitimate pattern.
    """

    if not isinstance(rules, Sequence) or isinstance(rules, (str, bytes)):
        return []
    hits: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for row in rules:
        if not isinstance(row, Mapping) or row.get("kind") != "import":
            continue
        identifier = str(row.get("id") or "")
        patterns = row.get("paths", [])
        if not isinstance(patterns, Sequence) or isinstance(patterns, (str, bytes)):
            continue
        for path in _paths(root, tuple(str(item) for item in patterns if isinstance(item, str))):
            evaluated = _js_eval(path)
            if evaluated is not None:
                unresolved_args = list(evaluated["unresolved"])
            else:
                try:
                    text = path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                arguments, environment = _dynamic_callsites(text)
                unresolved_args = [
                    argument
                    for argument in arguments
                    if "import.meta" not in argument
                    and not isinstance(_resolve_expr(argument, environment), str)
                ]
            for argument in unresolved_args:
                token = " ".join(str(argument).split())[:120]
                key = (path.relative_to(root).as_posix(), token)
                if key in seen:
                    continue
                seen.add(key)
                hits.append(
                    {
                        "path": path.relative_to(root).as_posix(),
                        "token": token or "<dynamic>",
                        "kind": "dynamic",
                        "rule_id": identifier,
                    }
                )
    return hits


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


__all__ = ["classify_residue_rules", "scan_residue_rules", "scan_unresolved_dynamic_references"]
