"""Concrete F2P observation probes for BATCH-F2P-0002.

Each probe drives the documented entry point declared by the observation's
atomic_claim / coverage.surface and evaluates the required target postcondition
with the same semantic predicate on the source and target sides.
"""
import json
import os
import sqlite3
import threading
import time

from probe_common import (
    HelperUnderProbe,
    TMP_DATA,
    cors_ok,
    read_log_lines,
    served_by_target,
)

_CACHE = {}
_LOCK = threading.Lock()


def helper(root, name):
    key = (root, name)
    with _LOCK:
        if key not in _CACHE:
            instance = HelperUnderProbe(root, name)
            instance.start()
            _CACHE[key] = instance
    return _CACHE[key]


def shutdown_all():
    for instance in list(_CACHE.values()):
        try:
            instance.stop()
        except Exception:
            pass
    _CACHE.clear()


def _json_ct(resp):
    return "application/json" in resp.headers.get("content-type", "")


def _reset_log(mod):
    path = getattr(mod, "ACCOUNT_LOG_PATH", None)
    if path and os.path.exists(path):
        os.remove(path)


def _reset_snapshot(mod):
    path = getattr(mod, "ACCOUNT_RECORDS_SNAPSHOT_PATH", None)
    if path and os.path.exists(path):
        os.remove(path)


def _fresh_db(mod):
    path = getattr(mod, "RANDOM_EMAIL_DB_PATH", None)
    if path and os.path.exists(path):
        os.remove(path)


# --- R001: helpers served through a Flask app -----------------------------

def p_f2p_026(root):
    h = helper(root, "custom")
    resp = h.request("POST", "/health", body={})
    ok = (
        resp.status == 200
        and resp.json() is not None
        and resp.json().get("ok") is True
        and _json_ct(resp)
        and cors_ok(resp)
        and served_by_target(resp)
    )
    return ok, {"status": resp.status, "body": resp.text[:200], "kind": h.kind}


def p_f2p_027(root):
    h = helper(root, "custom")
    _fresh_db(h.mod)
    resp = h.request("POST", "/random-email", body={"domain": "example.com", "n": 1})
    payload = resp.json() or {}
    ok = (
        resp.status == 200
        and payload.get("ok") is True
        and isinstance(payload.get("email"), str)
        and payload.get("email")
        and isinstance(payload.get("emails"), list)
        and payload.get("domain") == "example.com"
        and payload.get("count") == 1
        and served_by_target(resp)
    )
    return ok, {"status": resp.status, "body": resp.text[:200], "kind": h.kind}


def p_f2p_044(root):
    h_hot = helper(root, "hotmail")
    h_custom = helper(root, "custom")
    callables = all(
        callable(getattr(h_hot.mod, name, None))
        for name in ("select_latest_code", "refresh_access_token", "fetch_graph_messages", "get_json")
    ) and callable(getattr(h_custom.mod, "select_latest_code", None))
    _reset_log(h_hot.mod)
    resp_hot = h_hot.request(
        "POST",
        "/append-account-log",
        body={"email": "a@b.com", "password": "p", "status": "success", "reason": "r"},
    )
    resp_custom = h_custom.request("POST", "/health", body={})
    ok = (
        callables
        and resp_hot.status == 200
        and resp_custom.status == 200
        and served_by_target(resp_hot)
        and served_by_target(resp_custom)
    )
    return ok, {"callables": callables, "hotmail": resp_hot.status, "custom": resp_custom.status}


# --- R030: unknown path JSON 404 ------------------------------------------

def p_f2p_028(root):
    h = helper(root, "custom")
    resp = h.request("POST", "/does-not-exist", body={})
    payload = resp.json() or {}
    ok = (
        resp.status == 404
        and payload.get("ok") is False
        and payload.get("error") == "Unsupported path: /does-not-exist"
        and _json_ct(resp)
        and served_by_target(resp)
    )
    return ok, {"status": resp.status, "body": resp.text[:200], "kind": h.kind}


def p_f2p_043(root):
    h = helper(root, "hotmail")
    resp = h.request("POST", "/does-not-exist", body={})
    payload = resp.json() or {}
    ok = (
        resp.status == 404
        and payload.get("ok") is False
        and payload.get("error") == "Unsupported path: /does-not-exist"
        and _json_ct(resp)
        and served_by_target(resp)
    )
    return ok, {"status": resp.status, "body": resp.text[:200], "kind": h.kind}


# --- R029: OPTIONS preflight 204 with CORS --------------------------------

def _options_check(root, name):
    h = helper(root, name)
    resp = h.request("OPTIONS", "/messages", body=None, content_type=None)
    ok = (
        resp.status == 204
        and cors_ok(resp)
        and resp.headers.get("allow") is None
        and served_by_target(resp)
    )
    return ok, {"status": resp.status, "headers": dict(resp.headers), "kind": h.kind}


def p_f2p_029(root):
    return _options_check(root, "custom")


def p_f2p_030(root):
    return _options_check(root, "custom")


def p_f2p_038(root):
    return _options_check(root, "hotmail")


# --- R031: method not allowed remapped to JSON 404 ------------------------

def _wrong_method(root, name):
    h = helper(root, name)
    resp = h.request("GET", "/messages", body=None, content_type=None)
    payload = resp.json() or {}
    ok = (
        resp.status == 404
        and payload.get("ok") is False
        and "Unsupported path" in str(payload.get("error") or "")
        and _json_ct(resp)
        and served_by_target(resp)
    )
    return ok, {"status": resp.status, "body": resp.text[:200], "kind": h.kind}


def p_f2p_032(root):
    return _wrong_method(root, "hotmail")


def p_f2p_042(root):
    return _wrong_method(root, "hotmail")


# --- R038: missing credentials HTTP 500 -----------------------------------

def _missing_credentials(root):
    h = helper(root, "hotmail")
    resp = h.request("POST", "/messages", body={})
    payload = resp.json() or {}
    ok = (
        resp.status == 500
        and payload.get("ok") is False
        and payload.get("error") == "Missing email/clientId/refreshToken"
        and served_by_target(resp)
    )
    return ok, {"status": resp.status, "body": resp.text[:200], "kind": h.kind}


def p_f2p_034(root):
    return _missing_credentials(root)


def p_f2p_047(root):
    return _missing_credentials(root)


# --- R033: JSON body parsed regardless of content type --------------------

def _text_plain_json(root):
    h = helper(root, "custom")
    _fresh_db(h.mod)
    resp = h.request(
        "POST",
        "/random-email",
        body={"domain": "example.com", "n": 1},
        content_type="text/plain",
    )
    payload = resp.json() or {}
    ok = (
        resp.status == 200
        and payload.get("ok") is True
        and bool(payload.get("email"))
        and served_by_target(resp)
    )
    return ok, {"status": resp.status, "body": resp.text[:200], "kind": h.kind}


def p_f2p_035(root):
    return _text_plain_json(root)


def p_f2p_036(root):
    return _text_plain_json(root)


# --- R022: no http.server on the execution path ---------------------------

def p_f2p_039(root):
    h = helper(root, "hotmail")
    _reset_log(h.mod)
    resp = h.request(
        "POST",
        "/append-account-log",
        body={"email": "a@b.com", "password": "p", "status": "success", "reason": "r"},
    )
    ok = (
        resp.status == 200
        and not h.has_source_http_symbols()
        and served_by_target(resp)
    )
    return ok, {"status": resp.status, "source_symbols": h.has_source_http_symbols(), "kind": h.kind}


def p_f2p_025(root):
    h = helper(root, "custom")
    statuses = {}
    for path in ("/messages", "/code", "/health", "/env", "/random-email"):
        body = {"domain": "example.com", "n": 1} if path == "/random-email" else {}
        resp = h.request("POST", path, body=body)
        statuses[path] = resp.status
    ok = (
        all(status not in (404, 405) for status in statuses.values())
        and not h.has_source_http_symbols()
        and h.flask_app is not None
    )
    return ok, {"statuses": statuses, "source_symbols": h.has_source_http_symbols(), "kind": h.kind}


def p_f2p_048(root):
    h = helper(root, "hotmail")
    _reset_log(h.mod)
    checks = []
    success = h.request(
        "POST",
        "/append-account-log",
        body={"email": "a@b.com", "password": "p", "status": "success", "reason": "r"},
    )
    checks.append(success.status == 200 and (success.json() or {}).get("ok") is True and _json_ct(success) and cors_ok(success) and served_by_target(success))
    not_found = h.request("POST", "/does-not-exist", body={})
    checks.append(not_found.status == 404 and (not_found.json() or {}).get("ok") is False and _json_ct(not_found) and cors_ok(not_found) and served_by_target(not_found))
    failure = h.request("POST", "/messages", body={})
    checks.append(failure.status == 500 and (failure.json() or {}).get("ok") is False and _json_ct(failure) and cors_ok(failure) and served_by_target(failure))
    preflight = h.request("OPTIONS", "/messages", body=None, content_type=None)
    checks.append(preflight.status == 204 and cors_ok(preflight) and served_by_target(preflight))
    ok = all(checks) and not h.has_source_http_symbols()
    return ok, {"checks": checks, "kind": h.kind}


# --- R023: no subprocess / RPC / FFI delegation ---------------------------

def p_f2p_037(root):
    h = helper(root, "hotmail")
    _reset_log(h.mod)
    resp = h.request(
        "POST",
        "/append-account-log",
        body={"email": "a@b.com", "password": "p", "status": "success", "reason": "r"},
    )
    ok = (
        resp.status == 200
        and served_by_target(resp)
        and not resp.delegation_dispatched
    )
    return ok, {"status": resp.status, "delegation": resp.delegation_dispatched, "kind": h.kind}


# --- R039: random-email transaction + distinct rows -----------------------

def p_f2p_040(root):
    h = helper(root, "custom")
    _fresh_db(h.mod)
    resp = h.request("POST", "/random-email", body={"domain": "example.com", "n": 3})
    payload = resp.json() or {}
    emails = payload.get("emails") or []
    committed = 0
    if emails:
        connection = sqlite3.connect(h.mod.RANDOM_EMAIL_DB_PATH)
        try:
            committed = connection.execute(
                "SELECT COUNT(*) FROM generated_emails WHERE email IN (%s)"
                % ",".join("?" for _ in emails),
                list(emails),
            ).fetchone()[0]
        finally:
            connection.close()
    ok = (
        resp.status == 200
        and payload.get("ok") is True
        and len(emails) == 3
        and len(set(emails)) == 3
        and committed == 3
        and served_by_target(resp)
    )
    return ok, {"status": resp.status, "count": len(emails), "committed": committed, "kind": h.kind}


# --- R036: sync account run snapshot --------------------------------------

def _sync_snapshot(root):
    h = helper(root, "hotmail")
    _reset_snapshot(h.mod)
    resp = h.request(
        "POST",
        "/sync-account-run-records",
        body={"records": [{"email": "a@b.com", "password": "p", "finalStatus": "success", "retryCount": 2}]},
    )
    payload = resp.json() or {}
    file_path = str(payload.get("filePath") or "")
    summary = None
    record_retry = None
    if os.path.exists(h.mod.ACCOUNT_RECORDS_SNAPSHOT_PATH):
        with open(h.mod.ACCOUNT_RECORDS_SNAPSHOT_PATH, "r", encoding="utf-8") as handle:
            snapshot = json.load(handle)
        summary = snapshot.get("summary")
        records = snapshot.get("records") or []
        record_retry = records[0].get("retryCount") if records else None
    ok = (
        resp.status == 200
        and payload.get("ok") is True
        and file_path.endswith(os.path.join("data", "account-run-history.json"))
        and summary == {"total": 1, "success": 1, "failed": 0, "retryTotal": 2}
        and record_retry == 2
        and served_by_target(resp)
    )
    return ok, {"status": resp.status, "filePath": file_path, "summary": summary, "retry": record_retry, "kind": h.kind}


def p_f2p_041(root):
    return _sync_snapshot(root)


def p_f2p_046(root):
    return _sync_snapshot(root)


# --- R037: append-account-log single tab-separated line -------------------

def p_f2p_045(root):
    h = helper(root, "hotmail")
    _reset_log(h.mod)
    resp = h.request(
        "POST",
        "/append-account-log",
        body={
            "email": "a@b.com",
            "password": "p",
            "status": "success",
            "reason": "ok",
            "recordedAt": "2026-01-01T00:00:00Z",
        },
    )
    payload = resp.json() or {}
    lines = read_log_lines(h.mod.ACCOUNT_LOG_PATH)
    fields = lines[-1].split("\t") if lines else []
    ok = (
        resp.status == 200
        and payload.get("ok") is True
        and str(payload.get("filePath") or "").endswith("account-run-history.txt")
        and len(lines) == 1
        and fields == ["2026-01-01T00:00:00Z", "a@b.com", "p", "success", "ok"]
        and served_by_target(resp)
    )
    return ok, {"status": resp.status, "lines": lines, "fields": fields, "kind": h.kind}


# --- R041: threaded serving + shared lock ---------------------------------

def p_f2p_031(root):
    h = helper(root, "hotmail")
    _reset_log(h.mod)
    results = {}

    def fire(index):
        results[index] = h.request(
            "POST",
            "/append-account-log",
            body={
                "email": f"user{index}@example.com",
                "password": "p",
                "status": "success",
                "reason": "r",
                "recordedAt": f"2026-01-01T00:00:0{index}Z",
            },
        )

    threads = [threading.Thread(target=fire, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    lines = read_log_lines(h.mod.ACCOUNT_LOG_PATH)
    complete = all(len(line.split("\t")) == 5 for line in lines)
    ok = (
        all(resp.status == 200 for resp in results.values())
        and all(served_by_target(resp) for resp in results.values())
        and len(lines) == 2
        and complete
    )
    return ok, {"statuses": {k: v.status for k, v in results.items()}, "lines": lines, "kind": h.kind}


# --- R035: UTF-8 JSON content type ----------------------------------------

def p_f2p_033(root):
    h = helper(root, "custom")
    resp = h.request("POST", "/health", body={})
    content_type = resp.headers.get("content-type", "")
    ok = (
        resp.status == 200
        and content_type == "application/json; charset=utf-8"
        and served_by_target(resp)
    )
    return ok, {"status": resp.status, "content_type": content_type, "kind": h.kind}


PROBES = {
    "F2P-025": p_f2p_025,
    "F2P-026": p_f2p_026,
    "F2P-027": p_f2p_027,
    "F2P-028": p_f2p_028,
    "F2P-029": p_f2p_029,
    "F2P-030": p_f2p_030,
    "F2P-031": p_f2p_031,
    "F2P-032": p_f2p_032,
    "F2P-033": p_f2p_033,
    "F2P-034": p_f2p_034,
    "F2P-035": p_f2p_035,
    "F2P-036": p_f2p_036,
    "F2P-037": p_f2p_037,
    "F2P-038": p_f2p_038,
    "F2P-039": p_f2p_039,
    "F2P-040": p_f2p_040,
    "F2P-041": p_f2p_041,
    "F2P-042": p_f2p_042,
    "F2P-043": p_f2p_043,
    "F2P-044": p_f2p_044,
    "F2P-045": p_f2p_045,
    "F2P-046": p_f2p_046,
    "F2P-047": p_f2p_047,
    "F2P-048": p_f2p_048,
}

OBSERVATION_ORDER = [
    "F2P-029", "F2P-035", "F2P-040", "F2P-045", "F2P-026", "F2P-030",
    "F2P-032", "F2P-034", "F2P-039", "F2P-041", "F2P-044", "F2P-025",
    "F2P-027", "F2P-028", "F2P-031", "F2P-033", "F2P-036", "F2P-037",
    "F2P-038", "F2P-042", "F2P-043", "F2P-046", "F2P-047", "F2P-048",
]
