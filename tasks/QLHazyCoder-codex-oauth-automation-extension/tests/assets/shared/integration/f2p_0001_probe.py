#!/usr/bin/env python3
"""Executable driver for the declarative BATCH-F2P-0001 target postconditions.

BATCH-F2P-0001 shipped atomic claims without an execution engine; this driver
runs each claim against the real serving path of one repository checkout and
evaluates the shared target-technology predicate (Flask owns routing, JSON
parsing, response writing and CORS; no ``http.server`` dispatch or delegation).

Usage: ``f2p_0001_probe.py <side> <repo> <out>``.
"""

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
from contextlib import contextmanager

HERE = os.path.dirname(os.path.abspath(__file__))
ASSET_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))

ENV_OBSERVATIONS = {"F2P-007", "F2P-009", "F2P-015"}
CONCURRENT_OBSERVATIONS = {"F2P-002", "F2P-014"}


def load_common():
    path = os.path.join(ASSET_ROOT, "shared", "batches", "BATCH-F2P-0002", "probe_common.py")
    spec = importlib.util.spec_from_file_location("f2p_0001_common", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def helper_kind(file_name):
    return "hotmail" if "hotmail" in (file_name or "") else "custom"


def technology_projection(common, repo):
    modules = {}
    served = True
    for helper, file_name in (("hotmail", "hotmail_helper"), ("custom", "custom_mail_helper")):
        try:
            instance = common.HelperUnderProbe(repo, helper)
            flask_app = instance.flask_app is not None
            source_symbols = instance.has_source_http_symbols()
            modules[helper] = {"flask_app": flask_app, "source_http_symbols": source_symbols}
            served = served and flask_app and not source_symbols
            instance.stop()
        except Exception as error:  # noqa: BLE001
            modules[helper] = {"error": "%s: %s" % (type(error).__name__, error)}
            served = False
    return served, modules


def env_projection(common, repo):
    work = tempfile.mkdtemp(prefix="f2p-0001-env-")
    tree = os.path.join(work, "repo")
    shutil.copytree(repo, tree, ignore=shutil.ignore_patterns(".git"))
    with open(os.path.join(tree, ".env"), "w", encoding="utf-8") as handle:
        handle.write("FLOWPILOT_CUSTOM_MAIL_HELPER_PORT=18000\n")
    os.environ.pop("FLOWPILOT_CUSTOM_MAIL_HELPER_PORT", None)
    instance = common.HelperUnderProbe(tree, "custom")
    port = getattr(instance.mod, "PORT", None)
    flask_app = instance.flask_app is not None
    source_symbols = instance.has_source_http_symbols()
    instance.stop()
    served = flask_app and not source_symbols
    passed = port == 18000 and served
    return passed, {
        "mode": "project-root-env",
        "port": port,
        "flask_app": flask_app,
        "source_http_symbols": source_symbols,
        "target_technology_served": served,
    }


STATUS_EXPECTATIONS = {
    "F2P-001": lambda status: status == 500,
    "F2P-005": lambda status: status in (200, 404, 500),
    "F2P-006": lambda status: status == 200,
    "F2P-010": lambda status: status == 404,
    "F2P-011": lambda status: status in (200, 500),
    "F2P-012": lambda status: status == 204,
    "F2P-016": lambda status: status == 200,
    "F2P-017": lambda status: status == 404,
    "F2P-019": lambda status: status == 500,
    "F2P-021": lambda status: status == 404,
    "F2P-022": lambda status: status == 200,
    "F2P-024": lambda status: status == 404,
}


def status_ok(observation_id, method, path, resp):
    expectation = STATUS_EXPECTATIONS.get(observation_id)
    if expectation is not None:
        return expectation(resp.status)
    if path.endswith("does-not-exist"):
        return resp.status == 404 and b"Unsupported path" in resp.body
    if method == "POST" and path in ("/messages", "/code"):
        return resp.status == 500
    return True


@contextmanager
def track_flask_json_parsing(app):
    """Observe Flask's parsing extension points without parsing a body ourselves.

    Both a custom Request.get_json and an app JSON provider are valid Flask
    implementations. Merely reading request.get_data then calling the stdlib
    parser outside those extension points does not migrate JSON parsing.
    """
    calls = []
    if app is None:
        yield calls
        return
    request_class = app.request_class
    original_request_parser = request_class.get_json
    own_request_parser = request_class.__dict__.get("get_json")
    original_provider_parser = app.json.loads
    own_provider_parser = vars(app.json).get("loads")

    def request_parser(*args, **kwargs):
        calls.append("request")
        return original_request_parser(*args, **kwargs)

    def provider_parser(*args, **kwargs):
        calls.append("provider")
        return original_provider_parser(*args, **kwargs)

    request_class.get_json = request_parser
    app.json.loads = provider_parser
    try:
        yield calls
    finally:
        if own_request_parser is None:
            del request_class.get_json
        else:
            request_class.get_json = own_request_parser
        if own_provider_parser is None:
            del app.json.loads
        else:
            app.json.loads = own_provider_parser


def run_http(common, repo, entry, helpers):
    spec = entry["probe"]
    helper = helper_kind(spec.get("file"))
    instance = helpers.get(helper)
    if instance is None:
        instance = common.HelperUnderProbe(repo, helper)
        instance.start()
        helpers[helper] = instance
    method = spec.get("method", "POST")
    path = spec.get("path", "/")
    body = spec.get("body")
    if body is None and method in ("POST",):
        body = {}
    content_type = spec.get("content_type", "application/json")
    with track_flask_json_parsing(instance.flask_app) as json_calls:
        resp = instance.request(method, path, body=body, content_type=content_type)
    served = common.served_by_target(resp)
    specific = status_ok(entry["observation_id"], method, path, resp)
    consuming_paths = {"/messages", "/code", "/random-email"} if helper == "custom" else {
        "/messages", "/code", "/append-account-log", "/sync-account-run-records"
    }
    # These declarative requests have valid framing and a nonempty JSON body.
    # Unknown paths, unsupported methods and body-independent routes must not
    # be required to invoke any parser.
    parsing_required = method == "POST" and path in consuming_paths
    parsing_owned = not parsing_required or bool(json_calls)
    source_symbols = instance.has_source_http_symbols()
    if entry["observation_id"] == "F2P-020":
        specific = specific and not source_symbols
    passed = served and specific and parsing_owned
    value = {
        "helper": helper,
        "method": method,
        "path": path,
        "status": resp.status,
        "flask_served": bool(resp.flask_served),
        "http_server_dispatched": bool(resp.http_server_dispatched),
        "delegation_dispatched": bool(resp.delegation_dispatched),
        "source_http_symbols": source_symbols,
        "target_technology_served": served,
        "specific_contract_ok": specific,
        "json_parsing_required": parsing_required,
        "flask_json_parser_calls": list(json_calls),
        "json_parsing_owned_by_target": parsing_owned,
    }
    return passed, value


def run_concurrent(common, repo, entry, helpers):
    spec = entry["probe"]
    helper = helper_kind(spec.get("file"))
    instance = helpers.get(helper)
    if instance is None:
        instance = common.HelperUnderProbe(repo, helper)
        instance.start()
        helpers[helper] = instance
    statuses = []
    served_flags = []
    lock = threading.Lock()

    def fire(index):
        resp = instance.request(
            "POST",
            spec.get("path", "/append-account-log"),
            body={
                "recordedAt": "2026-01-01T00:00:0%dZ" % index,
                "email": "user%d@example.com" % index,
                "password": "p%d" % index,
                "status": "success",
                "reason": "r%d" % index,
            },
        )
        with lock:
            statuses.append(resp.status)
            served_flags.append(common.served_by_target(resp))

    threads = [threading.Thread(target=fire, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    served = bool(served_flags) and all(served_flags)
    specific = all(status == 200 for status in statuses)
    return served and specific, {
        "helper": helper,
        "mode": "concurrent-append",
        "statuses": sorted(statuses),
        "target_technology_served": served,
        "specific_contract_ok": specific,
    }


def main(argv):
    side, repo, out_path = argv[1], argv[2], argv[3]
    common = load_common()
    entries_path = os.path.join(ASSET_ROOT, side, "batches", "BATCH-F2P-0001")
    with open(entries_path, "r", encoding="utf-8") as handle:
        entries = json.load(handle)["entries"]

    helpers = {}
    results = {}
    try:
        for entry in entries:
            observation_id = entry["observation_id"]
            spec = entry.get("probe", {})
            try:
                if observation_id in ENV_OBSERVATIONS:
                    passed, value = env_projection(common, repo)
                elif spec.get("method") == "RUN":
                    served, modules = technology_projection(common, repo)
                    passed = served
                    value = {
                        "mode": "target-technology",
                        "modules": modules,
                        "target_technology_served": served,
                    }
                elif observation_id in CONCURRENT_OBSERVATIONS:
                    passed, value = run_concurrent(common, repo, entry, helpers)
                else:
                    passed, value = run_http(common, repo, entry, helpers)
                results[observation_id] = {
                    "status": "PASS" if passed else "FAIL",
                    "value": value,
                }
            except Exception as error:  # noqa: BLE001
                results[observation_id] = {
                    "status": "INCONCLUSIVE",
                    "value": {"error": "%s: %s" % (type(error).__name__, error)},
                }
    finally:
        for instance in helpers.values():
            try:
                instance.stop()
            except Exception:  # noqa: BLE001
                pass

    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(results, handle, ensure_ascii=False, indent=2, sort_keys=True)
    print(json.dumps({key: value["status"] for key, value in results.items()}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
