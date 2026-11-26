#!/usr/bin/env python3
"""BATCH-P2P-0002 executable state-preservation probe.

Runs the repository's own behavioral surface against a helper checkout (the
source ``http.server`` implementation or the migrated Flask implementation) and
emits, for every P2P observation owned by this batch, that side's raw
PASS/FAIL status together with a stable common projection.  The projections for
the source and target sides are byte-identical after JSON key sorting and path
stripping.

The probe can be invoked directly (``p2p_probe.py [report]``), through a
per-side runner (``runner source|target [report]``) or through the
``run_source.sh`` / ``run_target.sh`` wrappers.  When the side is omitted it is
inferred from the probe's own location, and the repository root defaults to the
matching ``/input`` mount with a fallback to ``/workspace/repo``.
"""

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request

BATCH_ID = "BATCH-P2P-0002"
OBSERVATIONS = ["P2P-%03d" % index for index in range(25, 41)]

JS_TEST_FILE = "tests/background-openai-publisher-chatgpt2api.test.js"
JS_TEST_NAME = "OpenAI ChatGPT2API publisher posts tokens with bearer admin key"

CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
}

HOTMAIL_ROUTES = ["/messages", "/code", "/append-account-log", "/sync-account-run-records"]
CUSTOM_ROUTES = ["/messages", "/code", "/health", "/env", "/random-email"]
REQUIRED_ROUTES_BY_FILE = {
    "hotmail_helper.py": HOTMAIL_ROUTES,
    "custom_mail_helper.py": CUSTOM_ROUTES,
}
# Dispatch bodies used only by the behavioural fallback of ``module_routes``;
# they mirror the P2P-009/P2P-017 route probes so a served path never returns
# the unknown-path envelope (404/405).
ROUTE_PROBE_BODIES = {
    "hotmail_helper.py": {
        "/messages": {},
        "/code": {},
        "/append-account-log": {
            "email": "a@b.com",
            "password": "p",
            "status": "success",
            "recordedAt": "2026-01-01T00:00:00Z",
            "reason": "r",
        },
        "/sync-account-run-records": {
            "records": [
                {"email": "a@b.com", "password": "p", "finalStatus": "success", "retryCount": 2}
            ]
        },
    },
    "custom_mail_helper.py": {
        "/messages": {},
        "/code": {},
        "/health": {},
        "/env": {},
        "/random-email": {"domain": "example.com", "n": 2},
    },
}


def load_module(scripts_dir, name):
    path = os.path.join(scripts_dir, name + ".py")
    spec = importlib.util.spec_from_file_location(name + "_probe", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def resolve_flask_app(module):
    app = getattr(module, "app", None)
    if app is None and callable(getattr(module, "create_app", None)):
        app = module.create_app()
    return app


class Server:
    def __init__(self, module, kind):
        self.module = module
        self.kind = kind
        app = resolve_flask_app(module)
        if app is not None:
            from werkzeug.serving import make_server
            self.server = make_server("127.0.0.1", 0, app, threaded=True)
            self.port = self.server.server_port
            self.target = self.server.serve_forever
        else:
            from http.server import ThreadingHTTPServer
            handler = module.HotmailHelperHandler if kind == "hotmail" else module.CustomMailHelperHandler
            self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            self.port = self.server.server_address[1]
            self.target = self.server.serve_forever
        self.thread = threading.Thread(target=self.target, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        try:
            self.server.shutdown()
        except Exception:
            pass
        try:
            self.server.server_close()
        except Exception:
            pass


def http(port, method, path, body=None, headers=None, raw_body=None):
    url = "http://127.0.0.1:%d%s" % (port, path)
    data = raw_body
    if data is None and body is not None:
        data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json; charset=utf-8")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read()


def body_json(raw):
    if not raw:
        return None
    return json.loads(raw.decode("utf-8"))


def cors_ok(headers):
    return {key: headers.get(key) for key in CORS}


def app_routes(module):
    """Return the POST route rules registered on a Flask ``app``.

    The state-preservation contract is about *which paths are still served*,
    not how the candidate spelled the registration.  Reading the framework's
    own URL map keeps ``@app.route(...)``, ``@app.post(...)``, blueprints and
    ``add_url_rule(...)`` observationally equivalent, and it cannot be spoofed
    by leaving unused ``http.server`` ``self.path`` branches behind.  Returns
    ``None`` when the module is not a Flask application so the caller can fall
    back to the source-side textual scan.
    """

    app = resolve_flask_app(module)
    url_map = getattr(app, "url_map", None)
    if url_map is None:
        return None
    routes = set()
    for rule in url_map.iter_rules():
        methods = getattr(rule, "methods", None) or ()
        if "POST" in methods:
            routes.add(rule.rule)
    return sorted(routes)


def extract_routes(text):
    routes = set()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("@app.route("):
            start = stripped.find('"')
            end = stripped.find('"', start + 1)
            if start != -1 and end != -1:
                routes.add(stripped[start + 1:end])
        if 'self.path == "' in stripped:
            start = stripped.find('self.path == "') + len('self.path == "')
            end = stripped.find('"', start)
            routes.add(stripped[start:end])
    return sorted(routes)


def sha256_file(path):
    if not os.path.exists(path):
        return None
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        digest.update(handle.read())
    return digest.hexdigest()


def normalize_error(value):
    text = str(value or "")
    return text.split(":")[0]


def find_node():
    """Locate a Node.js interpreter without assuming it is on PATH."""
    candidates = []
    for env_key in ("NODE_BINARY", "NODE"):
        if os.environ.get(env_key):
            candidates.append(os.environ[env_key])
    for name in ("node", "nodejs"):
        located = shutil.which(name)
        if located:
            candidates.append(located)
    candidates.extend(
        ["/usr/local/bin/node", "/usr/bin/node", "/opt/node/bin/node", "/opt/nodejs/bin/node"]
    )
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    return None


def tap_counts(output):
    passed = failed = None
    for line in output.splitlines():
        stripped = line.strip()
        match = re.match(r"# pass (\d+)", stripped)
        if match:
            passed = int(match.group(1))
        match = re.match(r"# fail (\d+)", stripped)
        if match:
            failed = int(match.group(1))
    return passed, failed


def tap_named_result(output, name):
    for line in output.splitlines():
        stripped = line.strip()
        if name not in stripped:
            continue
        if stripped.startswith("not ok "):
            return False
        if stripped.startswith("ok "):
            return True
    return None


def junit_named_result(path, name):
    import xml.etree.ElementTree as element_tree

    try:
        root = element_tree.parse(path).getroot()
    except Exception:
        return None
    for case in root.iter("testcase"):
        case_name = case.get("name") or ""
        if name in case_name:
            return not any(child.tag in ("failure", "error") for child in case)
    return None


def run_js_publisher_test(repo):
    """Run the repository's own JS test case for P2P-028 and report its raw result.

    The projection is produced by really executing the repository's own
    JavaScript test file in this side's container, independently of the Python
    helper modules, so a Python import failure can never suppress or reshape the
    P2P-028 result.  Several execution strategies are attempted before falling
    back to a JUnit report, and the emitted projection is normalized to the single
    named test case so both sides always produce the same concrete value.
    """
    projection = {
        "test": JS_TEST_FILE,
        "test_name": JS_TEST_NAME,
        "pass": None,
        "fail": None,
        "present": False,
        "result": "FAIL",
        "executed": False,
    }
    node = find_node()
    if not node:
        sys.stderr.write("P2P-028 node interpreter not found\n")
        return projection

    passed_named = None
    strategies = [
        [node, "--test", "--test-name-pattern", JS_TEST_NAME, JS_TEST_FILE],
        [node, "--test", JS_TEST_FILE],
    ]
    for command in strategies:
        try:
            result = subprocess.run(
                command, cwd=repo, capture_output=True, text=True, timeout=600,
            )
        except Exception as error:  # pragma: no cover - defensive
            sys.stderr.write("P2P-028 node execution failed: %r\n" % (error,))
            continue
        projection["executed"] = True
        output = (result.stdout or "") + (result.stderr or "")
        passed, failed = tap_counts(output)
        if passed is not None and failed is not None and (passed + failed) >= 1:
            passed_named = failed == 0 and passed >= 1
            break
        named = tap_named_result(output, JS_TEST_NAME)
        if named is not None:
            passed_named = named
            break

    if passed_named is None:
        report_path = os.path.join(tempfile.mkdtemp(prefix="p2p-js-"), "junit.xml")
        try:
            subprocess.run(
                [node, "--test", "--test-reporter=junit",
                 "--test-reporter-destination", report_path, JS_TEST_FILE],
                cwd=repo, capture_output=True, text=True, timeout=600,
            )
            named = junit_named_result(report_path, JS_TEST_NAME)
            if named is not None:
                projection["executed"] = True
                passed_named = named
        except Exception as error:  # pragma: no cover - defensive
            sys.stderr.write("P2P-028 junit execution failed: %r\n" % (error,))

    if passed_named is not None:
        projection["present"] = True
        projection["pass"] = 1 if passed_named else 0
        projection["fail"] = 0 if passed_named else 1
        projection["result"] = "PASS" if passed_named else "FAIL"
    return projection


class Probe:
    def __init__(self, repo, side):
        self.repo = os.path.abspath(repo)
        self.side = side
        self.scripts = os.path.join(self.repo, "scripts")
        self.tmp = tempfile.mkdtemp(prefix="p2p-probe-")
        self.observations = {}

    def record(self, obs, value):
        self.observations[obs] = value

    def prepare(self):
        os.environ["FLOWPILOT_CUSTOM_MAIL_HELPER_PORT"] = "17374"
        os.environ["FLOWPILOT_CUSTOM_IMAP_TIMEOUT"] = "1"
        self.hotmail = load_module(self.scripts, "hotmail_helper")
        self.custom = load_module(self.scripts, "custom_mail_helper")
        self.hotmail_path = os.path.join(self.tmp, "account-run-history.txt")
        self.snapshot_path = os.path.join(self.tmp, "account-run-history.json")
        self.hotmail.ACCOUNT_LOG_PATH = self.hotmail_path
        self.hotmail.ACCOUNT_RECORDS_SNAPSHOT_PATH = self.snapshot_path

    def write_payload(self):
        return [
            {
                "email": "a@b.com",
                "password": "p",
                "finalStatus": "success",
                "retryCount": 2,
                "failedStep": 3,
                "autoRunContext": {"currentRun": 1, "totalRuns": 2, "attemptRun": 1},
            }
        ]

    def run(self):
        stages = [
            self.probe_random_email,
            self.probe_append_log,
            self.probe_assets,
            self.probe_envelopes,
            self.probe_concurrency,
            self.probe_normalization,
            self.probe_options,
            self.probe_routes,
            self.probe_missing_credentials,
            self.probe_failures,
            self.probe_cors,
            self.probe_env,
            self.probe_disk_formats,
        ]
        self.prepare()
        for stage in stages:
            try:
                stage()
            except Exception as error:  # pragma: no cover - defensive
                sys.stderr.write("probe stage %s failed: %r\n" % (stage.__name__, error))
        return self.observations

    def probe_random_email(self):
        db = os.path.join(self.tmp, "random-email.sqlite3")
        self.custom.RANDOM_EMAIL_DB_PATH = db
        with Server(self.custom, "custom") as server:
            status, headers, raw = http(server.port, "POST", "/random-email", {"domain": "example.com", "n": 3})
        payload = body_json(raw)
        keys = sorted(payload.keys()) if isinstance(payload, dict) else []
        connection = sqlite3.connect(db)
        rows = connection.execute("SELECT email FROM generated_emails").fetchall()
        connection.close()
        emails = [row[0] for row in rows]
        self.record("P2P-026", {
            "status": status,
            "unique_rows": len(set(emails)),
            "rows": len(emails),
            "all_example": all(item.endswith("@example.com") for item in emails),
        })
        self.record("P2P-031", {
            "status": status,
            "ok": payload.get("ok") if isinstance(payload, dict) else None,
            "keys": keys,
            "emails_len": len(payload.get("emails", [])) if isinstance(payload, dict) else None,
            "count": payload.get("count") if isinstance(payload, dict) else None,
        })

        db2 = os.path.join(self.tmp, "rollback.sqlite3")
        self.custom.RANDOM_EMAIL_DB_PATH = db2
        original_prefix = getattr(self.custom, "random_email_prefix", None)
        try:
            self.custom.random_email_prefix = lambda: "collision"
            with Server(self.custom, "custom") as server:
                status, headers, raw = http(server.port, "POST", "/random-email", {"domain": "example.com", "n": 3})
        finally:
            if original_prefix is not None:
                self.custom.random_email_prefix = original_prefix
        payload = body_json(raw)
        connection = sqlite3.connect(db2)
        rows = connection.execute("SELECT COUNT(*) FROM generated_emails").fetchone()[0]
        connection.close()
        self.record("P2P-025", {
            "status": status,
            "ok": payload.get("ok") if isinstance(payload, dict) else None,
            "error": normalize_error(payload.get("error")) if isinstance(payload, dict) else None,
            "rows_after_rollback": rows,
        })

    def probe_append_log(self):
        open(self.hotmail_path, "w").close()
        with Server(self.hotmail, "hotmail") as server:
            status, headers, raw = http(server.port, "POST", "/append-account-log", {
                "recordedAt": "2024-01-01T00:00:00Z",
                "email": "a@b.com",
                "password": "p",
                "status": "success",
                "reason": "done",
            })
        payload = body_json(raw)
        with open(self.hotmail_path, "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        fields = lines[0].split("\t") if lines else []
        self.record("P2P-027", {
            "status": status,
            "ok": payload.get("ok") if isinstance(payload, dict) else None,
            "file_path_basename": os.path.basename(payload.get("filePath", "")) if isinstance(payload, dict) else None,
            "lines": len(lines),
            "fields": fields,
        })

    def probe_assets(self):
        manifest = sha256_file(os.path.join(self.repo, "manifest.json"))
        rules = sha256_file(os.path.join(self.repo, "rules.json"))
        background = sha256_file(os.path.join(self.repo, "background.js"))
        sidepanel_dir = os.path.join(self.repo, "sidepanel")
        names = sorted(os.listdir(sidepanel_dir)) if os.path.isdir(sidepanel_dir) else []
        digest = hashlib.sha256()
        for name in names:
            digest.update(name.encode("utf-8"))
            digest.update(str(sha256_file(os.path.join(sidepanel_dir, name))).encode("utf-8"))
        self.record("P2P-029", {
            "manifest": manifest,
            "rules": rules,
            "background": background,
            "sidepanel": digest.hexdigest(),
        })

    def probe_envelopes(self):
        def fake_collect(*args, **kwargs):
            return {
                "messages": [{
                    "id": "1",
                    "receivedTimestamp": 100,
                    "subject": "Your code is 123456",
                    "bodyPreview": "",
                    "from": {"emailAddress": {"address": "no-reply@codeium.com"}},
                }],
                "mailboxResults": [{"mailbox": "INBOX", "count": 1}],
                "token_payload": {"next_refresh_token": "rt2", "token_endpoint": "live"},
                "transport": "graph",
            }

        def fake_select(*args, **kwargs):
            return {"code": "123456", "message": None, "usedTimeFallback": False}

        self.hotmail.collect_messages = fake_collect
        self.hotmail.select_latest_code = fake_select
        self.custom.fetch_recent_messages = lambda *a, **k: []
        self.custom.select_latest_code = lambda *a, **k: {"code": "123456", "message": None, "usedTimeFallback": False}
        hotmail_keys = {}
        custom_keys = {}
        with Server(self.hotmail, "hotmail") as server:
            _, _, raw = http(server.port, "POST", "/messages", {"email": "a@b.com", "clientId": "c", "refreshToken": "r"})
            hotmail_keys["messages"] = sorted(body_json(raw).keys())
            _, _, raw = http(server.port, "POST", "/code", {"email": "a@b.com", "clientId": "c", "refreshToken": "r"})
            hotmail_keys["code"] = sorted(body_json(raw).keys())
        with Server(self.custom, "custom") as server:
            _, _, raw = http(server.port, "POST", "/messages", {"top": 1})
            custom_keys["messages"] = sorted(body_json(raw).keys())
            _, _, raw = http(server.port, "POST", "/code", {"top": 1})
            custom_keys["code"] = sorted(body_json(raw).keys())
        self.record("P2P-030", {"hotmail": hotmail_keys, "custom": custom_keys})

    def probe_concurrency(self):
        open(self.hotmail_path, "w").close()
        count = 8
        results = [None] * count

        def worker(index, server):
            results[index] = http(server.port, "POST", "/append-account-log", {
                "recordedAt": "2024-01-01T00:00:0%d Z" % index,
                "email": "user%d@example.com" % index,
                "password": "p%d" % index,
                "status": "success",
                "reason": "r%d" % index,
            })

        with Server(self.hotmail, "hotmail") as server:
            threads = [threading.Thread(target=worker, args=(index, server)) for index in range(count)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
        statuses = {}
        for item in results:
            statuses[item[0]] = statuses.get(item[0], 0) + 1
        with open(self.hotmail_path, "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        complete = all(len(line.split("\t")) == 5 for line in lines)
        self.record("P2P-033", {
            "requests": count,
            "statuses": statuses,
            "lines": len(lines),
            "distinct_lines": len(set(lines)),
            "all_complete": complete,
        })

    def probe_normalization(self):
        status_vocab = {}
        for value in ["success", "failed", "stopped", "running", "SUCCESS"]:
            normalized = self.hotmail.normalize_account_run_snapshot_record({
                "email": "a@b.com", "password": "p", "finalStatus": value,
            })
            status_vocab[value] = normalized.get("finalStatus") if normalized else None
        retained = self.hotmail.normalize_account_run_snapshot_record({
            "email": "a@b.com", "password": "p", "finalStatus": "failed",
            "retryCount": 4, "failedStep": 7,
            "autoRunContext": {"currentRun": 1, "totalRuns": 2, "attemptRun": 1},
        })
        retained_projection = {
            "retryCount": retained.get("retryCount"),
            "failedStep": retained.get("failedStep"),
            "autoRunContext": retained.get("autoRunContext"),
        }
        hotmail_messages = [
            {"receivedTimestamp": 100, "subject": "code is 111111", "bodyPreview": "",
             "from": {"emailAddress": {"address": "no-reply@codeium.com"}}},
            {"receivedTimestamp": 200, "subject": "code 222222", "bodyPreview": "",
             "from": {"emailAddress": {"address": "no-reply@codeium.com"}}},
        ]
        hotmail_code = self.hotmail.select_latest_code(hotmail_messages, [], [], [], 0, [], [])
        custom_messages = [
            {"receivedTimestamp": 100, "subject": "\u9a8c\u8bc1\u7801 333333", "bodyPreview": "",
             "body": {"content": ""},
             "from": {"emailAddress": {"address": "no-reply@codeium.com"}}},
        ]
        custom_code = self.custom.select_latest_code(custom_messages, {})
        self.record("P2P-034", {
            "status_vocab": status_vocab,
            "retained": retained_projection,
            "hotmail_code": hotmail_code.get("code"),
            "custom_code": custom_code.get("code"),
            "token_endpoints": list(self.hotmail.TOKEN_ENDPOINTS.keys()),
        })

    def probe_options(self):
        with Server(self.hotmail, "hotmail") as server:
            status, headers, raw = http(server.port, "OPTIONS", "/messages")
            hotmail = {"status": status, "body_len": len(raw), "cors": cors_ok(headers)}
        with Server(self.custom, "custom") as server:
            status, headers, raw = http(server.port, "OPTIONS", "/health")
            custom = {"status": status, "body_len": len(raw), "cors": cors_ok(headers)}
        self.record("P2P-035", {"hotmail": hotmail, "custom": custom})

    def probe_routes(self):
        hotmail_routes = self.module_routes(self.hotmail, "hotmail_helper.py")
        custom_routes = self.module_routes(self.custom, "custom_mail_helper.py")
        self.record("P2P-036", {"hotmail": hotmail_routes, "custom": custom_routes})

    def module_routes(self, module, filename):
        # The state-preservation contract is *which declared paths stay served*.
        # A faithful migration may spell the registration differently (``@app.post``,
        # ``add_url_rule``, blueprints) and may add behaviourally-neutral rules such
        # as a catch-all fallback that only re-emits the unknown-path 404 envelope.
        # Project the observed rules onto the contract's declared routes so extra
        # benign rules are not a regression, while a dropped required route still is.
        required = REQUIRED_ROUTES_BY_FILE.get(filename, [])
        introspected = app_routes(module)
        if introspected is not None:
            routes = introspected
        else:
            with open(os.path.join(self.scripts, filename), "r", encoding="utf-8") as handle:
                routes = extract_routes(handle.read())
        declared = sorted(route for route in routes if route in required)
        if set(declared) >= set(required):
            return declared
        # A faithful migration may also register the contract paths through a
        # *single* catch-all rule (``/<path:path>``) plus in-view dispatch, so the
        # URL map only enumerates the fallback pattern.  Before reporting a missing
        # route, confirm the contract paths are actually served, using exactly the
        # dispatch semantics of P2P-009/P2P-017 (a served path never answers 404/405).
        served = self.served_routes(module, filename)
        if served is None:
            return declared
        return sorted(route for route in required if route in served)

    def served_routes(self, module, filename):
        required = REQUIRED_ROUTES_BY_FILE.get(filename, [])
        bodies = ROUTE_PROBE_BODIES.get(filename, {})
        kind = "hotmail" if filename.startswith("hotmail") else "custom"
        try:
            with Server(module, kind) as server:
                served = set()
                for route in required:
                    status, _, _ = http(server.port, "POST", route, bodies.get(route, {}))
                    if status not in (404, 405):
                        served.add(route)
            return served
        except Exception:
            return None

    def probe_missing_credentials(self):
        with Server(self.hotmail, "hotmail") as server:
            status, headers, raw = http(server.port, "POST", "/messages", {})
        payload = body_json(raw)
        self.record("P2P-037", {
            "status": status,
            "ok": payload.get("ok") if isinstance(payload, dict) else None,
            "error": payload.get("error") if isinstance(payload, dict) else None,
        })

    def probe_failures(self):
        self.custom.RANDOM_EMAIL_DB_PATH = os.path.join(self.tmp, "failure.sqlite3")
        with Server(self.hotmail, "hotmail") as server:
            status, headers, raw = http(server.port, "POST", "/sync-account-run-records", raw_body=b"not-json")
            hotmail_payload = body_json(raw)
            hotmail = {
                "status": status,
                "keys": sorted(hotmail_payload.keys()),
                "ok": hotmail_payload.get("ok"),
                "error_class": normalize_error(hotmail_payload.get("error")),
            }
        with Server(self.custom, "custom") as server:
            status, headers, raw = http(server.port, "POST", "/random-email", {"domain": "example.com", "n": "abc"})
            custom_payload = body_json(raw)
            custom = {
                "status": status,
                "keys": sorted(custom_payload.keys()),
                "ok": custom_payload.get("ok"),
                "error": custom_payload.get("error"),
            }
        self.record("P2P-032", {"hotmail": hotmail, "custom": custom})

    def probe_cors(self):
        observations = {}
        with Server(self.hotmail, "hotmail") as server:
            _, headers, _ = http(server.port, "POST", "/messages", {})
            observations["500"] = cors_ok(headers)
            _, headers, _ = http(server.port, "POST", "/does-not-exist", {})
            observations["404"] = cors_ok(headers)
            _, headers, _ = http(server.port, "OPTIONS", "/messages")
            observations["204"] = cors_ok(headers)
        with Server(self.custom, "custom") as server:
            _, headers, _ = http(server.port, "POST", "/health", {})
            observations["200"] = cors_ok(headers)
            _, headers, raw = http(server.port, "POST", "/health", {})
            observations["content_type"] = headers.get("Content-Type")
        self.record("P2P-038", observations)

    def probe_env(self):
        os.environ["FLOWPILOT_SUB2API_REDIRECT_URI"] = "https://example.test/cb"
        env_file = os.path.join(self.tmp, ".env")
        with open(env_file, "w", encoding="utf-8") as handle:
            handle.write("P2P_PROBE_ENV_KEY=probe-value\n")
        os.environ.pop("P2P_PROBE_ENV_KEY", None)
        self.custom.load_dotenv_file(env_file)
        loaded = os.environ.get("P2P_PROBE_ENV_KEY")
        project_root = os.path.abspath(os.path.join(os.path.dirname(self.custom.__file__), ".."))
        with Server(self.custom, "custom") as server:
            status, headers, raw = http(server.port, "POST", "/env", {})
        payload = body_json(raw)
        self.record("P2P-039", {
            "status": status,
            "dotenv_loaded": loaded,
            "project_root_is_parent_of_scripts": project_root == self.repo,
            "env": payload.get("env") if isinstance(payload, dict) else None,
        })

    def probe_disk_formats(self):
        db = os.path.join(self.tmp, "format.sqlite3")
        self.custom.RANDOM_EMAIL_DB_PATH = db
        with Server(self.custom, "custom") as server:
            http(server.port, "POST", "/random-email", {"domain": "example.com", "n": 1})
        connection = sqlite3.connect(db)
        columns = [row[1] for row in connection.execute("PRAGMA table_info(generated_emails)").fetchall()]
        connection.close()
        with Server(self.hotmail, "hotmail") as server:
            http(server.port, "POST", "/sync-account-run-records", {"records": self.write_payload()})
        with open(self.snapshot_path, "r", encoding="utf-8") as handle:
            snapshot = json.load(handle)
        self.record("P2P-040", {
            "sqlite_columns": columns,
            "snapshot_keys": sorted(snapshot.keys()),
            "summary_keys": sorted(snapshot["summary"].keys()),
            "record_keys": sorted(snapshot["records"][0].keys()),
        })


def evaluate(obs, value):
    """Derive the raw PASS/FAIL status for one observation from its projection."""
    try:
        if obs == "P2P-025":
            return (value.get("status") == 500 and value.get("ok") is False
                    and value.get("rows_after_rollback") == 0)
        if obs == "P2P-026":
            return (value.get("status") == 200 and value.get("all_example") is True
                    and value.get("rows", 0) > 0 and value.get("rows") == value.get("unique_rows"))
        if obs == "P2P-027":
            return (value.get("status") == 200 and value.get("ok") is True
                    and value.get("lines") == 1 and len(value.get("fields", [])) == 5
                    and str(value.get("file_path_basename", "")).endswith("account-run-history.txt"))
        if obs == "P2P-028":
            return (value.get("present") is True and value.get("fail") == 0
                    and (value.get("pass") or 0) >= 1 and value.get("result") == "PASS")
        if obs == "P2P-029":
            return all(isinstance(value.get(key), str) and value.get(key)
                       for key in ("manifest", "rules", "background", "sidepanel"))
        if obs == "P2P-030":
            hotmail = value.get("hotmail", {})
            custom = value.get("custom", {})
            return ("ok" in hotmail.get("messages", []) and "ok" in hotmail.get("code", [])
                    and "ok" in custom.get("messages", []) and "ok" in custom.get("code", []))
        if obs == "P2P-031":
            return (value.get("status") == 200 and value.get("ok") is True
                    and value.get("count") == value.get("emails_len")
                    and "email" in value.get("keys", []))
        if obs == "P2P-032":
            return (value.get("hotmail", {}).get("status") == 500
                    and value.get("custom", {}).get("status") == 500
                    and value.get("hotmail", {}).get("keys") == ["error", "ok"]
                    and value.get("custom", {}).get("keys") == ["error", "ok"])
        if obs == "P2P-033":
            statuses = value.get("statuses", {})
            success = sum(count for code, count in statuses.items() if str(code) == "200")
            return (value.get("all_complete") is True and value.get("requests") == value.get("lines")
                    and success == value.get("requests") and len(statuses) == 1)
        if obs == "P2P-034":
            vocab = value.get("status_vocab", {})
            return (value.get("hotmail_code") == "123456" and value.get("custom_code") == "123456"
                    and vocab.get("success") == "success" and vocab.get("failed") == "failed"
                    and vocab.get("stopped") == "stopped" and vocab.get("running") is None
                    and bool(value.get("token_endpoints")))
        if obs == "P2P-035":
            def one(entry):
                return (entry.get("status") == 204 and entry.get("body_len") == 0
                        and all(entry.get("cors", {}).get(key) == expected for key, expected in CORS.items()))
            return one(value.get("hotmail", {})) and one(value.get("custom", {}))
        if obs == "P2P-036":
            hotmail = value.get("hotmail", [])
            custom = value.get("custom", [])
            return (all(route in hotmail for route in HOTMAIL_ROUTES)
                    and all(route in custom for route in CUSTOM_ROUTES))
        if obs == "P2P-037":
            return value.get("status") == 500 and value.get("ok") is False and bool(value.get("error"))
        if obs == "P2P-038":
            for key in ("200", "204", "404", "500"):
                entry = value.get(key, {})
                if not all(entry.get(name) == expected for name, expected in CORS.items()):
                    return False
            return str(value.get("content_type", "")).startswith("application/json")
        if obs == "P2P-039":
            return (value.get("status") == 200 and value.get("dotenv_loaded") == "probe-value"
                    and value.get("project_root_is_parent_of_scripts") is True)
        if obs == "P2P-040":
            columns = value.get("sqlite_columns", [])
            return ("id" in columns and "email" in columns
                    and set(value.get("snapshot_keys", [])) >= {"generatedAt", "records", "summary"}
                    and set(value.get("summary_keys", [])) >= {"total", "success", "failed", "retryTotal"})
    except Exception:
        return False
    return value is not None


def resolve_side(args):
    if args.side:
        return args.side
    here = os.path.abspath(__file__)
    if os.sep + "target" + os.sep in here:
        return "target"
    if os.sep + "source" + os.sep in here:
        return "source"
    if args.repo and "target" in os.path.abspath(args.repo):
        return "target"
    return "source"


def resolve_repo(side, args):
    candidates = []
    if args.repo:
        candidates.append(args.repo)
    for env_key in ("P2P_REPO", "BATCH_REPO", "REPO", "TARGET_REPO", "SOURCE_REPO"):
        if os.environ.get(env_key):
            candidates.append(os.environ[env_key])
    candidates.append(os.path.join("/input", side))
    candidates.append("/workspace/repo")
    for candidate in candidates:
        if candidate and os.path.isdir(os.path.join(candidate, "scripts")):
            return os.path.abspath(candidate)
    for candidate in candidates:
        if candidate and os.path.isdir(candidate):
            return os.path.abspath(candidate)
    return os.path.abspath(candidates[-1])


def resolve_report(args, side):
    for candidate in (args.out, args.report):
        if candidate:
            return candidate
    for env_key in ("P2P_REPORT", "REPORT"):
        if os.environ.get(env_key):
            return os.environ[env_key]
    for candidate in ("/logs/result.json",
                      os.path.join(tempfile.gettempdir(), "batch-p2p-0002-%s-report.json" % side)):
        directory = os.path.dirname(candidate)
        if os.path.isdir(directory) and os.access(directory, os.W_OK):
            return candidate
    return os.path.join(tempfile.gettempdir(), "batch-p2p-0002-report.json")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("report", nargs="?", default=None,
                        help="optional destination for the runtime report")
    parser.add_argument("--repo", default=None)
    parser.add_argument("--side", choices=["source", "target"], default=None)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    side = resolve_side(args)
    repo = resolve_repo(side, args)

    probe = Probe(repo, side)
    observations = {"P2P-028": run_js_publisher_test(repo)}
    try:
        observations.update(probe.run())
    except Exception as error:  # pragma: no cover - defensive
        sys.stderr.write("probe preparation failed: %r\n" % (error,))

    results = {}
    statuses = {}
    for observation_id in OBSERVATIONS:
        if observation_id in observations:
            value = observations[observation_id]
            present = True
        else:
            value = {"result": "FAIL", "present": False}
            present = False
        ok = evaluate(observation_id, value) if present else False
        statuses[observation_id] = "PASS" if ok else "FAIL"
        results[observation_id] = {
            "status": statuses[observation_id],
            "type": "json",
            "present": present,
            "value": value,
        }
    overall = "PASS" if all(status == "PASS" for status in statuses.values()) else "FAIL"

    report = {
        "schema_version": "migration-asset-fragment:v2",
        "batch_id": BATCH_ID,
        "side": side,
        "status": overall,
        "observations": {key: results[key]["value"] for key in OBSERVATIONS},
        "projection": {key: results[key]["value"] for key in OBSERVATIONS},
        "results": results,
        "statuses": statuses,
    }
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    destination = resolve_report(args, side)
    try:
        with open(destination, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    except Exception as error:  # pragma: no cover - defensive
        sys.stderr.write("could not write report to %s: %r\n" % (destination, error))
    sys.stdout.write(text + "\n")


if __name__ == "__main__":
    main()
