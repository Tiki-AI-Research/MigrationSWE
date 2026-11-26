"""Language-neutral evaluation of declarative residue rules."""

from __future__ import annotations

import base64
import binascii
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


def strip_comments(text: str, *, hash_comments: bool = False) -> str:
    """Blank line/block comments while preserving string contents and offsets.

    A comment or docstring that merely *mentions* the source runtime is not a
    residue call, so it must not count toward a residue rule.  String literals
    are preserved (rules such as ``require("fastify")`` intentionally match
    code).  ``#`` comments are only stripped for languages that use them
    (Python/shell), never for JS/TS where ``#`` is a private-field marker.
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
            if hash_comments and character == "#":
                while index < length and text[index] != "\n":
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
        index += 1
    return "".join(characters)


def _literal_contains(path: Path, target: str) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    text = strip_comments(text, hash_comments=path.suffix.lower() in {".py", ".sh"})
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


_JS_SUFFIXES = frozenset({".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".mts", ".cts"})
_JS_ARRAY_JOIN = re.compile(r"\[([^\[\]]*)\]\s*\.join\(\s*(['\"])([^'\"]*)\2\s*\)")
_JS_STR_CONCAT = re.compile(r"(['\"])((?:[^'\"\\]|\\.)*)\1\s*\+\s*(['\"])((?:[^'\"\\]|\\.)*)\3")
_JS_ASSIGN = re.compile(r"\b(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*(['\"])((?:[^'\"\\]|\\.)*)\2")
_JS_DYNAMIC_LOADS = (
    re.compile(r"\bimport\s*\(\s*([^()]*?)\s*\)"),
    re.compile(r"(?<![\w$.])require\s*\(\s*([^()]*?)\s*\)"),
    re.compile(r"createRequire\s*\([^()]*\)\s*\(\s*([^()]*?)\s*\)"),
)
# Names bound to a module loader: ``createRequire(...)``, ``require`` or
# ``module.require``.  Such a name is a dynamic-load sink when called (directly,
# via ``.call``/``.apply`` or through ``Reflect.apply``).
_JS_LOADER_BIND = re.compile(
    r"\b(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*(?::[^;]*?)?=\s*"
    r"(?:[A-Za-z_$][A-Za-z0-9_$]*\s*\.\s*)*(?:createRequire|require|module\s*\.\s*require)\b"
)
# Encoded / assembled string obfuscations decoded before module-name matching.
_JS_B64 = re.compile(
    r"Buffer\s*\.\s*from\s*\(\s*(['\"])([A-Za-z0-9+/=]+)\1\s*,\s*(['\"])base64\3\s*\)"
    r"(?:\s*\.\s*toString\s*\(\s*(['\"])([A-Za-z0-9_-]*)\4\s*\))?"
)
_JS_FROMCHAR = re.compile(r"String\s*\.\s*fromCharCode\s*\(([^()]*)\)")
# ``[99,111,...].map(String.fromCharCode).join("")`` and the arrow variant.
_JS_CHARCODE_MAP = (
    re.compile(
        r"\[\s*([0-9][0-9\s,]*)\]\s*\.\s*map\s*\(\s*String\s*\.\s*fromCharCode\s*\)"
        r"\s*\.\s*join\s*\(\s*(['\"])([^'\"]*)\2\s*\)"
    ),
    re.compile(
        r"\[\s*([0-9][0-9\s,]*)\]\s*\.\s*map\s*\(\s*\(\s*[A-Za-z_$][A-Za-z0-9_$]*\s*\)\s*=>\s*"
        r"String\s*\.\s*fromCharCode\s*\(\s*[A-Za-z_$][A-Za-z0-9_$]*\s*\)\s*\)\s*"
        r"\.\s*join\s*\(\s*(['\"])([^'\"]*)\2\s*\)"
    ),
)
# Subprocess / process-execution sinks: reaching the source runtime through a
# child process is exactly constraint-3 delegation ("deceptive fallback").
_JS_CHILD_PROCESS = re.compile(
    r"(?:child_process|execFile|execFileSync|spawnSync|\bspawn\b|\bexec\b|\bfork\b|process\s*\.\s*execPath)"
)


def _balanced_inner(text: str, open_index: int) -> str:
    """Return the text between a balanced ``( ... )`` starting at ``open_index``."""

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
                return text[open_index + 1 : index]
        elif character in "'\"`":
            quote = character
            index += 1
            while index < length and text[index] != quote:
                if text[index] == "\\":
                    index += 1
                index += 1
        index += 1
    return text[open_index + 1 :]


def _js_b64_sub(match: "re.Match[str]") -> str:
    raw = match.group(2)
    try:
        data = base64.b64decode(raw + "=" * (-len(raw) % 4), validate=False).decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        return match.group(0)
    return '"%s"' % data.replace("\\", "\\\\").replace('"', '\\"')


def _js_fromchar_sub(match: "re.Match[str]") -> str:
    parts = [part.strip() for part in match.group(1).split(",") if part.strip()]
    try:
        value = "".join(chr(int(part, 0)) for part in parts)
    except ValueError:
        return match.group(0)
    return '"%s"' % value.replace("\\", "\\\\").replace('"', '\\"')


def _js_charcode_map_sub(match: "re.Match[str]") -> str:
    codes = [part.strip() for part in match.group(1).split(",") if part.strip()]
    try:
        value = "".join(chr(int(code)) for code in codes)
    except ValueError:
        return match.group(0)
    return '"%s"' % value.replace("\\", "\\\\").replace('"', '\\"')


def _js_fold_inline(text: str) -> str:
    """Recover module names hidden by join/concatenation/base64/fromCharCode."""

    for _ in range(12):
        before = text
        text = _JS_ARRAY_JOIN.sub(
            lambda match: '"%s"'
            % match.group(3).join(re.findall(r"['\"]([^'\"]*)['\"]", match.group(1))),
            text,
        )
        text = _JS_STR_CONCAT.sub(lambda match: '"%s"' % (match.group(2) + match.group(4)), text)
        text = _JS_B64.sub(_js_b64_sub, text)
        text = _JS_FROMCHAR.sub(_js_fromchar_sub, text)
        for pattern in _JS_CHARCODE_MAP:
            text = pattern.sub(_js_charcode_map_sub, text)
        if text == before:
            break
    return text


def _js_dynamic_refs(text: str, target: str, *, allow_subprocess: bool = True) -> bool:
    """Semantic check: does this module load the runtime *by value*.

    Loader identity is tracked by value: ``const f = require`` /
    ``createRequire(...)`` / ``module.require``, aliases of those names, and
    borrowed loaders (``f.call``/``f.apply`` / ``Reflect.apply(f, ...)``).
    Arguments are extracted with balanced parentheses and decoded
    (join/concat/base64/fromCharCode).  A run-time load whose name cannot be
    resolved **fails closed**, and a subprocess/``process.execPath`` bridge is a
    delegation attempt.  A resolvable load of a non-target module (e.g.
    ``node:util``) is not flagged.
    """

    folded = _js_fold_inline(strip_comments(text))
    env = {match.group(1): match.group(3) for match in _JS_ASSIGN.finditer(folded)}
    loaders = set(_JS_LOADER_BIND.findall(folded))
    for _ in range(4):
        added = {
            match.group(1)
            for match in re.finditer(
                r"\b(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*(?::[^;]*?)?=\s*"
                r"([A-Za-z_$][A-Za-z0-9_$]*)\s*(?=[;\n,)]|$)",
                folded,
            )
            if match.group(2) in loaders
        }
        if not added - loaders:
            break
        loaders |= added

    def resolve(expr: str) -> str | None:
        expr = expr.strip()
        literal = re.fullmatch(r"(['\"])((?:[^'\"\\]|\\.)*)\1", expr)
        if literal:
            return literal.group(2)
        if re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", expr) and expr in env:
            return env[expr]
        array = re.search(r"\[\s*(['\"])((?:[^'\"\\]|\\.)*)\1\s*\]", expr)
        if array:
            return array.group(2)
        return None

    open_positions: list[int] = []
    for pattern in (
        re.compile(r"(?<![\w$.])(?:require|import)\s*\("),
        re.compile(r"createRequire\s*\([^()]*\)\s*\("),
    ):
        open_positions.extend(match.end() - 1 for match in pattern.finditer(folded))
    for name in loaders:
        open_positions.extend(
            match.end() - 1
            for match in re.finditer(r"(?<![\w$.])" + re.escape(name) + r"\s*\(", folded)
        )
    for name in sorted(loaders | {"require", "createRequire"}):
        for match in re.finditer(
            r"(?<![\w$.])" + re.escape(name) + r"\s*\.\s*(?:call|apply)\s*\(", folded
        ):
            open_positions.append(match.end() - 1)
        for match in re.finditer(r"Reflect\s*\.\s*apply\s*\(\s*" + re.escape(name) + r"\s*,", folded):
            open_positions.append(folded.index("(", match.start()))

    unresolved = False
    for position in sorted(set(open_positions)):
        argument = _balanced_inner(folded, position)
        if not argument.strip():
            # A zero-argument call (e.g. a factory function returned by
            # ``createRequire(...)("express")`` then invoked as ``express()``)
            # is not a module load; do not fail closed on it.
            continue
        value = resolve(argument)
        if value is None:
            unresolved = True
        elif _target_matches(value, target):
            return True
    if allow_subprocess and _JS_CHILD_PROCESS.search(folded):
        # Executing a payload through the interpreter (``-e``/``-c``) or
        # ``process.execPath`` is a delegation bridge; a plain ``fork``/``spawn``
        # of the app's own helper is not.
        if re.search(r"process\s*\.\s*execPath", folded):
            unresolved = True
        for match in re.finditer(
            r"\b(?:execFile|execFileSync|spawnSync|spawn|exec|fork)\s*\(", folded
        ):
            if re.search(r"['\"]-[ec]['\"]", _balanced_inner(folded, match.end() - 1)):
                unresolved = True
                break
    return unresolved


def _deceptive_targets(config: object) -> tuple[str, ...]:
    """Bare source-runtime module names to scan the whole tree for."""

    names: set[str] = set()
    if isinstance(config, Mapping):
        technology = config.get("source_technology")
        if isinstance(technology, Mapping) and isinstance(technology.get("name"), str):
            if technology["name"].strip():
                names.add(technology["name"].strip().casefold())
        policy = config.get("policy")
        rules = policy.get("deceptive_fallback_rules") if isinstance(policy, Mapping) else None
        if isinstance(rules, Sequence) and not isinstance(rules, (str, bytes)):
            for row in rules:
                target = row.get("target") if isinstance(row, Mapping) else None
                if isinstance(target, str) and re.fullmatch(r"@?[a-z0-9][a-z0-9@/._-]*", target.strip().casefold()):
                    names.add(target.strip().casefold())
    return tuple(sorted(names))


_BINARY_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".bmp", ".svgz", ".woff",
    ".woff2", ".ttf", ".otf", ".eot", ".wasm", ".zip", ".gz", ".tgz", ".tar",
    ".bz2", ".xz", ".7z", ".pdf", ".mp4", ".mov", ".webm", ".mp3", ".so", ".dll",
    ".dylib", ".a", ".class", ".jar", ".pyc", ".exe",
})


def scan_source_runtime_tree(root: Path, targets: Sequence[str]) -> list[dict[str, str]]:
    """Scan the whole ``javascript-app/`` tree for source-runtime loads.

    Residue rules cover only the paths they name, so a candidate can hide a
    source-runtime load in an unlisted file — a ``*.cjs`` module, or an opaque
    payload (``*.dat``) that a subprocess later executes.  Every text file under
    the migration target is scanned so relocation cannot evade detection.
    """

    hits: list[dict[str, str]] = []
    base = Path(root) / "javascript-app"
    if not base.is_dir() or base.is_symlink():
        return hits
    for path in sorted(base.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root)
        if any(part in _EXCLUDED_PARTS for part in relative.parts):
            continue
        if path.suffix.lower() in _BINARY_SUFFIXES:
            continue
        try:
            if path.stat().st_size > 1_000_000:
                continue
        except OSError:
            continue
        allow_subprocess = "tests" not in {part.casefold() for part in relative.parts}
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            text = ""
        compact = "".join(text.split())
        decoded = ""
        if len(compact) >= 24 and re.fullmatch(r"[A-Za-z0-9+/=]+", compact):
            try:
                decoded = base64.b64decode(compact + "=" * (-len(compact) % 4)).decode("utf-8", "ignore")
            except (binascii.Error, ValueError):
                decoded = ""
        for target in targets:
            found = _import_contains(path, target, allow_subprocess=allow_subprocess)
            if not found and decoded:
                found = bool(
                    re.search(r"['\"]" + re.escape(target) + r"['\"]", decoded)
                    or _js_dynamic_refs(decoded, target, allow_subprocess=allow_subprocess)
                )
            if found:
                hits.append(
                    {
                        "path": relative.as_posix(),
                        "token": target,
                        "kind": "runtime",
                        "rule_id": "WHOLE-TREE-SCAN",
                    }
                )
                break
    return hits


def _import_contains(path: Path, target: str, *, allow_subprocess: bool = True) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    text = strip_comments(text, hash_comments=path.suffix.lower() in {".py", ".sh"})
    # Decode assembled/encoded literals (join / concatenation / base64 /
    # fromCharCode) so a source-runtime name hidden in a payload is visible to
    # the static specifier scan as well.
    if path.suffix.lower() in _JS_SUFFIXES:
        text = _js_fold_inline(text)
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
    matched = any(
        _target_matches(candidate, target)
        or _target_matches(re.split(r"[./\\]", candidate, maxsplit=1)[0], target)
        for candidate in candidates
    )
    if matched:
        return True
    if path.suffix.lower() in _JS_SUFFIXES:
        return _js_dynamic_refs(text, target, allow_subprocess=allow_subprocess)
    return False


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


__all__ = ["classify_residue_rules", "scan_residue_rules"]
