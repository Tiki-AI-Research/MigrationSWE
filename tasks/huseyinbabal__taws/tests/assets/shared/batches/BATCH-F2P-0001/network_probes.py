#!/usr/bin/env python3
"""Network/behavioural F2P probes for the non-interactive taws surface.

The probes drive the *built candidate binary* against a loopback mock AWS
endpoint and assert only the declared public contract: the four AWS protocols
(query / json / rest-json / rest-xml), SigV4 signing, pagination, the
read-only write guard, and the endpoint override precedence.  Nothing here
depends on the candidate's Go package layout, module path, or symbol names.

Usage:

    network_probes.py --bin /path/to/taws --records /path/to/records.jsonl

Each probe appends one record line:

    {"observation_id": "F2P-025", "status": "pass"|"fail", "detail": "..."}

The harness merges these records into the shared F2P report.  Any transport,
build, or assertion problem is reported as a *fail* rather than silently
passing.
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time

SIGV4_PREFIX = "AWS4-HMAC-SHA256 "
SIG_RE = re.compile(r"Signature=[0-9a-f]{64}")
REQUEST_TIMEOUT = 45

AWS_ACCESS_KEY = "AKIDEXAMPLE"
AWS_SECRET_KEY = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _serve(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        captured = self.server.captured
        captured.append({
            "method": self.command,
            "path": self.path,
            "headers": {key.lower(): value for key, value in self.headers.items()},
            "body": body,
        })
        index = min(len(captured) - 1, len(self.server.responses) - 1)
        response = self.server.responses[index]
        payload = response["body"].encode("utf-8")
        self.send_response(response.get("status", 200))
        self.send_header("Content-Type", response.get("content_type", "application/json"))
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = do_DELETE = do_PUT = _serve

    def log_message(self, *args):  # silence the default stderr logging
        pass


class _Server:
    def __init__(self, responses):
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.responses = responses
        self.httpd.captured = []
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def captured(self):
        return self.httpd.captured

    def url(self):
        return "http://127.0.0.1:%d" % self.port

    def stop(self):
        try:
            self.httpd.shutdown()
        except Exception:
            pass
        try:
            self.httpd.server_close()
        except Exception:
            pass


def _env(home):
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": home,
        "AWS_ACCESS_KEY_ID": AWS_ACCESS_KEY,
        "AWS_SECRET_ACCESS_KEY": AWS_SECRET_KEY,
        "AWS_REGION": "us-east-1",
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_SHARED_CREDENTIALS_FILE": os.path.join(home, "aws", "credentials"),
        "AWS_CONFIG_FILE": os.path.join(home, "aws", "config"),
        "XDG_CONFIG_HOME": os.path.join(home, ".config"),
    }
    return env


def _run(binary, args, env):
    try:
        return subprocess.run(
            [binary] + args,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=REQUEST_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return None
    except OSError:
        return None


def _json_stdout(proc):
    if proc is None:
        return None
    text = proc.stdout.decode("utf-8", "replace").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _auth(captured):
    if not captured:
        return ""
    return captured[0]["headers"].get("authorization", "")


def _sigv4_ok(captured, service):
    auth = _auth(captured)
    if not auth.startswith(SIGV4_PREFIX):
        return False, "missing AWS4-HMAC-SHA256 Authorization header"
    marker = "/us-east-1/%s/aws4_request" % service
    if marker not in auth:
        return False, "Authorization missing scope %s" % marker
    if "SignedHeaders=" not in auth:
        return False, "Authorization missing SignedHeaders"
    if not SIG_RE.search(auth):
        return False, "Authorization missing 64-hex Signature"
    if not captured[0]["headers"].get("x-amz-date"):
        return False, "missing X-Amz-Date header"
    return True, ""


# --- canned protocol responses ------------------------------------------------

IAM_USERS_XML = (
    '<ListUsersResponse xmlns="https://iam.amazonaws.com/doc/2010-05-08/">'
    "<ListUsersResult><Users>"
    "<member><UserId>AIDA1</UserId><UserName>alice</UserName>"
    "<Arn>arn:aws:iam::1:user/alice</Arn><Path>/</Path>"
    "<CreateDate>2024-01-01T00:00:00Z</CreateDate></member>"
    "<member><UserId>AIDA2</UserId><UserName>bob</UserName>"
    "<Arn>arn:aws:iam::1:user/bob</Arn><Path>/</Path>"
    "<CreateDate>2024-01-02T00:00:00Z</CreateDate></member>"
    "</Users><IsTruncated>false</IsTruncated></ListUsersResult>"
    "<ResponseMetadata><RequestId>req-1</RequestId></ResponseMetadata></ListUsersResponse>"
)

DYNAMO_JSON = json.dumps({"TableNames": ["alpha", "beta"]})

LAMBDA_PAGE1 = json.dumps({
    "Functions": [{"FunctionName": "fn-1", "Runtime": "go1.x"}],
    "NextMarker": "TOK2",
})
LAMBDA_PAGE2 = json.dumps({
    "Functions": [{"FunctionName": "fn-2", "Runtime": "go1.x"}],
})

S3_XML = (
    "<ListAllMyBucketsResult><Buckets>"
    "<Bucket><Name>bkt-one</Name>"
    "<CreationDate>2024-01-01T00:00:00.000Z</CreationDate></Bucket>"
    "</Buckets></ListAllMyBucketsResult>"
)


class Probe:
    def __init__(self, observation_id, func):
        self.observation_id = observation_id
        self.func = func


def probe_query_sigv4(ctx):
    server = _Server([{"status": 200, "content_type": "text/xml", "body": IAM_USERS_XML}])
    try:
        proc = _run(ctx.binary, ["fetch", "iam-users", "--region", "us-east-1",
                                 "--endpoint-url", server.url()], ctx.env)
        payload = _json_stdout(proc)
        if proc is None or proc.returncode != 0:
            return False, "fetch iam-users did not succeed (rc=%s)" % (
                None if proc is None else proc.returncode)
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            return False, "stdout is not the declared {items,next_token} object"
        if len(payload["items"]) != 2:
            return False, "expected 2 items, got %r" % (payload.get("items"),)
        text = json.dumps(payload)
        if "alice" not in text or "bob" not in text:
            return False, "parsed items missing expected user names"
        ok, detail = _sigv4_ok(server.captured, "iam")
        if not ok:
            return False, detail
        request = server.captured[0]
        combined = request["path"] + " " + request["body"]
        if "Action=ListUsers" not in combined or "Version=" not in combined:
            return False, "query request missing Action/Version"
        return True, "query protocol list + SigV4 verified"
    finally:
        server.stop()


def probe_json_target(ctx):
    server = _Server([{"status": 200, "content_type": "application/x-amz-json-1.0",
                       "body": DYNAMO_JSON}])
    try:
        proc = _run(ctx.binary, ["fetch", "dynamodb-tables", "--region", "us-east-1",
                                 "--endpoint-url", server.url()], ctx.env)
        payload = _json_stdout(proc)
        if proc is None or proc.returncode != 0:
            return False, "fetch dynamodb-tables did not succeed (rc=%s)" % (
                None if proc is None else proc.returncode)
        if not isinstance(payload, dict) or len(payload.get("items") or []) != 2:
            return False, "expected 2 items, got %r" % (payload,)
        text = json.dumps(payload)
        if "alpha" not in text or "beta" not in text:
            return False, "parsed items missing expected table names"
        target = server.captured[0]["headers"].get("x-amz-target", "") if server.captured else ""
        if target != "DynamoDB_20120810.ListTables":
            return False, "unexpected X-Amz-Target header %r" % target
        ok, detail = _sigv4_ok(server.captured, "dynamodb")
        if not ok:
            return False, detail
        return True, "json protocol + X-Amz-Target + SigV4 verified"
    finally:
        server.stop()


def probe_rest_json(ctx):
    server = _Server([{"status": 200, "content_type": "application/json",
                       "body": json.dumps({"Functions": [{"FunctionName": "fn-a", "Runtime": "go1.x"}]})}])
    try:
        proc = _run(ctx.binary, ["fetch", "lambda-functions", "--region", "us-east-1",
                                 "--endpoint-url", server.url()], ctx.env)
        payload = _json_stdout(proc)
        if proc is None or proc.returncode != 0:
            return False, "fetch lambda-functions did not succeed (rc=%s)" % (
                None if proc is None else proc.returncode)
        if not isinstance(payload, dict) or len(payload.get("items") or []) != 1:
            return False, "expected 1 item, got %r" % (payload,)
        if "fn-a" not in json.dumps(payload):
            return False, "parsed item missing function name"
        request = server.captured[0] if server.captured else {}
        if request.get("method") != "GET" or not request.get("path", "").startswith("/2015-03-31/functions"):
            return False, "unexpected rest-json request %s %s" % (
                request.get("method"), request.get("path"))
        ok, detail = _sigv4_ok(server.captured, "lambda")
        if not ok:
            return False, detail
        return True, "rest-json protocol verified"
    finally:
        server.stop()


def probe_rest_xml(ctx):
    server = _Server([{"status": 200, "content_type": "text/xml", "body": S3_XML}])
    try:
        proc = _run(ctx.binary, ["fetch", "s3-buckets", "--region", "us-east-1",
                                 "--endpoint-url", server.url()], ctx.env)
        payload = _json_stdout(proc)
        if proc is None or proc.returncode != 0:
            return False, "fetch s3-buckets did not succeed (rc=%s)" % (
                None if proc is None else proc.returncode)
        if not isinstance(payload, dict) or len(payload.get("items") or []) != 1:
            return False, "expected 1 item, got %r" % (payload,)
        if "bkt-one" not in json.dumps(payload):
            return False, "parsed item missing bucket name"
        request = server.captured[0] if server.captured else {}
        if request.get("method") != "GET" or request.get("path") != "/":
            return False, "unexpected rest-xml request %s %s" % (
                request.get("method"), request.get("path"))
        ok, detail = _sigv4_ok(server.captured, "s3")
        if not ok:
            return False, detail
        return True, "rest-xml protocol verified"
    finally:
        server.stop()


def probe_pagination(ctx):
    # Page 1 advertises a next token.
    server1 = _Server([{"status": 200, "content_type": "application/json", "body": LAMBDA_PAGE1}])
    try:
        proc = _run(ctx.binary, ["fetch", "lambda-functions", "--region", "us-east-1",
                                 "--endpoint-url", server1.url()], ctx.env)
        payload = _json_stdout(proc)
        if proc is None or proc.returncode != 0:
            return False, "first page did not succeed (rc=%s)" % (
                None if proc is None else proc.returncode)
        if not isinstance(payload, dict) or str(payload.get("next_token")) != "TOK2":
            return False, "expected next_token TOK2, got %r" % (payload.get("next_token") if isinstance(payload, dict) else payload)
    finally:
        server1.stop()

    # Page 2 must send the token as the configured input token and clear it.
    server2 = _Server([{"status": 200, "content_type": "application/json", "body": LAMBDA_PAGE2}])
    try:
        proc = _run(ctx.binary, ["fetch", "lambda-functions", "--region", "us-east-1",
                                 "--page-token", "TOK2", "--endpoint-url", server2.url()], ctx.env)
        payload = _json_stdout(proc)
        if proc is None or proc.returncode != 0:
            return False, "second page did not succeed (rc=%s)" % (
                None if proc is None else proc.returncode)
        request = server2.captured[0] if server2.captured else {}
        if "Marker=TOK2" not in request.get("path", ""):
            return False, "second request did not send the input token: %r" % request.get("path")
        if "fn-2" not in json.dumps(payload):
            return False, "second page missing expected item"
        if str(payload.get("next_token")) != "":
            return False, "expected empty next_token on last page, got %r" % payload.get("next_token")
        return True, "pagination input/output tokens verified"
    finally:
        server2.stop()


def probe_readonly_guard(ctx):
    # Read-only: the action must never reach the endpoint.
    server = _Server([{"status": 200, "content_type": "application/x-amz-json-1.0", "body": "{}"}])
    try:
        proc = _run(ctx.binary, ["--readonly", "act", "dynamodb-tables", "delete_table",
                                 "mytable", "--region", "us-east-1",
                                 "--endpoint-url", server.url()], ctx.env)
        if proc is None:
            return False, "read-only action timed out"
        if proc.returncode == 0:
            return False, "read-only action unexpectedly succeeded"
        if server.captured:
            return False, "read-only action sent %d request(s) to the endpoint" % len(server.captured)
    finally:
        server.stop()

    # Control: without --readonly the same action must reach the endpoint.
    server2 = _Server([{"status": 200, "content_type": "application/x-amz-json-1.0", "body": "{}"}])
    try:
        proc = _run(ctx.binary, ["act", "dynamodb-tables", "delete_table", "mytable",
                                 "--region", "us-east-1", "--endpoint-url", server2.url()], ctx.env)
        if proc is None or proc.returncode != 0:
            return False, "control action did not succeed (rc=%s)" % (
                None if proc is None else proc.returncode)
        if len(server2.captured) != 1:
            return False, "control action expected 1 request, got %d" % len(server2.captured)
        target = server2.captured[0]["headers"].get("x-amz-target", "")
        if not target.endswith(".DeleteTable"):
            return False, "control action missing DeleteTable target (%r)" % target
    finally:
        server2.stop()

    # An action belonging to another EC2 resource must not be dispatched merely
    # because both resources share the same service.
    server3 = _Server([{"content_type": "text/xml", "body": "<DeleteVolumeResponse/>"}])
    try:
        proc = _run(ctx.binary, ["act", "ec2-instances", "delete_volume", "vol-test",
                                 "--region", "us-east-1", "--endpoint-url", server3.url()], ctx.env)
        if proc is None or proc.returncode == 0 or server3.captured:
            return False, "act dispatched an action not configured for the requested resource"
        return True, "read-only and resource-specific action guards verified with write control"
    finally:
        server3.stop()


def probe_endpoint_precedence(ctx):
    # --endpoint-url must win over a dead AWS_ENDPOINT_URL.
    server = _Server([{"status": 200, "content_type": "text/xml", "body": IAM_USERS_XML}])
    try:
        env = dict(ctx.env)
        env["AWS_ENDPOINT_URL"] = "http://127.0.0.1:1"
        proc = _run(ctx.binary, ["fetch", "iam-users", "--region", "us-east-1",
                                 "--endpoint-url", server.url()], env)
        if proc is None or proc.returncode != 0:
            return False, "--endpoint-url did not override AWS_ENDPOINT_URL (rc=%s)" % (
                None if proc is None else proc.returncode)
        if len(server.captured) != 1:
            return False, "flag endpoint received %d requests" % len(server.captured)
    finally:
        server.stop()

    # Without the flag, AWS_ENDPOINT_URL must be honored.
    server2 = _Server([{"status": 200, "content_type": "text/xml", "body": IAM_USERS_XML}])
    try:
        env = dict(ctx.env)
        env["AWS_ENDPOINT_URL"] = server2.url()
        proc = _run(ctx.binary, ["fetch", "iam-users", "--region", "us-east-1"], env)
        if proc is None or proc.returncode != 0:
            return False, "AWS_ENDPOINT_URL was not honored (rc=%s)" % (
                None if proc is None else proc.returncode)
        if len(server2.captured) != 1:
            return False, "env endpoint received %d requests" % len(server2.captured)
        return True, "endpoint override precedence verified"
    finally:
        server2.stop()


def probe_credential_chain(ctx):
    # Environment keys precede a named profile's static keys too. Use the
    # public CLI and inspect the actual signing identity, without depending
    # on Go package names or a candidate-authored test name.
    profile = "quality-named-profile"
    os.makedirs(os.path.dirname(ctx.env["AWS_SHARED_CREDENTIALS_FILE"]), exist_ok=True)
    with open(ctx.env["AWS_SHARED_CREDENTIALS_FILE"], "w", encoding="utf-8") as handle:
        handle.write("[%s]\naws_access_key_id=FILEKEY\naws_secret_access_key=file-secret\n" % profile)
    with open(ctx.env["AWS_CONFIG_FILE"], "w", encoding="utf-8") as handle:
        handle.write("[profile %s]\nregion=us-east-1\n" % profile)
    for label, settings in (
        ("static", ""),
        ("SSO", "sso_start_url=https://example.awsapps.com/start\nsso_region=us-east-1\n"
                "sso_account_id=123456789012\nsso_role_name=Example\n"),
        ("console login", "login_session=quality-session\n"),
        ("role", "role_arn=arn:aws:iam::123456789012:role/example\nsource_profile=missing-source\n"),
    ):
        with open(ctx.env["AWS_CONFIG_FILE"], "w", encoding="utf-8") as handle:
            handle.write("[profile %s]\nregion=us-east-1\n%s" % (profile, settings))
        server = _Server([{"content_type": "text/xml", "body": IAM_USERS_XML}])
        try:
            proc = _run(ctx.binary, ["fetch", "iam-users", "--profile", profile,
                                     "--region", "us-east-1", "--endpoint-url", server.url()], ctx.env)
            if proc is None or proc.returncode != 0 or len(server.captured) != 1:
                return False, "named-profile %s configuration overrode environment credentials" % label
            if "Credential=%s/" % AWS_ACCESS_KEY not in _auth(server.captured):
                return False, "named-profile %s credentials overrode environment credentials" % label
        finally:
            server.stop()
    return True, "environment credentials precede named-profile static, SSO, console and role sources"


def probe_completion(ctx):
    # The contract permits different script text. Check successful generation,
    # bash syntax/registration and the declared fallback through the public CLI.
    for shell in ("bash", "zsh", "fish", "powershell", "elvish", "unknown-shell"):
        proc = _run(ctx.binary, ["completion", shell], ctx.env)
        if proc is None or proc.returncode != 0 or not proc.stdout.strip():
            return False, "completion %s did not generate a script" % shell
        if shell == "bash":
            syntax = subprocess.run(["bash", "-n"], input=proc.stdout,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
            if syntax.returncode != 0:
                return False, "bash completion contains invalid shell syntax"
            if not re.search(rb"(?:^|\s)complete\s", proc.stdout):
                return False, "bash completion does not register a completion"
    for args in (["completion"], ["completion", "bash", "extra"]):
        proc = _run(ctx.binary, args, ctx.env)
        if proc is None or proc.returncode == 0:
            return False, "completion did not enforce its single shell argument"
    return True, "completion shells, unknown-shell fallback and argument count verified"


def probe_configuration(ctx):
    # A missing/invalid/unreadable preferences file must warn and use defaults.
    # The warning may be sent to diagnostics or a log file; neither wording
    # nor log filename is specified by the public contract.
    for name, contents in (("missing", None), ("invalid", "profile: [unterminated\n"),
                           ("unreadable", None)):
        with tempfile.TemporaryDirectory(prefix="config-", dir=ctx.home) as home:
            env = _env(home)
            directory = os.path.join(env["XDG_CONFIG_HOME"], "taws")
            os.makedirs(directory)
            config = os.path.join(directory, "config.yaml")
            if name == "unreadable":
                os.mkdir(config)  # Reading a directory as a file fails even for root.
            elif contents is not None:
                with open(config, "w", encoding="utf-8") as handle:
                    handle.write(contents)
            server = _Server([{"content_type": "text/xml", "body": IAM_USERS_XML}])
            try:
                proc = _run(ctx.binary, ["fetch", "iam-users", "--log-level", "warn",
                                         "--endpoint-url", server.url()], env)
                if proc is None or proc.returncode != 0 or len(server.captured) != 1:
                    return False, "%s preferences did not fall back to defaults" % name
                if len((_json_stdout(proc) or {}).get("items", [])) != 2:
                    return False, "%s preferences broke fetch output" % name
                diagnostic = proc.stdout.decode("utf-8", "replace") + proc.stderr.decode("utf-8", "replace")
                for base, _, files in os.walk(home):
                    for filename in files:
                        if filename.endswith(".log"):
                            with open(os.path.join(base, filename), "r", encoding="utf-8", errors="replace") as handle:
                                diagnostic += handle.read()
                if not re.search(r"\bwarn(?:ing)?\b", diagnostic, re.IGNORECASE):
                    return False, "%s preferences produced no warning" % name
                ok, detail = _sigv4_ok(server.captured, "iam")
                if not ok:
                    return False, detail
            finally:
                server.stop()
    return True, "missing, invalid and unreadable preferences warn and use defaults"


PROBES = [
    Probe("F2P-009", probe_completion),
    Probe("F2P-012", probe_credential_chain),
    Probe("F2P-005", probe_configuration),
    Probe("F2P-025", probe_query_sigv4),
    Probe("F2P-026", probe_json_target),
    Probe("F2P-027", probe_rest_json),
    Probe("F2P-028", probe_rest_xml),
    Probe("F2P-029", probe_pagination),
    Probe("F2P-030", probe_readonly_guard),
    Probe("F2P-031", probe_endpoint_precedence),
]


class _Context:
    def __init__(self, binary, home):
        self.binary = binary
        self.home = home
        self.env = _env(home)


def main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--bin", required=True)
    parser.add_argument("--records", required=True)
    args = parser.parse_args(argv[1:])

    binary = os.path.abspath(args.bin)
    with tempfile.TemporaryDirectory(prefix="taws-f2p-net-") as work:
        context = _Context(binary, work)
        with open(args.records, "a", encoding="utf-8") as sink:
            for probe in PROBES:
                if not os.path.isfile(binary):
                    record = {"observation_id": probe.observation_id, "status": "fail",
                              "detail": "candidate binary was not built"}
                else:
                    try:
                        passed, detail = probe.func(context)
                        record = {"observation_id": probe.observation_id,
                                  "status": "pass" if passed else "fail", "detail": detail}
                    except Exception as exc:  # never let a probe crash the harness
                        record = {"observation_id": probe.observation_id, "status": "fail",
                                  "detail": "probe error: %s" % exc}
                sink.write(json.dumps(record) + "\n")
                sink.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
