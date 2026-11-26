"""Language-neutral evaluation of declarative residue rules."""

from __future__ import annotations

import functools
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
_RULE_KINDS = {"literal", "dependency", "import", "file", "step_lookup", "runtime_indirection"}


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


# ---------------------------------------------------------------------------
# Constant-string reconstruction.
#
# A residue rule must not be defeatable by *respelling* the forbidden token.
# A candidate can hide `execFileSync` / `child_process` by assembling the name
# from adjacent string fragments (`["execFile", "Sync"].join("")`, `"ja"+"va"`,
# `String.fromCharCode(...)`, or forwarding it through a `const`).  The literal
# substring test above only sees the raw source text, so it misses every such
# equivalent spelling.  The scanner below folds those constant expressions back
# into the concrete strings they evaluate to, so a rule target is matched
# against the *value* a piece of source produces rather than its spelling.
#
# Only fully-constant expressions are folded; anything involving an unknown
# identifier or a runtime value stays opaque (``_UNKNOWN``) and is ignored.  The
# folding is deliberately conservative so it never invents a token that the
# source cannot actually produce.

_UNKNOWN = object()

_TOKEN = re.compile(
    r"""
      (?P<ws>\s+)
    | (?P<comment>//[^\n]*|/\*.*?\*/)
    | (?P<string>'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*"|`(?:\\.|[^`\\])*`)
    | (?P<number>0[xX][0-9a-fA-F]+|\d+(?:\.\d+)?)
    | (?P<ident>[A-Za-z_$][A-Za-z0-9_$]*)
    | (?P<punct>[\[\]\(\)\{\}\.,;:+\-*/%=<>!&|?])
    """,
    re.DOTALL | re.VERBOSE,
)

_UNESCAPE = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "0": "\0",
    "\\": "\\",
    "'": "'",
    '"': '"',
    "`": "`",
}


def _unescape(body: str) -> str:
    out: list[str] = []
    index = 0
    length = len(body)
    while index < length:
        character = body[index]
        if character == "\\" and index + 1 < length:
            following = body[index + 1]
            if following in _UNESCAPE:
                out.append(_UNESCAPE[following])
                index += 2
                continue
            if following in ("x", "u"):
                digits = 2 if following == "x" else 4
                hex_value = body[index + 2 : index + 2 + digits]
                try:
                    out.append(chr(int(hex_value, 16)))
                    index += 2 + digits
                    continue
                except ValueError:
                    pass
            out.append(following)
            index += 2
            continue
        out.append(character)
        index += 1
    return "".join(out)


def _tokenize(text: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    for match in _TOKEN.finditer(text):
        kind = match.lastgroup
        value = match.group()
        if kind in {"ws", "comment"}:
            continue
        if kind == "string":
            quote = value[0]
            body = value[1:-1]
            if quote == "`" and "${" in body:
                # Interpolated template literals are runtime values, not constants.
                tokens.append(("opaque", ""))
                continue
            tokens.append(("string", _unescape(body)))
            continue
        if kind == "ident":
            tokens.append(("ident", value))
            continue
        if kind == "number":
            tokens.append(("number", value))
            continue
        tokens.append(("punct", value))
    return tokens


def _peek(tokens: list[tuple[str, str]], index: int) -> tuple[str, str]:
    return tokens[index] if 0 <= index < len(tokens) else ("", "")


def _parse_primary(tokens: list[tuple[str, str]], index: int, env: Mapping[str, object]):
    kind, value = _peek(tokens, index)
    if kind == "string":
        return value, index + 1
    if kind == "punct" and value == "[":
        items, index = _parse_sequence(tokens, index + 1, env, "]")
        if items is _UNKNOWN:
            return _UNKNOWN, index
        return list(items), index
    if kind == "punct" and value == "(":
        inner, index = _parse_concat(tokens, index + 1, env)
        if _peek(tokens, index) == ("punct", ")"):
            index += 1
        return inner, index
    if kind == "ident":
        if value == "String" and _peek(tokens, index + 1) == ("punct", "."):
            if _peek(tokens, index + 2) == ("ident", "fromCharCode") and _peek(
                tokens, index + 3
            ) == ("punct", "("):
                codes: list[str] = []
                cursor = index + 4
                while True:
                    item_kind, item_value = _peek(tokens, cursor)
                    if item_kind == "number":
                        try:
                            codes.append(chr(int(item_value, 0)))
                        except ValueError:
                            return _UNKNOWN, cursor
                        cursor += 1
                    elif item_kind == "string" and len(item_value) == 1:
                        codes.append(item_value)
                        cursor += 1
                    else:
                        return _UNKNOWN, cursor
                    if _peek(tokens, cursor) == ("punct", ","):
                        cursor += 1
                        continue
                    break
                if _peek(tokens, cursor) == ("punct", ")"):
                    cursor += 1
                return "".join(codes), cursor
        if value in env:
            return env[value], index + 1
        return _UNKNOWN, index + 1
    return _UNKNOWN, index + 1


def _parse_postfix(tokens: list[tuple[str, str]], index: int, env: Mapping[str, object]):
    value, index = _parse_primary(tokens, index, env)
    while _peek(tokens, index) == ("punct", "."):
        if _peek(tokens, index + 1) == ("ident", "join") and _peek(
            tokens, index + 2
        ) == ("punct", "("):
            separator, cursor = _parse_concat(tokens, index + 3, env)
            if _peek(tokens, cursor) == ("punct", ")"):
                cursor += 1
            if value is _UNKNOWN or not isinstance(value, list) or not isinstance(
                separator, str
            ):
                return _UNKNOWN, cursor
            if any(not isinstance(item, str) for item in value):
                return _UNKNOWN, cursor
            return separator.join(value), cursor
        return _UNKNOWN, index + 1
    return value, index


def _parse_concat(tokens: list[tuple[str, str]], index: int, env: Mapping[str, object]):
    value, index = _parse_postfix(tokens, index, env)
    while _peek(tokens, index) == ("punct", "+"):
        rhs, index = _parse_postfix(tokens, index + 1, env)
        if not isinstance(value, str) or not isinstance(rhs, str):
            return _UNKNOWN, index
        value = value + rhs
    return value, index


def _parse_sequence(
    tokens: list[tuple[str, str]], index: int, env: Mapping[str, object], closer: str
):
    items: list[object] = []
    while True:
        if _peek(tokens, index) == ("punct", closer):
            return items, index + 1
        item, index = _parse_concat(tokens, index, env)
        if item is _UNKNOWN:
            return _UNKNOWN, index
        items.append(item)
        if _peek(tokens, index) == ("punct", ","):
            index += 1
            continue
        if _peek(tokens, index) == ("punct", closer):
            return items, index + 1
        return _UNKNOWN, index


def _constant_environment(tokens: list[tuple[str, str]]) -> dict[str, object]:
    env: dict[str, object] = {}
    for _ in range(6):
        changed = False
        for index in range(len(tokens) - 2):
            kind, value = tokens[index]
            if kind != "ident" or value not in {"const", "let", "var"}:
                continue
            if tokens[index + 1][0] != "ident" or tokens[index + 2] != ("punct", "="):
                continue
            name = tokens[index + 1][1]
            resolved, _ = _parse_concat(tokens, index + 3, env)
            if resolved is _UNKNOWN:
                continue
            if name not in env or env[name] != resolved:
                env[name] = resolved
                changed = True
        if not changed:
            break
    return env


@functools.lru_cache(maxsize=256)
def _decode_string_constants(text: str) -> frozenset[str]:
    """Fold constant strings, including concatenation and const forwarding."""
    tokens = _tokenize(text)
    env = _constant_environment(tokens)

    found: set[str] = set()
    for kind, value in tokens:
        if kind == "string":
            found.add(value)
    for index in range(len(tokens)):
        resolved, _ = _parse_concat(tokens, index, env)
        if resolved is _UNKNOWN:
            continue
        if isinstance(resolved, str):
            found.add(resolved)
        elif isinstance(resolved, list):
            found.update(item for item in resolved if isinstance(item, str))
    return frozenset(found)


def _literal_contains(path: Path, target: str) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    if target.casefold() in text.casefold():
        return True
    needle = target.casefold()
    return any(needle in candidate.casefold() for candidate in _decode_string_constants(text))


def _rust_build_only(text: str) -> bool:
    """A synchronous cargo build loads Rust; it does not run legacy TS helpers.

    This narrow exception requires literal compiler commands and argument arrays.
    Unknown/computed process invocations remain forbidden, including when mixed
    with a legitimate build. It never exempts `node legacy.js` or cargo run.
    """
    if re.search(r"\[\s*[A-Za-z_$][\w$]*\s*\]\s*\(", text):
        return False
    calls = list(re.finditer(
        r"\b(execFileSync|execFile|spawnSync|spawn|execSync|exec|fork|Worker)\s*(?:['\"]\s*\])?\s*\(", text
    ))
    if not calls:
        return False
    for call in calls:
        if call.group(1) not in {"execFileSync", "execFile", "spawnSync", "spawn"}:
            return False
        args = text[call.end():]
        if not re.match(r'''\s*['"]cargo['"]\s*,\s*\[\s*['"]build['"]''', args):
            return False
    return True


def _step_lookup_contains(path: Path, target: str) -> bool:
    text = path.read_text(encoding="utf-8", errors="ignore")
    # Searching module paths or locating an addon is facade wiring. A step
    # search reads .id, or directly operates on steps / step ids. Do not infer
    # production step logic from an unrelated Array.find or String.indexOf.
    method = target.strip(". (")
    # Fold the actual computed member expression. An unrelated Rust operation
    # name "indexOf" plus steps[Number(nativeIndex)] is only ABI marshalling.
    env = _constant_environment(_tokenize(text))
    receivers = r"(?:\b([\w$]+)|\(\s*(steps)\s+as\s+[^)]+\))\s*\["
    for match in re.finditer(receivers, text):
        receiver = match.group(1) or match.group(2)
        if not re.search(r"step|^ids$", receiver, re.I):
            continue
        tokens = _tokenize(text[match.end():])
        member, end = _parse_concat(tokens, 0, env)
        if (isinstance(member, str) and member.casefold() == method.casefold()
                and _peek(tokens, end) == ("punct", "]")
                and _peek(tokens, end + 1) == ("punct", "(")):
            return True
    for match in re.finditer(r"\b([\w$]+)\s*\.\s*" + re.escape(method) + r"\s*\(", text):
        receiver = match.group(1)
        tail = text[match.end():].split(";", 1)[0]
        if re.search(r"step|^ids$", receiver, re.I) or re.search(r"\.\s*id\b", tail):
            return True
    return False


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
    if kind == "step_lookup":
        return _step_lookup_contains(path, target)
    if kind == "runtime_indirection":
        return _literal_contains(path, target) and not _rust_build_only(path.read_text(encoding="utf-8", errors="ignore"))
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
