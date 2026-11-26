"""Detect process sinks that invoke the source language runtime.

This is deliberately narrower than a full taint engine: a hit is L2 only when a
process sink and a source-runtime token are both present in the candidate tree.
The detector still handles the common obfuscations that plain literal rules
miss: aliases, qualified names, whitespace and single-character concatenation.
"""

from __future__ import annotations

import ast
import base64
import binascii
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
    {
        ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".py", ".go", ".rs", ".java",
        # Shell/batch wrappers are a common way to hide a source-runtime bridge
        # from a source-file scanner; scan them by content too.
        ".sh", ".bash", ".zsh", ".ksh", ".cmd", ".bat", ".ps1",
    }
)
_IGNORED_PARTS = frozenset(
    {".git", "node_modules", ".venv", "venv", "target", "dist", "build", "vendor", ".gomodcache", ".gocache", "__pycache__"}
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
    "create_subprocess_exec(",
    "create_subprocess_shell(",
    "os.posix_spawn(",
    "os.posix_spawnp(",
    "os.spawn",
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
# Shell scripts do not use an API name to start a process; the command itself is
# the sink.  ``exec node dist/index.js`` inside a wrapper is a source-runtime
# fallback exactly like ``subprocess.Popen(["node", ...])``.
_SHELL_SUFFIXES = frozenset({".sh", ".bash", ".zsh", ".ksh", ".cmd", ".bat", ".ps1"})
_SHELL_SINKS = (
    "exec ",
    "node ",
    "nodejs ",
    "npx ",
    "/bin/sh",
    "/bin/bash",
    "sh -c",
    "bash -c",
    "cmd /c",
)
_WINDOW = 240
_MAX_EXTENSIONLESS_BYTES = 1_000_000
_OS_PROCESS_NAMES = frozenset(
    {
        "system", "popen", "execv", "execve", "execvp", "execvpe", "execl",
        "execle", "execlp", "execlpe", "spawnl", "spawnle", "spawnlp", "spawnv",
        "spawnve", "spawnvp", "spawnvpe", "posix_spawn", "posix_spawnp",
    }
)
_ESCAPE_PATTERNS = (
    (re.compile(r"\\U([0-9a-fA-F]{8})"), 16),
    (re.compile(r"\\u([0-9a-fA-F]{4})"), 16),
    (re.compile(r"\\x([0-9a-fA-F]{2})"), 16),
    (re.compile(r"\\([0-7]{1,3})"), 8),
)
_B64_CALL = re.compile(
    r"(?:b64decode|urlsafe_b64decode|standard_b64decode|a2b_base64)\s*\(\s*"
    r"b?['\"]([A-Za-z0-9+/=_-]+)['\"]"
)
_HEX_CALL = re.compile(r"fromhex\s*\(\s*['\"]([0-9a-fA-F\s]+)['\"]")


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


_BLOCK_STRING_LITERAL = re.compile(r'"""(?:.|\n)*?"""|\'\'\'(?:.|\n)*?\'\'\'')
_PLAIN_STRING_LITERAL = re.compile(r'"(?:[^"\\\n]|\\.)*"|\'(?:[^\'\\\n]|\\.)*\'')


def _joined_literals(text: str) -> str:
    """Concatenate string literals in a window to expose 'g' + 'o' splits.

    Triple-quoted strings are removed first so a window that starts inside a
    docstring does not mis-pair its quotes.  This lets a token assembled as a
    list of fragments (``["lib", "node", ".so"]``) collapse to ``libnode.so``.
    """

    text = _BLOCK_STRING_LITERAL.sub(" ", text)
    return "".join(
        match.group(0)[1:-1] for match in _PLAIN_STRING_LITERAL.finditer(text)
    ).casefold()


_STRING_LITERAL = r"\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'"
_CONCATENATED_LITERALS = re.compile(
    r"(?:" + _STRING_LITERAL + r")(?:\s*\+\s*(?:" + _STRING_LITERAL + r"))+"
)
_SINGLE_LITERAL = re.compile(_STRING_LITERAL)


def _packed_literals(text: str) -> str:
    """Join adjacent concatenated string literals (``"no" + "de"`` -> ``node``).

    Unlike :func:`_collapsed` this keeps other identifiers separated, so a
    source-runtime token assembled from fragments is exposed without merging it
    into the following statement.
    """

    values: list[str] = []
    for match in _CONCATENATED_LITERALS.finditer(text):
        parts = _SINGLE_LITERAL.findall(match.group(0))
        if len(parts) >= 2:
            values.append("".join(part[1:-1] for part in parts))
    return " ".join(values).casefold()


def _decode_escapes(text: str) -> str:
    """Decode ``\\xNN``/``\\uNNNN``/``\\UNNNNNNNN``/octal escapes in a window.

    A source-runtime name written as ``"\\x6e\\x6f\\x64\\x65"`` is the same
    command as ``"node"`` at run time; exposing the decoded form keeps such
    payloads inside the lexical matcher instead of requiring a full taint pass.
    """

    def replace(match: re.Match[str], base: int) -> str:
        try:
            value = int(match.group(1), base)
        except ValueError:
            return match.group(0)
        if value > 0x10FFFF:
            return match.group(0)
        return chr(value)

    for pattern, base in _ESCAPE_PATTERNS:
        text = pattern.sub(lambda m, b=base: replace(m, b), text)
    return text


def _decoded_payloads(text: str) -> str:
    """Decode base64/``bytes.fromhex`` literal payloads into text."""

    values: list[str] = []
    for match in _B64_CALL.finditer(text):
        padded = match.group(1) + "=" * (-len(match.group(1)) % 4)
        for decoder in (base64.b64decode, base64.urlsafe_b64decode):
            try:
                values.append(decoder(padded).decode("utf-8", "ignore"))
            except (binascii.Error, ValueError):
                continue
    for match in _HEX_CALL.finditer(text):
        compact = re.sub(r"\s+", "", match.group(1))
        try:
            values.append(bytes.fromhex(compact).decode("utf-8", "ignore"))
        except ValueError:
            continue
    return " ".join(values).casefold()


def _python_constant_process_hits(text: str, relative: str, wanted: set[str]) -> list[dict[str, Any]]:
    """Follow bounded literal values into imported Python process APIs.

    An OS module alias and a decoded command assigned before the call have no
    literal sink/token window. Resolve their actual import and argument rather
    than treating every decoded payload near an unrelated process as fallback.
    Candidate expressions are inspected, never executed.
    """
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return []
    hits: list[dict[str, Any]] = []

    def qualified(node, aliases):
        if isinstance(node, ast.Name):
            return aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            return qualified(node.value, aliases) + '.' + node.attr
        return ''

    def value(node, aliases, constants, depth=0):
        if depth > 12:
            return None
        descend = lambda child: value(child, aliases, constants, depth + 1)
        try:
            if isinstance(node, ast.Constant):
                return node.value if isinstance(node.value, (str, bytes, int)) else None
            if isinstance(node, ast.Name):
                return constants.get(node.id)
            if isinstance(node, (ast.List, ast.Tuple)) and len(node.elts) <= 256:
                return [descend(item) for item in node.elts]
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
                left, right = descend(node.left), descend(node.right)
                if type(left) is type(right) and isinstance(left, (str, bytes)):
                    return (left + right)[:8192]
            if not isinstance(node, ast.Call):
                return None
            name = qualified(node.func, aliases)
            args = [descend(arg) for arg in node.args]
            if name in {'base64.b64decode', 'base64.urlsafe_b64decode',
                        'base64.standard_b64decode', 'binascii.a2b_base64'} and args:
                if isinstance(args[0], (str, bytes)) and len(args[0]) <= 8192:
                    return base64.urlsafe_b64decode(args[0])
            if name == 'bytes.fromhex' and args and isinstance(args[0], str):
                return bytes.fromhex(args[0][:8192])
            if name == 'bytes' and args and isinstance(args[0], list):
                return bytes(args[0])
            if name == 'chr' and len(args) == 1 and isinstance(args[0], int):
                return chr(args[0])
            if isinstance(node.func, ast.Attribute):
                owner = descend(node.func.value)
                if node.func.attr == 'decode' and isinstance(owner, bytes):
                    return owner.decode(args[0] if args and isinstance(args[0], str) else 'utf-8')
                if node.func.attr == 'join' and isinstance(owner, str) and args:
                    if isinstance(args[0], list) and all(isinstance(x, str) for x in args[0]):
                        return owner.join(args[0])[:8192]
        except (ValueError, TypeError, UnicodeError, LookupError, OverflowError):
            return None
        return None

    def block(statements, aliases, constants):
        aliases, constants = dict(aliases), dict(constants)
        for statement in statements:
            if isinstance(statement, ast.Import):
                for item in statement.names:
                    aliases[item.asname or item.name.split('.')[0]] = item.name
            elif isinstance(statement, ast.ImportFrom) and statement.module:
                for item in statement.names:
                    aliases[item.asname or item.name] = statement.module + '.' + item.name
            elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                local = dict(constants)
                if hasattr(statement, 'args'):
                    for arg in (*statement.args.posonlyargs, *statement.args.args,
                                *statement.args.kwonlyargs):
                        local.pop(arg.arg, None)
                block(statement.body, aliases, local)
                continue
            if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                resolved = value(statement.value, aliases, constants)
                targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        constants[target.id] = resolved
            for call in (item for item in ast.walk(statement) if isinstance(item, ast.Call)):
                name = qualified(call.func, aliases)
                is_sink = (name.startswith('subprocess.') and name.rsplit('.', 1)[-1] in {
                    'run', 'Popen', 'call', 'check_call', 'check_output', 'getoutput', 'getstatusoutput'
                }) or (name.startswith('os.') and name.rsplit('.', 1)[-1] in _OS_PROCESS_NAMES)
                if not is_sink:
                    continue
                argument = call.args[0] if call.args else next(
                    (item.value for item in call.keywords if item.arg in {'args', 'executable', 'file'}), None)
                command = value(argument, aliases, constants)
                if isinstance(command, list):
                    command = command[0] if command else None
                if isinstance(command, bytes):
                    command = command.decode('utf-8', 'replace')
                if not isinstance(command, str):
                    continue
                token = next((item for item in sorted(wanted)
                              if process_token_present(command, item)), None)
                if token:
                    hits.append(SubprocessHit(
                        path=relative, line=call.lineno, sink=name, token=token,
                        evidence=(ast.get_source_segment(text, call) or name)[:200]).as_dict())

    block(tree.body, {}, {})
    return hits


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
    # ``from subprocess import run`` / ``from os import execvp`` borrow the sink
    # under a bare name that the fixed ``subprocess.``/``os.exec`` literals miss.
    for match in re.finditer(
        r"from\s+subprocess\s+import\s+([A-Za-z_][A-Za-z0-9_, \t]*)", text
    ):
        for name in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", match.group(1)):
            aliases.append(f"{name}(")
    for match in re.finditer(
        r"from\s+os\s+import\s+([A-Za-z_][A-Za-z0-9_, \t]*)", text
    ):
        for name in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", match.group(1)):
            if name in _OS_PROCESS_NAMES:
                aliases.append(f"{name}(")
    # ``getattr(subprocess, "run")`` / ``getattr(os, "execvp")`` reach the sink
    # through introspection.  Anchor on the getattr call itself.
    for match in re.finditer(r"getattr\s*\(\s*(subprocess|os)\s*,", text):
        aliases.append(f"getattr({match.group(1)}")
    return aliases


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
        relative = path.relative_to(root)
        if any(part in _IGNORED_PARTS for part in relative.parts):
            continue
        suffix = path.suffix.casefold()
        if suffix:
            if suffix not in _SOURCE_SUFFIXES:
                continue
        else:
            # Extensionless helper scripts (``omg_bridge``) are a common way to
            # hide a bridge from a suffix allowlist.  Skip dotfiles and large
            # payloads, but scan real helper files.
            if any(part.startswith(".") for part in relative.parts):
                continue
            if path.stat().st_size > _MAX_EXTENSIONLESS_BYTES:
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
    hits: list[dict[str, Any]] = []
    for path in _candidate_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if path.suffix.casefold() == '.py':
            hits.extend(_python_constant_process_hits(text, path.relative_to(root).as_posix(), wanted))
        clean = _strip_comments(text)
        lowered = clean.casefold()
        sinks = _SINKS
        if path.suffix.casefold() in _SHELL_SUFFIXES:
            sinks = (*_SINKS, *_SHELL_SINKS)
        sink_list = (*sinks, *_alias_sinks(clean))
        for sink in sink_list:
            start = lowered.find(sink.casefold())
            while start >= 0:
                end = min(len(clean), start + _WINDOW)
                window = clean[start:end]
                collapsed = _collapsed(window)
                joined = _joined_literals(window)
                packed = _packed_literals(window)
                decoded = _decode_escapes(window)
                payloads = _decoded_payloads(window)
                matched = next(
                    (
                        token for token in wanted
                        if process_token_present(window, token)
                        or process_token_present(collapsed, token)
                        or process_token_present(joined, token)
                        or token_present(packed, token)
                        or process_token_present(decoded, token)
                        or token_present(payloads, token)
                    ),
                    None,
                )
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
        # Constant propagation across the whole file: a process sink and a
        # source-runtime command assembled from concatenated literals (for
        # example ``"no" + "de"``) may live in different functions, outside any
        # fixed window.  A file that launches a process and also rebuilds a
        # runtime token from fragments is still delegating to the source
        # runtime.  Only *reconstructed* fragments count here, so a file that
        # merely mentions ``node`` in a literal is not flagged.
        packed_file = _packed_literals(clean)
        if packed_file and any(
            sink.casefold() in lowered for sink in sink_list
        ):
            reconstructed = next(
                (token for token in wanted if token_present(packed_file, token)),
                None,
            )
            if reconstructed is not None:
                hits.append(
                    SubprocessHit(
                        path=path.relative_to(root).as_posix(),
                        line=1,
                        sink="<file-constant>",
                        token=reconstructed,
                        evidence=packed_file[:200],
                    ).as_dict()
                )
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
