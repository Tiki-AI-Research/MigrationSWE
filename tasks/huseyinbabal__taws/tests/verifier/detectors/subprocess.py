"""Detect process sinks that invoke the source language runtime.

This is deliberately narrower than a full taint engine: a hit is L2 only when a
process sink and a source-runtime token are both present in the candidate tree.
The detector still handles the common obfuscations that plain literal rules
miss: aliases, qualified names, whitespace and single-character concatenation.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .runtime_identity import (
    ambiguous_program_tokens,
    process_runtime_tokens,
    process_token_present,
    token_present,
)


_SOURCE_SUFFIXES = frozenset(
    {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".py", ".go", ".rs", ".java"}
)
# Only version-control metadata and bytecode/tool caches are skipped by name:
# they definitionally cannot hold authored production source, and their names
# are not attacker-controllable in a way that hides a fallback.
#
# Every other whole-file / whole-directory exemption was removed as a
# circumvention surface (round-3 finding ``RC-MI-GENERATED-MARKER-BYPASS``):
#
# * build-output-looking directory names (``target``/``dist``/``build``) are no
#   longer skipped -- a migrated service legitimately keeps hand-written source
#   in ``internal/build`` and an earlier round hid a fallback there;
# * dependency trees (``vendor``/``node_modules``) are no longer skipped -- the
#   source-runtime vocabulary is derived from the *source* technology, so a
#   vendored Go package can never be a false positive for a Rust->Go migration,
#   and a fallback could otherwise be parked under ``vendor/``;
# * a machine-written banner ("``// Code generated ... DO NOT EDIT.``") is
#   attacker-controlled *text*, so it no longer exempts a file.  Deciding whether
#   to scan a file by a comment the candidate can freely write let one comment
#   opt the whole file out of the subprocess, dynamic-load and FFI detectors at
#   once.  Scanning is safe because a hit still requires a process sink *and* a
#   source-runtime token, which legitimate generated/vendored code never pairs.
_IGNORED_PARTS = frozenset(
    {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
)
# Test-only artifacts are not production code.  The migration constraint scopes
# the fallback ban to the migrated *production* implementation, so a candidate's
# own tests must not be read as delegation: a Go ``*_test.go`` file is never
# linked into the production binary (``go build`` skips it), and ``testdata/`` is
# ignored by the Go toolchain, so neither can host a fallback the migrated
# program actually runs.  Scanning them only produced false positives -- for
# example a completion test shelling out to ``bash`` to exercise the generated
# ``_taws`` completion function, where ``taws`` is the command word being
# completed rather than a program being invoked.
#
# Each suffix keeps its own toolchain's convention, so a production file whose
# name merely starts with ``test`` (``test_helpers.go``, which Go *does* compile
# into the package) is still scanned.
_GO_TEST_NAME = re.compile(r"(?:^|/)[^/]*_test\.go$", re.IGNORECASE)
_PY_TEST_NAME = re.compile(
    r"(?:^|/)(?:test_[^/]*\.py|[^/]*_test\.py|conftest\.py)$", re.IGNORECASE
)
_JS_TEST_NAME = re.compile(
    r"(?:^|/)[^/]*\.(?:test|spec)\.[cm]?[jt]sx?$", re.IGNORECASE
)
_JAVA_TEST_NAME = re.compile(r"(?:^|/)[^/]*(?:Test|Tests|Spec)\.java$", re.IGNORECASE)
_TEST_ARTIFACT_DIRS = frozenset({"testdata", "__tests__"})
_SINKS = (
    "child_process",
    "child_process.spawn(",
    "child_process.exec(",
    "child_process.fork(",
    "spawnSync(",
    "execSync(",
    "execFile(",
    "worker_threads",
    "subprocess.",
    "os.system(",
    "os.popen(",
    "os.exec",
    "os.StartProcess(",
    "exec.Command(",
    "exec.CommandContext(",
    "syscall.Exec(",
    "syscall.ForkExec(",
    "std::process::Command",
    "Command::new(",
    "tokio::process",
    "process::Command",
    "ProcessBuilder(",
    "Runtime.exec(",
    "Runtime.getRuntime().exec(",
)

# Low-level process-creation / process-image APIs.  A migrated service has no
# legitimate reason to spawn a helper process through them, so the call form is
# fail-closed on its own even when the invoked binary is resolved dynamically
# (for example from an environment variable or a split/obfuscated literal).
# Higher-level ``exec.Command`` stays token-gated because ordinary tooling can
# legitimately shell out to a non-source program; these primitives cannot.
_FAIL_CLOSED_SINKS = (
    "os.StartProcess(",
    "syscall.Exec(",
    "syscall.ForkExec(",
)

# The same process-image primitives taken as *values* (``reflect.ValueOf(
# os.StartProcess)``, ``launcher := os.StartProcess``, a helper argument, a
# struct field or a slice element) never carry a trailing call paren at the
# reference site, so the call-shaped sinks above miss them.  A migrated service
# has no legitimate reason to reference these primitives at all, so a bare value
# reference is fail-closed evidence on its own.
_FAIL_CLOSED_VALUE = (
    "os.StartProcess",
    "syscall.Exec",
    "syscall.ForkExec",
)

# ``os/exec`` command constructors taken as a *value* instead of being called
# (``run := exec.Command``, ``reflect.ValueOf(exec.Command)``, a struct field or
# a slice element).  The call form stays token-gated; the value form is a
# delegation mechanism even when the program name is obfuscated.
_EXEC_VALUE_NAMES = ("Command", "CommandContext")

_WINDOW = 240


@dataclass(frozen=True)
class SubprocessHit:
    path: str
    line: int
    sink: str
    token: str
    evidence: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "detector": "subprocess",
            "capability": "callgraph",
            "path": self.path,
            "line": self.line,
            "sink": self.sink,
            "token": self.token,
            "evidence": self.evidence,
        }


def source_runtime_tokens(config: Mapping[str, Any]) -> set[str]:
    """Backward-compatible name for process-level runtime tokens."""

    return process_runtime_tokens(config)


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"(?m)//.*$", " ", text)
    text = re.sub(r"(?m)#.*$", " ", text)
    return text


def _collapsed(text: str) -> str:
    """Remove quotes, plus signs and whitespace to expose 'g' + 'o' splits."""

    return re.sub(r"[\s'\"+`]", "", text).casefold()


def _joined_literals(text: str) -> str:
    """Concatenate string literals in a window to expose 'g' + 'o' splits."""

    return "".join(re.findall(r"['\"]([^'\"]*)['\"]", text)).casefold()


_STRING_LITERAL = r"""'(?:[^'\\\n]|\\.)*'|"(?:[^"\\\n]|\\.)*"|`[^`]*`"""
_STRING_RE = re.compile(_STRING_LITERAL)
# A constant string assignment, whether it terminates the line or is followed by
# further statements (``; exec.Command(name)``).  Requiring end-of-line let a
# one-liner statement sequence hide the assignment from the folder.
_ASSIGN_RE = re.compile(
    rf"""(?<![\w$.])([A-Za-z_$][\w$]*)[ \t]*(?::=|=)[ \t]*"""
    rf"""(?P<rhs>(?:{_STRING_LITERAL})[ \t]*(?:\+[ \t]*(?:{_STRING_LITERAL}))*)"""
    r"""(?=[ \t]*(?:[;\n]|$))"""
)


# A hidden fallback can rebuild the source-runtime command name at run time so
# no single statement holds the literal token: numeric byte arrays, decoder
# calls, or a helper's string concatenation.  Nothing that reconstructs a
# command is itself a legitimate migration technique, so fold those constant
# expressions back into a literal before the sink/token co-occurrence check.
_CONST_BYTE_STRING_RE = re.compile(
    r"string\s*\(\s*\[\](?:byte|rune)\s*\{([^{}]*)\}\s*\)"
)
_CONST_BYTE_ARRAY_RE = re.compile(r"\[\](?:byte|rune)\s*\{([^{}]*)\}")
_CONST_BASE64_RE = re.compile(
    r"base64\.[A-Za-z]*Encoding\.DecodeString\s*\(\s*\"([^\"\\\n]*)\"\s*\)"
)
_CONST_BASE32_RE = re.compile(
    r"base32\.[A-Za-z]*Encoding\.DecodeString\s*\(\s*\"([^\"\\\n]*)\"\s*\)"
)
_CONST_HEX_RE = re.compile(r"hex\.DecodeString\s*\(\s*\"([^\"\\\n]*)\"\s*\)")
# ``fmt.Sprintf`` and ``strings.Join`` rebuild a command name at run time from
# constant pieces so no single statement holds the literal token.  A migrated
# program that reconstructs a *command* this way is doing the same thing as
# ``"car" + "go"``; fold the constant pieces back into a literal so the
# sink/token co-occurrence stays visible.
_CONST_SPRINTF_RE = re.compile(r"fmt\.Sprintf\((?P<body>[^()]*)\)")
_CONST_JOIN_RE = re.compile(
    r"strings\.Join\(\s*\[\]string\s*\{(?P<body>[^{}]*)\}\s*,\s*(?P<sep>\"(?:[^\"\\\n]|\\.)*\")\s*\)"
)
_GO_PRINTF_VERB_RE = re.compile(
    r"%(?:\[(?P<index>\d+)\])?[+#0\- ]*(?:\d+)?(?:\.\d+)?(?P<verb>[sdvqtxXobfceEgG%])"
)
_CONST_FUNC_RETURN_RE = re.compile(
    r"func\s+(?P<name>[A-Za-z_]\w*)\s*\(\s*\)\s*string\s*\{\s*"
    r"return\s+(?P<expr>[^;{}]*?)\s*\}",
    re.S,
)
_CONST_ASSIGN_RE = re.compile(
    r"(?<![\w$.])(?P<names>[A-Za-z_$][\w$]*(?:[ \t]*,[ \t]*[A-Za-z_$][\w$]*)*)"
    r"[ \t]*:?=[ \t]*(?P<rhs>\"(?:[^\"\\\n]|\\.)*\"|`[^`]*`)"
    r"(?=[ \t]*(?:[;\n]|$))"
)
_CONST_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", "\\": "\\", "'": "'", '"': '"'}


def _literal_value(expr: str) -> str | None:
    """Return the constant value when ``expr`` is only literal concatenation."""

    parts = _STRING_RE.findall(expr)
    if not parts:
        return None
    remainder = _STRING_RE.sub("", expr)
    if remainder.strip(" \t\r\n+()"):
        return None
    return "".join(part[1:-1] for part in parts)


def _decode_numeric_array(body: str) -> str | None:
    values: list[int] = []
    for item in body.split(","):
        token = item.strip()
        if not token:
            continue
        token = re.sub(r"^(?:byte|rune|int|int32|uint8)\s*\((.*)\)$", r"\1", token).strip()
        if len(token) >= 2 and token[0] == "'" and token[-1] == "'":
            inner = token[1:-1]
            if len(inner) == 1:
                values.append(ord(inner))
                continue
            if len(inner) == 2 and inner[0] == "\\" and inner[1] in _CONST_ESCAPES:
                values.append(ord(_CONST_ESCAPES[inner[1]]))
                continue
            return None
        try:
            values.append(int(token, 0))
        except ValueError:
            return None
    if not values:
        return None
    try:
        value = bytes(values).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    return value if value.isprintable() and len(value) <= 64 else None


def _literal_wrap(value: str | None) -> str | None:
    """Render ``value`` as a simple double-quoted literal when it is safe to do so."""

    if not value or not any(char.isalnum() for char in value):
        return None
    if any(char in value for char in "\"'`\\\n\r"):
        return None
    return '"' + value + '"'


def _fold_numeric_arrays(text: str) -> str:
    def _convert(match: re.Match[str]) -> str:
        literal = _literal_wrap(_decode_numeric_array(match.group(1)))
        return literal if literal is not None else match.group(0)

    text = _CONST_BYTE_STRING_RE.sub(_convert, text)
    return _CONST_BYTE_ARRAY_RE.sub(_convert, text)


def _fold_decoders(text: str) -> str:
    def _base64(match: re.Match[str]) -> str:
        raw = match.group(1)
        try:
            value = base64.b64decode(raw + "=" * (-len(raw) % 4), validate=False).decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError):
            return match.group(0)
        literal = _literal_wrap(value)
        return literal if literal is not None else match.group(0)

    def _base32(match: re.Match[str]) -> str:
        raw = match.group(1).upper()
        try:
            value = base64.b32decode(raw + "=" * (-len(raw) % 8), casefold=True).decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError):
            return match.group(0)
        literal = _literal_wrap(value)
        return literal if literal is not None else match.group(0)

    def _hex(match: re.Match[str]) -> str:
        try:
            value = bytes.fromhex(match.group(1)).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return match.group(0)
        literal = _literal_wrap(value)
        return literal if literal is not None else match.group(0)

    text = _CONST_BASE64_RE.sub(_base64, text)
    text = _CONST_BASE32_RE.sub(_base32, text)
    return _CONST_HEX_RE.sub(_hex, text)


def _split_top_level(body: str) -> list[str]:
    """Split on commas that are not nested inside brackets/braces/parens."""

    parts: list[str] = []
    current: list[str] = []
    depth = 0
    for char in body:
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))
    return [item.strip() for item in parts]


def _go_sprintf(fmt: str, args: Sequence[str]) -> str | None:
    """Evaluate a constant ``fmt.Sprintf`` format against literal arguments."""

    pieces: list[str] = []
    position = 0
    sequential = 0
    for match in _GO_PRINTF_VERB_RE.finditer(fmt):
        pieces.append(fmt[position:match.start()])
        position = match.end()
        verb = match.group("verb")
        if verb == "%":
            pieces.append("%")
            continue
        index = match.group("index")
        if index is not None:
            slot = int(index) - 1
        else:
            slot = sequential
            sequential += 1
        if slot < 0 or slot >= len(args):
            return None
        argument = args[slot]
        if verb in "sdvq":
            pieces.append(argument)
        elif verb in "txXob":
            try:
                pieces.append(format(int(argument, 0), {"x": "x", "X": "X", "o": "o", "b": "b"}.get(verb, "x")))
            except ValueError:
                return None
        elif verb == "c":
            try:
                pieces.append(chr(int(argument, 0)))
            except ValueError:
                return None
        elif verb in "fegEG":
            try:
                pieces.append(repr(float(argument)))
            except ValueError:
                return None
        else:
            return None
    pieces.append(fmt[position:])
    return "".join(pieces)


def _fold_sprintf(text: str) -> str:
    def _convert(match: re.Match[str]) -> str:
        parts = _split_top_level(match.group("body"))
        if not parts:
            return match.group(0)
        fmt = _literal_value(parts[0])
        if fmt is None:
            return match.group(0)
        arguments: list[str] = []
        for raw in parts[1:]:
            value = _literal_value(raw)
            if value is None:
                return match.group(0)
            arguments.append(value)
        folded = _go_sprintf(fmt, arguments)
        literal = _literal_wrap(folded)
        return literal if literal is not None else match.group(0)

    return _CONST_SPRINTF_RE.sub(_convert, text)


def _fold_strings_join(text: str) -> str:
    def _convert(match: re.Match[str]) -> str:
        pieces = [
            _literal_value(item)
            for item in _split_top_level(match.group("body"))
            if item
        ]
        if any(piece is None for piece in pieces):
            return match.group(0)
        separator = _literal_value(match.group("sep"))
        if separator is None:
            return match.group(0)
        literal = _literal_wrap(separator.join(piece for piece in pieces if piece is not None))
        return literal if literal is not None else match.group(0)

    return _CONST_JOIN_RE.sub(_convert, text)


def _fold_function_returns(text: str) -> str:
    folded = text
    for match in _CONST_FUNC_RETURN_RE.finditer(text):
        literal = _literal_wrap(_literal_value(match.group("expr")))
        if literal is None:
            continue
        folded = re.sub(
            rf"(?<![\w$.]){re.escape(match.group('name'))}\s*\(\s*\)",
            lambda _m, _lit=literal: _lit,
            folded,
        )
    return folded


def _fold_string_assignments(text: str) -> str:
    folded = text
    for match in _CONST_ASSIGN_RE.finditer(text):
        value = match.group("rhs")[1:-1]
        if not value:
            continue
        replacement = '"' + value + '"'
        for name in re.split(r"[ \t]*,[ \t]*", match.group("names")):
            if not name or name == "_":
                continue
            folded = re.sub(
                rf"(?<![\w$]){re.escape(name)}(?![\w$])",
                lambda _m, _lit=replacement: _lit,
                folded,
            )
    return folded


def _canonicalize_import_aliases(text: str) -> str:
    """Rewrite explicit import aliases back to their package's declared name.

    Attackers rename the *package* (``import b64 "encoding/base64"``) so the
    literal decoder pattern ``base64\\.`` no longer matches and the constant is
    never folded.  The package path is the stable identity, so map each explicit
    alias qualifier onto the path's last segment before folding: ``b64.`` ->
    ``base64.``, ``f.`` -> ``fmt.``, ``str.`` -> ``strings.``, ``h.`` -> ``hex.``
    (and ``o.`` -> ``os.``, ``ex.`` -> ``exec.`` ...).  This makes every existing
    constant folder alias-aware at once instead of enumerating decoder spellings.
    """

    for name, path in _go_import_specs(text):
        if name in {"", ".", "_"}:
            continue
        canonical = path.rsplit("/", 1)[-1]
        if canonical and canonical != name:
            text = re.sub(
                rf"(?<![\w.]){re.escape(name)}(?=\s*\.)",
                canonical,
                text,
            )
    return text


def _resolve_constant_text(text: str) -> str:
    """Fold runtime-constructed constants (arrays, decoders, helpers) to literals."""

    resolved = _canonicalize_import_aliases(text)
    for _ in range(4):
        previous = resolved
        resolved = _fold_decoders(resolved)
        resolved = _fold_numeric_arrays(resolved)
        resolved = _fold_function_returns(resolved)
        resolved = _fold_string_assignments(resolved)
        resolved = _fold_literal_assignments(resolved)
        resolved = _fold_sprintf(resolved)
        resolved = _fold_strings_join(resolved)
        if resolved == previous:
            break
    return resolved


def _fold_literal_assignments(text: str) -> str:
    """Inline simple ``name := "a" + "b"`` concatenations into their uses.

    A split runtime token is often parked in a variable (``bin := "ca" +
    "rgo"``) and only referenced from the process sink, so a window around the
    sink never sees the concatenation.  Folding those constant assignments back
    into the sink window keeps the sink/token co-occurrence visible without
    resorting to whole-file substring matching.
    """

    folded = text
    for match in _ASSIGN_RE.finditer(text):
        name = match.group(1)
        value = "".join(
            part[1:-1] for part in _STRING_RE.findall(match.group("rhs"))
        )
        if not value or not any(char.isalnum() for char in value):
            continue
        folded = re.sub(
            rf"(?<![\w$]){re.escape(name)}(?![\w$])",
            lambda _m, _lit='"' + value + '"': _lit,
            folded,
        )
    return folded


def _alias_sinks(text: str) -> list[str]:
    aliases: list[str] = []
    for match in re.finditer(
        r"use\s+std::process::Command\s+as\s+([A-Za-z_][A-Za-z0-9_]*)", text
    ):
        aliases.append(f"{match.group(1)}::new(")
    for match in re.finditer(
        r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*require\(\s*['\"]"
        r"(?:node:)?child_process['\"]\s*\)",
        text,
    ):
        aliases.extend(
            f"{match.group(1)}.{name}(" for name in ("spawn", "exec", "fork", "execFile")
        )
    for match in re.finditer(
        r"import\s+subprocess\s+as\s+([A-Za-z_][A-Za-z0-9_]*)", text
    ):
        aliases.append(f"{match.group(1)}.")
    for match in re.finditer(
        r"import\s+([A-Za-z_][A-Za-z0-9_]*)\s+['\"]os/exec['\"]", text
    ):
        aliases.append(f"{match.group(1)}.Command(")
    return aliases


def _token_present(text: str, token: str) -> bool:
    """Backward-compatible lexical matcher for test/internal callers."""

    return token_present(text, token)


_GO_IMPORT_LITERAL = r'("(?:[^"\\]|\\.)*"|`[^`]*`)'


def _go_unquote(literal: str) -> str:
    if len(literal) >= 2 and literal[0] in "\"`":
        return literal[1:-1]
    return literal


def _go_import_specs(text: str) -> list[tuple[str, str]]:
    """Return ``(local_name, import_path)`` for every Go import spec.

    ``local_name`` is the explicit alias, ``"."`` for a dot import, ``"_"`` for
    a blank import or ``""`` when the imported package's declared name is used.
    Go permits both interpreted (``"os"``) and raw (``\\`os/exec\\```) string
    literals for an import path, so both must be recognised; otherwise a
    rename-only obfuscation of the process sink slides through.
    """

    specs: list[tuple[str, str]] = []
    single = re.compile(
        r"(?m)^\s*import\s+(?:([A-Za-z_][A-Za-z0-9_]*|\.|_)\s+)?"
        + _GO_IMPORT_LITERAL
    )
    for match in single.finditer(text):
        specs.append((match.group(1) or "", _go_unquote(match.group(2))))
    block = re.compile(r"(?ms)^\s*import\s*\((.*?)\)")
    spec = re.compile(
        r"^(?:([A-Za-z_][A-Za-z0-9_]*|\.|_)\s+)?" + _GO_IMPORT_LITERAL + r"$"
    )
    for group in block.finditer(text):
        for raw in re.split(r"[\n;]", group.group(1)):
            candidate = re.sub(r"//.*$", "", raw).strip()
            if not candidate:
                continue
            match = spec.match(candidate)
            if match:
                specs.append((match.group(1) or "", _go_unquote(match.group(2))))
    return specs


def _go_package_bindings(text: str, package: str) -> tuple[set[str], bool]:
    """Return local qualifiers and dot-import status for ``package``."""

    names: set[str] = set()
    dotted = False
    for name, path in _go_import_specs(text):
        if path != package:
            continue
        if name == ".":
            dotted = True
        elif name == "_":
            continue
        elif name:
            names.add(name)
        else:
            names.add(path.rsplit("/", 1)[-1])
    return names, dotted


def _go_process_mechanisms(text: str) -> set[str]:
    """Names of the process-creation mechanisms reachable from imports.

    Includes qualified names (``os.StartProcess``), dot-imported bare names and
    explicit renames, so a value reference can be resolved without assuming the
    canonical package qualifier.
    """

    mechanisms: set[str] = set()
    os_names, os_dot = _go_package_bindings(text, "os")
    syscall_names, syscall_dot = _go_package_bindings(text, "syscall")
    exec_names, exec_dot = _go_package_bindings(text, "os/exec")
    for alias in os_names:
        mechanisms.add(f"{alias}.StartProcess")
    for alias in syscall_names:
        mechanisms.update((f"{alias}.Exec", f"{alias}.ForkExec"))
    for alias in exec_names:
        mechanisms.update((f"{alias}.Command", f"{alias}.CommandContext"))
    if os_dot:
        mechanisms.add("StartProcess")
    if syscall_dot:
        mechanisms.update(("Exec", "ForkExec"))
    if exec_dot:
        mechanisms.update(("Command", "CommandContext"))
    return mechanisms


def _go_value_targets(text: str) -> set[str]:
    """Names whose bare (non-call) reference is fail-closed evidence.

    Low-level process-image primitives (``os.StartProcess`` / ``syscall.Exec`` /
    ``syscall.ForkExec``) and ``os/exec`` command constructors are included via
    the canonical spellings and every import-resolved alias (renamed imports,
    dot imports, raw import paths).
    """

    targets: set[str] = set(_FAIL_CLOSED_VALUE)
    targets.update(f"exec.{name}" for name in _EXEC_VALUE_NAMES)
    for mechanism in _go_process_mechanisms(text):
        leaf = mechanism.rsplit(".", 1)[-1]
        if leaf in {"StartProcess", "Exec", "ForkExec"}:
            targets.add(mechanism)
        elif leaf in _EXEC_VALUE_NAMES:
            targets.add(mechanism)
    return targets


def _go_exec_value_aliases(text: str) -> tuple[dict[str, int], set[int]]:
    """Track simple ``os/exec`` constructor aliases instead of failing closed.

    The task expressly permits a small set of external commands.  Production
    code commonly makes those call sites replaceable in tests with an alias such
    as ``var execCommand = exec.Command``.  Treating the constructor value alone
    as source-runtime delegation rejects that permitted pattern before the
    invoked program is inspected.

    Only a direct identifier binding is exempted here.  The returned forwarder
    summary lets the existing cross-function analysis inspect every call of the
    alias and still reject ``execCommand("cargo", ...)`` or a renamed source
    binary.  Other value uses (reflection, function arguments, fields, slices,
    and so on) remain fail-closed because their eventual target cannot be
    resolved locally.
    """

    aliases: dict[str, int] = {}
    exempt_offsets: set[int] = set()
    for mechanism in sorted(_go_process_mechanisms(text), key=len, reverse=True):
        leaf = mechanism.rsplit(".", 1)[-1]
        if leaf not in _EXEC_VALUE_NAMES:
            continue
        argument_index = 1 if leaf == "CommandContext" else 0
        pattern = re.compile(
            rf"(?<![\w.])(?:var\s+)?(?P<name>[A-Za-z_]\w*)\s*"
            rf"(?::=|=)\s*(?P<value>{re.escape(mechanism)})(?![\w.])"
            rf"(?!\s*\()"
        )
        for match in pattern.finditer(text):
            aliases.setdefault(match.group("name"), argument_index)
            exempt_offsets.add(match.start("value"))
    return aliases, exempt_offsets


def _go_fail_closed_calls(text: str) -> set[str]:
    """Import-resolved *call* forms of the low-level process-image primitives.

    A renamed import (``import o "os"``), a dot import (``. "syscall"``) or a
    raw import path reaches the same primitive under a different spelling, so
    the *call* form must stay fail-closed exactly like the canonical
    ``os.StartProcess(`` / ``syscall.Exec(`` / ``syscall.ForkExec(``.  A
    migrated service has no legitimate reason to spawn a helper through them.
    """

    calls: set[str] = set()
    for mechanism in _go_process_mechanisms(text):
        if mechanism.rsplit(".", 1)[-1] in {"StartProcess", "Exec", "ForkExec"}:
            calls.add(f"{mechanism}(")
    return calls


def _go_gated_calls(text: str) -> set[str]:
    """Import-resolved *call* forms of the ``os/exec`` command constructors.

    ``Command`` / ``CommandContext`` reached through a dot import (no qualifier
    at the call site) or a rename are still command-construction sites, so they
    keep the token-gated behaviour of ``exec.Command``: an ordinary tool call
    with no source-runtime token stays legal.
    """

    calls: set[str] = set()
    for mechanism in _go_process_mechanisms(text):
        if mechanism.rsplit(".", 1)[-1] in _EXEC_VALUE_NAMES:
            calls.add(f"{mechanism}(")
    return calls


def _normalized_identifier(value: str) -> str:
    """Casefold and drop every non-alphanumeric separator from an identifier.

    ``taws-rs``, ``taws_rs`` and ``tawsrs`` are the same source program spelled
    with different separators; normalising lets one derived token match the
    whole renamed-binary family instead of enumerating spellings.
    """

    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _matched_token(argument: str, wanted: Sequence[str] | set[str]) -> str | None:
    """Match a source-runtime token inside a resolved program-name argument."""

    haystack = _normalized_identifier(argument)
    if not haystack:
        return None
    return next(
        (
            token for token in sorted(wanted)
            if len(_normalized_identifier(token)) >= 3
            and _normalized_identifier(token) in haystack
        ),
        None,
    )


def _call_arguments(window: str, sink: str) -> list[str]:
    """Return the top-level comma-separated arguments of a call sink."""

    index = len(sink)
    depth = 0
    arguments: list[str] = []
    current: list[str] = []
    while index < len(window):
        char = window[index]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif char == "," and depth == 0:
            arguments.append("".join(current))
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    arguments.append("".join(current))
    return [item.strip() for item in arguments if item.strip()]


def _argument_token_match(window: str, sink: str, wanted: set[str]) -> str | None:
    """Match a wanted token against the call's constant arguments, separator-insensitively.

    Program names are attacker-controlled (``"taws-rs"``, ``"taws_rs"``,
    ``"taws"+"rs"``): the window-level word-boundary match sees the first
    spelling but not the underscore form, so the resolved arguments are also
    compared after removing separators.  Only the sink's own arguments are
    inspected, so a ``.taws`` path elsewhere in the window is not mistaken for a
    program name.
    """

    if not sink.endswith("("):
        return None
    haystack = _normalized_identifier(" ".join(_call_arguments(window, sink)))
    if not haystack:
        return None
    for token in sorted(wanted):
        needle = _normalized_identifier(token)
        if len(needle) >= 3 and needle in haystack:
            return token
    return None


def _program_argument_token_match(window: str, sink: str, wanted: set[str]) -> str | None:
    """Match an *ambiguous* token only where it is the sink's program argument.

    When the source-program name is also the target executable name (the
    migration preserves the project name), its presence anywhere in the sink
    window is not evidence of delegation: it is routinely a command *word* in a
    shell string (``exec.Command("bash", "-c", "COMP_WORDS=(taws ...)")``), an
    argument payload or a test fixture.  Requiring the token to be the sink's
    derived program argument -- the first argument of ``exec.Command``, the
    second of ``exec.CommandContext`` -- keeps a genuine re-spawn of the source
    binary (``exec.Command("taws", ...)``) detectable while ignoring incidental
    mentions elsewhere in the arguments or the window.
    """

    if not wanted or not sink.endswith("("):
        return None
    index = _PROGRAM_ARG_INDEX.get(sink)
    if index is None:
        index = _PROGRAM_ARG_LEAF.get(sink.rsplit(".", 1)[-1])
    if index is None:
        return None
    arguments = _call_arguments(window, sink)
    if index >= len(arguments):
        return None
    return _matched_token(arguments[index], wanted)


# ---------------------------------------------------------------------------
# Command-word resolution for the *ambiguous* source-program token
# ---------------------------------------------------------------------------
#
# When the migration preserves the project name, the source program's name is
# also the target executable's name (``taws``): the migrated binary is built
# under the same spelling.  The token's mere presence in a sink window is then
# not evidence -- it is routinely a *data* word (a ``COMP_WORDS=(...)``
# completion entry, an argument payload, a fixture path).  Requiring it to be the
# sink's literal program *argument* keeps that false positive out, but it also
# lets the whole "launch the same-named binary through a launcher" family through:
# a shell script (``exec.Command("sh", "-c", "taws login")``), a prefix launcher
# (``exec.Command("env", "taws", "login")``), or a chain of either.  Those are
# still delegations to the source program, so the ambiguous token is matched
# wherever it is *executed as a program* -- the sink's own argument list and,
# recursively, the script payload of any shell it invokes -- while a token that
# only ever appears as data (an argument value, an assignment word, a completion
# word) is never a program word and stays ignored.
#
# The launcher/shell recognition is deliberately *not* a spelling of the source
# token (attackers vary that freely); it is the fixed set of standard process
# launchers and shells whose program operand position is well defined by
# convention.

_SHELL_NAMES = frozenset(
    {"sh", "bash", "zsh", "dash", "ksh", "ash", "busybox", "toybox", "csh", "tcsh", "fish"}
)
_LAUNCHER_NAMES = frozenset(
    {
        "env",
        "sudo",
        "doas",
        "nice",
        "ionice",
        "nohup",
        "setsid",
        "stdbuf",
        "timeout",
        "chrt",
        "setpriv",
        "runuser",
        "su",
        "command",
        "exec",
        "xargs",
        "time",
        "unshare",
        "nsenter",
        "strace",
        "ltrace",
        "fakeroot",
        "watch",
        "pkexec",
        "systemd-run",
        "dbus-run-session",
    }
)
# A launcher's own options and operands that are not its program word: flags
# (``-n``), ``KEY=VALUE`` assignments (``env FOO=bar prog``) and bare durations
# (``timeout 5 prog``).
_LAUNCHER_FLAG_RE = re.compile(r"^-[-\w]*$")
_LAUNCHER_ASSIGN_RE = re.compile(r"^[A-Za-z_]\w*=")
_LAUNCHER_DURATION_RE = re.compile(r"^\d+(?:\.\d+)?[smhd]?$")
_OPAQUE_WORD = "\x00"
_MAX_COMMAND_DEPTH = 6


def _command_word_token(word: str, wanted: Sequence[str] | set[str]) -> str | None:
    """Match a token that is the *program* named by a resolved command word.

    The word is reduced to its basename and matched on identifier boundaries, so
    ``taws``, ``/usr/bin/taws`` and ``taws-rs`` are hits while ``_taws`` (a shell
    completion *function* name) and ``taws_rs`` are not -- the underscore surface
    is exactly what the completion false positive looked like.
    """

    base = word.rsplit("/", 1)[-1].casefold()
    if not base or base == _OPAQUE_WORD:
        return None
    for token in sorted(wanted):
        needle = token.casefold()
        if len(needle) < 3:
            continue
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(needle)}(?![A-Za-z0-9_])", base):
            return token
    return None


def _shell_words(segment: str) -> list[str]:
    """Split one shell command line into words, honouring quotes."""

    words: list[str] = []
    current: list[str] = []
    quote: str | None = None
    escaped = False
    for character in segment:
        if escaped:
            current.append(character)
            escaped = False
            continue
        if character == "\\" and quote != "'":
            escaped = True
            continue
        if quote is not None:
            if character == quote:
                quote = None
            else:
                current.append(character)
            continue
        if character in "'\"":
            quote = character
            continue
        if character.isspace():
            if current:
                words.append("".join(current))
                current = []
            continue
        current.append(character)
    if current:
        words.append("".join(current))
    return words


def _shell_commands(script: str) -> list[list[str]]:
    """Split a shell script into command lines on top-level separators."""

    commands: list[list[str]] = []
    current: list[str] = []
    quote: str | None = None
    escaped = False
    index = 0
    while index < len(script):
        character = script[index]
        if escaped:
            current.append(character)
            escaped = False
            index += 1
            continue
        if character == "\\" and quote != "'":
            escaped = True
            current.append(character)
            index += 1
            continue
        if quote is not None:
            current.append(character)
            if character == quote:
                quote = None
            index += 1
            continue
        if character in "'\"":
            quote = character
            current.append(character)
            index += 1
            continue
        separator = None
        if character in ";&|\n":
            separator = character
            if character in ";&|" and index + 1 < len(script) and script[index + 1] == character:
                index += 1
        if separator is not None:
            commands.append(_shell_words("".join(current)))
            current = []
            index += 1
            continue
        current.append(character)
        index += 1
    commands.append(_shell_words("".join(current)))
    return [command for command in commands if command]


def _shell_script_arg(words: Sequence[str], start: int) -> tuple[str | None, int]:
    """Return ``(script, next_index)`` for a shell's ``-c``-style operand."""

    index = start
    while index < len(words):
        word = words[index]
        if not word.startswith("-"):
            break
        if "c" in word.lstrip("-"):
            if index + 1 < len(words):
                return words[index + 1], index + 2
            return None, index + 1
        index += 1
    return None, start


def _program_words(words: Sequence[str], depth: int = 0) -> list[str]:
    """Every word a command line *executes as a program*, resolving launchers.

    ``['env', 'taws', 'login']`` resolves to ``env`` plus ``taws`` (``env``'s
    program operand); ``['sh', '-c', 'exec taws login']`` resolves to ``sh`` plus
    the program word of the script payload (``exec`` is itself a launcher, so the
    payload's program is ``taws``).  The walk stops at the first word that is
    neither a launcher nor a shell, because everything after it is that program's
    data, not a further program.
    """

    programs: list[str] = []
    index = 0
    while index < len(words) and depth <= _MAX_COMMAND_DEPTH:
        word = words[index]
        if not word or word == _OPAQUE_WORD:
            index += 1
            continue
        if _LAUNCHER_FLAG_RE.match(word) or _LAUNCHER_ASSIGN_RE.match(word):
            index += 1
            continue
        programs.append(word)
        base = word.rsplit("/", 1)[-1].casefold()
        if base in _SHELL_NAMES:
            script, index = _shell_script_arg(words, index + 1)
            if script is not None:
                for command in _shell_commands(script):
                    programs.extend(_program_words(command, depth + 1))
            break
        if base in _LAUNCHER_NAMES:
            index += 1
            while index < len(words):
                operand = words[index]
                if (
                    _LAUNCHER_FLAG_RE.match(operand)
                    or _LAUNCHER_ASSIGN_RE.match(operand)
                    or _LAUNCHER_DURATION_RE.match(operand)
                ):
                    index += 1
                    continue
                break
            continue
        break
    return programs


def _ambiguous_command_match(window: str, sink: str, wanted: set[str]) -> str | None:
    """Match an ambiguous token used as the *program* of the sink's command chain.

    Only resolved program words are considered, so a same-named *data* word in the
    arguments (``exec.Command("aws", "--profile", "taws")``) is not a hit, while a
    same-named program reached through a shell or a launcher prefix is.
    """

    if not wanted or not sink.endswith("("):
        return None
    words = []
    for argument in _call_arguments(window, sink):
        value = _literal_value(argument)
        words.append(value if value is not None else _OPAQUE_WORD)
    for program in _program_words(words):
        matched = _command_word_token(program, wanted)
        if matched is not None:
            return matched
    return None


def _value_mentions(text: str, names: set[str]) -> list[tuple[str, int]]:
    """Offsets of bare (non-call) references to any name in *names*.

    ``(?!\\s*\\()`` skips the direct call form so the token-gated call sinks
    keep owning it; everything else (reflection, assignment, argument, struct
    field, slice element, ...) is reported.
    """

    mentions: list[tuple[str, int]] = []
    for name in sorted(names):
        pattern = (
            r"(?<![A-Za-z0-9_])"
            + re.escape(name)
            + r"(?![A-Za-z0-9_])(?!\s*\()"
        )
        for match in re.finditer(pattern, text):
            mentions.append((name, match.start()))
    return mentions



def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


# ---------------------------------------------------------------------------
# Cross-function delegation ("wrapper indirection")
#
# A hidden fallback does not have to spell the source program name next to the
# sink.  A common evasion is a neutral helper that forwards its *parameter* into
# the process constructor (``func RunSourceHelper(name string, args ...string)
# error { return exec.Command(name, args...).Run() }``); the call site then
# carries the source-program literal but no sink, and the sink carries only the
# parameter.  Round-2 QA (``atk_fallback_wrapper_indirection``) confirmed the
# 240-character sink window missed exactly this; round-3 QA
# (``atk_fallback_closure_indirection``) hid the same forwarding inside an
# *anonymous* function literal bound to a variable.  Recognising only the shape
# of the *declaration* (``func <name>(``) is itself an attacker-controlled
# surface, so summarise forwarding by **function value** instead: every function
# body, named or anonymous, is registered under each identifier that can
# reference it -- the declaration name, ``var x = func(...)`` / ``x := func(...)``
# bindings, struct fields, and function-value aliases -- and immediately-invoked
# literals are matched directly.  The call sites of those identifiers are then
# matched against the constant (folded) arguments.
# ---------------------------------------------------------------------------

_GO_FUNC_SIG = re.compile(
    r"func\s+(?:\([^)]*\)\s*)?(?P<name>[A-Za-z_]\w*)\s*\((?P<params>[^()]*)\)[^{;]*\{",
    re.S,
)
# An anonymous function literal: ``func`` immediately followed by its parameter
# list (no name between them).  A method declaration (``func (r T) Name(...)``)
# also matches here, but with the *receiver* captured as the parameter list,
# which never forwards a command name, so it is inert; the named signature above
# owns it.
_GO_ANON_FUNC_SIG = re.compile(
    r"(?<![\w.])func\s*\((?P<params>[^()]*)\)[^{;]*\{",
    re.S,
)
# Binding targets a function literal can be assigned to: ``var x = func (...)``,
# ``x := func (...)``, ``x = func (...)`` (variable) and a composite-literal
# field ``Field: func (...)``.  The binding is read from the text just before the
# ``func`` keyword.  Anchoring on the preceding identifier rather than the
# literal keeps the summary independent of which spelling the candidate chose.
_ANON_VAR_BIND_RE = re.compile(
    r"(?<![\w.])(?:var\s+|const\s+)?(?P<name>[A-Za-z_]\w*)\s*(?::=|=)\s*$"
)
_ANON_FIELD_BIND_RE = re.compile(r"(?<![\w.])(?P<name>[A-Za-z_]\w*)\s*:\s*$")
# A function value aliased onto another identifier (``y := x`` / ``var y = x``)
# where ``x`` is already a forwarding function value: ``y`` forwards identically.
_FUNC_VALUE_ALIAS_RE = re.compile(
    r"(?<![\w.])(?:var\s+)?(?P<lhs>[A-Za-z_]\w*)\s*(?::=|=)\s*"
    r"(?P<rhs>[A-Za-z_]\w*)\s*(?=[;\n]|$)"
)
_FIELD_REF_PREFIX = "."
# Command constructors whose *first program-name argument* is the fallback
# target.  ``exec.CommandContext`` takes the context first, so its program name
# is the second argument.
_FORWARD_SINKS: tuple[tuple[str, int], ...] = (
    ("exec.CommandContext(", 1),
    ("exec.Command(", 0),
    ("CommandContext(", 1),
    ("Command(", 0),
)

# Position of the *program name* argument for the process sinks whose spelling
# makes it unambiguous.  Used only to localise a token that is otherwise
# ambiguous (the source project name doubles as the target executable name), so
# that it must be the program argument rather than an incidental argument word.
_PROGRAM_ARG_INDEX: dict[str, int] = {sink: index for sink, index in _FORWARD_SINKS}
_PROGRAM_ARG_INDEX.update(
    {
        "spawnSync(": 0,
        "execSync(": 0,
        "execFile(": 0,
        "child_process.spawn(": 0,
        "child_process.exec(": 0,
        "child_process.fork(": 0,
    }
)
# Qualified/aliased spellings (``ex.Command(`` from ``import ex "os/exec"``) are
# resolved by their leaf call so the program-argument position is still found.
_PROGRAM_ARG_LEAF: dict[str, int] = {
    "CommandContext(": 1,
    "Command(": 0,
    "spawnSync(": 0,
    "execSync(": 0,
    "execFile(": 0,
    "spawn(": 0,
    "exec(": 0,
    "fork(": 0,
}


def _go_param_names(params: str) -> list[str | None]:
    """Ordered parameter names of a Go parameter list (``None`` for anonymous)."""

    names: list[str | None] = []
    for group in _split_top_level(params):
        text = group.strip()
        if not text:
            continue
        match = re.match(
            r"^(?P<names>[A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)?\s*(?P<type>.+)$",
            text,
        )
        if not match or not match.group("names"):
            names.append(None)
            continue
        names.extend(re.split(r"\s*,\s*", match.group("names")))
    return names


def _balanced_body_span(text: str, open_index: int) -> tuple[int, int]:
    """Return ``(body_start, body_end)`` for the ``{...}`` block at *open_index*."""

    depth = 0
    for index in range(open_index, len(text)):
        character = text[index]
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return open_index + 1, index
    return open_index + 1, len(text)


def _balanced_body(text: str, open_index: int) -> str:
    """Return the contents of the ``{...}`` block starting at *open_index*."""

    start, end = _balanced_body_span(text, open_index)
    return text[start:end]


def _anon_binding_name(prefix: str) -> str | None:
    """Referent of an anonymous function literal, from the text before ``func``.

    Returns the variable name for ``var x = func`` / ``x := func`` / ``x = func``
    and a ``.Field`` marker for a composite-literal field binding (``Field:
    func``); ``None`` when the literal is not bound to a named referent (an
    argument, a return value, ``go``/``defer``, or an immediately-invoked
    literal).
    """

    prefix = prefix.rstrip()
    match = _ANON_VAR_BIND_RE.search(prefix)
    if match:
        return match.group("name")
    match = _ANON_FIELD_BIND_RE.search(prefix)
    if match:
        return _FIELD_REF_PREFIX + match.group("name")
    return None


def _forward_slot(body: str, names: Sequence[str | None]) -> int | None:
    """Program-argument position when *body* forwards a parameter into a sink."""

    if "Command" not in body:
        return None
    for sink, argument_index in _FORWARD_SINKS:
        start = body.find(sink)
        while start >= 0:
            arguments = _call_arguments(body[start:], sink)
            if argument_index < len(arguments) and arguments[argument_index] in names:
                return argument_index
            start = body.find(sink, start + 1)
    return None


def _function_units(
    folded: str,
) -> list[tuple[tuple[str, ...], list[str | None], str]]:
    """Every function body with the referent identifiers that can reach it.

    A *unit* is ``(referents, parameter_names, body)``.  Named declarations carry
    their name; anonymous literals carry the variable or field they are bound to.
    Literals with no named referent (arguments, returned values, ``go``/``defer``,
    IIFE) carry no referent and are handled separately by
    :func:`_iife_call_hits`.
    """

    units: list[tuple[tuple[str, ...], list[str | None], str]] = []
    for signature in _GO_FUNC_SIG.finditer(folded):
        names = _go_param_names(signature.group("params"))
        body = _balanced_body(folded, signature.end() - 1)
        units.append(((signature.group("name"),), names, body))
    for literal in _GO_ANON_FUNC_SIG.finditer(folded):
        names = _go_param_names(literal.group("params"))
        body = _balanced_body(folded, literal.end() - 1)
        referent = _anon_binding_name(folded[: literal.start()])
        referents = (referent,) if referent else ()
        units.append((referents, names, body))
    return units


def _forwarder_functions(folded: str) -> dict[str, int]:
    """Map ``function-value referent -> program-argument position``.

    Covers named declarations, anonymous literals bound to a variable or struct
    field, and the bodies they share.  The key is any identifier whose call can
    reach a body that forwards a parameter into a command constructor's program
    slot; a key prefixed with ``.`` is a struct field, matched at
    ``recv.Field(...)`` call sites.
    """

    forwards: dict[str, int] = {}
    for referents, names, body in _function_units(folded):
        if not names or not referents:
            continue
        argument_index = _forward_slot(body, names)
        if argument_index is None:
            continue
        for referent in referents:
            forwards.setdefault(referent, argument_index)
    return forwards


def _forwarder_call_hits(
    folded: str,
    forwards: Mapping[str, int],
) -> list[tuple[str, int, str]]:
    """Call sites of a forwarder: ``(name, offset, program-argument-text)``."""

    found: list[tuple[str, int, str]] = []
    for name, argument_index in sorted(forwards.items()):
        if name.startswith(_FIELD_REF_PREFIX):
            pattern = rf"\.{re.escape(name[len(_FIELD_REF_PREFIX):])}\s*\("
        else:
            pattern = rf"(?<![\w.]){re.escape(name)}\s*\("
        for match in re.finditer(pattern, folded):
            paren = folded.find("(", match.start())
            if paren < 0:
                continue
            arguments = _call_arguments(folded[paren:], "(")
            if argument_index < len(arguments):
                found.append((name, match.start(), arguments[argument_index]))
    return found


def _iife_call_hits(folded: str) -> list[tuple[int, str]]:
    """Immediately-invoked forwarding literals: ``(offset, program-argument)``.

    ``func(name string, args ...string) error { return exec.Command(name,
    args...).Run() }("taws", "login")`` has no referent identifier at all; the
    source constant is in the invocation list directly after the body.
    """

    hits: list[tuple[int, str]] = []
    for literal in _GO_ANON_FUNC_SIG.finditer(folded):
        names = _go_param_names(literal.group("params"))
        if not names:
            continue
        body = _balanced_body(folded, literal.end() - 1)
        argument_index = _forward_slot(body, names)
        if argument_index is None:
            continue
        _, close = _balanced_body_span(folded, literal.end() - 1)
        if not folded[close + 1:].lstrip().startswith("("):
            continue
        paren = folded.index("(", close + 1)
        arguments = _call_arguments(folded[paren:], "(")
        if argument_index < len(arguments):
            hits.append((literal.start(), arguments[argument_index]))
    return hits


def _extend_forwarders(
    folded_texts: Sequence[str],
    forwards: dict[str, int],
) -> dict[str, int]:
    """Close the forwarder set transitively.

    ``func a(n string) error { return b(n) }`` is itself a forwarder when ``b``
    forwards its program argument, and an anonymous body bound to a variable is a
    forwarder on the same terms.  A function value aliased onto another name
    (``y := x``) forwards identically, so a two-hop (or N-hop) chain -- whether
    through declarations, literals or aliases -- does not reopen the delegation
    hole.
    """

    changed = True
    while changed:
        changed = False
        known = list(forwards.items())
        for folded in folded_texts:
            for referents, names, body in _function_units(folded):
                if not names or not referents:
                    continue
                if all(referent in forwards for referent in referents):
                    continue
                argument_index = _forward_slot(body, names)
                if argument_index is None:
                    for callee, index in known:
                        if index >= len(names):
                            continue
                        callee_name = callee.lstrip(_FIELD_REF_PREFIX)
                        for match in re.finditer(
                            rf"(?<![\w.])(?:\.)?{re.escape(callee_name)}\s*\(",
                            body,
                        ):
                            paren = body.find("(", match.start())
                            if paren < 0:
                                continue
                            arguments = _call_arguments(body[paren:], "(")
                            if index < len(arguments) and arguments[index] in names:
                                argument_index = index
                                break
                        if argument_index is not None:
                            break
                if argument_index is None:
                    continue
                for referent in referents:
                    if referent not in forwards:
                        forwards[referent] = argument_index
                        changed = True
            for alias in _FUNC_VALUE_ALIAS_RE.finditer(folded):
                lhs = alias.group("lhs")
                rhs = alias.group("rhs")
                if rhs in forwards and lhs not in forwards:
                    forwards[lhs] = forwards[rhs]
                    changed = True
    return forwards


def _is_test_artifact(relative: Path) -> bool:
    """Return True for a candidate's own test file or ``testdata/`` asset.

    Only names the toolchains themselves reserve for tests are exempted; a file
    that merely *mentions* testing (for example ``internal/build/run.go``) is
    still production code and is scanned.
    """

    if any(part.casefold() in _TEST_ARTIFACT_DIRS for part in relative.parts):
        return True
    text = relative.as_posix()
    suffix = relative.suffix.casefold()
    if suffix == ".go":
        return _GO_TEST_NAME.search(text) is not None
    if suffix == ".py":
        return _PY_TEST_NAME.search(text) is not None
    if suffix in {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}:
        return _JS_TEST_NAME.search(text) is not None
    if suffix == ".java":
        return _JAVA_TEST_NAME.search(text) is not None
    return False


def _candidate_files(root: Path) -> list[Path]:
    if not root.is_dir() or root.is_symlink():
        return []
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        if path.suffix.casefold() not in _SOURCE_SUFFIXES:
            continue
        relative = path.relative_to(root)
        if any(part in _IGNORED_PARTS for part in relative.parts):
            continue
        if _is_test_artifact(relative):
            continue
        files.append(path)
    return files


def detect_source_runtime_subprocess(
    root: Path,
    config: Mapping[str, Any],
    *,
    tokens: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    wanted = {str(item).casefold() for item in (tokens or source_runtime_tokens(config))}
    wanted.discard("")
    if not wanted:
        return []
    # Tokens whose spelling is shared with the target executable name (the
    # source project name is preserved by the migration) cannot be trusted from a
    # free-text window match; they must be the sink's derived program argument.
    ambiguous = ambiguous_program_tokens(config) & wanted
    strong = wanted - ambiguous
    # First pass: read every candidate file and summarise the cross-function
    # forwarders.  A wrapper in one file can be called from another, so the
    # summary is global to the candidate tree.
    resolved_files: list[tuple[Path, str, str, str]] = []
    forwards: dict[str, int] = {}
    for path in _candidate_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        clean = _strip_comments(text)
        folded = _resolve_constant_text(clean)
        resolved_files.append((path, text, clean, folded))
        value_aliases, _ = _go_exec_value_aliases(clean)
        for name, argument_index in value_aliases.items():
            forwards.setdefault(name, argument_index)
        for name, argument_index in _forwarder_functions(folded).items():
            forwards.setdefault(name, argument_index)
    if forwards:
        _extend_forwarders([item[3] for item in resolved_files], forwards)
    hits: list[dict[str, Any]] = []
    for path, text, clean, folded in resolved_files:
        lowered = folded.casefold()
        # Value references (reflection / aliases / arguments / slice elements) to
        # a process-image primitive or an ``os/exec`` constructor carry no
        # trailing call paren and are fail-closed regardless of any runtime
        # token, so ``run := exec.Command`` or ``reflect.ValueOf(exec.Command)``
        # cannot hide behind a dynamically resolved command name.
        _, traced_value_offsets = _go_exec_value_aliases(clean)
        for name, offset in _value_mentions(clean, _go_value_targets(clean)):
            if offset in traced_value_offsets:
                continue
            window = clean[offset:min(len(clean), offset + _WINDOW)]
            hits.append(
                SubprocessHit(
                    path=path.relative_to(root).as_posix(),
                    line=_line_of(text, offset),
                    sink=name,
                    token=name,
                    evidence=window[:200],
                ).as_dict()
            )
        # Cross-function delegation: a helper forwards a parameter into the
        # program slot; the source-runtime constant only appears at its call
        # site (possibly in another file).
        if forwards:
            for name, offset, argument in _forwarder_call_hits(folded, forwards):
                matched = _matched_token(argument, wanted)
                if matched is not None:
                    hits.append(
                        SubprocessHit(
                            path=path.relative_to(root).as_posix(),
                            line=_line_of(text, offset),
                            sink=f"{name}(",
                            token=matched,
                            evidence=folded[offset:offset + _WINDOW][:200],
                        ).as_dict()
                    )
        # Immediately-invoked forwarding literal: no referent identifier exists,
        # so the source constant is in the invocation list itself.
        for offset, argument in _iife_call_hits(folded):
            matched = _matched_token(argument, wanted)
            if matched is not None:
                hits.append(
                    SubprocessHit(
                        path=path.relative_to(root).as_posix(),
                        line=_line_of(text, offset),
                        sink="func(",
                        token=matched,
                        evidence=folded[offset:offset + _WINDOW][:200],
                    ).as_dict()
                )
        fail_closed = set(_FAIL_CLOSED_SINKS) | _go_fail_closed_calls(clean)
        ordered_sinks: list[str] = []
        seen_sinks: set[str] = set()
        for sink in (
            *_SINKS,
            *_go_gated_calls(clean),
            *sorted(fail_closed),
            *_alias_sinks(folded),
        ):
            if sink in seen_sinks:
                continue
            seen_sinks.add(sink)
            ordered_sinks.append(sink)
        for sink in ordered_sinks:
            start = lowered.find(sink.casefold())
            while start >= 0:
                end = min(len(folded), start + _WINDOW)
                window = folded[start:end]
                if sink in fail_closed:
                    # Low-level process-image APIs have no legitimate use in a
                    # migrated service: the call itself is the evidence.
                    matched: str | None = sink.rstrip("(")
                else:
                    collapsed = _collapsed(window)
                    joined = _joined_literals(window)
                    matched = next(
                        (
                            token for token in strong
                            if process_token_present(window, token)
                            or process_token_present(collapsed, token)
                            or process_token_present(joined, token)
                        ),
                        None,
                    )
                    if matched is None:
                        # The command name is attacker-controlled: catch
                        # separator/renamed spellings of the source program that
                        # the word-boundary match above cannot see.
                        matched = _argument_token_match(window, sink, strong)
                    if matched is None and ambiguous:
                        # An ambiguous program name (the source project name is
                        # also the target executable name) is never evidence as an
                        # incidental data word; it is evidence as the sink's
                        # derived program argument *or* as the program reached
                        # through a shell/launcher prefix it spawns.
                        matched = _program_argument_token_match(window, sink, ambiguous)
                        if matched is None:
                            matched = _ambiguous_command_match(window, sink, ambiguous)
                if matched is not None:
                    hits.append(
                        SubprocessHit(
                            path=path.relative_to(root).as_posix(),
                            line=_line_of(text, start),
                            sink=sink,
                            token=matched,
                            evidence=window[:200],
                        ).as_dict()
                    )
                    break
                start = lowered.find(sink.casefold(), start + 1)
    # De-duplicate by path/line/sink/token.
    seen: set[tuple[str, int, str, str]] = set()
    unique: list[dict[str, Any]] = []
    for hit in hits:
        key = (hit["path"], hit["line"], hit["sink"], hit["token"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(hit)
    return unique


__all__ = ["detect_source_runtime_subprocess", "source_runtime_tokens"]
