"""Detect a source-runtime fallback carried in a non-source asset.

The process / FFI / dynamic-load detectors all scan only *source-suffixed*
files (``subprocess._candidate_files``).  A deceptive migration can instead
carry the source-runtime invocation in an asset the built program reads at run
time and executes -- an embedded helper (``//go:embed helper.sh``), a script
shipped next to the binary and executed by path, or any file reached from a
path literal.  The source-runtime *token* then never meets a process *sink* in
the same source-file window, so the window-based detectors cannot see it while
the program still delegates to the original implementation.

This detector closes the whole *asset carrier* family, in the same shape as the
source-side detector:

* **reference closure (source -> sink hop).**  Starting from the candidate's
  source files it resolves every ``//go:embed`` target and every path-shaped
  string literal that names a file in the tree, then follows references found
  *inside* those assets too (bounded BFS).  An asset that merely launches a
  second asset (``deploy/run.sh`` -> ``tools/helper.txt``) can therefore no
  longer park the token one hop away from the source.
* **de-obfuscation views.**  Each candidate asset is matched both as comment-
  stripped text and after decoding the encoded literals a shell/script fallback
  actually uses (base64 blobs and ``\\xNN`` escape runs), so a token that only
  exists in encoded form in the asset is still evidence.
* **program-word resolution.**  For shell-style assets the command line is
  tokenised with the *same* shell machinery as the source detector
  (``_shell_commands`` / ``_program_words`` / ``_command_word_token``).  That
  resolves a split spelling (``car"go"``) to its program word and recognises a
  renamed spelling of the source program (``taws-rs``) without matching the
  bare preserved name as an incidental data word.

Only unambiguous compiler/runtime tokens (``cargo`` / ``rustc`` for a Rust
source) are used for the free-text match: the source project's own program name
is normally also the target executable name, so it legitimately appears as a
command *word* in completion scripts and fixtures (the round-11 false positive).
A program word that is a *renamed* spelling of that name (``taws-rs``) is still
delegation; a bare ``taws`` command word in an asset is not distinguishable from
running the migrated binary, so it stays out (see the residual note in
``docs/repair-history-r14.md``).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from .runtime_identity import ambiguous_program_tokens, process_token_present
from .subprocess import (
    _candidate_files,
    _command_word_token,
    _line_of,
    _program_words,
    _shell_commands,
    _shell_words,
    _strip_comments,
    source_runtime_tokens,
)


_IGNORED_PARTS = frozenset({".git", "__pycache__", ".venv", "venv", "node_modules"})

# Shell-style runner assets: a file the migrated program (or a user following the
# project's own instructions) can execute.  Nothing here is legitimate for a
# project that has migrated *away* from the source technology, so a
# compiler/runtime token inside one is fail-closed evidence.
_RUNNER_SUFFIXES = frozenset(
    {
        ".sh",
        ".bash",
        ".zsh",
        ".ksh",
        ".csh",
        ".tcsh",
        ".ash",
        ".dash",
        ".fish",
        ".nu",
        ".ps1",
        ".psm1",
        ".bat",
        ".cmd",
        ".com",
        ".py",
        ".rb",
        ".pl",
        ".php",
        ".awk",
        ".sed",
        ".tcl",
        ".lua",
        ".mk",
    }
)
_RUNNER_BASENAMES = frozenset(
    {
        "makefile",
        "gnumakefile",
        "justfile",
        "rakefile",
        "dockerfile",
        "containerfile",
        "entrypoint.sh",
        "run.sh",
        "build.sh",
    }
)

# Shell-family assets whose command lines are resolved into program words.  A
# ``.py``/``.rb`` asset is matched textually only (its interpreter semantics are
# not shell), which keeps program-word resolution precise.
_SHELL_SUFFIXES = frozenset(
    {
        ".sh",
        ".bash",
        ".zsh",
        ".ksh",
        ".csh",
        ".tcsh",
        ".ash",
        ".dash",
        ".fish",
        ".nu",
        ".bat",
        ".cmd",
        ".com",
        ".mk",
    }
)

_EMBED_RE = re.compile(r"(?m)^[ \t]*//go:embed[ \t]+(?P<body>.+?)[ \t]*$")
_STRING_RE = re.compile(r"""'(?:[^'\\\n]|\\.)*'|"(?:[^"\\\n]|\\.)*"|`[^`]*`""")
_MAX_ASSET_BYTES = 4 * 1024 * 1024

# Encoded literals a script fallback actually uses.  Base64 blobs are matched
# generically (any long base64 alphabet run) and re-decoded, so the token need
# not be spelled in the file at all; ``\xNN`` escape runs cover the other common
# shell/script spelling.
_BASE64_BLOB_RE = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{7,}={0,2}(?![A-Za-z0-9+/=])")
_HEX_ESCAPE_BLOB_RE = re.compile(r"(?:\\x[0-9A-Fa-f]{2}){4,}")

_MAX_REFERENCE_NODES = 256


def _relative_parts(path: Path, root: Path) -> tuple[str, ...] | None:
    try:
        return path.relative_to(root).parts
    except ValueError:
        return None


def _candidate_assets(root: Path) -> list[Path]:
    if not root.is_dir() or root.is_symlink():
        return []
    assets: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        parts = _relative_parts(path, root)
        if parts is None or any(part in _IGNORED_PARTS for part in parts):
            continue
        assets.append(path)
    return assets


def source_asset_baseline(root: Path) -> dict[str, str]:
    """Fingerprint the trusted repository before applying the model patch."""
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in _candidate_assets(root)
    }


def _embed_targets(root: Path, source: Path, text: str) -> list[Path]:
    """Resolve ``//go:embed`` patterns to the files they embed (relative to the package dir)."""

    targets: list[Path] = []
    base = source.parent
    for match in _EMBED_RE.finditer(text):
        for raw in match.group("body").split():
            pattern = raw.strip("\"'`")
            if pattern.startswith("all:"):
                pattern = pattern[4:]
            if not pattern:
                continue
            if any(char in pattern for char in "*?["):
                candidates = sorted(base.glob(pattern))
            else:
                candidate = base / pattern
                if candidate.is_dir():
                    candidates = sorted(candidate.rglob("*"))
                else:
                    candidates = [candidate]
            for item in candidates:
                if item.is_file() and not item.is_symlink():
                    targets.append(item)
    return targets


def _reference_is_file(path: Path) -> bool:
    """An arbitrary literal/command word need not be a valid filesystem name."""

    try:
        return path.is_file() and not path.is_symlink()
    except (OSError, ValueError):
        # For example ENAMETOOLONG (also possible below 300 Unicode characters)
        # or an embedded NUL in a binary reached through a source reference.
        return False


def _literal_targets(root: Path, source: Path, text: str) -> list[Path]:
    """Resolve path-shaped string literals to files that exist in the tree."""

    targets: list[Path] = []
    for match in _STRING_RE.finditer(text):
        raw = match.group(0)
        value = raw[1:-1]
        if not value or len(value) > 300 or "\n" in value or "://" in value:
            continue
        relative = value[2:] if value.startswith("./") else value
        if not relative or relative.startswith("/"):
            continue
        for base in (source.parent, root):
            candidate = base / relative
            if _reference_is_file(candidate):
                targets.append(candidate)
                break
    return targets


def _resolve_relative(root: Path, source: Path, value: str) -> Path | None:
    value = value.strip("\"'`")
    if not value or len(value) > 300 or "://" in value or "\n" in value:
        return None
    if value.startswith("$") or value.startswith("~") or value.startswith("-"):
        return None
    relative = value[2:] if value.startswith("./") else value
    if not relative or relative.startswith("/"):
        return None
    for base in (source.parent, root):
        candidate = base / relative
        if _reference_is_file(candidate):
            return candidate
    return None


def _shell_targets(root: Path, source: Path, text: str) -> list[Path]:
    """Resolve *unquoted* shell command words that name files in the tree.

    ``_literal_targets`` only reads quoted string literals, so a shell asset that
    launches a sibling asset with a bare word (``exec ./tools/helper.txt``) would
    not be followed.  Shell command lines are tokenised with the same machinery
    the source detector uses, which also unwraps ``sh -c '...'`` payloads.
    """

    targets: list[Path] = []
    for command in _shell_commands(text):
        for word in command:
            candidate = _resolve_relative(root, source, word)
            if candidate is not None:
                targets.append(candidate)
    return targets


def _referenced_assets(root: Path) -> dict[Path, str]:
    """Bounded reference closure from source files through the assets they reach.

    The reference graph is followed transitively: a file referenced by a source
    file, and any file *that* file references in turn, all count as reachable by
    the built program, so the token cannot be parked one hop away from the
    source window (``deploy/run.sh`` -> ``tools/helper.txt``).
    """

    referenced: dict[Path, str] = {}
    frontier: list[Path] = []
    for source in _candidate_files(root):
        try:
            text = source.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for kind, target in (
            ("embed", _embed_targets(root, source, text)),
            ("literal", _literal_targets(root, source, text)),
        ):
            for item in target:
                if item not in referenced:
                    referenced[item] = kind
                    frontier.append(item)

    seen: set[Path] = set()
    while frontier and len(seen) < _MAX_REFERENCE_NODES:
        path = frontier.pop(0)
        if path in seen:
            continue
        seen.add(path)
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for kind, target in (
            ("asset-embed", _embed_targets(root, path, text)),
            ("asset-literal", _literal_targets(root, path, text)),
            ("asset-shell", _shell_targets(root, path, text)),
        ):
            for item in target:
                if item not in referenced:
                    referenced[item] = kind
                    frontier.append(item)
    return referenced


def _is_runner(relative_parts: Sequence[str], text: str) -> bool:
    name = relative_parts[-1].casefold() if relative_parts else ""
    if name in _RUNNER_BASENAMES:
        return True
    if Path(name).suffix in _RUNNER_SUFFIXES:
        return True
    first_line = text.split("\n", 1)[0].strip()
    return first_line.startswith("#!")


def _is_shell_asset(relative_parts: Sequence[str], text: str) -> bool:
    name = relative_parts[-1].casefold() if relative_parts else ""
    if name in _RUNNER_BASENAMES:
        return True
    if Path(name).suffix in _SHELL_SUFFIXES:
        return True
    return text.split("\n", 1)[0].strip().startswith("#!")


def _command_offset(text: str, token: str) -> tuple[int, str] | None:
    """First offset where *token* is used as a command, plus the evidence line."""

    if not process_token_present(text, token):
        return None
    pattern = re.compile(
        rf"(?<![A-Za-z0-9_]){re.escape(token.casefold())}(?![A-Za-z0-9_])"
    )
    match = pattern.search(text.casefold())
    if match is None:
        return None
    start = match.start()
    line = text.splitlines()
    index = text.count("\n", 0, start)
    evidence = line[index].strip() if index < len(line) else ""
    return start, evidence


def _decode_blob(value: str) -> str | None:
    """Decode one base64/hex-escape blob to printable text, when it decodes cleanly."""

    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), validate=False)
    except (binascii.Error, ValueError):
        return None
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return decoded if decoded.isprintable() and 0 < len(decoded) <= 4096 else None


def _hex_blob(value: str) -> str | None:
    try:
        decoded = bytes.fromhex(value.replace("\\x", "")).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    return decoded if decoded.isprintable() else None


def _decoded_views(clean: str) -> list[tuple[str, int]]:
    """Return ``(decoded_text, raw_offset)`` for every encoded literal in *clean*."""

    views: list[tuple[str, int]] = []
    for match in _BASE64_BLOB_RE.finditer(clean):
        decoded = _decode_blob(match.group(0))
        if decoded is not None:
            views.append((decoded, match.start()))
    for match in _HEX_ESCAPE_BLOB_RE.finditer(clean):
        decoded = _hex_blob(match.group(0))
        if decoded is not None:
            views.append((decoded, match.start()))
    return views


def _program_hits(
    clean: str,
    strong: set[str],
    ambiguous: set[str],
) -> list[tuple[int, str, str]]:
    """Resolve shell program words and return ``(offset, token, evidence)`` hits.

    A strong (unambiguous compiler/runtime) token is evidence in any program
    position.  An ambiguous program name -- the source project name, which the
    migration preserves for the target executable -- is evidence only as a
    *renamed* program word (``taws-rs``), never as the bare preserved name, which
    is indistinguishable from running the migrated binary (the round-11 false
    positive).
    """

    hits: list[tuple[int, str, str]] = []
    for words in _shell_commands(clean):
        for program in _program_words(words):
            base = program.rsplit("/", 1)[-1].casefold()
            matched = _command_word_token(program, strong)
            if matched is None and ambiguous and base not in ambiguous:
                matched = _command_word_token(program, ambiguous)
            if matched is None:
                continue
            offset = clean.find(program)
            if offset < 0:
                offset = 0
            line = clean.splitlines()
            index = clean.count("\n", 0, offset)
            evidence = line[index].strip() if index < len(line) else program
            hits.append((offset, matched, evidence))
    return hits


def detect_source_runtime_asset_fallback(
    root: Path,
    config: Mapping[str, Any],
    *,
    tokens: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Return L2 evidence for a source-runtime fallback hidden in an asset."""

    wanted = {str(item).casefold() for item in (tokens or source_runtime_tokens(config))}
    wanted.discard("")
    # The source project's program name is normally the preserved target name, so
    # it cannot distinguish a delegation from a completion/fixture command word.
    ambiguous = wanted & ambiguous_program_tokens(config)
    strong = wanted - ambiguous
    strong.discard("")
    if not strong and not ambiguous:
        return []

    referenced = _referenced_assets(root) if strong else {}

    hits: list[dict[str, Any]] = []
    for asset in _candidate_assets(root):
        parts = _relative_parts(asset, root)
        if parts is None:
            continue
        try:
            raw = asset.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if len(raw) > _MAX_ASSET_BYTES:
            raw = raw[:_MAX_ASSET_BYTES]
        runner = _is_runner(parts, raw)
        reference = referenced.get(asset)
        if reference is None and not runner:
            continue
        # An unchanged, unreachable source build recipe is residue, not proof
        # that the migrated program delegates work to Rust. The baseline comes
        # from the grader before patch application, never from candidate data.
        # Referenced/embedded helpers remain scanned even when unchanged, and
        # every new or modified runner keeps the existing fallback checks.
        baseline = config.get("_source_asset_baseline", {})
        relative = asset.relative_to(root).as_posix()
        if reference is None and isinstance(baseline, Mapping):
            expected = baseline.get(relative)
            if expected and hashlib.sha256(asset.read_bytes()).hexdigest() == expected:
                continue
        clean = _strip_comments(raw)

        def emit(offset: int, token: str, evidence: str, kind: str) -> None:
            hits.append(
                {
                    "detector": "asset_fallback",
                    "capability": "runtime",
                    "path": relative,
                    "line": _line_of(raw, offset),
                    "sink": f"asset:{reference or 'runner'}",
                    "token": token,
                    "evidence": evidence[:200],
                    "carrier": kind,
                }
            )

        # Free-text match: the token spelled in the asset, or in an encoded
        # literal the asset decodes back to the token at run time.
        for token in sorted(strong):
            found = _command_offset(clean, token)
            if found is not None:
                emit(found[0], token, found[1], "literal")
            for decoded, offset in _decoded_views(clean):
                found = _command_offset(decoded, token)
                if found is not None:
                    emit(offset, token, found[1], "encoded")

        # Structural match: the token (or a renamed spelling of the source
        # program) as an executed *program word* of a shell-style asset.  This
        # resolves split spellings and renamed source binaries that the
        # word-boundary text match cannot see.
        if _is_shell_asset(parts, raw):
            for offset, token, evidence in _program_hits(clean, strong, ambiguous):
                emit(offset, token, evidence, "program-word")

    seen: set[tuple[str, int, str, str]] = set()
    unique: list[dict[str, Any]] = []
    for hit in hits:
        key = (hit["path"], hit["line"], hit["sink"], hit["token"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(hit)
    return unique


__all__ = ["detect_source_runtime_asset_fallback"]
