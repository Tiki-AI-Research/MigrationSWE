"""Language-neutral evaluation of declarative residue rules."""

from __future__ import annotations

import json
import os
import re
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Iterable, Mapping, Sequence

try:
    from .runtime_sinks import scan_runtime_sinks
except ImportError:  # Standalone Runtime Draft layout.
    from runtime_sinks import scan_runtime_sinks


# Directories that never carry candidate source and are skipped wholesale:
# version control metadata, virtualenvs and installed dependencies.  Build
# output directories (``build``/``dist``/``target``/``coverage``) are
# deliberately *not* excluded: a hidden fallback parked under ``src/build`` is
# still a hidden fallback, and dropping them from the scan let four attack
# samples (``src/build/…`` / ``src/dist/…``) escape detection entirely.
_EXCLUDED_PARTS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
}
_RULE_KINDS = {"literal", "dependency", "import", "file", "runtime_sink"}


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


def _walk_files(directory: Path) -> Iterable[Path]:
    """Walk a directory without descending into excluded or symlinked trees."""

    for current, directories, files in os.walk(directory):
        directories[:] = [
            name
            for name in directories
            if name not in _EXCLUDED_PARTS
            and not (Path(current) / name).is_symlink()
        ]
        for name in files:
            yield Path(current) / name


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
            collected.extend(_walk_files(path))
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
    {".js", ".mjs", ".cjs", ".jsx", ".ts", ".mts", ".cts", ".tsx"}
)


def _strip_js_comments(text: str) -> str:
    """Blank JavaScript/TypeScript comments while preserving code and strings.

    Comment prose must not be mistaken for real usage (a comment that merely
    mentions ``onSend`` or ``fastify`` is not a source fallback), yet genuine
    dynamic imports such as ``import('fastify')`` live in string literals and
    must still be detected.  Comments are replaced with spaces (newlines kept)
    so offsets stay stable; string and template-literal content is untouched.
    """

    characters = list(text)
    length = len(text)
    index = 0
    state = "code"
    quote = ""

    def blank(position: int) -> None:
        if 0 <= position < length and characters[position] != "\n":
            characters[position] = " "

    while index < length:
        character = text[index]
        if state == "code":
            if character == "/" and index + 1 < length and text[index + 1] == "/":
                while index < length and text[index] != "\n":
                    blank(index)
                    index += 1
                continue
            if character == "/" and index + 1 < length and text[index + 1] == "*":
                blank(index)
                blank(index + 1)
                index += 2
                while index < length and not (
                    text[index] == "*" and index + 1 < length and text[index + 1] == "/"
                ):
                    blank(index)
                    index += 1
                if index < length:
                    blank(index)
                    blank(index + 1)
                    index += 2
                continue
            if character in {"'", '"', "`"}:
                state = "string"
                quote = character
            index += 1
            continue
        if character == "\\":
            index += 2
            continue
        if character == quote:
            state = "code"
            quote = ""
        index += 1
    return "".join(characters)


def _read_for_matching(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    if path.suffix.casefold() in _JS_SUFFIXES:
        return _strip_js_comments(text)
    return text


def _literal_contains(path: Path, target: str) -> bool:
    return target.casefold() in _read_for_matching(path).casefold()


def _npm_alias_name(value: object) -> str | None:
    """Return the aliased package of an ``npm:``/``pnpm:``/``yarn:`` spec.

    ``"express": "npm:fastify@5"`` installs Fastify under the ``express`` name;
    comparing only dependency *names* would miss it.  Local file/link specs are
    intentionally ignored because they do not alias a registry package.
    """

    if not isinstance(value, str):
        return None
    spec = value.strip()
    lowered = spec.casefold()
    prefix = next(
        (item for item in ("npm:", "pnpm:", "yarn:") if lowered.startswith(item)),
        None,
    )
    if prefix is None:
        return None
    spec = spec[len(prefix):]
    if "@" in spec[1:]:
        spec = spec.rpartition("@")[0]
    return spec or None


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
            if not isinstance(entries, Mapping):
                continue
            for name, value in entries.items():
                if _target_matches(str(name), target):
                    return True
                alias = _npm_alias_name(value)
                if alias is not None and _target_matches(alias, target):
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
    text = _read_for_matching(path)
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
        patterns = row.get("paths", [])
        if (
            not isinstance(identifier, str)
            or not identifier
            or not isinstance(kind, str)
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
        if kind == "runtime_sink":
            category = row.get("category")
            modules = row.get("modules", [])
            legacy_runtimes = row.get("legacy_runtimes", [])
            if (
                not isinstance(category, str)
                or not category
                or not isinstance(modules, Sequence)
                or isinstance(modules, (str, bytes))
                or any(not isinstance(item, str) or not item for item in modules)
                or not isinstance(legacy_runtimes, Sequence)
                or isinstance(legacy_runtimes, (str, bytes))
                or any(
                    not isinstance(item, str) or not item
                    for item in legacy_runtimes
                )
            ):
                raise ValueError(
                    f"invalid runtime_sink residue rule: {identifier}"
                )
            for path in _paths(root, tuple(patterns)):
                for hit in scan_runtime_sinks(
                    path,
                    rule_id=identifier,
                    category=category,
                    modules=tuple(modules),
                    legacy_runtimes=tuple(legacy_runtimes),
                ):
                    hit["path"] = path.relative_to(root).as_posix()
                    hits.append(hit)
            continue
        target = row.get("target")
        if not isinstance(target, str) or not target:
            raise ValueError(f"invalid residue rule: {identifier}")
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
