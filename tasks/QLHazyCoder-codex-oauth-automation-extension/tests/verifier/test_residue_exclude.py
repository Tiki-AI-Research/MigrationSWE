#!/usr/bin/env python3
"""Deterministic regression for the residue ``exclude`` guard.

Two coarse literals also appear in *target*-technology software, so the rule
must not fire when the same file carries a target-technology marker:

* ``BATCH-F2P-0003-RES-005`` matches ``server.serve_forever()``, which is also
  how a Werkzeug ``make_server(...).serve_forever()`` server is driven;
* ``BATCH-F2P-0001-ORDINARY-003`` matches ``rfile``, which is also the stream
  attribute that Werkzeug ``WSGIRequestHandler`` inherits from
  ``http.server.BaseHTTPRequestHandler`` (``self.rfile``).

This script locks both directions for each rule:

* the stdlib ``http.server`` shape is still detected (the exclusion is absent);
* the equivalent Werkzeug shape is *not* reported.

Run directly (``python3 test_residue_exclude.py``) or via ``pytest``.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from residue import scan_residue_rules

RES_005 = {
    "id": "BATCH-F2P-0003-RES-005",
    "kind": "literal",
    "paths": ["scripts/hotmail_helper.py", "scripts/custom_mail_helper.py"],
    "target": "server.serve_forever()",
    "exclude": ["make_server", "werkzeug"],
}

ORDINARY_003 = {
    "id": "BATCH-F2P-0001-ORDINARY-003",
    "kind": "literal",
    "paths": ["scripts/hotmail_helper.py", "scripts/custom_mail_helper.py"],
    "target": "rfile",
    "exclude": ["make_server", "werkzeug"],
}

# ``exclude_paths`` prunes whole files by repo-relative pattern (used so an
# out-of-unit rule can scan the tree while ignoring the migration unit helpers).
EXCLUDE_PATH_RULE = {
    "id": "R-EXCLUDE-PATH-001",
    "kind": "python_symbol",
    "paths": ["."],
    "target": "ThreadingHTTPServer",
    "exclude_paths": ["scripts/hotmail_helper.py"],
}

STDLIB_LEGACY = """\
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def serve(host, port):
    return ThreadingHTTPServer((host, port), BaseHTTPRequestHandler)
"""

STDLIB_HELPER = """\
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 17373), BaseHTTPRequestHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
"""

WERKZEUG_HELPER = """\
from flask import Flask
from werkzeug.serving import make_server


def main():
    server = make_server("127.0.0.1", 17373, Flask(__name__), threaded=True)
    server.serve_forever()
"""

NO_SERVER_HELPER = """\
from flask import Flask

app = Flask(__name__)


def main():
    app.run(host="127.0.0.1", port=17373)
"""

STDLIB_RFILE_HELPER = """\
from http.server import BaseHTTPRequestHandler


class HelperHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(length)
"""

WERKZEUG_RFILE_HELPER = """\
from flask import Flask
from werkzeug.serving import WSGIRequestHandler, make_server


class HelperRequestHandler(WSGIRequestHandler):
    def _capture(self):
        return self.rfile


def main():
    make_server("127.0.0.1", 17373, Flask(__name__), request_handler=HelperRequestHandler)
"""


def _scan(text: str, rules: list[dict[str, object]]) -> list[dict[str, str]]:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        (root / "scripts").mkdir()
        (root / "scripts" / "hotmail_helper.py").write_text(text, encoding="utf-8")
        return scan_residue_rules(root, rules)


def main() -> int:
    failures: list[str] = []

    stdlib_hits = _scan(STDLIB_HELPER, [RES_005])
    if not stdlib_hits:
        failures.append("stdlib serve_forever must still be detected")
    if len(stdlib_hits) > 1:
        failures.append("stdlib helper produced duplicate hits")

    werkzeug_hits = _scan(WERKZEUG_HELPER, [RES_005])
    if werkzeug_hits:
        failures.append(
            "werkzeug make_server serve_forever must be excluded: %r" % (werkzeug_hits,)
        )

    plain_hits = _scan(NO_SERVER_HELPER, [RES_005])
    if plain_hits:
        failures.append("app.run helper must not match RES-005")

    stdlib_rfile_hits = _scan(STDLIB_RFILE_HELPER, [ORDINARY_003])
    if not stdlib_rfile_hits:
        failures.append("stdlib handler.rfile must still be detected")

    werkzeug_rfile_hits = _scan(WERKZEUG_RFILE_HELPER, [ORDINARY_003])
    if werkzeug_rfile_hits:
        failures.append(
            "werkzeug self.rfile must be excluded from ORDINARY-003: %r" % (werkzeug_rfile_hits,)
        )

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        (root / "scripts" / "legacy_runtime").mkdir(parents=True)
        (root / "scripts" / "hotmail_helper.py").write_text(STDLIB_LEGACY, encoding="utf-8")
        (root / "scripts" / "legacy_runtime" / "listener.py").write_text(
            STDLIB_LEGACY, encoding="utf-8"
        )
        exclude_path_hits = scan_residue_rules(root, [EXCLUDE_PATH_RULE])
    if {hit["path"] for hit in exclude_path_hits} != {"scripts/legacy_runtime/listener.py"}:
        failures.append(
            "exclude_paths must skip only the named helper: %r" % (exclude_path_hits,)
        )

    if failures:
        for item in failures:
            print("FAIL:", item)
        return 1
    print("residue exclude regression: 6/6 PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
