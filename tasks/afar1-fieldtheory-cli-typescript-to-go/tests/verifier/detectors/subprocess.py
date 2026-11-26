"""Detect process sinks that invoke the source language runtime.

This is deliberately narrower than a full taint engine: a hit is L2 only when a
process sink and a source-runtime token are both present in the candidate tree.
The detector still handles the common obfuscations that plain literal rules
miss: aliases, qualified names, whitespace and single-character concatenation.
"""

from __future__ import annotations

import re
from collections import ChainMap, Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .runtime_identity import (
    process_runtime_tokens,
    process_token_present,
    token_present,
)


_SOURCE_SUFFIXES = frozenset(
    {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".py", ".go", ".rs", ".java"}
)
# Resource/script files are delivered alongside the target build just like
# source files.  A source-runtime invocation hidden in a shell script, template
# or embedded asset is invisible to a detector that only reads source suffixes.
_RESOURCE_SUFFIXES = frozenset(
    {".sh", ".bash", ".zsh", ".ksh", ".dash", ".fish", ".tmpl", ".tpl"}
)
# Documentation/data files are never executed as programs; excluding them keeps
# a README code block or a data manifest referenced by name from being pulled
# into the executable scan surface (a false positive).
_NON_EXECUTABLE_SUFFIXES = frozenset(
    {
        ".md", ".markdown", ".rst", ".txt", ".adoc",
        ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
        ".csv", ".tsv", ".lock", ".sum", ".mod", ".svg", ".png", ".jpg", ".jpeg",
        ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz", ".tar", ".whl",
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
    "exec.Command(",
    "exec.CommandContext(",
    # Go's ``os/exec`` package also launches a process when an ``exec.Cmd``
    # value is built directly (``exec.Cmd{Path: ...}``, ``&exec.Cmd{...}``,
    # ``new(exec.Cmd)``, ``var c exec.Cmd``) instead of going through the
    # ``exec.Command`` helper.  Matching the package-qualified type closes the
    # whole value-construction family, not just the helper spelling.
    "exec.Cmd",
    "os.StartProcess(",
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

# A shell command starts a line or follows a command separator / command
# prefix.  Matching the runtime token in command position (``exec node``,
# ``$(tsx``, ``| deno``) keeps prose or data mentions from counting as an
# execution, which is why resource files cannot reuse the source-sink window.
_SHELL_COMMAND = re.compile(
    r"(?m)(?:^|[|&;()`{}]|\$\()"
    r"[ \t]*"
    r"(?:exec[ \t]+|command[ \t]+|env[ \t]+|nohup[ \t]+|sudo[ \t]+|xargs[ \t]+)*"
    r"(?P<command>[A-Za-z0-9_./\-]+)"
)

# ``//go:embed`` pulls an arbitrary file (any suffix, or none) into the target
# binary.  Those referenced resources join the scan surface even when their
# extension is not otherwise source-like.
_EMBED_DIRECTIVE = re.compile(r"//go:embed[ \t]+([^\n]+)")
_EMBED_TOKEN = re.compile(r'"[^"]*"|\S+')

# A function whose return value is built only from literals is a second
# channel for the command value: ``exec.Command(legacyRuntimeCommand(), …)``
# where ``func legacyRuntimeCommand() string { return "no" + "de" }``.  The
# caller window then contains no runtime token, so lexically-following single
# assignments alone cannot see the fallback.
_FUNC_DEF = re.compile(
    r"\bfunc\s+(?:\([^)]*\)\s*)?([A-Za-z_][A-Za-z0-9_]*)\s*\([^)]*\)[^{;\n]*\{"
)
_RETURN_STMT = re.compile(r"\breturn\b([^\n;}]*)")



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


def _strip_comments_keep_strings(text: str) -> str:
    """Blank comments while preserving string-literal contents (source files).

    The regex-only ``_strip_comments`` deletes to end-of-line when a literal
    contains a comment introducer (``"http://…"``, ``"#!/bin/sh"``).  A decoy
    literal placed earlier on the same line as a hidden fallback therefore
    erased the fallback before the constant evaluator could read it.  This scan
    is string-literal aware and offset-preserving: only real comment text is
    blanked.  It is used for target-language *source* files; shell/script
    resources keep the regex stripper (an apostrophe in prose would otherwise
    open a bogus literal).
    """

    characters = list(text)
    length = len(text)
    state = "normal"
    quote = ""
    index = 0
    while index < length:
        character = text[index]
        if state == "normal":
            if character == "/" and index + 1 < length and text[index + 1] == "/":
                while index < length and text[index] != "\n":
                    characters[index] = " "
                    index += 1
                continue
            if character == "#" and not (
                index + 1 < length and text[index + 1] == "["
            ):
                while index < length and text[index] != "\n":
                    characters[index] = " "
                    index += 1
                continue
            if character == "/" and index + 1 < length and text[index + 1] == "*":
                characters[index] = " "
                characters[index + 1] = " "
                index += 2
                while index < length and not (
                    text[index] == "*"
                    and index + 1 < length
                    and text[index + 1] == "/"
                ):
                    if text[index] != "\n":
                        characters[index] = " "
                    index += 1
                if index < length:
                    characters[index] = " "
                    index += 1
                if index < length:
                    characters[index] = " "
                    index += 1
                continue
            if character in ("'", '"', "`"):
                state = "string"
                quote = character
                index += 1
                continue
            index += 1
            continue
        if quote != "`" and character == "\\":
            index += 2
            continue
        if character == quote:
            state = "normal"
        index += 1
    return "".join(characters)


def _collapsed(text: str) -> str:
    """Remove quotes, plus signs and whitespace to expose 'g' + 'o' splits."""

    return re.sub(r"[\s'\"+`]", "", text).casefold()


def _joined_literals(text: str) -> str:
    """Concatenate string literals in a window to expose 'g' + 'o' splits."""

    return "".join(re.findall(r"['\"]([^'\"]*)['\"]", text)).casefold()


# A single assignment whose right-hand side is an expression.  Capturing the
# whole right-hand side (not just literal assignments) lets bounded constant
# propagation follow concatenations and indirect values back to their literals.
# ``+=`` is captured as well: ``s := "no"; s += "de"`` builds the same value as
# ``s := "no" + "de"`` and must not hide the runtime token.
_ASSIGNMENT = re.compile(
    r"(?m)(?<![A-Za-z0-9_])(?:var\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*(:?=|\+=)(?!=)\s*([^\n;]+)"
)
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_GETENV = re.compile(r"(?:os\.)?getenv\s*\(\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)
_INT_LITERAL = re.compile(r"0[xX][0-9a-fA-F_]+|0[bB][01_]+|0[oO][0-7_]+|\d[\d_]*")

# Bounded constant evaluator.  A hidden fallback names the source runtime (or
# the resource that runs it) with a *value* it builds at compile time; a
# detector that only reads the literal spelling of that value is defeated by
# every equivalent construction.  The evaluator folds the common constant
# families to a single string so the token/resource match runs on the value:
# string and rune literals, ``[]byte{...}`` / ``[]byte("...")`` slices,
# ``string``/``rune``/``int`` conversions, ``filepath.Join``/``path.Join``,
# ``strings.*``, ``strconv.*`` and ``fmt.Sprintf``, plus ``+``/arithmetic.
#
# ``(kind, payload)`` values: ``str`` text, ``int`` integer, ``bytes`` byte
# string, ``list`` of values.  ``None`` means "not a compile-time constant".
_ESCAPES = {
    "n": "\n", "t": "\t", "r": "\r", "a": "\a", "b": "\b", "f": "\f",
    "v": "\v", "0": "\0", "\\": "\\", "'": "'", '"': '"', "`": "`",
}
_STRING_CONVERTERS = frozenset({"string"})
_INT_CONVERTERS = frozenset(
    {
        "rune", "byte", "int", "int8", "int16", "int32", "int64",
        "uint", "uint8", "uint16", "uint32", "uint64", "uintptr",
    }
)
_JOIN_FUNCTIONS = frozenset({"filepath.Join", "path.Join"})
_KNOWN_CALLS = frozenset(
    set(_STRING_CONVERTERS)
    | set(_INT_CONVERTERS)
    | set(_JOIN_FUNCTIONS)
    | {
        "strings.Join",
        "strings.ToLower",
        "strings.ToUpper",
        "strings.TrimSpace",
        "strings.TrimPrefix",
        "strings.TrimSuffix",
        "strings.Replace",
        "strings.ReplaceAll",
        "strconv.Itoa",
        "strconv.Quote",
        "strconv.Unquote",
        "fmt.Sprintf",
    }
)
_SPRINTF_VERB = re.compile(r"%[-+ #0]*\d*(?:\.\d+)?[sdqcvxXob]")


def _read_quoted(text: str, index: int) -> tuple[str | None, int]:
    """Return the content and end offset of the literal starting at ``index``."""

    quote = text[index]
    length = len(text)
    if quote == "`":
        close = text.find("`", index + 1)
        if close < 0:
            return None, index + 1
        return text[index + 1 : close], close + 1
    cursor = index + 1
    out: list[str] = []
    while cursor < length:
        character = text[cursor]
        if character == "\\":
            if cursor + 1 < length:
                following = text[cursor + 1]
                out.append(_ESCAPES.get(following, following))
                cursor += 2
                continue
            cursor += 1
            continue
        if character == quote:
            return "".join(out), cursor + 1
        out.append(character)
        cursor += 1
    return None, index + 1


def _dotted_name(text: str, index: int) -> tuple[str, int]:
    match = _IDENTIFIER.match(text, index)
    if not match:
        return "", index
    end = match.end()
    parts = [match.group(0)]
    cursor = end
    while cursor < len(text) and text[cursor] == ".":
        following = _IDENTIFIER.match(text, cursor + 1)
        if not following:
            break
        parts.append(following.group(0))
        cursor = following.end()
    return ".".join(parts), cursor


def _balanced(text: str, open_index: int) -> int:
    """Index of the bracket matching the opener at ``open_index`` (or ``-1``)."""

    opener = text[open_index]
    closer = {"(": ")", "{": "}", "[": "]"}.get(opener)
    if closer is None:
        return -1
    depth = 0
    cursor = open_index
    length = len(text)
    quote = ""
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
        if character == opener:
            depth += 1
        elif character == closer:
            depth -= 1
            if depth == 0:
                return cursor
        cursor += 1
    return -1


def _split_arguments(body: str) -> list[str]:
    """Split a call or literal argument list at top-level commas."""

    parts: list[str] = []
    depth = 0
    quote = ""
    start = 0
    cursor = 0
    length = len(body)
    while cursor < length:
        character = body[cursor]
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
            depth -= 1
        elif character == "," and depth == 0:
            parts.append(body[start:cursor])
            start = cursor + 1
        cursor += 1
    tail = body[start:]
    if tail.strip() or parts:
        parts.append(tail)
    return [part.strip() for part in parts if part.strip()]


def _as_string(value: Any) -> str | None:
    if value is None:
        return None
    kind = value[0]
    if kind == "str":
        return value[1]
    if kind == "bytes":
        raw: bytes = value[1]
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("latin-1")
    if kind == "int":
        code = value[1]
        if 0 <= code <= 0x10FFFF and not (0xD800 <= code <= 0xDFFF):
            return chr(code)
    return None


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    if value[0] == "int":
        return value[1]
    text = _as_string(value)
    if text is None:
        return None
    if len(text) == 1:
        return ord(text)
    try:
        return int(text)
    except ValueError:
        return None


def _parse_int_literal(text: str) -> tuple[str, int] | None:
    clean = text.replace("_", "")
    lowered = clean.casefold()
    try:
        if lowered.startswith("0x"):
            return ("int", int(clean, 16))
        if lowered.startswith("0b"):
            return ("int", int(clean, 2))
        if lowered.startswith("0o"):
            return ("int", int(clean, 8))
        if len(clean) > 1 and clean.startswith("0") and clean.isdigit():
            return ("int", int(clean, 8))
        return ("int", int(clean, 10))
    except ValueError:
        return None


def _decode_byte_slice(body: str) -> str:
    """Decode a ``[]byte{110, 111, ...}`` body into the text it spells."""

    numbers = re.findall(r"0[xX][0-9a-fA-F]+|\d+", body)
    try:
        return "".join(chr(int(number, 0)) for number in numbers)
    except (ValueError, OverflowError):
        return ""


def _apply_binary(operator: str, left: Any, right: Any) -> Any:
    if operator == "+":
        if left[0] == "int" and right[0] == "int":
            return ("int", left[1] + right[1])
        if left[0] == "bytes" and right[0] == "bytes":
            return ("bytes", left[1] + right[1])
        left_text, right_text = _as_string(left), _as_string(right)
        if left_text is not None and right_text is not None:
            return ("str", left_text + right_text)
        return None
    if left[0] == "int" and right[0] == "int":
        divisor = right[1]
        if operator == "-":
            return ("int", left[1] - divisor)
        if operator == "*":
            return ("int", left[1] * divisor)
        if operator == "/" and divisor != 0:
            return ("int", left[1] // divisor)
        if operator == "%" and divisor != 0:
            return ("int", left[1] % divisor)
    return None


def _sprintf(template: str, args: Sequence[Any]) -> str | None:
    out: list[str] = []
    cursor = 0
    position = 0
    for match in _SPRINTF_VERB.finditer(template):
        out.append(template[cursor : match.start()])
        verb = match.group(0)[-1]
        if position >= len(args):
            return None
        argument = args[position]
        position += 1
        if verb == "s":
            text = _as_string(argument)
            if text is None:
                return None
            out.append(text)
        elif verb in "dq":
            text = _as_string(argument)
            if text is None:
                number = _as_int(argument)
                if number is None:
                    return None
                out.append(str(number))
            else:
                out.append(text if verb == "d" else '"' + text + '"')
        elif verb == "c":
            number = _as_int(argument)
            if number is None:
                return None
            out.append(chr(number))
        elif verb == "v":
            text = _as_string(argument)
            if text is None:
                return None
            out.append(text)
        else:  # x / X / o / b integer formats
            number = _as_int(argument)
            if number is None:
                return None
            out.append(format(number, verb))
        cursor = match.end()
    out.append(template[cursor:])
    return "".join(out)


def _call_value(name: str, args: Sequence[Any]) -> Any:
    if name in _STRING_CONVERTERS:
        if len(args) != 1:
            return None
        text = _as_string(args[0])
        return ("str", text) if text is not None else None
    if name in _INT_CONVERTERS:
        if len(args) != 1:
            return None
        number = _as_int(args[0])
        return ("int", number) if number is not None else None
    if name in _JOIN_FUNCTIONS:
        texts = [text for text in (_as_string(arg) for arg in args) if text]
        return ("str", "/".join(texts))
    if name == "strings.Join":
        if len(args) == 2 and args[0] is not None and args[0][0] == "list":
            separator = _as_string(args[1])
            pieces = [_as_string(item) for item in args[0][1]]
            if separator is not None and all(piece is not None for piece in pieces):
                return ("str", separator.join(pieces))
        return None
    if name in ("strings.ToLower", "strings.ToUpper"):
        text = _as_string(args[0]) if len(args) == 1 else None
        if text is None:
            return None
        return ("str", text.casefold() if name.endswith("Lower") else text.upper())
    if name == "strings.TrimSpace":
        text = _as_string(args[0]) if len(args) == 1 else None
        return ("str", text.strip()) if text is not None else None
    if name in ("strings.TrimPrefix", "strings.TrimSuffix"):
        if len(args) != 2:
            return None
        text, affix = _as_string(args[0]), _as_string(args[1])
        if text is None or affix is None:
            return None
        if name.endswith("Prefix"):
            return ("str", text[len(affix) :] if text.startswith(affix) else text)
        return ("str", text[: -len(affix)] if affix and text.endswith(affix) else text)
    if name in ("strings.Replace", "strings.ReplaceAll"):
        if len(args) < 3:
            return None
        text, old, new = _as_string(args[0]), _as_string(args[1]), _as_string(args[2])
        if text is None or old is None or new is None:
            return None
        if name == "strings.ReplaceAll":
            return ("str", text.replace(old, new))
        count = _as_int(args[3]) if len(args) == 4 else -1
        return ("str", text.replace(old, new, count if count is not None else -1))
    if name == "strconv.Itoa":
        number = _as_int(args[0]) if len(args) == 1 else None
        return ("str", str(number)) if number is not None else None
    if name == "strconv.Quote":
        text = _as_string(args[0]) if len(args) == 1 else None
        if text is None:
            return None
        return ("str", '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"')
    if name == "strconv.Unquote":
        text = _as_string(args[0]) if len(args) == 1 else None
        if text is None or len(text) < 2 or text[0] not in "\"'`" or text[-1] != text[0]:
            return None
        return ("str", text[1:-1])
    if name == "fmt.Sprintf":
        if not args:
            return None
        template = _as_string(args[0])
        if template is None:
            return None
        rendered = _sprintf(template, args[1:])
        return ("str", rendered) if rendered is not None else None
    return None


def _eval_primary(text: str, values: Mapping[str, str], index: int) -> tuple[Any, int]:
    length = len(text)
    if index >= length:
        return None, index
    character = text[index]
    if character in ("'", '"', "`"):
        raw, end = _read_quoted(text, index)
        if raw is None:
            return None, index + 1
        if character == "'":
            return (("int", ord(raw[0])) if raw else None), end
        return ("str", raw), end
    if character == "[":
        for lead, kind in (("[]byte{", "bytes"), ("[]rune{", "list"), ("[]string{", "list")):
            if text.startswith(lead, index):
                close = _balanced(text, index + len(lead) - 1)
                if close < 0:
                    return None, index + 1
                body = text[index + len(lead) : close]
                elements = [_eval_atom(part, values) for part in _split_arguments(body)]
                if kind == "bytes":
                    numbers = [_as_int(element) for element in elements]
                    if elements and all(number is not None for number in numbers):
                        return ("bytes", bytes(number & 0xFF for number in numbers)), close + 1
                    return None, close + 1
                return ("list", elements), close + 1
        if text.startswith("[]byte(", index):
            close = _balanced(text, index + 6)
            if close < 0:
                return None, index + 1
            inner = _eval_atom(text[index + 7 : close], values)
            converted = _as_string(inner)
            if converted is None:
                return None, close + 1
            return ("bytes", converted.encode("utf-8", "surrogatepass")), close + 1
        return None, index + 1
    match = _IDENTIFIER.match(text, index)
    if match:
        name, name_end = _dotted_name(text, index)
        cursor = name_end
        while cursor < length and text[cursor] in " \t":
            cursor += 1
        if cursor < length and text[cursor] == "(" and name in _KNOWN_CALLS:
            close = _balanced(text, cursor)
            if close >= 0:
                arguments = [
                    _eval_atom(part, values)
                    for part in _split_arguments(text[cursor + 1 : close])
                ]
                return _call_value(name, arguments), close + 1
        env = _GETENV.match(text, index)
        if env:
            return ("str", env.group(1)), env.end()
        value = values.get(match.group(0))
        if value:
            return ("str", value), match.end()
        return None, name_end
    numeric = _INT_LITERAL.match(text, index)
    if numeric:
        return _parse_int_literal(numeric.group(0)), numeric.end()
    return None, index + 1


def _eval_unary(text: str, values: Mapping[str, str], index: int) -> tuple[Any, int]:
    length = len(text)
    while index < length and text[index] in " \t":
        index += 1
    if index >= length:
        return None, index
    character = text[index]
    if character == "(":
        close = _balanced(text, index)
        if close < 0:
            return None, index + 1
        value, _ = _eval_expr(text[index + 1 : close], values, 0)
        return value, close + 1
    if character == "-":
        value, end = _eval_unary(text, values, index + 1)
        if value is not None and value[0] == "int":
            return ("int", -value[1]), end
        return None, end
    return _eval_primary(text, values, index)


def _eval_expr(text: str, values: Mapping[str, str], index: int = 0) -> tuple[Any, int]:
    length = len(text)
    value, end = _eval_unary(text, values, index)
    if value is None:
        return None, end
    while end < length:
        cursor = end
        while cursor < length and text[cursor] in " \t":
            cursor += 1
        if cursor >= length:
            break
        operator = text[cursor]
        if operator not in "+-*/%":
            break
        right, right_end = _eval_unary(text, values, cursor + 1)
        if right is None:
            return None, right_end
        value = _apply_binary(operator, value, right)
        if value is None:
            return None, right_end
        end = right_end
    return value, end


def _eval_atom(expression: str, values: Mapping[str, str]) -> Any:
    value, _ = _eval_expr(expression, values, 0)
    return value


def _scan_pieces(text: str, values: Mapping[str, str]) -> str:
    pieces: list[str] = []
    previous_end = 0
    first = True
    index = 0
    length = len(text)
    while index < length:
        value, end = _eval_expr(text, values, index)
        if value is not None and end > index and value[0] in ("str", "bytes"):
            content = _as_string(value)
            if content:
                gap = text[previous_end:index]
                fused = "+" in gap and not gap.replace("+", "").strip()
                pieces.append("" if first or fused else " ")
                pieces.append(content)
                previous_end = end
                first = False
                index = end
                continue
        index += 1
    return "".join(pieces)


def _evaluate(text: str, values: Mapping[str, str]) -> str:
    """Concatenate the constant string an expression can build, in order.

    Runs the bounded constant evaluator over ``text``.  Pieces a ``+`` joins
    are fused (so ``a := "no"; b := "de"`` reads as ``node`` and
    ``string(rune(110))`` reads as ``n``); every other adjacency keeps a space
    boundary so two unrelated arguments never fuse into a false token.
    """

    return _scan_pieces(text, values)


def _resolve_values(text: str) -> dict[str, str]:
    """Map assigned identifiers to the constant text their value can build.

    Runs a few bounded passes so ``a := "no"; b := "de"; c := a+b`` resolves
    without needing a real evaluator.  Identifiers whose value is not driven by
    constant content are omitted, so an unrelated ``bin := cfg.Bin`` never
    contributes evidence.
    """

    assignments = [
        (match.group(1), match.group(2), match.group(3))
        for match in _ASSIGNMENT.finditer(text)
    ]
    writes = Counter(name for name, _, _ in assignments)
    values: dict[str, str] = {}
    for _ in range(4):
        previous = values
        values = {}
        # A reused local name (err, line, text, ...) is not a single constant
        # shared by every function in the file. Feeding its newly expanded
        # value back into later assignments caused exponential allocation.
        # Resolve repeated names against the previous pass only. Single-write
        # aliases can still propagate in source order, including long chains.
        single_writes: dict[str, str] = {}
        context = ChainMap(single_writes, previous)
        for name, operator, expression in assignments:
            built = _evaluate(expression, context)
            if operator == "+=" and values.get(name):
                values[name] = values[name] + built
            else:
                values[name] = built
            if writes[name] == 1:
                single_writes[name] = values[name]
        if values == previous:
            break
    return {name: value for name, value in values.items() if value}


def constant_strings(text: str) -> set[str]:
    """Every string value the file's constant expressions can build.

    Used to decide which resource files a target-language program can reach: a
    path is reachable whether it is written as one literal or assembled from
    fragments (``base + ext``, ``filepath.Join``, ``fmt.Sprintf``, a constant
    helper).  Only ``.go`` callers use this, so the string-aware comment
    stripper (safe for Go, unsafe for shell prose) is applied here.
    """

    clean = _strip_comments_keep_strings(text)
    values = _resolve_values(clean)
    values.update(_function_values(clean, values))
    found: set[str] = set()
    for match in re.finditer(r'"(?:\\.|[^"\\])*"|`[^`]*`', text):
        raw = match.group(0)
        if len(raw) >= 2:
            found.add(raw[1:-1])
    for value in values.values():
        if value:
            found.add(value)
    return found


def _block_end(text: str, open_index: int) -> int | None:
    """Return the index of the ``}`` matching the ``{`` at ``open_index``."""

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
    return None


def _function_values(text: str, values: Mapping[str, str]) -> dict[str, str]:
    """Map function names to the literal text their ``return`` can build.

    This extends the bounded constant propagation one hop across a call
    boundary so a command value computed by a helper is still resolved at the
    sink.  Only functions whose returns are literal-driven contribute; anything
    built from a runtime value is omitted, keeping ``exec.Command(bin, …)``
    from being reported.
    """

    resolved: dict[str, str] = {}
    for match in _FUNC_DEF.finditer(text):
        open_index = match.end() - 1
        close_index = _block_end(text, open_index)
        if close_index is None:
            continue
        body = text[open_index + 1 : close_index]
        pieces: list[str] = []
        for statement in _RETURN_STMT.finditer(body):
            expression = statement.group(1).strip()
            if not expression:
                continue
            built = _evaluate(expression, values)
            if built:
                pieces.append(built)
        if pieces:
            resolved[match.group(1)] = " ".join(pieces)
    return resolved



def _segment_tokens(text: str, wanted: set[str]) -> set[str]:
    """Return wanted tokens that appear as whole ``_``/``-`` separated segments."""

    segments = {segment.casefold() for segment in re.split(r"[^A-Za-z0-9]+", text)}
    return {token for token in wanted if token in segments}


def _env_tainted_identifiers(text: str, wanted: set[str]) -> dict[str, str]:
    """Map identifiers read from a source-runtime-named environment variable.

    A command taken from the environment (``bin := os.Getenv("..._NODE")``)
    carries no literal token at the sink, but the environment key names the
    source runtime, so the identifier is treated as that token.
    """

    tainted: dict[str, str] = {}
    for match in _ASSIGNMENT.finditer(text):
        for env in _GETENV.finditer(match.group(3)):
            for token in _segment_tokens(env.group(1), wanted):
                tainted[match.group(1)] = token
    return tainted


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
        r"(?:^|[\s(])([A-Za-z_][A-Za-z0-9_]*)[ \t]+['\"]os/exec['\"]", text
    ):
        alias = match.group(1)
        # Covers ``import oe "os/exec"`` and the grouped ``import ( oe "os/exec" )``
        # spelling; an aliased import reaches the same value-construction surface.
        aliases.append(f"{alias}.Command(")
        aliases.append(f"{alias}.Cmd")
    return aliases


def _process_launch_present(text: str) -> bool:
    """True when the target-language text reaches a process-launch sink.

    Uses the same alias-aware vocabulary as the sink scan, so an aliased
    ``import oe "os/exec"`` (or a grouped import) counts as a launcher exactly
    like a direct ``exec.Command``.  Reachability of a resource must not hinge
    on the *spelling* of the import.
    """

    lowered = text.casefold()
    if any(sink.casefold() in lowered for sink in _SINKS):
        return True
    return bool(_alias_sinks(text))


def _token_present(text: str, token: str) -> bool:
    """Backward-compatible lexical matcher for test/internal callers."""

    return token_present(text, token)


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _read_prefix(path: Path, size: int) -> bytes:
    try:
        with path.open("rb") as handle:
            return handle.read(size)
    except OSError:
        return b""


def _is_binary(path: Path) -> bool:
    return b"\x00" in _read_prefix(path, 8192)


def _embed_tokens(declaration: str) -> list[str]:
    tokens: list[str] = []
    for raw in _EMBED_TOKEN.findall(declaration):
        token = raw
        if len(token) >= 2 and token.startswith('"') and token.endswith('"'):
            token = token[1:-1]
        if token:
            tokens.append(token)
    return tokens


def _embedded_files(root: Path, sources: Sequence[Path]) -> list[Path]:
    """Resolve ``//go:embed`` references to the resource files they pull in."""

    found: list[Path] = []
    seen: set[str] = set()
    for path in sources:
        if path.suffix.casefold() not in _SOURCE_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for directive in _EMBED_DIRECTIVE.finditer(text):
            for token in _embed_tokens(directive.group(1)):
                relative = token.lstrip("/")
                if not relative or ".." in Path(relative).parts:
                    continue
                for candidate in sorted(path.parent.glob(relative)):
                    if candidate.is_symlink() or not candidate.is_file():
                        continue
                    try:
                        key = candidate.relative_to(root).as_posix()
                    except ValueError:
                        continue
                    if key in seen or _is_binary(candidate):
                        continue
                    seen.add(key)
                    found.append(candidate)
    return found


def _referenced_files(root: Path, sources: Sequence[Path]) -> list[Path]:
    """Resolve resource files named by target-language constant expressions.

    A shell script or runner can be hidden behind any suffix -- including none
    -- so a bridge referenced by ``filepath.Join(root, "scripts", "helper")``
    must join the scan surface even though ``helper`` looks like no known
    script suffix.  Naming is resolved through the bounded constant evaluator
    (``base + ext``, ``fmt.Sprintf``, a constant helper), not just literal
    spellings, so the surface stays bounded to what production code can reach
    while still covering a name assembled from fragments.
    """

    literals: list[str] = []
    for path in sources:
        if path.suffix.casefold() != ".go":
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        literals.extend(constant_strings(text))
    if not literals:
        return []
    found: list[Path] = []
    seen: set[str] = set()
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in _IGNORED_PARTS for part in relative.parts):
            continue
        suffix = path.suffix.casefold()
        if suffix in _SOURCE_SUFFIXES or suffix in _RESOURCE_SUFFIXES:
            continue
        if suffix in _NON_EXECUTABLE_SUFFIXES:
            continue
        key = relative.as_posix()
        if key in seen:
            continue
        name = path.name
        if not any(
            (name and name in literal) or key in literal for literal in literals
        ):
            continue
        if _is_binary(path):
            continue
        seen.add(key)
        found.append(path)
    return found


def _candidate_files(root: Path) -> list[Path]:
    if not root.is_dir() or root.is_symlink():
        return []
    files: list[Path] = []
    seen: set[str] = set()
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in _IGNORED_PARTS for part in relative.parts):
            continue
        suffix = path.suffix.casefold()
        if suffix not in _SOURCE_SUFFIXES and suffix not in _RESOURCE_SUFFIXES:
            continue
        key = relative.as_posix()
        if key in seen:
            continue
        seen.add(key)
        files.append(path)
    reachable_seeds: list[Path] = []
    for resolver in (_embedded_files, _referenced_files, _process_referenced_files):
        for path in resolver(root, list(files)):
            key = path.relative_to(root).as_posix()
            if key in seen:
                continue
            seen.add(key)
            files.append(path)
            reachable_seeds.append(path)
    # A resource that only forwards to the script which actually launches the
    # source runtime must not keep that launcher out of the candidate surface:
    # expand reachability transitively (resource -> resource) so the detector
    # scans every file the target language can reach, not just the first hop.
    for path in _referenced_resource_closure(root, reachable_seeds):
        key = path.relative_to(root).as_posix()
        if key in seen:
            continue
        seen.add(key)
        files.append(path)
    return sorted(files)


def _source_files(root: Path) -> list[Path]:
    if not root.is_dir() or root.is_symlink():
        return []
    found: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in _IGNORED_PARTS for part in relative.parts):
            continue
        if path.suffix.casefold() in _SOURCE_SUFFIXES:
            found.append(path)
    return found


# A bridge can be chained (``go -> a.sh -> b.sh -> c.sh``), so reachability is
# a bounded transitive closure rather than a single hop.  The cap only exists
# to keep a pathological/self-referential tree finite; an ordinary chain is far
# shorter.
_REFERENCE_DEPTH = 8


def _match_launched_resources(root: Path, literals: Sequence[str]) -> list[Path]:
    """Non-source files a set of literals names by value.

    This one-hop matcher is shared by the initial target-language seed arm and
    every resource->resource hop of the reachability closure, so the two can
    never drift apart.  Matching is **suffix-agnostic**: a bridge script is a
    launcher argument whether it is spelled ``.sh``, ``.txt``, ``.log`` or with
    no suffix at all; only source files are excluded (they are scanned as code,
    not as launched resources).  A name is bound when it appears either as the
    file's basename or as its repo-relative path, which covers
    ``filepath.Join(root, "scripts", "inner.sh")`` as well as a bare
    ``inner.sh`` inside another script.
    """

    if not literals:
        return []
    found: list[Path] = []
    seen: set[str] = set()
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in _IGNORED_PARTS for part in relative.parts):
            continue
        if path.suffix.casefold() in _SOURCE_SUFFIXES:
            continue
        key = relative.as_posix()
        if key in seen:
            continue
        name = path.name
        if not any(
            (name and name in literal) or key in literal for literal in literals
        ):
            continue
        if _is_binary(path):
            continue
        seen.add(key)
        found.append(path)
    return found


def _launched_resource_literals(sources: Sequence[Path]) -> list[str]:
    """Constant values a process-launching ``.go`` file can name a resource by.

    Reachability is decided on the *value* a name can build (bounded constant
    folding over ``base + ext`` / ``filepath.Join`` / ``fmt.Sprintf`` / a
    constant helper), not on its literal spelling, and only for target-language
    files that actually reach a process-launch sink.
    """

    literals: list[str] = []
    for path in sources:
        if path.suffix.casefold() != ".go":
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if not _process_launch_present(_strip_comments_keep_strings(text)):
            continue
        literals.extend(constant_strings(text))
    return literals


def _resource_reference_literals(path: Path) -> list[str]:
    """Text of a reachable resource, to find the next resource it names.

    A bridge may only forward to the script that actually launches the source
    runtime (``sh "$(dirname "$0")/inner.sh"``).  Returning the whole body --
    comment-stripped and with bounded shell variables inlined -- as a literal
    lets the same suffix-agnostic substring matcher that binds a ``.go`` name
    also bind the name this resource builds, closing the resource->resource
    edge of reachability.
    """

    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    clean = _strip_comments(text)
    expanded = _expand_shell_variables(clean, _shell_variables(clean))
    return [clean, expanded] if expanded != clean else [clean]


def _referenced_resource_closure(root: Path, seeds: Iterable[Path]) -> list[Path]:
    """Bounded transitive closure of resources reachable from ``seeds``.

    Every reachable non-source file is re-parsed for the names it builds and
    the newly named files join the surface, to a fixed point (bounded by
    ``_REFERENCE_DEPTH``).  A resource that only forwards to another script can
    therefore no longer hide the file that launches the source runtime.
    """

    closure: list[Path] = []
    seen: set[str] = set()
    frontier: list[Path] = []
    for path in seeds:
        if path.suffix.casefold() in _SOURCE_SUFFIXES:
            continue
        try:
            key = path.relative_to(root).as_posix()
        except ValueError:
            continue
        if key in seen:
            continue
        seen.add(key)
        closure.append(path)
        frontier.append(path)
    for _ in range(_REFERENCE_DEPTH):
        if not frontier:
            break
        literals: list[str] = []
        for path in frontier:
            literals.extend(_resource_reference_literals(path))
        if not literals:
            break
        new: list[Path] = []
        for path in _match_launched_resources(root, literals):
            try:
                key = path.relative_to(root).as_posix()
            except ValueError:
                continue
            if key in seen:
                continue
            seen.add(key)
            new.append(path)
        if not new:
            break
        closure.extend(new)
        frontier = new
    return closure


def _process_referenced_files(root: Path, sources: Sequence[Path]) -> list[Path]:
    """Non-source files a process-launching target-language file reaches.

    Seeds are the resources a launching ``.go`` file names by value; the
    result is the transitive closure over resource->resource references, so a
    bridge that merely forwards to the real launcher cannot keep it out of the
    surface.  This is the resource arm of the shared reachability contract used
    by both the candidate enumeration and the deceptive scope filter.
    """

    literals = _launched_resource_literals(sources)
    if not literals:
        return []
    return _referenced_resource_closure(
        root, _match_launched_resources(root, literals)
    )


def reachable_resource_paths(root: Path) -> tuple[str, ...]:
    """Relative paths of every resource reachable from the migration surface.

    Union of ``//go:embed`` targets and the non-source files a process-launching
    target-language file reaches, closed transitively over resource->resource
    references.  The scope filter and the candidate enumeration both consume
    this single contract, so the two can no longer drift apart -- the failure
    that let an aliased import, an embedded bridge, a non-script suffix, or a
    second-hop forwarding script slip past the deceptive scan.
    """

    if not root.is_dir() or root.is_symlink():
        return ()
    sources = _source_files(root)
    seeds = [*_embedded_files(root, sources), *_process_referenced_files(root, sources)]
    found: dict[str, Path] = {}
    for path in _referenced_resource_closure(root, seeds):
        try:
            key = path.relative_to(root).as_posix()
        except ValueError:
            continue
        found.setdefault(key, path)
    return tuple(sorted(found))


def _is_resource_file(path: Path) -> bool:
    """True for non-source resources (scripts, templates, embedded assets)."""

    return path.suffix.casefold() not in _SOURCE_SUFFIXES


# A shell variable assignment (``R=node``, ``BIN="$DIR/node"``).  Resolving the
# bounded ones lets the command-position scan see a runtime name that prose
# hides behind indirection (``R=node; exec "$R"``), the shell analogue of the
# Go constant-folding the source scan already performs.
_SHELL_ASSIGN = re.compile(
    r"(?m)(?:^|[;&|(){}\s])([A-Za-z_][A-Za-z0-9_]*)=(?!=)([^\n]*)"
)
_SHELL_QUOTED_VAR = re.compile(r'"\$(?:\{)?([A-Za-z_][A-Za-z0-9_]*)\}?"')
_SHELL_VAR = re.compile(r"\$(?:\{)?([A-Za-z_][A-Za-z0-9_]*)\}?")


def _shell_expand(text: str, index: int, values: Mapping[str, str]) -> tuple[str | None, int]:
    """Resolve a ``$NAME`` / ``${NAME}`` reference at ``index`` to its value."""

    cursor = index + 1
    if cursor < len(text) and text[cursor] == "{":
        close = text.find("}", cursor)
        if close < 0:
            return None, cursor
        name = text[cursor + 1 : close]
        # ``${NAME:-x}`` and friends carry a default/alternate; only the plain
        # name is bounded here, so anything with an operator is left unresolved.
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) and name in values:
            return values[name], close + 1
        return None, close + 1
    match = _IDENTIFIER.match(text, cursor)
    if not match:
        return None, cursor
    name = match.group(0)
    if name in values:
        return values[name], match.end()
    return None, match.end()


def _shell_value(raw: str, values: Mapping[str, str]) -> str | None:
    """Best-effort constant value of a shell word, or ``None`` when not bounded."""

    pieces: list[str] = []
    index = 0
    length = len(raw)
    while index < length:
        character = raw[index]
        if character in " \t":
            break
        if character in ";&|<>":
            break
        if character == "'":
            close = raw.find("'", index + 1)
            if close < 0:
                return None
            pieces.append(raw[index + 1 : close])
            index = close + 1
            continue
        if character == '"':
            cursor = index + 1
            buffer: list[str] = []
            while cursor < length:
                if raw[cursor] == "\\" and cursor + 1 < length:
                    buffer.append(raw[cursor + 1])
                    cursor += 2
                    continue
                if raw[cursor] == '"':
                    break
                if raw[cursor] == "$":
                    value, cursor = _shell_expand(raw, cursor, values)
                    if value is None:
                        return None
                    buffer.append(value)
                    continue
                buffer.append(raw[cursor])
                cursor += 1
            if cursor >= length:
                return None
            pieces.append("".join(buffer))
            index = cursor + 1
            continue
        if character == "$":
            value, cursor = _shell_expand(raw, index, values)
            if value is None:
                return None
            pieces.append(value)
            index = cursor
            continue
        pieces.append(character)
        index += 1
    return "".join(pieces) if pieces else None


def _shell_variables(text: str) -> dict[str, str]:
    """Map shell variable names to the constant word their assignment builds."""

    assignments = [
        (match.group(1), match.group(2)) for match in _SHELL_ASSIGN.finditer(text)
    ]
    resolved: dict[str, str] = {}
    for _ in range(4):
        for name, raw in assignments:
            value = _shell_value(raw, resolved)
            if value:
                resolved[name] = value
    return resolved


def _expand_shell_variables(text: str, values: Mapping[str, str]) -> str:
    """Inline bounded variable values, dropping the quotes around a lone var.

    ``exec "$R"`` with ``R=node`` becomes ``exec node`` so the command-position
    matcher sees the runtime token; newlines are preserved so reported line
    numbers stay valid.
    """

    if not values:
        return text
    text = _SHELL_QUOTED_VAR.sub(lambda m: values.get(m.group(1), m.group(0)), text)
    text = _SHELL_VAR.sub(lambda m: values.get(m.group(1), m.group(0)), text)
    return text


def _shell_command_hits(
    root: Path,
    path: Path,
    clean: str,
    text: str,
    wanted: set[str],
) -> list[dict[str, Any]]:
    ordered = sorted(wanted)
    relative = path.relative_to(root).as_posix()
    hits: list[dict[str, Any]] = []
    resolved_text = _expand_shell_variables(clean, _shell_variables(clean))
    for match in _SHELL_COMMAND.finditer(resolved_text):
        command = match.group("command")
        token = next(
            (item for item in ordered if process_token_present(command, item)), None
        )
        if token is None:
            continue
        hits.append(
            SubprocessHit(
                path=relative,
                line=_line_of(resolved_text, match.start("command")),
                sink="shell-command",
                token=token,
                evidence=resolved_text[match.start() : match.start() + 200],
            ).as_dict()
        )
    return hits


def _package_function_values(root: Path, files: Sequence[Path]) -> dict[str, str]:
    """Collect literal-returning functions across the candidate tree.

    Returns are keyed by function name; a name resolving to more than one value
    across files is dropped as ambiguous, so a same-named helper in an unrelated
    package can never supply a false token to a sink.
    """

    resolved: dict[str, str] = {}
    ambiguous: set[str] = set()
    for path in files:
        if path.suffix.casefold() not in _SOURCE_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        clean = _strip_comments_keep_strings(text)
        local_values = _resolve_values(clean)
        for name, value in _function_values(clean, local_values).items():
            if name in ambiguous:
                continue
            previous = resolved.get(name)
            if previous is not None and previous != value:
                ambiguous.add(name)
                resolved.pop(name, None)
            else:
                resolved[name] = value
    return {name: value for name, value in resolved.items() if name not in ambiguous}


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
    candidate_files = _candidate_files(root)
    package_functions = _package_function_values(root, candidate_files)
    hits: list[dict[str, Any]] = []
    for path in candidate_files:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        is_resource = _is_resource_file(path)
        # Source files use the string-literal-aware stripper so a decoy literal
        # containing ``//``/``#`` cannot erase a fallback on the same line; a
        # shell/script resource keeps the regex stripper (an apostrophe in prose
        # would otherwise open a bogus literal and hide a later command).
        clean = _strip_comments(text) if is_resource else _strip_comments_keep_strings(text)
        lowered = clean.casefold()
        values = dict(package_functions)
        values.update(_resolve_values(clean))
        functions = _function_values(clean, values)
        if functions:
            values.update(functions)
        env_tainted = _env_tainted_identifiers(clean, wanted)
        for sink in (*_SINKS, *_alias_sinks(clean)):
            start = lowered.find(sink.casefold())
            while start >= 0:
                end = min(len(clean), start + _WINDOW)
                window = clean[start:end]
                collapsed = _collapsed(window)
                joined = _joined_literals(window)
                resolved = _evaluate(window, values)
                matched = next(
                    (
                        token for token in wanted
                        if process_token_present(window, token)
                        or process_token_present(collapsed, token)
                        or process_token_present(joined, token)
                        or process_token_present(resolved, token)
                    ),
                    None,
                )
                if matched is None:
                    matched = next(
                        (
                            token for ident, token in sorted(env_tainted.items())
                            if process_token_present(window, ident)
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
        if is_resource:
            hits.extend(_shell_command_hits(root, path, clean, text, wanted))
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
    "constant_strings",
    "reachable_resource_paths",
]
