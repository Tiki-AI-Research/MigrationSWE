"""Language-neutral evaluation of declarative residue rules."""

from __future__ import annotations

import base64
import binascii
import json
import re
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Iterable, Mapping, Sequence


# Directory pruning for the residue scan.  Only VCS metadata and dependency /
# interpreter caches are skipped: a deceptive fallback must not become
# invisible just because the attacker placed it under ``build/``, ``dist/``,
# ``target/`` or ``coverage/`` (those are *source-bearing* directories in a
# candidate workspace and are routinely written by hand).
_EXCLUDED_PARTS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    ".gomodcache",
    ".gocache",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
}
_RULE_KINDS = {"literal", "dependency", "import", "file", "runtime"}


def _safe_relative(value: str) -> bool:
    path = Path(value)
    return bool(value) and not path.is_absolute() and ".." not in path.parts and "\\" not in value


def _eligible(root: Path, path: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    return (
        path.is_file()
        and not path.is_symlink()
        and not any(part in _EXCLUDED_PARTS for part in relative.parts)
    )


def _paths(root: Path, patterns: Sequence[str]) -> Iterable[Path]:
    if not patterns:
        return ()
    collected: list[Path] = []
    for pattern in patterns:
        if not _safe_relative(pattern):
            continue
        path = root / pattern
        if any(token in pattern for token in "*?["):
            collected.extend(root.glob(pattern))
        elif path.is_dir() and not path.is_symlink():
            collected.extend(path.rglob("*"))
        else:
            collected.append(path)
    candidates = collected
    unique = {path.resolve(): path for path in candidates if _eligible(root, path)}
    return (unique[key] for key in sorted(unique, key=lambda item: item.as_posix()))


def _target_matches(value: str, target: str) -> bool:
    actual = value.casefold().strip()
    expected = target.casefold().strip()
    return actual == expected or fnmatchcase(actual, expected)


def _without_comments(text: str) -> str:
    """Blank comments so residue rules do not match prose.

    A migrated file often mentions the old framework in a comment ("no longer
    uses Vue"); that is not residual usage.  Only comment syntax is stripped --
    string literals are left intact because imports/dynamic loads are strings.
    """

    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"(?m)//.*$", " ", text)
    text = re.sub(r"(?m)#.*$", " ", text)
    return text


def _literal_contains(path: Path, target: str) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return target.casefold() in _without_comments(text).casefold()


# --- target-language constant folding -------------------------------------
#
# A literal scan alone misses a deceptive source fallback whose executable
# name or payload is assembled at run time (``"car"+"go"``,
# ``strings.Join([]string{"car","go"}, "")``) and then forwarded indirectly
# through local variables.  The target technology for language migrations is
# the compiled language under test, so fold constant string expressions in
# source files and re-check the declarative literal rules against the resolved
# values.  This stays additive: literal/dependency/import rules keep their
# original behaviour and the folded pass only raises a hit.

_TWO_CHAR_OPERATORS = (
    ":=", "==", "!=", "<=", ">=", "&&", "||", "->", "<-", "+=", "-=", "*=",
    "/=", "%=", "&=", "|=", "^=", "<<", ">>", "...",
)

_GO_ESCAPES = {
    "n": "\n", "t": "\t", "r": "\r", "a": "\a", "b": "\b", "f": "\f",
    "v": "\v", "\\": "\\", '"': '"', "'": "'", "`": "`", "0": "\0",
}


def _unescape_go(character: str) -> str:
    return _GO_ESCAPES.get(character, character)


def _go_tokens(text: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    index = 0
    length = len(text)
    while index < length:
        character = text[index]
        if character in " \t\r":
            index += 1
            continue
        if character == "\n":
            tokens.append(("nl", "\n"))
            index += 1
            continue
        if character == "/" and index + 1 < length and text[index + 1] == "/":
            while index < length and text[index] != "\n":
                index += 1
            continue
        if character == "/" and index + 1 < length and text[index + 1] == "*":
            index += 2
            while index + 1 < length and not (text[index] == "*" and text[index + 1] == "/"):
                index += 1
            index += 2
            continue
        if character == '"':
            cursor = index + 1
            value: list[str] = []
            while cursor < length:
                if text[cursor] == "\\" and cursor + 1 < length:
                    value.append(_unescape_go(text[cursor + 1]))
                    cursor += 2
                    continue
                if text[cursor] == '"':
                    break
                value.append(text[cursor])
                cursor += 1
            tokens.append(("str", "".join(value)))
            index = cursor + 1
            continue
        if character == "`":
            cursor = text.find("`", index + 1)
            if cursor == -1:
                cursor = length
            tokens.append(("str", text[index + 1:cursor]))
            index = cursor + 1
            continue
        if character == "'":
            cursor = index + 1
            value = []
            while cursor < length:
                if text[cursor] == "\\" and cursor + 1 < length:
                    value.append(_unescape_go(text[cursor + 1]))
                    cursor += 2
                    continue
                if text[cursor] == "'":
                    break
                value.append(text[cursor])
                cursor += 1
            tokens.append(("str", "".join(value)))
            index = cursor + 1
            continue
        if character.isalpha() or character == "_":
            cursor = index
            while cursor < length and (text[cursor].isalnum() or text[cursor] == "_"):
                cursor += 1
            tokens.append(("ident", text[index:cursor]))
            index = cursor
            continue
        if character.isdigit():
            cursor = index
            while cursor < length and (text[cursor].isalnum() or text[cursor] in "._"):
                cursor += 1
            tokens.append(("num", text[index:cursor]))
            index = cursor
            continue
        pair = text[index:index + 2]
        if pair in _TWO_CHAR_OPERATORS:
            tokens.append(("op", pair))
            index += 2
            continue
        tokens.append(("op", character))
        index += 1
    return tokens


def _go_slice_value(tokens, index, environment):
    # ``index`` points at the ``{`` opening a ``[]string{...}`` composite.
    items: list[object] = []
    while index < len(tokens) and tokens[index] != ("op", "}"):
        value, index = _go_expression(tokens, index, environment)
        items.append(value)
        if index < len(tokens) and tokens[index] == ("op", ","):
            index += 1
            continue
        break
    if index < len(tokens) and tokens[index] == ("op", "}"):
        index += 1
    return ("slice", items), index


def _go_skip_to(tokens, index, closer):
    while index < len(tokens) and tokens[index] != ("op", closer):
        index += 1
    if index < len(tokens):
        index += 1
    return index


def _go_composite_value(tokens, index, environment):
    # ``index`` points at the ``[`` of a slice/array composite literal such as
    # ``[]string{...}``, ``[4]uint8{...}``, ``[...]byte{...}``.
    cursor = index + 1
    if cursor < len(tokens) and (
        tokens[cursor][0] in {"num", "ident"} or tokens[cursor] == ("op", "...")
    ):
        cursor += 1
    if cursor < len(tokens) and tokens[cursor] == ("op", "]"):
        cursor += 1
    else:
        return None, index + 1
    if (
        cursor < len(tokens)
        and tokens[cursor][0] == "ident"
        and cursor + 1 < len(tokens)
        and tokens[cursor + 1] == ("op", "{")
    ):
        return _go_slice_value(tokens, cursor + 2, environment)
    return None, cursor


def _collect_strings(value: object, sink: list[str]) -> None:
    """Record every string a folded value can denote (scalars + slice items)."""

    if isinstance(value, str):
        if value:
            sink.append(value)
        return
    if isinstance(value, tuple) and len(value) == 2 and value[0] == "slice":
        for item in value[1]:
            _collect_strings(item, sink)


def _is_opaque(value: object) -> bool:
    return isinstance(value, tuple) and len(value) == 2 and value[0] == "opaque"


# --- runtime payload resolution -------------------------------------------
#
# The literal pass cannot see a payload that is only materialised at run time.
# Two families matter for a deceptive source fallback:
#
#   * a payload carried as an *encoded literal* (base64/hex/URL escaping) that
#     is decoded before it reaches a process sink -- resolve the literal, so the
#     decoded command text is matched by the ordinary rules;
#   * a payload that is genuinely opaque (embed/read/env) -- remember it as an
#     opaque value so the source -> sink analysis can flag it.
#
# ``string -> opaque`` propagation is deliberately limited to *payload*
# operations; extracting a struct field (``cfg.EnginePath``) is not a payload
# chain and must not be treated as one.
_GO_DECODE_CALLS = {
    "base64.StdEncoding.DecodeString",
    "base64.URLEncoding.DecodeString",
    "base64.RawStdEncoding.DecodeString",
    "base64.RawURLEncoding.DecodeString",
    "base64.StdEncoding.Decode",
    "base64.URLEncoding.Decode",
    "hex.DecodeString",
    "hex.Decode",
    "strconv.Unquote",
    "url.QueryUnescape",
    "url.PathUnescape",
}
_GO_OPAQUE_READ_CALLS = {
    "os.ReadFile",
    "ioutil.ReadFile",
    "os.Open",
    "os.OpenFile",
    "io.ReadAll",
    "io.ReadFull",
    "io.ReadAtLeast",
    "os.Getenv",
    "os.LookupEnv",
    "os.Create",
    "ioutil.ReadAll",
}
_GO_OPAQUE_METHODS = {
    "DecodeString",
    "Decode",
    "Unquote",
    "QueryUnescape",
    "PathUnescape",
    "ReadFile",
    "ReadAll",
    "ReadFull",
    "ReadAtLeast",
    "ReadString",
    "ReadBytes",
    "ReadDir",
    "Getenv",
    "LookupEnv",
}
_GO_OPAQUE_CALLS = _GO_DECODE_CALLS | _GO_OPAQUE_READ_CALLS

# Environment-injected values (``os.Getenv`` / ``os.LookupEnv``) are the classic
# out-of-band channel for a deceptive fallback: the migrated code reads the name
# of the runtime to delegate to from the process environment (``CHESS_TUI_ENGINE``)
# and executes it.  Unlike a configuration-file read they are *not* part of the
# task's declared configuration surface, so a value derived from them keeps its
# payload provenance even when it is temporarily parked in a struct field.
_GO_ENV_CALLS = {"os.Getenv", "os.LookupEnv"}
_GO_ENV_METHODS = {"Getenv", "LookupEnv"}

# Method spellings of the decode calls: a decoded payload keeps its provenance
# through a struct field just like an environment-injected one.  File-read
# methods (``ReadFile`` / ``ReadAll`` …) are deliberately *not* here.
_GO_FIELD_METHODS = _GO_ENV_METHODS | {
    "DecodeString",
    "Decode",
    "Unquote",
    "QueryUnescape",
    "PathUnescape",
}

# Integer/rune conversions (``rune(99)``, ``byte(0x20)``) that a constant program
# name may be built from when the literal characters are spelled numerically.
_GO_INT_CONVERSIONS = {
    "rune", "byte", "int", "int8", "int16", "int32", "int64",
    "uint", "uint8", "uint16", "uint32", "uint64", "uintptr",
}

# Process / plugin / dynamic-load sinks and the position of the *program*
# argument that names what will be executed.
_GO_PROCESS_SINKS = {
    "exec.Command": 0,
    "exec.CommandContext": 1,
    "os.StartProcess": 0,
    "syscall.Exec": 0,
    "syscall.ForkExec": 0,
    "plugin.Open": 0,
}
_GO_WRITE_CALLS = {"os.WriteFile", "ioutil.WriteFile", "os.WriteFile"}


# --- alias resolution -----------------------------------------------------
#
# A sink does not have to be spelled with its canonical package qualifier: Go
# lets a file rename the package (``import ex "os/exec"`` -> ``ex.Command``) or
# dot-import it (``import . "os/exec"`` -> ``Command``).  Both forms reach the
# same process/plugin sink, so resolve the local identifier back to the canonical
# package segment before any downstream analysis.  Only identifiers that are
# actually imported in the file are rewritten, so legitimate selector use of a
# same-named variable is untouched.

_GO_IMPORT_BLOCK = re.compile(r"\bimport\s*\((.*?)\)", re.S)
_GO_IMPORT_SPEC = re.compile(r'(?:([A-Za-z_.]\w*|\.|_)\s+)?"([^"]+)"')


def _go_import_aliases(text: str) -> tuple[dict[str, str], set[str]]:
    """Return ``(local_name -> canonical_pkg, dot_imported_pkgs)`` for a Go file."""

    cleaned = _without_comments(text)
    specs: list[tuple[str | None, str]] = []
    for block in _GO_IMPORT_BLOCK.finditer(cleaned):
        for match in _GO_IMPORT_SPEC.finditer(block.group(1)):
            specs.append((match.group(1), match.group(2)))
    for match in re.finditer(
        r'\bimport\s+(?:([A-Za-z_.]\w*|\.|_)\s+)?"([^"]+)"', cleaned
    ):
        specs.append((match.group(1), match.group(2)))
    mapping: dict[str, str] = {}
    dots: set[str] = set()
    for alias, path in specs:
        package = path.rsplit("/", 1)[-1]
        if not package:
            continue
        if alias == "_":
            continue
        if alias == ".":
            dots.add(package)
            continue
        mapping[alias or package] = package
    return mapping, dots


_GO_DOT_EXPORTS: dict[str, tuple[str, str]] = {}


def _go_dot_export(name: str) -> tuple[str, str] | None:
    """Map a dot-imported bare call head to ``(pkg, dotted_rest)``."""

    if not _GO_DOT_EXPORTS:
        ordered = (
            sorted(_GO_PROCESS_SINKS)
            + sorted(_GO_DECODE_CALLS)
            + sorted(_GO_OPAQUE_READ_CALLS)
        )
        for qualified in ordered:
            package, _, rest = qualified.partition(".")
            if not rest:
                continue
            head = rest.split(".")[0]
            _GO_DOT_EXPORTS.setdefault(head, (package, rest))
    return _GO_DOT_EXPORTS.get(name)


def _go_rewrite_aliases(tokens, mapping: dict[str, str], dots: set[str]):
    """Rewrite aliased / dot-imported package heads to their canonical names."""

    if not mapping and not dots:
        return tokens
    rewritten: list[tuple[str, str]] = []
    length = len(tokens)
    for index, token in enumerate(tokens):
        if token[0] == "ident":
            following = tokens[index + 1] if index + 1 < length else None
            if token[1] in mapping and following == ("op", "."):
                rewritten.append(("ident", mapping[token[1]]))
                continue
            if dots:
                export = _go_dot_export(token[1])
                if export is not None and export[0] in dots and following in {
                    ("op", "."),
                    ("op", "("),
                }:
                    rewritten.append(("ident", export[0]))
                    for part in export[1].split("."):
                        rewritten.append(("op", "."))
                        rewritten.append(("ident", part))
                    continue
        rewritten.append(token)
    return rewritten


def _go_dotted_name(tokens, index):
    """Return ``(dotted_name, next_index)`` for ``ident(.ident)*`` at ``index``."""

    if index >= len(tokens) or tokens[index][0] != "ident":
        return None, index
    cursor = index
    parts: list[str] = []
    while cursor < len(tokens) and tokens[cursor][0] == "ident":
        parts.append(tokens[cursor][1])
        cursor += 1
        if cursor < len(tokens) and tokens[cursor] == ("op", "."):
            cursor += 1
            continue
        break
    return ".".join(parts), cursor


def _go_dotted_call(tokens, index):
    """Return ``(dotted_name, paren_index)`` for ``ident(.ident)*(`` at ``index``."""

    name, cursor = _go_dotted_name(tokens, index)
    if name is not None and cursor < len(tokens) and tokens[cursor] == ("op", "("):
        return name, cursor
    return None, index


_GO_KEYWORDS = frozenset(
    {
        "if", "for", "switch", "select", "else", "struct", "interface", "func",
        "map", "case", "default", "go", "defer", "return", "range", "var",
        "const", "type", "chan", "package", "import",
    }
)


def _go_struct_value(tokens, index, environment):
    # ``index`` points at the ``{`` of a ``Type{field: value, …}`` literal.
    cursor = index + 1
    fields: dict[str, object] = {}
    while cursor < len(tokens) and tokens[cursor] != ("op", "}"):
        if (
            tokens[cursor][0] == "ident"
            and cursor + 1 < len(tokens)
            and tokens[cursor + 1] == ("op", ":")
        ):
            key = tokens[cursor][1]
            value, cursor = _go_expression(tokens, cursor + 2, environment)
            fields[key] = value
            if cursor < len(tokens) and tokens[cursor] == ("op", ","):
                cursor += 1
                continue
            break
        break
    if cursor < len(tokens) and tokens[cursor] == ("op", "}"):
        cursor += 1
    return ("struct", fields), cursor


def _go_call_sites(tokens) -> Iterable[tuple[str, int]]:
    for position in range(len(tokens)):
        name, paren = _go_dotted_call(tokens, position)
        if name is not None:
            yield name, paren


def _go_callee_start(tokens, paren):
    """Start index of the callee primary expression ending at ``paren`` (a ``(``).

    Unlike :func:`_go_dotted_call` this accepts an *indexed* / chained callee
    (``fns[0](p)``), whose root name would otherwise never be seen as a call
    site: the callee is peeled back through ``[…]`` and ``(…)`` groups and
    dotted selectors to the root identifier.
    """

    cursor = paren - 1
    if cursor < 0:
        return None
    while cursor >= 0:
        token = tokens[cursor]
        if token[0] == "ident":
            if token[1] in _GO_KEYWORDS:
                return None
            if cursor - 1 >= 0 and tokens[cursor - 1] == ("op", "."):
                cursor -= 2
                continue
            return cursor
        if token == ("op", "]"):
            depth = 1
            cursor -= 1
            while cursor >= 0 and depth > 0:
                if tokens[cursor] == ("op", "]"):
                    depth += 1
                elif tokens[cursor] == ("op", "["):
                    depth -= 1
                cursor -= 1
            continue
        if token == ("op", ")"):
            depth = 1
            cursor -= 1
            while cursor >= 0 and depth > 0:
                if tokens[cursor] == ("op", ")"):
                    depth += 1
                elif tokens[cursor] == ("op", "("):
                    depth -= 1
                cursor -= 1
            continue
        return None
    return None


def _go_call_exprs(tokens) -> Iterable[tuple[list, int]]:
    """Yield ``(callee_tokens, paren_index)`` for every call in ``tokens``.

    The callee is the raw token slice of the primary expression before the
    ``(`` -- a name, a selector, an index, or a chain of them -- so a
    function-valued callee can be resolved through the *same* value flow as any
    other expression.
    """

    for paren in range(len(tokens)):
        if tokens[paren] != ("op", "("):
            continue
        start = _go_callee_start(tokens, paren)
        if start is None:
            continue
        yield tokens[start:paren], paren


def _go_sink_sites(tokens, environment) -> list[tuple[str, int]]:
    """Sink call sites, including a sink reached through a local alias.

    ``run := exec.Command; run(prog, …)`` performs the same invocation, so the
    alias is resolved back to the sink it names.
    """

    sites: list[tuple[str, int]] = []
    for position in range(len(tokens)):
        name, paren = _go_dotted_call(tokens, position)
        if name is None:
            continue
        if name in _GO_PROCESS_SINKS:
            sites.append((name, paren))
            continue
        value = environment.get(name)
        if isinstance(value, tuple) and len(value) == 2 and value[0] == "sink":
            sites.append((value[1], paren))
    return sites


def _go_cmd_struct_program(tokens, index, environment):
    """Resolve the program named by an ``exec.Cmd{Path: …}`` literal."""

    cursor = index + 1
    while cursor < len(tokens) and tokens[cursor] != ("op", "}"):
        if (
            tokens[cursor][0] == "ident"
            and tokens[cursor][1] in {"Path", "Args"}
            and cursor + 1 < len(tokens)
            and tokens[cursor + 1] == ("op", ":")
        ):
            value, after = _go_expression(tokens, cursor + 2, environment)
            if tokens[cursor][1] == "Path":
                yield value
            else:
                for item in _literal_values(value):
                    yield item
                    break
            cursor = after
        cursor += 1


def _go_call_arguments(tokens, index):
    """Split the argument list of a call opening at ``index`` (a ``(``)."""

    arguments: list[list[tuple[str, str]]] = []
    cursor = index + 1
    current: list[tuple[str, str]] = []
    depth = 0
    while cursor < len(tokens):
        token = tokens[cursor]
        if token[0] == "op" and token[1] in "([{":
            depth += 1
        elif token[0] == "op" and token[1] in ")]}":
            if depth == 0 and token[1] == ")":
                arguments.append(current)
                return arguments, cursor + 1
            depth -= 1
        elif token == ("op", ",") and depth == 0:
            arguments.append(current)
            current = []
            cursor += 1
            continue
        current.append(token)
        cursor += 1
    arguments.append(current)
    return arguments, cursor


def _go_decode_literal(name: str, value: str) -> str | None:
    candidate = value.encode("utf-8", "ignore")
    try:
        if name.startswith("base64."):
            padding = "=" * (-len(candidate) % 4)
            return base64.b64decode(candidate + padding.encode()).decode("utf-8")
        if name.startswith("hex."):
            return bytes.fromhex(value).decode("utf-8")
        if name == "strconv.Unquote":
            return json.loads(value)
    except (binascii.Error, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return None


def _go_payload_value(name, tokens, index, environment):
    # ``index`` points at the ``(`` of a decoder/read call.
    arguments, cursor = _go_call_arguments(tokens, index)
    if name in _GO_DECODE_CALLS or name.rsplit(".", 1)[-1] in _GO_OPAQUE_METHODS:
        if name in _GO_DECODE_CALLS and arguments:
            first, _ = _go_expression(arguments[0], 0, environment)
            if isinstance(first, str):
                decoded = _go_decode_literal(name, first)
                if decoded is not None:
                    return decoded, cursor
        return ("opaque", name), cursor
    return None, cursor


def _go_append_value(tokens, index, environment):
    arguments, cursor = _go_call_arguments(tokens, index)
    resolved = [_go_expression(argument, 0, environment)[0] for argument in arguments]
    if any(_is_opaque(item) for item in resolved):
        return ("opaque", "append"), cursor
    items: list[object] = []
    for item in resolved:
        if isinstance(item, tuple) and len(item) == 2 and item[0] == "slice":
            items.extend(item[1])
        elif item is not None:
            items.append(item)
    return ("slice", items), cursor


def _go_format_value(format_text: str, arguments: list[object]) -> str | None:
    """Resolve a ``fmt`` verb string so assembled command names still appear."""

    pieces: list[str] = []
    argument_index = 0
    index = 0
    length = len(format_text)
    while index < length:
        character = format_text[index]
        if character != "%":
            pieces.append(character)
            index += 1
            continue
        if index + 1 < length and format_text[index + 1] == "%":
            pieces.append("%")
            index += 2
            continue
        cursor = index + 1
        while cursor < length and format_text[cursor] in "+-# 0123456789.":
            cursor += 1
        if cursor >= length:
            return None
        verb = format_text[cursor]
        index = cursor + 1
        if argument_index >= len(arguments):
            return None
        argument = arguments[argument_index]
        argument_index += 1
        if isinstance(argument, str):
            piece = f'"{argument}"' if verb == "q" else argument
        elif (
            isinstance(argument, tuple)
            and len(argument) == 2
            and argument[0] == "num"
        ):
            number = _go_count(argument)
            if verb == "c" and number is not None and 0 <= number <= 0x10FFFF:
                piece = chr(number)
            elif verb in {"d", "s", "v"}:
                piece = argument[1]
            else:
                piece = None
        else:
            piece = None
        if piece is None:
            return None
        pieces.append(piece)
    return "".join(pieces)


def _go_sprintf_value(tokens, index, environment):
    # ``index`` points at the ``(`` right after a ``fmt.Sprintf``-style callee.
    if index >= len(tokens) or tokens[index] != ("op", "("):
        return None, index
    cursor = index + 1
    format_value, cursor = _go_expression(tokens, cursor, environment)
    arguments: list[object] = []
    while cursor < len(tokens) and tokens[cursor] == ("op", ","):
        argument, cursor = _go_expression(tokens, cursor + 1, environment)
        arguments.append(argument)
    if cursor < len(tokens) and tokens[cursor] == ("op", ")"):
        cursor += 1
    if _is_opaque(format_value) or any(_is_opaque(item) for item in arguments):
        return ("opaque", "sprintf"), cursor
    if not isinstance(format_value, str):
        return None, cursor
    return _go_format_value(format_value, arguments), cursor


def _go_string_conversion(tokens, index, environment):
    # ``index`` points at the ``(`` right after a ``string`` conversion.
    if index >= len(tokens) or tokens[index] != ("op", "("):
        return None, index
    cursor = index + 1
    inner, cursor = _go_expression(tokens, cursor, environment)
    if cursor < len(tokens) and tokens[cursor] == ("op", ")"):
        cursor += 1
    if isinstance(inner, str):
        return inner, cursor
    if _is_opaque(inner):
        return ("opaque", "string"), cursor
    if isinstance(inner, tuple) and len(inner) == 2 and inner[0] == "num":
        number = _go_count(inner)
        if number is None or not 0 <= number <= 0x10FFFF:
            return None, cursor
        return chr(number), cursor
    if isinstance(inner, tuple) and len(inner) == 2 and inner[0] == "slice":
        characters: list[str] = []
        for item in inner[1]:
            if isinstance(item, str) and len(item) == 1:
                characters.append(item)
            elif isinstance(item, tuple) and len(item) == 2 and item[0] == "num":
                try:
                    characters.append(chr(int(item[1])))
                except (ValueError, OverflowError):
                    return None, cursor
            else:
                return None, cursor
        return "".join(characters), cursor
    return None, cursor


def _go_index_value(tokens, index, base, environment):
    # ``index`` points at the ``[`` of an index expression on ``base``.
    cursor = index + 1
    if cursor < len(tokens) and tokens[cursor] == ("op", "]"):
        return None, cursor + 1
    key, cursor = _go_expression(tokens, cursor, environment)
    cursor = _go_skip_to(tokens, cursor, "]")
    if _is_opaque(base):
        return ("opaque", "index"), cursor
    if isinstance(base, tuple) and len(base) == 2 and base[0] == "map":
        folded_key = _map_key(key)
        if folded_key is not None and folded_key in base[1]:
            return base[1][folded_key], cursor
        return None, cursor
    if (
        isinstance(base, tuple)
        and len(base) == 2
        and base[0] == "slice"
        and isinstance(key, tuple)
        and len(key) == 2
        and key[0] == "num"
    ):
        try:
            position = int(key[1])
        except ValueError:
            return None, cursor
        items = base[1]
        if 0 <= position < len(items):
            return items[position], cursor
    return None, cursor


def _is_num(value: object) -> bool:
    return isinstance(value, tuple) and len(value) == 2 and value[0] == "num"


def _map_key(value: object):
    """Hashable canonical key for a constant map-literal key, or ``None``."""

    if isinstance(value, str):
        return value
    if _is_num(value):
        return ("num", value[1])
    return None


def _go_map_value(tokens, index, environment):
    # ``index`` points at the ``{`` of a ``map[K]V{…}`` composite literal.
    cursor = index + 1
    mapping: dict[object, object] = {}
    while cursor < len(tokens) and tokens[cursor] != ("op", "}"):
        key, cursor = _go_expression(tokens, cursor, environment)
        value: object = None
        if cursor < len(tokens) and tokens[cursor] == ("op", ":"):
            value, cursor = _go_expression(tokens, cursor + 1, environment)
        folded_key = _map_key(key)
        if folded_key is not None:
            mapping[folded_key] = value
        if cursor < len(tokens) and tokens[cursor] == ("op", ","):
            cursor += 1
            continue
        break
    if cursor < len(tokens) and tokens[cursor] == ("op", "}"):
        cursor += 1
    return ("map", mapping), cursor


def _go_map_type_value(tokens, index, environment):
    # ``index`` points at the ``map`` keyword of ``map[K]V{…}``.
    cursor = index + 1
    if cursor < len(tokens) and tokens[cursor] == ("op", "["):
        cursor = _go_skip_to(tokens, cursor, "]")
    while cursor < len(tokens) and tokens[cursor] != ("op", "{"):
        if tokens[cursor][0] not in {"ident", "op"} or tokens[cursor] == ("op", "("):
            return None, cursor
        cursor += 1
    if cursor >= len(tokens):
        return None, cursor
    return _go_map_value(tokens, cursor, environment)


def _go_int_conversion(tokens, index, environment):
    # ``index`` points at the ``(`` after an integer/rune conversion head.
    if index >= len(tokens) or tokens[index] != ("op", "("):
        return None, index
    cursor = index + 1
    inner, cursor = _go_expression(tokens, cursor, environment)
    if cursor < len(tokens) and tokens[cursor] == ("op", ")"):
        cursor += 1
    if _is_num(inner):
        number = _go_count(inner)
        if number is None:
            return None, cursor
        return ("num", str(number)), cursor
    if isinstance(inner, str) and len(inner) == 1:
        return ("num", str(ord(inner))), cursor
    if _is_opaque(inner):
        return ("opaque", "int"), cursor
    return None, cursor


def _go_count(value):
    if isinstance(value, tuple) and len(value) == 2 and value[0] == "num":
        try:
            return int(value[1])
        except ValueError:
            return None
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    return None


def _go_string_call(method, tokens, index, environment):
    # ``index`` points at the ``(`` right after ``strings.<method>``.
    if index >= len(tokens) or tokens[index] != ("op", "("):
        return None, index
    cursor = index + 1
    raw: list[object] = []
    while cursor < len(tokens) and tokens[cursor] != ("op", ")"):
        if tokens[cursor] == ("op", ","):
            cursor += 1
            continue
        argument, cursor = _go_expression(tokens, cursor, environment)
        raw.append(argument)
    if cursor < len(tokens) and tokens[cursor] == ("op", ")"):
        cursor += 1
    if any(_is_opaque(item) for item in raw):
        return ("opaque", method), cursor
    if method in {"Replace", "ReplaceAll"}:
        expected = 4 if method == "Replace" else 3
        if len(raw) != expected or not all(isinstance(item, str) for item in raw):
            return None, cursor
        if method == "Replace":
            count = _go_count(raw[3])
            if count is None or count < 0:
                return None, cursor
            return raw[0].replace(raw[1], raw[2], count), cursor
        return raw[0].replace(raw[1], raw[2]), cursor
    if method == "Repeat":
        if len(raw) != 2 or not isinstance(raw[0], str):
            return None, cursor
        repeats = _go_count(raw[1])
        if repeats is None or repeats < 0 or repeats > 64:
            return None, cursor
        return raw[0] * repeats, cursor
    if not all(isinstance(item, str) for item in raw):
        return None, cursor
    subject = raw[0] if raw else ""
    if method in {"ToUpper", "ToTitle"}:
        return subject.upper(), cursor
    if method == "ToLower":
        return subject.lower(), cursor
    if method == "TrimSpace":
        return subject.strip(), cursor
    if method in {"TrimPrefix", "TrimSuffix"}:
        if len(raw) != 2:
            return None, cursor
        if method == "TrimPrefix":
            return (subject[len(raw[1]):] if subject.startswith(raw[1]) else subject), cursor
        return (subject[: -len(raw[1])] if raw[1] and subject.endswith(raw[1]) else subject), cursor
    if method == "Trim":
        if len(raw) != 2:
            return None, cursor
        return subject.strip(raw[1]), cursor
    return None, cursor


def _go_join_value(tokens, index, environment):
    # ``index`` points at ``(`` right after ``strings.Join``.
    if index >= len(tokens) or tokens[index] != ("op", "("):
        return None, index
    index += 1
    if index < len(tokens) and tokens[index] == ("op", "["):
        cursor = index + 1
        if cursor < len(tokens) and tokens[cursor] == ("op", "]"):
            cursor += 1
        if cursor < len(tokens) and tokens[cursor][0] == "ident" and cursor + 1 < len(tokens) and tokens[cursor + 1] == ("op", "{"):
            parts, index = _go_slice_value(tokens, cursor + 2, environment)
        else:
            parts, index = None, cursor
    elif index < len(tokens) and tokens[index][0] == "ident":
        parts = environment.get(tokens[index][1])
        index += 1
    else:
        parts = None
    if index < len(tokens) and tokens[index] == ("op", ","):
        index += 1
    separator, index = _go_expression(tokens, index, environment)
    if index < len(tokens) and tokens[index] == ("op", ")"):
        index += 1
    if _is_opaque(parts) or _is_opaque(separator):
        return ("opaque", "join"), index
    if (
        isinstance(parts, tuple)
        and parts
        and parts[0] == "slice"
        and isinstance(separator, str)
        and all(isinstance(item, str) for item in parts[1])
    ):
        return separator.join(parts[1]), index
    return None, index


_GO_JOIN_CALLS = {"Join"}
_GO_FORMAT_CALLS = {"Sprintf", "Sprint", "Sprintln"}
_GO_STRING_CALLS = {
    "ToUpper", "ToLower", "ToTitle", "TrimSpace", "Trim", "TrimPrefix",
    "TrimSuffix", "Replace", "ReplaceAll", "Repeat",
}


def _go_term(tokens, index, environment):
    if index >= len(tokens):
        return None, index
    kind, value = tokens[index]
    if kind == "str":
        return value, index + 1
    if kind == "num":
        return ("num", value), index + 1
    if kind == "op" and value == "(":
        inner, index = _go_expression(tokens, index + 1, environment)
        if index < len(tokens) and tokens[index] == ("op", ")"):
            index += 1
        return inner, index
    if kind == "op" and value == "[":
        return _go_composite_value(tokens, index, environment)
    if kind == "ident":
        if value == "string" and index + 1 < len(tokens) and tokens[index + 1] == ("op", "("):
            return _go_string_conversion(tokens, index + 1, environment)
        if value == "map" and index + 1 < len(tokens) and tokens[index + 1] == ("op", "["):
            return _go_map_type_value(tokens, index, environment)
        if (
            value in _GO_INT_CONVERSIONS
            and index + 1 < len(tokens)
            and tokens[index + 1] == ("op", "(")
        ):
            return _go_int_conversion(tokens, index + 1, environment)
        dotted, after = _go_dotted_name(tokens, index)
        if (
            dotted is not None
            and after < len(tokens)
            and tokens[after] == ("op", "{")
            and dotted.rsplit(".", 1)[-1] not in _GO_KEYWORDS
        ):
            return _go_struct_value(tokens, after, environment)
        name, paren = _go_dotted_call(tokens, index)
        if name is not None:
            method = name.rsplit(".", 1)[-1]
            if name in _GO_OPAQUE_CALLS or method in _GO_OPAQUE_METHODS:
                return _go_payload_value(name, tokens, paren, environment)
            if name == "append":
                return _go_append_value(tokens, paren, environment)
            if name.endswith("filepath.Join"):
                return _go_join_paths(tokens, paren, environment)
            if method in _GO_JOIN_CALLS:
                return _go_join_value(tokens, paren, environment)
            if method in _GO_FORMAT_CALLS:
                return _go_sprintf_value(tokens, paren, environment)
            if method in _GO_STRING_CALLS:
                return _go_string_call(method, tokens, paren, environment)
            if name in environment:
                # A local helper whose (single, foldable) return value is known.
                return environment[name], paren
            return None, paren
        if dotted is not None and dotted in _GO_PROCESS_SINKS and "." in dotted:
            # A sink referenced as a value (``run := exec.Command``); the call
            # that follows resolves the invocation.
            return ("sink", dotted), after
        base = environment.get(value)
        cursor = index + 1
        if cursor < len(tokens) and tokens[cursor] == ("op", "["):
            return _go_index_value(tokens, cursor, base, environment)
        if (
            cursor + 1 < len(tokens)
            and tokens[cursor] == ("op", ".")
            and tokens[cursor + 1][0] == "ident"
            and isinstance(base, tuple)
            and len(base) == 2
            and base[0] == "struct"
        ):
            return base[1].get(tokens[cursor + 1][1]), cursor + 2
        if base is not None:
            return base, cursor
        return None, cursor
    return None, index + 1


def _go_join_paths(tokens, index, environment):
    arguments, cursor = _go_call_arguments(tokens, index)
    resolved = [_go_expression(argument, 0, environment)[0] for argument in arguments]
    if any(_is_opaque(item) for item in resolved):
        return ("opaque", "filepath.Join"), cursor
    if all(isinstance(item, str) for item in resolved):
        return "/".join(resolved), cursor
    return None, cursor


def _go_expression(tokens, index, environment):
    value, index = _go_term(tokens, index, environment)
    while index < len(tokens) and tokens[index] == ("op", "+"):
        right, index = _go_term(tokens, index + 1, environment)
        if _is_opaque(value) or _is_opaque(right):
            value = ("opaque", "concat")
        elif isinstance(value, str) and isinstance(right, str):
            value = value + right
        elif _is_num(value) and _is_num(right):
            left_number = _go_count(value)
            right_number = _go_count(right)
            if left_number is None or right_number is None:
                value = None
            else:
                value = ("num", str(left_number + right_number))
        else:
            value = None
    return value, index


def _go_folded_strings(
    text: str,
    *,
    import_aliases: tuple[dict[str, str], set[str]] | None = None,
    function_returns: Mapping[str, object] | None = None,
    has_decoder: bool = False,
) -> list[str]:
    mapping, dots = import_aliases or ({}, set())
    tokens = _go_rewrite_aliases(_go_tokens(text), mapping, dots)
    environment: dict[str, object] = {}
    resolved: list[str] = []
    length = len(tokens)
    # First pass: fold assignment right-hand sides and remember every scalar and
    # every slice element they can denote.  Constant propagation must not stop at
    # top-level string assignments: executables are routinely carried as
    # ``argv := []string{"car" + "go", ...}`` and read back through ``argv[0]``.
    _go_fold_assignments(tokens, environment, resolved)
    # Local helpers with a statically foldable return widen constant propagation
    # (``exec.Command(binaryName())``, ``exec.Cmd{Path: binaryName()}``); fold the
    # assignments again so callers pick the resolved value up.
    returns = _go_function_return_values(tokens)
    for name, value in returns.items():
        environment.setdefault(name, value)
    # Cross-file helpers: a function returning a (literal) program name declared
    # in another file is the same behaviour once its value flows into the sink.
    if function_returns:
        for name, value in function_returns.items():
            environment.setdefault(name, value)
    if returns:
        _go_fold_assignments(tokens, environment, resolved, compound=False)
    # Second pass: fold expressions that never reach a named variable, for
    # example an inline ``exec.Command("car" + "go", ...)`` or
    # ``exec.Command(fmt.Sprintf("%s%s", "car", "go"), ...)``.
    seen = set(resolved)
    for position in range(length):
        kind = tokens[position][0]
        if kind == "str":
            following = tokens[position + 1] if position + 1 < length else None
            if following != ("op", "+"):
                continue
        elif kind == "ident":
            following = tokens[position + 1] if position + 1 < length else None
            if following not in {("op", "("), ("op", "."), ("op", "[")}:
                continue
        elif kind == "op" and tokens[position][1] == "[":
            pass
        else:
            continue
        value, _ = _go_expression(tokens, position, environment)
        collected: list[str] = []
        _collect_strings(value, collected)
        for item in collected:
            if item not in seen:
                seen.add(item)
                resolved.append(item)
    # Third pass: canonicalise process/plugin sink invocations.  A program name
    # forwarded through a local variable (``fallback := "chess-tui";
    # exec.Command(fallback)``), a slice element, a helper-call result, a
    # ``exec.Cmd{Path: …}`` literal or a sink alias (``run := exec.Command;
    # run(prog)``) must still match the declarative ``exec.Command("chess-tui``
    # rule, so reconstruct the call site from the *resolved* program argument.
    for name, paren in _go_sink_sites(tokens, environment):
        arguments, _ = _go_call_arguments(tokens, paren)
        index_arg = _GO_PROCESS_SINKS[name]
        if index_arg >= len(arguments):
            continue
        program, _ = _go_expression(list(arguments[index_arg]), 0, environment)
        for literal in _literal_values(program):
            canonical = f'{name}("{literal}"'
            if canonical not in seen:
                seen.add(canonical)
                resolved.append(canonical)
    for position in range(length):
        struct_name, after = _go_dotted_name(tokens, position)
        if struct_name is None or not struct_name.endswith("Cmd"):
            continue
        if after >= length or tokens[after] != ("op", "{"):
            continue
        for program in _go_cmd_struct_program(tokens, after, environment):
            for literal in _literal_values(program):
                canonical = f'exec.Command("{literal}"'
                if canonical not in seen:
                    seen.add(canonical)
                    resolved.append(canonical)
    # Fourth pass: if the file (or, for a cross-file split, the tree) materialises
    # a payload through a decoder, also resolve the *encoded literals* the file
    # may carry so the decoded command text is matched by the ordinary rules -- a
    # payload encoded in one file and decoded in another is otherwise opaque to
    # the literal scan.
    if has_decoder or any(name in _GO_DECODE_CALLS for name, _ in _go_call_sites(tokens)):
        for decoded in _decoded_literals(resolved):
            if decoded not in seen:
                seen.add(decoded)
                resolved.append(decoded)
    return resolved


def _decoded_literals(values: Sequence[str]) -> Iterable[str]:
    for value in tuple(values):
        text = value.strip()
        decoded: str | None = None
        if _GO_BASE64_LITERAL.match(text):
            padding = "=" * (-len(text) % 4)
            try:
                decoded = base64.urlsafe_b64decode(text + padding).decode("utf-8")
            except (binascii.Error, ValueError, UnicodeDecodeError):
                decoded = None
        if decoded is None and len(text) % 2 == 0 and _GO_HEX_LITERAL.match(text):
            try:
                decoded = bytes.fromhex(text).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                decoded = None
        if decoded and decoded != value and _mostly_printable(decoded):
            yield decoded


_GO_BASE64_LITERAL = re.compile(r"^[A-Za-z0-9+/_-]{8,}={0,2}$")
_GO_HEX_LITERAL = re.compile(r"^[0-9a-fA-F]{8,}$")


def _mostly_printable(text: str) -> bool:
    if len(text) < 6:
        return False
    printable = sum(character.isprintable() or character in "\t\n\r" for character in text)
    return printable / len(text) >= 0.9


def _literal_values(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, tuple) and len(value) == 2 and value[0] == "slice":
        for item in value[1]:
            yield from _literal_values(item)


def _go_statement_end(tokens, index: int) -> int:
    depth = 0
    while index < len(tokens):
        token = tokens[index]
        if token[0] == "op" and token[1] in "([{":
            depth += 1
        elif token[0] == "op" and token[1] in ")]}":
            if depth == 0:
                return index
            depth -= 1
        elif depth == 0 and (token[0] == "nl" or token == ("op", ";")):
            return index
        index += 1
    return index


def _go_matching_brace(tokens, index: int) -> int:
    depth = 0
    while index < len(tokens):
        token = tokens[index]
        if token == ("op", "{"):
            depth += 1
        elif token == ("op", "}"):
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return len(tokens)


def _go_fold_assignments(tokens, environment, resolved, compound: bool = True) -> None:
    """Fold ``name := expr`` / ``name = expr`` / ``name += expr`` into ``environment``.

    ``compound=False`` is used for the re-run after helper returns are merged: a
    ``+=`` propagation must be applied exactly once or the value would compound
    twice (``"car" + "go" + "go"``).
    """

    index = 0
    length = len(tokens)
    while index < length:
        operator = tokens[index + 1] if index + 1 < length else None
        if tokens[index][0] == "ident" and operator in {
            ("op", ":="),
            ("op", "="),
            ("op", "+="),
        }:
            name = tokens[index][1]
            after = index + 2
            if operator == ("op", "+="):
                if not compound:
                    index += 1
                    continue
                right, after = _go_expression(tokens, index + 2, environment)
                base = environment.get(name)
                if _is_opaque(base) or _is_opaque(right):
                    value: object = ("opaque", "concat")
                elif isinstance(base, str) and isinstance(right, str):
                    value = base + right
                elif _is_num(base) and _is_num(right):
                    left_number = _go_count(base)
                    right_number = _go_count(right)
                    value = (
                        None
                        if left_number is None or right_number is None
                        else ("num", str(left_number + right_number))
                    )
                else:
                    value = None
            else:
                value, after = _go_expression(tokens, index + 2, environment)
            environment[name] = value
            _collect_strings(value, resolved)
            index = after
            while index < length and tokens[index][0] != "nl" and tokens[index] != ("op", ";"):
                index += 1
            index += 1
            continue
        index += 1


def _go_function_return_values(tokens) -> dict[str, object]:
    """Fold the return value of a local helper with a single literal result.

    ``func binary() string { return "car" + "go" }`` is the same behaviour as a
    direct literal once the helper is inlined; resolving it keeps the sink scan
    aware of helper-forwarded program names.
    """

    values: dict[str, object] = {}
    for name, body in _go_function_bodies(tokens):
        local: dict[str, object] = {}
        collected: list[str] = []
        index = 0
        while index < len(body):
            operator = body[index + 1] if index + 1 < len(body) else None
            if body[index][0] == "ident" and operator in {
                ("op", ":="),
                ("op", "="),
                ("op", "+="),
            }:
                identifier = body[index][1]
                if operator == ("op", "+="):
                    right, after = _go_expression(body, index + 2, local)
                    base = local.get(identifier)
                    if _is_opaque(base) or _is_opaque(right):
                        value: object = ("opaque", "concat")
                    elif isinstance(base, str) and isinstance(right, str):
                        value = base + right
                    else:
                        value = None
                else:
                    value, after = _go_expression(body, index + 2, local)
                local[identifier] = value
                index = after
            index += 1
        found: list[object] = []
        for index in range(len(body) - 1):
            if body[index] != ("ident", "return"):
                continue
            value, _ = _go_expression(body, index + 1, local)
            if value is not None:
                found.append(value)
        if not found:
            continue
        if len(found) == 1:
            values[name] = found[0]
        else:
            items: list[object] = []
            for value in found:
                if isinstance(value, tuple) and len(value) == 2 and value[0] == "slice":
                    items.extend(value[1])
                else:
                    items.append(value)
            values[name] = ("slice", items)
        _collect_strings(values[name], collected)
        if not collected:
            values.pop(name)
    return values


def _go_function_bodies(tokens) -> list[tuple[str, list]]:
    bodies: list[tuple[str, list]] = []
    for index in range(len(tokens) - 1):
        if tokens[index] != ("ident", "func"):
            continue
        cursor = index + 1
        if cursor < len(tokens) and tokens[cursor] == ("op", "("):
            cursor = _go_skip_to(tokens, cursor, ")")
        if cursor >= len(tokens) or tokens[cursor][0] != "ident":
            continue
        name = tokens[cursor][1]
        cursor += 1
        if cursor < len(tokens) and tokens[cursor] == ("op", "("):
            cursor = _go_skip_to(tokens, cursor, ")")
        while cursor < len(tokens) and tokens[cursor] != ("op", "{"):
            cursor += 1
        if cursor >= len(tokens):
            continue
        end = _go_matching_brace(tokens, cursor)
        bodies.append((name, tokens[cursor + 1:end]))
    return bodies


def _go_matching_paren(tokens, index: int) -> int:
    depth = 0
    while index < len(tokens):
        token = tokens[index]
        if token == ("op", "("):
            depth += 1
        elif token == ("op", ")"):
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return len(tokens)


def _go_parameter_names(tokens_slice) -> set[str]:
    """Parameter identifiers of a Go function signature (receiver excluded)."""

    names: set[str] = set()
    groups: list[list] = []
    current: list = []
    depth = 0
    for token in tokens_slice:
        if token[0] == "op" and token[1] in "([{":
            depth += 1
        elif token[0] == "op" and token[1] in ")]}":
            depth -= 1
        if depth == 0 and token == ("op", ","):
            groups.append(current)
            current = []
            continue
        current.append(token)
    groups.append(current)
    for group in groups:
        cursor = 0
        while cursor < len(group):
            if group[cursor][0] == "ident" and group[cursor][1] not in _GO_KEYWORDS:
                names.add(group[cursor][1])
                cursor += 1
                if cursor < len(group) and group[cursor] == ("op", ","):
                    cursor += 1
                    continue
                break
            break
    return names


def _go_parameter_list(tokens_slice) -> list[str]:
    """Ordered parameter identifiers of a Go function signature.

    Unlike :func:`_go_parameter_names` this preserves position, which is what a
    function-value indirection needs: the callback handed to a helper is bound to
    a *specific* argument slot (``runone(spawn, env)`` binds ``f`` to ``spawn`` at
    position 0), so the analysis must map call arguments back onto parameters.
    A parameter group (``a, b int``) contributes each name before its type.
    """

    names: list[str] = []
    groups: list[list] = []
    current: list = []
    depth = 0
    for token in tokens_slice:
        if token[0] == "op" and token[1] in "([{":
            depth += 1
        elif token[0] == "op" and token[1] in ")]}":
            depth -= 1
        if depth == 0 and token == ("op", ","):
            groups.append(current)
            current = []
            continue
        current.append(token)
    groups.append(current)
    for group in groups:
        for token in group:
            if token[0] == "ident" and token[1] not in _GO_KEYWORDS:
                names.append(token[1])
                break
    return names


def _go_function_declarations(tokens) -> list[tuple[str, set[str], list]]:
    declarations: list[tuple[str, set[str], list]] = []
    for index in range(len(tokens) - 1):
        if tokens[index] != ("ident", "func"):
            continue
        cursor = index + 1
        if cursor < len(tokens) and tokens[cursor] == ("op", "("):
            cursor = _go_matching_paren(tokens, cursor) + 1
        if cursor >= len(tokens) or tokens[cursor][0] != "ident":
            continue
        name = tokens[cursor][1]
        cursor += 1
        params: set[str] = set()
        if cursor < len(tokens) and tokens[cursor] == ("op", "("):
            end = _go_matching_paren(tokens, cursor)
            params = _go_parameter_names(tokens[cursor + 1:end])
            cursor = end + 1
        while cursor < len(tokens) and tokens[cursor] != ("op", "{"):
            cursor += 1
        if cursor >= len(tokens):
            continue
        end = _go_matching_brace(tokens, cursor)
        declarations.append((name, params, tokens[cursor + 1:end]))
    return declarations


def _go_function_declarations_ordered(tokens) -> list[tuple[str, list[str], list]]:
    """Function declarations with *ordered* parameter names (receiver excluded)."""

    declarations: list[tuple[str, list[str], list]] = []
    for index in range(len(tokens) - 1):
        if tokens[index] != ("ident", "func"):
            continue
        cursor = index + 1
        if cursor < len(tokens) and tokens[cursor] == ("op", "("):
            cursor = _go_matching_paren(tokens, cursor) + 1
        if cursor >= len(tokens) or tokens[cursor][0] != "ident":
            continue
        name = tokens[cursor][1]
        cursor += 1
        params: list[str] = []
        if cursor < len(tokens) and tokens[cursor] == ("op", "("):
            end = _go_matching_paren(tokens, cursor)
            params = _go_parameter_list(tokens[cursor + 1:end])
            cursor = end + 1
        while cursor < len(tokens) and tokens[cursor] != ("op", "{"):
            cursor += 1
        if cursor >= len(tokens):
            continue
        end = _go_matching_brace(tokens, cursor)
        declarations.append((name, params, tokens[cursor + 1:end]))
    return declarations


def _go_parameter_specs(tokens_slice) -> list[tuple[str, bool]]:
    """Ordered ``(name, is_variadic)`` specs of a Go function signature.

    Mirrors :func:`_go_parameter_list` but also records whether the parameter is
    *variadic* (``fns ...func(...)``): a variadic parameter is a sequence the
    caller fills with the trailing arguments, so it must be modelled as a
    container of values rather than a single slot.
    """

    specs: list[tuple[str, bool]] = []
    groups: list[list] = []
    current: list = []
    depth = 0
    for token in tokens_slice:
        if token[0] == "op" and token[1] in "([{":
            depth += 1
        elif token[0] == "op" and token[1] in ")]}":
            depth -= 1
        if depth == 0 and token == ("op", ","):
            groups.append(current)
            current = []
            continue
        current.append(token)
    groups.append(current)
    for group in groups:
        for position, token in enumerate(group):
            if token[0] == "ident" and token[1] not in _GO_KEYWORDS:
                # ``...`` may surface as three consecutive ``.`` tokens, so the
                # ellipsis is detected as a run rather than a single token.
                tail = group[position + 1:]
                variadic = any(
                    tail[j:j + 3] == [("op", "."), ("op", "."), ("op", ".")]
                    for j in range(len(tail))
                ) or any(item == ("op", "...") for item in tail)
                specs.append((token[1], variadic))
                break
    return specs


def _go_function_variadic_params(tokens) -> dict[str, tuple[list[str], int]]:
    """Map ``func name`` -> (ordered params, index of the variadic param or -1)."""

    result: dict[str, tuple[list[str], int]] = {}
    for index in range(len(tokens) - 1):
        if tokens[index] != ("ident", "func"):
            continue
        cursor = index + 1
        if cursor < len(tokens) and tokens[cursor] == ("op", "("):
            cursor = _go_matching_paren(tokens, cursor) + 1
        if cursor >= len(tokens) or tokens[cursor][0] != "ident":
            continue
        name = tokens[cursor][1]
        cursor += 1
        params: list[str] = []
        variadic = -1
        if cursor < len(tokens) and tokens[cursor] == ("op", "("):
            end = _go_matching_paren(tokens, cursor)
            specs = _go_parameter_specs(tokens[cursor + 1:end])
            params = [param for param, _ in specs]
            for position, (_, is_variadic) in enumerate(specs):
                if is_variadic:
                    variadic = position
            cursor = end + 1
        while cursor < len(tokens) and tokens[cursor] != ("op", "{"):
            cursor += 1
        if cursor >= len(tokens):
            continue
        result.setdefault(name, (params, variadic))
    return result


def _go_declared_type_names(tokens) -> list[str]:
    """Names introduced by ``type`` declarations (single and grouped form).

    A *type conversion* ``T(x)`` runs no user code, so it preserves the value of
    ``x``; knowing which identifiers name a declared type lets the function-value
    resolver see through ``launcher(spawn)`` just like a bare ``spawn``.
    """

    names: list[str] = []
    index = 0
    while index < len(tokens):
        if tokens[index] != ("ident", "type"):
            index += 1
            continue
        cursor = index + 1
        if cursor < len(tokens) and tokens[cursor] == ("op", "("):
            end = _go_matching_paren(tokens, cursor)
            depth = 0
            line_start = True
            for token in tokens[cursor + 1:end]:
                if token[0] == "op" and token[1] in "([{":
                    depth += 1
                elif token[0] == "op" and token[1] in ")]}":
                    depth -= 1
                if depth != 0:
                    continue
                if token[0] == "nl" or token == ("op", ";"):
                    line_start = True
                    continue
                if line_start:
                    if token[0] == "ident" and token[1] not in _GO_KEYWORDS:
                        names.append(token[1])
                    line_start = False
            index = end + 1
            continue
        if cursor < len(tokens) and tokens[cursor][0] == "ident":
            names.append(tokens[cursor][1])
        index = cursor + 1
    return names


def _go_params_reach_sink(
    body,
    params: set[str],
    wrapper_functions=frozenset(),
    func_value_params=frozenset(),
    is_wrapper=None,
) -> bool:
    """True when a parameter (or a value derived only from parameters) names
    what a process/plugin sink executes, directly or through another helper that
    itself forwards its argument to a sink (transitive wrapper forwarding).

    ``wrapper_functions`` are helpers known to reach a sink; ``func_value_params``
    are *this* function's parameters that are themselves bound to a wrapper at a
    call site (a function value handed in as an argument).  Both are treated as
    forwarding targets so ``f(p)`` inside a combinator reaches the sink once the
    caller supplies a wrapper function value for ``f``.

    ``is_wrapper`` resolves a *callee expression* to a wrapper function value
    (containers, factories, indexed callees -- ``fns[0](p)``): the value flow is
    the same however the callee is spelled, so a call through an indexed wrapper
    reaches the sink exactly like a call through a named one."""

    if not params:
        return False
    environment = set(params)
    changed = True
    while changed:
        changed = False
        for index, token in enumerate(body):
            if token not in {("op", ":="), ("op", "=")}:
                continue
            names: list[str] = []
            cursor = index - 1
            valid = True
            while cursor >= 0:
                previous = body[cursor]
                if previous[0] == "nl" or (previous[0] == "op" and previous[1] in ";{}()"):
                    break
                if previous[0] == "ident":
                    names.append(previous[1])
                    cursor -= 1
                    continue
                if previous == ("op", ","):
                    cursor -= 1
                    continue
                valid = False
                break
            if not valid or not names:
                continue
            end = _go_statement_end(body, index + 1)
            if _go_expression_opaque(body[index + 1:end], environment, set()):
                for name in names:
                    if name not in environment:
                        environment.add(name)
                        changed = True
    for name, paren in _go_sink_sites(body, {}):
        arguments, _ = _go_call_arguments(body, paren)
        index_arg = _GO_PROCESS_SINKS[name]
        if index_arg < len(arguments) and _go_expression_opaque(
            arguments[index_arg], environment, set()
        ):
            return True
    for position in range(len(body)):
        struct_name, after = _go_dotted_name(body, position)
        if struct_name is None or not struct_name.endswith("Cmd"):
            continue
        if after >= len(body) or body[after] != ("op", "{"):
            continue
        cursor = after + 1
        while cursor + 1 < len(body) and body[cursor] != ("op", "}"):
            if (
                body[cursor][0] == "ident"
                and body[cursor][1] in {"Path", "Args"}
                and body[cursor + 1] == ("op", ":")
            ):
                end = _go_argument_end(body, cursor + 2)
                if _go_expression_opaque(body[cursor + 2:end], environment, set()):
                    return True
            cursor += 1
    # A parameter handed to another helper that itself forwards to a sink reaches
    # the same process: ``mid(p) { return spawn(p) }`` is a forwarding wrapper
    # once ``spawn`` is known to be one, which makes the closure transitive.  The
    # same holds when the forwarding target is a *function-valued parameter* that
    # the caller binds to a wrapper (``runone(spawn, p)`` -> ``f(p)`` with ``f``
    # bound to ``spawn``): resolving that binding keeps the chain connected across
    # the higher-order indirection instead of stopping at the call.
    for name, paren in _go_call_sites(body):
        if name not in wrapper_functions and name not in func_value_params:
            continue
        arguments, _ = _go_call_arguments(body, paren)
        if any(_go_expression_opaque(argument, environment, set()) for argument in arguments):
            return True
    # The callee may not be a bare name: ``fns[0](p)`` / ``(<-ch)(p)`` /
    # ``table()[i](p)`` all invoke a wrapper function value obtained through the
    # value flow.  ``is_wrapper`` (a resolver bound to the tree context) decides,
    # so the *shape* of the callee does not matter.
    if is_wrapper is not None:
        for callee, paren in _go_call_exprs(body):
            if not is_wrapper(callee):
                continue
            arguments, _ = _go_call_arguments(body, paren)
            if any(
                _go_expression_opaque(argument, environment, set())
                for argument in arguments
            ):
                return True
    return False


def _go_func_literal_wrapper(tokens_slice, wrapper_functions=frozenset()) -> bool:
    """True when an inline ``func(…) { … }`` literal forwards its parameter to a
    process/plugin sink (a wrapper written as an anonymous function value)."""

    if not tokens_slice or tokens_slice[0] != ("ident", "func"):
        return False
    cursor = 1
    params: list[str] = []
    if cursor < len(tokens_slice) and tokens_slice[cursor] == ("op", "("):
        end = _go_matching_paren(tokens_slice, cursor)
        params = _go_parameter_list(tokens_slice[cursor + 1:end])
        cursor = end + 1
    while cursor < len(tokens_slice) and tokens_slice[cursor] != ("op", "{"):
        cursor += 1
    if cursor >= len(tokens_slice):
        return False
    end = _go_matching_brace(tokens_slice, cursor)
    return _go_params_reach_sink(
        tokens_slice[cursor + 1:end], set(params), wrapper_functions
    )


class _GoWrapperContext:
    """Shape-independent function-value environment for the wrapper closure.

    ``names`` holds the identifiers / fields already known to denote a wrapper (a
    function that reaches a process/plugin sink).  ``seqs`` / ``maps`` / ``structs``
    hold the element and field token slices of container / struct values and
    ``factories`` holds helpers whose *return value* is a wrapper value
    (``func pick() func(string) error { return spawn }``).  Together they let the
    resolver evaluate a function value however it is *spelled* -- ``handlers[0]``,
    ``pick()``, ``h.f`` -- instead of only accepting a bare named reference.

    The container side is itself shape-independent: ``container_factories`` holds
    helpers whose return value is a *container* (``func table() []func(string) error
    { return … }``), so a value built by *any* producer -- a composite literal,
    ``append``, ``make`` + index assignment, a map index assignment, a factory
    return or a container alias -- is registered the same way and an element access
    (``table()[0]``, ``handlers[i]``, ``m[k]``) resolves through it.
    """

    __slots__ = (
        "names",
        "seqs",
        "maps",
        "structs",
        "factories",
        "container_factories",
        "type_names",
        "alias_parent",
        "value_aliases",
    )

    def __init__(self, names) -> None:
        self.names = names
        self.seqs: dict[str, list] = {}
        self.maps: dict[str, list] = {}
        self.structs: dict[str, list] = {}
        self.factories: set[str] = set()
        self.container_factories: dict[str, list] = {}
        # Declared ``type`` names.  A *type conversion* ``T(x)`` preserves the
        # converted value (it runs no user code), so ``launcher(spawn)`` /
        # ``any(spawn)`` still denote a wrapper when ``spawn`` does.
        self.type_names: set[str] = {"any"}
        # Value-slot aliasing.  ``alias_parent`` is a union-find over simple
        # names built from ``x := y`` / ``x := &y`` bindings: ``relay := ch``
        # makes ``relay`` and ``ch`` the *same* slot, so a send on one is visible
        # to a receive on the other (``q := &h`` makes ``q.ch`` and ``h.ch`` the
        # same slot).  ``value_aliases`` maps a raw name to the token slices it
        # was bound to, so a slot whose value is itself an expression (a receive,
        # a call, a container producer) can be resolved through it.
        self.alias_parent: dict[str, str] = {}
        self.value_aliases: dict[str, list] = {}

    def find(self, name: str) -> str:
        parent = self.alias_parent
        root = name
        while parent.get(root, root) != root:
            root = parent[root]
        while parent.get(name, name) != name:
            parent[name], name = root, parent[name]
        return root

    def canon(self, name: str) -> str:
        """Canonical slot name: the root of the first dotted component."""
        root, separator, rest = name.partition(".")
        resolved = self.find(root)
        if not separator:
            return resolved
        return f"{resolved}.{rest}"


def _go_matching_bracket(tokens, index: int) -> int:
    depth = 0
    while index < len(tokens):
        token = tokens[index]
        if token == ("op", "["):
            depth += 1
        elif token == ("op", "]"):
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return len(tokens)


def _go_top_level_colon(tokens):
    depth = 0
    for index, token in enumerate(tokens):
        if token[0] == "op" and token[1] in "([{":
            depth += 1
        elif token[0] == "op" and token[1] in ")]}":
            depth -= 1
        elif depth == 0 and token == ("op", ":"):
            return index
    return None


def _go_literal_entries(tokens, brace_index):
    """Top-level ``{…}`` elements of a composite literal as raw token slices."""

    end = _go_matching_brace(tokens, brace_index)
    entries: list[list] = []
    start = brace_index + 1
    depth = 0
    cursor = start
    while cursor < end:
        token = tokens[cursor]
        if token[0] == "op" and token[1] in "([{":
            depth += 1
        elif token[0] == "op" and token[1] in ")]}":
            depth -= 1
        elif depth == 0 and token == ("op", ","):
            entries.append(tokens[start:cursor])
            start = cursor + 1
        cursor += 1
    if start < end:
        entries.append(tokens[start:end])
    return entries, end


def _go_skip_type_to_brace(tokens, index):
    """Advance over a composite literal's element type up to its opening ``{``."""

    depth = 0
    while index < len(tokens):
        token = tokens[index]
        if token[0] == "op" and token[1] in "([":
            depth += 1
        elif token[0] == "op" and token[1] in ")]":
            depth -= 1
        elif token == ("op", "{"):
            return index
        elif token[0] == "nl" or token == ("op", ";"):
            return None
        index += 1
    return None


def _go_composite_literal(tokens, index):
    """Parse a composite literal opening at ``index``.

    Returns ``(kind, data, end)`` with ``kind`` one of ``seq`` (a list of element
    token slices), ``map`` (``(key, value)`` token-slice pairs) or ``struct``
    (``(field, value)`` pairs), or ``None`` when ``index`` does not open one.
    """

    cursor = index
    if cursor < len(tokens) and tokens[cursor] == ("op", "&"):
        cursor += 1
    if cursor >= len(tokens):
        return None
    if tokens[cursor] == ("op", "["):
        brace = _go_skip_type_to_brace(tokens, _go_matching_bracket(tokens, cursor) + 1)
        if brace is None:
            return None
        entries, end = _go_literal_entries(tokens, brace)
        return "seq", entries, end + 1
    if tokens[cursor] == ("ident", "map"):
        cursor += 1
        if cursor < len(tokens) and tokens[cursor] == ("op", "["):
            cursor = _go_matching_bracket(tokens, cursor) + 1
        brace = _go_skip_type_to_brace(tokens, cursor)
        if brace is None:
            return None
        entries, end = _go_literal_entries(tokens, brace)
        pairs = []
        for entry in entries:
            colon = _go_top_level_colon(entry)
            if colon is not None:
                pairs.append((entry[:colon], entry[colon + 1:]))
        return "map", pairs, end + 1
    dotted, after = _go_dotted_name(tokens, cursor)
    if (
        dotted
        and after < len(tokens)
        and tokens[after] == ("op", "{")
        and dotted.rsplit(".", 1)[-1] not in _GO_KEYWORDS
    ):
        entries, end = _go_literal_entries(tokens, after)
        fields = []
        for entry in entries:
            colon = _go_top_level_colon(entry)
            if colon is None or colon == 0:
                continue
            key = entry[colon - 1]
            if key[0] == "ident":
                fields.append((key[1], entry[colon + 1:]))
        return "struct", fields, end + 1
    return None


def _go_literal_lookup(kind, data, index_expr, context, depth, resolve=None) -> bool:
    """Resolve ``container[index]`` against a parsed composite literal.

    ``resolve`` decides what the element must denote (a wrapper value by
    default, a factory value for a factory container).  Passing it in keeps the
    container *addressing* logic in one place: any container of wrappers -- or of
    factories -- is indexed the same way however it was produced.
    """

    if resolve is None:
        resolve = _go_resolve_wrapper_value
    key, _ = _go_expression(index_expr, 0, {})
    if kind == "seq":
        if _is_num(key):
            try:
                position = int(key[1])
            except ValueError:
                return False
            if 0 <= position < len(data):
                return resolve(data[position], context, depth + 1)
            return False
        return any(resolve(element, context, depth + 1) for element in data)
    if kind == "map":
        folded = _map_key(key)
        if folded is not None:
            for key_tokens, value_tokens in data:
                candidate, _ = _go_expression(key_tokens, 0, {})
                canonical = _map_key(candidate)
                # A non-constant write key could name any key, so it may alias the
                # lookup key; a constant write key must match exactly.
                if canonical == folded or canonical is None:
                    if resolve(value_tokens, context, depth + 1):
                        return True
            return False
        return any(resolve(value, context, depth + 1) for _, value in data)
    return False


def _go_strip_outer_parens(tokens_slice):
    """Drop redundant outer ``( … )`` and a leading ``&`` from an expression."""

    slice_ = tokens_slice
    while (
        len(slice_) >= 2
        and slice_[0] == ("op", "(")
        and _go_matching_paren(slice_, 0) == len(slice_) - 1
    ):
        slice_ = slice_[1:-1]
    if slice_ and slice_[0] == ("op", "&"):
        slice_ = slice_[1:]
    return slice_


def _go_split_trailing_index(tokens_slice):
    """``head[idx]`` -> ``(head, idx_tokens)`` when a balanced ``[…]`` closes the
    expression; ``None`` otherwise.

    This lets every container *consumer* -- ``x[i]``, ``f()[i]``, ``[]T{…}[i]``,
    ``m[k]`` -- share a single element-resolution path instead of each written
    shape needing its own branch.
    """

    if not tokens_slice or tokens_slice[-1] != ("op", "]"):
        return None
    close = len(tokens_slice) - 1
    depth = 0
    cursor = close
    while cursor >= 0:
        token = tokens_slice[cursor]
        if token == ("op", "]"):
            depth += 1
        elif token == ("op", "["):
            depth -= 1
            if depth == 0:
                break
        cursor -= 1
    if cursor <= 0:
        return None
    index_expr = tokens_slice[cursor + 1:close]
    if not index_expr:
        return None
    return tokens_slice[:cursor], index_expr


def _go_make_container(slice_, name, paren, context, depth):
    """``make([]T, …)`` / ``make(map[K]V, …)`` -> an (empty) container value."""

    if name.rsplit(".", 1)[-1] != "make":
        return None
    arguments, _ = _go_call_arguments(slice_, paren)
    if arguments and any(token == ("ident", "map") for token in arguments[0]):
        return "map", []
    return "seq", []


def _go_resolve_container_value(tokens_slice, context, depth: int = 0):
    """Shape-independent evaluation of an expression to a *container value*.

    Returns ``(kind, data)`` with ``kind`` one of ``seq`` / ``map`` / ``struct``,
    or ``None`` when the expression is not a container.  Producers covered:
    composite literals, ``append``, ``make``, named containers / aliases, struct
    fields and helpers that return a container (``table()``), recursively.  A
    deceptive fallback may build the container of wrappers by *any* of these, so
    element resolution must not depend on which one was used.
    """

    if depth > 10 or not tokens_slice:
        return None
    slice_ = _go_strip_outer_parens(tokens_slice)
    if not slice_:
        return None
    # ``<-ch`` yields the value the channel carries: if that value is itself a
    # container (a channel of slices / tables), the received value is a
    # container, exactly like ``container[i]``.
    receive = _go_split_leading_receive(slice_)
    if receive is not None:
        channel = _go_receive_container(receive, context, depth)
        if channel is None:
            return None
        kind, data = channel
        elements = (
            list(data)
            if kind == "seq"
            else [value for _, value in data]
            if kind == "map"
            else []
        )
        for element in elements:
            resolved = _go_resolve_container_value(element, context, depth + 1)
            if resolved is not None:
                return resolved
        return None
    literal = _go_composite_literal(slice_, 0)
    if literal is not None and literal[2] == len(slice_):
        kind, data, _ = literal
        if kind in {"seq", "map", "struct"}:
            return kind, data
        return None
    name, paren = _go_dotted_call(slice_, 0)
    if name is not None and _go_matching_paren(slice_, paren) == len(slice_) - 1:
        made = _go_make_container(slice_, name, paren, context, depth)
        if made is not None:
            return made
        if name == "append":
            arguments, _ = _go_call_arguments(slice_, paren)
            if not arguments:
                return None
            base = _go_resolve_container_value(arguments[0], context, depth + 1)
            if base is None or base[0] != "seq":
                return None
            return "seq", list(base[1]) + list(arguments[1:])
        for candidate in (name, name.rsplit(".", 1)[-1]):
            if candidate in context.container_factories:
                return _go_resolve_container_value(
                    context.container_factories[candidate], context, depth + 1
                )
        return None
    base, after = _go_dotted_name(slice_, 0)
    if base is not None and after == len(slice_):
        # The name may be an *alias* of another slot (``relay := ch`` /
        # ``q := &h``): resolve through its canonical name so a container stored
        # under the aliased slot is seen from either spelling.
        for candidate in (context.canon(base), base):
            if candidate in context.seqs:
                return "seq", context.seqs[candidate]
            if candidate in context.maps:
                return "map", context.maps[candidate]
            if candidate in context.structs:
                return "struct", context.structs[candidate]
        # A slot whose value is an expression (``fs := <-ch``, ``r := table()``)
        # is resolved through the binding, so the container it denotes is the
        # same however the value was *delivered*.
        for value in context.value_aliases.get(base, ()):
            resolved = _go_resolve_container_value(value, context, depth + 1)
            if resolved is not None:
                return resolved
        if "." in base:
            root, field = base.rsplit(".", 1)
            for holder in (context.canon(root), root):
                if holder in context.structs:
                    for field_name, value in context.structs[holder]:
                        if field_name == field:
                            return _go_resolve_container_value(value, context, depth + 1)
    # A selector on *any* base (``outer{…}.inner``): evaluate the receiver as a
    # container and follow the field, so a nested composite literal resolves just
    # like a named struct variable.
    selector = _go_split_trailing_selector(slice_)
    if selector is not None:
        receiver, field = selector
        container = _go_resolve_container_value(receiver, context, depth + 1)
        if container is not None and container[0] == "struct":
            for field_name, value in container[1]:
                if field_name == field:
                    return _go_resolve_container_value(value, context, depth + 1)
    return None


def _go_split_trailing_selector(tokens_slice):
    """``base.field`` -> ``(base, field)`` when the expression ends in a selector.

    The base is returned as raw tokens rather than a dotted name so a selector on
    *any* expression -- a composite literal (``registry{fn: spawn}.fn``), a call
    or another selector -- is decomposed the same way as ``r.fn``.
    """

    if (
        len(tokens_slice) >= 2
        and tokens_slice[-2] == ("op", ".")
        and tokens_slice[-1][0] == "ident"
    ):
        return tokens_slice[:-2], tokens_slice[-1][1]
    return None


def _go_split_trailing_call(tokens_slice):
    """``callee(args)`` -> ``(callee, arguments)`` when a balanced ``(…)`` closes
    the expression; ``None`` otherwise."""

    if not tokens_slice or tokens_slice[-1] != ("op", ")"):
        return None
    depth = 0
    cursor = len(tokens_slice) - 1
    while cursor >= 0:
        token = tokens_slice[cursor]
        if token == ("op", ")"):
            depth += 1
        elif token == ("op", "("):
            depth -= 1
            if depth == 0:
                break
        cursor -= 1
    if cursor <= 0:
        return None
    arguments, _ = _go_call_arguments(tokens_slice, cursor)
    return tokens_slice[:cursor], arguments


def _go_split_trailing_type_assertion(tokens_slice):
    """``base.(Type)`` -> ``base`` tokens (the asserted value keeps the function
    value it boxes); ``None`` otherwise."""

    if not tokens_slice or tokens_slice[-1] != ("op", ")"):
        return None
    depth = 0
    cursor = len(tokens_slice) - 1
    while cursor >= 0:
        token = tokens_slice[cursor]
        if token == ("op", ")"):
            depth += 1
        elif token == ("op", "("):
            depth -= 1
            if depth == 0:
                break
        cursor -= 1
    if cursor <= 0 or tokens_slice[cursor - 1] != ("op", "."):
        return None
    return tokens_slice[:cursor - 1]


def _go_split_leading_receive(tokens_slice):
    """``<-ch`` -> the channel operand tokens; ``None`` when not a receive.

    Go's ``<-`` is a channel operator: a *prefix* receive (``x := <-ch``,
    ``runone(<-ch, …)``) draws a value out of the channel, so the operand
    evaluates to the same value the channel carries.
    """

    if tokens_slice and tokens_slice[0] == ("op", "<-"):
        operand = tokens_slice[1:]
        if operand:
            return operand
    return None


def _go_selector_name_before(tokens, index):
    """Dotted ``ident(.ident)*`` immediately ending at ``index`` (exclusive)."""

    cursor = index
    parts: list[str] = []
    while (
        cursor >= 1
        and tokens[cursor - 1][0] == "ident"
        and tokens[cursor - 1][1] not in _GO_KEYWORDS
    ):
        parts.append(tokens[cursor - 1][1])
        cursor -= 1
        if cursor >= 1 and tokens[cursor - 1] == ("op", "."):
            cursor -= 1
            continue
        break
    if not parts:
        return None, index
    parts.reverse()
    return ".".join(parts), cursor


def _go_index_key(index_expr) -> str:
    """Canonical textual key for a container index expression."""

    key, _ = _go_expression(index_expr, 0, {})
    if isinstance(key, str):
        return key
    if _is_num(key):
        return key[1]
    return "*"


def _go_expr_start(tokens, index):
    """Start index of the primary expression ending just before ``index``.

    A channel's send/receive side may be *any* addressable expression -- a name,
    a field (``h.ch``), an index (``m["a"]``) -- so the left operand is delimited
    by the enclosing statement rather than assumed to be a single identifier.
    """

    depth = 0
    cursor = index - 1
    delimiters = {
        ("op", ";"),
        ("op", ","),
        ("op", ":"),
        ("op", "="),
        ("op", ":="),
        ("op", "<-"),
        ("op", "+="),
        ("op", "-="),
    }
    keywords = {"go", "case", "return", "defer", "var", "const", "type", "func"}
    while cursor >= 0:
        token = tokens[cursor]
        if token[0] == "op" and token[1] in ")]}":
            depth += 1
        elif token[0] == "op" and token[1] in "([{":
            if depth == 0:
                return cursor + 1
            depth -= 1
        elif depth == 0 and (token[0] == "nl" or token in delimiters):
            return cursor + 1
        elif depth == 0 and token[0] == "ident" and token[1] in keywords:
            return cursor + 1
        cursor -= 1
    return 0


def _go_trim_trailing_colon(tokens_slice):
    """Drop a trailing ``:`` (a ``select`` case label) from a value slice."""

    cursor = len(tokens_slice)
    while cursor > 0 and tokens_slice[cursor - 1] == ("op", ":"):
        cursor -= 1
    return tokens_slice[:cursor]


def _go_slot_key(tokens_slice, context):
    """Canonical key of an addressable value *slot*, or ``None``.

    A slot is a name (``ch``), a field (``h.ch``) or an index (``m["a"]``,
    ``fns[0]``) whose root is canonicalised through the name aliases, so the
    sender and the receiver of the same value compute the same key however each
    of them spelled it.
    """

    if not tokens_slice:
        return None
    slice_ = _go_strip_outer_parens(tokens_slice)
    if not slice_:
        return None
    index = _go_split_trailing_index(slice_)
    if index is not None:
        head, index_expr = index
        base = _go_slot_key(head, context)
        if base is None:
            return None
        return f"{base}[{_go_index_key(index_expr)}]"
    selector = _go_split_trailing_selector(slice_)
    if selector is not None:
        head, field = selector
        base = _go_slot_key(head, context)
        if base is None:
            return None
        return f"{base}.{field}"
    name, after = _go_dotted_name(slice_, 0)
    if name is not None and after == len(slice_):
        return context.canon(name)
    return None


def _go_channel_send(tokens, index, context):
    """``dst <- value`` at ``index`` -> ``(slot_key, value_tokens)``.

    A channel transports a value from the sender to any receiver, so it behaves
    as a container: the sent value is registered as the channel's element and a
    later ``<-dst`` resolves it just like ``container[i]``.  ``dst`` may be any
    addressable expression -- a name, a field, an index -- so ``m["a"] <- v`` and
    ``q.ch <- v`` are the same value flow as ``ch <- v``.  A prefix receive
    (``<-ch``) and the channel *type* spellings ``<-chan T`` / ``chan<- T`` are
    not sends and return ``None``.
    """

    if tokens[index] != ("op", "<-") or index == 0:
        return None
    if index + 1 >= len(tokens) or tokens[index + 1][0] == "nl":
        return None
    if tokens[index + 1] == ("ident", "chan"):
        return None
    start = _go_expr_start(tokens, index)
    lhs = tokens[start:index]
    if not lhs:
        return None
    key = _go_slot_key(lhs, context)
    if key is None:
        return None
    end = _go_statement_end(tokens, index + 1)
    value = _go_trim_trailing_colon(tokens[index + 1:end])
    if not value:
        return None
    return key, value


def _go_receive_container(channel_expr, context, depth):
    """Container value the channel ``channel_expr`` carries.

    The channel slot is looked up by its canonical key first (``<-ch`` /
    ``<-h.ch`` / ``<-m["a"]``); otherwise the operand is evaluated as a container
    so a channel produced by a helper (``<-(pipe())``) still resolves.
    """

    key = _go_slot_key(channel_expr, context)
    if key is not None:
        if key in context.seqs:
            return "seq", context.seqs[key]
        if key in context.maps:
            return "map", context.maps[key]
    return _go_resolve_container_value(channel_expr, context, depth + 1)


def _go_func_literal_body(tokens_slice):
    """``(params, body)`` of a ``func(…)[results] { … }`` literal, or ``None``."""

    if not tokens_slice or tokens_slice[0] != ("ident", "func"):
        return None
    cursor = 1
    params: list[str] = []
    if cursor < len(tokens_slice) and tokens_slice[cursor] == ("op", "("):
        close = _go_matching_paren(tokens_slice, cursor)
        params = _go_parameter_list(tokens_slice[cursor + 1:close])
        cursor = close + 1
    while cursor < len(tokens_slice) and tokens_slice[cursor] != ("op", "{"):
        cursor += 1
    if cursor >= len(tokens_slice):
        return None
    end = _go_matching_brace(tokens_slice, cursor)
    return params, tokens_slice[cursor + 1:end]


def _go_func_literal_factory(tokens_slice, context, depth: int = 0) -> bool:
    """True when a ``func`` literal's body *returns a wrapper* function value.

    Such a literal is a *value factory* (``func() func(string) error { return
    spawn }``): calling the value it is bound to yields the wrapper, exactly
    like a declared ``pick()`` helper.
    """

    parts = _go_func_literal_body(tokens_slice)
    if parts is None:
        return False
    _, body = parts
    return _go_body_returns_wrapper(body, context)


def _go_resolve_receive_wrapper(channel_expr, context, depth: int) -> bool:
    """Resolve ``<-ch``: any value the channel carries that is a wrapper."""

    container = _go_receive_container(channel_expr, context, depth)
    if container is None:
        return False
    kind, data = container
    if kind == "seq":
        return any(_go_resolve_wrapper_value(value, context, depth + 1) for value in data)
    if kind == "map":
        return any(
            _go_resolve_wrapper_value(value, context, depth + 1) for _, value in data
        )
    return False


def _go_resolve_receive_factory(channel_expr, context, depth: int) -> bool:
    """Resolve ``<-ch`` to a *factory* function value the channel carries."""

    container = _go_receive_container(channel_expr, context, depth)
    if container is None:
        return False
    kind, data = container
    if kind == "seq":
        return any(_go_resolve_factory_value(value, context, depth + 1) for value in data)
    if kind == "map":
        return any(
            _go_resolve_factory_value(value, context, depth + 1) for _, value in data
        )
    return False


def _go_resolve_factory_value(tokens_slice, context, depth: int = 0) -> bool:
    """Resolve an expression to a *factory* function value.

    A factory is a function value whose *call* yields a wrapper (a declared
    helper, a method factory, a local closure, or a channel element that is
    itself a factory).  The resolver is shape-independent for the same reason
    as :func:`_go_resolve_wrapper_value`: a value can be produced and delivered
    however the attacker spells it.
    """

    if depth > 12 or not tokens_slice:
        return False
    slice_ = _go_strip_outer_parens(tokens_slice)
    if not slice_:
        return False
    if slice_[0] == ("ident", "func") and _go_func_literal_factory(slice_, context, depth):
        return True
    receive = _go_split_leading_receive(slice_)
    if receive is not None:
        return _go_resolve_receive_factory(receive, context, depth + 1)
    # ``container[i]()``: the element of a container may itself be a factory
    # (``fns := []func() func(...) {…}; fns[0]()``), so addressing a factory
    # container is resolved exactly like addressing a wrapper container.
    split = _go_split_trailing_index(slice_)
    if split is not None:
        head, index_expr = split
        container = _go_resolve_container_value(head, context, depth + 1)
        if container is None:
            return False
        kind, data = container
        return _go_literal_lookup(
            kind, data, index_expr, context, depth, resolve=_go_resolve_factory_value
        )
    selector = _go_split_trailing_selector(slice_)
    if selector is not None:
        base, field = selector
        if field in context.factories or field.rsplit(".", 1)[-1] in context.factories:
            return True
        container = _go_resolve_container_value(base, context, depth + 1)
        if container is not None and container[0] == "struct":
            for field_name, value in container[1]:
                if field_name == field:
                    return _go_resolve_factory_value(value, context, depth + 1)
        return False
    name, after = _go_dotted_name(slice_, 0)
    if name is not None and after == len(slice_):
        return name in context.factories or name.rsplit(".", 1)[-1] in context.factories
    return False


def _go_resolve_selector_wrapper(base, field, context, depth) -> bool:
    """Resolve ``base.field`` to a wrapper function value.

    ``field`` may be a wrapper *method* selected on any receiver
    (``reg{}.launch``), a struct field whose value is a wrapper
    (``registry{fn: spawn}.fn``), or the tail of a fully qualified name that was
    already bound to a wrapper (``h.f = spawn`` then ``runone(h.f, …)``).  The
    base is evaluated as a value so a composite-literal receiver is handled
    exactly like a named one.
    """

    base_name, after = _go_dotted_name(base, 0)
    if base_name is not None and after == len(base):
        dotted = f"{base_name}.{field}"
        if dotted in context.names or field in context.names:
            return True
    elif field in context.names:
        return True
    container = _go_resolve_container_value(base, context, depth + 1)
    if container is not None and container[0] == "struct":
        for field_name, value in container[1]:
            if field_name == field:
                return _go_resolve_wrapper_value(value, context, depth + 1)
    return False


def _go_resolve_call_wrapper(callee, arguments, context, depth) -> bool:
    """Resolve ``callee(args)`` to a wrapper function value.

    Covered shapes: a helper whose return value is a wrapper (``pick()``); a
    *factory method* invoked on any receiver (``reg{}.pick()``); and a type
    conversion of a function value (``launcher(spawn)`` / ``any(spawn)``), where
    the converted argument is preserved because a conversion runs no user code.
    """

    callee = _go_strip_outer_parens(callee)
    if not callee:
        return False
    if callee[0] == ("ident", "func") and not any(token == ("op", "{") for token in callee):
        # Conversion to an (unnamed) function type -- ``func(…)…(x)`` -- preserves
        # the converted value, exactly like a named type conversion.
        return any(
            _go_resolve_wrapper_value(argument, context, depth + 1)
            for argument in arguments
        )
    # The callee may be a factory value however it is spelled -- a declared
    # helper, a method factory, a local closure or a channel-received factory.
    if _go_resolve_factory_value(callee, context, depth + 1):
        return True
    name, after = _go_dotted_name(callee, 0)
    if name is not None and after == len(callee):
        short = name.rsplit(".", 1)[-1]
        if name in context.type_names or short in context.type_names:
            return any(
                _go_resolve_wrapper_value(argument, context, depth + 1)
                for argument in arguments
            )
    return False


def _go_resolve_wrapper_value(tokens_slice, context, depth: int = 0) -> bool:
    """Shape-independent resolution of an expression to a wrapper function value.

    The evaluation does not depend on how the value is *written*: it follows a
    named wrapper / method value / struct field, an inline ``func`` literal, a
    slice/array/map element (``handlers[0]``), an ``append``-built container, a
    ``make`` + index-assignment container, a container produced by a factory
    (``table()[0]``), the return of a function-value factory (``pick()``), a
    *type conversion* / assertion of a function value (``launcher(spawn)`` /
    ``box.(func(string) error)``) and a selector on *any* base -- a
    composite-literal receiver (``reg{}.launch``) or field
    (``registry{fn: spawn}.fn``) -- recursing through each of them.
    """

    if depth > 12 or not tokens_slice:
        return False
    slice_ = _go_strip_outer_parens(tokens_slice)
    if not slice_:
        return False
    if slice_[0] == ("ident", "func"):
        if _go_func_literal_wrapper(slice_, context.names):
            return True
        # A pure function *type* has no body and can only be the callee of a
        # conversion (``func(…)…(x)``); fall through so the call handling below
        # treats it as the value-preserving conversion it is.
    # ``<-ch``: a channel transports a value to its receiver, so the received
    # value is the channel's element, resolved like ``container[i]``.
    receive = _go_split_leading_receive(slice_)
    if receive is not None:
        return _go_resolve_receive_wrapper(receive, context, depth)
    # ``x.(func(…)…)``: the assertion yields the same function value it boxes.
    asserted = _go_split_trailing_type_assertion(slice_)
    if asserted is not None:
        return _go_resolve_wrapper_value(asserted, context, depth + 1)
    # A trailing index reduces a container *value* (however produced -- literal,
    # ``make`` + assignment, ``append``, factory return or alias) to its element.
    split = _go_split_trailing_index(slice_)
    if split is not None:
        head, index_expr = split
        container = _go_resolve_container_value(head, context, depth + 1)
        if container is None:
            return False
        kind, data = container
        return _go_literal_lookup(kind, data, index_expr, context, depth)
    # A trailing call: a factory return, a factory method, or a type conversion.
    call = _go_split_trailing_call(slice_)
    if call is not None:
        return _go_resolve_call_wrapper(call[0], call[1], context, depth)
    # A trailing selector on *any* base: a wrapper method name or a struct field.
    selector = _go_split_trailing_selector(slice_)
    if selector is not None:
        return _go_resolve_selector_wrapper(selector[0], selector[1], context, depth)
    base, after = _go_dotted_name(slice_, 0)
    if base is not None and after == len(slice_):
        return base in context.names or base.rsplit(".", 1)[-1] in context.names
    return False


def _go_append_elements(target, value):
    """Raw element slices ``append(base, …)`` adds when assigned to ``target``."""
    name, paren = _go_dotted_call(value, 0)
    if name != "append" or _go_matching_paren(value, paren) != len(value) - 1:
        return None
    arguments, _ = _go_call_arguments(value, paren)
    if not arguments:
        return None
    base, extras = arguments[0], arguments[1:]
    if len(base) == 1 and base[0][0] == "ident" and base[0][1] == target:
        return extras
    literal = _go_composite_literal(base, 0)
    if literal is not None and literal[2] == len(base) and literal[0] == "seq":
        return list(literal[1]) + extras
    return None


def _go_range_loop_vars(tokens, operator_index):
    """Loop variables of a ``for … := range …`` statement, left to right."""

    names: list[str] = []
    cursor = operator_index - 1
    while cursor >= 0:
        token = tokens[cursor]
        if token == ("ident", "for"):
            break
        if token[0] == "ident" and token[1] not in _GO_KEYWORDS and token[1] != "_":
            names.append(token[1])
        cursor -= 1
    names.reverse()
    return names


def _go_body_returns_wrapper(body, context) -> bool:
    """True when a helper's ``return`` yields a wrapper function value."""

    index = 0
    while index < len(body):
        if body[index] == ("ident", "return"):
            end = _go_statement_end(body, index + 1)
            if _go_resolve_wrapper_value(body[index + 1:end], context):
                return True
            index = end + 1
            continue
        index += 1
    return False


def _go_body_return_container(body, context):
    """Return-value token slice of the first ``return`` yielding a container.

    A helper such as ``func table() []func(string) error { return []func(…){} }``
    is a *container* factory; storing its return value makes ``table()[0]``
    resolve exactly like a directly named container element.
    """

    index = 0
    while index < len(body):
        if body[index] == ("ident", "return"):
            end = _go_statement_end(body, index + 1)
            value = body[index + 1:end]
            if _go_resolve_container_value(value, context) is not None:
                return value
            index = end + 1
            continue
        index += 1
    return None


def _go_is_wrapper_value(tokens_slice, wrapper_functions, context=None) -> bool:
    """True when an expression denotes a wrapper function *value*.

    Kept as a thin adapter over :func:`_go_resolve_wrapper_value` for callers that
    only hold the flat ``wrapper_functions`` set; the tree builder passes a full
    :class:`_GoWrapperContext` so container / factory / field spellings resolve
    too.
    """

    if context is None:
        context = _GoWrapperContext(wrapper_functions)
    return _go_resolve_wrapper_value(tokens_slice, context)


def _go_expression_opaque(tokens_slice, environment, opaque_functions, field_taint=None) -> bool:
    for index, token in enumerate(tokens_slice):
        if token[0] == "ident" and token[1] in environment:
            if index + 1 < len(tokens_slice) and tokens_slice[index + 1] == ("op", "."):
                # ``cfg.EnginePath`` extracts a struct field: a *configuration*
                # read does not carry taint through a field, but an
                # environment-injected / decoded payload parked in a field does.
                if field_taint and token[1] in field_taint:
                    return True
                continue
            return True
    for name, _ in _go_call_sites(tokens_slice):
        if (
            name in _GO_OPAQUE_CALLS
            or name.rsplit(".", 1)[-1] in _GO_OPAQUE_METHODS
            or name in opaque_functions
        ):
            return True
    return False


def _go_expression_field_opaque(tokens_slice, field_environment) -> bool:
    """True when an expression derives from a field-propagating opaque source.

    Environment-injected payloads (``os.Getenv`` / ``os.LookupEnv``) and decoded
    payloads keep their provenance when they are stored in a struct field; a raw
    file read is the task's declared configuration surface, so a config-driven
    engine path extracted from a field stays clean.  Direct file reads still
    taint ordinary data flow (``read -> Fields -> exec``).
    """

    for token in tokens_slice:
        if token[0] == "ident" and token[1] in field_environment:
            return True
    for name, _ in _go_call_sites(tokens_slice):
        method = name.rsplit(".", 1)[-1]
        if name in _GO_ENV_CALLS or name in _GO_DECODE_CALLS:
            return True
        if method in _GO_FIELD_METHODS:
            return True
    return False


def _go_body_has_opaque_source(body, opaque_functions) -> bool:
    for name, _ in _go_call_sites(body):
        if (
            name in _GO_OPAQUE_CALLS
            or name.rsplit(".", 1)[-1] in _GO_OPAQUE_METHODS
            or name in opaque_functions
        ):
            return True
    return False


def _go_embed_declared_names(text: str) -> list[str]:
    names: list[str] = []
    for match in re.finditer(r"(?m)^[ \t]*//go:embed[^\n]*\n", text):
        tail = text[match.end():]
        block = re.match(r"\s*var\s*\(([^)]*)\)", tail, flags=re.S)
        if block:
            for line in block.group(1).splitlines():
                fields = line.strip().split()
                if fields:
                    names.append(fields[0])
            continue
        single = re.match(r"\s*var\s+([A-Za-z_]\w*)", tail)
        if single:
            names.append(single.group(1))
    return names


def _go_embed_files(path: Path) -> list[Path]:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    collected: list[Path] = []
    directory = path.parent
    for match in re.finditer(r"(?m)^[ \t]*//go:embed[^\n]*$", text):
        for raw in match.group(0).split(None, 1)[1].split():
            pattern = raw.strip('"').strip("`")
            if pattern.startswith("all:"):
                pattern = pattern[4:]
            if not pattern or pattern.startswith("/") or ".." in pattern:
                continue
            if any(token in pattern for token in "*?["):
                candidates = sorted(directory.glob(pattern))
            else:
                candidate = directory / pattern
                candidates = [candidate]
            for candidate in candidates:
                if candidate.is_dir():
                    for hit in sorted(candidate.rglob("*")):
                        if hit.is_file() and not hit.name.startswith((".", "_")):
                            collected.append(hit)
                elif candidate.is_file():
                    collected.append(candidate)
    return collected


def _go_opaque_sink_hit(
    text: str,
    *,
    import_aliases: tuple[dict[str, str], set[str]] | None = None,
    tree_opaque_functions: set[str] | None = None,
    wrapper_functions: set[str] | None = None,
) -> bool:
    """Detect an opaque runtime payload flowing into a process/plugin sink.

    This is the source -> sink check the declarative rules cannot express: a
    payload materialised from a decoder, an embedded asset or a read is
    propagated across statements and reported when it names what will be
    executed.  Legitimate engine spawns pass a literal or a configuration
    value -- neither is an opaque payload -- so they stay clean.

    ``tree_opaque_functions`` carries the whole-tree opaque-function set so a
    payload decoded in one file (``decode.go``) and consumed by a sink in another
    (``legacy.go``) is still connected; ``import_aliases`` normalises aliased /
    dot-imported package qualifiers (``ex.Command`` -> ``exec.Command``).
    """

    mapping, dots = import_aliases or ({}, set())
    tokens = _go_rewrite_aliases(_go_tokens(text), mapping, dots)
    environment = set(_go_embed_declared_names(text))
    # Values that keep their provenance through a struct field (env / decoded /
    # embedded payloads).  A raw file read taints ordinary data flow but is the
    # task's configuration surface, so it is intentionally absent here.
    field_environment = set(environment)
    aliases: dict[str, str] = {}
    for index in range(len(tokens) - 1):
        if tokens[index][0] != "ident" or tokens[index + 1] not in {("op", ":="), ("op", "=")}:
            continue
        dotted, after = _go_dotted_name(tokens, index + 2)
        if (
            dotted in _GO_PROCESS_SINKS
            and after < len(tokens)
            and tokens[after] != ("op", "(")
        ):
            aliases[tokens[index][1]] = dotted
    bodies = _go_function_bodies(tokens)
    opaque_functions: set[str] = set(tree_opaque_functions or ())
    changed = True
    while changed:
        changed = False
        for name, body in bodies:
            if name in opaque_functions:
                continue
            if _go_body_has_opaque_source(body, opaque_functions):
                opaque_functions.add(name)
                changed = True
    changed = True
    while changed:
        changed = False
        for index in range(len(tokens)):
            token = tokens[index]
            if token[0] != "op" or token[1] not in {":=", "=", "+="}:
                continue
            names: list[str] = []
            saw_dot = False
            cursor = index - 1
            valid = True
            while cursor >= 0:
                previous = tokens[cursor]
                if previous[0] == "nl" or (previous[0] == "op" and previous[1] in ";{}()"):
                    break
                if previous[0] == "ident":
                    names.append(previous[1])
                    cursor -= 1
                    continue
                if previous == ("op", ","):
                    cursor -= 1
                    continue
                if previous == ("op", "."):
                    # A selector target (``p.Program = rhs``) parks the value in
                    # a struct field: keep the selector root (``p``) tainted so a
                    # later ``p.Program`` read still reaches the sink.
                    saw_dot = True
                    cursor -= 1
                    continue
                valid = False
                break
            if not valid or not names:
                continue
            if saw_dot:
                names = [names[-1]]
            end = _go_statement_end(tokens, index + 1)
            rhs = tokens[index + 1:end]
            if _go_expression_opaque(rhs, environment, opaque_functions, field_environment):
                for name in names:
                    if name not in environment:
                        environment.add(name)
                        changed = True
            if _go_expression_field_opaque(rhs, field_environment):
                for name in names:
                    if name not in field_environment:
                        field_environment.add(name)
                        changed = True
        for name, paren in _go_call_sites(tokens):
            if name.rsplit(".", 1)[-1] not in {"WriteFile", "Create", "WriteString"}:
                continue
            arguments, _ = _go_call_arguments(tokens, paren)
            if len(arguments) < 2:
                continue
            if not _go_expression_opaque(arguments[1], environment, opaque_functions):
                continue
            for token in arguments[0]:
                if token[0] == "ident" and token[1] not in environment:
                    environment.add(token[1])
                    changed = True
    for position in range(len(tokens)):
        name, paren = _go_dotted_call(tokens, position)
        if name is None:
            continue
        if wrapper_functions and (
            name in wrapper_functions
            or name.rsplit(".", 1)[-1] in wrapper_functions
        ):
            # A local helper that forwards its argument into a sink (directly, or
            # through a function value the caller binds): an opaque argument
            # handed to it reaches the process/plugin sink.
            arguments, _ = _go_call_arguments(tokens, paren)
            if any(
                _go_expression_opaque(argument, environment, opaque_functions, field_environment)
                for argument in arguments
            ):
                return True
            continue
        sink = name if name in _GO_PROCESS_SINKS else aliases.get(name)
        if sink is None:
            continue
        arguments, _ = _go_call_arguments(tokens, paren)
        index_arg = _GO_PROCESS_SINKS[sink]
        if index_arg < len(arguments) and _go_expression_opaque(
            arguments[index_arg], environment, opaque_functions, field_environment
        ):
            return True
    # ``exec.Cmd{Path: …}`` names the program without an ``exec.Command`` call.
    for position in range(len(tokens)):
        struct_name, after = _go_dotted_name(tokens, position)
        if struct_name is None or not struct_name.endswith("Cmd"):
            continue
        if after >= len(tokens) or tokens[after] != ("op", "{"):
            continue
        cursor = after + 1
        while cursor + 1 < len(tokens) and tokens[cursor] != ("op", "}"):
            if (
                tokens[cursor][0] == "ident"
                and tokens[cursor][1] in {"Path", "Args"}
                and tokens[cursor + 1] == ("op", ":")
            ):
                end = _go_argument_end(tokens, cursor + 2)
                if _go_expression_opaque(tokens[cursor + 2:end], environment, opaque_functions, field_environment):
                    return True
            cursor += 1
    return False


def _go_argument_end(tokens, index: int) -> int:
    """End index of a composite-literal field value (top-level ``,`` or ``}``)."""

    depth = 0
    while index < len(tokens):
        token = tokens[index]
        if token[0] == "op" and token[1] in "([{":
            depth += 1
        elif token[0] == "op" and token[1] in ")]}":
            if depth == 0:
                return index
            depth -= 1
        elif depth == 0 and (token == ("op", ",") or token[0] == "nl"):
            return index
        index += 1
    return index


_FOLDED_CACHE: dict[tuple[str, int, int], list[str]] = {}


class _GoTreeContext:
    """Whole-tree Go model backing the cross-file source->sink scan.

    A deceptive fallback can split the payload across files (decode in one,
    process sink in another) and rename imported packages; both are invisible to
    a purely per-file scan.  The context is built once per candidate tree and
    threads the tree-wide function returns / opaque-function set / import aliases
    into every per-file match.
    """

    __slots__ = (
        "returns",
        "opaque_functions",
        "wrapper_functions",
        "has_decoder",
        "aliases",
        "folded_cache",
        "runtime_cache",
    )

    def __init__(
        self,
        returns: dict[str, object],
        opaque_functions: set[str],
        wrapper_functions: set[str],
        has_decoder: bool,
        aliases: dict[str, tuple[dict[str, str], set[str]]],
    ) -> None:
        self.returns = returns
        self.opaque_functions = opaque_functions
        self.wrapper_functions = wrapper_functions
        self.has_decoder = has_decoder
        self.aliases = aliases
        self.folded_cache: dict[tuple[str, int, int], list[str]] = {}
        self.runtime_cache: dict[tuple[str, int, int], bool] = {}

    def import_aliases(self, path: Path) -> tuple[dict[str, str], set[str]]:
        return self.aliases.get(str(path.resolve()), ({}, set()))


def _go_source_files(root: Path) -> list[Path]:
    collected: list[Path] = []
    for path in sorted(root.rglob("*.go")):
        if _eligible(root, path):
            collected.append(path)
    return collected


def _go_tree_context(root: Path) -> _GoTreeContext:
    returns: dict[str, object] = {}
    bodies: list[tuple[str, list]] = []
    declarations: list[tuple[str, list[str], list]] = []
    aliases: dict[str, tuple[dict[str, str], set[str]]] = {}
    call_sites: list[tuple[str, list[list]]] = []
    value_aliases: list[tuple[str, list]] = []
    index_writes: list[tuple[str, list, list]] = []
    channel_send_sites: list[tuple[list, int]] = []
    variadic_params: dict[str, tuple[list[str], int]] = {}
    range_bindings: list[tuple[list[str], list]] = []
    type_names: set[str] = set()
    has_decoder = False
    for path in _go_source_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        mapping, dots = _go_import_aliases(text)
        if mapping or dots:
            aliases[str(path.resolve())] = (mapping, dots)
        tokens = _go_rewrite_aliases(_go_tokens(text), mapping, dots)
        if any(name in _GO_DECODE_CALLS for name, _ in _go_call_sites(tokens)):
            has_decoder = True
        for name, value in _go_function_return_values(tokens).items():
            returns.setdefault(name, value)
        for name, params, body in _go_function_declarations_ordered(tokens):
            bodies.append((name, body))
            declarations.append((name, params, body))
        variadic_params.update(_go_function_variadic_params(tokens))
        type_names.update(_go_declared_type_names(tokens))
        for position in range(len(tokens) - 1):
            if tokens[position][0] != "ident":
                continue
            name, paren = _go_dotted_call(tokens, position)
            if name is None:
                continue
            # ``func runone(…)`` opens with the callee name right after the ``func``
            # keyword: that is a declaration, not a call site.
            if position > 0 and tokens[position - 1] == ("ident", "func"):
                continue
            arguments, _ = _go_call_arguments(tokens, paren)
            call_sites.append((name, arguments))
        # ``fn := spawn`` / ``h.handler = spawn`` bind a function *value* to an
        # identifier or field; remember the binding so a later
        # ``runone(fn, env)`` (or ``runone(h.handler, env)``) resolves it.
        for index in range(len(tokens) - 2):
            if tokens[index][0] != "ident":
                continue
            target = tokens[index][1]
            if tokens[index + 1] in {("op", ":="), ("op", "=")}:
                after = index + 2
            elif (
                tokens[index + 1] == ("op", ".")
                and index + 3 < len(tokens)
                and tokens[index + 2][0] == "ident"
                and tokens[index + 3] in {("op", ":="), ("op", "=")}
            ):
                target = f"{target}.{tokens[index + 2][1]}"
                after = index + 4
            else:
                continue
            end = _go_statement_end(tokens, after)
            # A ``select`` communication clause (``case f = <-ch:``) leaves the
            # case label's ``:`` on the value; drop it so the received value
            # resolves like any other ``f = <-ch`` binding.
            value_aliases.append((target, _go_trim_trailing_colon(tokens[after:end])))
        # ``var handlers []func(string) error = []func(string) error{spawn}``
        # declares a container with the type sitting between the name and ``=``;
        # register the *declared* name so a later ``handlers[0]`` still resolves
        # (the generic scan above would otherwise bind the type token instead).
        for index in range(len(tokens) - 2):
            if tokens[index] != ("ident", "var"):
                continue
            name = tokens[index + 1]
            if name[0] != "ident" or name[1] in _GO_KEYWORDS:
                continue
            statement_end = _go_statement_end(tokens, index + 1)
            for cursor in range(index + 2, statement_end):
                if tokens[cursor] == ("op", "="):
                    value_aliases.append((name[1], tokens[cursor + 1:statement_end]))
                    break
        # ``handlers[0] = spawn`` / ``m["run"] = spawn`` (also ``r.h[i] = spawn``)
        # *construct* a container element in place: the left value is an index
        # expression, not a plain identifier, so it is collected separately and
        # registered as an element of the container (seq position / map key),
        # independent of how the container itself was produced (``make``, literal,
        # factory return, ``var`` declaration, …).
        for index in range(len(tokens) - 1):
            if tokens[index][0] != "ident":
                continue
            base, after = _go_dotted_name(tokens, index)
            if base is None or after >= len(tokens) or tokens[after] != ("op", "["):
                continue
            close = _go_matching_bracket(tokens, after)
            if close + 1 >= len(tokens) or tokens[close + 1] != ("op", "="):
                continue
            index_writes.append(
                (base, tokens[after + 1:close], tokens[close + 2:_go_statement_end(tokens, close + 2)])
            )
        # ``dst <- value``: a channel transports the value from the sender to its
        # receiver, so register it as the channel slot's element -- the send side
        # of the same value flow as an index write.  ``dst`` may be any
        # addressable expression (a name, a field, an index), so the sites are
        # collected raw and keyed once the name aliases are known (below).
        for index in range(len(tokens)):
            if tokens[index] == ("op", "<-"):
                channel_send_sites.append((tokens, index))
        # ``for _, fn := range handlers { runone(fn, env) }`` binds the loop
        # variable to the container's elements; remember the binding so the loop
        # variable resolves to a wrapper value just like a direct element access.
        for index in range(1, len(tokens) - 2):
            if tokens[index] not in {("op", ":="), ("op", "=")}:
                continue
            if tokens[index + 1] != ("ident", "range"):
                continue
            variables = _go_range_loop_vars(tokens, index)
            if not variables:
                continue
            start = index + 2
            if start < len(tokens) and (
                tokens[start] == ("op", "[") or tokens[start] == ("ident", "map")
            ):
                literal = _go_composite_literal(tokens, start)
                if literal is None:
                    continue
                if literal[0] == "seq":
                    range_bindings.append((variables, list(literal[1])))
                elif literal[0] == "map":
                    range_bindings.append(
                        (variables, [value for _, value in literal[1]])
                    )
                continue
            container, _ = _go_dotted_name(tokens, start)
            if container is not None:
                range_bindings.append((variables, container))

    # Shape-independent function-value environment.  Containers of wrappers are
    # registered *by value* however they are produced -- composite literal,
    # ``append`` accumulation, ``make`` + index assignment, map index assignment,
    # a factory return (``table()``) or a container alias -- so an element access
    # ``handlers[0]`` / ``table()[0]`` / ``m[k]`` resolves to the element's value
    # instead of only working for one written producer shape.
    wrapper_functions: set[str] = set()
    context = _GoWrapperContext(wrapper_functions)
    context.type_names |= type_names

    # Name aliasing: ``x := y`` (and the pointer form ``x := &y``) makes two
    # slots the *same* slot, so a value parked through one spelling is seen
    # through the other (``relay := ch`` then ``relay <- v`` / ``<-ch``;
    # ``q := &h`` then ``q.ch <- v`` / ``<-h.ch``).
    def _union(left, right):
        root_left, root_right = context.find(left), context.find(right)
        if root_left != root_right:
            context.alias_parent[root_right] = root_left

    for target, value in value_aliases:
        stripped = _go_strip_outer_parens(value)
        alias_name, after = _go_dotted_name(stripped, 0)
        if alias_name is not None and after == len(stripped):
            _union(target, alias_name)
    for target, value in value_aliases:
        context.value_aliases.setdefault(target, []).append(value)

    def _has_container(target):
        key = context.canon(target)
        return key in context.seqs or key in context.maps or key in context.structs

    def _register_container(target, kind, data):
        target = context.canon(target)
        if kind == "seq":
            store = context.seqs.setdefault(target, [])
        elif kind == "map":
            store = context.maps.setdefault(target, [])
        elif kind == "struct":
            store = context.structs.setdefault(target, [])
        else:
            return False
        before = len(store)
        store.extend(data)
        return len(store) != before

    # (1) value aliases: composite literal / ``append`` / ``make`` / named alias.
    for target, value in value_aliases:
        if _has_container(target):
            continue
        appended = _go_append_elements(target, value)
        if appended is not None:
            _register_container(target, "seq", appended)
            continue
        container = _go_resolve_container_value(value, context)
        if container is not None:
            _register_container(target, container[0], container[1])
    # (2) in-place index construction ``container[idx] = value``.
    for target, index_expr, value in index_writes:
        target = context.canon(target)
        if target in context.maps:
            kind = "map"
        elif target in context.seqs:
            kind = "seq"
        elif target in context.structs:
            kind = "struct"
        else:
            key, _ = _go_expression(index_expr, 0, {})
            kind = "map" if isinstance(key, str) else "seq"
        if kind == "map":
            context.maps.setdefault(target, []).append((index_expr, value))
        else:
            context.seqs.setdefault(target, []).append(value)
    # (2b) channel sends ``dst <- value``: the channel is the receiving end's
    #      container, so register each sent value as an element of the channel
    #      slot.  A later ``<-dst`` reads it back like any container element, and
    #      a channel that carries a factory is resolved through the same flow.
    for tokens, index in channel_send_sites:
        send = _go_channel_send(tokens, index, context)
        if send is None:
            continue
        channel, value = send
        store = context.seqs.setdefault(channel, [])
        if value not in store:
            store.append(value)
    # (3) container factories (``func table() []func(…) { return … }``) and
    #     container aliases that depend on a factory return; iterate to a fixed
    #     point since one factory may return another's result.
    changed = True
    while changed:
        changed = False
        for name, params, body in declarations:
            if name in context.container_factories:
                continue
            returned = _go_body_return_container(body, context)
            if returned is not None:
                context.container_factories[name] = returned
                changed = True
        for target, value in value_aliases:
            if _has_container(target):
                continue
            container = _go_resolve_container_value(value, context)
            if container is not None:
                _register_container(target, container[0], container[1])
                changed = True
    # Resolve ``for … := range <container>`` bindings now that the containers are
    # known: the loop variable denotes the container's elements, so an element
    # that is a wrapper makes the loop variable one too.
    for variables, source in range_bindings:
        if isinstance(source, str):
            if source in context.seqs:
                elements = context.seqs[source]
            elif source in context.maps:
                elements = [value for _, value in context.maps[source]]
            elif source in context.structs:
                elements = [value for _, value in context.structs[source]]
            elif source in context.container_factories:
                resolved = _go_resolve_container_value(
                    context.container_factories[source], context
                )
                if resolved is None:
                    continue
                elements = (
                    list(resolved[1])
                    if resolved[0] == "seq"
                    else [value for _, value in resolved[1]]
                )
            else:
                continue
        else:
            elements = source
        if not elements:
            continue
        for variable in variables:
            for element in elements:
                value_aliases.append((variable, list(element)))
    # Wrapper closure: a helper that forwards a parameter to *another* wrapper
    # (``mid(p) { return spawn(p) }``) reaches the process just as directly as one
    # that calls the sink itself, so iterate to a fixed point instead of only
    # recognising the one-level ``exec.Command(param)`` shape.  Function *values*
    # are part of the same closure: a higher-order helper reaches the sink once
    # its function-valued parameter resolves to a wrapper, which also works when
    # the value is passed through further parameters (``outer`` -> ``runone``),
    # parked in an alias/field, held in a container or returned by a factory.
    changed = True
    while changed:
        changed = False
        for target, value in value_aliases:
            if target in wrapper_functions:
                continue
            if _go_resolve_wrapper_value(value, context):
                wrapper_functions.add(target)
                changed = True
        # A *local* function value whose body returns a wrapper is a factory too
        # (``makeLauncher := func() func(string) error { return spawn }``): the
        # declaration-only scan above would miss it, so register it from its
        # binding.  Calling the bound identifier then denotes a wrapper value.
        for target, value in value_aliases:
            if target in context.factories:
                continue
            if _go_func_literal_factory(value, context):
                context.factories.add(target)
                changed = True
        # A variadic parameter (``fns ...func(...)``) is a *sequence* the caller
        # fills with the trailing arguments, so ``fns[0]`` / ``return fns[0]``
        # resolve through the same container value flow as any other element
        # (``gather(spawn)`` yields the launcher; ``runone(env, spawn)`` reaches
        # the sink through ``fns[0](p)``).
        for name, params, body in declarations:
            variadic = variadic_params.get(name)
            if variadic is None:
                continue
            ordered, var_index = variadic
            if var_index < 0 or var_index >= len(ordered):
                continue
            variable = context.canon(ordered[var_index])
            store = context.seqs.setdefault(variable, [])
            for callee, arguments in call_sites:
                if callee != name and callee.rsplit(".", 1)[-1] != name:
                    continue
                for argument in arguments[var_index:]:
                    if not argument:
                        continue
                    # ``xs...`` spreads a slice into the variadic parameter; the
                    # ellipsis may surface as three ``.`` tokens.
                    if argument[-1] == ("op", "...") or argument[-3:] == [
                        ("op", "."),
                        ("op", "."),
                        ("op", "."),
                    ]:
                        base_expr = (
                            argument[:-1]
                            if argument[-1] == ("op", "...")
                            else argument[:-3]
                        )
                        spread = _go_resolve_container_value(base_expr, context)
                        extras = (
                            list(spread[1])
                            if spread is not None and spread[0] == "seq"
                            else []
                        )
                    else:
                        extras = [list(argument)]
                    for element in extras:
                        if list(element) not in store:
                            store.append(list(element))
                            changed = True
        for name, params, body in declarations:
            # A helper whose return value is a wrapper function value acts as a
            # factory (``pick()``); a call to it denotes a wrapper value.
            if name not in context.factories and _go_body_returns_wrapper(body, context):
                context.factories.add(name)
                changed = True
            # A parameter that receives a wrapper function value at any call site
            # is itself a wrapper value; binding it keeps a higher-order chain
            # (``outer(f, p) { return runone(f, p) }``) connected end to end.
            for callee, arguments in call_sites:
                if callee != name and callee.rsplit(".", 1)[-1] != name:
                    continue
                for position, param in enumerate(params):
                    if param in wrapper_functions or position >= len(arguments):
                        continue
                    if _go_resolve_wrapper_value(arguments[position], context):
                        wrapper_functions.add(param)
                        changed = True
            if name in wrapper_functions:
                continue
            if _go_params_reach_sink(
                body,
                set(params),
                wrapper_functions,
                is_wrapper=lambda expression: _go_resolve_wrapper_value(
                    expression, context
                ),
            ):
                wrapper_functions.add(name)
                changed = True
    opaque_functions: set[str] = set()
    changed = True
    while changed:
        changed = False
        for name, body in bodies:
            if name in opaque_functions:
                continue
            if _go_body_has_opaque_source(body, opaque_functions):
                opaque_functions.add(name)
                changed = True
    return _GoTreeContext(
        returns, opaque_functions, wrapper_functions, has_decoder, aliases
    )


_TREE_CACHE: dict[str, tuple[tuple, _GoTreeContext]] = {}


def _go_context(root: Path) -> _GoTreeContext:
    key = str(root.resolve())
    signature = tuple(
        (path.relative_to(root).as_posix(), path.stat().st_mtime_ns, path.stat().st_size)
        for path in _go_source_files(root)
        if _safe_stat(path)
    )
    cached = _TREE_CACHE.get(key)
    if cached is not None and cached[0] == signature:
        return cached[1]
    context = _go_tree_context(root)
    _TREE_CACHE[key] = (signature, context)
    return context


def _safe_stat(path: Path) -> bool:
    try:
        path.stat()
    except OSError:
        return False
    return True


def _folded_strings(path: Path, context: _GoTreeContext | None = None) -> list[str]:
    if path.suffix.casefold() not in {".go", ".rs", ".java", ".kt", ".cs", ".cpp", ".cc", ".c", ".h"}:
        return []
    try:
        stat = path.stat()
    except OSError:
        return []
    key = (str(path.resolve()), stat.st_mtime_ns, stat.st_size)
    cache = context.folded_cache if context is not None else _FOLDED_CACHE
    if key in cache:
        return cache[key]
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    values = _go_folded_strings(
        text,
        import_aliases=context.import_aliases(path) if context is not None else None,
        function_returns=context.returns if context is not None else None,
        has_decoder=context.has_decoder if context is not None else False,
    )
    cache[key] = values
    return values


def _folded_contains(path: Path, target: str, context: _GoTreeContext | None = None) -> bool:
    normalized = target.casefold().strip()
    if not normalized:
        return False
    for value in _folded_strings(path, context):
        folded = value.casefold()
        if normalized in folded or folded.strip() == normalized:
            return True
    return False


def _embedded_contains(path: Path, target: str) -> bool:
    """Scan the files a ``//go:embed`` directive pulls into the binary.

    A fallback payload must not become invisible by living in a non-``.go``
    asset (``//go:embed run.sh``); the embedded file is source that reaches the
    process at run time, so it is scanned with the same rules.
    """

    normalized = target.casefold().strip()
    if not normalized or path.suffix.casefold() != ".go":
        return False
    for embedded in _go_embed_files(path):
        try:
            text = embedded.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if normalized in _without_comments(text).casefold():
            return True
    return False


_RUNTIME_CACHE: dict[tuple[str, int, int], bool] = {}


def _runtime_fallback(path: Path, context: _GoTreeContext | None = None) -> bool:
    if path.suffix.casefold() != ".go":
        return False
    try:
        stat = path.stat()
    except OSError:
        return False
    key = (str(path.resolve()), stat.st_mtime_ns, stat.st_size)
    cache = context.runtime_cache if context is not None else _RUNTIME_CACHE
    if key in cache:
        return cache[key]
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    value = _go_opaque_sink_hit(
        text,
        import_aliases=context.import_aliases(path) if context is not None else None,
        tree_opaque_functions=context.opaque_functions if context is not None else None,
        wrapper_functions=context.wrapper_functions if context is not None else None,
    )
    cache[key] = value
    return value


def _dependency_contains(path: Path, target: str) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, Mapping):
        for field in (
            "dependencies",
            "devDependencies",
            "peerDependencies",
            "optionalDependencies",
            "require",
            "require-dev",
        ):
            entries = payload.get(field)
            if isinstance(entries, Mapping) and any(
                _target_matches(str(name), target) for name in entries
            ):
                return True
    dependency_lines = (
        re.compile(r"^\s*([A-Za-z0-9@._+/-]+)\s*(?:[<>=~!].*)?$"),
        re.compile(r"^\s*(?:gem|implementation|api|compileOnly|runtimeOnly)\s*[( ]\s*['\"]([^'\"]+)"),
        re.compile(r"^\s*([A-Za-z0-9_.+-]+)\s*=\s*['\"]?[0-9*^~<>=]"),
    )
    lines = _without_comments(text).splitlines()
    for line in lines:
        for pattern in dependency_lines:
            match = pattern.search(line)
            if match and _target_matches(match.group(1), target):
                return True
    # Go modules declare dependencies as ``require path version`` and inside
    # ``require ( ... )`` blocks; ``replace``/``exclude`` name a module too.
    # The generic line patterns above cannot see that shape, which previously
    # made the dependency rules vacuous for ``go.mod``.
    go_module = path.name == "go.mod" or any(
        line.lstrip().startswith(("module ", "require (")) for line in lines[:5]
    )
    if go_module:
        go_patterns = (
            re.compile(r"^\s*(?:require\s+)?([A-Za-z0-9][A-Za-z0-9._/\-]*)\s+v[0-9]"),
            re.compile(r"^\s*(?:replace|exclude)\s+([A-Za-z0-9][A-Za-z0-9._/\-]*)"),
        )
        for line in lines:
            for pattern in go_patterns:
                match = pattern.search(line)
                if match and _target_matches(match.group(1), target):
                    return True
    return False


def _import_contains(path: Path, target: str) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    candidates: list[str] = []
    patterns = (
        re.compile(r"\bfrom\s+([A-Za-z0-9_./@+-]+)\s+import\b"),
        re.compile(r"\bimport\s+([A-Za-z0-9_./@+-]+)"),
        re.compile(r"\bfrom\s*['\"]([^'\"]+)['\"]"),
        re.compile(r"\brequire\s*\(\s*['\"]([^'\"]+)['\"]\s*\)"),
        re.compile(r"\brequire\s+['\"]([^'\"]+)['\"]"),
        re.compile(r"\b(?:use|using)\s+([A-Za-z0-9_.\\]+)"),
    )
    for pattern in patterns:
        candidates.extend(
            match.group(1) for match in pattern.finditer(_without_comments(text))
        )
    return any(
        _target_matches(candidate, target)
        or _target_matches(re.split(r"[./\\]", candidate, maxsplit=1)[0], target)
        for candidate in candidates
    )


def _matches(
    root: Path,
    path: Path,
    *,
    kind: str,
    target: str,
    context: _GoTreeContext | None = None,
) -> bool:
    if kind == "literal":
        return (
            _literal_contains(path, target)
            or _folded_contains(path, target, context)
            or _embedded_contains(path, target)
        )
    if kind == "dependency":
        return _dependency_contains(path, target)
    if kind == "import":
        return _import_contains(path, target)
    if kind == "runtime":
        return _runtime_fallback(path, context)
    relative = path.relative_to(root).as_posix()
    return fnmatchcase(relative.casefold(), target.casefold()) or fnmatchcase(
        path.name.casefold(), target.casefold()
    )


def scan_residue_rules(
    root: Path, rules: object
) -> list[dict[str, str]]:
    if not isinstance(rules, Sequence) or isinstance(rules, (str, bytes)):
        return []
    hits: list[dict[str, str]] = []
    context: _GoTreeContext | None = None
    context_ready = False
    for row in rules:
        if not isinstance(row, Mapping):
            continue
        identifier = row.get("id")
        kind = row.get("kind")
        target = row.get("target")
        patterns = row.get("paths", [])
        if (
            not isinstance(identifier, str)
            or not identifier
            or not isinstance(kind, str)
            or not isinstance(target, str)
            or not target
            or not isinstance(patterns, Sequence)
            or isinstance(patterns, (str, bytes))
            or not patterns
            or any(
                not isinstance(item, str) or not _safe_relative(item)
                for item in patterns
            )
        ):
            raise ValueError(f"invalid residue rule: {identifier or '<unknown>'}")
        if kind not in _RULE_KINDS:
            continue
        if not context_ready:
            context = _go_context(root)
            context_ready = True
        for path in _paths(root, tuple(patterns)):
            matched = _matches(
                root, path, kind=str(kind), target=target, context=context
            )
            if matched:
                hits.append(
                    {
                        "path": path.relative_to(root).as_posix(),
                        "token": target,
                        "kind": str(kind),
                        "rule_id": identifier,
                    }
                )
    return hits


def classify_residue_rules(
    root: Path, rules: object
) -> list[dict[str, object]]:
    """Classify residue rules without treating unsupported syntax as a failure."""

    if not isinstance(rules, Sequence) or isinstance(rules, (str, bytes)):
        return []
    diagnostics: list[dict[str, object]] = []
    for row in rules:
        if not isinstance(row, Mapping):
            continue
        identifier = row.get("id")
        kind = row.get("kind")
        if kind not in _RULE_KINDS:
            diagnostics.append(
                {
                    "rule_id": str(identifier or "<unknown>"),
                    "kind": str(kind or ""),
                    "status": "UNSUPPORTED",
                    "hits": [],
                    "reason": "residue rule syntax is not supported by the generic scanner",
                }
            )
            continue
        hits = scan_residue_rules(root, [row])
        diagnostics.append(
            {
                "rule_id": str(identifier),
                "kind": str(kind),
                "status": "FAIL" if hits else "PASS",
                "hits": hits,
            }
        )
    return diagnostics


__all__ = ["classify_residue_rules", "scan_residue_rules"]
