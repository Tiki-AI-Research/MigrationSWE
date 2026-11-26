"""Language-neutral evaluation of declarative residue rules."""

from __future__ import annotations

import json
import re
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Iterable, Mapping, Sequence

try:
    from .go_source import GoSource
except ImportError:  # Standalone verifier layout.
    from go_source import GoSource


# Security gate: the scan face must match the *build* face.  `go build ./...`
# compiles every Go package under the module except dot/underscore-prefixed
# directories, `testdata` and `vendor`, so a source-fallback hidden anywhere
# else (a new top-level package, `build/`, and even `node_modules/`, which Go
# happily builds) is real, buildable code.  Dot/underscore-prefixed names are
# already skipped by the prefix rule below; only `testdata` is additionally
# excluded by name because Go's `./...` pattern does too.
_EXCLUDED_PARTS = {"testdata"}
_RULE_KINDS = {"literal", "dependency", "import", "file", "go_http"}


def _safe_relative(value: str) -> bool:
    path = Path(value)
    return bool(value) and not path.is_absolute() and ".." not in path.parts and "\\" not in value


def _eligible(root: Path, path: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    if not path.is_file() or path.is_symlink():
        return False
    for part in relative.parts:
        if part in _EXCLUDED_PARTS or part.startswith(".") or part.startswith("_"):
            return False
    return True


def _paths(root: Path, patterns: Sequence[str], exclude: Sequence[str] = ()) -> Iterable[Path]:
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
    unique = {
        path.resolve(): path for path in candidates if _eligible(root, path)
        and not any(fnmatchcase(path.relative_to(root).as_posix(), item) for item in exclude)
    }
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


def _go_views(text: str) -> tuple[str, str, list[str]]:
    """Return (code_view, no_comments, comments) preserving character offsets.

    ``code_view`` blanks comment and string-literal *contents* so prose or data
    strings cannot satisfy a sink rule; ``no_comments`` blanks only comments so
    code/string tokens stay readable; ``comments`` keeps the raw comment bodies
    for directive detection (``//go:linkname``, ``//export``).

    Strings and comments are resolved in a single lexical pass so a ``//`` that
    occurs *inside* a string literal (``"https://"``) is not mistaken for a
    comment; that mistake would swallow the closing quote and blank following
    code (hiding a real sink such as ``os.StartProcess``).
    """

    code_chars = list(text)
    no_comments_chars = list(text)
    comments: list[str] = []
    index = 0
    length = len(text)
    state = "normal"
    quote = ""
    while index < length:
        character = text[index]
        if state == "normal":
            if character == "/" and index + 1 < length and text[index + 1] == "/":
                end = index
                while end < length and text[end] != "\n":
                    end += 1
                comments.append(text[index:end])
                for offset in range(index, end):
                    code_chars[offset] = " "
                    no_comments_chars[offset] = " "
                index = end
                continue
            if character == "/" and index + 1 < length and text[index + 1] == "*":
                end = index + 2
                while end + 1 < length and not (
                    text[end] == "*" and text[end + 1] == "/"
                ):
                    end += 1
                end = end + 2 if end + 1 < length else length
                comments.append(text[index:end])
                for offset in range(index, end):
                    no_comments_chars[offset] = " "
                    if text[offset] != "\n":
                        code_chars[offset] = " "
                index = end
                continue
            if character in ('"', "`", "'"):
                state = "string"
                quote = character
                index += 1
                continue
            index += 1
            continue
        if quote != "`" and character == "\\":
            if index + 1 < length:
                if text[index + 1] != "\n":
                    code_chars[index + 1] = " "
                index += 2
            else:
                index += 1
            continue
        if character == quote:
            state = "normal"
            index += 1
            continue
        if character != "\n":
            code_chars[index] = " "
        index += 1
    return "".join(code_chars), "".join(no_comments_chars), comments


_GO_IMPORT_SPEC = re.compile(
    r'(?:(?P<alias>[A-Za-z_][A-Za-z0-9_]*|\.)\s+)?"(?P<path>[^"\n]*)"'
)

# Whole Go tokens (identifiers, numbers, ``...``, single punctuation).  Scanned
# against the comment/string-blanked ``code_view`` so whitespace never separates
# a selector's parts and ``...`` cannot be mistaken for the selector dot.
_GO_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[0-9]+|\.\.\.|[^\sA-Za-z0-9_]")


def _parse_go_imports(no_comments: str) -> list[tuple[str, str]]:
    """Parse ``import`` declarations into ``(local_alias, import_path)`` pairs.

    ``local_alias`` is the explicit alias, ``"."`` for a dot import, or ``""``
    when the file uses the path's base name as the package identifier.
    """

    imports: list[tuple[str, str]] = []
    length = len(no_comments)
    for match in re.finditer(r"\bimport\b", no_comments):
        index = match.end()
        while index < length and no_comments[index] in " \t\r\n":
            index += 1
        if index < length and no_comments[index] == "(":
            depth = 1
            cursor = index + 1
            while cursor < length and depth:
                if no_comments[cursor] == "(":
                    depth += 1
                elif no_comments[cursor] == ")":
                    depth -= 1
                cursor += 1
            body = no_comments[index + 1 : cursor - 1 if cursor <= length else length]
        else:
            spec = _GO_IMPORT_SPEC.match(no_comments[index:])
            if spec:
                imports.append((spec.group("alias") or "", spec.group("path")))
            continue
        for spec in _GO_IMPORT_SPEC.finditer(body):
            imports.append((spec.group("alias") or "", spec.group("path")))
    return imports


def _directive_present(target: str, comments: Sequence[str]) -> bool:
    pattern = re.compile(re.escape(target) + r"\b")
    return any(pattern.search(comment) for comment in comments)


def _symbol_pattern(symbol: str) -> str:
    """A trailing ``*`` makes the token a prefix family (``Dial*`` -> ``Dial\\w*``)."""

    prefix = symbol[:-1]
    if prefix:
        return re.escape(prefix) + r"[A-Za-z0-9_]*"
    return r"[A-Za-z0-9_]+"


def _selector_present(
    package: str, symbol: str, imports: Sequence[tuple[str, str]], code_view: str
) -> bool:
    """Match ``package.symbol`` by Go selector semantics, not raw substrings.

    Aliases are taken from the file's real ``import`` declarations (explicit
    alias, dot import, or the import path's base name), so an unrelated nested
    field named after the package does not bind.  On the token stream produced
    from the already comment/string-blanked ``code_view``, whitespace, newlines
    and comments around ``.`` are insignificant (``net . Dial``,
    ``os /*c*/ . StartProcess``), but the package qualifier must be the *head*
    of its selector chain — ``cfg.net.DialTimeout`` is a field access, not the
    imported ``net`` package.
    """

    aliases: set[str] = set()
    dot_import = False
    for alias, import_path in imports:
        base = import_path.rstrip("/").rsplit("/", 1)[-1]
        if import_path != package and base != package:
            continue
        if alias == ".":
            dot_import = True
        elif alias and alias != "_":
            aliases.add(alias)
        elif base:
            aliases.add(base)
    tokens = _GO_TOKEN.findall(code_view)
    symbol_pattern = re.compile(_symbol_pattern(symbol))
    for index, token in enumerate(tokens):
        if token not in aliases or (index and tokens[index - 1] == "."):
            continue
        if (
            index + 2 < len(tokens)
            and tokens[index + 1] == "."
            and symbol_pattern.fullmatch(tokens[index + 2])
        ):
            return True
    if dot_import:
        for index, token in enumerate(tokens):
            if (
                symbol_pattern.fullmatch(token)
                and index + 1 < len(tokens)
                and tokens[index + 1] == "("
            ):
                return True
    return False


def _go_mechanism_contains(path: Path, target: str) -> bool:
    """Resolve a declarative token against Go source *semantics*, not substrings.

    Import-path tokens match the parsed import set (alias-insensitive); selector
    tokens (``pkg.Symbol``) bind through the file's import aliases (including
    dot imports); directive tokens match real comment directives.  ``#cgo`` is
    only meaningful alongside ``import "C"``, so a bare ``// #cgo`` prose line
    is not a fallback.  A bare source-runtime name (``cargo``/``rustc``/…) is
    resolved against the *comment-stripped* view: a code string such as
    ``var _ = "cargo"`` counts as evidence, while prose inside comments does
    not (fixes the comment false positive without weakening the code-string
    control the QC archives as a positive sample).
    """

    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    code_view, no_comments, comments = _go_views(text)
    imports = _parse_go_imports(no_comments)
    token = target.strip()
    if token.startswith("import"):
        spec = re.fullmatch(r'"([^"\n]+)"', token[len("import") :].strip())
        if spec:
            return any(import_path == spec.group(1) for _, import_path in imports)
        return token.casefold() in code_view.casefold()
    if re.fullmatch(r'"[^"\n]+"', token):
        wanted = token[1:-1]
        return any(import_path == wanted for _, import_path in imports)
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+", token) and "." in token.split("/")[0]:
        return any(token in import_path for _, import_path in imports)
    if token == "#cgo":
        directive = any(
            comment.lstrip().startswith("//")
            and comment.lstrip()[2:].lstrip().startswith("#cgo")
            for comment in comments
        )
        return directive and any(import_path == "C" for _, import_path in imports)
    if token.startswith("//") or token.startswith("#"):
        return _directive_present(token, comments)
    selector = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*\*?)", token)
    if selector:
        return _selector_present(selector.group(1), selector.group(2), imports, code_view)
    return token.casefold() in no_comments.casefold()


def _http_client_references(source: GoSource, types: set[str]) -> list[tuple[int, int]]:
    refs = source.references('net/http', {'Client', 'Transport', 'DefaultClient', 'DefaultTransport'})
    refs.extend((index, index) for index, item in enumerate(source.tokens)
                if item in types and (not index or source.tokens[index - 1] != '.'))
    return refs


def _go_http_contains(
    path: Path, *, source: GoSource | None = None,
    shared_clients: set[str] | None = None, shared_senders: set[str] | None = None,
    factories: set[str] | None = None,
    client_types: set[str] | None = None,
    imported_clients: Sequence[tuple[int, int]] = (),
    imported_factories: Sequence[tuple[int, int]] = (),
) -> bool:
    """Identify outbound HTTP operations, not a harmless net/http import.

    Resolve package calls, function aliases, default clients/transports, and
    explicitly typed or constructed clients. A Request, status constant,
    ResponseWriter, or an idle Client alone is not a network operation.
    This supplements the independent RPC, raw-socket and process rules.
    """
    if source is None:
        try:
            source = GoSource(path.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            return False
    if source.calls("net/http", {"Get", "Head", "Post", "PostForm"}):
        return True
    items = source.tokens
    methods = {"Do", "Get", "Head", "Post", "PostForm", "RoundTrip"}
    clients = set(shared_clients or ())
    senders = set(shared_senders or ())
    factories = set(factories or ())
    for start, end in [*_http_client_references(source, client_types or set()), *imported_clients]:
        # Calls on a default client, composite literal or method expression.
        cursor = end + 1
        if cursor < len(items) and items[cursor] == '{':
            depth = 1
            cursor += 1
            while cursor < len(items) and depth:
                depth += (items[cursor] == '{') - (items[cursor] == '}')
                cursor += 1
        while cursor < len(items) and items[cursor] == ')':
            cursor += 1
        method_value = (cursor + 1 < len(items) and items[cursor] == '.'
                        and items[cursor + 1] in methods)
        if method_value and cursor + 2 < len(items) and items[cursor + 2] == '(':
            return True

        boundary = start - 1
        while boundary >= 0 and items[boundary] not in {';', '{', '}'}:
            boundary -= 1
        before = items[boundary + 1:start]
        assignment = max((index for index, item in enumerate(before)
                          if item in {'=', ':='}), default=-1)
        if assignment >= 0:
            left = before[:assignment]
            # c := &http.Client{}; holder.client = http.DefaultClient.
            if left and left[-1].isidentifier() and left[-1] != '_':
                (senders if method_value else clients).add(left[-1])
        else:
            # var c *http.Client; a function parameter or a struct field.
            cursor = start - 1
            while cursor >= 0 and items[cursor] == '*':
                cursor -= 1
            if cursor >= 0 and items[cursor].isidentifier() and items[cursor] != '_':
                clients.add(items[cursor])

    for start, end in imported_factories:
        invoked = end + 1 < len(items) and items[end + 1] == '('
        if invoked:
            close = source.closing(end + 1)
            if (close + 3 < len(items) and items[close + 1] == '.'
                    and items[close + 2] in methods and items[close + 3] == '('):
                return True
        boundary = start - 1
        while boundary >= 0 and items[boundary] not in {';', '{', '}'}:
            boundary -= 1
        before = items[boundary + 1:start]
        if len(before) >= 2 and before[-1] in {'=', ':='} and before[-2].isidentifier():
            if before[-2] != '_':
                (clients if invoked else factories).add(before[-2])

    # Propagate straightforward value aliases, including extracting a method
    # value (send := client.Do; send(req)). Network calls through a method value
    # must not bypass the check just because the selector is not at the call.
    for _ in range(len(items)):
        before = (len(clients), len(senders), len(factories))
        for index in range(2, len(items)):
            if items[index - 1] not in {'=', ':='} or not items[index - 2].isidentifier():
                continue
            name = items[index - 2]
            if name == '_':
                continue
            if items[index] in factories:
                if index + 1 < len(items) and items[index + 1] == '(':
                    clients.add(name)
                else:
                    factories.add(name)
            elif (items[index] in clients and index + 2 < len(items)
                    and items[index + 1] == '.' and items[index + 2] in methods):
                senders.add(name)
            elif items[index] in clients:
                clients.add(name)
            elif items[index] in senders:
                senders.add(name)
        if before == (len(clients), len(senders), len(factories)):
            break
    for index, item in enumerate(items):
        if item in senders and index + 1 < len(items) and items[index + 1] == '(':
            return True
        if item in factories and index + 1 < len(items) and items[index + 1] == '(':
            end = source.closing(index + 1)
            if (end + 3 < len(items) and items[end + 1] == '.'
                    and items[end + 2] in methods and items[end + 3] == '('):
                return True
        if (item in clients and index + 3 < len(items) and items[index + 1] == '.'
                and items[index + 2] in methods and items[index + 3] == '('):
            return True
    return False


def _go_http_package_hits(root: Path, paths: Sequence[Path]) -> set[Path]:
    """Carry explicitly typed globals, fields and client factories across files.

    Go imports are file-local, but declarations are package-wide. Without this
    step, moving a Client's declaration into a sibling file would hide Do/Get
    calls. No candidate code is imported or executed during this analysis.
    """
    packages: dict[Path, dict[Path, GoSource]] = {}
    for path in paths:
        if path.suffix != '.go':
            continue
        try:
            packages.setdefault(path.parent, {})[path] = GoSource(path.read_text(encoding='utf-8', errors='ignore'))
        except OSError:
            continue
    if not any(package == 'net/http' for sources in packages.values()
               for source in sources.values() for _alias, package in source.imports):
        return set()
    hits: set[Path] = set()
    bindings = {}
    for directory, sources in packages.items():
        clients: set[str] = set()
        senders: set[str] = set()
        factories: set[str] = set()
        types: set[str] = set()
        for _ in range(sum(len(source.tokens) for source in sources.values())):
            before = len(types)
            for source in sources.values():
                for start, _end in _http_client_references(source, types):
                    items = source.tokens
                    if start >= 3 and items[start - 3] == 'type' and items[start - 1] == '=':
                        types.add(items[start - 2])
            if len(types) == before:
                break
        for source in sources.values():
            items = source.tokens
            for start, end in _http_client_references(source, types):
                stack: list[int] = []
                boundary = -1
                for index in range(start):
                    if items[index] == '{':
                        stack.append(index)
                    elif items[index] == '}' and stack:
                        stack.pop()
                    if items[index] in {';', '{', '}'}:
                        boundary = index
                before = items[boundary + 1:start]
                assignment = max((i for i, item in enumerate(before) if item in {'=', ':='}), default=-1)
                if not stack and assignment >= 1 and before[0] != 'type':
                    name = before[assignment - 1]
                    if name.isidentifier() and name != '_':
                        method_value = end + 2 < len(items) and items[end + 1] == '.'
                        (senders if method_value else clients).add(name)
                elif not stack and before and before[0] == 'var' and assignment < 0:
                    clients.update(item for item in before[1:] if item.isidentifier())
                elif stack and stack[-1] and items[stack[-1] - 1] == 'struct':
                    # Named fields and embedded *http.Client fields.
                    left = [item for item in before if item != '*']
                    name = left[-1] if left else items[end]
                    if name.isidentifier():
                        clients.add(name)
                elif not stack and before and before[0] == 'func':
                    cursor = boundary + 2
                    if cursor < len(items) and items[cursor] == '(':
                        cursor = source.closing(cursor) + 1  # method receiver
                    if cursor + 1 < len(items) and items[cursor + 1] == '(':
                        close = source.closing(cursor + 1)
                        if close < start:
                            factories.add(items[cursor])
        bindings[directory] = (clients, senders, factories, types)
    try:
        module = re.search(r'^module\s+(\S+)', (root / 'go.mod').read_text(), re.M)
    except OSError:
        module = None
    imports_to_directories = {
        module[1].rstrip('/') + ('/' + directory.relative_to(root).as_posix()
                                if directory != root else ''): directory
        for directory in packages
    } if module else {}
    for directory, sources in packages.items():
        clients, senders, factories, types = bindings[directory]
        for path, source in sources.items():
            imported_clients = []
            imported_factories = []
            imported_call = False
            for _alias, package in source.imports:
                other = imports_to_directories.get(package)
                if other not in bindings:
                    continue
                other_clients, other_senders, other_factories, other_types = bindings[other]
                imported_call = imported_call or bool(source.calls(package, {
                    name for name in other_senders if name[:1].isupper()}))
                imported_clients.extend(source.references(package, {
                    name for name in other_clients | other_types if name[:1].isupper()}))
                imported_factories.extend(source.references(package, {
                    name for name in other_factories if name[:1].isupper()}))
            if imported_call or _go_http_contains(path, source=source, shared_clients=clients,
                                 shared_senders=senders, factories=factories, client_types=types,
                                 imported_clients=imported_clients, imported_factories=imported_factories):
                hits.add(path)
    return hits


_GO_MOD_DIRECTIVE = re.compile(r"^(require|replace|exclude)\b")


def _go_mod_module_paths(line: str) -> list[str]:
    """Return go.mod module-path tokens from one directive body."""

    body = line.strip()
    if not body:
        return []
    body = body.split("//", 1)[0].strip()
    if "=>" in body:
        left, right = body.split("=>", 1)
        tokens = left.split() + right.split()
    else:
        tokens = body.split()
    return [
        token
        for token in tokens
        if re.fullmatch(r"[A-Za-z0-9._~\-]+(?:/[A-Za-z0-9._~\-]+)*", token)
    ]


def _go_mod_modules(text: str) -> set[str]:
    """Extract module paths from go.mod ``require``/``replace``/``exclude``.

    Handles both single-line directives and parenthesised blocks, and ignores
    trailing ``// indirect`` style comments.  This closes the gap where a
    candidate declares a third-party module whose Go syntax the npm-style
    dependency matcher could never parse.
    """

    modules: set[str] = set()
    lines = [raw.split("//", 1)[0].rstrip() for raw in text.splitlines()]
    index = 0
    count = len(lines)
    while index < count:
        stripped = lines[index].strip()
        block = re.match(r"^(require|replace|exclude)\s*\($", stripped)
        if block:
            index += 1
            while index < count and lines[index].strip() != ")":
                modules.update(_go_mod_module_paths(lines[index]))
                index += 1
            index += 1
            continue
        directive = _GO_MOD_DIRECTIVE.match(stripped)
        if directive:
            body = stripped[directive.end() :]
            modules.update(_go_mod_module_paths(body))
        index += 1
    return modules


def _dependency_contains(path: Path, target: str) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    if path.name == "go.mod" or path.suffix == ".mod":
        if any(_target_matches(module, target) for module in _go_mod_modules(text)):
            return True
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


def _matches(
    root: Path, path: Path, *, kind: str, target: str, go_mechanism: bool = False
) -> bool:
    if kind == "go_http":
        return path.suffix == ".go" and _go_http_contains(path)
    if go_mechanism and kind == "literal" and path.suffix == ".go":
        return _go_mechanism_contains(path, target)
    if kind == "literal":
        return _literal_contains(path, target)
    if kind == "dependency":
        return _dependency_contains(path, target)
    if kind == "import":
        return _import_contains(path, target)
    relative = path.relative_to(root).as_posix()
    return fnmatchcase(relative.casefold(), target.casefold()) or fnmatchcase(
        path.name.casefold(), target.casefold()
    )


def scan_residue_rules(
    root: Path, rules: object, *, go_mechanism: bool = False
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
            or any(not isinstance(item, str) or not _safe_relative(item) for item in exclude)
        ):
            raise ValueError(f"invalid residue rule: {identifier or '<unknown>'}")
        if kind not in _RULE_KINDS:
            continue
        if kind == "go_http" and target != "net/http":
            raise ValueError(f"invalid Go HTTP rule target: {target}")
        paths = tuple(_paths(root, tuple(patterns), tuple(exclude)))
        http_hits = _go_http_package_hits(root, paths) if kind == "go_http" else set()
        for path in paths:
            matched = path in http_hits if kind == "go_http" else _matches(
                root, path, kind=str(kind), target=target, go_mechanism=go_mechanism)
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
    root: Path, rules: object, *, go_mechanism: bool = False
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
        hits = scan_residue_rules(root, [row], go_mechanism=go_mechanism)
        diagnostics.append(
            {
                "rule_id": str(identifier),
                "kind": str(kind),
                "status": "FAIL" if hits else "PASS",
                "hits": hits,
            }
        )
    return diagnostics


def _selfcheck() -> None:
    """Lock the R8 selector-dot bypass shut with synthetic Go fixtures.

    Every positive must bind through alias/dot-import and arbitrary
    whitespace/comments around ``.``; every negative must stay unbound.  The
    same fixtures fail against the pre-fix whitespace-sensitive selector regex,
    so a regression cannot silently reopen the bypass.
    """

    import tempfile

    positives = (
        ("net-direct", "net.Dial*", 'package p\nimport "net"\nvar _ = net.DialTimeout\n'),
        ("net-space", "net.Dial*", 'package p\nimport "net"\nfunc f() { net . DialTimeout("tcp", "h:1", 0) }\n'),
        ("net-newline", "net.Dial*", 'package p\nimport "net"\nfunc f() { net.\nDialTimeout("tcp", "h:1", 0) }\n'),
        (
            "net-alias-comment",
            "net.Listen*",
            'package p\nimport n "net"\nfunc f() { n /* alias */ . Listen("tcp", "h:1") }\n',
        ),
        ("net-dotimport", "net.Dial*", 'package p\nimport . "net"\nfunc f() { DialTimeout("tcp", "h:1", 0) }\n'),
        (
            "os-dot-comment",
            "os.StartProcess",
            'package p\nimport "os"\nfunc f() { os . /* spawn */ StartProcess("/x", nil, nil) }\n',
        ),
    )
    negatives = (
        ("net-ip", "net.Dial*", 'package p\nimport "net"\nvar _ = net.IP{}\n'),
        ("os-getenv", "os.StartProcess", 'package p\nimport "os"\nvar _ = os.Getenv("HOME")\n'),
        ("comment-mention", "net.Dial*", "package p\n// net . DialTimeout must not run\n"),
        ("string-mention", "os.StartProcess", 'package p\nvar _ = "os . StartProcess"\n'),
        ("nested-field", "net.Dial*", 'package p\nfunc f() { cfg . net . DialTimeout("tcp", "h:1", 0) }\n'),
        ("nested-field-tight", "net.Dial*", 'package p\nfunc f() { cfg.net.DialTimeout("tcp", "h:1", 0) }\n'),
        ("no-import", "net.Dial*", 'package p\nfunc f() { net.DialTimeout("tcp", "h:1", 0) }\n'),
    )
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        for name, target, source in positives:
            path = root / f"{name}.go"
            path.write_text(source, encoding="utf-8")
            assert _go_mechanism_contains(path, target), f"selector missed: {name}"
        for name, target, source in negatives:
            path = root / f"{name}.go"
            path.write_text(source, encoding="utf-8")
            assert not _go_mechanism_contains(path, target), f"selector false positive: {name}"


if __name__ == "__main__":  # pragma: no cover - manual selfcheck entry point
    _selfcheck()
    print("residue selfcheck: PASS")


__all__ = ["classify_residue_rules", "scan_residue_rules"]
