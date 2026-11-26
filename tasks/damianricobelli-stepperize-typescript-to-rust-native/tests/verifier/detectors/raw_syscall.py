"""Detect raw process-launch primitives that bypass symbol-level delegation sinks.

The ``subprocess`` / ``ffi`` / ``dynamic`` detectors all key on a **symbol**
(``std::process::Command``, ``dlopen``, ``child_process`` ...) and, for the
process family, also require a source-runtime **token** in the same window.  A
candidate can delegate to the source runtime without ever naming such a symbol:
write the program name into a byte array and issue the ``execve`` system call
directly, either with inline assembly (``core::arch::asm!``) or through
``libc::syscall``.  No sink string and no command token appear, so the textual
rules and the F2P fallback probe both miss it (observed: the ``syscall_bridge``
adversarial sample scored ``reward=1.0``).

This detector closes that blind spot at the **primitive** level: it flags a raw
process-launch primitive irrespective of how the command name is spelled, so the
whole family is covered rather than one spelling:

* inline-assembly system calls -- ``asm!`` / ``global_asm!`` / ``naked_asm!``
  whose body contains a ``syscall`` / ``svc`` / ``ecall`` / ``sysenter`` /
  ``int 0x80`` instruction (any architecture, any register encoding, any
  syscall number -- decimal, hex or a named constant);
* the ``libc::syscall`` / ``libc::syscall64`` raw syscall entry points, and any
  bare ``syscall(...)`` call (for example through an ``extern "C"`` binding);
* exec-family symbols -- ``execve`` / ``execveat`` / ``execvp`` / ``execvpe`` /
  ``execv`` / ``execl`` / ``execlp`` / ``execle`` / ``posix_spawn`` /
  ``posix_spawnp`` and the ``SYS_execve`` / ``__NR_execve`` constants;
* replacing the process image through ``std::os::unix::process::CommandExt``
  (``... .exec()``).

None of these primitives has a legitimate use in the migrated data-model
library: the instruction requires production Rust to implement the
responsibilities itself and forbids delegation through subprocesses, FFI, RPC or
dynamic loading.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .runtime_identity import strip_detector_comments
from .subprocess import _balanced_arguments, _candidate_files, _line_of


# ``asm!`` and its friends are macros: ``asm!(...)``, ``global_asm! {...}`` or
# ``core::arch::asm!``.  Matching the macro head lets us inspect only the
# assembly body, so a ``syscall`` mention in a comment or a doc string elsewhere
# in the file is not mistaken for a raw syscall.
_ASM_MACRO_RE = re.compile(
    r"(?<![\w])(?:[A-Za-z_][\w]*\s*::\s*)*(?:global_asm|naked_asm|asm)\s*!",
)
_SYSCALL_INSTRUCTION_RE = re.compile(
    r"\b(?:syscall|sysenter|svc|ecall|ecallw)\b|int\s+0x80",
    re.IGNORECASE,
)
# Exec-family entry points.  Longest spellings precede their prefixes so the
# reported token is precise; word boundaries prevent ``executable`` from
# matching ``exec``.
_EXEC_SYMBOL_RE = re.compile(
    r"\b(?:execveat|execve|execvpe|execvp|execv|execlp|execle|execl|"
    r"posix_spawnp|posix_spawn|SYS_execveat|SYS_execve|__NR_execveat|__NR_execve)\b"
)
_LIBC_SYSCALL_RE = re.compile(r"(?<![\w])(?:libc\s*::\s*)?syscall(?:64)?\s*\(")
_COMMAND_EXT_RE = re.compile(r"\bCommandExt\b")
_PROCESS_EXEC_RE = re.compile(r"\.\s*exec\s*\(")

_WINDOW = 400


@dataclass(frozen=True)
class RawProcessHit:
    path: str
    line: int
    sink: str
    token: str
    evidence: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "detector": "raw_process_launch",
            "capability": "runtime",
            "path": self.path,
            "line": self.line,
            "sink": self.sink,
            "token": self.token,
            "evidence": self.evidence,
        }


def _asm_syscall_hits(clean: str) -> list[tuple[int, str, str]]:
    """Return ``(offset, sink, evidence)`` for inline-asm system calls."""

    hits: list[tuple[int, str, str]] = []
    for match in _ASM_MACRO_RE.finditer(clean):
        # The macro head is followed by the delimiter that opens its body.
        remainder = clean[match.end():]
        opening = re.search(r"[\(\{]", remainder)
        if opening is None:
            continue
        open_index = match.end() + opening.start()
        arguments = _balanced_arguments(clean, open_index)
        # Assembly text is a Rust string literal, so a newline/tab between two
        # instructions is the two-character escape ``\n``/``\t`` rather than
        # real whitespace.  Turn those escapes into spaces before matching so
        # ``"mov rax, 59\nsyscall"`` is recognised as the same primitive as a
        # body written across real lines.
        normalized = re.sub(r"\\[nrt]", " ", arguments)
        instruction = _SYSCALL_INSTRUCTION_RE.search(normalized)
        if instruction is not None:
            hits.append((match.start(), "inline-asm-syscall", instruction.group(0)))
    return hits


def detect_raw_process_launch(
    root: Path,
    config: Mapping[str, Any],
    *,
    tokens: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    del config, tokens  # primitive-level scan: no token/config vocabulary needed
    hits: list[dict[str, Any]] = []
    for path in _candidate_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        clean = strip_detector_comments(text, path.suffix)
        relative = path.relative_to(root).as_posix()

        for offset, sink, token in _asm_syscall_hits(clean):
            hits.append(
                RawProcessHit(
                    path=relative,
                    line=_line_of(clean, offset),
                    sink=sink,
                    token=token,
                    evidence=clean[offset:offset + _WINDOW][:200],
                ).as_dict()
            )

        for match in _LIBC_SYSCALL_RE.finditer(clean):
            hits.append(
                RawProcessHit(
                    path=relative,
                    line=_line_of(clean, match.start()),
                    sink="raw-syscall-call",
                    token=match.group(0).strip(),
                    evidence=clean[max(0, match.start() - 80):match.start() + _WINDOW][:200],
                ).as_dict()
            )

        for match in _EXEC_SYMBOL_RE.finditer(clean):
            hits.append(
                RawProcessHit(
                    path=relative,
                    line=_line_of(clean, match.start()),
                    sink="exec-family",
                    token=match.group(0),
                    evidence=clean[max(0, match.start() - 120):match.start() + 120][:200],
                ).as_dict()
            )

        if _COMMAND_EXT_RE.search(clean) and _PROCESS_EXEC_RE.search(clean):
            match = _PROCESS_EXEC_RE.search(clean)
            hits.append(
                RawProcessHit(
                    path=relative,
                    line=_line_of(clean, match.start()),
                    sink="command-ext-exec",
                    token="CommandExt::exec",
                    evidence=clean[max(0, match.start() - 120):match.start() + 120][:200],
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


__all__ = ["detect_raw_process_launch"]
