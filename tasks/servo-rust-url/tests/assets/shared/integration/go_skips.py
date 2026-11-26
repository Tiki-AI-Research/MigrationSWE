"""Source views for Go skip checks, including inactive platform guards.

Only simple comparisons with the real runtime.GOOS (or a package constant
defined by one) are evaluated. Unknown conditions stay visible. The native
JUnit skip sink remains authoritative for any skip that actually executes.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


_LEXEME = re.compile(
    r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|'
    r"'(?:\\.|[^'\\])*'|`[^`]*`",
)
_PLATFORM = {"linux": "linux", "darwin": "darwin", "win32": "windows"}


def _blank(value: str) -> str:
    return "".join("\n" if c == "\n" else " " for c in value)


def _view(source: str, *, strings: bool) -> str:
    def replace(match: re.Match[str]) -> str:
        value = match.group()
        if value.startswith(("//", "/*")):
            return _blank(value)
        return value if strings else _blank(value)

    return _LEXEME.sub(replace, source)


def _runtime_aliases(source: str) -> set[str]:
    source = _view(source, strings=True)
    names: set[str] = set()
    for declaration in re.finditer(r'\bimport\s*(\([^)]*\)|(?:\w+\s+)?"[^"\n]*")', source):
        for entry in re.finditer(r'(?:\b(\w+)\s+)?"runtime"', declaration.group(1)):
            names.add(entry.group(1) or "runtime")
    return names


def _platform_condition(expression: str, aliases: set[str], flags: dict[str, bool]) -> bool | None:
    expression = expression.strip()
    if expression in flags:
        return flags[expression]
    if expression.startswith("!") and expression[1:].strip() in flags:
        return not flags[expression[1:].strip()]
    match = re.fullmatch(r'(\w+)\s*\.\s*GOOS\s*(==|!=)\s*"([a-z0-9]+)"', expression)
    actual = _PLATFORM.get(sys.platform)
    if match is None or match[1] not in aliases or actual is None:
        return None
    equal = actual == match[3]
    return equal if match[2] == "==" else not equal


def _package_flags(source: str, path: Path | None) -> dict[str, bool]:
    package = re.search(r'\bpackage\s+(\w+)', _view(source, strings=False))
    sources = [source]
    if path is not None and package is not None:
        sources = []
        for sibling in sorted(path.parent.glob("*.go")):
            if sibling.is_symlink():
                continue
            text = source if sibling == path else sibling.read_text(encoding="utf-8", errors="replace")
            sibling_package = re.search(r'\bpackage\s+(\w+)', _view(text, strings=False))
            # Do not infer constants from files with unknown build conditions.
            if sibling_package and sibling_package[1] == package[1] and not re.search(r'(?m)^//(?:go:build| \+build)\b', text):
                sources.append(text)
    found: dict[str, list[bool]] = {}
    for text in sources:
        aliases = _runtime_aliases(text)
        for match in re.finditer(r'\bconst\s+(\w+)\s*=\s*([^\n;]+)', _view(text, strings=True)):
            value = _platform_condition(match[2], aliases, {})
            if value is not None:
                found.setdefault(match[1], []).append(value)
    flags = {name: values[0] for name, values in found.items() if len(values) == 1}
    # A shadowed name is not proven to refer to the package constant. Only
    # direct guard uses are exempt; unknown uses retain the static check.
    code = _view(source, strings=False)
    for name in list(flags):
        for match in re.finditer(r'\b' + re.escape(name) + r'\b', code):
            prefix = code[:match.start()]
            if not re.search(r'\bif\s*!?\s*$', prefix) and not re.search(r'\bconst\s*$', prefix):
                del flags[name]
                break
    return flags


def scannable_source(source: str, path: Path | None = None) -> str:
    """Blank inactive platform bodies, comments, and non-reflection strings."""

    strings_view = _view(source, strings=True)
    code = _view(source, strings=False)
    aliases = _runtime_aliases(source)
    flags = _package_flags(source, path)
    characters = list(source)
    for guard in re.finditer(r'\bif\s+([^{};\n]+)\{', strings_view):
        if code[guard.start():guard.start() + 2] != "if" or code[guard.end() - 1] != "{":
            continue
        if _platform_condition(guard[1], aliases, flags) is not False:
            continue
        opening = guard.end() - 1
        depth, end = 1, opening + 1
        while end < len(code) and depth:
            if code[end] == "{":
                depth += 1
            elif code[end] == "}":
                depth -= 1
            end += 1
        if depth == 0:
            characters[opening + 1:end - 1] = _blank(source[opening + 1:end - 1])
    filtered = "".join(characters)

    def replace(match: re.Match[str]) -> str:
        value = match.group()
        # Preserve the method-name literal required by reflective skip checks.
        if value in ('"Skip"', '"Skipf"', '"SkipNow"', '`Skip`', '`Skipf`', '`SkipNow`'):
            return value.replace("`", '"')
        return _blank(value)

    return _LEXEME.sub(replace, filtered)


def main() -> int:
    root = Path(sys.argv[1])
    sources = {}
    for path in sorted(root.rglob("*_test.go")):
        if path.is_symlink() or any(part in {".git", "vendor", ".gocache", ".gopath"} for part in path.relative_to(root).parts):
            continue
        sources[path.relative_to(root).as_posix()] = scannable_source(path.read_text(encoding="utf-8"), path)
    print(json.dumps(sources))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
