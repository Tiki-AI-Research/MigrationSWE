"""Language-neutral evaluation of declarative residue rules."""

from __future__ import annotations

import json
import re
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Iterable, Mapping, Sequence

try:
    from .javascript_analysis import analyze_javascript
except ImportError:
    from javascript_analysis import analyze_javascript


_EXCLUDED_PARTS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
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
        # Generated roots do not imply that identically named directories
        # inside production sources are generated or safe to ignore.
        and relative.parts[0] not in _EXCLUDED_PARTS | {"target", "dist", "build", "coverage"}
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
    # Node's built-in modules have both bare and node:-qualified specifiers.
    # Treat them as the same capability so a prefix cannot evade policy.
    if actual.startswith("node:"):
        actual = actual[5:]
    if expected.startswith("node:"):
        expected = expected[5:]
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
    # Account for statically concatenated module-name fragments (for example
    # require('mar' + 'ked') and const name = 'mar' + 'ked'). This remains a
    # conservative lexical check; the caller scopes it to production sources.
    concatenation = re.compile(
        r"(['\"])([^'\"]*)\1(?:\s*\+\s*(['\"])([^'\"]*)\3)+"
    )
    for match in concatenation.finditer(text):
        fragments = re.findall(r"['\"]([^'\"]*)['\"]", match.group(0))
        candidates.append("".join(fragments))
    candidates.extend(str(item) for item in analyze_javascript(text)["imports"])
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
