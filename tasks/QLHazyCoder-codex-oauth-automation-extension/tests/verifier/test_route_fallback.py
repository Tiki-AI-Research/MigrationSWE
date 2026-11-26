#!/usr/bin/env python3
"""Deterministic regression for the declarative-route behavioural fallback.

Two places read a Flask ``app``'s URL map to decide which contract routes stay
served:

* ``BATCH-P2P-0002`` ``Probe.module_routes`` (observable ``P2P-036``);
* ``BATCH-F2P-0003._module_probe`` (``post_messages`` → ``F2P-057``/``F2P-070``).

A faithful migration may register the contract paths either explicitly or
through a single catch-all rule (``/<path:path>``) plus in-view dispatch.  Both
must be recognised, while a genuinely dropped route must still fail.  This
script locks all three directions with in-memory Flask apps so it never depends
on Harbor or the network.

Run directly (``python3 test_route_fallback.py``).
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

VERIFIER_DIR = Path(__file__).resolve().parent
ASSETS = VERIFIER_DIR.parent / "assets"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


p2p = _load(ASSETS / "shared" / "batches" / "BATCH-P2P-0002" / "p2p_probe.py", "route_p2p_probe")
f3 = _load(ASSETS / "shared" / "batches" / "BATCH-F2P-0003" / "f2p_probe_core.py", "route_f2p_core")

HOTMAIL = ["/messages", "/code", "/append-account-log", "/sync-account-run-records"]
CUSTOM = ["/messages", "/code", "/health", "/env", "/random-email"]
HOTMAIL_SORTED = sorted(HOTMAIL)
CUSTOM_SORTED = sorted(CUSTOM)

EXPLICIT_TMPL = '''\
from flask import Flask
app = Flask(__name__)


def _view(**_):
    return {"ok": True}


for _path in %s:
    app.add_url_rule(_path, view_func=_view, methods=["POST"])
'''

CATCHALL_TMPL = '''\
from flask import Flask, request
app = Flask(__name__)
_ROUTES = %s


@app.route("/<path:path>", methods=["POST"], defaults={"path": ""},
           provide_automatic_options=False)
def _view(path):
    if request.path in _ROUTES:
        return {"ok": True}
    return {"ok": False, "error": "Unsupported path: " + request.path}, 404
'''


def _write_helper(root: str, filename: str, source: str) -> Path:
    scripts = Path(root) / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / filename).write_text(source, encoding="utf-8")
    return scripts


def _p2p_routes(tmp: str, filename: str, source: str) -> list[str]:
    scripts = _write_helper(tmp, filename, source)
    probe = p2p.Probe(tmp, "target")
    module = p2p.load_module(str(scripts), filename[:-3])
    return probe.module_routes(module, filename)


def _f3_post_messages(tmp: str, filename: str, source: str) -> dict:
    _write_helper(tmp, filename, source)
    return f3._module_probe(tmp, filename[:-3])


def _run() -> list[str]:
    failures: list[str] = []

    # P2P-036: explicit registration still recognised.
    with tempfile.TemporaryDirectory() as tmp:
        got = _p2p_routes(tmp, "hotmail_helper.py", EXPLICIT_TMPL % (HOTMAIL,))
        if got != HOTMAIL_SORTED:
            failures.append("explicit hotmail routes not recognised: %r" % (got,))

    # P2P-036: catch-all registration now recognised (the false positive).
    with tempfile.TemporaryDirectory() as tmp:
        got = _p2p_routes(tmp, "hotmail_helper.py", CATCHALL_TMPL % (HOTMAIL,))
        if got != HOTMAIL_SORTED:
            failures.append("catch-all hotmail routes not recognised: %r" % (got,))
    with tempfile.TemporaryDirectory() as tmp:
        got = _p2p_routes(tmp, "custom_mail_helper.py", CATCHALL_TMPL % (CUSTOM,))
        if got != CUSTOM_SORTED:
            failures.append("catch-all custom routes not recognised: %r" % (got,))

    # P2P-036: a genuinely dropped route must still fail.
    with tempfile.TemporaryDirectory() as tmp:
        dropped = [r for r in HOTMAIL if r != "/messages"]
        got = _p2p_routes(tmp, "hotmail_helper.py", CATCHALL_TMPL % (dropped,))
        if "/messages" in got:
            failures.append("dropped hotmail /messages not detected: %r" % (got,))

    # F2P-057 / F2P-070: catch-all hotmail app serves POST /messages.
    with tempfile.TemporaryDirectory() as tmp:
        info = _f3_post_messages(tmp, "hotmail_helper.py", CATCHALL_TMPL % (HOTMAIL,))
        if not info.get("post_messages"):
            failures.append("catch-all post_messages not detected: %r" % (info,))
        if not info.get("post_messages_served"):
            failures.append("catch-all post_messages_served not set: %r" % (info,))

    # F2P-057 / F2P-070: dropping /messages must still fail.
    with tempfile.TemporaryDirectory() as tmp:
        dropped = [r for r in HOTMAIL if r != "/messages"]
        info = _f3_post_messages(tmp, "hotmail_helper.py", CATCHALL_TMPL % (dropped,))
        if info.get("post_messages"):
            failures.append("dropped post_messages not detected: %r" % (info,))

    return failures


def main() -> int:
    failures = _run()
    if failures:
        for item in failures:
            print("FAIL:", item)
        return 1
    print("route fallback regression: 6/6 PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
