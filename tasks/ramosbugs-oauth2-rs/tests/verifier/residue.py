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
_GO_TEST_NAME = re.compile(r"(?:^|/)[^/]*_test\.go$")
_GENERIC_TEST_NAME = re.compile(
    r"(?:^|/)(?:test[^/]*|[^/]*(?:\.test|\.spec)\.[^/]*|[^/]*_(?:test|spec)\.[^/]+)$",
    re.IGNORECASE,
)


def _is_test_file(path: Path) -> bool:
    """Target-language test source predicate.

    Used to keep repo-level residue rules on production code: a migrated
    repository-wide scan must catch residue in a *new* production file, while a
    candidate's own test files stay outside the residue scope (they are covered
    by ``tests_not_tampered`` instead).
    """

    relative = path.as_posix()
    if path.suffix == ".go":
        return bool(_GO_TEST_NAME.search(relative))
    return bool(_GENERIC_TEST_NAME.search(relative))


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


def _blank_comments(text: str) -> str:
    """Blank ``//`` / ``/* */`` / ``#`` comments, preserving line structure.

    Residue matching must reflect executable code, not prose: a comment that
    merely *mentions* a source symbol is not evidence that the symbol is still
    used.  String literals (including Go raw backtick strings) are copied
    verbatim so a ``//`` inside them is not mistaken for a comment.
    """

    out: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        following = text[index + 1] if index + 1 < length else ""
        if char in "\"'`":
            quote = char
            out.append(char)
            index += 1
            while index < length:
                current = text[index]
                if current == "\\" and quote != "`" and index + 1 < length:
                    out.append(current)
                    out.append(text[index + 1])
                    index += 2
                    continue
                out.append(current)
                index += 1
                if current == quote:
                    break
            continue
        if char == "/" and following == "/":
            while index < length and text[index] != "\n":
                out.append(" ")
                index += 1
            continue
        if char == "/" and following == "*":
            out.append("  ")
            index += 2
            while index < length and not (
                text[index] == "*" and index + 1 < length and text[index + 1] == "/"
            ):
                out.append("\n" if text[index] == "\n" else " ")
                index += 1
            if index < length:
                out.append("  ")
                index += 2
            continue
        if char == "#":
            while index < length and text[index] != "\n":
                out.append(" ")
                index += 1
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _blank_python_noise(text: str) -> str:
    import ast
    import io as _io
    import tokenize as _tokenize

    lines = text.splitlines()
    try:
        for token in _tokenize.generate_tokens(_io.StringIO(text).readline):
            if token.type == _tokenize.COMMENT:
                row, column = token.start
                if 1 <= row <= len(lines):
                    lines[row - 1] = lines[row - 1][:column]
    except (IndentationError, SyntaxError, _tokenize.TokenError):
        pass
    blanked = "\n".join(lines)
    try:
        tree = ast.parse(text)
    except (IndentationError, SyntaxError, ValueError):
        return blanked
    out = blanked.splitlines()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", [])
        if not body:
            continue
        first = body[0]
        value = getattr(first, "value", None)
        if (
            isinstance(first, ast.Expr)
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        ):
            start = getattr(first, "lineno", 1)
            end = getattr(first, "end_lineno", start) or start
            for line_number in range(start, end + 1):
                if 1 <= line_number <= len(out):
                    out[line_number - 1] = ""
    return "\n".join(out)


def _code_text(path: Path) -> str:
    """Return source text with comments (and Python docstrings) blanked out.

    Literal/import residue matching must reflect executable code, not prose: a
    comment or docstring that merely *mentions* a source symbol (for example
    ``// Go SetBasicAuth over the raw ... is a failing migration``) is not
    evidence that the symbol is still used.  Line structure is preserved so the
    other rule kinds keep their positions.
    """

    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    if path.suffix == ".py":
        return _blank_python_noise(text)
    return _blank_comments(text)


def _literal_contains(path: Path, target: str) -> bool:
    text = _code_text(path)
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
    text = _code_text(path)
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


# --------------------------------------------------------------------------- #
# Semantic hidden-fallback detection (target-language sinks)
# --------------------------------------------------------------------------- #
# A migration must not keep the source runtime reachable from the migrated code
# by spawning a process, loading a native/shared object or a plugin.  A scan of
# literal keywords is blind to import aliases (``o "os"`` + ``o.StartProcess``),
# comment/whitespace splitting and struct-literal forms, so these rules are
# evaluated with a mechanism-first, fail-closed analysis over the *whole*
# candidate repository rather than a fixed directory or file whitelist:
#
# * every production ``.go`` file (``_test.go`` excluded) is scanned, whatever
#   directory it lives in -- a bridged file cannot hide in ``build``/``dist``/
#   ``target`` or outside the migrated package;
# * comments and string literals are blanked before matching, so prose that
#   merely names a mechanism does not count;
# * the candidate's own import bindings are resolved (``import o "os"``,
#   ``import ex "os/exec"``, ...) and the *qualified selectors* are matched by
#   resolved package path, so the alias the candidate chooses is irrelevant;
# * the residual delegation mechanisms are matched directly: cgo (``import
#   "C"``), the ``//go:linkname`` directive and ``net/rpc``.
_GO_SINK_KINDS = frozenset({"go-hidden-fallback"})
# Only non-vendored, non-cache directories are skipped: the mechanism scan must
# not inherit the build-output exclusions used by the ordinary residue rules.
_GO_SINK_EXCLUDED_PARTS = frozenset(
    {".git", "vendor", "node_modules", ".gomodcache", ".gocache"}
)
# package path -> qualified selectors that implement runtime delegation.
# The mapping is *mechanism-first*: the resolved package path selects the
# family, so an import alias (``o "os"``, ``ex "os/exec"``, ``u "golang.org/
# x/sys/unix"``) cannot hide a sink.  ``os.Process.Start`` and ``syscall.Exec``
# both only need the ``os``/``syscall`` packages, so a plain import of those is
# deliberately *not* a failure by itself.
_GO_SINK_SELECTORS = {
    "os": ("StartProcess",),
    "os/exec": ("Command", "CommandContext", "Cmd"),
    "syscall": ("Exec", "ForkExec", "StartProcess"),
    "plugin": ("Open",),
    "golang.org/x/sys/unix": ("Exec", "Execve", "ForkExec", "PosixSpawn"),
}
# ``os.Process.Start`` is a method on an ``os`` value (written as
# ``os.Process.Start`` or ``(*os.Process).Start``), so it is matched by the
# ``Process`` type reference; the ``os`` package is otherwise benign.
_GO_PROCESS_START = re.compile(r"\bProcess\b[^;\n]*?\.\s*Start\s*\(")
# Raw syscall entry points: these are a delegation mechanism only when the
# program also reaches for an exec-family syscall number.  ``syscall.Getpid``
# and friends must stay allowed.
_GO_SYSCALL_CALL_SELECTORS = ("Syscall", "Syscall6", "RawSyscall", "RawSyscall6")
# Exec/process-family syscall constants.  Their presence alongside a raw
# syscall entry point is the mechanism; the exec subset below is itself a
# mechanism even with no visible import (``SYS_EXECVE`` never has a benign use
# in this migration).
_GO_EXEC_CONSTANTS = (
    "SYS_EXECVE",
    "SYS_EXECVEAT",
    "SYS_FORK",
    "SYS_VFORK",
    "SYS_POSIX_SPAWN",
)
_GO_BARE_EXEC_CONSTANTS = ("SYS_EXECVE", "SYS_EXECVEAT", "SYS_POSIX_SPAWN")
_GO_EXEC_CONSTANT_RE = re.compile(
    r"\b(?:%s)\b" % "|".join(_GO_EXEC_CONSTANTS), re.IGNORECASE
)
_GO_BARE_EXEC_CONSTANT_RE = re.compile(
    r"\b(?:%s)\b" % "|".join(_GO_BARE_EXEC_CONSTANTS), re.IGNORECASE
)
# Importing any of these packages is itself the delegation mechanism, so a bare
# import fails closed even before a selector is used.  ``syscall`` is *not* in
# this list: importing it for ``Getpid``-style calls is legitimate, so only the
# exec mechanism (selector + constant, or a bare constant) fails there.
_GO_SINK_IMPORTS = ("C", "os/exec", "net/rpc", "runtime/cgo")
_GO_LINKNAME = re.compile(r"(?m)^[ \t]*//go:linkname\b")
_GO_IMPORT_SINGLE = re.compile(
    r'\bimport\s+(?:(?P<alias>[A-Za-z_][\w]*|[._])\s+)?(?P<path>"(?:[^"\\]|\\.)*")'
)
_GO_IMPORT_BLOCK = re.compile(r"\bimport\s*\((?P<body>[^)]*)\)", re.DOTALL)
_GO_IMPORT_SPEC = re.compile(
    r'^\s*(?:(?P<alias>[A-Za-z_][\w]*|[._])\s+)?(?P<path>"(?:[^"\\]|\\.)*")\s*$'
)


def _blank_comments_and_strings(text: str) -> str:
    """Blank ``//`` / ``/* */`` / ``#`` comments and string/rune bodies.

    Hidden-fallback matching must reflect executable identifiers, not prose and
    not text that only appears inside a string literal.  Positions are preserved
    (comment and string bodies become spaces) so a caller can still report a
    stable match; import bindings are parsed from the raw text instead.
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
            if character == "#":
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
            index += 1
            if index < length:
                if text[index] != "\n":
                    characters[index] = " "
                index += 1
            continue
        if character == quote:
            state = "normal"
            index += 1
            continue
        if character != "\n":
            characters[index] = " "
        index += 1
    return "".join(characters)


def _go_import_specs(text: str) -> list[tuple[str | None, str]]:
    """Return ``(alias, package path)`` for every import spec in ``text``.

    Both the single ``import ...`` form and the parenthesised block form are
    understood; ``text`` is expected to be comment-blanked so a prose mention of
    an import is not mistaken for a real import.
    """

    specs: list[tuple[str | None, str]] = []
    for match in _GO_IMPORT_SINGLE.finditer(text):
        specs.append((match.group("alias"), match.group("path")[1:-1]))
    for match in _GO_IMPORT_BLOCK.finditer(text):
        for spec in re.split(r"[\n;]", match.group("body")):
            spec_match = _GO_IMPORT_SPEC.match(spec)
            if spec_match:
                specs.append(
                    (spec_match.group("alias"), spec_match.group("path")[1:-1])
                )
    return specs


def _production_go_files(root: Path) -> Iterable[Path]:
    if not root.is_dir() or root.is_symlink():
        return ()
    collected: list[Path] = []
    for path in root.rglob("*.go"):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in _GO_SINK_EXCLUDED_PARTS for part in relative.parts):
            continue
        if path.name.endswith("_test.go"):
            continue
        collected.append(path)
    return tuple(sorted(collected, key=lambda item: item.as_posix()))


def _go_sink_hits(
    root: Path, patterns: Sequence[str], identifier: str
) -> list[dict[str, str]]:
    """Fail-closed, path- and alias-independent hidden-fallback scan.

    ``patterns`` is accepted for rule-schema compatibility but is intentionally
    not used to scope the walk: the mechanism scan covers the entire candidate
    repository so the rule cannot be evaded by relocating a bridged file.
    """

    del patterns
    hits: list[dict[str, str]] = []
    for path in _production_go_files(root):
        try:
            raw = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        code = _blank_comments_and_strings(raw)
        specs = _go_import_specs(_blank_comments(raw))
        token = ""
        # Import-level mechanisms fail closed even when the package is only
        # blank-imported; there is no benign reason to reach for them here.
        for _alias, package in specs:
            if any(
                package == item or package.startswith(item + "/")
                for item in _GO_SINK_IMPORTS
            ):
                token = f'import "{package}"'
                break
        if not token:
            # Selector-level mechanisms are matched by resolved package path, so
            # the alias the candidate chose does not matter.
            for alias, package in specs:
                if alias in (".", "_"):
                    continue
                name = alias or package.rsplit("/", 1)[-1]
                selectors = _GO_SINK_SELECTORS.get(package)
                if selectors:
                    for selector in selectors:
                        if re.search(
                            r"\b" + re.escape(name) + r"\s*\.\s*" + selector + r"\b", code
                        ):
                            token = f"{name}.{selector}"
                            break
                if token:
                    break
                # ``os.Process.Start``: the receiver is an ``os`` value, not the
                # package selector, so match the method on any ``Process``.
                if package == "os" and _GO_PROCESS_START.search(code):
                    token = "os.Process.Start"
                    break
                # Raw syscall entry points are only a delegation mechanism when
                # an exec-family syscall constant is also in play.
                if package == "syscall" and _GO_EXEC_CONSTANT_RE.search(code):
                    for selector in _GO_SYSCALL_CALL_SELECTORS:
                        if re.search(
                            r"\b" + re.escape(name) + r"\s*\.\s*" + selector + r"\b",
                            code,
                        ):
                            token = f"{name}.{selector}"
                            break
                if token:
                    break
        if not token:
            # Bare mechanism: an exec-family syscall constant is never benign in
            # a migrated package, whether or not ``syscall`` is imported (a dot
            # import or a helper alias must not hide it).
            match = _GO_BARE_EXEC_CONSTANT_RE.search(code)
            if match:
                token = f"exec-syscall-constant {match.group(0)}"
        if not token and _GO_LINKNAME.search(raw):
            token = "//go:linkname"
        if token:
            hits.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "token": token[:120],
                    "kind": "go-hidden-fallback",
                    "rule_id": identifier,
                    "via": "mechanism",
                }
            )
    return hits


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
            or not isinstance(patterns, Sequence)
            or isinstance(patterns, (str, bytes))
            or not patterns
            or any(
                not isinstance(item, str) or not _safe_relative(item)
                for item in patterns
            )
        ):
            raise ValueError(f"invalid residue rule: {identifier or '<unknown>'}")
        if kind in _GO_SINK_KINDS:
            hits.extend(_go_sink_hits(root, tuple(patterns), identifier))
            continue
        if not isinstance(target, str) or not target:
            raise ValueError(f"invalid residue rule: {identifier or '<unknown>'}")
        if kind not in _RULE_KINDS:
            continue
        exclude_tests = bool(row.get("exclude_test_files"))
        for path in _paths(root, tuple(patterns)):
            if exclude_tests and _is_test_file(path):
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
        if kind in _GO_SINK_KINDS:
            hits = scan_residue_rules(root, [row])
            diagnostics.append(
                {
                    "rule_id": str(identifier),
                    "kind": str(kind),
                    "status": "FAIL" if hits else "PASS",
                    "hits": hits,
                }
            )
            continue
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
