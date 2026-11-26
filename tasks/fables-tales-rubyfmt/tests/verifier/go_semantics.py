"""Semantic hidden-runtime fallback detection for Go candidates.

The declarative residue scanner is deliberately language-neutral: it can only
see literal bytes, so a hidden fallback to the previous Rust implementation can
be expressed without any contiguous ``cargo``/``rustc`` literal appearing::

    name := "car" + "go"
    argv := []string{name, "run", "--release", "--bin", "rubyfmt-main"}
    proc, _ := os.StartProcess(name, argv, attr)

This module adds the semantic half required by the
``no_deceptive_source_fallback`` gate for a Rust→Go migration:

* only production ``*.go`` files are scanned (``_test.go``, ``vendor/`` and
  genuine build-output trees are excluded; a package that merely *happens* to
  be named ``build`` such as ``internal/build/`` is production source and is
  scanned);
* string constants are folded through ``+`` concatenation, escape decoding
  (``\\xHH``/``\\uHHHH``/octal) and ``[]byte{...}``/``[]rune{...}`` construction
  so a runtime-assembled executable name resolves back to
  ``cargo``/``rustc``/``rubyfmt-main``;
* four hidden-fallback mechanisms are covered: process creation, FFI, RPC/HTTP
  bridge and dynamic loading;
* the legitimate production surface is preserved: cgo Prism
  (``internal/prism/prism.go``), TTY ``ioctl`` syscalls and signal syscalls are
  not sinks and are never flagged.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Sequence

try:  # package layout
    from .residue import _strip_go_comments
except ImportError:  # standalone verifier bundle layout
    from residue import _strip_go_comments  # type: ignore[no-redef]


_EXCLUDED_PARTS = {
    ".git",
    "node_modules",
    "vendor",
    "third_party",
    ".venv",
    "venv",
    "target",
    "dist",
    "coverage",
    "__pycache__",
}
# NOTE: the generic name ``build`` is deliberately *not* excluded.  A Go
# package living at ``internal/build/`` is ordinary production source, and the
# declarative ``runtime_sink`` rules in ``config.json`` declare ``**/*.go``
# coverage.  Excluding every path segment named ``build`` let a hidden fallback
# hide inside a legitimately-named package (``internal/build/fallback.go``) and
# escape the ``no_deceptive_source_fallback`` gate entirely.  Only genuine
# build-output trees without production ``*.go`` remain excluded above.

# Process creation: spawning anything from the production formatter is only
# legitimate when it is actually a hidden fallback to the source runtime, so it
# is combined with a resolved legacy token below.
_PROCESS_SINKS = (
    re.compile(r"\bos\s*\.\s*StartProcess\s*\("),
    re.compile(r"\bexec\s*\.\s*Command(?:Context)?\s*\("),
    re.compile(r"\bsyscall\s*\.\s*(?:Exec|ForkExec)\s*\("),
)
# Dynamic loading of a foreign implementation.
_DYNAMIC_SINKS = (
    re.compile(r"\bplugin\s*\.\s*Open\s*\("),
    re.compile(r"\b(?:dlopen|dlmopen)\s*\("),
)
# RPC/HTTP bridge back to the source runtime.
_RPC_SINKS = (
    re.compile(r"\brpc\s*\.\s*Dial[A-Za-z]*\s*\("),
    re.compile(r"\bgrpc\s*\.\s*Dial(?:Context)?\s*\("),
    re.compile(r"\bnet\s*\.\s*Dial(?:Timeout)?\s*\("),
)
# FFI: cgo linking the previous runtime, or a C shim that calls into it.
_FFI_SINKS = (
    re.compile(r"#cgo[^\n]*LDFLAGS[^\n]*-l(?:rubyfmt|librubyfmt)"),
    re.compile(r"\bC\s*\.\s*(?:rubyfmt|librubyfmt|ruby_prism)_"),
)
# Category name declared by a ``deceptive_fallback_rules`` ``runtime_sink`` row
# (see ``config.json``) mapped to the call-site patterns that realise it.  The
# declarative rules and the semantic scanner therefore share one vocabulary.
_CATEGORY_SINKS: dict[str, tuple[re.Pattern[str], ...]] = {
    "process": _PROCESS_SINKS,
    "ffi": _FFI_SINKS,
    "rpc_bridge": _RPC_SINKS,
    "dynamic_loading": _DYNAMIC_SINKS,
}
_EXECUTABLE_TOKEN = re.compile(r"^[A-Za-z0-9_.-]+$")
# Tokens that unambiguously name the previous Rust implementation.
_STRONG_LEGACY_TOKENS = (
    "librubyfmt",
    "ruby-prism",
    "ruby_prism::parse",
    "rubyfmt-main",
    "rubyfmt_main",
    "tikv-jemallocator",
)
# Executables whose use from production Go is a fallback unless declared.
_RUNTIME_BINARIES = ("cargo", "rustc", "rubyfmt-main")

_GO_STRING = r'"(?:\\.|[^"\\])*"|`[^`]*`'
_CONCAT = re.compile(rf"(?:{_GO_STRING})(?:\s*\+\s*(?:{_GO_STRING}))+")
_SINGLE = re.compile(_GO_STRING)
# Go integer/rune literals permitted inside ``[]byte{...}``/``[]rune{...}``.
_ARRAY_INT = re.compile(r"0[xX][0-9a-fA-F]+|0[oO][0-7]+|0[bB][01]+|[0-9]+")
_ARRAY_CHAR = re.compile(r"'(?:\\.|[^'\\])*'")
_ARRAY_LITERAL = re.compile(
    r"\[\s*\]\s*(?:byte|uint8|rune)\s*\{([^{}]*)\}", re.DOTALL
)

_GO_ESCAPES = {
    "a": "\a",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
    "\\": "\\",
    "'": "'",
    '"': '"',
}


def _decode(token: str) -> str:
    """Resolve a Go string/rune literal, decoding every escape form.

    A hidden runtime name need not appear as a literal ``cargo`` substring: it
    can be spelled with hex/octal/unicode escapes (``"c\\x61rgo"``).  The old
    implementation collapsed only ``\\<char>`` and left ``\\x61`` as ``x61``,
    which let such names evade the folded-token match.
    """

    if token.startswith("`") and token.endswith("`"):
        return token[1:-1]
    inner = token[1:-1]
    resolved: list[str] = []
    index = 0
    length = len(inner)
    while index < length:
        character = inner[index]
        if character != "\\":
            resolved.append(character)
            index += 1
            continue
        index += 1
        if index >= length:
            break
        escape = inner[index]
        if escape == "x" and re.match(r"[0-9a-fA-F]{2}", inner[index + 1 : index + 3]):
            resolved.append(chr(int(inner[index + 1 : index + 3], 16)))
            index += 3
            continue
        if escape == "u" and re.match(r"[0-9a-fA-F]{4}", inner[index + 1 : index + 5]):
            resolved.append(chr(int(inner[index + 1 : index + 5], 16)))
            index += 5
            continue
        if escape == "U" and re.match(r"[0-9a-fA-F]{8}", inner[index + 1 : index + 9]):
            resolved.append(chr(int(inner[index + 1 : index + 9], 16)))
            index += 9
            continue
        if escape in "01234567":
            digits = escape
            index += 1
            while index < length and len(digits) < 3 and inner[index] in "01234567":
                digits += inner[index]
                index += 1
            resolved.append(chr(int(digits, 8) & 0xFF))
            continue
        resolved.append(_GO_ESCAPES.get(escape, escape))
        index += 1
    return "".join(resolved)


def _decode_array(elements: str) -> str | None:
    """Decode a ``[]byte{...}``/``[]rune{...}`` literal into text.

    Only plain integer/rune literals are accepted.  If any element is a
    non-constant expression the whole array is rejected (``None``): a partially
    understood array must never fabricate a misleading token.
    """

    resolved: list[str] = []
    for raw in elements.split(","):
        item = raw.strip()
        if not item:
            continue
        if _ARRAY_INT.fullmatch(item):
            prefixed = item[:2].lower() in {"0x", "0o", "0b"}
            value = int(item, 0) if prefixed else int(item, 10)
            if value < 0 or value > 0x10FFFF:
                return None
            resolved.append(chr(value))
            continue
        if _ARRAY_CHAR.fullmatch(item):
            decoded = _decode(item)
            if len(decoded) != 1:
                return None
            resolved.append(decoded)
            continue
        return None
    return "".join(resolved) if resolved else None


def _fold_literals(text: str) -> set[str]:
    """Return resolved string constants from literals and constant arrays."""

    resolved: set[str] = set()
    for match in _CONCAT.finditer(text):
        parts = _SINGLE.findall(match.group(0))
        resolved.add("".join(_decode(part) for part in parts))
    for match in _SINGLE.finditer(text):
        resolved.add(_decode(match.group(0)))
    for match in _ARRAY_LITERAL.finditer(text):
        decoded = _decode_array(match.group(1))
        if decoded is not None:
            resolved.add(decoded)
    return resolved


def _blank_strings(text: str) -> str:
    """Blank Go string and rune contents while preserving offsets."""

    characters = list(text)
    length = len(text)
    index = 0
    while index < length:
        character = text[index]
        if character in ("'", '"', "`"):
            quote = character
            characters[index] = " "
            index += 1
            while index < length and text[index] != quote:
                if text[index] == "\\" and quote != "`" and index + 1 < length:
                    characters[index] = " "
                    index += 1
                if index < length and text[index] != "\n":
                    characters[index] = " "
                index += 1
            if index < length:
                characters[index] = " "
                index += 1
            continue
        index += 1
    return "".join(characters)


def _production_go_files(root: Path) -> Iterator[Path]:
    if not root.is_dir() or root.is_symlink():
        return
    packages: dict[str, list[Path]] = {}
    sources: dict[Path, str] = {}
    production: set[str] = set()
    for path in sorted(root.rglob("*.go")):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in _EXCLUDED_PARTS for part in relative.parts):
            continue
        if path.name.endswith("_test.go"):
            continue
        try:
            code = _strip_go_comments(path.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
        directory = relative.parent.as_posix()
        packages.setdefault(directory, []).append(path)
        sources[path] = code
        declaration = re.search(r"\bpackage\s+([A-Za-z_]\w*)", code)
        # A standalone development command that compares both implementations
        # is not a fallback in the formatter. Keep the public library, the
        # documented CLI, and conservatively every library package in scope.
        # Follow imports below as well: a helper cannot evade scanning by
        # declaring itself as main or moving under a tools directory.
        if directory in {".", "cmd/rubyfmt"} or not declaration or declaration[1] != "main":
            production.add(directory)

    module = "rubyfmt"
    manifest = root / "go.mod"
    if manifest.is_file() and not manifest.is_symlink():
        match = re.search(r"(?m)^\s*module\s+([^\s]+)", manifest.read_text(encoding="utf-8", errors="ignore"))
        if match:
            module = match[1].strip('"`')
    pending = list(production)
    import_declaration = re.compile(
        rf"\bimport\s+(?:\((?P<group>[^)]*)\)|(?P<single>(?:[\w.]+\s+)?(?:{_GO_STRING})))",
        re.DOTALL,
    )
    while pending:
        directory = pending.pop()
        for path in packages.get(directory, []):
            for match in import_declaration.finditer(sources[path]):
                body = match["group"] if match["group"] is not None else match["single"]
                for token in _SINGLE.findall(body):
                    dependency = _decode(token)
                    if dependency == module:
                        target = "."
                    elif dependency.startswith(module + "/"):
                        target = dependency[len(module) + 1:]
                    else:
                        continue
                    if target in packages and target not in production:
                        production.add(target)
                        pending.append(target)
    for directory in sorted(production):
        yield from packages.get(directory, [])


def _has_runtime_binary(token: str, binaries: Iterable[str]) -> str | None:
    for binary in binaries:
        if re.search(rf"(?:^|[^A-Za-z0-9_]){re.escape(binary)}(?:$|[^A-Za-z0-9_])", token):
            return binary
    return None


def _resolve_scope(
    rules: Sequence[Mapping[str, object]] | None,
) -> tuple[dict[str, tuple[re.Pattern[str], ...]], tuple[str, ...]]:
    """Derive the sink categories and legacy executables from the declared rules.

    ``config.json``'s ``deceptive_fallback_rules`` rows of kind ``runtime_sink``
    are the authoritative source: they name the mechanisms and the legacy
    runtime tokens the gate must intercept.  With no rules (standalone use, unit
    tests) the scanner falls back to its complete built-in coverage so it can
    never silently under-detect.
    """

    if not rules:
        return dict(_CATEGORY_SINKS), _RUNTIME_BINARIES
    categories: dict[str, tuple[re.Pattern[str], ...]] = {}
    binaries: list[str] = []
    for rule in rules:
        if not isinstance(rule, Mapping):
            continue
        category = rule.get("category")
        if isinstance(category, str) and category in _CATEGORY_SINKS:
            categories[category] = _CATEGORY_SINKS[category]
        tokens = rule.get("legacy_runtimes", [])
        if isinstance(tokens, Sequence) and not isinstance(tokens, (str, bytes)):
            for token in tokens:
                if isinstance(token, str) and _EXECUTABLE_TOKEN.match(token) and token not in binaries:
                    binaries.append(token)
    return (
        categories or dict(_CATEGORY_SINKS),
        tuple(binaries) or _RUNTIME_BINARIES,
    )


def scan_go_runtime_sinks(
    root: Path,
    rules: Sequence[Mapping[str, object]] | None = None,
) -> list[dict[str, object]]:
    """Return semantic hidden-fallback hits for a candidate repository tree."""

    categories, binaries = _resolve_scope(rules)
    hits: list[dict[str, object]] = []
    for path in _production_go_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        code = _blank_strings(_strip_go_comments(text))
        tokens = _fold_literals(_strip_go_comments(text))
        relative = path.relative_to(root).as_posix()
        mechanisms = [
            name
            for name, patterns in categories.items()
            if any(pattern.search(code) for pattern in patterns)
        ]
        for token in sorted(tokens):
            for legacy in _STRONG_LEGACY_TOKENS:
                if legacy in token:
                    hits.append(
                        {
                            "path": relative,
                            "token": token,
                            "kind": "runtime_sink",
                            "mechanism": "legacy_runtime",
                            "matched": legacy,
                            "rule_id": "GO-SEMANTIC-LEGACY-RUNTIME",
                        }
                    )
                    break
        if mechanisms:
            for token in sorted(tokens):
                binary = _has_runtime_binary(token, binaries)
                if binary is None:
                    continue
                for mechanism in mechanisms:
                    hits.append(
                        {
                            "path": relative,
                            "token": token,
                            "kind": "runtime_sink",
                            "mechanism": mechanism,
                            "matched": binary,
                            "rule_id": f"GO-SEMANTIC-{mechanism.upper()}",
                        }
                    )
    return hits


def classify_go_runtime_sinks(
    root: Path,
    rules: Sequence[Mapping[str, object]] | None = None,
) -> list[dict[str, object]]:
    """Summarise the semantic scan for diagnostics."""

    hits = scan_go_runtime_sinks(root, rules)
    return [
        {
            "rule_id": "GO-SEMANTIC-RUNTIME-SINK",
            "kind": "runtime_sink",
            "status": "FAIL" if hits else "PASS",
            "hits": hits,
        }
    ]


__all__ = ["scan_go_runtime_sinks", "classify_go_runtime_sinks"]
