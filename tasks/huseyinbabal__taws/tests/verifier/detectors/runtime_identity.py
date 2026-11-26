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
    return migration if isinstance(migration, Mapping) else {}


def _source(config: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the source technology declaration.

    Real Case configurations carry the source/target technologies at the top
    level (``source_technology`` / ``target_technology``); the legacy
    ``migration.source`` nesting is still honoured when present.  Reading only
    ``migration.source`` left the source-runtime vocabulary empty on every real
    case, which silently disabled the fallback detectors.
    """

    source = _migration(config).get("source")
    if isinstance(source, Mapping):
        return source
    top = config.get("source_technology")
    return top if isinstance(top, Mapping) else {}


def source_language(config: Mapping[str, Any]) -> str:
    return str(_source(config).get("language_name") or "").strip().casefold()


def source_name(config: Mapping[str, Any]) -> str:
    name = str(_source(config).get("name") or "").strip().casefold()
    return name if name not in _GENERIC_SOURCE_NAMES else ""


def source_program_name(config: Mapping[str, Any]) -> str:
    """Return the source project's executable/program name when it is derivable.

    The compiler front-end (``cargo``/``rustc``) is not the only way a migrated
    program can re-execute the *source* implementation: the source project is
    also published as a built binary under the project name (here ``taws``), and
    a deceptive fallback re-spawns that binary under its own or a renamed
    spelling (``taws-rs``, ``taws_rs``, ``rust-taws`` ...).  Hard-coding only
    ``cargo``/``rustc`` therefore misses the whole renamed-binary family.

    The name is derived, in order, from an explicit source program/package name
    field, a non-generic ``source.name``, or the repository tail (the project
    name).  Generic language names (``Rust``/``Go`` ...) are never program names.
    """

    source = _source(config)
    for key in ("program_name", "package_name", "binary_name", "crate_name"):
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().casefold()
    name = str(source.get("name") or "").strip().casefold()
    if name and name not in _GENERIC_SOURCE_NAMES:
        return name
    for key in ("repository_url", "repository", "source_repository"):
        value = config.get(key)
        if not isinstance(value, str):
            continue
        tail = value.rstrip("/").rsplit("/", 1)[-1]
        if tail.endswith(".git"):
            tail = tail[:-4]
        tail = tail.strip().casefold()
        if tail and tail not in _GENERIC_SOURCE_NAMES:
            return tail
    return ""


def _target_program_names(config: Mapping[str, Any]) -> set[str]:
    """Declared target executable/program names, when the contract carries any.

    Real migration contracts usually keep the executable name implicit (the
    target program is named after the source project), so this set is often
    empty; ambiguity therefore cannot be decided from this set alone.
    """

    names: set[str] = set()
    target = _migration(config).get("target")
    if isinstance(target, Mapping):
        for key in (
            "program_name",
            "package_name",
            "binary_name",
            "crate_name",
            "executable",
        ):
            value = target.get(key)
            if isinstance(value, str) and value.strip():
                names.add(value.strip().casefold())
    technology = config.get("target_technology")
    if isinstance(technology, Mapping):
        for key in ("program_name", "binary_name", "executable"):
            value = technology.get(key)
            if isinstance(value, str) and value.strip():
                names.add(value.strip().casefold())
    return names


def ambiguous_program_tokens(config: Mapping[str, Any]) -> set[str]:
    """Source-program tokens whose spelling is shared with the target program.

    Migration contracts keep the project/executable name: the migrated program
    is named after the source project (here ``taws``), so a source-program token
    derived from the repository/project tail is *also* the target executable's
    name.  Such a token legitimately appears as a command *word* -- a shell
    completion context (``COMP_WORDS=(taws ...)``), an argument payload, a test
    fixture -- without any delegation to the source runtime.  Detectors must not
    treat the token's mere presence in a sink window as a fallback; they must
    require it to be the sink's derived *program argument* instead.

    A program name supplied by an explicit source field (``program_name`` /
    ``binary_name`` ...) is unambiguous unless the contract also declares it as
    a target program name, and compiler/runtime tokens (``cargo`` / ``rustc``)
    are never ambiguous.
    """

    program = source_program_name(config)
    if not program:
        return set()
    if program in _LANGUAGE_TOKENS.get(source_language(config), set()):
        return set()
    if program in _target_program_names(config):
        return {program}
    source = _source(config)
    explicit = any(
        isinstance(source.get(key), str) and source.get(key).strip()
        for key in ("program_name", "package_name", "binary_name", "crate_name")
    )
    if explicit:
        return set()
    name = str(source.get("name") or "").strip().casefold()
    if name and name not in _GENERIC_SOURCE_NAMES:
        return set()
    # Derived from the repository tail: that is the project name the migration
    # preserves as the target executable name, so the spelling is ambiguous.
    return {program}


def process_runtime_tokens(config: Mapping[str, Any]) -> set[str]:
    """Return command/runtime tokens used by the subprocess detector.

    The vocabulary is derived, not hard-coded: the source language's compiler
    tokens plus the source project's program name (so a renamed source binary is
    still recognised as the source runtime rather than as a benign external
    tool).
    """

    tokens = set(_LANGUAGE_TOKENS.get(source_language(config), set()))
    name = source_name(config)
    if name:
        tokens.add(name)
    program = source_program_name(config)
    if program:
        tokens.add(program)
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
    "ambiguous_program_tokens",
    "native_runtime_signals",
    "process_runtime_tokens",
    "process_token_present",
    "source_language",
    "source_name",
    "source_program_name",
    "strip_detector_comments",
    "token_present",
]
