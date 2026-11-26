"""Detect process sinks that invoke the source language runtime.

This is deliberately narrower than a full taint engine: a hit is L2 only when a
process sink and a source-runtime token are both present in the candidate tree.
The detector still handles the common obfuscations that plain literal rules
miss: aliases, qualified names, whitespace and single-character concatenation.

Sink recognition is *not* a fixed spelling list.  For the migrated (target)
language the launch capability is resolved from the source itself:

* Go imports are parsed into ``local package name -> import path`` (covering
  aliases and dot imports), and the standard-library process-launch *symbols*
  are recognised by ``(import path, symbol)`` -- ``os/exec.Command``,
  ``os.StartProcess``, ``syscall.Exec``/``ForkExec``/``StartProcess``,
  ``golang.org/x/sys/unix.*``, a raw ``SYS_EXECVE`` ``Syscall*`` -- so every
  import alias and every equivalent writer (``sc.Exec``, ``o.StartProcess``,
  ``ex.Command``) is a sink, not only its canonical spelling;
* a reference to a launch symbol is itself the launch capability, so a function
  value (``var run = exec.Command``) is a sink too;
* an ``exec.Cmd`` *value* is tracked (explicit ``var``/field/parameter/``new``
  declarations) together with its field assignments and launch methods
  (``.Run``/``.Start``/``.Output``/``.CombinedOutput``), so a command populated
  field-by-field and only then run is a sink.

Every recognized site is resolved for its command identity; anything that is not
the sanctioned ``git`` -- or that cannot be resolved statically -- is a
fail-closed source-runtime fallback.  This keeps the detector ahead of
equivalent-writing variants (run-time-built names, import aliases, struct
literals, field-populated ``exec.Cmd`` values, function values) instead of
chasing individual spellings.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .runtime_identity import (
    decode_go_string_escapes,
    local_string_bindings,
    process_runtime_tokens,
    process_token_present,
    resolve_literal_expression,
    target_source_suffixes,
    token_present,
)


_SOURCE_SUFFIXES = frozenset(
    {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".py", ".go", ".rs", ".java"}
)
# Build-output directory names (``build``/``target``/``dist``) are deliberately
# NOT ignored: a source file may hide a delegated call by placing it in a
# directory that merely *looks* like generated output.  Only true dependency
# caches and VCS metadata are skipped.
_IGNORED_PARTS = frozenset(
    {".git", "node_modules", ".venv", "venv", "vendor", ".gomodcache", ".gocache", "__pycache__"}
)

# External commands that are part of the declared migration contract: the
# instruction fixes ``git`` as the repository-data source that the migrated core
# must execute itself.  A target-language process sink that resolves to anything
# else -- or that cannot be resolved at all -- is a fail-closed source-fallback
# signal (the same fail-closed direction the environment-variable rule already
# takes), because the migrated core has no other sanctioned subprocess.
_SANCTIONED_TARGET_COMMANDS = frozenset({"git", "git.exe"})
# Process sinks whose first argument is a context/selector rather than the
# command name; the command is the following argument.
_CONTEXT_AWARE_SINKS = ("CommandContext(",)
_TEST_FILE_NAME = re.compile(
    r"(?:^|/)(?:test_[^/]*|[^/]*(?:\.test|\.spec)\.[^/]*"
    r"|[^/]*_(?:test|spec)\.[^/]+|[^/]*(?:Test|Tests|Spec)\.[^/]+)$",
    re.IGNORECASE,
)

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
# Process-launch *selectors* whose package qualifier is not fixed by an import
# alias, so a plain substring list cannot enumerate them: any
# ``X.StartProcess(`` (``os.StartProcess`` and its import aliases,
# ``syscall.StartProcess``) and any ``X.Cmd{`` composite literal
# (``exec.Cmd`` / its ``os/exec`` import alias).  The migrated core has exactly
# one sanctioned subprocess (``git`` through ``exec.Command``), so every other
# launch writer is resolved for its command identity and fail-closed when the
# command cannot be proven to be ``git``.
_QUALIFIED_SINK_PATTERNS = (
    re.compile(r"[A-Za-z_][A-Za-z0-9_]*\s*\.\s*StartProcess\s*\("),
    re.compile(r"[A-Za-z_][A-Za-z0-9_]*\s*\.\s*Cmd\s*\{"),
)
_WINDOW = 240

# ---------------------------------------------------------------------------
# Go process-launch semantics
#
# A launch site is the *capability*, not a spelling.  The standard library
# exposes process creation through a closed set of package paths and symbols;
# resolving import aliases to their path and matching ``(path, symbol)`` makes
# every equivalent writer a sink (``sc.Exec`` == ``syscall.Exec``,
# ``o.StartProcess`` == ``os.StartProcess``), and a bare reference to such a
# symbol (a function value, ``var run = exec.Command``) is the same capability.
# ---------------------------------------------------------------------------
_GO_LAUNCH_CALL_SYMBOLS: Mapping[str, frozenset[str]] = {
    "os/exec": frozenset({"Command", "CommandContext"}),
    "os": frozenset({"StartProcess"}),
    "syscall": frozenset({"Exec", "ForkExec", "StartProcess"}),
    "golang.org/x/sys/unix": frozenset({"Exec", "ForkExec", "StartProcess"}),
}
# ``exec.Cmd`` composite literals: ``(path, type)``.
_GO_LAUNCH_TYPE_SYMBOLS: Mapping[str, frozenset[str]] = {
    "os/exec": frozenset({"Cmd"}),
}
# Raw syscalls only launch a process when their execve number is in the call.
_GO_RAW_SYSCALL_PACKAGES = frozenset({"syscall", "golang.org/x/sys/unix"})
_GO_RAW_SYSCALL_SYMBOLS = frozenset(
    {"RawSyscall", "RawSyscall6", "Syscall", "Syscall6"}
)
_GO_EXECVE_MARKERS = ("SYS_EXECVE", "SYS_EXECVEAT")
_GO_IMPORT_BLOCK = re.compile(r"\bimport\s*\(([^)]*)\)", re.S)
_GO_IMPORT_SINGLE = re.compile(
    r'\bimport\s+(?:(?P<name>[A-Za-z_.][A-Za-z0-9_.]*)\s+)?'
    r'(?P<path>"(?:[^"\\]|\\.)*")'
)
_GO_IMPORT_SPEC = re.compile(
    r'(?:(?P<name>[A-Za-z_.][A-Za-z0-9_.]*)\s+)?(?P<path>"(?:[^"\\]|\\.)*")'
)
_GO_SELECTOR_REF = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)")
_GO_SELECTOR_LITERAL = re.compile(
    r"([A-Za-z_][A-Za-z0-9_]*)\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)\s*\{"
)


def _unquote_go_literal(raw: str) -> str:
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        return decode_go_string_escapes(raw[1:-1])
    return raw


def _go_imports(text: str) -> tuple[dict[str, str], set[str]]:
    """Parse Go imports into ``local name -> import path`` plus dot imports.

    Handles single imports and grouped blocks, explicit aliases, dot imports and
    the blank identifier.  The default local name is the final path segment,
    which is the Go package name for every package the launch contract cares
    about (``os``, ``os/exec`` -> ``exec``, ``syscall``,
    ``golang.org/x/sys/unix`` -> ``unix``).
    """

    aliases: dict[str, str] = {}
    dot: set[str] = set()
    specs: list[tuple[str | None, str]] = []
    for block in _GO_IMPORT_BLOCK.finditer(text):
        for spec in _GO_IMPORT_SPEC.finditer(block.group(1)):
            specs.append((spec.group("name"), spec.group("path")))
    for single in _GO_IMPORT_SINGLE.finditer(text):
        specs.append((single.group("name"), single.group("path")))
    for name, raw_path in specs:
        path = _unquote_go_literal(raw_path)
        if name == ".":
            dot.add(path)
            continue
        if name == "_":
            continue
        local = name or path.rsplit("/", 1)[-1]
        local = local.strip()
        if local and local != ".":
            aliases[local] = path
    return aliases, dot


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


_STRING_LITERAL = r"""(?:"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|`[^`]*`)"""
_LITERAL_CHAIN = re.compile(_STRING_LITERAL + r"(?:\s*\+\s*" + _STRING_LITERAL + r")+")


def _resolved_literal_chains(window: str) -> list[str]:
    """Resolve ``"car" + "go"`` style chains into their runtime command.

    Concatenating every literal in a window glues the resolved command to the
    surrounding identifiers (``cargocommand``); resolving each ``+`` chain on
    its own keeps the token boundary intact.  Each part is decoded for Go
    escapes first, so ``"\\x63\\x61" + "\\x72\\x67\\x6f"`` resolves to ``cargo``.
    """

    values: list[str] = []
    for match in _LITERAL_CHAIN.finditer(window):
        parts = re.findall(_STRING_LITERAL, match.group(0), flags=re.S)
        resolved = "".join(
            part[1:-1] if part.startswith("`") else decode_go_string_escapes(part[1:-1])
            for part in parts
        ).casefold()
        if resolved:
            values.append(resolved)
    return values


_BYTE_ARRAY = re.compile(
    r"\[\](?:byte|uint8|rune|int32)\s*\{([^{}]*)\}"
)
_STRING_LITERAL_ONLY = re.compile(
    r"""\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'"""
)
_INT_TOKEN = re.compile(r"0[xX][0-9a-fA-F]+|\d+")


def _decode_base64(value: str) -> str:
    """Return the UTF-8 text of a base64 literal, or ``""`` when not decodable."""

    token = re.sub(r"\s+", "", value)
    if len(token) < 4:
        return ""
    padded = token + "=" * (-len(token) % 4)
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            raw = decoder(padded)
        except (binascii.Error, ValueError):
            continue
        text = raw.decode("utf-8", "ignore").strip()
        if text and any(character.isalpha() for character in text):
            return text
    return ""


def _decode_hex(value: str) -> str:
    """Return the UTF-8 text of a hex literal, or ``""`` when not decodable."""

    token = re.sub(r"\s+", "", value)
    if len(token) < 2 or len(token) % 2 or not re.fullmatch(r"[0-9a-fA-F]+", token):
        return ""
    try:
        raw = bytes.fromhex(token)
    except ValueError:
        return ""
    return raw.decode("utf-8", "ignore").strip()


def _decoded_candidates(window: str) -> list[str]:
    """Resolve run-time-constructed command names in a window.

    The plain lexical matchers only see characters that are literally present.
    An attacker can still build the delegated command at run time from an
    integer byte/rune slice, a base64/hex literal, or an environment variable.
    Decoding those forms here closes the common obfuscations without executing
    any candidate code.
    """

    values: set[str] = set()
    for match in _BYTE_ARRAY.finditer(window):
        numbers: list[int] = []
        for token in _INT_TOKEN.findall(match.group(1)):
            try:
                numbers.append(int(token, 0))
            except ValueError:
                continue
        if not numbers:
            continue
        values.add(bytes(number & 0xFF for number in numbers).decode("utf-8", "ignore"))
        values.add("".join(chr(number) for number in numbers if 0 < number < 0x110000))
    for match in _STRING_LITERAL_ONLY.finditer(window):
        literal = match.group(0)[1:-1]
        decoded_base64 = _decode_base64(literal)
        if decoded_base64:
            values.add(decoded_base64)
        decoded_hex = _decode_hex(literal)
        if decoded_hex:
            values.add(decoded_hex)
        # Go escape sequences (``"\x63\x61\x72\x67\x6f"``) are decoded by the
        # compiler, so the run-time command name is the decoded text.
        decoded_escapes = decode_go_string_escapes(literal)
        if decoded_escapes != literal:
            values.add(decoded_escapes)
    for chain in _resolved_literal_chains(window):
        values.add(chain)
    return [value for value in values if value]


# Environment-variable reads that can supply a run-time command name.  A process
# sink whose command is taken from the environment has no literal identity to
# verify, so the fail-closed L2 contract treats it as a source-runtime fallback.
_ENV_READ = re.compile(
    r"(?:os\.Getenv|os\.LookupEnv|os\.environ(?:\.get)?|os\.getenv"
    r"|std::env::var(?:_os)?|process\.env(?:\.\w+)?|System\.getenv"
    r"|Environment\.GetEnvironmentVariable|getenv)\b"
)
_ENV_ASSIGN = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_]*)\s*:?=\s*"
    r"(?:os\.Getenv|os\.LookupEnv|std::env::var(?:_os)?|process\.env"
    r"|os\.environ(?:\.get)?|System\.getenv|getenv)\b"
)


def _env_identifiers(text: str) -> set[str]:
    return {match.group(1) for match in _ENV_ASSIGN.finditer(text)}


def _command_argument(text: str, start: int, sink: str) -> str | None:
    """Return the first argument expression of a process-sink call."""

    position = start + len(sink)
    if sink.endswith("("):
        opening = position - 1
    else:
        opening = text.find("(", position)
        if opening < 0 or opening - position > 40:
            return None
    index = opening + 1
    depth = 0
    characters: list[str] = []
    while index < len(text):
        character = text[index]
        if character in ("'", '"', "`"):
            quote = character
            index += 1
            while index < len(text) and text[index] != quote:
                if text[index] == "\\":
                    index += 1
                index += 1
            index += 1
            continue
        if character in "([{":
            depth += 1
        elif character in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif character == "," and depth == 0:
            break
        characters.append(character)
        index += 1
    return "".join(characters).strip()


def _env_command_source(
    text: str, start: int, sink: str, env_names: set[str]
) -> str | None:
    """Return the environment expression when it names a sink's command."""

    if not env_names and not _ENV_READ.search(text):
        return None
    argument = _command_argument(text, start, sink)
    if not argument:
        return None
    if _ENV_READ.search(argument):
        return argument[:80]
    stringified = re.fullmatch(r"string\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)", argument)
    if stringified and stringified.group(1) in env_names:
        return argument[:80]
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", argument) and argument in env_names:
        return argument
    return None


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
        aliases.append(f"{match.group(1)}.CommandContext(")
        aliases.append(f"{match.group(1)}.Cmd{{")
    for match in re.finditer(
        r"import\s+([A-Za-z_][A-Za-z0-9_]*)\s+['\"]os['\"]", text
    ):
        aliases.append(f"{match.group(1)}.StartProcess(")
    return aliases


def _qualified_sink_tokens(text: str) -> list[str]:
    """Return the concrete ``X.StartProcess(`` / ``X.Cmd{`` sites in ``text``.

    The returned tokens are the exact source spans (so the shared
    ``str.find`` scan locates them), which lets the selector family cover every
    package qualifier -- ``os``/``syscall`` and any import alias -- without a
    per-spelling list.
    """

    tokens: list[str] = []
    seen: set[str] = set()
    for pattern in _QUALIFIED_SINK_PATTERNS:
        for match in pattern.finditer(text):
            if match.group(0).rstrip().endswith("{") and not _go_selector_is_composite_literal(
                text, match.start()
            ):
                continue
            token = match.group(0)
            if token not in seen:
                seen.add(token)
                tokens.append(token)
    return tokens


def _go_selector_is_composite_literal(text: str, start: int) -> bool:
    """Reject ``exec.Cmd`` return types that precede a function body brace.

    Go permits whitespace between a type and ``{``.  Consequently the lexical
    shape ``*exec.Cmd {`` occurs both in a composite literal and in a function
    declaration such as ``func gitCommand(...) *exec.Cmd {``.  A return type is
    not a process-launch site; the actual ``exec.Command`` call in the body is
    inspected separately.
    """

    index = start - 1
    while index >= 0 and text[index].isspace():
        index -= 1
    if index >= 0 and text[index] == "*":
        index -= 1
        while index >= 0 and text[index].isspace():
            index -= 1
    return index < 0 or text[index] != ")"


def _balanced_parens(text: str, opening: int) -> str | None:
    """Return the text between a balanced ``( ... )`` pair, or ``None``."""

    if opening < 0 or opening >= len(text) or text[opening] != "(":
        return None
    depth = 0
    index = opening
    while index < len(text):
        character = text[index]
        if character in ("'", '"', "`"):
            quote = character
            index += 1
            while index < len(text) and text[index] != quote:
                if text[index] == "\\":
                    index += 1
                index += 1
            index += 1
            continue
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return text[opening + 1 : index]
        index += 1
    return None


def _statement_expression(text: str, start: int) -> str:
    """Return the expression that follows an ``=`` up to the statement end."""

    depth = 0
    index = start
    out: list[str] = []
    while index < len(text):
        character = text[index]
        if character in ("'", '"', "`"):
            quote = character
            out.append(character)
            index += 1
            while index < len(text) and text[index] != quote:
                if text[index] == "\\":
                    out.append(text[index])
                    index += 1
                    if index >= len(text):
                        break
                out.append(text[index])
                index += 1
            if index < len(text):
                out.append(text[index])
                index += 1
            continue
        if character in "([{":
            depth += 1
        elif character in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif character in "\n;" and depth == 0:
            break
        out.append(character)
        index += 1
    return "".join(out).strip()


def _go_call_has_execve(text: str, reference_end: int) -> bool:
    """Return True when the call after ``reference_end`` is an ``execve`` syscall."""

    opening = text.find("(", reference_end)
    if opening < 0 or opening - reference_end > 40:
        return False
    inner = _balanced_parens(text, opening)
    if inner is None:
        return False
    return any(marker in inner for marker in _GO_EXECVE_MARKERS)


def _next_nonspace(text: str, index: int) -> int:
    while index < len(text) and text[index] in " \t\r\n":
        index += 1
    return index


def _go_function_value_alias(text: str, reference_start: int) -> str | None:
    """Return the local name a launch symbol is bound to, ``NAME = pkg.Symbol``."""

    match = re.search(
        r"([A-Za-z_][A-Za-z0-9_]*)\s*:?=\s*$", text[:reference_start]
    )
    return match.group(1) if match else None


def _go_launch_sink_tokens(text: str) -> list[str]:
    """Return concrete Go process-launch sites resolved from the file's imports.

    Imports are resolved to package paths, so a launch symbol is recognized by
    ``(import path, symbol)`` rather than by the local spelling: ``sc.Exec`` is
    the same sink as ``syscall.Exec``.  A composite ``exec.Cmd{...}`` literal is
    a launch site too.  A launch symbol written as a *value* (a function value,
    ``var run = exec.Command``) is resolved through its local alias: the alias
    call sites are judged for their command, and only an unprovable value (no
    local alias call) fails closed.  The shared identity resolver fails closed
    when a command cannot be proven to be ``git``.
    """

    imports, dot = _go_imports(text)
    tokens: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        if value and value not in seen:
            seen.add(value)
            tokens.append(value)

    for match in _GO_SELECTOR_REF.finditer(text):
        local, symbol = match.group(1), match.group(2)
        path = imports.get(local)
        if path is None:
            continue
        is_launch_call = symbol in _GO_LAUNCH_CALL_SYMBOLS.get(path, frozenset())
        is_raw = path in _GO_RAW_SYSCALL_PACKAGES and symbol in _GO_RAW_SYSCALL_SYMBOLS
        if not (is_launch_call or is_raw):
            continue
        following = _next_nonspace(text, match.end())
        is_call = following < len(text) and text[following] == "("
        if is_call:
            if is_launch_call or _go_call_has_execve(text, match.end()):
                add(match.group(0))
            continue
        if not is_launch_call:
            continue
        alias = _go_function_value_alias(text, match.start())
        alias_calls = 0
        if alias:
            for call in re.finditer(rf"(?<![.\w]){re.escape(alias)}\s*\(", text):
                add(call.group(0))
                alias_calls += 1
        if alias_calls == 0:
            # A bare launch-symbol value with no resolvable local call site is an
            # unprovable launch capability: fail closed.
            add(match.group(0))
    for match in _GO_SELECTOR_LITERAL.finditer(text):
        if not _go_selector_is_composite_literal(text, match.start()):
            continue
        local, type_name = match.group(1), match.group(2)
        path = imports.get(local)
        if path is not None and type_name in _GO_LAUNCH_TYPE_SYMBOLS.get(path, frozenset()):
            add(match.group(0))
    for path in dot:
        for symbol in sorted(_GO_LAUNCH_CALL_SYMBOLS.get(path, frozenset())):
            for match in re.finditer(rf"(?<![.\w]){re.escape(symbol)}\s*\(", text):
                add(match.group(0))
    return tokens


def _go_exec_cmd_handles(text: str, exec_locals: Sequence[str]) -> set[str]:
    """Return identifiers declared with an explicit ``exec.Cmd`` type."""

    if not exec_locals:
        return set()
    alternation = "|".join(re.escape(name) for name in exec_locals)
    names: set[str] = set()
    for match in re.finditer(
        rf"\b([A-Za-z_][A-Za-z0-9_]*)\s+\*?\s*(?:{alternation})\s*\.\s*Cmd\b", text
    ):
        names.add(match.group(1))
    for match in re.finditer(
        rf"\b([A-Za-z_][A-Za-z0-9_]*)\s*:?=\s*new\s*\(\s*(?:{alternation})\s*\.\s*Cmd\s*\)",
        text,
    ):
        names.add(match.group(1))
    return names


def _go_cmd_handle_identity(
    text: str, name: str, exec_locals: Sequence[str], bindings: Mapping[str, str]
) -> tuple[bool, bool, str]:
    """Resolve an ``exec.Cmd`` handle to ``(has_evidence, sanctioned, described)``.

    A locally declared ``exec.Cmd`` value is a launch site; its command identity
    is read from the writers that populate it (``.Path``/``.Args`` assignment, a
    composite literal or an ``exec.Command`` assignment).  A command that cannot
    be proven to be ``git`` -- including a local value with no provable writer --
    fails closed.  A handle that only appears as a parameter/field/return type is
    not reported here: the populated site is the sink.
    """

    pattern = re.escape(name)
    alternation = "|".join(re.escape(item) for item in exec_locals)
    violations: list[str] = []
    resolved_count = 0
    has_evidence = False

    def note(resolved: str | None, described: str) -> None:
        nonlocal resolved_count
        if resolved is None:
            violations.append(described or "<unresolved>")
            return
        resolved_count += 1
        if _basename(resolved) not in _SANCTIONED_TARGET_COMMANDS:
            violations.append(resolved)

    # A locally declared ``var NAME exec.Cmd`` is a process value by itself: a
    # launch of it that cannot be proven to run ``git`` fails closed.  A handle
    # that is only a parameter/field/return type is resolved where its command
    # is actually populated (the populated site is the sink), so an unpopulated
    # parameter is not reported here.
    if re.search(rf"\bvar\s+{pattern}\s+\*?\s*(?:{alternation})\s*\.\s*Cmd\b", text):
        has_evidence = True
    for match in re.finditer(
        rf"\b{pattern}\s*:?=\s*&?\s*(?:{alternation})\s*\.\s*Cmd\s*\{{", text
    ):
        has_evidence = True
        sanctioned, described = _command_of_struct_literal(text, match.end() - 1, bindings)
        if sanctioned:
            resolved_count += 1
        else:
            violations.append(described or "<unresolved>")
    for match in re.finditer(rf"\b{pattern}\s*\.\s*Path\s*=\s*", text):
        has_evidence = True
        expression = _statement_expression(text, match.end())
        note(resolve_literal_expression(expression, bindings), expression[:80])
    for match in re.finditer(rf"\b{pattern}\s*\.\s*Args\s*=\s*", text):
        has_evidence = True
        expression = _statement_expression(text, match.end())
        first = _first_string_element(expression)
        if first is None:
            note(None, expression[:80])
        else:
            note(resolve_literal_expression(first, bindings), first[:80])
    for match in re.finditer(
        rf"\b{pattern}\s*:?=\s*\*?\s*(?:{alternation})\s*\.\s*Command(Context)?\s*\(", text
    ):
        has_evidence = True
        arguments = _split_arguments(text[match.end() :])
        if not arguments:
            note(None, "<empty>")
            continue
        argument = arguments[1] if (match.group(1) and len(arguments) >= 2) else arguments[0]
        note(resolve_literal_expression(argument, bindings), argument[:80])

    if not has_evidence:
        return False, True, ""
    if resolved_count and not violations:
        return True, True, ""
    return True, False, (violations[0] if violations else "<unresolved>")


def _go_cmd_handle_hits(
    clean: str, relative: str, source: str, bindings: Mapping[str, str]
) -> list[dict[str, Any]]:
    """Return process-fallback hits for ``exec.Cmd`` values in target code."""

    imports, _dot = _go_imports(clean)
    exec_locals = sorted(
        {local for local, path in imports.items() if path == "os/exec"}
    )
    if not exec_locals:
        return []
    hits: list[dict[str, Any]] = []
    for name in sorted(_go_exec_cmd_handles(clean, exec_locals)):
        has_evidence, sanctioned, described = _go_cmd_handle_identity(
            clean, name, exec_locals, bindings
        )
        if not has_evidence or sanctioned:
            continue
        located = re.search(rf"\b{re.escape(name)}\b", clean)
        offset = located.start() if located else 0
        hits.append(
            SubprocessHit(
                path=relative,
                line=_line_of(source, offset),
                sink=f"{name} exec.Cmd",
                token=described,
                evidence=clean[max(0, offset - 80) : offset + 160],
            ).as_dict()
        )
    return hits


def _is_test_file(relative: str) -> bool:
    parts = relative.replace("\\", "/").split("/")
    name = parts[-1]
    if name.casefold().endswith(".go"):
        return name.casefold().endswith("_test.go")
    return "test" in {part.casefold() for part in parts[:-1]} or bool(
        _TEST_FILE_NAME.search(relative)
    )


def _basename(value: str) -> str:
    return value.replace("\\", "/").rsplit("/", 1)[-1].casefold()


def _opening_paren(text: str, start: int, sink: str) -> int | None:
    position = start + len(sink)
    if sink.endswith("("):
        return position - 1
    opening = text.find("(", position)
    if opening < 0 or opening - position > 40:
        return None
    return opening


def _command_of_sink(
    text: str, start: int, sink: str, bindings: Mapping[str, str]
) -> tuple[bool, str]:
    """Return ``(sanctioned, described_command)`` for a process sink.

    ``sanctioned`` is True only when the sink's command argument resolves to a
    sanctioned external tool.  An argument that cannot be resolved statically
    (environment variable, function call, struct field, index/map lookup) is
    *not* sanctioned: the migrated core must prove the command it executes, and
    the fail-closed contract treats an unprovable command as a source fallback.
    """

    if sink.endswith("{"):
        return _command_of_struct_literal(text, start, bindings)
    opening = _opening_paren(text, start, sink)
    if opening is None:
        return False, ""
    arguments = _split_arguments(text[opening + 1 :])
    if not arguments:
        return False, ""
    argument = arguments[0]
    if any(sink.rstrip("(").endswith(name.rstrip("(")) for name in _CONTEXT_AWARE_SINKS):
        if len(arguments) >= 2:
            argument = arguments[1]
        else:
            return False, ""
    resolved = resolve_literal_expression(argument, bindings)
    if resolved is not None:
        return _basename(resolved) in _SANCTIONED_TARGET_COMMANDS, resolved
    literals = [
        match.group(0)[1:-1] for match in re.finditer(_STRING_LITERAL, argument, flags=re.S)
    ]
    if literals and all(_basename(value) in _SANCTIONED_TARGET_COMMANDS for value in literals):
        return True, ""
    return False, (literals[0] if literals else argument[:80])


def _balanced_braces(text: str, opening: int) -> str | None:
    """Return the text between a balanced ``{ ... }`` pair, or ``None``."""

    if opening < 0 or opening >= len(text) or text[opening] != "{":
        return None
    depth = 0
    index = opening
    while index < len(text):
        character = text[index]
        if character in ("'", '"', "`"):
            quote = character
            index += 1
            while index < len(text) and text[index] != quote:
                if text[index] == "\\":
                    index += 1
                index += 1
            index += 1
            continue
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return text[opening + 1 : index]
        index += 1
    return None


def _struct_literal_fields(body: str) -> dict[str, str]:
    """Map ``Key: value`` struct-literal fields to their value expressions."""

    fields: dict[str, str] = {}
    for item in _split_arguments(body):
        key, separator, value = item.partition(":")
        if not separator:
            continue
        key = key.strip()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            fields[key] = value.strip()
    return fields


def _first_string_element(expression: str) -> str | None:
    """Return the first element expression of a ``[]string{...}`` literal."""

    match = re.search(r"\[\]\s*string\s*\{", expression)
    if match is None:
        return None
    body = _balanced_braces(expression, match.end() - 1)
    if body is None:
        return None
    elements = _split_arguments(body)
    return elements[0] if elements else None


def _command_of_struct_literal(
    text: str, start: int, bindings: Mapping[str, str]
) -> tuple[bool, str]:
    """Return ``(sanctioned, described)`` for an ``X.Cmd{...}`` process literal.

    ``exec.Cmd`` carries the command through ``Path`` (with ``Args`` holding the
    argv); a literal without ``Path`` falls back to ``Args[0]``.  Every path is
    resolved with the same decoder as call sinks so a run-time-built name
    (byte array / escape / concatenation / local constant) is judged by its
    run-time identity, and an unprovable command fails closed.
    """

    brace = text.find("{", start)
    if brace < 0 or brace - start > 40:
        return False, ""
    body = _balanced_braces(text, brace)
    if body is None:
        return False, ""
    fields = _struct_literal_fields(body)
    if "Path" in fields:
        expression = fields["Path"]
    elif "Args" in fields:
        first = _first_string_element(fields["Args"])
        if first is None:
            return False, fields["Args"][:80]
        expression = first
    else:
        return False, ""
    resolved = resolve_literal_expression(expression, bindings)
    if resolved is not None:
        return _basename(resolved) in _SANCTIONED_TARGET_COMMANDS, resolved
    literals = [
        match.group(0)[1:-1] for match in re.finditer(_STRING_LITERAL, expression, flags=re.S)
    ]
    if literals and all(_basename(value) in _SANCTIONED_TARGET_COMMANDS for value in literals):
        return True, ""
    return False, (literals[0] if literals else expression[:80])


def _split_arguments(text: str) -> list[str]:
    """Split a call's argument text into top-level arguments."""

    arguments: list[str] = []
    depth = 0
    current: list[str] = []
    index = 0
    while index < len(text):
        character = text[index]
        if character in ("'", '"', "`"):
            quote = character
            current.append(character)
            index += 1
            while index < len(text) and text[index] != quote:
                if text[index] == "\\":
                    current.append(text[index])
                    index += 1
                    if index >= len(text):
                        break
                current.append(text[index])
                index += 1
            if index < len(text):
                current.append(text[index])
                index += 1
            continue
        if character in "([{":
            depth += 1
        elif character in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif character == "," and depth == 0:
            arguments.append("".join(current).strip())
            current = []
            index += 1
            continue
        current.append(character)
        index += 1
    tail = "".join(current).strip()
    if tail:
        arguments.append(tail)
    return arguments


def _token_present(text: str, token: str) -> bool:
    """Backward-compatible lexical matcher for test/internal callers."""

    return token_present(text, token)


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


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
    target_suffixes = target_source_suffixes(config)
    hits: list[dict[str, Any]] = []
    for path in _candidate_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        relative = path.relative_to(root).as_posix()
        clean = _strip_comments(text)
        lowered = clean.casefold()
        env_names = _env_identifiers(clean)
        # The command-identity check applies to *target-language production*
        # code: protected source-language modules (e.g. a Rust TUI module that
        # spawns a user command) and target test files are not the migration's
        # repository-data path.
        in_target_language = path.suffix.casefold() in target_suffixes
        identity_scope = in_target_language and not _is_test_file(relative)
        bindings = local_string_bindings(clean) if identity_scope else {}
        for sink in (
            *_SINKS,
            *_alias_sinks(clean),
            *_qualified_sink_tokens(clean),
            *_go_launch_sink_tokens(clean),
        ):
            start = lowered.find(sink.casefold())
            while start >= 0:
                # Include the preceding window too: an attacker commonly binds
                # the runtime command to a local first (``tool := "car"+"go"``)
                # and only passes the variable to the sink, so a forward-only
                # window cannot resolve the concatenated/aliased token.
                begin = max(0, start - _WINDOW)
                end = min(len(clean), start + _WINDOW)
                window = clean[begin:end]
                collapsed = _collapsed(window)
                joined = _joined_literals(window)
                chains = _resolved_literal_chains(window)
                decoded = _decoded_candidates(window)
                forms = (window, collapsed, joined, *chains, *decoded)
                matched = next(
                    (
                        token for token in wanted
                        if any(process_token_present(form, token) for form in forms)
                    ),
                    None,
                )
                if matched is not None:
                    hits.append(
                        SubprocessHit(
                            path=relative,
                            line=_line_of(text, start),
                            sink=sink,
                            token=matched,
                            evidence=window[:200],
                        ).as_dict()
                    )
                    break
                env_source = _env_command_source(clean, start, sink, env_names)
                if env_source is not None:
                    hits.append(
                        SubprocessHit(
                            path=relative,
                            line=_line_of(text, start),
                            sink=sink,
                            token=env_source,
                            evidence=window[:200],
                        ).as_dict()
                    )
                    break
                if identity_scope:
                    sanctioned, described = _command_of_sink(clean, start, sink, bindings)
                    if not sanctioned:
                        hits.append(
                            SubprocessHit(
                                path=relative,
                                line=_line_of(text, start),
                                sink=sink,
                                token=described or "<unresolved>",
                                evidence=window[:200],
                            ).as_dict()
                        )
                        break
                start = lowered.find(sink.casefold(), start + 1)
        if identity_scope:
            hits.extend(_go_cmd_handle_hits(clean, relative, text, bindings))
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
