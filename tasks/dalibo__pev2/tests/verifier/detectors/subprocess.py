"""Detect process sinks that invoke the source language runtime.

A hit is reported only when a process sink and a source-runtime token are both
present.  The detector still handles the common obfuscations that plain literal
rules miss:

* module aliases (``use std::process::Command as C``, ``use std::{process as P}``,
  ``use std::process as P``);
* qualified ``::`` / ``.`` separators padded with whitespace
  (``P :: Command :: new``);
* executable names assembled at run time from byte/char arrays
  (``[0x6e, 0x6f, 0x64, 0x65]``) or concatenated string literals;
* executable names written *element by element* into a container
  (``name.push(0x6e); name.push(0x6f); ...`` / ``buf.extend([...])`` /
  ``name += "\x6e"``), which a bounded array-only decoder would miss.

The association is deliberately not limited to a tiny window: a file that both
launches a process and carries a source-runtime command identity is evidence of
a source fallback even when the attacker separates the two across the file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .runtime_identity import (
    process_runtime_tokens,
    process_token_present,
    token_present,
)


_SOURCE_SUFFIXES = frozenset(
    {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".py", ".go", ".rs", ".java"}
)
_IGNORED_PARTS = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "target",
        "dist",
        "build",
        "vendor",
        ".gomodcache",
        ".gocache",
        "__pycache__",
        # The verifier stages the offline registry at ``<repo>/cargo-home`` and
        # excludes it from the candidate diff (``.git/info/exclude``); it is
        # environment-provided build input, not candidate-authored source, so it
        # must stay out of the source-runtime fallback scans.
        "cargo-home",
        ".cargo",
    }
)
# Test-only artifacts are not production code.  The deceptive-fallback ban is
# scoped to the migrated *production* implementation, so a candidate's own tests
# must not be read as delegation: a Rust integration test under ``tests/`` is a
# separate test crate that ``cargo build`` never links into the library, and a
# JS/TS spec or ``testdata/`` asset is likewise test scaffolding.  Scanning them
# only produced false positives -- for example a candidate-authored differential
# suite (``crates/*/tests/differential.mjs``) that transpiles the original
# TypeScript sources and compares them against the migrated Rust binary: it
# imports ``node:`` builtins and ``child_process`` to drive ``cargo`` but is not
# part of the shipped implementation.
#
# Each suffix keeps its own toolchain's convention, so a production file whose
# name merely *contains* "test" is still scanned.
_GO_TEST_NAME = re.compile(r"(?:^|/)[^/]*_test\.go$", re.IGNORECASE)
_PY_TEST_NAME = re.compile(
    r"(?:^|/)(?:test_[^/]*\.py|[^/]*_test\.py|conftest\.py)$", re.IGNORECASE
)
_JS_TEST_NAME = re.compile(
    r"(?:^|/)[^/]*\.(?:test|spec)\.[cm]?[jt]sx?$", re.IGNORECASE
)
_JAVA_TEST_NAME = re.compile(r"(?:^|/)[^/]*(?:Test|Tests|Spec)\.java$", re.IGNORECASE)
_TEST_ARTIFACT_DIRS = frozenset({"testdata", "__tests__", "tests"})


def _is_test_artifact(relative: Path) -> bool:
    """True for a candidate's own test file or test-only asset directory."""

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
    # libc process primitives.  A C `system`/`popen`/`exec*` call that runs a
    # source-runtime command is the same delegation as an interpreter-bound
    # sink; leaving them out let a "fake migration" that shells out to `node`
    # through `system()` pass the hard gate.
    "system(",
    "popen(",
    "execve(",
    "execvp(",
    "execv(",
    "execlp(",
    "execl(",
    "posix_spawn(",
    "posix_spawnp(",
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
_WINDOW = 240
_ARRAY = re.compile(r"\[([^\[\]]*)\]")
_HEX = re.compile(r"0x[0-9a-fA-F]+")
_DECIMAL = re.compile(r"\d+")
_CHAR = re.compile(r"'(.)'")

# ---------------------------------------------------------------------------
# Reconstructing a runtime name that is *written element by element* into a
# container (``name.push(0x6c); name.push(0x69); ...``) instead of being spelled
# out as a literal / bracketed array.  This is the same obfuscation as
# ``[0x6c, 0x69, ...]``: a bounded array-only decoder is trivially bypassed by
# any of the equivalent incremental forms (``push``/``append``/``extend``/
# ``write``/index assignment/``+=``/``chr()`` wrappers).  The decoder below
# rebuilds, per receiving container, the sequence of literal elements written to
# it (in source order) and returns the resulting candidates, so the association
# logic does not depend on a fixed list of spellings.
# ---------------------------------------------------------------------------
_ELEMENT_METHODS = (
    "push_back",
    "push_str",
    "extend_from_slice",
    "WriteString",
    "WriteByte",
    "WriteRune",
    "write_all",
    "write_str",
    "push",
    "append",
    "extend",
    "write",
    "insert",
    "concat",
    "add",
)
_RECEIVER = r"[A-Za-z_$][A-Za-z0-9_$]*(?:\s*(?:\.|::)\s*[A-Za-z_$][A-Za-z0-9_$]*)*"
_ELEMENT_WRITE = re.compile(
    r"(?P<recv>" + _RECEIVER + r")\s*(?:\.|::)\s*(?P<method>(?:"
    + "|".join(_ELEMENT_METHODS)
    + r"))\s*\((?P<arg>[^()]*(?:\([^()]*\)[^()]*)*)\)"
)
_INDEX_WRITE = re.compile(
    r"(?P<recv>" + _RECEIVER + r")\s*\[\s*[^\]]*\]\s*=\s*(?P<arg>[^;{}\n]*)"
)
_APPEND_ASSIGN = re.compile(r"(?P<recv>" + _RECEIVER + r")\s*\+=\s*(?P<arg>[^;{}\n]*)")

# ``\"lib\x6eode.so\"`` / ``'\u{6e}'`` / C-style escapes.  Group 5 captures a
# single-character escape (``\n``/``\t``/``\\``...), group 4 an octal escape.
_STRING_ESCAPE = re.compile(
    r"\\(?:x([0-9a-fA-F]{2})|u\{([0-9a-fA-F]{1,6})\}|u([0-9a-fA-F]{4})"
    r"|([0-7]{1,3})|(.))",
    re.S,
)
_SIMPLE_ESCAPES = {
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "0": "\0",
    "a": "\a",
    "b": "\b",
    "f": "\f",
    "v": "\v",
    "\\": "\\",
    "'": "'",
    '"': '"',
    "?": "?",
}
_INT_LITERAL = re.compile(r"^[-+]?(?:0[xX][0-9a-fA-F]+|0[oO][0-7]+|0[bB][01]+|\d+)$")
_NUMERIC_SUFFIX = re.compile(r"_?(?:u|i|f)(?:8|16|32|64|128|size)\b")
_NUMERIC_CAST = re.compile(
    r"\s+as\s+[A-Za-z_][A-Za-z0-9_:]*(?:<[^<>]*>)?(?![A-Za-z0-9_:])"
)
_CHR_WRAPPER = re.compile(
    r"^(?:chr|char::from_u32|from_u32|String::from_char|char_from_u32)"
    r"\s*\(\s*(?P<inner>[^()]*)\s*\)$"
)


def _unescape_string(body: str) -> str:
    """Decode ``\\xHH`` / ``\\uHHHH`` / ``\\u{...}`` / octal / C escapes."""

    def _replace(match: re.Match[str]) -> str:
        if match.group(4) is not None:  # octal
            try:
                code = int(match.group(4), 8)
            except ValueError:
                return match.group(0)
            return chr(code) if 0 <= code <= 0x10FFFF else match.group(0)
        if match.group(5) is not None:  # single-character escape
            return _SIMPLE_ESCAPES.get(match.group(5), match.group(5))
        digits = match.group(1) or match.group(2) or match.group(3)
        try:
            code = int(digits, 16)
        except (TypeError, ValueError):
            return match.group(0)
        return chr(code) if 0 <= code <= 0x10FFFF else match.group(0)

    return _STRING_ESCAPE.sub(_replace, body)


def _string_literal_fragment(text: str) -> str | None:
    """Decode a string/byte/raw/char literal, or return ``None``.

    ``"lib"``, ``b"lib"``, ``r"lib"``, ``'l'`` and ``b'\\x6c'`` all decode to
    the characters they contribute; an identifier or expression that merely
    starts with an alphabetic prefix (``buffer.clone()``) is rejected.
    """

    observed = 0
    raw = False
    for character in text[:2]:
        if character in ("r", "R"):
            raw = True
        elif character not in ("b", "B"):
            break
        observed += 1
    rest = text[observed:]
    if len(rest) < 2 or rest[0] not in "\"'" or rest[-1] != rest[0]:
        return None
    body = rest[1:-1]
    return body if raw else _unescape_string(body)


def _literal_fragment(expr: str) -> str | None:
    """Return the character(s) a single element expression writes, or ``None``.

    Handles string/byte/raw literals, char literals, integer code points (with
    optional ``as u8`` casts / ``_u8`` suffixes) and a bracketed byte/char array
    argument (``extend(&[...])`` / ``vec![...]`` / ``write_all(&[...])``).
    """

    text = expr.strip()
    if not text:
        return None
    # Drop a leading borrow / ``mut`` / opening deref.
    while text[:1] in ("&", "*"):
        text = text[1:].lstrip()
    if text.startswith("mut "):
        text = text[4:].lstrip()
    # Strip a fully-wrapping pair of parentheses (``(0x6c)`` / ``((0x6c))``)
    # without touching a call such as ``chr(0x6c)``.
    while (
        len(text) >= 2
        and text[0] == "("
        and text[-1] == ")"
        and text[1:-1].count("(") == text[1:-1].count(")")
    ):
        text = text[1:-1].strip()
    if not text:
        return None
    # String / byte / raw / char literal.
    literal = _string_literal_fragment(text)
    if literal is not None:
        return literal
    # A bracketed array argument: decode its numeric/char elements.
    if "[" in text and "]" in text:
        decoded = _decode_runtime_arrays(text)
        return "".join(decoded) if decoded else None
    # ``chr(0x6e)`` / ``char::from_u32(110)`` style wrapper.
    wrapper = _CHR_WRAPPER.match(text)
    if wrapper:
        return _literal_fragment(wrapper.group("inner"))
    # Integer code point, tolerating ``as u8`` casts and ``_u8`` suffixes.
    text = _NUMERIC_CAST.sub("", text)
    text = _NUMERIC_SUFFIX.sub("", text).strip()
    if not text:
        return None
    if _INT_LITERAL.match(text):
        try:
            value = int(text, 0)
        except ValueError:
            try:
                value = int(text, 10)
            except ValueError:
                return None
        return chr(value) if 0 <= value <= 0x10FFFF else None
    return None


def _normalised_receiver(receiver: str) -> str:
    return re.sub(r"\s+", "", receiver).casefold()


def _decode_element_sequences(text: str) -> list[str]:
    """Rebuild strings written element-by-element into a container.

    Every literal element written to a container (via ``push``/``append``/
    ``extend``/``write``/index assignment/``+=``) is decoded and the fragments
    for the same receiver are concatenated in source order.  The result is a set
    of candidate runtime names that no longer depends on the concrete spelling
    (array vs. one call per element vs. string fragments).
    """

    events: list[tuple[int, str, str]] = []
    for pattern in (_ELEMENT_WRITE, _INDEX_WRITE, _APPEND_ASSIGN):
        for match in pattern.finditer(text):
            fragment = _literal_fragment(match.group("arg"))
            if fragment is None:
                continue
            receiver = _normalised_receiver(match.group("recv"))
            if not receiver:
                continue
            events.append((match.start(), receiver, fragment))
    events.sort(key=lambda item: item[0])
    containers: dict[str, list[str]] = {}
    order: list[str] = []
    for _offset, receiver, fragment in events:
        if receiver not in containers:
            containers[receiver] = []
            order.append(receiver)
        containers[receiver].append(fragment)
    assembled: list[str] = []
    for receiver in order:
        candidate = "".join(containers[receiver])
        if len(candidate) >= 2:
            assembled.append(candidate)
    return assembled


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


_STRING_LITERAL_TOKEN = re.compile(
    r"""(?:[bBrR])?(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')"""
)


def _decoded_joined_literals(text: str) -> str:
    """Concatenate string literals *after decoding escapes*.

    The escape spelling of a command name (``"\\x6e\\x6f\\x64\\x65"``) is the
    string-literal equivalent of a numeric/char array and is decoded by the FFI
    detector and the F2P probe; align the subprocess binding decoder so a value
    rebound through a constant cannot slip past it.
    """

    fragments: list[str] = []
    for match in _STRING_LITERAL_TOKEN.finditer(text):
        fragment = _string_literal_fragment(match.group(0))
        if fragment:
            fragments.append(fragment)
    return "".join(fragments).casefold()


def _compact(text: str) -> str:
    """Drop horizontal whitespace while preserving newlines for line numbers."""

    return re.sub(r"[^\S\n]+", "", text)


def _decode_runtime_arrays(text: str) -> list[str]:
    """Decode numeric/char *array literals* that assemble a runtime name.

    ``[0x6e, 0x6f, 0x64, 0x65]`` and ``['n', 'o', 'd', 'e']`` both yield
    ``node``; ``let code_points: [u8; 4] = [...]`` keeps its type annotation
    (``u8; 4`` is not a numeric array and is ignored).
    """

    decoded: list[str] = []
    for match in _ARRAY.finditer(text):
        body = match.group(1).strip()
        if not body:
            continue
        characters: list[str] = []
        valid = True
        for item in body.split(","):
            item = item.strip()
            # A blank element is a trailing comma / blank line (standard
            # ``rustfmt`` formatting), not a malformed entry: skip it so the
            # array is not discarded wholesale.
            if not item:
                continue
            # Decode the element with the same vocabulary as an incremental
            # element write, so byte/char/string literals, ``0x6c as u8`` casts
            # and ``_u8``/``u8`` suffixes are all recovered here too.
            fragment = _literal_fragment(item)
            if fragment is None:
                valid = False
                break
            characters.append(fragment)
        if valid and characters:
            decoded.append("".join(characters))
    return decoded


def _decode_runtime_literals(text: str) -> list[str]:
    """Decode every run-time assembly of a runtime name in ``text``.

    Combines the array-literal decoder with the incremental element-write
    decoder, so both ``[0x6e, 0x6f, 0x64, 0x65]`` and a sequence of
    ``name.push(0x6e)`` calls are recovered.
    """

    return [*_decode_runtime_arrays(text), *_decode_element_sequences(text)]


def _alias_sinks(text: str) -> list[str]:
    aliases: list[str] = []
    for match in re.finditer(
        r"use\s+std::process::Command\s+as\s+([A-Za-z_][A-Za-z0-9_]*)", text
    ):
        aliases.append(f"{match.group(1)}::new(")
    # ``use std::process as P`` and the grouped ``use std::{process as P}`` form
    # both rename the module, after which ``P::Command`` is the same sink.
    for match in re.finditer(
        r"use\s+std::process\s+as\s+([A-Za-z_][A-Za-z0-9_]*)", text
    ):
        aliases.append(f"{match.group(1)}::Command")
    for match in re.finditer(r"use\s+std::\{([^}]*)\}", text):
        for member in match.group(1).split(","):
            member = member.strip()
            module = re.match(r"process\s+as\s+([A-Za-z_][A-Za-z0-9_]*)", member)
            if module:
                aliases.append(f"{module.group(1)}::Command")
            command = re.match(
                r"process::Command\s+as\s+([A-Za-z_][A-Za-z0-9_]*)", member
            )
            if command:
                aliases.append(f"{command.group(1)}::new(")
    # Nested grouping ``use std::process::{Command as C}`` renames the item
    # without an intervening ``std::{...}`` list, so the loops above do not see
    # it; ``C::new(...)`` is the same process sink as ``Command::new(...)``.
    for match in re.finditer(r"use\s+std::process::\{([^}]*)\}", text):
        for member in match.group(1).split(","):
            member = member.strip()
            command = re.match(r"Command\s+as\s+([A-Za-z_][A-Za-z0-9_]*)", member)
            if command:
                aliases.append(f"{command.group(1)}::new(")
            module = re.match(r"process\s+as\s+([A-Za-z_][A-Za-z0-9_]*)", member)
            if module:
                aliases.append(f"{module.group(1)}::Command")
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


def _command_token_present(text: str, token: str) -> bool:
    """A runtime token used as a command name, not a word in a phrase.

    Used for the whole-file association so a benign ``"Node Type"`` string in a
    file that happens to launch some other process is not mistaken for a
    ``node`` fallback.
    """

    needle = str(token).casefold()
    if not needle:
        return False
    for match in re.finditer(
        rf"(?<![A-Za-z0-9_]){re.escape(needle)}(?![A-Za-z0-9_])", text.casefold()
    ):
        if re.match(r"\s+[A-Za-z]", text[match.end():]):
            continue
        return True
    return False


def _window_token(window: str, wanted: Sequence[str]) -> str | None:
    collapsed = _collapsed(window)
    joined = _joined_literals(window)
    decoded = " ".join(_decode_runtime_literals(window))
    for token in wanted:
        if (
            process_token_present(window, token)
            or process_token_present(collapsed, token)
            or process_token_present(joined, token)
            or process_token_present(decoded, token)
        ):
            return token
    return None


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _candidate_files(root: Path, config: Mapping[str, Any] | None = None) -> list[Path]:
    if not root.is_dir() or root.is_symlink():
        return []
    # This task bans delegation by the native production implementation.
    # Standalone JS differential/benchmark drivers are not Rust entry points.
    # Scan every Rust source across the workspace (including helper crates),
    # rather than treating a development driver's subprocess as library code.
    # A Rust call into such a driver still has a Rust process/FFI/loading sink.
    technology = (config or {}).get("target_technology", {})
    language = technology.get("language", {}) if isinstance(technology, Mapping) else {}
    target_name = technology.get("language_name", "") if isinstance(technology, Mapping) else ""
    if not target_name and isinstance(language, Mapping):
        target_name = language.get("name", "")
    native_rust = str(target_name).casefold() == "rust"
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        if path.suffix.casefold() not in _SOURCE_SUFFIXES:
            continue
        if native_rust and path.suffix.casefold() != ".rs":
            continue
        relative = path.relative_to(root)
        if any(part in _IGNORED_PARTS for part in relative.parts):
            continue
        if _is_test_artifact(relative):
            continue
        files.append(path)
    return files


# ---------------------------------------------------------------------------
# Cross-file / cross-crate association of a process sink with the
# source-runtime command it launches.  The command name may live in a different
# file (or a different crate) from the sink and be reached through a
# constant/static/let binding:
#
#     // crates/qa-legacy/src/name.rs
#     pub const QA_NODE_PROGRAM: &str = "node";
#     // crates/qa-legacy/src/lib.rs
#     std::process::Command::new(name::QA_NODE_PROGRAM);
#
# A per-file window cannot see that, so the detector propagates the literal
# bindings across the whole candidate tree and resolves the *command argument*
# of every process sink against them (source -> sink).  Only identifiers bound
# to a source-runtime command token are accepted, so an ordinary
# ``Command::new("cargo")`` is never mistaken for a fallback.
# ---------------------------------------------------------------------------
# A Rust type annotation may itself contain ``;``/`,`/``+``/``<``/``>`` inside a
# bracketed segment (``[&str; 2]``, ``HashMap<K, Vec<V>>``, ``[u8; 4]``).  The
# annotation group therefore consumes balanced ``[...]`` chunks as a unit so an
# array/generic-typed binding is still captured instead of being skipped at the
# inner ``;`` (which defeated ``let names: [&str; 2] = ["no", "de"];``).
_TYPE_ANNOTATION = r"(?::\s*(?:\[[^\[\]]*\]|[^=;{}])*)?"
_BINDING_PATTERNS = (
    # Rust ``const`` / ``static`` (optional type annotation, trailing ``;``).
    re.compile(
        r"(?:^|[;{}\s])(?:pub\s+)?(?:const|static)\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
        r"\s*" + _TYPE_ANNOTATION + r"=\s*(?P<value>[^;{}]*)[;}]",
        re.M,
    ),
    # Rust ``let`` (optional type annotation).
    re.compile(
        r"(?:^|[;{}\s])let\s+(?:mut\s+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
        r"\s*" + _TYPE_ANNOTATION + r"=\s*(?P<value>[^;{}]*)[;}]",
        re.M,
    ),
    # JS/TS ``const`` / ``let`` / ``var``.
    re.compile(
        r"(?:^|[;{}\s])(?:const|let|var)\s+(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)"
        r"\s*=\s*(?P<value>[^;\n]*)",
        re.M,
    ),
    # Go short variable declaration ``name := <expr>``.
    re.compile(
        r"(?:^|[;{}\s])(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*:=\s*(?P<value>[^\n;]*)",
        re.M,
    ),
)
_BINDING_ALIAS = re.compile(
    r"use\s+[A-Za-z0-9_:]*?(?P<orig>[A-Za-z_][A-Za-z0-9_]*)"
    r"\s+as\s+(?P<alias>[A-Za-z_][A-Za-z0-9_]*)"
)
_IDENTIFIER_CHAIN = re.compile(
    r"[A-Za-z_$][A-Za-z0-9_$]*(?:\s*(?:\.|::)\s*[A-Za-z_$][A-Za-z0-9_$]*)*"
)


def _binding_value_token(value: str, ordered: Sequence[str]) -> str | None:
    """Return a source-runtime token that ``value`` can assemble, else ``None``."""

    representations = [
        value,
        _collapsed(value),
        _joined_literals(value),
        _decoded_joined_literals(value),
        *_decode_runtime_literals(value),
    ]
    for token in ordered:
        if any(_command_token_present(rep, token) for rep in representations):
            return token
    return None


# ---------------------------------------------------------------------------
# Indirect (*source -> sink*) command-name resolution.
#
# The command a process sink launches need not be a literal, a ``const`` and it
# need not even be an identifier bound to a literal: the equivalent family also
# contains
#
#     fn runtime_program() -> String { "no".to_string() + "de" }   // return value
#     struct Config { program: String }                            // field passing
#     Command::new(helper())                                       // wrapper method
#     fn a() -> String { b() }                                     // transitivity
#
# so every *candidate-defined* symbol (binding, struct/object field, function or
# method return) is classified as ``runtime`` / ``safe`` / ``unknown`` and the
# sink's command argument is resolved against that table.  An argument that
# reaches a candidate-defined symbol whose value cannot be shown to be a benign
# literal is *fail-closed*: the mechanism reports the risk (L2) instead of
# assuming innocence, which is the opposite of a spellings whitelist.
# ---------------------------------------------------------------------------
_RUNTIME = "runtime"
_SAFE = "safe"
_UNKNOWN = "unknown"

# Identifier-free expressions built only from literals and pure string/number
# constructors.  ``"cargo"``, ``&"cargo"``, ``String::from("cargo")`` and
# ``"car" . to_string() + "go"`` are safe; ``env!("CARGO")``, ``prog()`` and
# ``format!("{}", value)`` are not (they reach an unresolved symbol).
_SAFE_IDENTIFIERS = frozenset(
    {
        "string", "str", "bytes", "vec", "char", "u8", "u32", "i32", "usize",
        "from", "new", "with_capacity", "to_string", "to_owned", "into", "as_str",
        "as_ref", "as_slice", "as_bytes", "to_vec", "concat", "format", "push_str",
        "to_ascii_lowercase", "to_lowercase", "trim", "clone", "owned", "box",
        "unwrap", "expect", "chars", "collect", "iter", "map", "chr", "from_u32",
        "from_char", "from_utf8", "to_string_lossy", "vec_macro",
    }
)
_NUMBER_LITERAL = re.compile(
    r"(?<![A-Za-z0-9_])(?:0[xX][0-9a-fA-F]+|0[oO][0-7]+|0[bB][01]+|\d+(?:\.\d+)?)"
    r"(?:_?(?:u|i|f)(?:8|16|32|64|128|size))?"
)


def _literal_only(expression: str) -> bool:
    """True when ``expression`` mentions no identifier outside the safe set."""

    text = _STRING_LITERAL_TOKEN.sub(" ", expression)
    text = _NUMBER_LITERAL.sub(" ", text)
    for word in re.findall(r"[A-Za-z_$][A-Za-z0-9_$]*", text):
        if word.casefold() not in _SAFE_IDENTIFIERS:
            return False
    return True


def _assembled_token(
    expression: str, ordered: Sequence[str], *, allow_raw: bool = True
) -> str | None:
    """Return a source-runtime token the ``expression`` can assemble.

    ``allow_raw`` is enabled for a *value* bound to a name or passed as an
    argument (where the bare token is meaningful), and disabled for a whole
    function body, where an ordinary local called ``node`` must not be mistaken
    for an executable name.
    """

    representations = [] if not allow_raw else [expression, _collapsed(expression)]
    representations += [
        _joined_literals(expression),
        _decoded_joined_literals(expression),
        *_decode_runtime_literals(expression),
    ]
    for token in ordered:
        if any(_command_token_present(rep, token) for rep in representations):
            return token
    return None


def _classify(
    expression: str, ordered: Sequence[str], *, allow_raw: bool = True
) -> tuple[str, str | None]:
    if not expression.strip():
        return (_UNKNOWN, None)
    token = _assembled_token(expression, ordered, allow_raw=allow_raw)
    if token is not None:
        return (_RUNTIME, token)
    if _literal_only(expression):
        return (_SAFE, None)
    return (_UNKNOWN, None)


def _matching_brace(text: str, open_index: int) -> int:
    """Index of the ``}`` matching the ``{`` at ``open_index`` (or ``-1``)."""

    depth = 0
    index = open_index
    length = len(text)
    quote = ""
    while index < length:
        character = text[index]
        if quote:
            if character == "\\":
                index += 2
                continue
            if character == quote:
                quote = ""
            index += 1
            continue
        if character in ("'", '"', "`"):
            quote = character
            index += 1
            continue
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return -1


_FUNCTION_HEADS = (
    # Rust ``fn`` (visibility / constness / async / unsafe / abi modifiers).
    re.compile(
        r"(?:^|[\s;{}()])(?:pub\s+)?(?:const\s+)?(?:async\s+)?(?:unsafe\s+)?"
        r"(?:extern\s+\"[^\"]*\"\s+)?fn\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
        r"\s*(?:<[^{}();]*>)?\s*\([^()]*\)\s*(?:->\s*[^{};]*?)?\{",
        re.M,
    ),
    # JS/TS ``function name(...) {``.
    re.compile(
        r"(?:^|[\s;{}()])(?:export\s+)?(?:async\s+)?function\s+"
        r"(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)\s*\([^()]*\)\s*\{",
        re.M,
    ),
    # JS/TS arrow function assigned to a name.
    re.compile(
        r"(?:^|[\s;{}()])(?:const|let|var)\s+(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)"
        r"\s*=\s*(?:async\s*)?(?:\([^()]*\)|[A-Za-z_$][A-Za-z0-9_$]*)\s*=>\s*\{",
        re.M,
    ),
    # Go ``func name(...) ... {`` (methods keep the method name).
    re.compile(
        r"(?:^|[\s;{}()])func\s+(?:\([^()]*\)\s*)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
        r"\s*\([^()]*\)\s*(?:\([^()]*\)|[^{};]*?)\s*\{",
        re.M,
    ),
)
_FIELD_PATTERN = re.compile(
    r"(?:^|[;{}\s,])(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)\s*:\s*"
    r"(?P<value>[^,;{}\n]*(?:\([^()]*\)[^,;{}\n]*)*)",
    re.M,
)


def _function_bodies(clean: str) -> list[tuple[str, str]]:
    """Return ``(name, body)`` for every function/method defined in ``clean``."""

    found: list[tuple[str, str]] = []
    seen: set[tuple[str, int]] = set()
    for pattern in _FUNCTION_HEADS:
        for match in pattern.finditer(clean):
            open_index = match.end() - 1
            close = _matching_brace(clean, open_index)
            if close < 0:
                continue
            key = (match.group("name"), open_index)
            if key in seen:
                continue
            seen.add(key)
            found.append((match.group("name"), clean[open_index + 1 : close]))
    return found


def _container_tokens(clean: str, ordered: Sequence[str]) -> dict[str, str]:
    """Map a container variable rebuilt element-by-element to its token.

    ``let mut name = String::new(); name.push(0x6e); … name`` must not be
    classified as *safe* merely because ``String::new()`` is literal-free.
    """

    events: dict[str, list[tuple[int, str]]] = {}
    for pattern in (_ELEMENT_WRITE, _INDEX_WRITE, _APPEND_ASSIGN):
        for match in pattern.finditer(clean):
            fragment = _literal_fragment(match.group("arg"))
            if fragment is None:
                continue
            receiver = _normalised_receiver(match.group("recv"))
            if receiver:
                events.setdefault(receiver, []).append((match.start(), fragment))
    resolved: dict[str, str] = {}
    for receiver, items in events.items():
        candidate = "".join(fragment for _offset, fragment in sorted(items))
        if len(candidate) < 2:
            continue
        for token in ordered:
            if _command_token_present(candidate, token):
                resolved[receiver] = token
                break
    return resolved


def _resolve_identifier(argument: str, definitions: Mapping[str, tuple[str, str | None]]) -> list[tuple[str, str | None, str]]:
    """Resolve every identifier chain in ``argument`` against ``definitions``."""

    resolved: list[tuple[str, str | None, str]] = []
    for match in _IDENTIFIER_CHAIN.finditer(argument):
        tail = re.split(r"\s*(?:\.|::)\s*", match.group(0))[-1]
        entry = definitions.get(tail)
        if entry is not None:
            resolved.append((entry[0], entry[1], tail))
    return resolved


def _argument_resolution(
    argument: str,
    ordered: Sequence[str],
    definitions: Mapping[str, tuple[str, str | None]],
) -> tuple[str, str | None, str]:
    """Classify a sink's command argument: ``runtime`` / ``safe`` / ``unknown``."""

    if not argument.strip():
        return (_UNKNOWN, None, "empty")
    token = _assembled_token(argument, ordered)
    if token is not None:
        return (_RUNTIME, token, "argument")
    resolved = _resolve_identifier(argument, definitions)
    for kind, child, name in resolved:
        if kind == _RUNTIME:
            return (_RUNTIME, child, f"binding:{name}")
    if resolved:
        unknown = next((name for kind, _c, name in resolved if kind == _UNKNOWN), None)
        if unknown is not None:
            return (_UNKNOWN, None, f"unresolved:{unknown}")
        return (_SAFE, None, "safe-binding")
    if _literal_only(argument):
        return (_SAFE, None, "literal")
    return (_UNKNOWN, None, "fresh-symbol")


def _collect_definitions(
    sources: Sequence[str], ordered: Sequence[str]
) -> dict[str, tuple[str, str | None]]:
    """Classify every candidate-defined symbol that may name a command."""

    definitions: dict[str, tuple[str, str | None]] = {}
    pending: list[tuple[str, str]] = []

    def record(name: str, kind: str, token: str | None, expression: str) -> None:
        # Keep the strongest evidence seen for a name so a later, more specific
        # writing is not lost to an earlier type declaration: ``program: String``
        # (a *safe* struct field type) must not mask the initialiser
        # ``program: name::PROG.to_string()`` (an *unknown* value that forwards a
        # runtime constant).  Rank: runtime > unknown > safe; a runtime verdict is
        # never downgraded.
        rank = {_SAFE: 0, _UNKNOWN: 1, _RUNTIME: 2}
        current = definitions.get(name)
        if current is not None and rank.get(current[0], 1) >= rank.get(kind, 1):
            return
        definitions[name] = (kind, token)
        if kind == _UNKNOWN:
            pending.append((name, expression))

    for clean in sources:
        for pattern in _BINDING_PATTERNS:
            for match in pattern.finditer(clean):
                record(
                    match.group("name"),
                    *_classify(match.group("value"), ordered),
                    match.group("value"),
                )
        for match in _FIELD_PATTERN.finditer(clean):
            record(
                match.group("name"),
                *_classify(match.group("value"), ordered, allow_raw=False),
                match.group("value"),
            )
        for name, body in _function_bodies(clean):
            record(name, *_classify(body, ordered, allow_raw=False), body)
        # A container rebuilt element-by-element overrides the ``safe`` verdict
        # of its ``String::new()`` initialiser.
        for receiver, token in _container_tokens(clean, ordered).items():
            definitions[receiver] = (_RUNTIME, token)
    # ``use name::NODE as P`` aliases an existing definition, and a definition
    # that merely forwards another one inherits its verdict (fixpoint).
    aliases: list[tuple[str, str]] = []
    for clean in sources:
        for match in _BINDING_ALIAS.finditer(clean):
            aliases.append((match.group("orig"), match.group("alias")))
    for _ in range(4):
        progressed = False
        for original, alias in aliases:
            entry = definitions.get(original)
            if entry is not None and alias not in definitions:
                definitions[alias] = entry
                progressed = True
        for name, expression in pending:
            if definitions.get(name, (_UNKNOWN, None))[0] != _UNKNOWN:
                continue
            forwarded = _resolve_identifier(expression, definitions)
            if forwarded and forwarded[0][0] != _UNKNOWN:
                definitions[name] = (forwarded[0][0], forwarded[0][1])
                progressed = True
        if not progressed:
            break
    return definitions


def _sink_argument(text: str, start: int) -> str:
    """Return the text of the first call argument at or after ``start``."""

    open_index = text.find("(", start)
    if open_index < 0:
        return ""
    depth = 0
    limit = min(len(text), open_index + 600)
    for index in range(open_index, limit):
        character = text[index]
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return text[open_index + 1:index]
    return text[open_index + 1:limit]


def _argument_token(
    argument: str,
    ordered: Sequence[str],
    bindings: Mapping[str, str],
) -> str | None:
    """Resolve a sink command argument to a runtime token via tree bindings."""

    if not argument:
        return None
    for match in _IDENTIFIER_CHAIN.finditer(argument):
        tail = re.split(r"\s*(?:\.|::)\s*", match.group(0))[-1]
        token = bindings.get(tail)
        if token is not None:
            return token
    return None


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
    ordered = sorted(wanted)
    scanned: list[tuple[Path, str, str, str]] = []
    for path in _candidate_files(root, config):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        clean = _strip_comments(text)
        compact = _compact(clean)
        if not compact:
            continue
        scanned.append((path, text, clean, compact))
    # Tree-level runtime identity: a source-runtime command spelled *anywhere* in
    # the candidate tree (as a literal, concatenation, decoded array or decoded
    # escape).  Mirrors ``ffi.py``'s ``tree_signals``/``cross_file_token`` so the
    # two hidden-fallback families share one mechanism instead of the subprocess
    # side relying on the sink file alone.
    tree_tokens: list[str] = []
    for _path, _text, clean, compact in scanned:
        for token in ordered:
            if token in tree_tokens:
                continue
            if any(
                _command_token_present(representation, token)
                for representation in (
                    compact,
                    _joined_literals(clean),
                    *_decode_runtime_literals(clean),
                )
            ):
                tree_tokens.append(token)
    # Tree-level classification of every candidate-defined symbol that may name
    # a command (bindings, fields, function/method returns, transitive forward).
    # ``use ...::NAME as ALIAS`` aliases and forwarding definitions are resolved
    # to a fixpoint inside ``_collect_definitions`` (bounded to avoid any
    # pathological input).
    definitions = _collect_definitions(
        [clean for _path, _text, clean, _comp in scanned], ordered
    )
    bindings = {
        name: token
        for name, (kind, token) in definitions.items()
        if kind == _RUNTIME and token is not None
    }
    hits: list[dict[str, Any]] = []
    for path, _text, clean, compact in scanned:
        lowered = compact.casefold()
        # Whole-file runtime identity: a literal, concatenation or decoded
        # numeric/char array anywhere in a file that also launches a process is
        # a fallback, even when the attacker splits name and sink.
        file_tokens = [
            token
            for token in ordered
            if any(
                _command_token_present(representation, token)
                for representation in (
                    compact,
                    _joined_literals(clean),
                    *_decode_runtime_literals(clean),
                )
            )
        ]
        for sink in (*_SINKS, *_alias_sinks(clean)):
            sink_key = _compact(sink).casefold()
            if not sink_key:
                continue
            start = lowered.find(sink_key)
            while start >= 0:
                window = compact[start:start + _WINDOW]
                matched = _window_token(window, ordered)
                if matched is None and file_tokens:
                    matched = file_tokens[0]
                if matched is None:
                    # Cross-file / cross-crate: the command name is a binding
                    # (``name::NODE``) declared in another file of the tree.
                    matched = _argument_token(
                        _sink_argument(compact, start), ordered, bindings
                    )
                if matched is None and sink_key.endswith("("):
                    # Source -> sink resolution of an indirect command name
                    # through candidate-defined symbols (function/method return,
                    # field, forwarded variable).  A candidate-defined symbol
                    # whose value cannot be shown benign fails closed.
                    kind, token, detail = _argument_resolution(
                        _sink_argument(compact, start), ordered, definitions
                    )
                    if kind == _RUNTIME:
                        matched = token
                    elif kind == _UNKNOWN and detail.startswith("unresolved:"):
                        matched = f"<unresolved: {detail}>"
                    elif kind == _UNKNOWN and tree_tokens:
                        # The sink's command argument is an unresolved symbol that
                        # is neither a resolvable binding nor a benign literal --
                        # e.g. a function parameter forwarded from a call site in
                        # another file (``fn run(p: &str) { Command::new(p) }`` /
                        # ``run("node")``) or a field/container element produced
                        # elsewhere.  The tree carries a source-runtime command
                        # identity, so fail closed (same cross-file signal rule
                        # ``ffi.py`` already applies) rather than assume that the
                        # unseen value is innocent.
                        matched = f"<tree-signal: {tree_tokens[0]}>"
                if matched is not None:
                    hits.append(
                        SubprocessHit(
                            path=path.relative_to(root).as_posix(),
                            line=_line_of(compact, start),
                            sink=sink,
                            token=matched,
                            evidence=window[:200],
                        ).as_dict()
                    )
                    break
                start = lowered.find(sink_key, start + 1)
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
