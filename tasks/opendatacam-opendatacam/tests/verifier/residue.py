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


_STRING_LITERAL = r"""(?:'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*")"""


_REGEX_PREV_CHARS = set("(,=:[!&|?{};+-*%^<>~")
_REGEX_PREV_WORDS = {
    "return", "typeof", "instanceof", "in", "of", "new", "delete", "void",
    "case", "do", "else", "yield", "await", "throw",
}


def _strip_comments(text: str) -> str:
    """Remove // and /* */ comments from JavaScript source.

    A residue token that only appears inside a comment is not a real leftover
    of the source framework, so it must not produce a hit.  String, template
    and regex literals are preserved (regex literals may contain quotes and
    slashes, which a naive scanner would misread).
    """

    out: list[str] = []
    i = 0
    n = len(text)
    quote: str | None = None
    prev: str | None = None

    def trailing_word() -> str:
        j = len(out) - 1
        chars: list[str] = []
        while j >= 0 and (out[j].isalnum() or out[j] in "_$"):
            chars.append(out[j])
            j -= 1
        return "".join(reversed(chars))

    while i < n:
        ch = text[i]
        if quote is not None:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            i += 2
            while i < n and text[i] not in ("\n", "\r"):
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        if ch == "/":
            previous = prev
            word = trailing_word()
            if previous is None or previous in _REGEX_PREV_CHARS or word in _REGEX_PREV_WORDS:
                out.append(ch)
                i += 1
                in_class = False
                while i < n:
                    current = text[i]
                    if current == "\\" and i + 1 < n:
                        out.append(current)
                        out.append(text[i + 1])
                        i += 2
                        continue
                    if current == "[":
                        in_class = True
                    elif current == "]":
                        in_class = False
                    elif current == "/" and not in_class:
                        out.append(current)
                        i += 1
                        break
                    elif current in ("\n", "\r"):
                        break
                    out.append(current)
                    i += 1
                while i < n and text[i].isalpha():
                    out.append(text[i])
                    i += 1
                prev = "/"
                continue
        out.append(ch)
        if not ch.isspace():
            prev = ch
        i += 1
    return "".join(out)


def _fold_constants(text: str) -> str:
    """Fold a bounded set of constant string forms so equivalent spellings match.

    ``require('expr' + 'ess')``, ``require(String('express'))``, static template
    literals, ``String.fromCharCode(...)``, ``['ex','press'].join('')`` and a
    string held in a local ``const`` all resolve to the same module/API name as
    the literal form; a value/semantic check must treat them identically.
    """

    concat = re.compile(
        r"(" + _STRING_LITERAL + r")\s*\+\s*(" + _STRING_LITERAL + r")"
    )
    wrapped = re.compile(r"\bString\(\s*(" + _STRING_LITERAL + r")\s*\)")
    static_template = re.compile(r"`([^`\\$]*)`")
    from_char_code = re.compile(r"\bString\.fromCharCode\(\s*([0-9]+(?:\s*,\s*[0-9]+)*)\s*\)")
    joined_array = re.compile(
        r"\[\s*((?:" + _STRING_LITERAL + r"\s*,\s*)*" + _STRING_LITERAL + r"\s*,?)\s*\]\s*\.join\(\s*("
        + _STRING_LITERAL
        + r"|\s*)\s*\)"
    )
    buffer_hex = re.compile(
        r"Buffer\.from\(\s*(?:'([0-9a-fA-F]+)'|\"([0-9a-fA-F]+)\")\s*,\s*['\"]hex['\"]\s*\)\.toString\(\s*(?:['\"]utf-?8['\"]\s*)?\)"
    )
    buffer_b64 = re.compile(
        r"Buffer\.from\(\s*(?:'([A-Za-z0-9+/=]+)'|\"([A-Za-z0-9+/=]+)\")\s*,\s*['\"]base64['\"]\s*\)\.toString\(\s*(?:['\"]utf-?8['\"]\s*)?\)"
    )
    unicode_escape = re.compile(r"\\u([0-9a-fA-F]{4})|\\x([0-9a-fA-F]{2})")

    def _decode_buffer(match: "re.Match[str]", base: int) -> str:
        payload = match.group(1) if match.group(1) is not None else match.group(2)
        try:
            if base == 16:
                decoded = bytes.fromhex(payload).decode("utf-8", "ignore")
            else:
                import base64 as _b64
                decoded = _b64.b64decode(payload).decode("utf-8", "ignore")
        except (ValueError, TypeError):
            return match.group(0)
        escaped = decoded.replace("\\", "\\\\").replace("'", "\\'")
        return "'" + escaped + "'"

    def _decode_escape(match: "re.Match[str]") -> str:
        try:
            return chr(int(match.group(1) or match.group(2), 16))
        except (ValueError, TypeError):
            return match.group(0)

    def _char_code(match: "re.Match[str]") -> str:
        try:
            return "'" + "".join(chr(int(part)) for part in match.group(1).split(",")) + "'"
        except (ValueError, TypeError):
            return match.group(0)

    def _join_array(match: "re.Match[str]") -> str:
        parts = re.findall(_STRING_LITERAL, match.group(1))
        separator = match.group(2)
        separator = separator[1:-1] if separator else ""
        body = separator.join(part[1:-1] for part in parts)
        escaped = body.replace("\\", "\\\\").replace("'", "\\'")
        return "'" + escaped + "'"

    assignment = re.compile(
        r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(" + _STRING_LITERAL + r")\s*;"
    )
    require_alias = re.compile(
        r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:require|module\.require)\s*;"
    )

    previous = None
    for _ in range(10):
        if previous == text:
            break
        previous = text
        text = concat.sub(lambda m: _join_literals(m.group(1), m.group(2)), text)
        text = wrapped.sub(lambda m: m.group(1), text)
        text = static_template.sub(lambda m: "'" + m.group(1) + "'", text)
        text = from_char_code.sub(_char_code, text)
        text = joined_array.sub(_join_array, text)
        text = buffer_hex.sub(lambda m: _decode_buffer(m, 16), text)
        text = buffer_b64.sub(lambda m: _decode_buffer(m, 64), text)
        text = unicode_escape.sub(_decode_escape, text)
        # Variable indirection (including split parts): a module name held in
        # (possibly several) local variables resolves to the same module as the
        # literal spelling once each variable is substituted by its value.
        mapping = {match.group(1): match.group(2) for match in assignment.finditer(text)}
        for name, literal in mapping.items():
            text = re.sub(r"\b" + re.escape(name) + r"\b", literal, text)
        # ``const X = require; X('express')`` is the same module resolution as
        # ``require('express')``.
        for name in require_alias.findall(text):
            text = re.sub(r"\b" + re.escape(name) + r"\s*\(", "require(", text)
    return text


def _join_literals(left: str, right: str) -> str:
    def inner(value: str) -> str:
        return value[1:-1]

    body = inner(left) + inner(right)
    escaped = body.replace("\\", "\\\\").replace("'", "\\'")
    return "'" + escaped + "'"


_MAX_NORMALIZE_BYTES = 512_000
_MAX_SCAN_BYTES = 2_000_000
_FOLD_HINT = re.compile(r"require|import|press", re.IGNORECASE)


def _read_text(path: Path) -> str | None:
    try:
        if path.stat().st_size > _MAX_SCAN_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


_BLOB_LITERAL = re.compile(r"(['\"])([A-Za-z0-9+/=]{24,})\1")


def _printable_text(data: bytes) -> str | None:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not text:
        return None
    printable = sum(1 for ch in text if ch.isprintable() or ch in "\r\n\t")
    return text if printable * 10 >= len(text) * 9 else None


def _decode_blobs(text: str) -> list[str]:
    """Return decoded views of an opaque payload (base64 / hex).

    A hidden source-runtime fallback may keep the original implementation as an
    opaque blob instead of a literal ``require('express')`` in source -- either
    as a whole stand-alone payload file or as a base64 string literal embedded
    in the entry, which is then decoded and handed to a child process, FFI probe
    or script engine.  Decoding both exposes the same module identity for a
    value/semantic check.
    """

    decoded: list[str] = []
    seen: set[str] = set()

    def add(candidate: str | None) -> None:
        if candidate and candidate not in seen:
            seen.add(candidate)
            decoded.append(candidate)

    def from_base64(payload: str) -> str | None:
        import base64 as _b64

        try:
            raw = _b64.b64decode(payload + "=" * (-len(payload) % 4), validate=False)
        except (ValueError, TypeError):
            return None
        return _printable_text(raw)

    def from_hex(payload: str) -> str | None:
        try:
            return _printable_text(bytes.fromhex(payload))
        except (ValueError, TypeError):
            return None

    compact = re.sub(r"\s+", "", text or "")
    if len(compact) >= 16 and re.fullmatch(r"[A-Za-z0-9+/=]+", compact):
        add(from_base64(compact))
    if len(compact) >= 16 and re.fullmatch(r"[0-9a-fA-F]+", compact) and len(compact) % 2 == 0:
        add(from_hex(compact))
    for match in _BLOB_LITERAL.finditer(text or ""):
        payload = match.group(2)
        add(from_base64(payload))
        add(from_hex(payload) if len(payload) % 2 == 0 else None)
    return decoded


def _normalize(text: str) -> str:
    """Strip comments, then fold constants when it can matter.

    Folding is skipped for very large files (for example lockfiles) or files
    with no import/require/express hint, so a whole-repo scan stays bounded.
    """

    text = _strip_comments(text)
    if len(text) > _MAX_NORMALIZE_BYTES or not _FOLD_HINT.search(text):
        return text
    return _fold_constants(text)


def _target_matches(value: str, target: str) -> bool:
    actual = value.casefold().strip()
    expected = target.casefold().strip()
    return actual == expected or fnmatchcase(actual, expected)


def _views(path: Path) -> list[str]:
    """Normalized textual views of a file: the source itself plus any decoded
    opaque payload it carries."""

    text = _read_text(path)
    if text is None:
        return []
    views = [_normalize(text)]
    for decoded in _decode_blobs(text):
        views.append(_normalize(decoded))
    return views


def _literal_contains(path: Path, target: str) -> bool:
    folded = target.casefold()
    return any(folded in view.casefold() for view in _views(path))


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
    patterns = (
        re.compile(r"\bfrom\s+([A-Za-z0-9_./@+-]+)\s+import\b"),
        re.compile(r"\bimport\s+([A-Za-z0-9_./@+-]+)"),
        re.compile(r"\bfrom\s*['\"]([^'\"]+)['\"]"),
        re.compile(r"\brequire\s*\(\s*['\"]([^'\"]+)['\"]\s*\)"),
        re.compile(r"\brequire\s+['\"]([^'\"]+)['\"]"),
        re.compile(r"\b(?:use|using)\s+([A-Za-z0-9_.\\]+)"),
    )
    for view in _views(path):
        candidates: list[str] = []
        for pattern in patterns:
            candidates.extend(match.group(1) for match in pattern.finditer(view))
        if any(
            _target_matches(candidate, target)
            or _target_matches(re.split(r"[./\\]", candidate, maxsplit=1)[0], target)
            for candidate in candidates
        ):
            return True
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
