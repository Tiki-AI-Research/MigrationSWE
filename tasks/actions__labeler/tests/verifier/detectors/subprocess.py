"""Detect process sinks that invoke the source language runtime.

This is deliberately narrower than a full taint engine: a hit is L2 only when a
process sink and a source-runtime token are both present in the candidate tree.
The detector still handles the common obfuscations that plain literal rules
miss: aliases, qualified names, whitespace and single-character concatenation.
"""

from __future__ import annotations

import re
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .runtime_identity import (
    process_runtime_tokens,
    process_token_present,
    token_present,
)
from .process_commands import runtime_in_argv


_SOURCE_SUFFIXES = frozenset(
    {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".py", ".go", ".rs", ".java"}
)
_IGNORED_PARTS = frozenset(
    {".git", "node_modules", ".venv", "venv", "target", "dist", "build", "vendor", ".gomodcache", ".gocache", "__pycache__"}
)
# Production code is what the migration-integrity gate protects.  Test/probe
# sources are injected by the evaluator and legitimately name process APIs for
# their own assertions, so they must never be treated as production fallback.
_TEST_DIR_PARTS = frozenset(
    {"test", "tests", "__tests__", "spec", "specs", "e2e", "testdata", "f2pprobe"}
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
    "os.StartProcess(",
    "syscall.Exec(",
    "syscall.ForkExec(",
    "syscall.Syscall(",
    "syscall.RawSyscall(",
    "std::process::Command",
    "Command::new(",
    "tokio::process",
    "process::Command",
    "ProcessBuilder(",
    "Runtime.exec(",
    "Runtime.getRuntime().exec(",
)
# Payload-encoding helpers.  A process sink that receives a decoded/obfuscated
# command is exactly the "hidden fallback" the gate must catch, even when the
# decoded runtime name never appears as a literal.
_ENCODING_SINKS = (
    "base64.stdencoding.decodestring(",
    "base64.rawstdencoding.decodestring(",
    "base64.urlencoding.decodestring(",
    "base64.b64decode(",
    "base64.b64encode(",
    "base64.encodebytes(",
    "hex.decodestring(",
    "bytes.fromhex(",
    "codecs.decode(",
    "atob(",
    "fromcharcode",
)
_JS_ARTIFACT = re.compile(r"\.(?:cjs|mjs|js|ts|tsx)\b", re.IGNORECASE)
_WINDOW = 320


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
    # Keep quoted shell fragments, URLs and escaped quotes intact. Replacing
    # comments with spaces preserves offsets used by diagnostic line numbers.
    chars, quote, cursor = list(text), "", 0
    while cursor < len(text):
        char = text[cursor]
        if quote:
            if char == "\\" and quote != "`":
                cursor += 2
                continue
            if char == quote:
                quote = ""
        elif char in "'\"`":
            quote = char
        elif text.startswith("/*", cursor):
            end = text.find("*/", cursor + 2)
            end = len(text) if end < 0 else end + 2
            for index in range(cursor, end):
                if chars[index] != "\n":
                    chars[index] = " "
            cursor = end
            continue
        elif text.startswith("//", cursor) or char == "#":
            end = text.find("\n", cursor)
            end = len(text) if end < 0 else end
            chars[cursor:end] = " " * (end - cursor)
            cursor = end
            continue
        cursor += 1
    return "".join(chars)


def _collapsed(text: str) -> str:
    """Remove quotes, plus signs and whitespace to expose 'g' + 'o' splits."""

    return re.sub(r"[\s'\"+`]", "", text).casefold()


def _joined_literals(text: str) -> str:
    """Concatenate string literals in a window to expose 'g' + 'o' splits."""

    return "".join(re.findall(r"['\"]([^'\"]*)['\"]", text)).casefold()


def _plus_joined(text: str) -> str:
    """Resolve ``"a" + "b"`` literal concatenation, keeping other text intact.

    ``"/usr/bin/no" + "de"`` becomes ``"/usr/bin/node"`` so a runtime token that
    was split across adjacent literals is matched without inventing a match
    across unrelated statements (which the all-literals join can do).
    """

    pattern = re.compile(r"(['\"])([^'\"]*)\1\s*\+\s*(['\"])([^'\"]*)\3")
    previous = None
    while previous != text:
        previous = text
        text = pattern.sub(
            lambda match: match.group(1) + match.group(2) + match.group(4) + match.group(1),
            text,
        )
    return text.casefold()


_GO_INT_ARRAY = re.compile(
    r"\[\]\s*"
    r"(?:byte|rune|uint8|uint16|uint32|uint64|int|int8|int16|int32|int64)\s*"
    r"\{([^{}]*)\}"
)
_JS_FROMCHARCODE = re.compile(r"fromCharCode\s*\(\s*([^)]*)\)", re.IGNORECASE)
_PY_BYTES_ARRAY = re.compile(
    r"\b(?:bytes|bytearray)\s*\(\s*\[([^\]]*)\]\s*\)", re.IGNORECASE
)
_NUMERIC_LITERAL = re.compile(r"0[xX][0-9a-fA-F]+|0[oO][0-7]+|0[bB][01]+|\d+|'(?:\\.|[^'])'")
_SIMPLE_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", "\\": "\\", "'": "'", '"': '"'}


def _decode_char_literal(inner: str) -> str:
    if inner.startswith("\\"):
        rest = inner[1:]
        if rest in _SIMPLE_ESCAPES:
            return _SIMPLE_ESCAPES[rest]
        if rest[:1] in ("x", "u", "U"):
            try:
                return chr(int(rest[1:], 16))
            except (ValueError, OverflowError):
                return ""
        return ""
    return inner if len(inner) == 1 else ""


def _decode_numeric_literals(body: str) -> str:
    """Rebuild a string from a numeric/char-array initializer body.

    A hidden process fallback frequently reconstructs the runtime command name
    (``string([]byte{110, 111, 100, 101})``, ``assemble([]int{0x6e, ...})``) so
    the runtime token never appears as a literal.  Reconstructing the constants
    makes that command visible to the token matcher.
    """

    codes: list[int] = []
    for token in _NUMERIC_LITERAL.findall(body):
        try:
            if token[:2].casefold() == "0x":
                codes.append(int(token, 16))
            elif token[:2].casefold() == "0o":
                codes.append(int(token, 8))
            elif token[:2].casefold() == "0b":
                codes.append(int(token, 2))
            elif token[0] == "'":
                decoded = _decode_char_literal(token[1:-1])
                if not decoded:
                    return ""
                codes.append(ord(decoded))
            else:
                codes.append(int(token))
        except (ValueError, OverflowError):
            return ""
        if not 0 <= codes[-1] <= 0x10FFFF:
            return ""
    if not codes:
        return ""
    try:
        return "".join(chr(code) for code in codes)
    except ValueError:
        return ""


def _decoded_arrays(text: str) -> list[str]:
    """Decode character/byte/rune array constants to their string values."""

    values: list[str] = []
    for match in _GO_INT_ARRAY.finditer(text):
        decoded = _decode_numeric_literals(match.group(1))
        if decoded:
            values.append(decoded)
    for pattern in (_JS_FROMCHARCODE, _PY_BYTES_ARRAY):
        for match in pattern.finditer(text):
            decoded = _decode_numeric_literals(match.group(1))
            if decoded:
                values.append(decoded)
    return values


def _command_like(value: str) -> bool:
    """True when a decoded constant looks like a command/path, not prose."""

    value = value.strip()
    if not 2 <= len(value) <= 128:
        return False
    return re.fullmatch(r"[A-Za-z0-9_./:\\-]+", value) is not None


_ENV_LOOKUP_ASSIGNMENTS = (
    re.compile(
        r"\b([A-Za-z_$][\w$]*)\s*(?:,\s*[A-Za-z_$][\w$]*\s*)?:?=\s*"
        r"(?:os\.(?:Getenv|LookupEnv)|syscall\.Getenv)\s*\(",
    ),
    re.compile(
        r"\b([A-Za-z_$][\w$]*)\s*(?:,\s*[A-Za-z_$][\w$]*\s*)?:?=\s*"
        r"os\.Environ\s*\(\s*\)\s*\[",
    ),
    re.compile(
        r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*process\.env\b",
    ),
    re.compile(
        r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*os\.environ\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?m)^\s*([A-Za-z_$][\w$]*)\s*(?:,\s*[A-Za-z_$][\w$]*\s*)?=\s*"
        r"os\.(?:getenv|environ)\b",
        re.IGNORECASE,
    ),
)
_ENV_LOOKUP_CALL = re.compile(
    r"(?:os\.(?:Getenv|LookupEnv)|syscall\.Getenv|process\.env|os\.environ"
    r"|\bgetenv\s*\(|\$\{?[A-Za-z_])",
    re.IGNORECASE,
)


def _env_tainted_names(text: str) -> set[str]:
    """Collect identifiers assigned from an environment lookup.

    A process sink that resolves its command from the environment is a hidden
    source-runtime fallback: the runtime name never appears as a literal, so the
    token matcher cannot see it.  Tracking the identifiers bound to
    ``os.Getenv``/``os.LookupEnv``/``process.env``/``os.environ`` lets the sink
    check reject that indirect, attacker-controlled command.
    """

    names: set[str] = set()
    for pattern in _ENV_LOOKUP_ASSIGNMENTS:
        for match in pattern.finditer(text):
            names.add(match.group(1))
    return names


def _first_argument(text: str, index: int) -> str:
    """Return the text of the first call argument starting at ``index``."""

    depth = 0
    quote = ""
    cursor = index
    length = len(text)
    while cursor < length:
        character = text[cursor]
        if quote:
            if character == "\\":
                cursor += 2
                continue
            if character == quote:
                quote = ""
            cursor += 1
            continue
        if character in ("'", '"', "`"):
            quote = character
            cursor += 1
            continue
        if character in "([{":
            depth += 1
        elif character in ")]}":
            if depth == 0:
                return text[index:cursor]
            depth -= 1
        elif character == "," and depth == 0:
            return text[index:cursor]
        cursor += 1
    return text[index:cursor]


def _command_argument(text: str, sink_start: int, sink: str) -> str:
    """Return the command/exec first argument following a process sink."""

    if sink.endswith("("):
        argument_start = sink_start + len(sink)
    else:
        paren = text.find("(", sink_start + len(sink))
        if paren < 0 or paren - (sink_start + len(sink)) > 48:
            return ""
        argument_start = paren + 1
    argument = _first_argument(text, argument_start)
    if sink.casefold().endswith(".commandcontext("):
        # CommandContext's first argument is a context, not the executable.
        argument_start += len(argument)
        if argument_start < len(text) and text[argument_start] == ",":
            argument = _first_argument(text, argument_start + 1)
    return argument


def _literal_go_command(text: str, start: int, sink: str) -> str | None:
    """Return a known Go command target, without borrowing nearby strings.

    A GraphQL field named "node" next to exec.Command("git", ...) is not
    evidence that the process invokes Node. Keep dynamic targets on the
    existing conservative path so environment and encoded delegates are still
    inspected rather than accepted as literal commands.
    """
    if not re.fullmatch(r"\w+\.(?:Command|CommandContext|StartProcess|Exec|ForkExec)\(", sink):
        return None
    expression = _plus_joined(_command_argument(text, start, sink)).strip()
    if re.fullmatch(r"`[^`]*`", expression):
        return expression[1:-1]
    try:
        value = json.loads(expression)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, str) else None


def _literal_go_argv(text: str, start: int, sink: str) -> list[str] | None:
    """Read literal arguments of exec.Command[Context], bounded to this call.

    A shell's data arguments and unrelated neighbouring GraphQL strings must
    never supply evidence of the executable being launched.
    """
    if not re.fullmatch(r"\w+\.Command(?:Context)?\(", sink):
        return None
    cursor = start + len(sink)
    expressions = []
    while cursor < len(text):
        expression = _first_argument(text, cursor)
        if not expression.strip():
            break
        expressions.append(expression)
        cursor += len(expression)
        if cursor >= len(text) or text[cursor] != ",":
            break
        cursor += 1
    if sink.endswith(".CommandContext("):
        expressions = expressions[1:]
    arguments = []
    for expression in expressions:
        expression = _plus_joined(expression).strip()
        if re.fullmatch(r"`[^`]*`", expression):
            arguments.append(expression[1:-1])
            continue
        try:
            value = json.loads(expression)
        except (ValueError, TypeError):
            return None
        if not isinstance(value, str):
            return None
        arguments.append(value)
    return arguments or None


def _is_environment_command(argument: str, env_names: set[str]) -> bool:
    """True when a sink's first argument is resolved from the environment."""

    value = argument.strip()
    if not value:
        return False
    if _ENV_LOOKUP_CALL.search(value):
        return True
    if re.fullmatch(r"[A-Za-z_$][\w$]*", value):
        return value in env_names
    head = re.match(r"[A-Za-z_$][\w$]*", value)
    return bool(head and head.group(0) in env_names)


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
    return aliases


def _token_present(text: str, token: str) -> bool:
    """Backward-compatible lexical matcher for test/internal callers."""

    return token_present(text, token)


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def evaluator_injected_parts(config: Mapping[str, Any]) -> frozenset[str]:
    """Directory names the case declares as evaluator-injected harness assets.

    The verifier materializes its own probes/harnesses into the candidate
    repository before running them (for example the ``blackbox`` contract
    probes).  Those files are authored by the evaluator, not the candidate, and
    legitimately name process APIs and source-technology strings for their own
    assertions, so they must never be treated as a candidate production
    fallback.  The case declares the injected destinations under
    ``policy.evaluator_injected_paths``; this is an evaluator-asset exclusion,
    not a per-candidate allowlist.
    """

    policy = config.get("policy") if isinstance(config, Mapping) else None
    if not isinstance(policy, Mapping):
        return frozenset()
    raw = policy.get("evaluator_injected_paths", [])
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return frozenset()
    parts: set[str] = set()
    for item in raw:
        text = str(item).strip().strip("/")
        if not text or ".." in text.split("/"):
            continue
        parts.update(part.casefold() for part in text.split("/") if part)
    return frozenset(parts)


def _is_test_source(
    relative: Path, injected_parts: frozenset[str] = frozenset()
) -> bool:
    name = relative.name.casefold()
    if name.endswith("_test.go") or name.endswith("_test.py"):
        return True
    if ".test." in name or ".spec." in name:
        return True
    if name.endswith("_test.snap") or name.endswith("_spec.rb"):
        return True
    test_parts = _TEST_DIR_PARTS | injected_parts
    return any(
        part.casefold() in test_parts for part in relative.parts[:-1]
    )


def _candidate_files(
    root: Path, injected_parts: frozenset[str] = frozenset()
) -> list[Path]:
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
        if _is_test_source(relative, injected_parts):
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
    hits: list[dict[str, Any]] = []
    injected_parts = evaluator_injected_parts(config)
    for path in _candidate_files(root, injected_parts):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        clean = _strip_comments(text)
        lowered = clean.casefold()
        file_encoding = any(sink in lowered for sink in _ENCODING_SINKS)
        file_artifact = bool(_JS_ARTIFACT.search(clean))
        env_names = _env_tainted_names(clean)
        for sink in (*_SINKS, *_alias_sinks(clean)):
            start = lowered.find(sink.casefold())
            while start >= 0:
                argv = _literal_go_argv(clean, start, sink) if path.suffix == ".go" else None
                if argv is not None:
                    matched = runtime_in_argv(argv, wanted)
                    if matched:
                        hits.append(SubprocessHit(
                            path=path.relative_to(root).as_posix(),
                            line=_line_of(text, start), sink=sink, token=matched,
                            evidence=clean[start:start + 200],
                        ).as_dict())
                        break
                    start = lowered.find(sink.casefold(), start + len(sink))
                    continue
                command = _literal_go_command(clean, start, sink) if path.suffix == ".go" else None
                if command is not None:
                    executable = command.rsplit("/", 1)[-1].casefold()
                    wrapper = executable in {"sh", "bash", "dash", "zsh", "env", "python", "python3", "perl", "ruby"}
                    invokes_source = any(process_token_present(command, token) for token in wanted)
                    if not wrapper and not invokes_source and not _JS_ARTIFACT.search(command):
                        start = lowered.find(sink.casefold(), start + len(sink))
                        continue
                begin = max(0, start - _WINDOW)
                end = min(len(clean), start + _WINDOW)
                window = clean[begin:end]
                array_views = _decoded_arrays(window)
                views = (
                    window,
                    _collapsed(window),
                    _joined_literals(window),
                    _plus_joined(window),
                    *array_views,
                )
                matched = next(
                    (
                        token for token in wanted
                        if any(process_token_present(view, token) for view in views)
                    ),
                    None,
                )
                if matched is None:
                    # The spawned command may be forwarded indirectly through an
                    # environment variable (``syscall.Exec(os.Getenv(...), ...)``
                    # / ``exec.Command(runtime, ...)``).  The runtime name never
                    # becomes a literal, so a command derived from the
                    # environment must be flagged as a hidden fallback.
                    argument = _command_argument(clean, start, sink)
                    if argument and _is_environment_command(argument, env_names):
                        matched = "<environment-derived-command>"
                if matched is None and wanted:
                    # A command name rebuilt from a character/byte/rune array is
                    # already an obfuscation signal near a process sink, even if
                    # the decoded token is not in the runtime token list.
                    if any(_command_like(value) for value in array_views):
                        matched = "<constructed-command>"
                    else:
                        # A spawned command whose payload is decoded/obfuscated
                        # or points at a JavaScript/TypeScript artifact is a
                        # hidden source fallback even when the runtime name is
                        # never a literal.  The whole file is used because the
                        # encoder and the sink may sit a few statements apart.
                        encoded = file_encoding and any(
                            sink_token in window.casefold() for sink_token in _ENCODING_SINKS
                        )
                        if encoded or file_artifact:
                            matched = "<encoded-or-artifact-runtime-payload>"
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


__all__ = [
    "detect_source_runtime_subprocess",
    "source_runtime_tokens",
    "evaluator_injected_parts",
]
