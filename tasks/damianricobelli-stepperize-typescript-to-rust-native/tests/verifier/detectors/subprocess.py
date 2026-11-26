"""Detect process sinks that invoke the source language runtime.

This is deliberately narrower than a full taint engine: a hit is L2 only when a
process sink and a source-runtime token are both present in the candidate tree.
The detector still handles the common obfuscations that plain literal rules
miss: aliases, qualified names, whitespace and single-character concatenation.
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
    {".git", "node_modules", ".venv", "venv", "target", "dist", "build", "vendor", ".gomodcache", ".gocache", "__pycache__"}
)
# A delegation sink does not have to live in a ``.rs`` file: ``include!`` can
# pull arbitrary text (``reference.inc``, ``generated.rs.txt`` ...) into a crate
# module.  Filtering candidates purely by extension leaves that file unscanned,
# so the language-neutral detectors also follow the textual include graph.
_INCLUDE_MACRO_RE = re.compile(r'\binclude!\s*[\(\{]')
_MOD_PATH_RE = re.compile(r'#\s*\[\s*path\s*=\s*(?:r#*)?"([^"]+)"')
_STRING_LITERAL_RE = re.compile(r'(?:r#*)?"([^"]*)"')
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


_SCOPED_WHITESPACE = re.compile(r"[ \t]*::[ \t]*")
_CALL_WHITESPACE = re.compile(r"[ \t]*\([ \t]*")
# ``::``, call delimiters and member access can each carry inert whitespace,
# and the obfuscation is free to break the path across lines
# (``use std\n::process\n::Command as C;``).  Matching a whitespace-tolerant
# regex instead of a collapsed literal keeps line numbers valid while still
# recognising the sink.
_SINK_DELIMITER_RE = re.compile(r"(::|\(|\)|\.)")


def _canonical_sinks(text: str) -> str:
    """Collapse inert whitespace so obfuscated sinks still match.

    Retained for callers that only expect spaces/tabs to be folded; the
    detector itself uses :func:`_sink_pattern`, which also tolerates newlines
    so a path split across lines cannot evade the sink scan.
    """

    value = _SCOPED_WHITESPACE.sub("::", text)
    return _CALL_WHITESPACE.sub("(", value)


def _sink_pattern(sink: str) -> re.Pattern[str]:
    """Compile a sink literal into a whitespace-tolerant matcher.

    Every ``::``/``(``/``)``/``.`` separator may be surrounded by arbitrary
    whitespace, including line breaks, so ``std\\n::process\\n::Command`` and
    ``C\\n::new`` are the same sink as the tidiest spelling.
    """

    tokens = [part for part in _SINK_DELIMITER_RE.split(sink) if part]
    return re.compile(r"\s*".join(re.escape(token) for token in tokens), re.IGNORECASE)


def _collapsed(text: str) -> str:
    """Remove quotes, plus signs and whitespace to expose 'g' + 'o' splits."""

    return re.sub(r"[\s'\"+`]", "", text).casefold()


def _joined_literals(text: str) -> str:
    """Concatenate string literals in a window to expose 'g' + 'o' splits."""

    return "".join(re.findall(r"['\"]([^'\"]*)['\"]", text)).casefold()


def _alias_sinks(text: str) -> list[str]:
    aliases: list[str] = []
    for match in re.finditer(
        r"use\s+std\s*::\s*process\s*::\s*Command\s+as\s+([A-Za-z_][A-Za-z0-9_]*)",
        text,
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


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _balanced_arguments(text: str, open_index: int) -> str:
    """Return the text inside the delimiter that starts at ``open_index``."""

    opener = text[open_index]
    closer = {"(": ")", "{": "}", "[": "]"}.get(opener, ")")
    depth = 0
    index = open_index
    while index < len(text):
        character = text[index]
        if character == opener:
            depth += 1
        elif character == closer:
            depth -= 1
            if depth == 0:
                return text[open_index + 1:index]
        index += 1
    return text[open_index + 1:]


def _include_reference(arguments: str) -> str | None:
    """Resolve the textual path an ``include!`` argument names.

    The argument is usually a single string literal, but ``concat!`` (and
    friends) let a crate build the path from several pieces.  Joining the
    literals exposes ``include!(concat!("../gen", ".inc"))`` as ``../gen.inc``,
    so it is followed exactly like a literal ``include!("../gen.inc")``.
    """

    pieces = _STRING_LITERAL_RE.findall(arguments)
    if not pieces:
        return None
    return "".join(pieces)


def _included_sources(path: Path) -> list[Path]:
    """Return files textually pulled into ``path`` by ``include!``/``#[path]``.

    ``include!("reference.inc")`` and ``#[path = "generated/part.rs"]`` let a
    crate embed arbitrary files.  Resolving the references relative to the
    including file keeps the scan bounded to sources the candidate actually
    compiles, instead of opening every file in the repository.
    """

    if path.suffix.casefold() != ".rs":
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    references: list[Path] = []
    for match in _INCLUDE_MACRO_RE.finditer(text):
        reference = _include_reference(_balanced_arguments(text, match.end() - 1))
        if reference:
            references.append(Path(reference))
    for match in _MOD_PATH_RE.finditer(text):
        references.append(Path(match.group(1)))
    return references


def _candidate_files(root: Path) -> list[Path]:
    if not root.is_dir() or root.is_symlink():
        return []
    try:
        resolved_root = root.resolve()
    except OSError:
        resolved_root = root
    files: list[Path] = []
    seen: set[Path] = set()

    def _accept(path: Path, *, from_include: bool) -> bool:
        if path.is_symlink() or not path.is_file():
            return False
        try:
            relative = path.relative_to(root)
        except ValueError:
            return False
        if any(part in _IGNORED_PARTS for part in relative.parts):
            return False
        if not from_include and path.suffix.casefold() not in _SOURCE_SUFFIXES:
            return False
        try:
            real = path.resolve()
        except OSError:
            return False
        if from_include and not real.is_relative_to(resolved_root):
            # An include may name a path outside the tree; never follow it.
            return False
        # Deduplicate by real path so a file reachable both by directory walk
        # and by ``include!("../evil.rs")`` is reported once.
        if real in seen:
            return False
        seen.add(real)
        return True

    queue: list[Path] = []
    for path in sorted(root.rglob("*")):
        if _accept(path, from_include=False):
            files.append(path)
            queue.append(path)
    # Breadth-first over the Rust textual include graph so a sink hidden in a
    # non-source-suffix file reachable from a crate module is still scanned.
    index = 0
    while index < len(queue):
        current = queue[index]
        index += 1
        for reference in _included_sources(current):
            candidate = current.parent / reference
            if _accept(candidate, from_include=True):
                files.append(candidate)
                queue.append(candidate)
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
        clean = _strip_comments(text)
        # Scan with whitespace-tolerant patterns so spacings such as
        # ``std :: process :: Command`` and line-broken paths such as
        # ``use std\n::process\n::Command`` are recognised as the same sink.
        patterns = [(sink, _sink_pattern(sink)) for sink in _SINKS]
        patterns.extend(
            (alias, _sink_pattern(alias)) for alias in _alias_sinks(clean)
        )
        for sink, pattern in patterns:
            for match in pattern.finditer(clean):
                start = match.start()
                end = min(len(clean), start + _WINDOW)
                window = clean[start:end]
                collapsed = _collapsed(window)
                joined = _joined_literals(window)
                matched = next(
                    (
                        token for token in wanted
                        if process_token_present(window, token)
                        or process_token_present(collapsed, token)
                        or process_token_present(joined, token)
                    ),
                    None,
                )
                if matched is not None:
                    hits.append(
                        SubprocessHit(
                            path=path.relative_to(root).as_posix(),
                            line=_line_of(clean, start),
                            sink=sink,
                            token=matched,
                            evidence=window[:200],
                        ).as_dict()
                    )
                    break
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
