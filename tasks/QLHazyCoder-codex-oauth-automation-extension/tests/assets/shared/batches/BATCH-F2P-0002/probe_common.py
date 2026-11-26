"""Shared F2P probe harness for BATCH-F2P-0002.

The harness loads a helper module from a given repository root (source or target
mount), drives the documented HTTP entry point on its real serving path, and
evaluates the same semantic predicate on both sides.  A probe passes only when
the observed behaviour matches AND the request was served by the frozen target
technology (Flask) AND no source-technology HTTP handler (http.server) or
delegation primitive participated in the execution path.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler

TMP_DATA = os.path.join(tempfile.gettempdir(), "BATCH-F2P-0002", "data")
os.makedirs(TMP_DATA, exist_ok=True)

HTTP_USED = {"used": False}
DELEGATION_USED = {"used": False}

_HTTP_METHODS = ("do_POST", "do_OPTIONS", "do_GET", "do_PUT", "do_DELETE", "do_PATCH")


def _instrument_http_server():
    for name in _HTTP_METHODS:
        original = getattr(BaseHTTPRequestHandler, name, None)
        if original is None:
            continue

        def make(orig):
            def wrapper(self, *args, **kwargs):
                HTTP_USED["used"] = True
                return orig(self, *args, **kwargs)

            return wrapper

        setattr(BaseHTTPRequestHandler, name, make(original))


def _instrument_delegation():
    original_popen = subprocess.Popen
    original_system = os.system
    original_popen_fn = os.popen

    def popen_wrapper(*args, **kwargs):
        DELEGATION_USED["used"] = True
        return original_popen(*args, **kwargs)

    def system_wrapper(*args, **kwargs):
        DELEGATION_USED["used"] = True
        return original_system(*args, **kwargs)

    def popen_fn_wrapper(*args, **kwargs):
        DELEGATION_USED["used"] = True
        return original_popen_fn(*args, **kwargs)

    subprocess.Popen = popen_wrapper
    os.system = system_wrapper
    os.popen = popen_fn_wrapper


_instrument_http_server()
_instrument_delegation()


class Response:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = {k.lower(): v for k, v in headers.items()}
        self.body = body

    @property
    def text(self):
        return self.body.decode("utf-8", errors="replace")

    def json(self):
        try:
            return json.loads(self.text)
        except Exception:
            return None


class HelperUnderProbe:
    def __init__(self, root, helper):
        self.root = root
        self.helper = helper
        self.mod = self._load()
        self._redirect_writable_paths()
        self._flask_app_checked = False
        self._flask_app = None
        self.kind = "http.server"
        self.client = None
        self.server = None
        self.port = None

    def _load(self):
        file_stem = "custom_mail" if self.helper == "custom" else self.helper
        path = os.path.join(self.root, "scripts", f"{file_stem}_helper.py")
        name = f"probe_{os.path.basename(self.root)}_{file_stem}"
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod

    def _redirect_writable_paths(self):
        mod = self.mod
        if hasattr(mod, "ACCOUNT_LOG_PATH"):
            mod.ACCOUNT_LOG_PATH = os.path.join(TMP_DATA, "account-run-history.txt")
        if hasattr(mod, "ACCOUNT_RECORDS_SNAPSHOT_PATH"):
            mod.ACCOUNT_RECORDS_SNAPSHOT_PATH = os.path.join(TMP_DATA, "account-run-history.json")
        if hasattr(mod, "RANDOM_EMAIL_DB_PATH"):
            mod.RANDOM_EMAIL_DB_PATH = os.path.join(TMP_DATA, "custom-mail-helper.sqlite3")

    @property
    def flask_app(self):
        if self._flask_app_checked:
            return self._flask_app
        try:
            import flask
        except Exception:
            return None
        app = getattr(self.mod, "app", None)
        if app is None and callable(getattr(self.mod, "create_app", None)):
            app = self.mod.create_app()
        self._flask_app = app if isinstance(app, flask.Flask) else None
        self._flask_app_checked = True
        return self._flask_app

    def start(self):
        if self.flask_app is not None:
            self.kind = "flask"
            return
        handler = None
        for value in vars(self.mod).values():
            if isinstance(value, type) and issubclass(value, BaseHTTPRequestHandler) and value is not BaseHTTPRequestHandler:
                handler = value
                break
        if handler is None:
            raise RuntimeError("no HTTP handler found")
        server_cls = getattr(self.mod, "ThreadingHTTPServer", None)
        if server_cls is None:
            from http.server import ThreadingHTTPServer as server_cls
        self.server = server_cls(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.kind = "http.server"

    def stop(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()

    def request(self, method, path, body=None, raw=None, content_type="application/json", headers=None):
        HTTP_USED["used"] = False
        DELEGATION_USED["used"] = False
        merged = dict(headers or {})
        if content_type is not None:
            merged.setdefault("Content-Type", content_type)
        if self.kind == "flask":
            client = self.flask_app.test_client()
            data = raw if raw is not None else (json.dumps(body) if body is not None else None)
            resp = client.open(path, method=method, data=data, headers=merged)
            result = Response(resp.status_code, dict(resp.headers), resp.get_data())
        else:
            url = f"http://127.0.0.1:{self.port}{path}"
            data = raw if raw is not None else (json.dumps(body).encode("utf-8") if body is not None else None)
            req = urllib.request.Request(url, data=data, method=method)
            for key, value in merged.items():
                req.add_header(key, value)
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    result = Response(resp.status, dict(resp.headers.items()), resp.read())
            except urllib.error.HTTPError as exc:
                result = Response(exc.code, dict(exc.headers.items()), exc.read())
        result.http_server_dispatched = HTTP_USED["used"]
        result.delegation_dispatched = DELEGATION_USED["used"]
        result.flask_served = self.flask_app is not None
        return result

    def has_source_http_symbols(self):
        return hasattr(self.mod, "BaseHTTPRequestHandler") or hasattr(self.mod, "ThreadingHTTPServer")


def cors_ok(resp):
    return (
        resp.headers.get("access-control-allow-origin") == "*"
        and resp.headers.get("access-control-allow-headers") == "Content-Type"
        and resp.headers.get("access-control-allow-methods") == "POST, OPTIONS"
    )


def served_by_target(resp):
    return bool(resp.flask_served) and not resp.http_server_dispatched and not resp.delegation_dispatched


def read_log_lines(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read().splitlines()
