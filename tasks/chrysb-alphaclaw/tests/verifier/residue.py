"""Language-neutral evaluation of declarative residue rules."""

from __future__ import annotations

import base64
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
_RULE_KINDS = {
    "literal",
    "dependency",
    "import",
    "file",
    # Semantic Express detection: scan every production JavaScript module for an
    # Express import/reference, then classify it by whether the module is
    # reachable from a server entrypoint.  ``express_runtime`` reports reachable
    # (deceptive) uses; ``express_leftover`` reports unreachable (ordinary) ones.
    "express_runtime",
    "express_leftover",
}
_PRODUCTION_SUFFIXES = (".js", ".cjs", ".mjs", "")
# The semantic scanner covers the whole production tree by default.  A rule may
# still declare ``http_layer_paths`` to delegate a directory to the F2P probe,
# but the production HTTP layer is exactly where hidden runtime bridges live, so
# it must not be skipped implicitly.
_DEFAULT_HTTP_LAYER: tuple[str, ...] = ()
# Framework use requires a real package load/API sink; a bare string mention or
# a local adapter filename such as ``./http/hono-express`` is not a bridge.
_EXPRESS_PACKAGE_SPECIFIER = re.compile(r"^express(?:/|$)", re.IGNORECASE)
_EXPRESS_USAGE = re.compile(
    r"\bexpress\s*\(\s*\)"
    r"|\bexpress\s*\.\s*(?:json|static|raw|Router|urlencoded)\s*\("
)
_MODULE_SPECIFIER = re.compile(
    r"""(?:
        \brequire\s*\(\s*(?P<require>["'][^"']+["'])
      | \bfrom\s*(?P<from>["'][^"']+["'])
      | \bimport\s*\(\s*(?P<dynamic>["'][^"']+["'])
      | \bexport\s+[^;]*?\bfrom\s*(?P<export>["'][^"']+["'])
    )""",
    re.VERBOSE,
)
_CALL_SITE = re.compile(r"\b(?:require|import)\s*\(")
_FROM_LITERAL = re.compile(r"\bfrom\s*(['\"])((?:\\.|[^'\\])*)\1", re.DOTALL)
_STRING = re.compile(
    r"'((?:\\.|[^'\\])*)'|\"((?:\\.|[^\"\\])*)\"|`((?:\\.|[^`\\])*)`",
    re.DOTALL,
)
_ARRAY_JOIN = re.compile(r"\[([^\]]*?)\]\s*\.\s*join\s*\(", re.DOTALL)
_LITERAL_PATTERN = r"(?:'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\")"
_CONCAT_CHAIN = re.compile(rf"(?:{_LITERAL_PATTERN}\s*\+\s*)+{_LITERAL_PATTERN}")
_FROM_CHAR_CODE = re.compile(r"String\s*\.\s*fromCharCode\s*\(([^()]*)\)")
_BUFFER_BASE64 = re.compile(
    r"Buffer\s*\.\s*from\s*\(\s*(['\"])(?P<data>[A-Za-z0-9+/=_-]+)\1\s*,"
    r"\s*(['\"])(?P<encoding>base64|base64url|hex)\3\s*\)"
    r"\s*\.\s*toString\s*\(",
    re.DOTALL,
)


def _strip_comments(text: str) -> str:
    """Blank JavaScript comments while preserving string literals.

    Module specifiers must stay visible, so quoted text is preserved; only
    comments (and their contents) are replaced with spaces.  Offsets and line
    numbers stay stable for diagnostics.
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
            index += 2
            continue
        if character == quote:
            state = "normal"
        index += 1
    return "".join(characters)


def _production_files(root: Path, patterns: Sequence[str]) -> list[str]:
    return sorted(
        {
            path.relative_to(root).as_posix()
            for path in _paths(root, patterns)
            if _is_javascript_source(path)
            and _is_within(path, root)
        }
    )


def _is_javascript_source(path: Path) -> bool:
    suffix = path.suffix.casefold()
    if suffix in {".js", ".cjs", ".mjs"}:
        return True
    if suffix:
        return False
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    if text.startswith("#!"):
        return False
    return any(
        token in text
        for token in (
            "require(",
            "module.exports",
            "import ",
            "export ",
            "const ",
            "let ",
            "var ",
            "function ",
            "=>",
        )
    )


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _normalize_relative(relative: str) -> str:
    return relative.replace("\\", "/").lstrip("./")


def _resolve_local(root: Path, relative: str, specifier: str) -> str | None:
    if not specifier.startswith("."):
        return None
    base = (root / relative).parent
    target = (base / specifier).resolve()
    candidates: list[Path] = [target]
    candidates.extend(target.with_name(target.name + suffix) for suffix in _PRODUCTION_SUFFIXES)
    candidates.extend(target / f"index{suffix}" for suffix in _PRODUCTION_SUFFIXES)
    for candidate in candidates:
        if candidate.is_file() and not candidate.is_symlink() and _is_within(candidate, root):
            return candidate.relative_to(root).as_posix()
    return None


def _balanced_arguments(source: str, open_index: int) -> str | None:
    depth = 0
    quote = ""
    index = open_index
    length = len(source)
    while index < length:
        character = source[index]
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
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return source[open_index + 1:index]
        index += 1
    return None


def _specifier_expressions(source: str) -> list[str]:
    expressions: list[str] = []
    for match in _CALL_SITE.finditer(source):
        arguments = _balanced_arguments(source, match.end() - 1)
        if arguments is not None:
            expressions.append(arguments)
    expressions.extend(match.group(0) for match in _FROM_LITERAL.finditer(source))
    return expressions


def _specifiers(source: str) -> list[str]:
    values: list[str] = []
    for expression in _specifier_expressions(source):
        values.extend(_folded_strings(expression))
    return values


def _strings(source: str) -> list[str]:
    values: list[str] = []
    for match in _STRING.finditer(source):
        values.append(next(group for group in match.groups() if group is not None))
    return values


def _unescape(value: str) -> str:
    return re.sub(r"\\(['\"`\\])", r"\1", value)


def _folded_strings(fragment: str) -> list[str]:
    values: list[str] = []
    for match in _STRING.finditer(fragment):
        value = next(group for group in match.groups() if group is not None)
        values.append(_unescape(value))
    for match in _ARRAY_JOIN.finditer(fragment):
        parts = _strings(match.group(1))
        if parts:
            values.append("".join(parts))
    for match in _CONCAT_CHAIN.finditer(fragment):
        parts = _strings(match.group(0))
        if len(parts) >= 2:
            values.append("".join(parts))
    for match in _FROM_CHAR_CODE.finditer(fragment):
        codes = [int(item) for item in re.findall(r"\d+", match.group(1))]
        if codes and all(0 < code < 0x110000 for code in codes):
            values.append("".join(chr(code) for code in codes))
    for match in _BUFFER_BASE64.finditer(fragment):
        data = match.group("data")
        encoding = match.group("encoding")
        try:
            if encoding == "hex":
                raw = bytes.fromhex(data)
            else:
                altchars = b"-_" if encoding == "base64url" else None
                raw = base64.b64decode(data + "=" * (-len(data) % 4), altchars=altchars)
            values.append(raw.decode("utf-8", errors="ignore"))
        except (ValueError, TypeError):
            continue
    return values


def _mask_strings(text: str) -> str:
    def blank(match: re.Match[str]) -> str:
        token = match.group(0)
        return token[0] + token[-1] if len(token) >= 2 else token

    return _STRING.sub(blank, text)


def _express_usage(source: str) -> bool:
    for expression in _specifier_expressions(source):
        if any(
            _EXPRESS_PACKAGE_SPECIFIER.match(value)
            for value in _folded_strings(expression)
        ):
            return True
    return bool(_EXPRESS_USAGE.search(_mask_strings(source)))


def _reachable_modules(root: Path, files: Sequence[str]) -> set[str]:
    file_set = set(files)
    entrypoints = {"index.js", "lib/index.js", "lib/server.js"}
    entrypoints.update(relative for relative in files if relative.startswith("bin/"))
    reachable: set[str] = set()
    queue = [relative for relative in sorted(entrypoints) if relative in file_set]
    while queue:
        current = queue.pop()
        if current in reachable:
            continue
        reachable.add(current)
        try:
            source = _strip_comments((root / current).read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
        for specifier in _specifiers(source):
            target = _resolve_local(root, current, specifier)
            if target in file_set and target not in reachable:
                queue.append(target)
    return reachable


def _express_semantic_hits(
    root: Path,
    patterns: Sequence[str],
    *,
    deceptive: bool,
    http_layer: Sequence[str],
    rule_id: str,
) -> list[dict[str, str]]:
    files = _production_files(root, patterns)
    if not files:
        return []
    layer = tuple(str(item).replace("\\", "/").strip("/") for item in http_layer)
    reachable = _reachable_modules(root, files)
    hits: list[dict[str, str]] = []
    for relative in files:
        normalized = relative.replace("\\", "/")
        if any(
            normalized == entry or normalized.startswith(entry + "/")
            for entry in layer
            if entry
        ):
            # A rule explicitly delegated this directory to the F2P probe.
            continue
        try:
            source = _strip_comments(
                (root / relative).read_text(encoding="utf-8", errors="ignore")
            )
        except OSError:
            continue
        if not _express_usage(source):
            continue
        is_runtime = relative in reachable
        if deceptive != is_runtime:
            continue
        hits.append(
            {
                "path": normalized,
                "token": "express",
                "kind": "express_runtime" if deceptive else "express_leftover",
                "rule_id": rule_id,
            }
        )
    return hits


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
        if kind in {"express_runtime", "express_leftover"}:
            declared_layer = row.get("http_layer_paths", _DEFAULT_HTTP_LAYER)
            layer = (
                tuple(str(item) for item in declared_layer)
                if isinstance(declared_layer, Sequence)
                and not isinstance(declared_layer, (str, bytes))
                else _DEFAULT_HTTP_LAYER
            )
            hits.extend(
                _express_semantic_hits(
                    root,
                    tuple(patterns),
                    deceptive=kind == "express_runtime",
                    http_layer=layer,
                    rule_id=identifier,
                )
            )
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
