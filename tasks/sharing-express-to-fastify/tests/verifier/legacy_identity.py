from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator

# Exact legacy runtime names.  Plain residue declared at the root keeps the
# existing "scope_incomplete" classification; a legacy package installed under
# a *different* name (npm alias / masquerade) or reachable only through another
# package's dependency chain (undeclared at the root) is deception.
LEGACY_PACKAGES = ("express", "express-fileupload", "express-basic-auth")

_NON_SOURCE_SUFFIXES = {
    ".json", ".node", ".html", ".htm", ".css", ".svg", ".txt",
    ".md", ".xml", ".png", ".jpg", ".jpeg", ".gif", ".ico",
    ".webp", ".map", ".woff", ".woff2", ".ttf", ".eot",
    ".pdf", ".zip", ".gz", ".pem", ".crt", ".key",
}

def is_production_source(path: Path) -> bool:
    """Include JavaScript and unknown extensions, but exclude known data assets."""
    return path.is_file() and path.suffix.lower() not in _NON_SOURCE_SUFFIXES


@lru_cache(maxsize=128)
def _lexical_views(text: str) -> tuple[str, str]:
    """Return comment-free source and code positions, preserving offsets.

    The first view retains string tokens used as module arguments.  The second
    blanks strings and regex literals so a call-looking string cannot become
    executable evidence.  This is a lexical guard for the existing patterns;
    no parser dependency is needed in the isolated evaluator.
    """
    visible = list(text)
    code = list(text)

    def blank(start: int, end: int, *, hide_visible: bool) -> None:
        for offset in range(start, end):
            if text[offset] not in "\r\n":
                code[offset] = " "
                if hide_visible:
                    visible[offset] = " "

    def regex_follows(index: int) -> bool:
        prefix = text[:index].rstrip()
        if not prefix:
            return True
        if prefix[-1] in "=([{,:;!?&|+-*%^~<>":
            return True
        return bool(re.search(r"\b(?:return|throw|case|delete|void|typeof|instanceof|in|of|yield|await)\s*$", prefix))

    size = len(text)

    def scan_template(index: int) -> int:
        chunk_start = index
        index += 1
        while index < size:
            if text[index] == "\\":
                index += 2
            elif text.startswith("${", index):
                blank(chunk_start, index + 2, hide_visible=False)
                index = scan_code(index + 2, interpolation=True)
                chunk_start = index
            elif text[index] == "`":
                index += 1
                blank(chunk_start, index, hide_visible=False)
                return index
            else:
                index += 1
        blank(chunk_start, size, hide_visible=False)
        return size

    def scan_code(index: int, *, interpolation: bool = False) -> int:
        depth = 1 if interpolation else 0
        while index < size:
            start = index
            char = text[index]
            if char == "/" and index + 1 < size and text[index + 1] == "/":
                index = text.find("\n", index + 2)
                if index < 0:
                    index = size
                blank(start, index, hide_visible=True)
                continue
            if char == "/" and index + 1 < size and text[index + 1] == "*":
                end = text.find("*/", index + 2)
                index = size if end < 0 else end + 2
                blank(start, index, hide_visible=True)
                continue
            if char in "'\"":
                index += 1
                while index < size:
                    if text[index] == "\\":
                        index += 2
                    elif text[index] == char:
                        index += 1
                        break
                    else:
                        index += 1
                blank(start, min(index, size), hide_visible=False)
                continue
            if char == "`":
                index = scan_template(index)
                continue
            if char == "/" and regex_follows(index):
                index += 1
                in_class = False
                while index < size and text[index] not in "\r\n":
                    if text[index] == "\\":
                        index += 2
                    elif text[index] == "[":
                        in_class = True
                        index += 1
                    elif text[index] == "]":
                        in_class = False
                        index += 1
                    elif text[index] == "/" and not in_class:
                        index += 1
                        while index < size and text[index].isalpha():
                            index += 1
                        blank(start, index, hide_visible=False)
                        break
                    else:
                        index += 1
                else:
                    index = start + 1
                continue
            if interpolation and char == "{":
                depth += 1
            elif interpolation and char == "}":
                depth -= 1
                if depth == 0:
                    blank(index, index + 1, hide_visible=False)
                    return index + 1
            index += 1
        return size

    scan_code(0)
    return "".join(visible), "".join(code)


def _code_matches(pattern: re.Pattern[str], text: str) -> Iterator[re.Match[str]]:
    visible, code = _lexical_views(text)
    for match in pattern.finditer(visible):
        start = match.start()
        while start < match.end() and visible[start].isspace():
            start += 1
        if start < match.end() and not code[start].isspace():
            yield match


def executable_text(text: str) -> str:
    """Remove comments without removing module specifier and target strings."""
    return _lexical_views(text)[0]


def production_sources(repo: Path) -> Iterator[Path]:
    """Scan bin/** plus local modules reachable from its load sites.

    A helper outside bin remains production code when bin loads it. Follow
    local specifiers recursively, including explicit nonstandard extensions,
    extensionless imports, directory indexes and package main entries.
    """
    root = repo.resolve()
    pending = sorted((repo / "bin").rglob("*"), reverse=True)
    seen: set[Path] = set()

    def resolve_local(target: Path) -> Path | None:
        options = [target, *(Path(str(target) + ext) for ext in (".js", ".cjs", ".mjs"))]
        if target.is_dir():
            try:
                main = json.loads((target / "package.json").read_text()).get("main")
                if isinstance(main, str):
                    entry = target / main
                    options.extend([entry, Path(str(entry) + ".js"), entry / "index.js"])
            except (OSError, ValueError, AttributeError):
                pass
            options.extend(target / name for name in ("index.js", "index.cjs", "index.mjs"))
        for option in options:
            resolved = option.resolve()
            if resolved.is_relative_to(root) and "node_modules" not in resolved.relative_to(root).parts:
                if is_production_source(resolved):
                    return resolved
        return None

    while pending:
        path = pending.pop().resolve()
        if path in seen or not path.is_relative_to(root) or not is_production_source(path):
            continue
        seen.add(path)
        yield path
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for specifier in [*literal_specifiers(text), *static_import_specifiers(text), *indirect_loader_specifiers(text)]:
            if specifier.startswith(("./", "../")):
                dependency = resolve_local(path.parent / specifier)
                if dependency is not None:
                    pending.append(dependency)

# Literal ``require('X')`` / ``import('X')`` module specifiers.  Dynamic
# require(variable) / concatenation is intentionally out of scope here; the
# hard gate's existing textual patterns cover those forms.
_LITERAL_SPECIFIER_RE = re.compile(
    r"""(?<![.\w$])(?:require|import)\s*\(\s*(['"])([^'"]+)\1\s*\)"""
)

# A compile-time constant usable as a module specifier: a quoted string or a
# template literal with no ``${...}`` substitution.  A template with runtime
# substitution is not a specifier; ``legacy_loader_evidence`` covers that
# shape conservatively.
_STATIC_SPECIFIER_LITERAL = (
    r"""(?:(?P<q>['"])(?P<quoted>[^'"\r\n]+)(?P=q)|`(?P<templated>[^`$\r\n]+)`)"""
)

# Indirect loader forms that reach the same ``require`` implementation without
# a literal ``require('X')`` call site: ``require.call`` / ``require.apply``,
# comma-operator indirection ``(0, require)('X')``,
# ``Reflect.apply(require, ...)`` and ``module.constructor._load('X')``.
# Each pattern captures the static specifier literal in ``quoted`` or
# ``templated``.
_INDIRECT_SPECIFIER_PATTERNS = (
    re.compile(
        r"""\bmodule\s*(?:\.\s*require|\[\s*['"]require['"]\s*\])\s*\(\s*"""
        + _STATIC_SPECIFIER_LITERAL
    ),
    re.compile(
        r"""\brequire\s*\.\s*(?:call|apply)\s*\(\s*[^,()]*,\s*(?:\[\s*)?"""
        + _STATIC_SPECIFIER_LITERAL
    ),
    re.compile(r"""\(\s*0\s*,\s*require\s*\)\s*\(\s*""" + _STATIC_SPECIFIER_LITERAL),
    re.compile(
        r"""\bReflect\s*\.\s*apply\s*\(\s*require\s*,\s*[^,()]*,\s*\[\s*"""
        + _STATIC_SPECIFIER_LITERAL
    ),
    re.compile(
        r"""\bmodule\s*(?:\.\s*constructor|\[\s*['"]constructor['"]\s*\])\s*(?:\.\s*_load|\[\s*['"]_load['"]\s*\])\s*\(\s*"""
        + _STATIC_SPECIFIER_LITERAL
    ),
    # Static template-literal ``require(`express`)``; a template containing a
    # ``${...}`` substitution is runtime-computed and yields no specifier.
    re.compile(r"""\brequire\s*\(\s*`(?P<templated>[^`$\r\n]+)`\s*\)"""),
)
_MODULE_CONSTRUCTOR_LOAD = (
    r"\bmodule\s*(?:\.\s*constructor|\[\s*['\"]constructor['\"]\s*\])"
    r"\s*(?:\.\s*_load|\[\s*['\"]_load['\"]\s*\])"
)

# String / template-literal concatenation folding.  A loader written as
# ``require('exp' + 'ress')`` or ``require(`exp${'ress'}`)`` only becomes a
# legacy name at runtime; folding the literals back together lets the static
# loader patterns see it.  Moved verbatim from the hard gate so the hard gate
# and the migration-scope check share one implementation.
_STRING_CONCAT_RE = re.compile(
    r"(['\"])([^'\"\r\n]*)\1\s*\+\s*(['\"])([^'\"\r\n]*)\3"
)
_TEMPLATE_LITERAL_CONCAT_RE = re.compile(
    r"`([^`$]*)\$\{\s*(['\"])([^'\"]*)\2\s*\}([^`]*)`"
)
_QUOTED_JS_STRING = r"(?:'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\")"
_STATIC_ARRAY_JOIN_RE = re.compile(
    r"\[\s*(?P<items>" + _QUOTED_JS_STRING +
    r"(?:\s*,\s*" + _QUOTED_JS_STRING + r")*)\s*\]"
    r"\s*\.\s*join\s*\(\s*(?P<separator>" + _QUOTED_JS_STRING + r")\s*\)"
)

# Static ESM ``import`` / ``export ... from`` statements.  Anchored on the
# statement keyword at the start of a line so prose and comments that merely
# mention a package name do not match.  The clause may span lines (multi-line
# named imports) but stops at a statement terminator or a quote.
_STATIC_IMPORT_SPECIFIER_RE = re.compile(
    r"""^[ \t]*import\b(?!\s*\()[^'";]*?['"]([^'"]+)['"]"""
    r"""|^[ \t]*export\b[^'";]*?\bfrom\s*['"]([^'"]+)['"]""",
    re.MULTILINE,
)


def _alias_target(spec: str) -> str:
    """Resolve the real package name targeted by an ``npm:`` alias specifier.

    ``npm:express@4.22.2`` -> ``express`` and ``npm:@scope/name@1.2.3`` ->
    ``@scope/name``.  The version separator is the last ``@`` that is not the
    leading scope marker.
    """
    body = spec[len("npm:") :]
    at = body.rfind("@")
    return body[:at] if at > 0 else body


def _matched_legacy(install_name: str, real_name: str, resolved: str) -> str | None:
    """Return the legacy package masquerading at ``install_name``, if any.

    A plain legacy install (``install_name == real_name == 'express'``) is
    residue, not deception, and must not match.
    """
    if real_name in LEGACY_PACKAGES and install_name != real_name:
        return real_name
    for legacy in LEGACY_PACKAGES:
        if f"/{legacy}/-/" in resolved and install_name != legacy:
            return legacy
    return None


def _record_entry(
    key: str,
    install_name: str,
    entry: dict[str, Any],
    findings: list[dict[str, Any]],
    alias_map: dict[str, str],
) -> None:
    real_name = entry.get("name") or install_name
    if not isinstance(real_name, str):
        real_name = install_name
    resolved = entry.get("resolved", "") or ""
    if not isinstance(resolved, str):
        resolved = ""
    if entry.get("dev") is not True and _matched_legacy(install_name, real_name, resolved) is not None:
        findings.append({
            "kind": "aliased_install",
            "key": key,
            "name": real_name,
            "resolved": resolved,
            "dev_only": entry.get("dev") is True,
        })
    if real_name != install_name:
        alias_map[install_name] = real_name


def _walk_packages_map(
    packages: dict[str, Any],
    findings: list[dict[str, Any]],
    alias_map: dict[str, str],
) -> None:
    for key, entry in packages.items():
        if key == "" or not isinstance(entry, dict):
            continue
        install_name = key.rsplit("node_modules/", 1)[-1]
        _record_entry(key, install_name, entry, findings, alias_map)


def _walk_dependencies_tree(
    node: Any,
    findings: list[dict[str, Any]],
    alias_map: dict[str, str],
) -> None:
    """Lockfile v1 fallback: entries carry ``resolved`` but no ``name``."""
    if not isinstance(node, dict):
        return
    for name, entry in node.items():
        if not isinstance(entry, dict):
            continue
        install_name = str(name).rsplit("node_modules/", 1)[-1]
        _record_entry(str(name), install_name, entry, findings, alias_map)
        _walk_dependencies_tree(entry.get("dependencies"), findings, alias_map)


def _root_alias_specs(root_deps: Any) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if not isinstance(root_deps, dict):
        return findings
    for name, spec in root_deps.items():
        if not isinstance(spec, str) or not spec.startswith("npm:"):
            continue
        target = _alias_target(spec)
        if target in LEGACY_PACKAGES:
            findings.append({
                "kind": "alias_spec",
                "name": name,
                "spec": spec,
                "target": target,
            })
    return findings


def _iter_lock_entries(lock: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield ``(key, entry)`` for every installed package in the lockfile."""
    packages = lock.get("packages")
    if isinstance(packages, dict) and packages:
        for key, entry in packages.items():
            if key != "" and isinstance(entry, dict):
                yield key, entry
        return

    def walk(node: Any) -> Iterator[tuple[str, dict[str, Any]]]:
        if not isinstance(node, dict):
            return
        for name, entry in node.items():
            if not isinstance(entry, dict):
                continue
            yield str(name), entry
            yield from walk(entry.get("dependencies"))

    yield from walk(lock.get("dependencies"))


def _root_declared_names(repo: Path, root_deps: Any) -> set[str]:
    """Package names the root manifest declares as direct dependencies.

    Combines lockfile root ``dependencies`` keys, root ``package.json``
    ``dependencies`` keys (best effort), and targets of root ``npm:`` alias
    specs.  ``devDependencies`` are not declared dependencies.
    """
    declared: set[str] = set()
    if isinstance(root_deps, dict):
        for name, spec in root_deps.items():
            declared.add(str(name))
            if isinstance(spec, str) and spec.startswith("npm:"):
                declared.add(_alias_target(spec))
    try:
        package = json.loads((repo / "package.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        package = None
    package_deps = package.get("dependencies") if isinstance(package, dict) else None
    if isinstance(package_deps, dict):
        declared.update(str(name) for name in package_deps)
    return declared


def _undeclared_legacy_findings(
    lock: dict[str, Any],
    declared: set[str],
) -> list[dict[str, Any]]:
    """Legacy packages reachable only through another package's dependencies.

    A legacy install declared at the root is ordinary residue; one that only
    enters the tree transitively is deception.  Root alias targets count as
    declared so ``aliased_install`` / ``alias_spec`` report them exactly once.
    """
    findings: list[dict[str, Any]] = []
    for key, entry in _iter_lock_entries(lock):
        if entry.get("dev") is True:
            continue
        install_name = key.rsplit("node_modules/", 1)[-1]
        real_name = entry.get("name") or install_name
        if not isinstance(real_name, str):
            real_name = install_name
        resolved = entry.get("resolved", "") or ""
        if not isinstance(resolved, str):
            resolved = ""
        matches_legacy = real_name in LEGACY_PACKAGES or any(
            f"/{legacy}/-/" in resolved for legacy in LEGACY_PACKAGES
        )
        if not matches_legacy or real_name in declared:
            continue
        findings.append({
            "kind": "undeclared_legacy_install",
            "key": key,
            "name": real_name,
            "resolved": resolved,
            "dev_only": False,
        })
    return findings


def load_lockfile_identity(repo: Path) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Resolve package identity from ``package-lock.json``.

    Returns ``(findings, alias_map)``:

    - ``findings``: deception evidence, ``kind`` in {``aliased_install``,
      ``alias_spec``, ``undeclared_legacy_install``}.  Plain legacy installs
      declared at the root are never reported; a legacy package that only
      enters the tree through another package's dependency chain is reported
      as ``undeclared_legacy_install``.
    - ``alias_map``: ``{install_name: real_name}`` for every entry whose
      install key differs from the real package name, used to resolve literal
      source specifiers.

    Raises the underlying ``OSError`` / ``json.JSONDecodeError`` when the
    lockfile is unreadable or invalid; the caller decides how to degrade.
    """
    lock = json.loads((repo / "package-lock.json").read_text(encoding="utf-8"))
    findings: list[dict[str, Any]] = []
    alias_map: dict[str, str] = {}

    packages = lock.get("packages")
    if isinstance(packages, dict) and packages:
        _walk_packages_map(packages, findings, alias_map)
        root_deps = (packages.get("") or {}).get("dependencies")
    else:
        _walk_dependencies_tree(lock.get("dependencies"), findings, alias_map)
        root_deps = lock.get("dependencies")

    findings.extend(_root_alias_specs(root_deps))
    findings.extend(_undeclared_legacy_findings(lock, _root_declared_names(repo, root_deps)))
    return findings, alias_map


def _decode_js_specifier(value: str) -> str:
    """Decode JS escapes in a captured string, never in surrounding code.

    In particular a literal backslash before ``u`` must stay a backslash;
    decoding twice would misclassify a different, non-legacy module name.
    """
    escapes = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}

    def decode(match: re.Match[str]) -> str:
        token = match.group(0)[1:]
        if token.startswith("u{"):
            codepoint = int(token[2:-1], 16)
            return chr(codepoint) if codepoint <= 0x10ffff else match.group(0)
        if token.startswith(("u", "x")) and len(token) > 1:
            return chr(int(token[1:], 16))
        if token in ("\n", "\r", "\r\n"):
            return ""
        return escapes.get(token, token)

    return re.sub(r"\\(?:u\{[0-9a-fA-F]+\}|u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|\r\n|[\s\S])", decode, value)


def literal_specifiers(text: str) -> list[str]:
    """Extract literal module specifiers from ``require()`` / ``import()``."""
    return [_decode_js_specifier(match.group(2)) for match in _code_matches(_LITERAL_SPECIFIER_RE, text)]


def indirect_loader_specifiers(text: str) -> list[str]:
    """Extract module specifiers from indirect ``require`` load forms.

    Covers ``require.call`` / ``require.apply``, comma-operator indirection
    ``(0, require)('X')``, ``Reflect.apply(require, ...)``,
    ``module.constructor._load('X')`` and static template-literal
    ``require(`X`)``.  A template literal containing a ``${...}`` substitution
    is runtime-computed and yields no specifier here; the conservative
    ``legacy_loader_evidence`` scan covers that shape.
    """
    specifiers: list[str] = []
    for pattern in _INDIRECT_SPECIFIER_PATTERNS:
        for match in _code_matches(pattern, text):
            groups = match.groupdict()
            specifiers.append(_decode_js_specifier(groups.get("quoted") or groups.get("templated")))
    return specifiers


def legacy_specifier_hits(text: str, alias_map: dict[str, str]) -> list[dict[str, Any]]:
    """Resolve module specifiers to legacy packages, following npm aliases.

    Covers literal ``require()`` / ``import()`` calls, static ESM
    ``import`` / ``export ... from`` statements, and the indirect loader forms
    extracted by :func:`indirect_loader_specifiers` (``require.call``,
    ``(0, require)``, ``Reflect.apply(require, ...)``,
    ``module.constructor._load``, static template literals).  Returns one
    entry per specifier that resolves to a package in ``LEGACY_PACKAGES``:
    ``{"specifier", "resolves_to", "aliased"}``.  A plain ``require('express')``
    resolves to itself (``aliased`` False) and is therefore reported exactly
    like an aliased one; callers decide whether the two forms carry different
    policy weight.
    """
    hits: list[dict[str, Any]] = []
    for specifier in [
        *literal_specifiers(text),
        *static_import_specifiers(text),
        *indirect_loader_specifiers(text),
    ]:
        if specifier.startswith("@"):
            parts = specifier.split("/", 2)
            package_name = "/".join(parts[:2]) if len(parts) >= 2 else specifier
            subpath = parts[2] if len(parts) == 3 else ""
            separator = "/" if subpath else ""
        else:
            package_name, separator, subpath = specifier.partition("/")
        resolved_name = alias_map.get(package_name, package_name)
        resolves_to = resolved_name + (separator + subpath if separator else "")
        if resolved_name in LEGACY_PACKAGES:
            hits.append({
                "specifier": specifier,
                "resolves_to": resolves_to,
                "aliased": package_name != resolved_name,
            })
    return hits


def _fold_literal_concatenations(text: str) -> str:
    previous = None
    while text != previous:
        previous = text
        text = _STATIC_ARRAY_JOIN_RE.sub(
            lambda match: repr(
                _decode_js_specifier(match.group("separator")[1:-1]).join(
                    _decode_js_specifier(item.group(0)[1:-1])
                    for item in re.finditer(_QUOTED_JS_STRING, match.group("items"))
                )
            ),
            text,
        )
        text = _STRING_CONCAT_RE.sub(lambda match: repr(match.group(2) + match.group(4)), text)
        text = _TEMPLATE_LITERAL_CONCAT_RE.sub(
            lambda match: repr(match.group(1) + match.group(3) + match.group(4)), text
        )
    return text


def legacy_loader_evidence(text: str, alias_map: dict[str, str] | None = None) -> list[dict[str, str]]:
    """Detect legacy-module loader forms that static specifier lists miss.

    This is the single source of truth shared by the hard gate and the
    migration-scope check, so the two can never disagree about whether a
    production file loads a legacy runtime.  It is deliberately
    limited to identifiable legacy loads: it covers string concatenation that
    becomes a legacy name, private loader indirection, require-function aliasing
    (``const r = require; r('express')``) and dynamic ``Function``
    construction (``new Function('return require')``).  An unresolved
    ``require(name)`` alone is not evidence of a legacy implementation.
    """
    source = executable_text(text)
    normalized = _fold_literal_concatenations(source)
    evidence: list[dict[str, str]] = []
    aliases = alias_map or {}

    def resolves_legacy(specifier: str | None) -> bool:
        if not specifier:
            return False
        if specifier.startswith("@"):
            parts = specifier.split("/", 2)
            package_name = "/".join(parts[:2]) if len(parts) >= 2 else specifier
        else:
            package_name = specifier.split("/", 1)[0]
        return aliases.get(package_name, package_name) in LEGACY_PACKAGES

    for match in _code_matches(_LITERAL_SPECIFIER_RE, source):
        raw = match.group(2)
        decoded = _decode_js_specifier(raw)
        if raw != decoded and decoded.split("/", 1)[0] in LEGACY_PACKAGES:
            evidence.append({"kind": "encoded_legacy_module_loader", "evidence": match.group(0)})
    for specifier in indirect_loader_specifiers(text):
        if specifier.split("/", 1)[0] in LEGACY_PACKAGES:
            evidence.append({"kind": "indirect_legacy_module_loader", "evidence": specifier})

    # Follow a constant module name into Node's private loader, including a
    # loader reference first stored in a variable.  The normalized text also
    # folds static array joins, so ['exp', 'ress'].join('') has the same identity
    # as 'express' at the call site.  A mere string mention is not a loader.
    static_names = {
        match.group(1): _decode_js_specifier(match.group(2)[1:-1])
        for match in _code_matches(re.compile(
            r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*"
            r"('(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|`[^`$]*`)",
        ), normalized)
    }
    loader_aliases = {
        match.group(1)
        for match in _code_matches(re.compile(
            r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*"
            + _MODULE_CONSTRUCTOR_LOAD + r"(?!\s*\()",
        ), normalized)
    }
    loader_callee = _MODULE_CONSTRUCTOR_LOAD
    if loader_aliases:
        loader_callee += r"|\b(?:" + "|".join(re.escape(name) for name in sorted(loader_aliases)) + r")\b"
    loader_argument = re.compile(
        r"(?:" + loader_callee + r")\s*\(\s*"
        r"(?P<argument>'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|`[^`$]*`|[A-Za-z_$][\w$]*)\s*\)"
    )
    for call in _code_matches(loader_argument, normalized):
        argument = call.group("argument")
        if argument[0] in "'\"`":
            specifier = _decode_js_specifier(argument[1:-1])
        else:
            specifier = static_names.get(argument)
        if resolves_legacy(specifier):
            evidence.append({"kind": "indirect_legacy_module_loader", "evidence": call.group(0)})

    # Treat every legacy-module loader form as a policy hit.  Static scans must
    # not be bypassable by aliases, createRequire/module.require, or string
    # concatenation that only becomes ``express`` at runtime.
    dynamic_loader = re.compile(
        r"(?<![.\w$])(?:require|module\.require|module\s*\[\s*['\"]require['\"]\s*\])"
        r"\s*\(\s*([A-Za-z_$][\w$]*)\s*\)"
    )
    for call in _code_matches(dynamic_loader, normalized):
        target = static_names.get(call.group(1))
        if resolves_legacy(target):
            evidence.append({"kind": "dynamic_legacy_module_loader", "evidence": call.group(0)})
    concatenated_loader = re.compile(
        r"\brequire\s*\(\s*(?:[^)]*\+[^)]*|`[^`]*\$\{[^}]+\}[^`]*`)\s*\)"
    )
    for call in _code_matches(concatenated_loader, source):
        folded = _fold_literal_concatenations(call.group(0))
        if any(name in folded for name in LEGACY_PACKAGES):
            evidence.append({"kind": "dynamic_legacy_module_loader", "evidence": call.group(0)})

    # ``const r = require; r('express')`` launders the loader through a
    # variable, so no ``require('X')`` call site exists.  The negative
    # lookahead keeps ordinary ``const fs = require('fs')`` module imports out
    # of scope: aliasing the *function* requires ``require`` itself, not a
    # call, and flagging every CommonJS import would drown the signal.
    require_alias = re.compile(
        r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*require\b(?!\s*\()"
    )
    for assignment in _code_matches(require_alias, normalized):
        alias_call = re.compile(
            r"\b" + re.escape(assignment.group(1)) + r"\s*\(\s*"
            r"(?P<argument>'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|[A-Za-z_$][\w$]*)\s*\)"
        )
        for call in _code_matches(alias_call, normalized):
            argument = call.group("argument")
            target = (
                _decode_js_specifier(argument[1:-1])
                if argument[0] in "'\"" else static_names.get(argument)
            )
            if resolves_legacy(target):
                evidence.append({"kind": "require_function_alias", "evidence": call.group(0)})

    # ``new Function('return require')()('express')`` rebuilds a require
    # reference the static loader patterns cannot name.
    dynamic_function_require = next(_code_matches(re.compile(
        r"new\s+Function\s*\(\s*['\"]return\s+require['\"]\s*\)"
        r"\s*\(\s*\)\s*\(\s*['\"](?:express|express-fileupload|express-basic-auth)(?:/[^'\"]*)?['\"]\s*\)"
    ), normalized), None)
    if dynamic_function_require:
        evidence.append({
            "kind": "dynamic_function_require",
            "evidence": dynamic_function_require.group(0),
        })
    return evidence


def static_import_specifiers(text: str) -> list[str]:
    """Extract module specifiers from static ESM import/export statements."""
    return [
        _decode_js_specifier(match.group(1) or match.group(2))
        for match in _code_matches(_STATIC_IMPORT_SPECIFIER_RE, text)
    ]


def node14_incompatible_specifiers(text: str) -> list[str]:
    """Return module loads that Node 14.18 cannot provide."""
    specifiers = [
        *literal_specifiers(text),
        *static_import_specifiers(text),
        *indirect_loader_specifiers(text),
    ]
    return [
        specifier for specifier in specifiers
        if specifier.removeprefix("node:") == "stream/promises"
    ]


def unresolved_module_loader_evidence(text: str) -> list[str]:
    """Report dynamic module names without treating them as legacy evidence."""
    normalized = _fold_literal_concatenations(executable_text(text))
    constants = {
        match.group(1)
        for match in _code_matches(re.compile(
            r"\bconst\s+([A-Za-z_$][\w$]*)\s*=\s*"
            r"(?:'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|`[^`$]*`)"
        ), normalized)
    }
    dynamic = re.compile(
        r"(?<![.\w$])(?:require|module\.require)\s*\(\s*([A-Za-z_$][\w$]*)\s*\)"
    )
    return [match.group(0) for match in _code_matches(dynamic, normalized)
            if match.group(1) not in constants]
