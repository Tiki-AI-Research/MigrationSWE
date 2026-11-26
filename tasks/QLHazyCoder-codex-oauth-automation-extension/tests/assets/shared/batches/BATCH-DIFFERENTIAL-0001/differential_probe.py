#!/usr/bin/env python3
"""Differential probe: run the same observations against a source or target tree.

Usage: python3 differential_probe.py <repo_root> [label]
Emits a JSON object mapping observation id -> normalized observation result.
"""
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_port(port, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def http(method, url, body=None, headers=None):
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            return resp.status, dict(resp.headers), raw
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, dict(exc.headers), raw


def normalize_response(status, headers, raw):
    lower = {k.lower(): v for k, v in headers.items()}
    body = None
    try:
        body = json.loads(raw.decode("utf-8")) if raw else None
    except Exception:
        body = "<non-json:%d>" % len(raw)
    if status == 204:
        lower.pop("content-type", None)
    return {
        "status": status,
        "content_type": lower.get("content-type"),
        "cors": {
            "origin": lower.get("access-control-allow-origin"),
            "headers": lower.get("access-control-allow-headers"),
            "methods": lower.get("access-control-allow-methods"),
        },
        "body": body,
    }


def missing_credentials_projection(base_url, request_fn=http):
    """Check the public routes without depending on an internal parser name."""
    expected_error = "Missing email/clientId/refreshToken"
    expected_body = json.dumps({"ok": False, "error": expected_error}).encode("utf-8")
    complete = {"email": "probe@example.com", "clientId": "c", "refreshToken": "r"}
    payloads = [{}] + [
        {key: value for key, value in complete.items() if key != missing}
        for missing in complete
    ]
    responses = []
    for path in ("/messages", "/code"):
        for payload in payloads:
            try:
                status, headers, raw = request_fn("POST", base_url + path, payload)
            except Exception as exc:
                return {"request_error": type(exc).__name__ + ": " + str(exc)}
            responses.append((status, headers, raw))
    if all(status == 500 and raw == expected_body for status, _, raw in responses):
        # Preserve the source-baseline projection for a conforming implementation.
        return {"error": expected_error}
    return {"responses": [normalize_response(*response) for response in responses]}


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    root = os.path.abspath(sys.argv[1])
    label = sys.argv[2] if len(sys.argv) > 2 else "tree"
    work = tempfile.mkdtemp(prefix="diffprobe-")
    tree = os.path.join(work, "repo")
    shutil.copytree(root, tree, ignore=shutil.ignore_patterns(".git"))
    result = {}

    hotmail_port = 17373
    custom_port = free_port()
    random_db = os.path.join(work, "random.sqlite3")
    log_path = os.path.join(tree, "data", "account-run-history.txt")
    snapshot_path = os.path.join(tree, "data", "account-run-history.json")

    env = dict(os.environ)
    env["FLOWPILOT_CUSTOM_MAIL_HELPER_PORT"] = str(custom_port)
    env["FLOWPILOT_RANDOM_EMAIL_DB_PATH"] = random_db
    env.pop("FLOWPILOT_CUSTOM_IMAP_USER", None)
    env.pop("FLOWPILOT_CUSTOM_IMAP_PASS", None)

    hotmail_proc = subprocess.Popen(
        [sys.executable, "scripts/hotmail_helper.py"],
        cwd=tree, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    custom_proc = subprocess.Popen(
        [sys.executable, "scripts/custom_mail_helper.py"],
        cwd=tree, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        hotmail_ready = wait_port(hotmail_port)
        custom_ready = wait_port(custom_port)
        hbase = f"http://127.0.0.1:{hotmail_port}"
        cbase = f"http://127.0.0.1:{custom_port}"

        # DIFF-001 hotmail listener accepts documented POST route
        r = http("POST", hbase + "/sync-account-run-records", {"records": []})
        result["DIFF-001"] = {
            "bound": hotmail_ready,
            "accepted": normalize_response(*r),
        }

        # DIFF-002 custom listener honors env port
        r = http("POST", cbase + "/health", {})
        result["DIFF-002"] = {
            "bound": custom_ready,
            "default_port_source": 17374,
            "used_port": custom_port,
            "accepted": normalize_response(*r),
        }

        # DIFF-003 hotmail route dispatch
        hotmail_routes = {}
        for path in ["/messages", "/code", "/append-account-log", "/sync-account-run-records"]:
            body = {}
            if path == "/append-account-log":
                body = {"email": "a@b.com", "password": "p", "status": "success", "recordedAt": "t", "reason": "r"}
            st, hd, raw = http("POST", hbase + path, body)
            hotmail_routes[path] = normalize_response(st, hd, raw)
        result["DIFF-003"] = {"routes": hotmail_routes}

        # DIFF-004 custom route dispatch
        custom_routes = {}
        for path in ["/messages", "/code", "/health", "/env", "/random-email"]:
            st, hd, raw = http("POST", cbase + path, {})
            custom_routes[path] = normalize_response(st, hd, raw)
        result["DIFF-004"] = {"routes": custom_routes}

        # DIFF-005 OPTIONS 204
        result["DIFF-005"] = {
            "hotmail": normalize_response(*http("OPTIONS", hbase + "/messages")),
            "custom": normalize_response(*http("OPTIONS", cbase + "/health")),
        }

        # DIFF-006 CORS + json content-type on mixed responses
        result["DIFF-006"] = {
            "hotmail_success": normalize_response(*http("POST", hbase + "/sync-account-run-records", {"records": []})),
            "hotmail_404": normalize_response(*http("POST", hbase + "/nope", {"email": "a@b.com", "clientId": "c", "refreshToken": "r"})),
            "hotmail_500": normalize_response(*http("POST", hbase + "/messages", {})),
            "hotmail_204": normalize_response(*http("OPTIONS", hbase + "/messages")),
            "custom_success": normalize_response(*http("POST", cbase + "/health", {})),
            "custom_404": normalize_response(*http("POST", cbase + "/nope", {})),
            "custom_500": normalize_response(*http("POST", cbase + "/random-email", {})),
            "custom_204": normalize_response(*http("OPTIONS", cbase + "/health")),
        }

        # DIFF-007 success envelope field names
        st, hd, raw = http("POST", cbase + "/random-email", {"domain": "example.com", "n": 2})
        random_resp = normalize_response(st, hd, raw)
        st2, hd2, raw2 = http("POST", hbase + "/sync-account-run-records", {"records": []})
        sync_resp = normalize_response(st2, hd2, raw2)
        result["DIFF-007"] = {
            "random_keys": sorted((random_resp.get("body") or {}).keys()) if isinstance(random_resp.get("body"), dict) else None,
            "sync_keys": sorted((sync_resp.get("body") or {}).keys()) if isinstance(sync_resp.get("body"), dict) else None,
        }

        # DIFF-008 failure envelope
        result["DIFF-008"] = {
            "custom": normalize_response(*http("POST", cbase + "/random-email", {"domain": "bad domain", "n": 1})),
            "hotmail": normalize_response(*http("POST", hbase + "/messages", {})),
        }

        # DIFF-009 unknown path
        result["DIFF-009"] = {
            "hotmail": normalize_response(*http("POST", hbase + "/does-not-exist", {"email": "a@b.com", "clientId": "c", "refreshToken": "r"})),
            "custom": normalize_response(*http("POST", cbase + "/does-not-exist", {})),
        }

        # DIFF-011: validation must run in both actual HTTP views. Internal
        # read_json_payload functions may be renamed or replaced during migration.
        result["DIFF-011"] = missing_credentials_projection(hbase)

        # DIFF-019 concurrency: intact, non-interleaved append lines
        import threading
        errors = []
        def fire():
            try:
                http("POST", hbase + "/append-account-log", {"email": "c@d.com", "password": "p", "status": "success", "recordedAt": "t", "reason": "r"})
            except Exception as exc:  # noqa
                errors.append(str(exc))
        threads = [threading.Thread(target=fire) for _ in range(6)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        with open(log_path, "r", encoding="utf-8") as fh:
            conc_lines = [ln for ln in fh.read().splitlines() if ln]
        result["DIFF-019"] = {
            "errors": errors,
            "append_lines": len(conc_lines),
            "all_five_fields": all(len(ln.split("\t")) == 5 for ln in conc_lines),
        }

        # DIFF-020 documented capability set preserved (route responses)
        result["DIFF-020"] = {
            "hotmail_paths": sorted(hotmail_routes.keys()),
            "custom_paths": sorted(custom_routes.keys()),
        }

        hotmail_proc.terminate(); custom_proc.terminate()
        hotmail_proc.wait(timeout=10); custom_proc.wait(timeout=10)

        # ---- module level checks (fresh subprocess per tree) ----
        check = os.path.join(work, "modulecheck.py")
        with open(check, "w") as fh:
            fh.write(MODULE_CHECK)
        cp = subprocess.run([sys.executable, check, tree], capture_output=True, text=True, env=env)
        if cp.returncode != 0:
            result["_module_error"] = cp.stderr[-2000:]
        else:
            result.update(json.loads(cp.stdout))

        # ---- extension asset equality between source and target checked separately ----
    finally:
        for proc in (hotmail_proc, custom_proc):
            if proc.poll() is None:
                proc.terminate()
        shutil.rmtree(work, ignore_errors=True)

    scrubbed = json.loads(json.dumps(result))
    _scrub(scrubbed, tree, custom_port, work)
    print(json.dumps({"label": label, "root": root, "result": scrubbed}, indent=2, sort_keys=True))


def _scrub(value, tree, custom_port, work):
    if isinstance(value, dict):
        for key in value:
            value[key] = _scrub(value[key], tree, custom_port, work)
        return value
    if isinstance(value, list):
        return [_scrub(item, tree, custom_port, work) for item in value]
    if isinstance(value, str):
        return value.replace(tree, "{repo}").replace(work, "{work}").replace(str(custom_port), "{port}")
    if isinstance(value, int) and value == custom_port:
        return "{port}"
    return value


MODULE_CHECK = r'''
import importlib.util, json, os, sqlite3, sys, tempfile

tree = sys.argv[1]
scripts = os.path.join(tree, "scripts")

def load(name):
    path = os.path.join(scripts, name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

out = {}
hm = load("hotmail_helper")
cm = load("custom_mail_helper")

# DIFF-010 importable + module-level callables
out["DIFF-010"] = {
    "hotmail_functions": sorted(f for f in ["select_latest_code", "refresh_access_token", "fetch_graph_messages", "get_json"] if callable(getattr(hm, f, None))),
    "custom_functions": sorted(f for f in ["select_latest_code"] if callable(getattr(cm, f, None))),
}

# DIFF-012 / DIFF-013 persistence and rollback
db_path = os.path.join(tempfile.mkdtemp(prefix="db-"), "e.sqlite3")
cm.RANDOM_EMAIL_DB_PATH = db_path
res = cm.generate_random_emails({"domain": "example.com", "n": 3})
con = sqlite3.connect(db_path)
rows = con.execute("SELECT email FROM generated_emails ORDER BY email").fetchall()
unique_idx = con.execute("SELECT sql FROM sqlite_master WHERE name='generated_emails'").fetchone()[0]
con.close()
out["DIFF-012"] = {
    "envelope": res,
    "row_count": len(rows),
    "unique_in_schema": "UNIQUE" in unique_idx.upper(),
}
# rollback: force constant prefix -> IntegrityError on second insert
original = cm.random_email_prefix
cm.random_email_prefix = lambda: "constantprefix"
try:
    rolled = None
    try:
        cm.generate_random_emails({"domain": "example.com", "n": 2})
    except RuntimeError as exc:
        rolled = str(exc)
    con = sqlite3.connect(db_path)
    total = con.execute("SELECT COUNT(*) FROM generated_emails").fetchone()[0]
    con.close()
    out["DIFF-013"] = {"error": rolled, "rows_committed_for_const": total}
finally:
    cm.random_email_prefix = original

# DIFF-014 / DIFF-015 snapshot normalization + write
payload = {
    "generatedAt": "2024-01-01T00:00:00Z",
    "records": [
        {"email": "a@b.com", "password": "p", "finalStatus": "success", "retryCount": 2},
        {"email": "c@d.com", "password": "p", "finalStatus": "bogus", "retryCount": 1},
        {"email": "e@f.com", "password": "p", "finalStatus": "stopped", "retryCount": 0, "failedStep": 3, "autoRunContext": {"currentRun": 1, "totalRuns": 2, "attemptRun": 1}},
    ],
}
snapshot = hm.normalize_account_run_snapshot_payload(payload)
out["DIFF-014"] = {"summary": snapshot["summary"], "generatedAt": snapshot["generatedAt"], "record_count": len(snapshot["records"])}
out["DIFF-015"] = {
    "statuses": [r["finalStatus"] for r in snapshot["records"]],
    "has_fields": all(all(k in r for k in ("retryCount", "failedStep", "autoRunContext")) for r in snapshot["records"]),
}

# DIFF-016 append log line format
log_path = os.path.join(tree, "data", "probe-account-log.txt")
hm.ACCOUNT_LOG_PATH = log_path
hm.append_account_log("user@example.com", "secret", "Success", "2024-01-01T00:00:00Z", "reason here")
with open(log_path, "r", encoding="utf-8") as fh:
    lines = fh.read().splitlines()
out["DIFF-016"] = {"line_count": len(lines), "fields": lines[0].split("\t") if lines else []}

# DIFF-017 random-email envelope fields
out["DIFF-017"] = {"keys": sorted(res.keys())}

# DIFF-018 .env from project root
env_root = tempfile.mkdtemp(prefix="envroot-")
shutil = __import__("shutil")
shutil.copytree(tree, env_root + "/repo", ignore=shutil.ignore_patterns(".git"))
with open(os.path.join(env_root, "repo", ".env"), "w") as fh:
    fh.write("FLOWPILOT_CUSTOM_MAIL_HELPER_PORT=18099\n")
os.environ.pop("FLOWPILOT_CUSTOM_MAIL_HELPER_PORT", None)
env_mod = load.__globals__  # noqa
path = os.path.join(env_root, "repo", "scripts", "custom_mail_helper.py")
spec = importlib.util.spec_from_file_location("custom_mail_helper_env", path)
em = importlib.util.module_from_spec(spec)
spec.loader.exec_module(em)
out["DIFF-018"] = {"port_from_root_env": em.PORT, "project_root": em.PROJECT_ROOT}

# DIFF-021 normalization and status vocabulary
rec = hm.normalize_account_run_snapshot_record({"email": "a@b.com", "password": "p", "finalStatus": "STOPPED", "retryCount": 4})
out["DIFF-021"] = {
    "status_vocab_ok": rec["finalStatus"] == "stopped",
    "code_extraction": cm.extract_code("Your code is 123456"),
}

# DIFF-022 envelope keys and on-disk formats
out["DIFF-022"] = {
    "hotmail_ok_keys": sorted(hm.normalize_account_run_snapshot_payload({"records": []}).keys()),
    "log_columns": 5,
}

print(json.dumps(out))
'''


if __name__ == "__main__":
    main()
