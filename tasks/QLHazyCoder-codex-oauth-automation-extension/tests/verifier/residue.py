"""Language-neutral evaluation of declarative residue rules."""

from __future__ import annotations

import ast
import base64
import io
import json
import re
import string
import tokenize
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Iterable, Mapping, Sequence


# Directory names pruned at *any* depth: never contain candidate source.
_EXCLUDED_ANY_PARTS = {
    ".git",
    "node_modules",
    "__pycache__",
}
# Directory names pruned only at the repository root.  These are build/venv
# outputs that must not shadow a real source directory such as
# ``scripts/build/``: an attacker can otherwise hide a runtime fallback there
# and have the whole subtree silently skipped (RC-DECEPTIVE-FALLBACK-PATH).
_EXCLUDED_TOP_PARTS = {
    ".venv",
    "venv",
    "target",
    "dist",
    "build",
    "coverage",
}
_RULE_KINDS = {"literal", "dependency", "import", "file", "python_symbol"}


def _safe_relative(value: str) -> bool:
    path = Path(value)
    return bool(value) and not path.is_absolute() and ".." not in path.parts and "\\" not in value


def _eligible(root: Path, path: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    parts = relative.parts
    return (
        path.is_file()
        and not path.is_symlink()
        and not any(part in _EXCLUDED_ANY_PARTS for part in parts)
        and (not parts or parts[0] not in _EXCLUDED_TOP_PARTS)
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


def _literal_contains(path: Path, target: str) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return target.casefold() in text.casefold()


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
    for line in text.splitlines():
        for pattern in dependency_lines:
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
        candidates.extend(match.group(1) for match in pattern.finditer(text))
    return any(
        _target_matches(candidate, target)
        or _target_matches(re.split(r"[./\\]", candidate, maxsplit=1)[0], target)
        for candidate in candidates
    )


_PY_NODES = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
_SYMBOL_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\Z")


def _position(node: ast.AST) -> tuple[int, int]:
    return int(getattr(node, "lineno", 0) or 0), int(getattr(node, "col_offset", 0) or 0)


def _fold_python_int(
    node: ast.AST, numbers: Mapping[str, Sequence[int]] | None = None
) -> int | None:
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, int)
        and not isinstance(node.value, bool)
    ):
        return int(node.value)
    if isinstance(node, ast.Name) and numbers is not None:
        bound = numbers.get(node.id)
        if bound is not None and len(bound) == 1:
            return int(bound[0])
        return None
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        inner = _fold_python_int(node.operand, numbers)
        if inner is None:
            return None
        return -inner if isinstance(node.op, ast.USub) else inner
    return None


def _fold_python_int_sequence(
    node: ast.AST, numbers: Mapping[str, Sequence[int]]
) -> list[int] | None:
    """Fold ``[104, 116, ...]`` (or a name bound to such a list) to ints."""

    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        values: list[int] = []
        for element in node.elts:
            value = _fold_python_int(element)
            if value is None:
                return None
            values.append(value)
        return values
    if isinstance(node, ast.Name):
        bound = numbers.get(node.id)
        return list(bound) if bound is not None else None
    return None


def _module_expression_name(node: ast.AST) -> str | None:
    """Best-effort module name for ``base64``/``__import__('base64')`` receivers."""

    if isinstance(node, ast.Name) and node.id:
        return node.id
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "__import__"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ):
        return node.args[0].value
    return None


def _fold_python_bytes(
    node: ast.AST, numbers: Mapping[str, Sequence[int]]
) -> bytes | None:
    """Fold ``bytes([...])``/``bytearray([...])``/literal bytes to raw bytes.

    Also resolves the runtime-reconstruction forms ``bytes.fromhex(...)`` and
    ``base64.b64decode(...)`` (including ``__import__('base64')``) so an encoded
    module/class name still folds to the source symbol it rebuilds.
    """

    if isinstance(node, ast.Constant) and isinstance(node.value, (bytes, bytearray)):
        return bytes(node.value)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        method = node.func.attr
        receiver = node.func.value
        if method == "b64decode" and node.args:
            literal = node.args[0]
            payload: object | None = None
            if isinstance(literal, ast.Constant) and isinstance(
                literal.value, (str, bytes, bytearray)
            ):
                payload = literal.value
            if payload is not None and _module_expression_name(receiver) == "base64":
                try:
                    return base64.b64decode(payload)
                except (ValueError, TypeError):
                    return None
            return None
        if (
            method == "fromhex"
            and isinstance(receiver, ast.Name)
            and receiver.id == "bytes"
            and len(node.args) == 1
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            try:
                return bytes.fromhex(node.args[0].value)
            except ValueError:
                return None
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"bytes", "bytearray"}
        and node.args
    ):
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, (bytes, bytearray)):
            return bytes(first.value)
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            encoding = "utf-8"
            if (
                len(node.args) > 1
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
            ):
                encoding = node.args[1].value
            try:
                return first.value.encode(encoding)
            except (LookupError, UnicodeEncodeError, ValueError):
                return None
        sequence = _fold_python_int_sequence(first, numbers)
        if sequence is not None:
            try:
                return bytes(sequence)
            except ValueError:
                return None
    return None


def _fold_python_scalar(
    node: ast.AST,
    bindings: Mapping[str, str],
    numbers: Mapping[str, Sequence[int]],
) -> str | int | None:
    """Fold a node to the plain scalar value a ``%``/``format`` slot renders."""

    integer = _fold_python_int(node, numbers)
    if integer is not None:
        return integer
    return _fold_python_string(node, bindings, numbers)


def _fold_percent_values(
    node: ast.AST,
    bindings: Mapping[str, str],
    numbers: Mapping[str, Sequence[int]],
) -> object | None:
    if isinstance(node, ast.Tuple):
        values: list[str | int] = []
        for element in node.elts:
            folded = _fold_python_scalar(element, bindings, numbers)
            if folded is None:
                return None
            values.append(folded)
        return tuple(values)
    return _fold_python_scalar(node, bindings, numbers)


def _apply_str_format(
    template: str,
    call: ast.Call,
    bindings: Mapping[str, str],
    numbers: Mapping[str, Sequence[int]],
) -> str | None:
    """Render ``str.format`` when every argument folds to a constant scalar."""

    positional: list[str] = []
    for argument in call.args:
        if isinstance(argument, ast.Starred):
            return None
        folded = _fold_python_scalar(argument, bindings, numbers)
        if folded is None:
            return None
        positional.append(str(folded))
    keyword: dict[str, str] = {}
    for entry in call.keywords:
        if entry.arg is None:
            return None
        folded = _fold_python_scalar(entry.value, bindings, numbers)
        if folded is None:
            return None
        keyword[entry.arg] = str(folded)
    try:
        return string.Formatter().vformat(template, positional, keyword)
    except (IndexError, KeyError, ValueError, TypeError, AttributeError):
        return None


def _fold_join_generator(
    separator: str,
    generator: ast.GeneratorExp,
    bindings: Mapping[str, str],
    numbers: Mapping[str, Sequence[int]],
) -> str | None:
    """Fold ``sep.join(<expr> for <name> in <int-list>)`` (for example ``chr``)."""

    if len(generator.generators) != 1:
        return None
    clause = generator.generators[0]
    if clause.ifs or not isinstance(clause.target, ast.Name):
        return None
    sequence = _fold_python_int_sequence(clause.iter, numbers)
    if sequence is None:
        return None
    items: list[str] = []
    for value in sequence:
        local = dict(numbers)
        local[clause.target.id] = [value]
        folded = _fold_python_string(generator.elt, bindings, local)
        if folded is None:
            return None
        items.append(folded)
    return separator.join(items)


def _fold_python_string(
    node: ast.AST,
    bindings: Mapping[str, str],
    numbers: Mapping[str, Sequence[int]],
) -> str | None:
    """Best-effort static folding of a Python string expression.

    Resolves literals, ``+`` concatenation, f-strings, ``%`` formatting,
    ``str.format``, ``str.join`` of literals (or of a ``chr``-driven generator),
    simple variables bound to a folded string, and the runtime-reconstruction
    forms ``chr(code)``, ``bytes([codes]).decode(encoding)``,
    ``bytes.fromhex(...).decode(...)`` and ``base64.b64decode(...).decode(...)``
    (including a named ``codes = [...]`` list).  Anything that cannot be
    resolved statically yields ``None`` so callers fall back to nothing.
    """

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return bindings.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _fold_python_string(node.left, bindings, numbers)
        right = _fold_python_string(node.right, bindings, numbers)
        if left is not None and right is not None:
            return left + right
        return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        template = _fold_python_string(node.left, bindings, numbers)
        if template is None:
            return None
        values = _fold_percent_values(node.right, bindings, numbers)
        if values is None:
            return None
        try:
            return template % values
        except (TypeError, ValueError, KeyError, IndexError):
            return None
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            inner = value.value if isinstance(value, ast.FormattedValue) else value
            folded = _fold_python_string(inner, bindings, numbers)
            if folded is None:
                return None
            parts.append(folded)
        return "".join(parts)
    if isinstance(node, ast.Call):
        function = node.func
        if isinstance(function, ast.Name) and function.id == "chr" and len(node.args) == 1:
            value = _fold_python_int(node.args[0], numbers)
            if value is not None:
                try:
                    return chr(value)
                except (ValueError, OverflowError):
                    return None
            return None
        if isinstance(function, ast.Attribute) and function.attr == "format":
            template = _fold_python_string(function.value, bindings, numbers)
            if template is not None:
                return _apply_str_format(template, node, bindings, numbers)
            return None
        if isinstance(function, ast.Attribute) and function.attr == "decode":
            raw = _fold_python_bytes(function.value, numbers)
            if raw is not None:
                encoding = "utf-8"
                if (
                    node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                ):
                    encoding = node.args[0].value
                try:
                    return raw.decode(encoding)
                except (LookupError, UnicodeDecodeError, ValueError):
                    return None
            return None
        if (
            isinstance(function, ast.Attribute)
            and function.attr == "join"
            and isinstance(function.value, ast.Constant)
            and isinstance(function.value.value, str)
            and len(node.args) == 1
        ):
            argument = node.args[0]
            if isinstance(argument, (ast.List, ast.Tuple, ast.Set)):
                items: list[str] = []
                for element in argument.elts:
                    folded = _fold_python_string(element, bindings, numbers)
                    if folded is None:
                        return None
                    items.append(folded)
                return function.value.value.join(items)
            if isinstance(argument, ast.GeneratorExp):
                return _fold_join_generator(
                    function.value.value, argument, bindings, numbers
                )
            return None
    return None


def _python_numeric_bindings(tree: ast.AST) -> dict[str, list[int]]:
    """Collect ``name -> [int, ...]`` for simple integer-sequence assignments."""

    numbers: dict[str, list[int]] = {}
    for target, value in _python_assignments(tree):
        sequence = _fold_python_int_sequence(value, numbers)
        if sequence is not None:
            numbers[target] = sequence
    return numbers


def _python_assignments(tree: ast.AST) -> list[tuple[str, ast.AST]]:
    """Yield ``(bound_name, value)`` for order-stable simple assignments."""

    assignments = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
    ]
    rows: list[tuple[str, ast.AST]] = []
    for node in sorted(assignments, key=_position):
        if isinstance(node, ast.Assign):
            value = node.value
            for target in node.targets:
                if isinstance(target, ast.Name):
                    rows.append((target.id, value))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            rows.append((node.target.id, node.value))
    return rows


def _python_string_bindings(
    tree: ast.AST, numbers: Mapping[str, Sequence[int]]
) -> dict[str, str]:
    """Collect ``name -> folded string`` for simple literal assignments.

    Handles ``=``/``: =`` binding and ``+=`` accumulation in source order so a
    name built up over several statements (``s = "ht"; s += "tp.server"``) still
    folds to the full string.
    """

    assignments = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign))
    ]
    bindings: dict[str, str] = {}
    for node in sorted(assignments, key=_position):
        if isinstance(node, ast.AugAssign):
            if not isinstance(node.target, ast.Name):
                continue
            left = bindings.get(node.target.id)
            right = _fold_python_string(node.value, bindings, numbers)
            if left is not None and right is not None:
                bindings[node.target.id] = left + right
            continue
        if isinstance(node, ast.Assign):
            targets = [target for target in node.targets if isinstance(target, ast.Name)]
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target] if isinstance(node.target, ast.Name) else []
            value = node.value
        else:  # pragma: no cover - defensive
            continue
        if value is None:
            continue
        folded = _fold_python_string(value, bindings, numbers)
        if folded is None:
            continue
        for target in targets:
            bindings[target.id] = folded
    return bindings


def _python_docstring_ids(tree: ast.AST) -> set[int]:
    """Return ids of string constants used purely as docstrings."""

    found: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, _PY_NODES):
            continue
        body = getattr(node, "body", [])
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            found.add(id(body[0].value))
    return found


def _attribute_chain(node: ast.AST) -> str | None:
    parts: list[str] = []
    current: ast.AST = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name) and current.id:
        parts.append(current.id)
        return ".".join(reversed(parts))
    return None


def _dotted_variants(value: str) -> set[str]:
    """Every dotted prefix and suffix of a dotted name (``a.b.c`` family)."""

    segments = value.split(".")
    variants = {value}
    for index in range(1, len(segments)):
        variants.add(".".join(segments[:index]))
        variants.add(".".join(segments[index:]))
    return variants


def _python_reference_values(tree: ast.AST) -> set[str]:
    """Executable references a candidate actually *uses*.

    Only load-context identifiers, attribute chains and ``from <module> import``
    modules count; a bare ``import http.server`` without any use is deliberately
    ignored so it stays an ordinary-residue signal rather than a cheat.
    """

    values: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id:
            values.add(node.id)
        elif isinstance(node, ast.Attribute):
            chain = _attribute_chain(node)
            if chain:
                values.update(_dotted_variants(chain))
        elif isinstance(node, ast.ImportFrom) and node.module:
            values.update(_dotted_variants(node.module))
    return values


def _python_folded_strings(
    tree: ast.AST,
    bindings: Mapping[str, str],
    numbers: Mapping[str, Sequence[int]],
) -> set[str]:
    """Folded string constants that are *not* docstrings."""

    docstrings = _python_docstring_ids(tree)
    values: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and id(node) in docstrings:
            continue
        folded = _fold_python_string(node, bindings, numbers)
        if folded:
            values.add(folded)
    return values


def _symbol_value_matches(value: str, needle: str) -> bool:
    text = value.casefold().strip()
    if not text:
        return False
    if text == needle:
        return True
    if text.startswith(needle + "."):
        return True
    if text.endswith("." + needle):
        return True
    return ("." + needle + ".") in text


def _docstring_spans(tree: ast.AST) -> list[tuple[int, int, int, int]]:
    spans: list[tuple[int, int, int, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, _PY_NODES):
            continue
        body = getattr(node, "body", [])
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            constant = body[0].value
            spans.append(
                (
                    int(getattr(constant, "lineno", 0) or 0),
                    int(getattr(constant, "col_offset", 0) or 0),
                    int(getattr(constant, "end_lineno", 0) or 0),
                    int(getattr(constant, "end_col_offset", 0) or 0),
                )
            )
    return spans


def _comment_spans(text: str) -> list[tuple[int, int, int, int]]:
    spans: list[tuple[int, int, int, int]] = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type == tokenize.COMMENT:
                spans.append((token.start[0], token.start[1], token.end[0], token.end[1]))
    except (tokenize.TokenError, IndentationError, SyntaxError, UnicodeError, ValueError):
        return spans
    return spans


def _blank_spans(text: str, spans: Sequence[tuple[int, int, int, int]]) -> str:
    """Replace every spanned character with a space, preserving newlines."""

    if not spans:
        return text
    lines = text.splitlines(keepends=True)
    offsets: list[int] = []
    cursor = 0
    for line in lines:
        offsets.append(cursor)
        cursor += len(line)
    offsets.append(cursor)

    def char_index(row: int, column: int) -> int:
        if row < 1:
            return 0
        if row > len(lines):
            return offsets[-1]
        return min(offsets[row - 1] + column, offsets[-1])

    characters = list(text)
    for start_row, start_col, end_row, end_col in spans:
        start = char_index(start_row, start_col)
        end = char_index(end_row or start_row, end_col or start_col)
        for index in range(start, max(start, end)):
            if characters[index] != "\n":
                characters[index] = " "
    return "".join(characters)


def _python_code_surface(text: str) -> str:
    """Source text with comments and docstrings blanked out.

    Used only for the non-identifier pattern targets (for example
    ``ThreadingHTTPServer((HOST``) so that a commented-out or docstring note
    mentioning the old technology cannot masquerade as an executable pattern.
    """

    spans: list[tuple[int, int, int, int]] = []
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        tree = None
    if tree is not None:
        spans.extend(_docstring_spans(tree))
    spans.extend(_comment_spans(text))
    return _blank_spans(text, spans)


def _python_symbol_contains(
    path: Path,
    target: str,
    *,
    dynamic_only: bool = False,
    require_present: Sequence[str] = (),
) -> bool:
    """Semantic residue match for Python sources.

    The module/class name is resolved through the AST instead of a raw text
    substring, so a commented-out or docstring note can never trigger a hit.
    Three surfaces are consulted:

    * executable references the candidate actually *uses* -- load-context
      identifiers, attribute chains and ``from <module> import`` modules;
    * string constants -- including ``+``/f-string/``str.format``/``%``/
      ``join`` concatenations, ``chr(code)``, ``bytes([codes]).decode(...)``,
      ``bytes.fromhex(...).decode(...)`` and ``base64.b64decode(...).decode(...)``
      -- constant-folded first, so an obfuscated
      ``__import__("ht" + "tp." + "server")`` still resolves to the target
      symbol;
    * a comment/docstring-stripped text scan for non-identifier patterns such as
      ``ThreadingHTTPServer((HOST``.

    ``dynamic_only`` targets are generic mechanism modules (``subprocess``,
    ``importlib``): a plain ``import subprocess`` plus ``subprocess.run(...)`` is
    ordinary target code, so those only match when the name is rebuilt
    dynamically (a folded string constant).

    ``require_present`` adds a per-file precondition: at least one marker must
    appear among the executable references / folded strings.  It is used to tell
    a *migrated* helper (Flask/Werkzeug present) that hides a source fallback
    apart from an *unmigrated* helper whose whole implementation is still the
    source technology (the latter is ordinary residue, not a cheat).
    """

    if path.suffix != ".py":
        return False
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    needle = target.casefold().strip()
    if not needle:
        return False
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        tree = None
    numbers = _python_numeric_bindings(tree) if tree is not None else {}
    bindings = _python_string_bindings(tree, numbers) if tree is not None else {}
    folded = _python_folded_strings(tree, bindings, numbers) if tree is not None else set()
    references = _python_reference_values(tree) if tree is not None else set()
    if require_present:
        surface = references | folded
        markers = [marker.casefold().strip() for marker in require_present]
        if not any(
            any(_symbol_value_matches(value, marker) for value in surface)
            for marker in markers
        ):
            return False
    if not _SYMBOL_RE.match(target.strip()):
        return needle in _python_code_surface(text).casefold()
    if tree is None:
        return False
    if dynamic_only:
        return any(_symbol_value_matches(value, needle) for value in folded)
    return any(
        _symbol_value_matches(value, needle) for value in references | folded
    )


def _matches(
    root: Path,
    path: Path,
    *,
    kind: str,
    target: str,
    dynamic_only: bool = False,
    require_present: Sequence[str] = (),
) -> bool:
    if kind == "literal":
        return _literal_contains(path, target)
    if kind == "dependency":
        return _dependency_contains(path, target)
    if kind == "import":
        return _import_contains(path, target)
    if kind == "python_symbol":
        return _python_symbol_contains(
            path, target, dynamic_only=dynamic_only, require_present=require_present
        )
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
    for row in rules:
        if not isinstance(row, Mapping):
            continue
        identifier = row.get("id")
        kind = row.get("kind")
        target = row.get("target")
        patterns = row.get("paths", [])
        exclude = row.get("exclude", [])
        exclude_paths = row.get("exclude_paths", [])
        require_present = row.get("require_present", [])
        if exclude is None:
            exclude = []
        if exclude_paths is None:
            exclude_paths = []
        if require_present is None:
            require_present = []
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
            or not isinstance(exclude, Sequence)
            or isinstance(exclude, (str, bytes))
            or any(not isinstance(item, str) or not item for item in exclude)
            or not isinstance(exclude_paths, Sequence)
            or isinstance(exclude_paths, (str, bytes))
            or any(
                not isinstance(item, str) or not _safe_relative(item)
                for item in exclude_paths
            )
            or not isinstance(require_present, Sequence)
            or isinstance(require_present, (str, bytes))
            or any(not isinstance(item, str) or not item for item in require_present)
        ):
            raise ValueError(f"invalid residue rule: {identifier or '<unknown>'}")
        if kind not in _RULE_KINDS:
            continue
        dynamic_only = bool(row.get("dynamic_only"))
        for path in _paths(root, tuple(patterns)):
            # ``exclude`` lists literal target-technology markers: when any is
            # present in the same file the rule is suppressed.  This keeps a
            # coarse source token (for example ``server.serve_forever()``) from
            # firing on an idiomatic target implementation that reuses the same
            # method name (Werkzeug ``make_server(...).serve_forever()``).
            if exclude and any(_literal_contains(path, token) for token in exclude):
                continue
            # ``exclude_paths`` prunes whole files by repo-relative pattern, so a
            # rule can scan the entire tree while still ignoring the migration
            # unit's own helper files (which the paired ``require_present`` rule
            # covers instead).
            if exclude_paths:
                relative = path.relative_to(root).as_posix()
                if any(fnmatchcase(relative, pattern) for pattern in exclude_paths):
                    continue
            matched = _matches(
                root,
                path,
                kind=str(kind),
                target=target,
                dynamic_only=dynamic_only,
                require_present=tuple(require_present),
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
