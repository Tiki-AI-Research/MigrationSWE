"""Language-neutral evaluation of declarative residue rules."""

from __future__ import annotations

import base64
import binascii
import difflib
import hashlib
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
# Entry-graph and source-copy checks must inspect bundled output too: a
# candidate can place the old runtime in dist/ or build/ and import it from a
# clean-looking index.js.  Declarative residue rules keep the broader build
# exclusions so they do not scan generated vendor output.
_ENTRY_EXCLUDED_PARTS = {".git", ".venv", "venv", "node_modules"}
_RULE_KINDS = {"literal", "dependency", "import", "file", "builtin-loader"}
_DANGEROUS_MODULES = {
    "child_process",
    "node:child_process",
    "worker_threads",
    "node:worker_threads",
    "devalue",
}
_DANGEROUS_TOKENS = (
    "child_process",
    "execFileSync",
    "execSync",
    "spawnSync",
    "spawn",
    "fork",
    "process.binding",
    "process._linkedBinding",
    "_linkedBinding",
    "createRequire",
    "worker_threads",
    "node_modules/devalue",
)
_MODULE_SPECIFIER = re.compile(
    r"""(?:
        \bfrom\s*['"]([^'"]+)['"]
        |\bimport\s*\(\s*['"]([^'"]+)['"]\s*\)
        |\brequire\s*\(\s*['"]([^'"]+)['"]\s*\)
        |^\s*import\s+['"]([^'"]+)['"]
    )""",
    re.VERBOSE | re.MULTILINE,
)
_DYNAMIC_MODULE_CALL = re.compile(
    r"\b(?:import|require)\s*\(\s*([^)]*)\)", re.MULTILINE
)
_STATIC_MODULE_CALL = re.compile(r"""(['"])(?:(?!\1).)*\1""", re.DOTALL)
_DYNAMIC_DANGEROUS_FRAGMENTS = (
    "child_",
    "childprocess",
    "worker_thread",
    "node_modules",
    "devalue",
    "getbuiltinmodule",
)
_ALLOWED_CREATE_REQUIRE_SPECIFIERS = {"@js-temporal/polyfill"}
# Candidate-authored test files are not shipped package source.  A test may
# legitimately drive a subprocess (for example to prove that a rejection does
# not leak an unhandled promise rejection), so scanning those files for
# deceptive bridges produces false L2 hits.  Reachable test modules are still
# covered by the entry-graph scan, which ignores this exclusion.
_CANDIDATE_TEST_FILE = re.compile(
    r"\.(?:test|spec)\.[cm]?[jt]sx?$", re.IGNORECASE
)
_CANDIDATE_TEST_DIRS = {"__tests__", "test", "tests"}


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


def _entry_eligible(root: Path, path: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    return (
        path.is_file()
        and not path.is_symlink()
        and not any(part in _ENTRY_EXCLUDED_PARTS for part in relative.parts)
    )


_BRACE_EXPANSION = re.compile(r"\{([^{}]*)\}")


def _expand_braces(pattern: str) -> list[str]:
    """Expand ``{a,b}`` alternations into concrete glob patterns.

    ``pathlib`` globbing has no brace syntax, but a rule describes the JavaScript
    source suffixes it applies to as ``src/**/*.{js,cjs,mjs}``.  Leaving the
    braces unexpanded would silently collect nothing for those rules, so a
    renamed ``.cjs``/``.mjs`` bridge could slip past the scan.
    """

    match = _BRACE_EXPANSION.search(pattern)
    if match is None:
        return [pattern]
    prefix = pattern[: match.start()]
    suffix = pattern[match.end() :]
    expanded: list[str] = []
    for option in match.group(1).split(","):
        expanded.extend(prefix + option + tail for tail in _expand_braces(suffix))
    return expanded


def _paths(root: Path, patterns: Sequence[str]) -> Iterable[Path]:
    if not patterns:
        return ()
    collected: list[Path] = []
    for pattern in patterns:
        if not _safe_relative(pattern):
            continue
        for resolved in _expand_braces(pattern):
            if not _safe_relative(resolved):
                continue
            path = root / resolved
            if any(token in resolved for token in "*?["):
                collected.extend(root.glob(resolved))
            elif path.is_dir() and not path.is_symlink():
                collected.extend(path.rglob("*"))
            else:
                collected.append(path)
    candidates = collected
    unique = {path.resolve(): path for path in candidates if _eligible(root, path)}
    return (unique[key] for key in sorted(unique, key=lambda item: item.as_posix()))


def is_candidate_test_file(root: Path, path: Path) -> bool:
    """Return True for candidate test modules, which are not shipped source."""

    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    if any(part.casefold() in _CANDIDATE_TEST_DIRS for part in relative.parts[:-1]):
        return True
    return bool(_CANDIDATE_TEST_FILE.search(relative.name))


def _target_matches(value: str, target: str) -> bool:
    actual = value.casefold().strip()
    expected = target.casefold().strip()
    return actual == expected or fnmatchcase(actual, expected)


# Candidate source suffixes whose declarative ``literal`` rules must be matched
# against the *evaluated* value, not only the textual spelling.
_JS_SOURCE_SUFFIXES = {".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"}


def _literal_contains(path: Path, target: str) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    if target.casefold() in text.casefold():
        return True
    if path.suffix.casefold() not in _JS_SOURCE_SUFFIXES:
        return False
    # A bridge does not have to spell a sink as one contiguous literal.  Raw
    # text is scanned first (so a token in a comment still counts); the same
    # rule is then re-applied to the constant-folded executable source so
    # reconstructed sinks (``'exec' + 'Sync'``, array ``join('')``, a
    # base64-decoded specifier, computed member access) are recognised on their
    # evaluated value.  Folding only the source suffixes keeps structured
    # manifests (``package.json`` / ``pnpm-lock.yaml``) on exact text.
    try:
        normalized = _normalize_js_code(_executable_code(text))
    except Exception:  # pragma: no cover - defensive
        return False
    return target.casefold() in normalized.casefold()


def _safe_static_create_require_usage(text: str) -> bool:
    """Recognize the task's legitimate optional Temporal polyfill loader.

    ``createRequire`` is a capability, not proof of delegation.  The migrated
    package may statically load its declared ``@js-temporal/polyfill`` when no
    global Temporal implementation exists.  Accept only that closed shape:
    ``createRequire(import.meta.url)`` assigned to a local name and every call
    through that name using the one allowed static package specifier.  Dynamic,
    path-based, old-package, or unused loaders remain anti-cheating hits.
    """

    code = _normalize_js_code(_executable_code(text))
    assignments = re.findall(
        r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*"
        r"createRequire\s*\(\s*import\.meta\.url\s*\)",
        code,
    )
    if not assignments:
        return False
    for name in assignments:
        calls = re.findall(
            rf"(?<![\w$.]){re.escape(name)}\s*\(\s*([^)]*?)\s*\)",
            code,
        )
        if not calls:
            return False
        for argument in calls:
            match = re.fullmatch(r"(['\"])([^'\"]+)\1", argument.strip())
            if match is None or match.group(2) not in _ALLOWED_CREATE_REQUIRE_SPECIFIERS:
                return False
    return True


# ``process.getBuiltinModule`` cannot, on its own, prove delegation to the old
# package.  Node exposes benign built-ins through it (for example
# ``node:util`` to call ``types.isProxy`` without triggering proxy traps).
# Only loads of a dangerous module, or loads whose specifier is not a static
# safe built-in, are bridges.
_SAFE_BUILTIN_MODULES = {"util", "node:util"}
_GET_BUILTIN_MODULE = re.compile(r"\bgetBuiltinModule\s*(?:\?\.)?\s*\(")
# A dynamic loader can also reach the built-in through a computed property or
# ``Reflect.get``.  The property name then survives only as a plain string
# literal (``process['getBuiltinModule']`` / ``Reflect.get(process,
# 'getBuiltinModule')``), so flag that spelling too.
_QUOTED_BUILTIN_MODULE = re.compile(r"""(['"])getBuiltinModule\1""")

# --- Lightweight constant folding for evasion-resistant review ---------------
#
# A deceptive bridge does not have to spell ``child_process`` or ``execSync`` as
# one contiguous literal.  ``process['getBuiltin' + 'Module']('child_' +
# 'process')``, ``process[['getBuiltin', 'Module'].join('')](spec)`` and
# ``Buffer.from('Y2hpbGRfcHJvY2Vzcw==', 'base64')`` all build the same runtime
# value while defeating a plain substring search.  We do not depend on an AST
# parser: we fold exactly the constant string expressions JavaScript evaluates
# eagerly (literal concatenation, templates with literal substitutions, array
# ``join('')``, ``String.fromCharCode`` and base64 decoding), then normalize
# computed member access, and match the dangerous names on the folded form.
# This reasons about *evaluated constants* instead of raw text while staying
# dependency-free.

_JS_STRING_LITERAL = r"""(?:'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*")"""
_JS_LITERAL_TOKEN = re.compile(_JS_STRING_LITERAL, re.DOTALL)
_JS_STRING_PARTS = re.compile(
    r"""'(?:\\.|[^'\\])*'""" + r'''|"(?:\\.|[^"\\])*"''', re.DOTALL
)
_ONE_LITERAL = re.compile(
    r"""^(?:'((?:\\.|[^'\\])*)'|"((?:\\.|[^"\\])*)")$""", re.DOTALL
)
_FOLD_CONCAT = re.compile(
    r"""(?P<head>"""
    + _JS_STRING_LITERAL
    + r""")(?P<tail>(?:\s*\+\s*"""
    + _JS_STRING_LITERAL
    + r""")+)""",
    re.DOTALL,
)
_FOLD_JOIN = re.compile(
    r"""\[\s*(?P<items>"""
    + _JS_STRING_LITERAL
    + r"""(?:\s*,\s*"""
    + _JS_STRING_LITERAL
    + r""")*)\s*\]\s*\.\s*join\s*\(\s*(?P<quote>['"])\s*(?P=quote)\s*\)""",
    re.DOTALL,
)
_FOLD_TEMPLATE = re.compile(r"""`(?P<body>(?:[^`\\]|\\.)*)`""", re.DOTALL)
_TEMPLATE_SUBSTITUTION = re.compile(r"\$\{([^{}]*)\}")
_FOLD_CHAR_CODES = re.compile(
    r"""String\s*\.\s*from(?:CharCode|CodePoint)\s*\(\s*(?P<args>[0-9a-fA-FxX,\s]+?)\s*\)"""
)
_FOLD_BASE64 = re.compile(
    r"""(?:Buffer\s*\.\s*from\s*\(\s*(?P<buffer>"""
    + _JS_STRING_LITERAL
    + r""")\s*,\s*(?P<encoding>['"])base64(?P=encoding)\s*\)"""
    r"""(?!\s*\.\s*toString\s*\(\s*['"](?!(?:utf-?8|utf8|ascii|latin1|binary)['"]))"""
    r"""|atob\s*\(\s*(?P<atob>"""
    + _JS_STRING_LITERAL
    + r""")\s*\))""",
    re.IGNORECASE | re.DOTALL,
)
_FOLD_COMPUTED_MEMBER = re.compile(
    r"""\[\s*(?P<quote>['"])(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)(?P=quote)\s*\]"""
)
_ESCAPE_SEQUENCES = {
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


def _unescape_js(body: str) -> str:
    out: list[str] = []
    index = 0
    while index < len(body):
        character = body[index]
        if character != "\\" or index + 1 >= len(body):
            out.append(character)
            index += 1
            continue
        marker = body[index + 1]
        if marker == "x" and index + 3 < len(body):
            try:
                out.append(chr(int(body[index + 2 : index + 4], 16)))
                index += 4
                continue
            except ValueError:
                pass
        if marker == "u":
            if index + 2 < len(body) and body[index + 2] == "{":
                close = body.find("}", index + 3)
                if close != -1:
                    try:
                        out.append(chr(int(body[index + 3 : close], 16)))
                        index = close + 1
                        continue
                    except ValueError:
                        pass
            elif index + 5 < len(body):
                try:
                    out.append(chr(int(body[index + 2 : index + 6], 16)))
                    index += 6
                    continue
                except ValueError:
                    pass
        out.append(_ESCAPE_SEQUENCES.get(marker, marker))
        index += 2
    return "".join(out)


def _decode_literal(literal: str) -> str | None:
    match = _ONE_LITERAL.match(literal.strip())
    if not match:
        return None
    body = match.group(1) if match.group(1) is not None else match.group(2)
    return _unescape_js(body)


def _quote_js(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _fold_string_parts(text: str) -> str:
    parts = _JS_STRING_PARTS.findall(text)
    decoded = [_decode_literal(part) for part in parts]
    return "".join(part for part in decoded if part)


def _fold_concatenations(code: str) -> str:
    def replace(match: re.Match) -> str:
        return _quote_js(_fold_string_parts(match.group(0)))

    return _FOLD_CONCAT.sub(replace, code)


def _fold_joins(code: str) -> str:
    def replace(match: re.Match) -> str:
        return _quote_js(_fold_string_parts(match.group("items")))

    return _FOLD_JOIN.sub(replace, code)


def _fold_templates(code: str) -> str:
    def replace(match: re.Match) -> str:
        body = match.group("body")
        if "${" not in body:
            return _quote_js(_unescape_js(body))
        pieces: list[str] = []
        position = 0
        for substitution in _TEMPLATE_SUBSTITUTION.finditer(body):
            value = _decode_literal(substitution.group(1).strip())
            if value is None:
                return match.group(0)
            pieces.append(_unescape_js(body[position : substitution.start()]))
            pieces.append(value)
            position = substitution.end()
        pieces.append(_unescape_js(body[position:]))
        return _quote_js("".join(pieces))

    return _FOLD_TEMPLATE.sub(replace, code)


def _fold_char_codes(code: str) -> str:
    def replace(match: re.Match) -> str:
        codes: list[int] = []
        for token in match.group("args").split(","):
            token = token.strip()
            if not token:
                continue
            try:
                codes.append(
                    int(token, 16) if token.lower().startswith("0x") else int(token)
                )
            except ValueError:
                return match.group(0)
        try:
            return _quote_js("".join(chr(code) for code in codes))
        except (ValueError, OverflowError):
            return match.group(0)

    return _FOLD_CHAR_CODES.sub(replace, code)


def _fold_base64(code: str) -> str:
    def replace(match: re.Match) -> str:
        literal = match.group("buffer") or match.group("atob")
        value = _decode_literal(literal)
        if value is None:
            return match.group(0)
        try:
            text = base64.b64decode(value, validate=True).decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError):
            return match.group(0)
        if len(text) > 512 or not all(
            character.isprintable() or character in "\t\n\r" for character in text
        ):
            return match.group(0)
        return _quote_js(text)

    return _FOLD_BASE64.sub(replace, code)


def _fold_js_constants(code: str) -> str:
    for _ in range(8):
        before = code
        code = _fold_joins(code)
        code = _fold_base64(code)
        code = _fold_char_codes(code)
        code = _fold_templates(code)
        code = _fold_concatenations(code)
        if code == before:
            break
    return code


def _normalize_computed_members(code: str) -> str:
    return _FOLD_COMPUTED_MEMBER.sub(r".\g<name>", code)


def _decode_escapes_in_literals(code: str) -> str:
    """Rewrite each string literal to its decoded value.

    An escaped spelling such as ``'child\\u005fprocess'`` equals
    ``child_process`` at runtime but contains no contiguous token in the raw
    text.  Decoding every literal (and re-quoting it canonically) folds those
    escapes before the dangerous-name match without needing an expression.
    """

    def replace(match: re.Match) -> str:
        value = _decode_literal(match.group(0))
        return _quote_js(value) if value is not None else match.group(0)

    return _JS_LITERAL_TOKEN.sub(replace, code)


def _executable_code(text: str) -> str:
    try:
        from integrity import _scan_javascript
    except ImportError:  # pragma: no cover - package import path
        from .integrity import _scan_javascript
    return _scan_javascript(text)[2]


def _normalize_js_code(code: str) -> str:
    return _normalize_computed_members(
        _fold_js_constants(_decode_escapes_in_literals(code))
    )


def _builtin_loader_hits_in_code(code: str) -> list[str]:
    hits: list[str] = []
    for match in _GET_BUILTIN_MODULE.finditer(code):
        opened = match.end() - 1
        depth = 0
        end = len(code)
        index = opened
        while index < len(code):
            character = code[index]
            if character in "'\"":
                quote = character
                index += 1
                while index < len(code):
                    if code[index] == "\\":
                        index += 2
                        continue
                    if code[index] == quote:
                        break
                    index += 1
            elif character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0:
                    end = index + 1
                    break
            index += 1
        argument = code[opened + 1 : max(opened + 1, end - 1)].strip()
        static = re.fullmatch(r"'([^'\\]*)'|\"([^\"\\]*)\"", argument)
        if static:
            specifier = (
                static.group(1) if static.group(1) is not None else static.group(2)
            ).strip().casefold()
            if specifier in _SAFE_BUILTIN_MODULES:
                continue
        hits.append(code[match.start() : end].strip())
    for match in _QUOTED_BUILTIN_MODULE.finditer(code):
        hits.append(code[match.start() : match.end()].strip())
    return hits


def _unsafe_builtin_loader_hits(text: str) -> list[str]:
    """Return ``getBuiltinModule`` loads that can reach a dangerous module."""

    return _builtin_loader_hits_in_code(_normalize_js_code(_executable_code(text)))


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
    if path.suffix.casefold() in {".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"}:
        # A local compatibility shim named ``uvu`` (or an alias imported via a
        # relative path) is legitimate after the runner migration.  Only
        # executable import specifiers count, and comments must not create a
        # false legacy-runner hit.
        try:
            from integrity import _scan_javascript
        except ImportError:  # pragma: no cover - package import path
            from .integrity import _scan_javascript
        _, _, text = _scan_javascript(text)
    candidates: list[str] = []
    patterns = (
        re.compile(r"\bfrom\s+([A-Za-z0-9_./@+-]+)\s+import\b"),
        re.compile(r"\bimport\s+([A-Za-z0-9_./@+-]+)"),
        re.compile(r"\bimport\s*\(\s*['\"]([^'\"]+)['\"]\s*\)"),
        re.compile(r"\bimport\s*['\"]([^'\"]+)['\"]"),
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
        if target == "createRequire":
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                return False
            if _safe_static_create_require_usage(text):
                return False
        return _literal_contains(path, target)
    if kind == "builtin-loader":
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return False
        return bool(_unsafe_builtin_loader_hits(text)) and not _safe_static_create_require_usage(text)
    if kind == "dependency":
        return _dependency_contains(path, target)
    if kind == "import":
        return _import_contains(path, target)
    relative = path.relative_to(root).as_posix()
    return fnmatchcase(relative.casefold(), target.casefold()) or fnmatchcase(
        path.name.casefold(), target.casefold()
    )


def scan_residue_rules(
    root: Path, rules: object, *, skip_candidate_tests: bool = False
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
            or kind not in _RULE_KINDS
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
        for path in _paths(root, tuple(patterns)):
            if skip_candidate_tests and is_candidate_test_file(root, path):
                continue
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


def _entry_points(root: Path) -> list[str]:
    entries: list[str] = []
    manifest = root / "package.json"
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    if isinstance(payload, Mapping):
        exports = payload.get("exports")
        root_export = exports.get(".") if isinstance(exports, Mapping) else None
        if isinstance(root_export, Mapping):
            entries.extend(
                str(value)
                for value in root_export.values()
                if isinstance(value, str)
            )
        elif isinstance(root_export, str):
            entries.append(root_export)
        for field in ("main", "module"):
            if isinstance(payload.get(field), str):
                entries.append(str(payload[field]))
    entries.append("index.js")
    resolved: list[str] = []
    for entry in entries:
        candidate = entry.replace("\\", "/").lstrip("./")
        if _safe_relative(candidate) and (root / candidate).is_file():
            resolved.append(candidate)
    return sorted(set(resolved))


def _resolve_relative(root: Path, source: str, specifier: str) -> str | None:
    base = (root / source).parent
    target = (base / specifier).resolve()
    try:
        relative = target.relative_to(root.resolve())
    except ValueError:
        return None
    value = relative.as_posix()
    if any(part in {".git", "node_modules"} for part in Path(value).parts):
        return None
    path = root / value
    if path.is_file() and not path.is_symlink():
        return value
    for suffix in (".js", ".mjs", ".cjs"):
        if (root / (value + suffix)).is_file():
            return value + suffix
    for name in ("index.js", "index.mjs"):
        if (path / name).is_file():
            return value.rstrip("/") + "/" + name
    return None


def scan_entry_graph(root: Path) -> list[dict[str, str]]:
    """Scan only modules reachable from the package entry points.

    This catches bridges moved outside ``src/`` (for example into ``lib/``)
    while not flagging build or test tooling that is not part of the shipped
    entry graph.
    """

    hits: list[dict[str, str]] = []
    pending = _entry_points(root)
    seen: set[str] = set()
    while pending:
        relative = pending.pop()
        if relative in seen:
            continue
        seen.add(relative)
        path = root / relative
        if not _entry_eligible(root, path):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        safe_create_require = _safe_static_create_require_usage(text)
        for token in _DANGEROUS_TOKENS:
            if token == "createRequire" and safe_create_require:
                continue
            if token.casefold() in text.casefold():
                hits.append(
                    {
                        "path": relative,
                        "token": token,
                        "kind": "literal",
                        "rule_id": "ENTRY-GRAPH-DANGEROUS-API",
                    }
                )
        # Constant-fold the executable source so reconstructed names
        # (``'child_' + 'process'``, arrays joined with ``''``, base64-decoded
        # specifiers, computed member calls) are matched on their evaluated
        # value instead of their literal spelling.  Raw text is still scanned
        # above so a literal in a comment cannot silently widen the check.
        normalized = _normalize_js_code(_executable_code(text))
        for token in _DANGEROUS_TOKENS:
            if token == "createRequire" and safe_create_require:
                continue
            if token.casefold() in normalized.casefold():
                hits.append(
                    {
                        "path": relative,
                        "token": token,
                        "kind": "literal",
                        "rule_id": "ENTRY-GRAPH-DANGEROUS-API",
                    }
                )
        for loader in _builtin_loader_hits_in_code(normalized):
            if safe_create_require:
                continue
            hits.append(
                {
                    "path": relative,
                    "token": loader[:160],
                    "kind": "builtin-loader",
                    "rule_id": "ENTRY-GRAPH-DANGEROUS-API",
                }
            )
        for match in _MODULE_SPECIFIER.finditer(text):
            specifier = next(
                (group for group in match.groups() if group is not None), ""
            )
            if not specifier:
                continue
            if specifier in _DANGEROUS_MODULES:
                hits.append(
                    {
                        "path": relative,
                        "token": specifier,
                        "kind": "import",
                        "rule_id": "ENTRY-GRAPH-EXTERNAL-DELEGATION",
                    }
                )
                continue
            if specifier.startswith("."):
                resolved = _resolve_relative(root, relative, specifier)
                if resolved is not None and resolved not in seen:
                    pending.append(resolved)
        for match in _DYNAMIC_MODULE_CALL.finditer(text):
            raw_argument = match.group(1).strip()
            # A plain static specifier is already handled by
            # ``_MODULE_SPECIFIER`` above.  Concatenated templates and
            # quote-plus-quote forms must still reach the dangerous-fragment
            # check (``'node:' + 'child_process'``).
            if _STATIC_MODULE_CALL.fullmatch(raw_argument):
                continue
            argument = raw_argument.casefold()
            # ``'child' + '_process'`` contains no single dangerous fragment
            # across the quotes, so also inspect the concatenation with quote,
            # backtick, plus and whitespace characters removed.
            compact = re.sub(r"[\"'`+\s]", "", raw_argument).casefold()
            if any(
                fragment in argument or fragment in compact
                for fragment in _DYNAMIC_DANGEROUS_FRAGMENTS
            ):
                hits.append(
                    {
                        "path": relative,
                        "token": match.group(0)[:160],
                        "kind": "import",
                        "rule_id": "ENTRY-GRAPH-DYNAMIC-EXTERNAL-DELEGATION",
                    }
                )
    unique: dict[tuple[str, str, str], dict[str, str]] = {}
    for hit in hits:
        unique[(hit["path"], hit["token"], hit["rule_id"])] = hit
    return [unique[key] for key in sorted(unique)]


def _regular_text_files(
    root: Path, *, excluded: set[str] | None = None
) -> Iterable[tuple[str, Path]]:
    if not root.is_dir() or root.is_symlink():
        return ()
    excluded = _EXCLUDED_PARTS if excluded is None else excluded
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if any(part in excluded for part in Path(relative).parts):
            continue
        yield relative, path


def _source_copy_lines(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    return [line.strip() for line in text.splitlines() if line.strip()]


def scan_source_copies(
    root: Path,
    source_root: Path,
    *,
    similarity: float = 0.90,
) -> list[dict[str, str]]:
    """Detect old source trees copied outside their original paths.

    A candidate can keep the entire old implementation under ``legacy/`` and
    route the new entry point to it.  Looking only for import specifiers misses
    that bridge because the copied module is reached through a benign-looking
    local import.  Exact hashes catch byte-for-byte copies; the similarity
    branch also catches copies with small edits.  A file at the original source
    path is not a copy: the migration may legitimately retain some modules
    (``src/constants.js`` is unchanged in the accepted solution).
    """

    source_inventory: dict[str, tuple[str, list[str]]] = {}
    for relative, path in _regular_text_files(source_root):
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
        source_inventory[relative] = (digest, _source_copy_lines(path))
    if not source_inventory:
        return []

    hits: list[dict[str, str]] = []
    for relative, path in _regular_text_files(
        root, excluded=_ENTRY_EXCLUDED_PARTS
    ):
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
        candidate_lines: list[str] | None = None
        for source_relative, (source_digest, source_lines) in source_inventory.items():
            if relative == source_relative:
                continue
            if digest == source_digest:
                hits.append(
                    {
                        "path": relative,
                        "token": "source-runtime/" + source_relative,
                        "kind": "file",
                        "rule_id": "ENTRY-GRAPH-SOURCE-COPY",
                    }
                )
                break
            if len(source_lines) < 20:
                continue
            if candidate_lines is None:
                candidate_lines = _source_copy_lines(path)
            if len(candidate_lines) < 20:
                continue
            ratio = difflib.SequenceMatcher(
                None, source_lines, candidate_lines, autojunk=False
            ).ratio()
            if ratio >= similarity:
                hits.append(
                    {
                        "path": relative,
                        "token": "source-runtime/" + source_relative,
                        "kind": "file",
                        "rule_id": "ENTRY-GRAPH-SOURCE-COPY-SIMILARITY",
                    }
                )
                break
    return hits


__all__ = [
    "is_candidate_test_file",
    "scan_entry_graph",
    "scan_residue_rules",
    "scan_source_copies",
]
