#!/usr/bin/env python3
"""Shared semantic scanner for the migration hard gate and the verifier.

This module is the *single source of truth* (quality-check item 116) for how a
source-framework declaration is recognised and classified:

* ``ordinary`` -- a plain literal reference to the old framework. It is a
  completeness signal (``migration_scope_complete``); an honest but incomplete
  migration must not be punished as cheating (item 114).
* ``hidden``   -- an *indirect / obfuscated* reference (split-string
  concatenation, ``Array.join``, template interpolation, aliased ``require``,
  ``module.require`` / ``process.binding`` indirection, runtime string decode).
  That is a deliberate attempt to keep the source runtime reachable while
  hiding it from a literal scan, so it belongs to
  ``no_deceptive_source_fallback`` (cheating, item 113).

It also owns the embedded-runtime / binary / archive detection. Node API style
and data-decoding mechanisms are not migration requirements. Both ``hardgate.py`` and
``verifier.py`` import these helpers instead of maintaining their own regexes,
so the two layers can never classify the same text two different ways.
"""
from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from js_module_scan import module_load_findings

# ---------------------------------------------------------------------------
# module identities
# ---------------------------------------------------------------------------
DEFAULT_SOURCE_MODULES = ("express", "body-parser", "multer")
DEFAULT_RUNTIME_MODULES = (
    "child_process",
    "net",
    "dgram",
    "dns",
    "worker_threads",
    "vm",
    "http",
    "https",
    "http2",
    "undici",
)

#: Base labels produced by the shared builder that are *bridge* evidence (feed
#: the L2 anti-cheating branch) rather than asset-tampering evidence.
BRIDGE_LABELS = {
    "requires child_process",
    "spawns a subprocess",
    "requires the source framework express",
    "requires a bridging runtime module",
    "uses the fetch/WebSocket network API",
    "builds a forbidden module specifier by concatenation",
    "builds a forbidden module specifier with join()",
    "builds a forbidden module specifier from a template literal",
    "loads a source/runtime module through an aliased call",
    "accesses require through the global object",
    "loads a module through module.require / process.mainModule.require",
    "loads a module through process.binding",
    "loads a forbidden module through a constructed specifier",
    "loads a module through a computed property access",
}

# ---------------------------------------------------------------------------
# embedded-runtime / binary / archive detection (items 43/44/47)
# ---------------------------------------------------------------------------
MAGIC_SIGNATURES: "List[Tuple[bytes, str]]" = [
    (b"\x7fELF", "ELF executable/object"),
    (b"MZ", "PE/COFF executable"),
    (b"\xca\xfe\xba\xbe", "Java class or Mach-O fat binary"),
    (b"\xcf\xfa\xed\xfe", "Mach-O 64-bit binary"),
    (b"PK\x03\x04", "zip archive"),
    (b"PK\x05\x06", "zip archive (empty)"),
    (b"\x1f\x8b", "gzip archive"),
    (b"BZh", "bzip2 archive"),
    (b"\xfd7zXZ\x00", "xz archive"),
    (b"7z\xbc\xaf\x27\x1c", "7z archive"),
]

ARCHIVE_SUFFIXES = (
    ".zip", ".gz", ".tgz", ".tar", ".bz2", ".xz", ".7z", ".jar", ".war",
    ".whl", ".egg", ".pyc", ".pyo", ".class", ".jar",
)
BINARY_SUFFIXES = (
    ".so", ".dylib", ".dll", ".node", ".exe", ".o", ".a", ".ko", ".bin",
    ".wasm", ".dylib", ".jnilib",
)
CODE_SUFFIXES = (
    ".js", ".cjs", ".mjs", ".jsx", ".ts", ".tsx", ".py", ".java", ".rb",
    ".go", ".rs", ".php", ".cs",
)


# ---------------------------------------------------------------------------
# pattern builders (shared by the integrity scan and the deceptive scan)
# ---------------------------------------------------------------------------
def module_reference_patterns(modules: str, label: str) -> "List[Tuple[re.Pattern[str], str]]":
    """Match every equivalent way of naming a built-in/source module.

    ``child_process`` and ``node:child_process`` are the same module, and
    ``await import('node:child_process')`` never reaches ``require``. Matching
    only the bare ``require('child_process')`` spelling therefore lets a
    candidate bridge back to the source runtime with a one-character edit, so
    both the ``node:`` prefix and the dynamic-``import()`` form are covered.
    """
    return [
        (re.compile(rf"""(?:require|import)\s*\(\s*['"](?:node:)?(?:{modules})['"]\s*\)"""), label),
        (re.compile(rf"""from\s+['"](?:node:)?(?:{modules})['"]"""), label),
    ]


def built_module_name_patterns(modules: Sequence[str]) -> "List[Tuple[re.Pattern[str], str]]":
    """Literal-splitting evasions of a forbidden module specifier.

    ``require(['ex', 'press'].join(''))``, ``require([ex, press].join(''))`` and
    ``require('ex' + 'press')`` all load ``express`` without ever spelling it, so
    a literal scan misses them. Fragments are enumerated for the *forbidden*
    module names only -- and each fragment may be a quoted string or a bare
    identifier -- which keeps the rule precise: ``require(path.join(...))`` on a
    real relative path spells no forbidden split point and is never flagged.
    """

    def fragment(text: str) -> str:
        escaped = re.escape(text)
        return r"""(?:['"]%s['"]|\b%s\b)""" % (escaped, escaped)

    built: "List[Tuple[re.Pattern[str], str]]" = []
    for module in modules:
        for split in range(1, len(module)):
            left, right = fragment(module[:split]), fragment(module[split:])
            built.append((
                re.compile(r"%s\s*\+\s*%s" % (left, right)),
                "builds a forbidden module specifier by concatenation",
            ))
            built.append((
                re.compile(r"\[\s*%s\s*,\s*%s\s*\]\s*\.\s*join\s*\(" % (left, right)),
                "builds a forbidden module specifier with join()",
            ))
    return built


def hidden_module_construction_patterns(
    modules: Sequence[str],
) -> "List[Tuple[re.Pattern[str], str]]":
    """Indirect module construction that a literal scan cannot see.

    These are the Node analogues of the "indirect declaration" evasion (item
    113): the module name never appears as one literal, or the loader itself is
    reached through an alias. Classified as *hidden* -> L2 cheating.
    """
    names = "|".join(re.escape(m) for m in modules)
    return [
        # ``require(`...${name}...`)`` -- template interpolation builds the specifier.
        (
            re.compile(r"""(?:require|import)\s*\(\s*`[^`]*\$\{"""),
            "builds a forbidden module specifier from a template literal",
        ),
        # ``globalThis['require']('express')`` / ``global['module']``
        (
            re.compile(r"""\b(?:globalThis|global|window)\s*\[\s*['"](?:require|module)['"]\s*\]"""),
            "accesses require through the global object",
        ),
        # ``module.require('express')`` / ``process.mainModule.require(...)``
        (
            re.compile(
                r"""(?:module\s*\.\s*require|process\s*\.\s*mainModule\s*\.\s*require)\s*\("""
                r"""\s*['"](?:node:)?(?:""" + names + r""")['"]"""
            ),
            "loads a module through module.require / process.mainModule.require",
        ),
        # ``process.binding('...')`` reaches a native module without require.
        (
            re.compile(r"""\bprocess\s*\.\s*binding\s*\("""),
            "loads a module through process.binding",
        ),
        (
            re.compile(r"""\b(?:internalBinding|NativeModule\s*\.\s*require)\s*\("""),
            "loads a module through process.binding",
        ),
    ]


def aliased_module_load_patterns(modules: Sequence[str]) -> "List[Tuple[re.Pattern[str], str]]":
    """``const r = require; r('express')`` -- an alias that hides the load."""
    names = "|".join(re.escape(m) for m in modules)
    return [
        (
            re.compile(
                r"""\b[\w$.]+\s*\(\s*['"](?:node:)?(?:"""
                r"""express|body-parser|multer|"""
                r"""child_process|net|dgram|dns|worker_threads|vm|http|https|http2|undici"""
                r""")['"]\s*[,)]"""
            ),
            "loads a source/runtime module through an aliased call",
        ),
        (re.compile(r"=\s*require\b(?!\s*\()"), "aliases require without calling it"),
        (re.compile(r"=\s*module\s*\.\s*require\b(?!\s*\()"), "aliases module.require without calling it"),
    ]


#: Dynamic/indirect loader forms. A *computed* ``require``
#: (``module['req'+'uire']``) is not evidence on its own -- the legitimate
#: ``codepoint_path_control`` control loads the builtin ``path`` exactly that
#: way -- so it only becomes bridge evidence when it is combined with a
#: *forbidden* specifier built at runtime (:func:`constructed_module_evidence`).
DYNAMIC_LOADER_PATTERNS: "List[Tuple[re.Pattern[str], str]]" = [
    (re.compile(r"\brequire\s*\["), "loads a module through a computed property access"),
    (re.compile(r"\bmodule\s*\["), "loads a module through a computed property access"),
    (re.compile(r"\b(?:globalThis|global|window)\s*\["), "accesses require through the global object"),
    (re.compile(r"""\bprocess\s*\[\s*['"]env['"]\s*\]"""), "loads a module through a computed property access"),
    (re.compile(r"\bmodule\s*\.\s*require\s*\("), "loads a module through module.require / process.mainModule.require"),
    (re.compile(r"\bprocess\s*\.\s*mainModule\s*\.\s*require\s*\("),
     "loads a module through module.require / process.mainModule.require"),
    (re.compile(r"\bprocess\s*\.\s*binding\s*\("), "loads a module through process.binding"),
    (re.compile(r"\bReflect\s*\.\s*apply\s*\(\s*require"), "loads a module through an aliased call"),
    (re.compile(r"\bReflect\s*\.\s*apply\s*\(\s*module"), "loads a module through an aliased call"),
    (re.compile(r"\.\s*call\s*\(\s*module\s*,"), "loads a module through an aliased call"),
]

_INT_LITERAL = re.compile(r"^(?:0[xX][0-9a-fA-F]+|0[oO][0-7]+|0[bB][01]+|\d+)$")
_CHARCODE_CALL = re.compile(r"\bString\s*\.\s*from(?:CharCode|CodePoint)\s*\(([^()]*)\)")
_SPREAD_CHARCODE_CALL = re.compile(
    r"\bString\s*\.\s*from(?:CharCode|CodePoint)\s*\(\s*\.\.\.\s*"
    r"(?:\[\s*([^\]]*)\s*\]|([A-Za-z_$][\w$]*))\s*\)"
)
_BUFFER_BASE64 = re.compile(r"""\bBuffer\s*\.\s*from\s*\(\s*['"]([^'"]*)['"]\s*,\s*['"]base64['"]""")
_BUFFER_HEX = re.compile(r"""\bBuffer\s*\.\s*from\s*\(\s*['"]([^'"]*)['"]\s*,\s*['"]hex['"]""")
_ATOB_CALL = re.compile(r"""\batob\s*\(\s*['"]([^'"]*)['"]""")
_LITERAL_PAIR = re.compile(r"""(['"])((?:(?!\1).)*)\1\s*\+\s*(['"])((?:(?!\3).)*)\3""")
# A simple array literal bound to a variable (``const codes = [101, 120]``) or
# listed inline (``[101, 120]``). ``arr.join('')`` and ``[...arr]`` hide the
# built string from a literal scan, so the elements are resolved before the
# decoded value is compared against the forbidden module set.
_ARRAY_BINDING = re.compile(
    r"""\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*\[([^\[\]]*)\]\s*;?"""
)
_ARRAY_JOIN = re.compile(
    r"\[\s*((?:['\"][^'\"]*['\"]\s*,?\s*)+)\]\s*\.\s*join\s*\(\s*(['\"])([^'\"]*)\2\s*\)"
)
_QUOTED_ITEM = re.compile(r"""['"]([^'"]*)['"]""")
_PLAIN_INT_ITEM = re.compile(r"^\s*(?:0[xX][0-9a-fA-F]+|0[oO][0-7]+|0[bB][01]+|\d+)\s*$")


def _decode_int_literal(token: str) -> "Optional[int]":
    token = token.strip().replace("_", "")
    if not _INT_LITERAL.match(token):
        return None
    try:
        return int(token, 0)
    except ValueError:
        return None


def _printable(value: str) -> bool:
    return bool(value) and 0 < len(value) <= 64 and all(32 <= ord(ch) < 127 for ch in value)


def _b64(text: str) -> Optional[str]:
    import base64
    try:
        raw = base64.b64decode(text + "=" * (-len(text) % 4), validate=False)
    except Exception:  # noqa: BLE001
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def decoded_string_literals(text: str) -> "List[str]":
    """Best-effort evaluation of runtime string construction inside *text*.

    ``String.fromCharCode(...)`` / ``String.fromCodePoint(...)`` with numeric
    literal arguments (or a spread of a literal/variable-bound array),
    ``Buffer.from(<literal>, 'base64'|'hex')`` and ``atob(<literal>)`` are
    evaluated, and ``'ex' + 'press'`` chains and ``['ex', 'press'].join('')``
    are folded. Only printable, short results are returned so the caller can
    compare them against the forbidden module set instead of flagging the
    *mechanism* (which would also punish a legitimate dynamic load of a builtin
    such as ``path``).
    """
    found: "List[str]" = []

    def push_char_codes(parts: str) -> None:
        codes = [_decode_int_literal(part) for part in parts.split(",")]
        if codes and all(code is not None for code in codes) and all(0 <= code <= 0x10FFFF for code in codes):
            value = "".join(chr(code) for code in codes)
            if _printable(value):
                found.append(value)

    for match in _CHARCODE_CALL.finditer(text):
        push_char_codes(match.group(1))
    for match in _SPREAD_CHARCODE_CALL.finditer(text):
        inline, name = match.group(1), match.group(2)
        if inline is not None:
            push_char_codes(inline)
            continue
        bound = _bound_array(text, name)
        if bound:  # ``String.fromCodePoint(...legacyCodes)``
            push_char_codes(", ".join(bound))
    for match in _ARRAY_JOIN.finditer(text):
        items = _QUOTED_ITEM.findall(match.group(1))
        if items:
            value = match.group(3).join(items)
            if _printable(value):
                found.append(value)
    for pattern in (_BUFFER_BASE64, _ATOB_CALL):
        for match in pattern.finditer(text):
            value = _b64(match.group(1))
            if value and _printable(value):
                found.append(value)
    for match in _BUFFER_HEX.finditer(text):
        try:
            value = bytes.fromhex(match.group(1)).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            continue
        if _printable(value):
            found.append(value)
    folded = text
    for _ in range(8):
        match = _LITERAL_PAIR.search(folded)
        if not match:
            break
        value = match.group(2) + match.group(4)
        folded = folded[:match.start()] + "'" + value + "'" + folded[match.end():]
        if _printable(value):
            found.append(value)
    return found


def _bound_array(text: str, name: str) -> "List[str]":
    """Elements of ``name = [...]`` when *name* is bound to a simple array literal."""
    for match in _ARRAY_BINDING.finditer(text):
        if match.group(1) != name:
            continue
        body = match.group(2).strip()
        if not body:
            continue
        items = [part.strip() for part in body.split(",") if part.strip()]
        if all(_PLAIN_INT_ITEM.match(item) for item in items):
            return items
    return []


def computed_env_names(text: str) -> "List[str]":
    """Env-var names that are *built* at runtime instead of written literally.

    ``process.env[['LEGACY', 'SOURCE', 'MODE'].join('_')]`` and
    ``env['LEGACY' + '_SOURCE']`` never spell the variable as one token, so the
    literal activation scan misses the flag and the gated branch is never
    executed (QC ADV-DYNAMIC-FALLBACK). Folding the same literal arrays /
    concatenations the decoder owns keeps the two views consistent.
    """
    names = set()
    for match in _ARRAY_JOIN.finditer(text):
        items = _QUOTED_ITEM.findall(match.group(1))
        if items:
            names.add(match.group(3).join(items))
    for value in decoded_string_literals(text):
        names.add(value)
    return sorted(
        name for name in names
        if re.fullmatch(r"[A-Z][A-Z0-9_]{2,}", name or "")
    )


def has_dynamic_loader(text: str) -> bool:
    """True when *text* reaches a module loader indirectly (computed/aliased require)."""
    return any(pattern.search(text) for pattern, _label in DYNAMIC_LOADER_PATTERNS)


def dynamic_loader_labels(text: str) -> "List[str]":
    return [label for pattern, label in DYNAMIC_LOADER_PATTERNS if pattern.search(text)]


def constructed_module_evidence(
    text: str, modules: Sequence[str] = DEFAULT_SOURCE_MODULES
) -> "List[Tuple[str, str]]":
    """Decoded/concatenated specifiers that name a *forbidden* module.

    Returns ``(decoded, label)`` pairs. The label is bridge evidence, because a
    specifier built at runtime that names the source framework or a bridging
    runtime module can only be an attempt to keep that runtime reachable while
    hiding it from a literal scan (item 113).
    """
    wanted = {str(module) for module in modules}
    evidence: "List[Tuple[str, str]]" = []
    for value in decoded_string_literals(text):
        if value in wanted:
            evidence.append((value, "loads a forbidden module through a constructed specifier"))
    return evidence


def build_static_cheat_patterns(
    source_modules: Sequence[str] = DEFAULT_SOURCE_MODULES,
    runtime_modules: Sequence[str] = DEFAULT_RUNTIME_MODULES,
) -> "List[Tuple[re.Pattern[str], str]]":
    """The full anti-cheating static pattern registry.

    Kept as one function so the hard gate never keeps a second, drifting copy
    of the source-framework patterns (item 116).
    """
    runtime = "|".join(re.escape(m) for m in runtime_modules)
    patterns: "List[Tuple[re.Pattern[str], str]]" = [
        (re.compile(r"\b(?:spawn|spawnSync|execFile|execFileSync|execSync)\s*\("), "spawns a subprocess"),
        (
            re.compile(r"\b(?:globalThis\s*\.\s*)?(?:fetch|WebSocket)\s*\("),
            "uses the fetch/WebSocket network API",
        ),
        (re.compile(r"\bmodule\s*\.\s*register\s*\("), "installs a dynamic ESM loader"),
        (re.compile(r"\beval\s*\("), "evaluates dynamic code"),
        (
            re.compile(r"""['"](/(?:tests|logs|solution)(?:/[^'"]*)?)['"]"""),
            "references evaluation asset paths",
        ),
        (
            re.compile(r"\b(?:migration_units|f2p_manifest|p2p_manifest|public_contract|"
                       r"base_tree\.sha256|answer\.json|verifier\.py|hardgate\.py)\b"),
            "references evaluation assets",
        ),
    ]
    return patterns


def ordinary_residue_patterns(modules: Sequence[str]) -> "List[re.Pattern[str]]":
    """Module references, rather than arbitrary strings naming a call mode.

    Dual Express/Koa compatibility is required. A mode label, diagnostic text,
    or documentation mentioning Express does not load the Express runtime.
    Keep actual module loads visible, including import and node: spellings.
    """
    names = "|".join(re.escape(str(module)) for module in modules)
    return [pattern for pattern, _ in module_reference_patterns(names, "source module")]


def strip_js_comments(line: str) -> str:
    """Remove ``//`` and ``/* ... */`` comments from a single source line.

    The scan is string-aware: a ``//`` that sits inside a string or template
    literal is part of the literal, not a comment start, so the rest of the
    line must stay visible. A plain ``re.sub(r"//.*$", ...)`` used to truncate
    at the literal's ``//`` and silently drop any deprecated API that shared
    the line (QC 122/125). Delegating to the stateful scanner keeps this layer
    and the anti-cheating scan in agreement about the same text.
    """
    return strip_js_noise(line)


def strip_js_noise(text: str, *, strings: bool = False) -> str:
    """Remove JS comments -- and optionally string/template literal *contents*.

    QC item 42/48: a literal scan must not treat documentation, a comment or a
    string as executable evidence. The scanner is stateful because block
    comments and template literals span lines, which a per-line regex cannot
    model. Removed characters become spaces so line/offset structure is kept.

    ``strings=False`` (the default) keeps string literals intact: the
    anti-cheating registry legitimately matches module specifiers, which *are*
    string literals. ``strings=True`` additionally blanks the contents of
    ``'``/``"``/backtick literals, which is what the skip/xfail scanner needs so
    that a test *title* mentioning ``skip`` is not mistaken for ``it.skip``.
    """
    out = list(text)
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if ch == "/" and nxt == "/":
            end = i
            while end < n and text[end] != "\n":
                end += 1
            out[i:end] = [c if c in "\r\n" else " " for c in text[i:end]]
            i = end
            continue
        if ch == "/" and nxt == "*":
            end = i + 2
            while end < n and not (text[end] == "*" and end + 1 < n and text[end + 1] == "/"):
                end += 1
            end = min(n, end + 2)
            out[i:end] = [c if c in "\r\n" else " " for c in text[i:end]]
            i = end
            continue
        # String literals are always *scanned* (so ``"/*"`` never opens a block
        # comment), but their contents are only blanked when ``strings=True``.
        if ch in ("'", '"', "`"):
            quote = ch
            end = i + 1
            while end < n and text[end] != quote:
                if text[end] == "\\":
                    end += 2
                    continue
                if text[end] == "\n" and quote != "`":
                    break
                end += 1
            end = min(n, end + 1)
            if strings:
                out[i:end] = [c if c in "\r\n" else " " for c in text[i:end]]
            i = end
            continue
        i += 1
    return "".join(out)


def classify_occurrence(
    line: str, source_modules: Sequence[str] = DEFAULT_SOURCE_MODULES
) -> Optional[str]:
    """Return ``"hidden"`` / ``"ordinary"`` for a source-framework reference.

    ``None`` means the line carries no source-framework reference at all. The
    distinction is what keeps the verifier's completeness signal apart from the
    gate's anti-cheating signal (items 113/114).
    """
    findings = module_load_findings(line, source_modules)
    return 'hidden' if any(item['kind'] == 'hidden' for item in findings) else ('ordinary' if findings else None)


# ---------------------------------------------------------------------------
# embedded-runtime / binary / archive tree scan (items 43/44/47)
# ---------------------------------------------------------------------------
def _magic_kind(header: bytes) -> Optional[str]:
    for signature, kind in MAGIC_SIGNATURES:
        if header.startswith(signature):
            return kind
    return None


def _archive_members(path: Path) -> "List[str]":
    try:
        with zipfile.ZipFile(path) as archive:
            return archive.namelist()[:50]
    except Exception:  # noqa: BLE001
        return []


def scan_embedded_runtime(
    root: Path,
    source_modules: Sequence[str] = DEFAULT_SOURCE_MODULES,
    *,
    scan_dirs: Sequence[str] = ("lib",),
    max_files: int = 5000,
) -> "List[Dict[str, Any]]":
    """Recursively look for compiled payloads, archives and vendored copies.

    Only the migration implementation tree (``lib/`` by default) is scanned:
    the frozen base tree legitimately ships the source framework as a
    *development* dependency under ``node_modules/``, so its mere presence there
    is not evidence -- an implementation that embeds or executes it is.
    """
    findings: "List[Dict[str, Any]]" = []
    seen = 0
    for rel_dir in scan_dirs:
        base = root / rel_dir
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            seen += 1
            if seen > max_files:
                findings.append({"path": rel_dir, "kind": "scan_truncated",
                                 "detail": f"more than {max_files} files"})
                break
            rel = path.relative_to(root).as_posix()
            suffix = path.suffix.lower()
            try:
                header = path.read_bytes()[:16]
            except OSError:
                continue
            kind = _magic_kind(header)
            if kind:
                findings.append({"path": rel, "kind": "binary_payload", "detail": kind})
                if suffix in ARCHIVE_SUFFIXES or header.startswith((b"PK\x03\x04", b"\x1f\x8b", b"BZh")):
                    findings.append({"path": rel, "kind": "embedded_archive",
                                     "detail": f"{kind}; members={_archive_members(path)}"})
                continue
            if suffix in BINARY_SUFFIXES or suffix in ARCHIVE_SUFFIXES:
                findings.append({"path": rel, "kind": "binary_payload", "detail": f"suffix {suffix}"})
    return findings


def runtime_dependency_findings(
    root: Path,
    source_modules: Sequence[str] = DEFAULT_SOURCE_MODULES,
) -> "List[Dict[str, Any]]":
    """Source framework reachable as a *runtime* dependency (item 44).

    ``dependencies`` (not ``devDependencies``) is the deployment surface: a
    migration that still lists the old framework there -- or that resolves it
    in ``package-lock.json`` as a production dependency -- has not actually
    removed the source runtime, no matter how clean ``lib/`` looks.

    A *packaging* trick (an npm alias such as ``"old-http": "npm:express@…”``,
    or a changed range) is a dependency-resolution failure, not a hidden source
    fallback: it is caught by ``target_build_valid.dependency_resolved`` and
    must stay a *non-cheating* gate failure. Only the deployment-surface
    dependency itself is reported here.
    """
    findings: "List[Dict[str, Any]]" = []
    wanted = {str(m) for m in source_modules}
    pkg_path = root / "package.json"
    if pkg_path.is_file():
        try:
            pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pkg = {}
        deps = pkg.get("dependencies") or {}
        for name in sorted(wanted & set(deps)):
            findings.append({
                "path": "package.json",
                "kind": "runtime_dependency",
                "detail": f"{name} is still a runtime dependency ({deps.get(name)!r})",
            })
    lock_path = root / "package-lock.json"
    if lock_path.is_file():
        try:
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            lock = {}
        packages = lock.get("packages") if isinstance(lock, dict) else None
        if isinstance(packages, dict):
            for key, entry in packages.items():
                if not isinstance(entry, dict):
                    continue
                if entry.get("dev") is True:
                    continue
                for name in wanted:
                    if key == f"node_modules/{name}":
                        findings.append({
                            "path": "package-lock.json",
                            "kind": "runtime_dependency",
                            "detail": f"resolved production package {name}",
                        })
    return findings
