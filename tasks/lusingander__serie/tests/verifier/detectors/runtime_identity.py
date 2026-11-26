"""Deterministic source-runtime identity helpers for verifier detectors.

The detector families intentionally use different evidence:

* process execution looks for a source-runtime command (``go``, ``node`` ...);
* FFI/native loading looks for a source-runtime *artifact* (``libgo.so`` ...).

A generic language name is not evidence that an FFI boundary falls back to the
source implementation.  Keeping the two vocabularies separate prevents legal
target-language FFI (for example Go ``import "C"`` with libmnl) from being
reported as an L2 source fallback.
"""

from __future__ import annotations

import re
from typing import Any, Mapping


# Language -> source-file suffixes.  Used to scope the target-language
# command-identity check so protected *source*-language files (for example a
# Rust TUI module that legitimately spawns a user command) are never rewritten
# into fallback evidence by the target-side rules.
_TARGET_LANGUAGE_SUFFIXES = {
    "go": frozenset({".go"}),
    "golang": frozenset({".go"}),
    "rust": frozenset({".rs"}),
    "python": frozenset({".py"}),
    "typescript": frozenset({".ts", ".tsx"}),
    "javascript": frozenset({".js", ".jsx", ".mjs", ".cjs"}),
    "java": frozenset({".java"}),
    "ruby": frozenset({".rb"}),
    "php": frozenset({".php"}),
}


_GENERIC_SOURCE_NAMES = {
    "go",
    "golang",
    "rust",
    "java",
    "python",
    "typescript",
    "javascript",
}

_LANGUAGE_TOKENS = {
    "typescript": {"node", "nodejs", "ts-node", "tsx", "deno", "bun"},
    "javascript": {"node", "nodejs", "deno", "bun"},
    "python": {"python", "python2", "python3", "pypy"},
    "go": {"go", "golang"},
    "java": {"java", "javaw", "jjs"},
    "ruby": {"ruby"},
    "php": {"php"},
    "rust": {"cargo", "rustc"},
}

# High-confidence artifacts that only appear when a source runtime is embedded
# or loaded.  Do not add generic names such as ``go`` or module paths such as
# ``golang.org/`` here; those are the false positives this registry exists to
# avoid.
_NATIVE_RUNTIME_SIGNALS = {
    "go": {
        "libgo",
        "libgo.so",
        "libgo.dylib",
        "libgo.a",
        "-lgo",
        "go1.",
        "runtime·",
    },
    "typescript": {"libnode", "libnode.so", "libnode.dylib", "node::", "v8::"},
    "javascript": {"libnode", "libnode.so", "libnode.dylib", "node::", "v8::"},
    "python": {
        "libpython",
        "libpython3",
        "libpython3.so",
        "Py_Initialize",
        "python3.",
    },
    "java": {"libjvm", "libjvm.so", "JNI_CreateJavaVM"},
    "ruby": {"libruby", "libruby.so"},
    "php": {"libphp", "libphp.so"},
    "rust": {"librustc", "rustc_driver"},
}

_WINDOWS_EXECUTABLE_SUFFIXES = {".exe", ".cmd", ".bat"}


def _migration(config: Mapping[str, Any]) -> Mapping[str, Any]:
    migration = config.get("migration")
    if isinstance(migration, Mapping) and migration:
        return migration
    # Legacy/private Case configs carry the migration identity as top-level
    # ``source_technology``/``target_technology`` records instead of a nested
    # ``migration`` block.  Normalize that shape so source-runtime detection
    # still has a language/name to work with.
    result: dict[str, Any] = {}
    for key, field in (("source", "source_technology"), ("target", "target_technology")):
        value = config.get(field)
        if isinstance(value, Mapping):
            result[key] = dict(value)
    return result


def source_language(config: Mapping[str, Any]) -> str:
    source = _migration(config).get("source")
    if not isinstance(source, Mapping):
        return ""
    return str(source.get("language_name") or "").strip().casefold()


def source_name(config: Mapping[str, Any]) -> str:
    source = _migration(config).get("source")
    if not isinstance(source, Mapping):
        return ""
    name = str(source.get("name") or "").strip().casefold()
    return name if name not in _GENERIC_SOURCE_NAMES else ""


def target_language(config: Mapping[str, Any]) -> str:
    target = _migration(config).get("target")
    if not isinstance(target, Mapping):
        return ""
    return str(target.get("language_name") or target.get("name") or "").strip().casefold()


def target_source_suffixes(config: Mapping[str, Any]) -> frozenset[str]:
    """Suffixes of the migrated (target) language, or empty when unknown."""

    return _TARGET_LANGUAGE_SUFFIXES.get(target_language(config), frozenset())


def process_runtime_tokens(config: Mapping[str, Any]) -> set[str]:
    """Return command/runtime tokens used by the subprocess detector."""

    tokens = set(_LANGUAGE_TOKENS.get(source_language(config), set()))
    name = source_name(config)
    if name:
        tokens.add(name)
    return tokens


def native_runtime_signals(config: Mapping[str, Any]) -> set[str]:
    """Return high-confidence source-runtime artifacts used by FFI detection."""

    return set(_NATIVE_RUNTIME_SIGNALS.get(source_language(config), set()))


def token_present(text: str, token: str) -> bool:
    """Match a runtime token without matching it inside another identifier."""

    needle = str(token).casefold()
    if not needle:
        return False
    return re.search(
        rf"(?<![A-Za-z0-9_]){re.escape(needle)}(?![A-Za-z0-9_])",
        text.casefold(),
    ) is not None


def _module_reference(text: str, offset: int, token: str) -> bool:
    """Return True for identifiers embedded in module/domain/import paths."""

    lowered = text.casefold()
    needle = token.casefold()
    start = offset
    end = start + len(needle)
    following = lowered[end:] if end < len(lowered) else ""
    if re.match(r"^[A-Za-z0-9_]+/", following):
        return True
    if re.match(r"^:[A-Za-z_]", following):
        # ``require("node:fs")`` / ``import "node:test"`` name a builtin module,
        # not a process to execute.  Go's ``//go:linkname`` directive (an
        # unquoted ``go:`` marker preceded by ``/``) still counts.
        if not (offset > 0 and text[offset - 1] == "/"):
            return True
    if re.match(r"^\.[A-Za-z]", following):
        suffix = following[: following.find("/") if "/" in following else None]
        if not any(suffix.casefold().startswith(item) for item in _WINDOWS_EXECUTABLE_SUFFIXES):
            return True
    return False


def _filename_extension(text: str, offset: int) -> bool:
    """Return True when the token is only a file-extension suffix.

    A source-runtime token that appears as ``<basename>.<token>`` (for example
    the native-addon artifact ``fs_core_engine.node``) is an artifact reference,
    not a command invocation.  Treating it as a command is the process-detector
    analogue of the FFI false positive that ``native_runtime_signals`` exists to
    avoid.  A leading path segment such as ``./node`` is still a command.
    """

    if offset < 2 or text[offset - 1] != ".":
        return False
    return bool(re.match(r"[A-Za-z0-9_\-]", text[offset - 2]))


def process_token_present(text: str, token: str) -> bool:
    """Return True when a source-runtime token is a command, not a module ref."""

    lowered = text.casefold()
    needle = token.casefold()
    if not needle:
        return False
    for match in re.finditer(
        rf"(?<![A-Za-z0-9_]){re.escape(needle)}(?![A-Za-z0-9_])",
        lowered,
    ):
        if _module_reference(text, match.start(), needle):
            continue
        if _filename_extension(text, match.start()):
            continue
        return True
    return False


# --------------------------------------------------------------------------
# Literal decoding / constant propagation
#
# A migrated command name can be built at run time from escapes, concatenation,
# byte arrays or local constants instead of a plain literal.  These helpers
# resolve such constructions *without executing candidate code* so that both
# the process detector and the declarative residue/literal rules can judge the
# run-time identity of a command rather than its surface spelling.
# --------------------------------------------------------------------------

_STRING_LITERAL = r"""(?:"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|`[^`]*`)"""
_LITERAL_CHAIN = re.compile(_STRING_LITERAL + r"(?:\s*\+\s*" + _STRING_LITERAL + r")+", re.S)
_SINGLE_LITERAL = re.compile(_STRING_LITERAL, re.S)
_BYTE_ARRAY = re.compile(r"\[\](?:byte|uint8|rune|int32)\s*\{([^{}]*)\}", re.S)
_BYTE_CONVERSION = re.compile(r"\[\](?:byte|uint8|rune|int32)\s*\(\s*(" + _STRING_LITERAL + r")\s*\)", re.S)
_INT_TOKEN = re.compile(r"0[xX][0-9a-fA-F]+|\d+")
_STRING_CONVERSION = re.compile(r"string\s*\(\s*(.*?)\s*\)\s*$", re.S)
_ASSIGNMENT_LINE = re.compile(
    r"(?m)^[ \t]*(?:const|var|let)?[ \t]*([A-Za-z_$][\w$]*)[ \t]*(?::=|=)[ \t]*(.+?)[ \t]*$"
)
# ``bin, err := exec.LookPath("git")`` binds the value returned by a call whose
# only literal is the command name; recognise that so a sanctioned tool reached
# through ``LookPath`` is not mistaken for an unprovable command.
_CALL_BINDING_LINE = re.compile(
    r"(?:^|[;{])[ \t]*(?:const|var|let)?[ \t]*([A-Za-z_$][\w$]*)[ \t]*,[^\n=]*?(?::=|=)[ \t]*"
    r"([A-Za-z_][\w.]*)\s*\((.*)\)[ \t]*$",
    re.M,
)

_GO_SIMPLE_ESCAPES = {
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


def decode_go_string_escapes(literal: str) -> str:
    """Decode Go interpreted-string escapes: ``\\xNN``, ``\\uNNNN``, ``\\NNN``.

    The argument is the *body* of a Go interpreted string literal (without the
    surrounding quotes), exactly as it appears in source.  Unknown escapes are
    kept verbatim so the function never raises on adversarial input.
    """

    out: list[str] = []
    index = 0
    length = len(literal)
    while index < length:
        character = literal[index]
        if character != "\\":
            out.append(character)
            index += 1
            continue
        index += 1
        if index >= length:
            break
        marker = literal[index]
        window = literal[index + 1 : index + 3]
        if marker == "x" and re.fullmatch(r"[0-9a-fA-F]{2}", window):
            out.append(chr(int(window, 16)))
            index += 3
            continue
        window = literal[index + 1 : index + 5]
        if marker == "u" and re.fullmatch(r"[0-9a-fA-F]{4}", window):
            out.append(chr(int(window, 16)))
            index += 5
            continue
        window = literal[index + 1 : index + 9]
        if marker == "U" and re.fullmatch(r"[0-9a-fA-F]{8}", window):
            codepoint = int(window, 16)
            if codepoint < 0x110000:
                out.append(chr(codepoint))
            index += 9
            continue
        if marker in "01234567":
            octal = re.match(r"[0-7]{1,3}", literal[index:])
            if octal is not None:
                out.append(chr(int(octal.group(0), 8)))
                index += len(octal.group(0))
                continue
        out.append(_GO_SIMPLE_ESCAPES.get(marker, marker))
        index += 1
    return "".join(out)


def _literal_value(token: str) -> str:
    """Return the run-time text of one string literal token (incl. quotes)."""

    if token.startswith("`"):
        return token[1:-1]
    if token[:1] in ("'", '"'):
        return decode_go_string_escapes(token[1:-1])
    return token


def _byte_array_value(body: str) -> str:
    numbers: list[int] = []
    for token in _INT_TOKEN.findall(body):
        try:
            numbers.append(int(token, 0))
        except ValueError:
            continue
    if not numbers:
        return ""
    return bytes(number & 0xFF for number in numbers).decode("utf-8", "ignore")


def resolve_literal_expression(
    expression: str, bindings: Mapping[str, str] | None = None
) -> str | None:
    """Resolve a source-level expression to its run-time string, or ``None``.

    Handles: string literals, ``"a" + "b"`` chains, Go escape sequences,
    ``[]byte{...}`` / ``string([]byte{...})``, ``[]byte("x")`` and identifiers
    bound to any of the above by ``const``/``var``/``:=``.
    """

    table = bindings or {}
    stripped = expression.strip().rstrip(";").strip()
    while len(stripped) >= 2 and stripped[0] == "(" and stripped[-1] == ")":
        inner = stripped[1:-1].strip()
        if inner.count("(") != inner.count(")"):
            break
        stripped = inner
    if not stripped:
        return None
    if _LITERAL_CHAIN.fullmatch(stripped):
        parts = _SINGLE_LITERAL.findall(stripped)
        return "".join(_literal_value(part) for part in parts)
    if _SINGLE_LITERAL.fullmatch(stripped):
        return _literal_value(stripped)
    conversion = _BYTE_CONVERSION.search(stripped)
    if conversion is not None:
        return _literal_value(conversion.group(1))
    array = _BYTE_ARRAY.search(stripped)
    if array is not None:
        value = _byte_array_value(array.group(1))
        if value:
            return value
    stringified = _STRING_CONVERSION.fullmatch(stripped)
    if stringified is not None:
        return resolve_literal_expression(stringified.group(1), table)
    if re.fullmatch(r"[A-Za-z_$][\w$]*", stripped):
        return table.get(stripped)
    return None


def local_string_bindings(text: str) -> dict[str, str]:
    """Collect ``name = <literal expression>`` bindings (three passes)."""

    bindings: dict[str, str] = {}
    for _ in range(3):
        for match in _ASSIGNMENT_LINE.finditer(text):
            value = resolve_literal_expression(match.group(2), bindings)
            if value:
                bindings[match.group(1)] = value
    for match in _CALL_BINDING_LINE.finditer(text):
        literals = _SINGLE_LITERAL.findall(match.group(3))
        if len(literals) != 1:
            continue
        bindings.setdefault(match.group(1), _literal_value(literals[0]))
    return bindings


def literal_scan_variants(text: str) -> list[str]:
    """Return comment-free views of ``text`` for literal-rule substring scans.

    In addition to the raw text this exposes (a) the text with quotes/plus
    signs/backticks/whitespace removed, so ``"serie-core-" + "rs"`` collapses to
    ``serie-core-rs``, and (b) the text with Go string escapes decoded, so
    ``"\\x73\\x65\\x72\\x69\\x65"`` collapses to ``serie``.
    """

    variants = [text]
    collapsed = re.sub(r"[\s'\"`+]", "", text)
    if collapsed != text:
        variants.append(collapsed)
    decoded = decode_go_string_escapes(text)
    if decoded != text:
        variants.append(decoded)
        collapsed_decoded = re.sub(r"[\s'\"`+]", "", decoded)
        if collapsed_decoded not in variants:
            variants.append(collapsed_decoded)
    return variants


def strip_detector_comments(text: str, suffix: str = "") -> str:
    """Blank comments while preserving actionable loader/cgo directives.

    ``#cgo`` lines are preserved because they carry real linker flags.  Line
    comments and block comments otherwise cannot produce detector evidence.
    """

    # Preserve block comments that contain cgo directives by replacing only the
    # delimiters, then remove ordinary block comments.
    def _block(match: re.Match[str]) -> str:
        body = match.group(0)
        return body if "#cgo" in body.casefold() else " " * len(body)

    value = re.sub(r"/\*.*?\*/", _block, text, flags=re.S)
    lines: list[str] = []
    for line in value.splitlines(keepends=True):
        lowered = line.casefold()
        if "#cgo" in lowered or "//go:" in lowered:
            lines.append(line)
            continue
        marker = line.find("//")
        if marker >= 0:
            line = line[:marker] + "\n"
        if suffix.casefold() == ".py" and "#" in line:
            marker = line.find("#")
            if marker >= 0:
                line = line[:marker] + "\n"
        lines.append(line)
    return "".join(lines)


__all__ = [
    "decode_go_string_escapes",
    "literal_scan_variants",
    "local_string_bindings",
    "native_runtime_signals",
    "process_runtime_tokens",
    "process_token_present",
    "resolve_literal_expression",
    "source_language",
    "source_name",
    "strip_detector_comments",
    "target_language",
    "target_source_suffixes",
    "token_present",
]
