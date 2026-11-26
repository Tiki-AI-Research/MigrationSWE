"""Language-neutral evaluation of declarative residue rules."""

from __future__ import annotations

import json
import re
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Iterable, Mapping, Sequence


# Only VCS metadata and dependency caches are skipped.  Build-output directory
# names (``dist``/``build``/``coverage``/``target``) are deliberately *not*
# excluded: a candidate can otherwise hide a deceptive source fallback inside
# such a directory and have the scan blind to it, which is exactly the
# ``adv-dfb-dist-bridge`` bypass.
_EXCLUDED_PARTS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
}
_RULE_KINDS = {"literal", "dependency", "import", "file", "regex", "stub", "go_test_skip"}


def go_test_skip_count(text: str) -> int:
    """Count test-disabling calls, distinguishing package functions from methods.

    An imported package's Skip function (for example itertools.Skip) advances
    an iterator, not the test runner. Keep counting renamed testing receivers
    and helpers, including an imported name shadowed by a local variable.
    """
    code = _blank_go_literals(text)
    packages: set[str] = set()
    for declaration in re.finditer(r"\bimport\s*(?:\([^)]*\)|[^\n;]+)", code):
        raw = text[declaration.start():declaration.end()]
        for match in re.finditer(r'(?:(\w+|\.)\s+)?"([^"\n]+)"', raw):
            quote = declaration.start() + raw.index('"', match.start())
            if code[quote] != '"':  # Ignore quoted prose inside import comments.
                continue
            alias, path = match.groups()
            name = alias or path.rsplit("/", 1)[-1]
            if name not in {"_", "."}:
                packages.add(name)
    count = 0
    for match in re.finditer(r"\b([A-Za-z_]\w*)\s*\.\s*Skip(?:Now|f)?\s*\(", code):
        name = match.group(1)
        # A package cannot have a testing.T method. Conservatively retain any
        # receiver name also used in a declaration/assignment in this file.
        shadow = re.search(
            rf"(?:\b(?:var|const)\s+{re.escape(name)}\b|"
            rf"\b{re.escape(name)}\s*(?:,\s*\w+\s*)*:=|"
            rf"[(,]\s*{re.escape(name)}\s+(?:\*|\[|[A-Za-z_]))",
            code,
        )
        if name not in packages or shadow:
            count += 1
    return count


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


def _blank_go_literals(text: str) -> str:
    """Blank Go comments, string and rune literals while keeping offsets.

    A ``func`` keyword or brace inside a comment/string must not be mistaken for
    real code, so the structural scanner runs on this sanitised view.
    """

    characters = list(text)
    length = len(text)
    state = "normal"
    quote = ""
    index = 0
    while index < length:
        character = text[index]
        if state == "normal":
            if text.startswith("//", index):
                while index < length and text[index] != "\n":
                    characters[index] = " "
                    index += 1
                continue
            if text.startswith("/*", index):
                characters[index] = " "
                characters[index + 1] = " "
                index += 2
                while index < length and not text.startswith("*/", index):
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
        if quote != "`" and character == "\\":
            index += 2
            continue
        if character == quote:
            state = "normal"
            index += 1
            continue
        if character != "\n":
            characters[index] = " "
        index += 1
    return "".join(characters)


def _skip_go_group(text: str, start: int, opener: str, closer: str) -> int:
    """Return the index just past the group opening at ``text[start] == opener``."""

    depth = 0
    index = start
    length = len(text)
    while index < length:
        character = text[index]
        if character == opener:
            depth += 1
        elif character == closer:
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return length


def _go_function_bodies(text: str):
    """Yield ``(name, body)`` for every Go function in a sanitised source text.

    Braces that belong to ``struct{...}``/``interface{...}`` literals in the
    signature are skipped, so a composite return type such as ``map[T]struct{}``
    is not mistaken for the function body.
    """

    index = 0
    length = len(text)
    while True:
        keyword = re.search(r"\bfunc\b", text[index:])
        if not keyword:
            return
        cursor = index + keyword.end()
        while cursor < length and text[cursor].isspace():
            cursor += 1
        if cursor < length and text[cursor] == "(":  # method receiver
            cursor = _skip_go_group(text, cursor, "(", ")")
            while cursor < length and text[cursor].isspace():
                cursor += 1
        name = re.match(r"[A-Za-z_]\w*", text[cursor:])
        if not name:
            index = cursor
            continue
        function_name = name.group(0)
        cursor += name.end()
        while cursor < length and text[cursor].isspace():
            cursor += 1
        if cursor < length and text[cursor] == "[":  # type parameters
            cursor = _skip_go_group(text, cursor, "[", "]")
            while cursor < length and text[cursor].isspace():
                cursor += 1
        if cursor >= length or text[cursor] != "(":
            index = cursor
            continue
        cursor = _skip_go_group(text, cursor, "(", ")")
        while cursor < length:
            if text[cursor] == "{":
                back = cursor - 1
                while back >= 0 and text[back].isspace():
                    back -= 1
                token = ""
                while back >= 0 and (text[back].isalnum() or text[back] == "_"):
                    token = text[back] + token
                    back -= 1
                if token in {"struct", "interface"}:
                    cursor = _skip_go_group(text, cursor, "{", "}")
                    continue
                end = _skip_go_group(text, cursor, "{", "}")
                yield function_name, text[cursor + 1:end - 1]
                index = end
                break
            cursor += 1
        else:
            index = cursor


def _is_go_stub_body(body: str) -> bool:
    """True when a Go body is empty or a single ``panic(...)`` call.

    An unfinished migration frequently ships a stub whose body is empty or a
    lone ``panic``; matching that *shape* catches residues whose panic text is
    reworded to avoid a literal token such as ``not implemented``.
    """

    stripped = body.strip().rstrip(";").strip()
    if not stripped:
        return True
    match = re.match(r"panic\s*\(", stripped)
    if not match:
        return False
    return _skip_go_group(stripped, match.end() - 1, "(", ")") == len(stripped)


def _stub_contains(path: Path, _target: str) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    sanitised = _blank_go_literals(text)
    return any(_is_go_stub_body(body) for _name, body in _go_function_bodies(sanitised))


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
        # Go modules: `require <module> vX.Y.Z` / `replace <module> [vX] => ...`
        # and the block forms where the module path is followed by a version
        # token (``require (\n\t<module> vX.Y.Z\n)``).
        re.compile(r"^\s*require\s+([A-Za-z0-9._~/-]+)\s+v[0-9]"),
        re.compile(r"^\s*replace\s+([A-Za-z0-9._~/-]+)(?:\s+v[0-9][^\s]*)?\s*=>"),
        re.compile(r"^\s*([A-Za-z0-9._~/-]+)\s+v[0-9][^\s]*$"),
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


def _regex_contains(path: Path, target: str) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    try:
        return re.search(target, text) is not None
    except re.error:
        return False


def _matches(root: Path, path: Path, *, kind: str, target: str) -> bool:
    if kind == "go_test_skip":
        return go_test_skip_count(path.read_text(encoding="utf-8", errors="ignore")) > 0
    if kind == "literal":
        return _literal_contains(path, target)
    if kind == "dependency":
        return _dependency_contains(path, target)
    if kind == "import":
        return _import_contains(path, target)
    if kind == "regex":
        return _regex_contains(path, target)
    if kind == "stub":
        return _stub_contains(path, target)
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
            if row.get("scope") == "production_go" and (
                path.suffix != ".go" or path.name.endswith("_test.go")
            ):
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


__all__ = ["classify_residue_rules", "scan_residue_rules", "go_test_skip_count"]
