"""Shared F2P probe core for BATCH-F2P-0003.

The migration under test replaces Python 3.10 ``http.server`` with Flask 3.1.2
inside ``scripts/hotmail_helper.py`` and ``scripts/custom_mail_helper.py``.

Every observation in this batch is an F2P target postcondition: the *same*
semantic predicate must hold only when the request handling path is owned by the
target technology (Flask), and must fail on the source technology
(``http.server``).  The probe therefore:

* copies the repository ``scripts`` into a scratch work tree so the read-only
  authoring mounts are never mutated;
* boots each helper's real entry point (``python <helper>.py``) as a subprocess
  and drives it over a real TCP socket;
* inspects the module import graph and the dependency manifests.

The observable target-technology signal is the HTTP ``Server`` response header:
Flask/Werkzeug emits ``Werkzeug/...`` while ``http.server`` emits
``BaseHTTP/...``.  The dependency predicate additionally requires an installed
Flask app object and ``Flask==3.1.2`` pinned in ``requirements.txt``.
"""

import ast
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tokenize
import urllib.error
import urllib.request

HOTMAIL_PORT = 17373
CUSTOM_PORT = 18374
TARGET_SERVER_MARKER = "Werkzeug"
SOURCE_SERVER_MARKER = "BaseHTTP"
FLASK_PIN = re.compile(r"flask\s*(?:\[[^\]]+\])?\s*==\s*3\.1\.2", re.IGNORECASE)
JSON_CONTENT_TYPE = "application/json; charset=utf-8"
CORS_ORIGIN = "*"
CORS_HEADERS = "Content-Type"
CORS_METHODS = "POST, OPTIONS"


def _copy_work_tree(repo_root, work_root):
    scripts_src = os.path.join(repo_root, "scripts")
    scripts_dst = os.path.join(work_root, "scripts")
    shutil.copytree(scripts_src, scripts_dst)
    os.makedirs(os.path.join(work_root, "data"), exist_ok=True)
    for name in ("requirements.txt", "package.json", ".env"):
        candidate = os.path.join(repo_root, name)
        if os.path.exists(candidate):
            shutil.copy2(candidate, os.path.join(work_root, name))
    return work_root


def _wait_for_port(port, proc, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def _http(method, port, path, data=None, headers=None, timeout=20.0):
    url = "http://127.0.0.1:%d%s" % (port, path)
    if isinstance(data, str):
        data = data.encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return {
                "status": response.status,
                "headers": {k: v for k, v in response.headers.items()},
                "body": response.read().decode("utf-8", errors="replace"),
            }
    except urllib.error.HTTPError as exc:
        return {
            "status": exc.code,
            "headers": {k: v for k, v in exc.headers.items()},
            "body": exc.read().decode("utf-8", errors="replace"),
        }
    except Exception as exc:  # connection reset and friends are real observations
        return {"status": 0, "headers": {}, "body": "", "error": str(exc)}


class HelperServer(object):
    def __init__(self, work_root, script_name, port, extra_env=None):
        self.work_root = work_root
        self.script = os.path.join(work_root, "scripts", script_name)
        self.port = port
        self.extra_env = extra_env or {}
        self.proc = None

    def __enter__(self):
        env = dict(os.environ)
        env.update(self.extra_env)
        env["PYTHONUNBUFFERED"] = "1"
        self.proc = subprocess.Popen(
            [sys.executable, self.script],
            cwd=self.work_root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        if not _wait_for_port(self.port, self.proc):
            output = b""
            if self.proc.poll() is not None:
                output = self.proc.stdout.read()
            self.__exit__(None, None, None)
            raise RuntimeError("helper did not accept connections: %r" % output[:400])
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()

    def call(self, method, path, payload=None, content_type="application/json"):
        headers = {}
        if content_type is not None:
            headers["Content-Type"] = content_type
        return _http(method, self.port, path, payload, headers)


def _module_probe(work_root, module_name):
    snippet = (
        "import json, importlib, inspect\n"
        "m = importlib.import_module(%r)\n"
        "out = {'callables': {}}\n"
        "for name in ('select_latest_code', 'refresh_access_token', 'fetch_graph_messages', 'get_json'):\n"
        "    out['callables'][name] = callable(getattr(m, name, None))\n"
        "try:\n"
        "    import flask\n"
        "    app = getattr(m, 'app', None)\n"
        "    if app is None and callable(getattr(m, 'create_app', None)):\n"
        "        app = m.create_app()\n"
        "    out['app_is_flask'] = isinstance(app, flask.Flask)\n"
        "    out['flask_version'] = getattr(flask, 'version', None) or flask.__version__\n"
        "    if out['app_is_flask']:\n"
        "        rules = [(r.rule, sorted(r.methods)) for r in app.url_map.iter_rules()]\n"
        "        out['rules'] = rules\n"
        "        declared = any(r == '/messages' and 'POST' in ms for r, ms in rules)\n"
        "        served = False\n"
        "        try:\n"
        "            client = app.test_client()\n"
        "            resp = client.post('/messages', data='{}', content_type='application/json')\n"
        "            served = resp.status_code not in (404, 405)\n"
        "        except Exception as inner:\n"
        "            out['post_messages_error'] = str(inner)\n"
        "        out['post_messages_declared'] = declared\n"
        "        out['post_messages_served'] = served\n"
        "        out['post_messages'] = declared or served\n"
        "    else:\n"
        "        out['rules'] = []\n"
        "        out['post_messages'] = False\n"
        "except Exception as exc:\n"
        "    out['app_is_flask'] = False\n"
        "    out['post_messages'] = False\n"
        "    out['flask_error'] = str(exc)\n"
        "print(json.dumps(out))\n"
    ) % (module_name,)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(work_root, "scripts") + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-c", snippet],
        cwd=work_root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    line = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "{}"
    try:
        return json.loads(line)
    except Exception:
        return {"error": "module probe failed", "stderr": proc.stderr[-400:]}


def _read_text(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except Exception:
        return ""


_PY_NODES = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def _note_spans(text):
    """Spans of docstrings and comments (the parts that are not code)."""

    spans = []
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        tree = None
    if tree is not None:
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
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type == tokenize.COMMENT:
                spans.append((token.start[0], token.start[1], token.end[0], token.end[1]))
    except (tokenize.TokenError, IndentationError, SyntaxError, UnicodeError, ValueError):
        pass
    return spans


def _blank_spans(text, spans):
    if not spans:
        return text
    lines = text.splitlines(keepends=True)
    offsets = []
    cursor = 0
    for line in lines:
        offsets.append(cursor)
        cursor += len(line)
    offsets.append(cursor)

    def char_index(row, column):
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


def _code_without_notes(text):
    """Source text with comments and docstrings blanked out.

    Used for the ``http.server`` presence checks so a legal migration that only
    *mentions* the retired technology in a comment or docstring is not mistaken
    for a runtime delegation back to the standard library
    (RC-F2P-LITERAL-FALSE-POSITIVE).
    """

    return _blank_spans(text, _note_spans(text))


def _served_by_target(response):
    server = str(response.get("headers", {}).get("Server", ""))
    return TARGET_SERVER_MARKER in server


def _cors_ok(response):
    headers = response.get("headers", {})
    return (
        headers.get("Access-Control-Allow-Origin") == CORS_ORIGIN
        and headers.get("Access-Control-Allow-Headers") == CORS_HEADERS
        and headers.get("Access-Control-Allow-Methods") == CORS_METHODS
    )


def _json_charset(response):
    return response.get("headers", {}).get("Content-Type") == JSON_CONTENT_TYPE


def _json_body(response):
    try:
        return json.loads(response.get("body") or "{}")
    except Exception:
        return {}


def _collect(repo_root, work_root):
    """Run every concrete probe once and return the raw observation bundle."""
    bundle = {"repo_root": repo_root, "work_root": work_root}
    bundle["requirements"] = _read_text(os.path.join(repo_root, "requirements.txt"))
    bundle["package_json"] = _read_text(os.path.join(repo_root, "package.json"))
    bundle["source_text"] = {
        "hotmail": _read_text(os.path.join(repo_root, "scripts", "hotmail_helper.py")),
        "custom": _read_text(os.path.join(repo_root, "scripts", "custom_mail_helper.py")),
    }
    bundle["modules"] = {
        "hotmail": _module_probe(work_root, "hotmail_helper"),
        "custom": _module_probe(work_root, "custom_mail_helper"),
    }

    hotmail = {}
    with HelperServer(work_root, "hotmail_helper.py", HOTMAIL_PORT) as server:
        hotmail["options_messages"] = server.call("OPTIONS", "/messages", content_type=None)
        hotmail["get_messages"] = server.call("GET", "/messages", content_type=None)
        hotmail["post_messages_empty"] = server.call("POST", "/messages", "{}")
        hotmail["post_unknown"] = server.call("POST", "/does-not-exist", "{}")
        hotmail["post_append"] = server.call(
            "POST",
            "/append-account-log",
            json.dumps(
                {
                    "email": "a@b.com",
                    "password": "p",
                    "status": "success",
                    "reason": "ok",
                    "recordedAt": "2026-01-01T00:00:00Z",
                }
            ),
        )
        hotmail["post_sync"] = server.call(
            "POST",
            "/sync-account-run-records",
            json.dumps(
                {
                    "records": [
                        {
                            "email": "a@b.com",
                            "password": "p",
                            "finalStatus": "success",
                            "retryCount": 2,
                        }
                    ]
                }
            ),
        )
        hotmail["post_code_empty"] = server.call("POST", "/code", "{}")
        concurrent = []

        def _fire():
            concurrent.append(server.call("POST", "/messages", "{}"))

        threads = [threading.Thread(target=_fire) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        hotmail["concurrent"] = concurrent
    bundle["hotmail"] = hotmail
    bundle["hotmail_log"] = _read_text(os.path.join(work_root, "data", "account-run-history.txt"))
    bundle["hotmail_snapshot"] = _read_text(os.path.join(work_root, "data", "account-run-history.json"))

    custom = {}
    with HelperServer(
        work_root,
        "custom_mail_helper.py",
        CUSTOM_PORT,
        extra_env={
            "FLOWPILOT_CUSTOM_MAIL_HELPER_PORT": str(CUSTOM_PORT),
            "FLOWPILOT_RANDOM_EMAIL_DB_PATH": os.path.join(work_root, "data", "custom.sqlite3"),
        },
    ) as server:
        custom["options_messages"] = server.call("OPTIONS", "/messages", content_type=None)
        custom["health"] = server.call("POST", "/health", "{}")
        custom["env"] = server.call("POST", "/env", "{}")
        custom["unknown"] = server.call("POST", "/does-not-exist", "{}")
        custom["get_messages"] = server.call("GET", "/messages", content_type=None)
        custom["messages_empty"] = server.call("POST", "/messages", "{}")
        custom["code_empty"] = server.call("POST", "/code", "{}")
        custom["random_plain"] = server.call(
            "POST", "/random-email", '{"domain":"example.com","n":1}', content_type="text/plain"
        )
        custom["random_json"] = server.call(
            "POST", "/random-email", '{"domain":"example.com","n":3}', content_type="application/json"
        )
        concurrent = []

        def _fire_custom():
            concurrent.append(server.call("POST", "/health", "{}"))

        threads = [threading.Thread(target=_fire_custom) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        custom["concurrent"] = concurrent
    bundle["custom"] = custom
    return bundle


def _evaluate(bundle):
    """Map each F2P observation id to (passed, detail) using the raw probe bundle."""
    results = {}

    def record(observation_id, passed, detail):
        results[observation_id] = {"passed": bool(passed), "detail": detail}

    hot = bundle["hotmail"]
    cus = bundle["custom"]
    modules = bundle["modules"]
    req = bundle.get("requirements", "")
    pkg = bundle.get("package_json", "")
    source_text = bundle.get("source_text", {})
    hot_text = source_text.get("hotmail", "")
    cus_text = source_text.get("custom", "")
    # The "no source runtime" postconditions must look at executable code, not
    # at comments/docstrings that merely name the retired technology.
    hot_code = _code_without_notes(hot_text)
    cus_code = _code_without_notes(cus_text)

    flask_pinned = any(
        FLASK_PIN.fullmatch(line.partition("#")[0].strip())
        for line in req.splitlines()
    )
    package_clean = "flask" not in pkg.lower()
    hot_module = modules.get("hotmail", {})
    cus_module = modules.get("custom", {})
    hot_callables = all(hot_module.get("callables", {}).values())
    cus_importable = bool(cus_module.get("app_is_flask")) and bool(
        cus_module.get("callables", {}).get("select_latest_code")
    )
    hot_has_app = bool(hot_module.get("app_is_flask")) and bool(hot_module.get("post_messages"))
    cus_has_app = bool(cus_module.get("app_is_flask"))
    no_http_server = ("http.server" not in hot_code) and ("http.server" not in cus_code)

    # F2P-070 / R042: requirements.txt pins Flask and import yields Flask app.
    record(
        "F2P-070",
        flask_pinned and hot_has_app,
        "flask_pinned=%s app_is_flask=%s post_messages=%s" % (flask_pinned, hot_module.get("app_is_flask"), hot_module.get("post_messages")),
    )
    # F2P-071 / R024: JS manifest untouched, Flask declared in a Python manifest.
    record(
        "F2P-071",
        flask_pinned and package_clean and os.path.exists(os.path.join(bundle["repo_root"], "scripts", "custom_mail_helper.py")),
        "flask_pinned=%s package_json_without_flask=%s" % (flask_pinned, package_clean),
    )
    # F2P-072 / R024: only the two scripts plus Flask dependency configuration changed.
    record(
        "F2P-072",
        flask_pinned and package_clean,
        "dependency manifest present=%s package_clean=%s" % (flask_pinned, package_clean),
    )
    # F2P-057 / R023: hotmail entry serves through Flask, no source-runtime delegation.
    record(
        "F2P-057",
        hot_has_app and no_http_server,
        "app_is_flask=%s no_http_server=%s" % (hot_module.get("app_is_flask"), no_http_server),
    )
    # F2P-058 / R036: snapshot summary written by the Flask route.
    snap = _json_body({"body": bundle.get("hotmail_snapshot", "")})
    summary = snap.get("summary", {}) if isinstance(snap, dict) else {}
    record(
        "F2P-058",
        hot["post_sync"]["status"] == 200
        and _served_by_target(hot["post_sync"])
        and summary == {"total": 1, "success": 1, "failed": 0, "retryTotal": 2},
        "status=%s summary=%s" % (hot["post_sync"]["status"], summary),
    )
    # F2P-059 / R034: custom /code path is Flask-owned and error responses carry CORS.
    record(
        "F2P-059",
        _served_by_target(cus["code_empty"]) and _cors_ok(cus["unknown"]) and _cors_ok(cus["messages_empty"]),
        "server=%s" % cus["code_empty"]["headers"].get("Server"),
    )
    # F2P-060 / R037: append-account-log line format written by the Flask route.
    fields = bundle.get("hotmail_log", "").rstrip("\n").split("\t")
    record(
        "F2P-060",
        hot["post_append"]["status"] == 200
        and _served_by_target(hot["post_append"])
        and fields == ["2026-01-01T00:00:00Z", "a@b.com", "p", "success", "ok"],
        "fields=%s" % fields,
    )
    # F2P-061 / R029: OPTIONS /messages -> 204 with CORS on a Flask server.
    record(
        "F2P-061",
        hot["options_messages"]["status"] == 204
        and _cors_ok(hot["options_messages"])
        and _served_by_target(hot["options_messages"]),
        "status=%s server=%s" % (hot["options_messages"]["status"], hot["options_messages"]["headers"].get("Server")),
    )
    # F2P-062 / R029: custom OPTIONS -> 204; module normalisation helper callable.
    record(
        "F2P-062",
        cus["options_messages"]["status"] == 204
        and _served_by_target(cus["options_messages"])
        and bool(cus_module.get("callables", {}).get("select_latest_code")),
        "status=%s callable=%s" % (cus["options_messages"]["status"], cus_module.get("callables", {}).get("select_latest_code")),
    )
    # F2P-063 / R023: custom /env is Flask-owned and no source-runtime delegation.
    record(
        "F2P-063",
        cus["env"]["status"] == 200 and _served_by_target(cus["env"]) and no_http_server,
        "status=%s server=%s" % (cus["env"]["status"], cus["env"]["headers"].get("Server")),
    )
    # F2P-064 / R029: concurrent custom POSTs intact and OPTIONS 204.
    bodies = [r["body"] for r in cus["concurrent"]]
    record(
        "F2P-064",
        cus["options_messages"]["status"] == 204
        and _served_by_target(cus["options_messages"])
        and all(body == '{"ok": true}' for body in bodies),
        "concurrent_bodies=%s" % bodies,
    )
    # F2P-065 / R022: custom OPTIONS path free of http.server.
    record(
        "F2P-065",
        cus["options_messages"]["status"] == 204
        and _served_by_target(cus["options_messages"])
        and ("http.server" not in cus_code),
        "status=%s no_http_server=%s" % (cus["options_messages"]["status"], "http.server" not in cus_code),
    )
    # F2P-066 / R030: importable module plus JSON 404 for unknown path.
    record(
        "F2P-066",
        cus_importable
        and cus["unknown"]["status"] == 404
        and _served_by_target(cus["unknown"])
        and _json_charset(cus["unknown"]),
        "importable=%s status=%s" % (cus_importable, cus["unknown"]["status"]),
    )
    # F2P-067 / R030: generated_emails persistence plus JSON 404.
    random_body = _json_body(cus["random_json"])
    record(
        "F2P-067",
        cus["random_json"]["status"] == 200
        and _served_by_target(cus["random_json"])
        and bool(random_body.get("emails"))
        and len(random_body.get("emails", [])) == 3
        and cus["unknown"]["status"] == 404
        and _served_by_target(cus["unknown"]),
        "emails=%s unknown_status=%s" % (random_body.get("emails"), cus["unknown"]["status"]),
    )
    # F2P-068 / R022: custom runtime entry free of http.server served by target stack.
    record(
        "F2P-068",
        cus["health"]["status"] == 200 and _served_by_target(cus["health"]) and ("http.server" not in cus_code),
        "status=%s server=%s" % (cus["health"]["status"], cus["health"]["headers"].get("Server")),
    )
    # F2P-069 / R034: custom 404 and 500 responses carry CORS.
    record(
        "F2P-069",
        _served_by_target(cus["unknown"]) and _cors_ok(cus["unknown"]) and _cors_ok(cus["messages_empty"]),
        "unknown_server=%s" % cus["unknown"]["headers"].get("Server"),
    )
    # F2P-049 / R034: hotmail 404 and 500 responses carry CORS.
    record(
        "F2P-049",
        _served_by_target(hot["post_unknown"]) and _cors_ok(hot["post_unknown"]) and _cors_ok(hot["post_messages_empty"]),
        "unknown_status=%s" % hot["post_unknown"]["status"],
    )
    # F2P-050 / R034: hotmail importable module API plus CORS errors.
    record(
        "F2P-050",
        hot_callables and _served_by_target(hot["post_unknown"]) and _cors_ok(hot["post_messages_empty"]),
        "callables=%s" % hot_callables,
    )
    # F2P-051 / R030: custom success envelope plus JSON 404.
    record(
        "F2P-051",
        cus["health"]["status"] == 200
        and _json_body(cus["health"]).get("ok") is True
        and cus["unknown"]["status"] == 404
        and _served_by_target(cus["unknown"]),
        "health=%s unknown=%s" % (cus["health"]["status"], cus["unknown"]["status"]),
    )
    # F2P-052 / R037: hotmail binds 127.0.0.1:17373 and appends the log line.
    record(
        "F2P-052",
        hot["post_append"]["status"] == 200 and len(fields) == 5 and _served_by_target(hot["post_append"]),
        "fields=%s server=%s" % (fields, hot["post_append"]["headers"].get("Server")),
    )
    # F2P-053 / R035: failure body carries the charset-qualified JSON content type.
    record(
        "F2P-053",
        _served_by_target(cus["messages_empty"]) and _json_charset(cus["messages_empty"]),
        "content_type=%s" % cus["messages_empty"]["headers"].get("Content-Type"),
    )
    # F2P-054 / R033: JSON body without a JSON content type still parses.
    record(
        "F2P-054",
        cus["random_plain"]["status"] == 200
        and _json_body(cus["random_plain"]).get("ok") is True
        and _served_by_target(cus["random_plain"]),
        "status=%s body=%s" % (cus["random_plain"]["status"], cus["random_plain"]["body"][:120]),
    )
    # F2P-055 / R023: hotmail snapshot route Flask-owned without source-runtime delegation.
    record(
        "F2P-055",
        hot["post_sync"]["status"] == 200 and _served_by_target(hot["post_sync"]) and no_http_server,
        "status=%s server=%s" % (hot["post_sync"]["status"], hot["post_sync"]["headers"].get("Server")),
    )
    # F2P-056 / R022: custom random-email route Flask-owned without http.server.
    record(
        "F2P-056",
        cus["random_json"]["status"] == 200 and _served_by_target(cus["random_json"]) and ("http.server" not in cus_code),
        "status=%s server=%s" % (cus["random_json"]["status"], cus["random_json"]["headers"].get("Server")),
    )
    return results


def run(repo_root, out_path):
    work_parent = tempfile.mkdtemp(prefix="f2p-0003-")
    work_root = os.path.join(work_parent, "work")
    _copy_work_tree(repo_root, work_root)
    bundle = _collect(repo_root, work_root)
    results = _evaluate(bundle)
    payload = {
        "batch_id": "BATCH-F2P-0003",
        "repo_root": repo_root,
        "observations": results,
        "summary": {
            "total": len(results),
            "passed": sum(1 for item in results.values() if item["passed"]),
            "failed": sum(1 for item in results.values() if not item["passed"]),
        },
    }
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return payload


def main(argv):
    repo_root = argv[1] if len(argv) > 1 else os.environ.get("PROBE_ROOT", "/input/target")
    out_path = argv[2] if len(argv) > 2 else os.path.join(os.getcwd(), "probe-results.json")
    payload = run(repo_root, out_path)
    print(json.dumps(payload["summary"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
