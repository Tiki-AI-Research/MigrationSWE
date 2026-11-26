#!/usr/bin/env python3
"""Deterministic regression for the semantic ``python_symbol`` residue kind.

The deceptive-fallback rules must not be satisfiable by simply *renaming* the
migrated-away stdlib technology.  Two escapes were found in adversarial review:

* dynamic loading -- ``__import__("ht" + "tp." + "server")`` plus
  ``getattr(module, "BaseHTTP" + "RequestHandler")`` leaves no contiguous
  ``http.server`` / ``BaseHTTPRequestHandler`` substring for the plain
  ``literal``/``import`` kinds to find;
* a brand-new file under the migration unit directory -- the old rules only
  enumerated two hard-coded helper files.

``python_symbol`` closes both by resolving the AST (constant-folded string
concatenation, ``bytes([...]).decode(...)``/``chr`` reconstruction, ``from
<module> import`` references and used identifier/attribute references) and by
scanning the whole candidate tree.  This script locks the behaviour in both
directions:

* a Werkzeug migration (the target technology) is *not* reported;
* literal, string-folded, ``bytes(...).decode(...)`` and new-file stdlib
  fallbacks *are* reported;
* hospital corners that are **legal** must stay clean -- a comment or a
  docstring that merely *names* the old technology, a bare unused ``import
  http.server`` (ordinary residue, not a hidden fallback) and ordinary target
  code that happens to use a generic mechanism module statically (for example
  ``subprocess.run(["uname", "-s"])``) must never be escalated to a cheat.

Run directly (``python3 test_residue_python_symbol.py``) or via ``pytest``.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from residue import scan_residue_rules

RULES = [
    {"id": "R-HTTP-SERVER", "kind": "python_symbol", "paths": ["scripts"], "target": "http.server"},
    {
        "id": "R-BASE-HANDLER",
        "kind": "python_symbol",
        "paths": ["scripts"],
        "target": "BaseHTTPRequestHandler",
    },
    {
        "id": "R-THREADING",
        "kind": "python_symbol",
        "paths": ["scripts"],
        "target": "ThreadingHTTPServer",
    },
    {"id": "R-SUBPROCESS", "kind": "python_symbol", "paths": ["scripts"], "target": "subprocess"},
]

# ``dynamic_only`` marks a generic mechanism module: only a *dynamically rebuilt*
# name (a folded string constant) counts, never a plain static import/usage.
DYN_RULES = [
    {
        "id": "R-SUBPROCESS-DYN",
        "kind": "python_symbol",
        "paths": ["scripts"],
        "target": "subprocess",
        "dynamic_only": True,
    },
]

ROOT_RULES = [
    {"id": "R-ROOT-HTTP", "kind": "python_symbol", "paths": ["."], "target": "http.server"},
    {
        "id": "R-ROOT-THREADING",
        "kind": "python_symbol",
        "paths": ["."],
        "target": "ThreadingHTTPServer",
    },
]

GOLDEN_HELPER = """\
from flask import Flask
from werkzeug.serving import WSGIRequestHandler, make_server


class HelperRequestHandler(WSGIRequestHandler):
    pass


def main():
    app = Flask(__name__)
    server = make_server("127.0.0.1", 17373, app, threaded=True)
    server.serve_forever()
"""

LITERAL_HELPER = """\
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class HelperHandler(BaseHTTPRequestHandler):
    pass


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 17373), HelperHandler)
    server.serve_forever()
"""

OBFUSCATED_HELPER = """\
def _legacy_fallback():
    module_name = "ht" + "tp." + "server"
    legacy = __import__(module_name, fromlist=["_"])
    handler_base = getattr(legacy, "BaseHTTP" + "RequestHandler")
    server_class = getattr(legacy, "Thread" + "ingHTTPServer")
    return server_class(("127.0.0.1", 17373), handler_base)
"""

NEWFILE_LEGACY = """\
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _LegacyHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        pass


def serve(host, port):
    server = ThreadingHTTPServer((host, port), _LegacyHandler)
    server.serve_forever()
"""

COMMENT_ONLY = """\
# Legacy note: this module used to serve with ThreadingHTTPServer.
from flask import Flask
"""

DOCSTRING_ONLY = '''\
"""Migration note: replaced BaseHTTPRequestHandler with Flask routing."""
from flask import Flask
'''

PLAIN_IMPORT_ONLY = """\
import http.server
from flask import Flask
"""

STATIC_SUBPROCESS = """\
import subprocess


def kernel_name():
    return subprocess.run(["uname", "-s"], capture_output=True, text=True)
"""

DYNAMIC_SUBPROCESS = """\
proc = __import__("sub" + "process")
"""

AUGASSIGN_SERVER = """\
module_name = "ht"
module_name += "tp.server"
"""

BYTES_DECODE_SERVER = """\
module_name = bytes([104, 116, 116, 112, 46, 115, 101, 114, 118, 101, 114]).decode("ascii")
handler_name = bytes([66, 97, 115, 101, 72, 84, 84, 80, 82, 101, 113, 117, 101, 115, 116, 72, 97, 110, 100, 108, 101, 114]).decode("ascii")
server_name = bytes([84, 104, 114, 101, 97, 100, 105, 110, 103, 72, 84, 84, 80, 83, 101, 114, 118, 101, 114]).decode("ascii")
"""

OUTSIDE_SCRIPTS_LEGACY = """\
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def serve(host, port):
    return ThreadingHTTPServer((host, port), BaseHTTPRequestHandler)
"""

# Runtime reconstruction through ``str.format`` / ``%`` (RC-DECEPTIVE-FALLBACK-FOLD-GAP).
FORMAT_HELPER = """\
def _legacy_fallback():
    module_name = "{}{}".format("http.", "server")
    server_name = "%s%s" % ("Threading", "HTTPServer")
    legacy = __import__(module_name, fromlist=["_"])
    return getattr(legacy, server_name)
"""

BASE64_HELPER = """\
def _legacy_fallback():
    module_name = __import__("base64").b64decode("aHR0cC5zZXJ2ZXI=").decode()
    legacy = __import__(module_name, fromlist=["_"])
    handler_base = getattr(legacy, __import__("base64").b64decode("QmFzZUhUVFBSZXF1ZXN0SGFuZGxlcg==").decode())
    server_class = getattr(legacy, __import__("base64").b64decode("VGhyZWFkaW5nSFRUUFNlcnZlcg==").decode())
    return server_class
"""

FROMHEX_HELPER = """\
def _legacy_fallback():
    module_name = bytes.fromhex("687474702e736572766572").decode()
    legacy = __import__(module_name, fromlist=["_"])
    server_class = getattr(legacy, bytes.fromhex("546872656164696e6748545450536572766572").decode())
    return server_class
"""

CHR_GEN_HELPER = """\
def _legacy_fallback():
    return "".join(chr(c) for c in [104, 116, 116, 112, 46, 115, 101, 114, 118, 101, 114])
"""

# A nested source directory called ``build`` must not be pruned: only a root
# ``build/`` output directory is skipped (RC-DECEPTIVE-FALLBACK-PATH).
BUILD_SUBDIR_LEGACY = """\
import http.server


def handler_class():
    return http.server.ThreadingHTTPServer
"""

# ``require_present`` (target technology in the same file) + ``exclude_paths``
# teach the scanner the difference between an unmigrated helper and a migrated
# one that hides a source fallback.
EXCLUDE_PATH_RULES = [
    {
        "id": "R-OUT",
        "kind": "python_symbol",
        "paths": ["."],
        "target": "ThreadingHTTPServer",
        "exclude_paths": ["scripts/hotmail_helper.py"],
    },
]

REQUIRE_PRESENT_RULES = [
    {
        "id": "R-IN",
        "kind": "python_symbol",
        "paths": ["scripts/hotmail_helper.py", "scripts/custom_mail_helper.py"],
        "target": "ThreadingHTTPServer",
        "require_present": ["flask", "werkzeug"],
    },
]

REQUIRE_PRESENT_HIT = """\
from flask import Flask
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def fallback():
    return ThreadingHTTPServer(("127.0.0.1", 1), BaseHTTPRequestHandler)
"""


def _scan(files: dict[str, str], rules: list[dict[str, object]] = RULES) -> list[dict[str, str]]:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        (root / "scripts").mkdir()
        for relative, text in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return scan_residue_rules(root, rules)


def _rule_ids(hits: list[dict[str, str]]) -> set[str]:
    return {hit["rule_id"] for hit in hits}


def main() -> int:
    failures: list[str] = []

    golden = _scan({"scripts/hotmail_helper.py": GOLDEN_HELPER})
    if golden:
        failures.append("werkzeug migration must not match python_symbol: %r" % (golden,))

    literal = _scan({"scripts/hotmail_helper.py": LITERAL_HELPER})
    if _rule_ids(literal) != {"R-HTTP-SERVER", "R-BASE-HANDLER", "R-THREADING"}:
        failures.append("literal stdlib fallback not fully detected: %r" % (literal,))

    obfuscated = _scan({"scripts/hotmail_helper.py": OBFUSCATED_HELPER})
    if _rule_ids(obfuscated) != {"R-HTTP-SERVER", "R-BASE-HANDLER", "R-THREADING"}:
        failures.append("string-folded fallback not resolved: %r" % (obfuscated,))

    newfile = _scan({"scripts/_flowpilot_legacy_server.py": NEWFILE_LEGACY})
    if not {"R-HTTP-SERVER", "R-BASE-HANDLER", "R-THREADING"}.issubset(_rule_ids(newfile)):
        failures.append("unscanned new file fallback slips through: %r" % (newfile,))

    subprocess_hit = _scan(
        {"scripts/hotmail_helper.py": 'proc = __import__("sub" + "process")\n'}
    )
    if _rule_ids(subprocess_hit) != {"R-SUBPROCESS"}:
        failures.append("folded subprocess import not detected: %r" % (subprocess_hit,))

    comment_only = _scan({"scripts/hotmail_helper.py": COMMENT_ONLY})
    if comment_only:
        failures.append("commented source token must not trigger python_symbol: %r" % (comment_only,))

    docstring_only = _scan({"scripts/custom_mail_helper.py": DOCSTRING_ONLY})
    if docstring_only:
        failures.append("docstring source token must not trigger python_symbol: %r" % (docstring_only,))

    plain_import = _scan({"scripts/hotmail_helper.py": PLAIN_IMPORT_ONLY})
    if _rule_ids(plain_import):
        failures.append(
            "bare unused `import http.server` must stay ordinary residue, not a cheat: %r"
            % (plain_import,)
        )

    static_subprocess = _scan({"scripts/_qa_util.py": STATIC_SUBPROCESS}, DYN_RULES)
    if static_subprocess:
        failures.append("static subprocess usage must not be a dynamic_only hit: %r" % (static_subprocess,))

    dynamic_subprocess = _scan({"scripts/hotmail_helper.py": DYNAMIC_SUBPROCESS}, DYN_RULES)
    if _rule_ids(dynamic_subprocess) != {"R-SUBPROCESS-DYN"}:
        failures.append("folded subprocess import must be a dynamic_only hit: %r" % (dynamic_subprocess,))

    bytes_decode = _scan({"scripts/hotmail_helper.py": BYTES_DECODE_SERVER})
    if _rule_ids(bytes_decode) != {"R-HTTP-SERVER", "R-BASE-HANDLER", "R-THREADING"}:
        failures.append("bytes([...]).decode() reconstruction not folded: %r" % (bytes_decode,))

    augassign = _scan({"scripts/hotmail_helper.py": AUGASSIGN_SERVER})
    if _rule_ids(augassign) != {"R-HTTP-SERVER"}:
        failures.append("+= string accumulation not folded: %r" % (augassign,))

    outside = _scan({"legacy_runtime/legacy_listener.py": OUTSIDE_SCRIPTS_LEGACY}, ROOT_RULES)
    if _rule_ids(outside) != {"R-ROOT-HTTP", "R-ROOT-THREADING"}:
        failures.append("out-of-scripts python fallback not scanned: %r" % (outside,))

    format_fold = _scan({"scripts/hotmail_helper.py": FORMAT_HELPER})
    if _rule_ids(format_fold) != {"R-HTTP-SERVER", "R-THREADING"}:
        failures.append("str.format / %% folding not resolved: %r" % (format_fold,))

    base64_fold = _scan({"scripts/hotmail_helper.py": BASE64_HELPER})
    if _rule_ids(base64_fold) != {"R-HTTP-SERVER", "R-BASE-HANDLER", "R-THREADING"}:
        failures.append("base64.b64decode folding not resolved: %r" % (base64_fold,))

    fromhex_fold = _scan({"scripts/hotmail_helper.py": FROMHEX_HELPER})
    if _rule_ids(fromhex_fold) != {"R-HTTP-SERVER", "R-THREADING"}:
        failures.append("bytes.fromhex folding not resolved: %r" % (fromhex_fold,))

    chr_gen_fold = _scan({"scripts/hotmail_helper.py": CHR_GEN_HELPER})
    if _rule_ids(chr_gen_fold) != {"R-HTTP-SERVER"}:
        failures.append("chr(...) generator join not folded: %r" % (chr_gen_fold,))

    build_subdir = _scan({"scripts/build/legacy_http.py": BUILD_SUBDIR_LEGACY}, ROOT_RULES)
    if _rule_ids(build_subdir) != {"R-ROOT-HTTP", "R-ROOT-THREADING"}:
        failures.append("nested scripts/build source must still be scanned: %r" % (build_subdir,))

    excluded = _scan(
        {"scripts/hotmail_helper.py": NEWFILE_LEGACY, "scripts/other.py": NEWFILE_LEGACY},
        EXCLUDE_PATH_RULES,
    )
    if _rule_ids(excluded) != {"R-OUT"} or {hit["path"] for hit in excluded} != {"scripts/other.py"}:
        failures.append("exclude_paths must skip only the named file: %r" % (excluded,))

    require_hit = _scan({"scripts/hotmail_helper.py": REQUIRE_PRESENT_HIT}, REQUIRE_PRESENT_RULES)
    if _rule_ids(require_hit) != {"R-IN"}:
        failures.append("require_present marker must keep the fallback hit: %r" % (require_hit,))

    require_miss = _scan({"scripts/hotmail_helper.py": LITERAL_HELPER}, REQUIRE_PRESENT_RULES)
    if require_miss:
        failures.append("require_present without the marker must suppress the hit: %r" % (require_miss,))

    if failures:
        for item in failures:
            print("FAIL:", item)
        return 1
    print("residue python_symbol regression: 21/21 PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
